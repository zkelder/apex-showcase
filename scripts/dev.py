from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.engine import Engine

    from apex.adapters.alpaca_market_data import AlpacaMarketDataHttpAdapter
    from apex.adapters.postgres import PostgresSettings
    from apex.adapters.real_securities_postgres import PostgresRealSecuritiesDataRepository
    from apex.adapters.securities_paper_postgres import (
        PostgresSecuritiesPaperCampaignBoundary,
        PostgresSecuritiesPaperRepository,
    )
    from apex.adapters.sustained_shadow_postgres import PostgresSustainedShadowRepository
    from apex.application.securities import SecuritiesModelSignalBuilder
    from apex.application.strategy_lab import StrategyLabService
    from apex.domain.real_securities_data import SecuritiesProviderConfiguration
    from apex.domain.sustained_shadow import SustainedShadowPlan

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

_DISPOSABLE_DEMO_DATABASE_PREFIX = "apex_demo_"
_PROTECTED_DATABASE_NAMES = frozenset({"apex", "apex_test", "postgres", "template0", "template1"})
_DISPOSABLE_DEMO_REQUIRED_TABLES = frozenset(
    {
        "operator_action",
        "strategy_evidence_review_policy",
        "strategy_evidence_snapshot",
        "evidence_criterion_result",
        "strategy_evidence_review",
        "paper_trial_authorization",
        "paper_trial_lineage",
    }
)
_DISPOSABLE_DEMO_REQUIRED_COLUMNS = {
    "paper_trial_authorization": frozenset(
        {
            "authorization_id",
            "review_id",
            "operator_action_id",
            "maximum_paper_cycles",
            "maximum_paper_orders",
        }
    ),
    "paper_trial_lineage": frozenset(
        {"lineage_id", "authorization_id", "entity_type", "entity_id"}
    ),
}


class DisposableDemoDatabaseError(RuntimeError):
    """The isolated fixture-demo database lifecycle could not proceed safely."""


class DisposableDemoSchemaPreflightError(DisposableDemoDatabaseError):
    """The empty disposable database was not migrated to the expected schema."""

    def __init__(
        self,
        *,
        expected_revision: str,
        observed_revisions: tuple[str, ...],
        missing_tables: tuple[str, ...],
        missing_columns: tuple[str, ...],
        migration_heads: tuple[str, ...],
    ) -> None:
        self.expected_revision = expected_revision
        self.observed_revisions = observed_revisions
        self.missing_tables = missing_tables
        self.missing_columns = missing_columns
        self.migration_heads = migration_heads
        observed = ",".join(observed_revisions) if observed_revisions else "absent"
        missing = ",".join((*missing_tables, *missing_columns)) or "none"
        heads = ",".join(migration_heads) if migration_heads else "absent"
        super().__init__(
            "lifecycle_stage=schema_preflight "
            f"expected_revision={expected_revision} observed_revision={observed} "
            f"migration_heads={heads} missing={missing}"
        )


def _python() -> str:
    if VENV_PYTHON.exists():
        return str(VENV_PYTHON)
    return sys.executable


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path if not current else os.pathsep.join((source_path, current))
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _run(arguments: Sequence[str], *, environment: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        list(arguments),
        cwd=ROOT,
        env=environment or _environment(),
        check=False,
    )
    if completed.returncode:
        raise SystemExit(completed.returncode)


def bootstrap() -> None:
    uv = shutil.which("uv")
    if uv is not None:
        _run((uv, "sync", "--extra", "dev", "--extra", "persistence", "--locked"))
        return
    _run(
        (
            sys.executable,
            "-m",
            "uv",
            "sync",
            "--extra",
            "dev",
            "--extra",
            "persistence",
            "--locked",
        )
    )


def format_code() -> None:
    _run((_python(), "-m", "ruff", "format", "."))
    _run((_python(), "-m", "ruff", "check", "--fix", "."))


def lint() -> None:
    _run((_python(), "-m", "ruff", "format", "--check", "."))
    _run((_python(), "-m", "ruff", "check", "."))


def typecheck() -> None:
    _run(
        (
            _python(),
            "-m",
            "mypy",
            "src",
            "scripts/dev.py",
            "research/weather_gfs_feasibility",
        )
    )


def test() -> None:
    _run((_python(), "-m", "pytest"))


def test_offline() -> None:
    code = """
import socket

def denied(*args, **kwargs):
    raise RuntimeError("outbound networking is disabled")

socket.create_connection = denied
socket.getaddrinfo = denied
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.socket.sendto = denied

import pytest
raise SystemExit(pytest.main([]))
"""
    environment = _environment()
    environment["APEX_OFFLINE"] = "1"
    _run((_python(), "-c", code), environment=environment)


def verify_core() -> None:
    lint()
    typecheck()
    test_offline()


def _docker() -> str:
    executable = shutil.which("docker") or shutil.which("docker.exe")
    if executable is None:
        raise SystemExit("Docker CLI is unavailable; install it or enable WSL integration.")
    return executable


def _compose(*arguments: str) -> None:
    _run((_docker(), "compose", "-f", str(ROOT / "compose.yaml"), *arguments))


def db_up() -> None:
    """Start PostgreSQL once, then wait through its brief healthy-starting phase."""

    _compose("up", "-d", "postgres")
    deadline = time.monotonic() + 30
    readiness = (
        _docker(),
        "compose",
        "-f",
        str(ROOT / "compose.yaml"),
        "exec",
        "-T",
        "postgres",
        "pg_isready",
        "-U",
        "apex",
        "-d",
        "apex",
    )
    while time.monotonic() < deadline:
        completed = subprocess.run(
            readiness,
            cwd=ROOT,
            env=_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            return
        time.sleep(0.5)
    raise SystemExit("PostgreSQL did not become ready within 30 seconds")


def db_down(*, volumes: bool) -> None:
    arguments = ["down"]
    if volumes:
        arguments.append("--volumes")
    _compose(*arguments)


def db_identity() -> None:
    """Print the Postgres/Compose target `db-up`/`verify-persistence`/`db-down`
    would actually reach right now, without mutating anything.

    Read-only: at most two `SELECT`-shaped probes (connectivity, then the
    schema-compatibility read), no DDL, no DROP/CREATE. Intended as the
    "inspect before mutation" step documented in
    docs/WORKTREE_VERIFICATION.md -- run it after sourcing
    scripts/dev-worktree-env.sh to confirm the isolated identity that
    resolved, or run it beforehand to see the unmodified defaults. Also the
    fastest way to answer "would an unattended worker start against this
    target right now" -- see docs/MIGRATION_COMPATIBILITY_GUARD_V1.md.
    """

    from apex.adapters.postgres import PostgresSettings, PostgresUnavailableError, check_postgres

    settings = PostgresSettings.from_env()
    compose_project = os.environ.get("COMPOSE_PROJECT_NAME")
    resolved_project = compose_project or f"(unset -- Compose defaults to '{ROOT.name}')"
    print(f"repository root         {ROOT}")
    print(f"compose project name    {resolved_project}")
    print(f"postgres host:port      {settings.host}:{settings.port}")
    print(f"primary database name   {settings.database}")
    print(f"test database name      {settings.test_database}")
    try:
        check_postgres(settings)
        reachable = "yes"
    except PostgresUnavailableError:
        reachable = "no"
    print(f"reachable right now     {reachable}")
    print(f"schema compatibility    {_describe_schema_compatibility(settings)}")


def _describe_schema_compatibility(settings: PostgresSettings) -> str:
    """Non-raising: a one-line, credential-free description of
    ``settings``'s current schema-compatibility state for `db-identity`'s
    read-only report. Never used by a worker's own startup guard -- that
    is `apex.adapters.schema_compatibility_postgres.require_compatible_schema`,
    which raises instead of describing."""

    from apex.adapters.postgres import PostgresUnavailableError, create_postgres_engine
    from apex.adapters.schema_compatibility_postgres import fetch_observed_schema_revisions
    from apex.domain.schema_compatibility import (
        REQUIRED_SCHEMA_REVISION,
        evaluate_schema_compatibility,
    )

    engine = create_postgres_engine(settings)
    try:
        observed = fetch_observed_schema_revisions(engine)
    except PostgresUnavailableError:
        return "unknown (database unreachable)"
    finally:
        engine.dispose()
    report = evaluate_schema_compatibility(
        required_revision=REQUIRED_SCHEMA_REVISION,
        observed_revisions=observed,
        worker_name="db-identity",
    )
    return report.detail


def disposable_demo_database_name(
    *, process_id: int | None = None, token: str | None = None
) -> str:
    """Return a safe, unique local-only name for one fixture demo database."""

    suffix = token or uuid.uuid4().hex[:16]
    if not suffix.isascii() or not suffix.isalnum():
        raise ValueError("disposable demo database token must be ASCII alphanumeric")
    identifier = f"{_DISPOSABLE_DEMO_DATABASE_PREFIX}{process_id or os.getpid()}_{suffix.lower()}"
    _validate_disposable_demo_database_name(identifier)
    return identifier


def _validate_disposable_demo_database_name(database: str) -> None:
    if (
        database in _PROTECTED_DATABASE_NAMES
        or not database.startswith(_DISPOSABLE_DEMO_DATABASE_PREFIX)
        or len(database) > 63
        or not database.isascii()
        or not database.replace("_", "").isalnum()
        or not database[0].islower()
    ):
        raise DisposableDemoDatabaseError("refusing an unsafe disposable demo database name")


def _validate_disposable_demo_settings(settings: PostgresSettings) -> None:
    # Loopback-only, not literally port 55432: a linked git worktree may
    # legitimately run its own isolated local Postgres on a different port
    # (see scripts/dev-worktree-env.sh / docs/WORKTREE_VERIFICATION.md).
    # `settings` always comes from `PostgresSettings.from_env()`, the same
    # environment-driven source every other command already trusts for its
    # port -- the safety property this guards is "definitely local, not an
    # unrelated remote host", which the host check alone already provides.
    if settings.host != "127.0.0.1":
        raise DisposableDemoDatabaseError(
            "disposable demo requires the project-local PostgreSQL host"
        )
    _validate_disposable_demo_database_name(settings.database)


def _database_environment(settings: PostgresSettings) -> dict[str, str]:
    """Build a child-process environment for exactly one explicit database."""

    environment = _environment()
    environment.update(
        {
            "APEX_POSTGRES_HOST": settings.host,
            "APEX_POSTGRES_PORT": str(settings.port),
            "APEX_POSTGRES_DB": settings.database,
            "APEX_POSTGRES_USER": settings.user,
            "APEX_POSTGRES_PASSWORD": settings.password,
            "APEX_POSTGRES_TEST_DB": settings.test_database,
        }
    )
    return environment


def _admin_connection(settings: PostgresSettings) -> Any:
    import psycopg

    return psycopg.connect(
        host=settings.host,
        port=settings.port,
        dbname="postgres",
        user=settings.user,
        password=settings.password,
        autocommit=True,
    )


def _database_exists(settings: PostgresSettings, database: str) -> bool:
    with _admin_connection(settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)", (database,))
        row = cursor.fetchone()
    return bool(row and row[0])


def _protected_database_state(settings: PostgresSettings) -> tuple[tuple[str, int, str], ...]:
    """Capture immutable catalog identity without opening either protected database."""

    protected = tuple(sorted({"apex", settings.test_database}))
    with _admin_connection(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT datname, oid, datcollversion "
            "FROM pg_database WHERE datname = ANY(%s) ORDER BY datname",
            (list(protected),),
        )
        rows = tuple((str(name), int(oid), str(version or "")) for name, oid, version in cursor)
    if tuple(row[0] for row in rows) != protected:
        raise DisposableDemoDatabaseError("protected Apex databases are not available")
    return rows


def _create_disposable_demo_database(settings: PostgresSettings) -> None:
    """Create one empty database; never clone, reset, or stamp an existing one."""

    from psycopg import sql

    _validate_disposable_demo_settings(settings)
    if _database_exists(settings, settings.database):
        raise DisposableDemoDatabaseError("disposable demo database name already exists")
    with _admin_connection(settings) as connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.database)))


def _drop_disposable_demo_database(settings: PostgresSettings) -> None:
    """Terminate and drop only the exact disposable database used by this run."""

    from psycopg import sql

    _validate_disposable_demo_settings(settings)
    with _admin_connection(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (settings.database,),
        )
        cursor.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(settings.database))
        )


def _alembic_config(connection: Any) -> Config:
    from alembic.config import Config

    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _migrate_disposable_demo_database(settings: PostgresSettings) -> None:
    """Run the ordinary Alembic base-to-head path against the explicit database."""

    from alembic import command

    from apex.adapters.postgres import create_postgres_engine

    _validate_disposable_demo_settings(settings)
    engine = create_postgres_engine(settings)
    try:
        with engine.connect() as connection:
            command.upgrade(_alembic_config(connection), "head")
            connection.commit()
    finally:
        engine.dispose()


def _disposable_demo_migration_heads() -> tuple[str, ...]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return tuple(sorted(ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads()))


def _schema_preflight_disposable_demo_database(settings: PostgresSettings) -> str:
    """Fail closed before seed services can touch a mismigrated demo database."""

    from apex.adapters.postgres import create_postgres_engine

    _validate_disposable_demo_settings(settings)
    migration_heads = _disposable_demo_migration_heads()
    expected_revision = migration_heads[0] if len(migration_heads) == 1 else "single-head-required"
    engine = create_postgres_engine(settings)
    try:
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            table_names = frozenset(inspector.get_table_names())
            observed_revisions: tuple[str, ...] = ()
            if "alembic_version" in table_names:
                observed_revisions = tuple(
                    sorted(
                        str(value)
                        for value in connection.scalars(
                            sa.text("SELECT version_num FROM alembic_version")
                        )
                    )
                )
            missing_tables = tuple(sorted(_DISPOSABLE_DEMO_REQUIRED_TABLES - table_names))
            missing_columns = tuple(
                sorted(
                    f"{table_name}.{column_name}"
                    for table_name, required_columns in _DISPOSABLE_DEMO_REQUIRED_COLUMNS.items()
                    for column_name in required_columns
                    if table_name not in table_names
                    or column_name
                    not in {column["name"] for column in inspector.get_columns(table_name)}
                )
            )
    finally:
        engine.dispose()
    if (
        len(migration_heads) != 1
        or observed_revisions != (expected_revision,)
        or missing_tables
        or missing_columns
    ):
        raise DisposableDemoSchemaPreflightError(
            expected_revision=expected_revision,
            observed_revisions=observed_revisions,
            missing_tables=missing_tables,
            missing_columns=missing_columns,
            migration_heads=migration_heads,
        )
    return expected_revision


def _postgres_was_running() -> bool:
    completed = subprocess.run(
        (_docker(), "compose", "-f", str(ROOT / "compose.yaml"), "ps", "-q", "postgres"),
        cwd=ROOT,
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _with_capture_database(action: Sequence[str], *, migrate_to_head: bool = False) -> None:
    was_running = _postgres_was_running()
    if not was_running:
        db_up()
    try:
        if migrate_to_head:
            migrate()
        _run(action)
    finally:
        if not was_running:
            db_down(volumes=False)


def _frontend() -> Path:
    return ROOT / "frontend"


def operator_build() -> None:
    """Produce the ignored same-origin static Operator Console bundle."""

    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is unavailable; install the WSL Node.js development prerequisite.")
    _run((npm, "--prefix", str(_frontend()), "ci"))
    _run((npm, "--prefix", str(_frontend()), "run", "build"))


def operator_api() -> None:
    _with_capture_database(
        (
            _python(),
            "-m",
            "uvicorn",
            "apex.operator_api:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ),
        migrate_to_head=True,
    )


def operator_ui() -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit("npm is unavailable; install the WSL Node.js development prerequisite.")
    _run((npm, "--prefix", str(_frontend()), "run", "dev"))


def operator_app() -> None:
    operator_build()
    operator_api()


def operator_demo_seed() -> str:
    """Seed existing fixture-only PAPER evidence without global registration."""

    from apex.adapters.autopilot_fixture import fixture_autopilot_registry
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        postgres_autopilot_store,
        postgres_operator_console_store,
    )
    from apex.application.authorize_order import PersistenceConflictError
    from apex.application.autopilot import (
        AutopilotCycleCommand,
        AutopilotIdempotencyConflictError,
        CampaignControlError,
        RunAutopilotShadowCycle,
    )
    from apex.application.operator_console import (
        CreateMoonshotCampaign,
        CreateMoonshotCampaignCommand,
        OperatorActionType,
        OperatorMode,
    )
    from apex.cli import autopilot_control, autopilot_core_demo, paper_portfolio_demo
    from apex.domain.autopilot import EvidenceTier
    from apex.domain.values import Money, RiskFraction

    try:
        result = paper_portfolio_demo()
        campaign_id = str(result["campaign_id"])
    except PersistenceConflictError:
        # The underlying fixture service deliberately refuses a second distinct
        # settlement.  Its already-retained deterministic campaign is the
        # idempotent seed result rather than an error or a new financial action.
        campaign_id = "paper-portfolio-demo-v4"
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        autopilot_store = postgres_autopilot_store(engine)
        operator_store = postgres_operator_console_store(engine)
        # Existing fixture-only SHADOW evidence is the stable idempotent seed
        # result; do not replay a second financial or control action.
        with contextlib.suppress(PersistenceConflictError, AutopilotIdempotencyConflictError):
            autopilot_core_demo()
        with contextlib.suppress(CampaignControlError):
            RunAutopilotShadowCycle(autopilot_store, fixture_autopilot_registry()).run(
                AutopilotCycleCommand(
                    campaign_id=campaign_id,
                    idempotency_key="operator-console-demo-shadow-v1",
                    maximum_strategies=2,
                    maximum_proposals=4,
                    deadline_at=datetime.now(tz=UTC) + timedelta(seconds=30),
                    operator_note="fixture-only local shadow demonstration",
                    requested_at=datetime.now(tz=UTC),
                )
            )
        operator_store.adopt_fixture_demo_campaign(
            campaign_id=campaign_id,
            name="Fixture-only PAPER portfolio demonstration",
            operating_mode=OperatorMode.PAPER,
            note="deterministic local Operator Console seed; no production strategy registration",
        )
        operator_store.adopt_fixture_demo_campaign(
            campaign_id="autopilot-core-demo-v2",
            name="Fixture-only SHADOW strategy demonstration",
            operating_mode=OperatorMode.SHADOW,
            note="deterministic local SHADOW evidence; no production strategy registration",
        )

        now = datetime.now(tz=UTC)

        def ensure_console_campaign(
            *, campaign_id: str, name: str, mode: OperatorMode, idempotency_key: str
        ) -> None:
            CreateMoonshotCampaign(operator_store).create(
                CreateMoonshotCampaignCommand(
                    campaign_id=campaign_id,
                    name=name,
                    initial_capital=Money("1000"),
                    maximum_authorized_loss=Money("500"),
                    starts_at=now,
                    expires_at=now + timedelta(days=1),
                    operating_mode=mode,
                    maximum_active_risk_fraction=RiskFraction("0.5"),
                    maximum_per_opportunity_risk_fraction=RiskFraction("0.1"),
                    maximum_per_strategy_risk_fraction=RiskFraction("0.25"),
                    maximum_per_correlation_bucket_risk_fraction=RiskFraction("0.25"),
                    maximum_simultaneous_reservations=4,
                    maximum_simultaneous_positions=4,
                    maximum_capital_lockup_seconds=3600,
                    minimum_conservative_expected_value=Money("0"),
                    minimum_evidence_tier=EvidenceTier.RESEARCH,
                    allow_profit_compounding=False,
                    operator_note="deterministic local console state example",
                    idempotency_key=idempotency_key,
                )
            )

        ensure_console_campaign(
            campaign_id="operator-console-running-shadow-v1",
            name="Local RUNNING SHADOW example",
            mode=OperatorMode.SHADOW,
            idempotency_key="operator-console-running-shadow-create-v1",
        )
        ensure_console_campaign(
            campaign_id="operator-console-paused-paper-v1",
            name="Local PAUSED PAPER example",
            mode=OperatorMode.PAPER,
            idempotency_key="operator-console-paused-paper-create-v1",
        )
        paused = autopilot_control(
            campaign_id="operator-console-paused-paper-v1",
            action="PAUSE",
            reason="deterministic local paused-state example",
            idempotency_key="operator-console-paused-paper-pause-v1",
        )
        operator_store.record_action(
            campaign_id="operator-console-paused-paper-v1",
            action_type=OperatorActionType.PAUSE_CAMPAIGN,
            idempotency_key="operator-console-paused-paper-pause-v1",
            operator_note="deterministic local paused-state example",
            target_entity_ids=("operator-console-paused-paper-v1",),
            reason_codes=("CAMPAIGN_PAUSED",),
            request={"action": "PAUSE"},
            result=paused,
        )
    finally:
        engine.dispose()
    return campaign_id


def evidence_review_paper_trial_demo() -> None:
    """Run the fixture-only proof in one newly-created isolated PostgreSQL database.

    This deliberately does not use ``apex_test``.  The normal test lifecycle
    can retain intentionally old migration state while this proof needs a
    verifiable base-to-head migration and a fresh application process bound to
    that same physical database.
    """

    from apex.adapters.postgres import PostgresSettings

    was_running = _postgres_was_running()
    if not was_running:
        db_up()
    disposable_settings: PostgresSettings | None = None
    protected_before: tuple[tuple[str, int, str], ...] | None = None
    primary_error: BaseException | None = None
    try:
        base_settings = PostgresSettings.from_env()
        disposable_settings = replace(
            base_settings,
            database=disposable_demo_database_name(),
        )
        _validate_disposable_demo_settings(disposable_settings)
        protected_before = _protected_database_state(base_settings)
        print(
            "lifecycle_stage=create_empty "
            f"database={disposable_settings.database} host={disposable_settings.host} "
            f"port={disposable_settings.port}"
        )
        _create_disposable_demo_database(disposable_settings)
        print(f"lifecycle_stage=alembic_upgrade database={disposable_settings.database}")
        _migrate_disposable_demo_database(disposable_settings)
        print(f"lifecycle_stage=schema_preflight database={disposable_settings.database}")
        revision = _schema_preflight_disposable_demo_database(disposable_settings)
        print(
            "lifecycle_stage=schema_preflight_passed "
            f"database={disposable_settings.database} revision={revision} "
            f"tables={','.join(sorted(_DISPOSABLE_DEMO_REQUIRED_TABLES))}"
        )
        print(f"lifecycle_stage=run_demo database={disposable_settings.database}")
        _run(
            (_python(), "-m", "apex.cli", "evidence-review-paper-trial-demo"),
            environment=_database_environment(disposable_settings),
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if disposable_settings is not None:
            try:
                print(f"lifecycle_stage=cleanup database={disposable_settings.database}")
                _drop_disposable_demo_database(disposable_settings)
                if _database_exists(disposable_settings, disposable_settings.database):
                    raise DisposableDemoDatabaseError(
                        "disposable demo database cleanup did not complete"
                    )
                if (
                    protected_before is not None
                    and _protected_database_state(disposable_settings) != protected_before
                ):
                    raise DisposableDemoDatabaseError(
                        "protected Apex database catalog state changed"
                    )
                print(f"lifecycle_stage=cleanup_complete database={disposable_settings.database}")
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                print(
                    "lifecycle_stage=cleanup_failed "
                    f"database={disposable_settings.database} error={type(cleanup_error).__name__}",
                    file=sys.stderr,
                )
        if not was_running:
            try:
                db_down(volumes=False)
            except BaseException as stop_error:
                if primary_error is None:
                    raise
                print(
                    f"lifecycle_stage=postgres_stop_failed error={type(stop_error).__name__}",
                    file=sys.stderr,
                )


def operator_console_demo() -> None:
    """Bounded noninteractive API proof; never exposes a remote listener."""

    was_running = _postgres_was_running()
    if not was_running:
        db_up()
    server: subprocess.Popen[bytes] | None = None
    try:
        migrate()
        campaign_id = operator_demo_seed()
        server = subprocess.Popen(
            (
                _python(),
                "-m",
                "uvicorn",
                "apex.operator_api:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ),
            cwd=ROOT,
            env=_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8000/api/v1/health", timeout=1
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.2)
        else:
            raise SystemExit("Operator Console did not become ready within 15 seconds")
        for path in (
            "/api/v1/capabilities",
            "/api/v1/campaigns",
            f"/api/v1/campaigns/{campaign_id}",
        ):
            with urllib.request.urlopen(f"http://127.0.0.1:8000{path}", timeout=5) as response:
                if response.status != 200:
                    raise SystemExit(f"Operator Console endpoint failed: {path}")
        create_payload = {
            "name": "Operator API idempotency proof",
            "initial_capital": "100",
            "maximum_authorized_loss": "50",
            "starts_at": "2026-07-23T00:00:00Z",
            "expires_at": "2026-07-24T00:00:00Z",
            "operating_mode": "SHADOW",
            "maximum_active_risk_fraction": "0.5",
            "maximum_per_opportunity_risk_fraction": "0.1",
            "maximum_per_strategy_risk_fraction": "0.25",
            "maximum_per_correlation_bucket_risk_fraction": "0.25",
            "maximum_simultaneous_reservations": 4,
            "maximum_simultaneous_positions": 4,
            "maximum_capital_lockup_seconds": 3600,
            "minimum_conservative_expected_value": "0",
            "minimum_evidence_tier": "RESEARCH",
            "allow_profit_compounding": False,
            "operator_note": "bounded API idempotency proof",
        }
        control = urllib.request.Request(
            "http://127.0.0.1:8000/api/v1/campaigns",
            data=json.dumps(create_payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:8000",
                "Idempotency-Key": "operator-console-demo-create-v1",
            },
            method="POST",
        )
        with urllib.request.urlopen(control, timeout=5) as response:
            if response.status != 201:
                raise SystemExit("Operator Console idempotent create failed")
        with urllib.request.urlopen(control, timeout=5) as response:
            if response.status != 201:
                raise SystemExit("Operator Console idempotent replay failed")
        print("Operator Console verified at http://127.0.0.1:8000")
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        if not was_running:
            db_down(volumes=False)


def migrate(*, test_database: bool = False) -> None:
    environment = _environment()
    if test_database:
        test_name = environment.get("APEX_POSTGRES_TEST_DB", "apex_test")
        environment["APEX_POSTGRES_DB"] = test_name
    _run((_python(), "-m", "alembic", "upgrade", "head"), environment=environment)
    _verify_migrated_to_required_revision(test_database=test_database)


def _verify_migrated_to_required_revision(*, test_database: bool) -> None:
    """Mechanizes the "migrate, then verify revision" pair of the
    documented production deployment sequence (see
    docs/MIGRATION_COMPATIBILITY_GUARD_V1.md) so a human running `migrate`
    gets an immediate, explicit answer rather than silent trust that
    `alembic upgrade head` actually reached the one revision the
    application declares it needs -- e.g. a multi-head or partially applied
    migration state `upgrade head` did not resolve as expected. Read-only;
    issues no DDL beyond what `migrate` itself already ran."""

    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.schema_compatibility_postgres import fetch_observed_schema_revisions
    from apex.domain.schema_compatibility import (
        REQUIRED_SCHEMA_REVISION,
        evaluate_schema_compatibility,
    )

    settings = PostgresSettings.from_env()
    if test_database:
        settings = settings.for_test()
    engine = create_postgres_engine(settings)
    try:
        observed = fetch_observed_schema_revisions(engine)
    finally:
        engine.dispose()
    report = evaluate_schema_compatibility(
        required_revision=REQUIRED_SCHEMA_REVISION,
        observed_revisions=observed,
        worker_name="scripts/dev.py migrate",
    )
    if not report.compatible:
        raise SystemExit(
            "migrate ran 'alembic upgrade head' but the database is still not at "
            f"the application's required revision: {report.detail}"
        )
    print(f"migrate: database is at the required revision ({REQUIRED_SCHEMA_REVISION})")


def _prepare_test_database() -> None:
    from apex.adapters.postgres import PostgresSettings, recreate_test_database

    recreate_test_database(PostgresSettings.from_env())
    migrate(test_database=True)


def _validate_persistence_pytest_args(pytest_args: Sequence[str]) -> tuple[str, ...]:
    """Allow the one capture-free certification mode without widening test control."""

    arguments = tuple(pytest_args)
    if arguments not in {(), ("--capture=no",)}:
        raise SystemExit("only --pytest-arg=--capture=no is permitted")
    return arguments


def test_persistence(*, pytest_args: Sequence[str] = ()) -> None:
    _prepare_test_database()
    environment = _environment()
    environment["APEX_POSTGRES_DB"] = environment.get("APEX_POSTGRES_TEST_DB", "apex_test")
    allowed_pytest_args = _validate_persistence_pytest_args(pytest_args)
    _run(
        (
            _python(),
            "-m",
            "pytest",
            "-o",
            "addopts=--strict-config --strict-markers",
            "-m",
            "postgres",
            *allowed_pytest_args,
            "tests/persistence",
        ),
        environment=environment,
    )


def verify_persistence(*, pytest_args: Sequence[str] = ()) -> None:
    from apex.adapters.postgres import PostgresSettings, check_postgres

    check_postgres(PostgresSettings.from_env())
    test_persistence(pytest_args=pytest_args)


def demo_core(fixture: str, capital: str, seed: str) -> None:
    _run(
        (
            _python(),
            "-m",
            "apex.cli",
            "demo-core",
            "--fixture",
            fixture,
            "--capital",
            capital,
            "--seed",
            seed,
        )
    )


def _autopilot_command(*arguments: str) -> tuple[str, ...]:
    return (_python(), "-m", "apex.cli", *arguments)


def autopilot_strategies() -> None:
    _with_capture_database(_autopilot_command("autopilot-strategies"), migrate_to_head=True)


def autopilot_status(*, campaign_id: str) -> None:
    _with_capture_database(
        _autopilot_command("autopilot-status", "--campaign-id", campaign_id), migrate_to_head=True
    )


def autopilot_cycle(
    *,
    campaign_id: str,
    mode: str,
    idempotency_key: str,
    maximum_strategies: int,
    maximum_proposals: int,
    deadline_seconds: int,
    operator_note: str,
) -> None:
    arguments = [
        "autopilot-cycle",
        "--campaign-id",
        campaign_id,
        "--mode",
        mode,
        "--idempotency-key",
        idempotency_key,
        "--maximum-strategies",
        str(maximum_strategies),
        "--maximum-proposals",
        str(maximum_proposals),
        "--deadline-seconds",
        str(deadline_seconds),
        "--operator-note",
        operator_note,
    ]
    _with_capture_database(_autopilot_command(*arguments), migrate_to_head=True)


def autopilot_control(*, campaign_id: str, action: str, reason: str, idempotency_key: str) -> None:
    _with_capture_database(
        _autopilot_command(
            f"autopilot-{action.lower()}",
            "--campaign-id",
            campaign_id,
            "--reason",
            reason,
            "--idempotency-key",
            idempotency_key,
        ),
        migrate_to_head=True,
    )


def live_shadow_session(
    *,
    campaign_id: str,
    strategy_id: str,
    iterations: int,
    interval_seconds: int,
    market_limit: int,
    maximum_pages: int = 1,
    maximum_enrichments: int = 20,
    observations_per_market: int = 3,
    orderbook_depth: int = 5,
) -> None:
    """Foreground-only wrapper that preserves the prior PostgreSQL state."""

    _with_capture_database(
        _autopilot_command(
            "live-shadow-session",
            "--campaign-id",
            campaign_id,
            "--strategy-id",
            strategy_id,
            "--iterations",
            str(iterations),
            "--interval-seconds",
            str(interval_seconds),
            "--market-limit",
            str(market_limit),
            "--maximum-pages",
            str(maximum_pages),
            "--maximum-enrichments",
            str(maximum_enrichments),
            "--observations-per-market",
            str(observations_per_market),
            "--orderbook-depth",
            str(orderbook_depth),
        ),
        migrate_to_head=True,
    )


def multi_strategy_shadow_cycle(
    *,
    campaign_id: str,
    idempotency_key: str,
    market_limit: int,
    maximum_predictions: int,
    maximum_opportunities: int,
) -> None:
    _with_capture_database(
        _autopilot_command(
            "multi-strategy-shadow-cycle",
            "--campaign-id",
            campaign_id,
            "--idempotency-key",
            idempotency_key,
            "--market-limit",
            str(market_limit),
            "--maximum-predictions",
            str(maximum_predictions),
            "--maximum-opportunities",
            str(maximum_opportunities),
        ),
        migrate_to_head=True,
    )


def multi_strategy_shadow_session(
    *,
    campaign_id: str,
    iterations: int,
    interval_seconds: int,
    market_limit: int,
    maximum_pages: int,
    maximum_enrichments: int,
    orderbook_depth: int,
) -> None:
    """Foreground-only shared public evidence; retain the prior DB state."""

    _with_capture_database(
        _autopilot_command(
            "multi-strategy-shadow-session",
            "--campaign-id",
            campaign_id,
            "--iterations",
            str(iterations),
            "--interval-seconds",
            str(interval_seconds),
            "--market-limit",
            str(market_limit),
            "--max-pages",
            str(maximum_pages),
            "--max-enrichments",
            str(maximum_enrichments),
            "--orderbook-depth",
            str(orderbook_depth),
        ),
        migrate_to_head=True,
    )


def reconcile_multi_strategy_shadow_session(*, session_id: str) -> None:
    """Explicit network-free recovery for one interrupted foreground session."""

    _with_capture_database(
        _autopilot_command(
            "reconcile-multi-strategy-shadow-session",
            "--session-id",
            session_id,
        ),
        migrate_to_head=True,
    )


def multi_strategy_shadow_demo() -> None:
    _run(_autopilot_command("multi-strategy-shadow-demo"))


def create_shadow_plan(
    *,
    campaign_id: str,
    name: str,
    iterations: int,
    interval_seconds: int,
    market_count: int,
    maximum_pages: int,
    maximum_enrichments: int,
    maximum_markets_per_event: int,
    maximum_markets_per_series: int,
    orderbook_depth: int,
    idempotency_key: str,
    market_tickers: str | None,
) -> None:
    _with_capture_database(
        _autopilot_command(
            "create-shadow-plan",
            "--campaign-id",
            campaign_id,
            "--name",
            name,
            "--iterations",
            str(iterations),
            "--interval-seconds",
            str(interval_seconds),
            "--market-count",
            str(market_count),
            "--max-pages",
            str(maximum_pages),
            "--max-enrichments",
            str(maximum_enrichments),
            "--max-markets-per-event",
            str(maximum_markets_per_event),
            "--max-markets-per-series",
            str(maximum_markets_per_series),
            "--orderbook-depth",
            str(orderbook_depth),
            "--idempotency-key",
            idempotency_key,
            *(("--market-tickers", market_tickers) if market_tickers is not None else ()),
        ),
        migrate_to_head=True,
    )


def discover_create_shadow_plan(
    *,
    campaign_id: str,
    name: str,
    window_start: str,
    window_end: str,
    iterations: int,
    interval_seconds: int,
    target_markets: int,
    minimum_markets: int,
    minimum_events: int,
    maximum_pages: int,
    maximum_inspected_markets: int,
    maximum_enrichments: int,
    maximum_markets_per_event: int,
    maximum_markets_per_series: int,
    orderbook_depth: int,
    idempotency_key: str,
    maximum_orderbook_preflights: int,
    microstructure_configuration_version: str,
    prior_plan_ids: list[str],
    exclude_prior_plan_markets: bool,
    exclude_prior_plan_events: bool,
    exclude_active_plan_markets: bool,
) -> None:
    """Invoke one bounded public-only validated-cohort launch."""

    _with_capture_database(
        _autopilot_command(
            "discover-create-shadow-plan",
            "--campaign-id",
            campaign_id,
            "--name",
            name,
            "--window-start",
            window_start,
            "--window-end",
            window_end,
            "--iterations",
            str(iterations),
            "--interval-seconds",
            str(interval_seconds),
            "--target-markets",
            str(target_markets),
            "--minimum-markets",
            str(minimum_markets),
            "--minimum-events",
            str(minimum_events),
            "--max-pages",
            str(maximum_pages),
            "--max-inspected-markets",
            str(maximum_inspected_markets),
            "--max-enrichments",
            str(maximum_enrichments),
            "--max-markets-per-event",
            str(maximum_markets_per_event),
            "--max-markets-per-series",
            str(maximum_markets_per_series),
            "--orderbook-depth",
            str(orderbook_depth),
            "--max-orderbook-preflights",
            str(maximum_orderbook_preflights),
            "--microstructure-configuration-version",
            microstructure_configuration_version,
            "--idempotency-key",
            idempotency_key,
            *tuple(item for plan_id in prior_plan_ids for item in ("--exclude-plan-id", plan_id)),
            *(
                ("--exclude-prior-plan-markets",)
                if exclude_prior_plan_markets
                else ("--no-exclude-prior-plan-markets",)
            ),
            *(("--exclude-prior-plan-events",) if exclude_prior_plan_events else ()),
            *(
                ("--exclude-active-plan-markets",)
                if exclude_active_plan_markets
                else ("--no-exclude-active-plan-markets",)
            ),
        ),
        migrate_to_head=True,
    )


def run_shadow_plan(*, plan_id: str) -> None:
    _with_capture_database(
        _autopilot_command("run-shadow-plan", "--plan-id", plan_id, "--once"),
        migrate_to_head=True,
    )


def shadow_worker(
    *, plan_id: str | None, once: bool, poll_seconds: int, maximum_plans: int
) -> None:
    arguments = [
        "shadow-worker",
        "--poll-seconds",
        str(poll_seconds),
        "--maximum-plans",
        str(maximum_plans),
    ]
    if plan_id is not None:
        arguments.extend(("--plan-id", plan_id))
    if once:
        arguments.append("--once")
    _with_capture_database(_autopilot_command(*arguments), migrate_to_head=True)


def collect_shadow_outcomes(*, campaign_id: str, maximum_markets: int) -> None:
    _with_capture_database(
        _autopilot_command(
            "collect-shadow-outcomes",
            "--campaign-id",
            campaign_id,
            "--market-limit",
            str(maximum_markets),
        ),
        migrate_to_head=True,
    )


def strategy_performance(*, campaign_id: str, strategy_id: str | None, plan_id: str | None) -> None:
    arguments = ["strategy-performance", "--campaign-id", campaign_id]
    if strategy_id is not None:
        arguments.extend(("--strategy-id", strategy_id))
    if plan_id is not None:
        arguments.extend(("--plan-id", plan_id))
    _with_capture_database(_autopilot_command(*arguments), migrate_to_head=True)


def shadow_operations_demo() -> None:
    _run(_autopilot_command("shadow-operations-demo"))


def strategy_shadow_demo() -> None:
    """Run the durable demo in the disposable local verification database.

    Keeping this proof isolated avoids changing a user's normal Operator Console
    state while still exercising PostgreSQL persistence and guards end to end.
    """

    was_running = _postgres_was_running()
    if not was_running:
        db_up()
    try:
        _prepare_test_database()
        environment = _environment()
        environment["APEX_POSTGRES_DB"] = environment.get("APEX_POSTGRES_TEST_DB", "apex_test")
        _run(_autopilot_command("strategy-shadow-demo"), environment=environment)
    finally:
        if not was_running:
            db_down(volumes=False)


def orderbook_microstructure_shadow_demo() -> None:
    """Run the bounded microstructure proof in the disposable verification DB."""

    was_running = _postgres_was_running()
    if not was_running:
        db_up()
    try:
        _prepare_test_database()
        environment = _environment()
        environment["APEX_POSTGRES_DB"] = environment.get("APEX_POSTGRES_TEST_DB", "apex_test")
        _run(_autopilot_command("orderbook-microstructure-shadow-demo"), environment=environment)
    finally:
        if not was_running:
            db_down(volumes=False)


def market_dynamics_shadow_demo() -> None:
    """Run the temporal-price proof in the disposable verification database."""

    was_running = _postgres_was_running()
    if not was_running:
        db_up()
    try:
        _prepare_test_database()
        environment = _environment()
        environment["APEX_POSTGRES_DB"] = environment.get("APEX_POSTGRES_TEST_DB", "apex_test")
        _run(_autopilot_command("market-dynamics-shadow-demo"), environment=environment)
    finally:
        if not was_running:
            db_down(volumes=False)


def create_shadow_campaign(
    *, name: str, capital: str, maximum_loss: str, expires_in_days: int, idempotency_key: str
) -> None:
    _with_capture_database(
        _autopilot_command(
            "create-shadow-campaign",
            "--name",
            name,
            "--capital",
            capital,
            "--maximum-loss",
            maximum_loss,
            "--expires-in-days",
            str(expires_in_days),
            "--idempotency-key",
            idempotency_key,
        ),
        migrate_to_head=True,
    )


LOCAL_GOVERNANCE_DATABASE = "apex"
LOCAL_GOVERNANCE_HOST = "127.0.0.1"
LOCAL_GOVERNANCE_PORT = 55432
LOCAL_GOVERNANCE_CONFIRMATION = "RECONCILE_LOCAL_APEX_GOVERNANCE"


def _local_governance_reconciliation_changes(
    connection: sa.Connection, *, apply: bool
) -> tuple[dict[str, str], ...]:
    """Plan or repair only the two impossible legacy evidence-state rows."""

    from apex.adapters.postgres import strategy_governance
    from apex.application.strategy_shadow import production_shadow_strategy_descriptors
    from apex.domain.values import content_digest

    expected = {
        item.strategy_id: item
        for item in production_shadow_strategy_descriptors()
        if item.strategy_id in {"operator-thesis-v1", "market-consensus-baseline-v1"}
    }
    rows = tuple(
        connection.execute(
            sa.select(strategy_governance)
            .where(
                sa.or_(
                    strategy_governance.c.authority_state == "SHADOW_READY",
                    strategy_governance.c.evidence_state == "SHADOW_READY",
                )
            )
            .order_by(strategy_governance.c.strategy_id, strategy_governance.c.version)
            .with_for_update()
        ).mappings()
    )
    plans = []
    for row in rows:
        descriptor = expected.get(str(row["strategy_id"]))
        if descriptor is None:
            raise RuntimeError(
                "ambiguous stale local strategy-governance row: "
                f"{row['strategy_id']}:{row['version']}"
            )
        expected_values = (
            row["version"] == descriptor.version
            and row["display_name"] == descriptor.display_name
            and row["family"] == descriptor.family.value
            and row["implementation_state"] == descriptor.implementation_state.value
            and row["authority_state"] == "SHADOW"
            and row["evidence_state"] == "SHADOW_READY"
            and row["last_evidence_decision"] == descriptor.last_evidence_decision
            and row["supported_instrument_class"] == descriptor.supported_instrument_class
            and row["maximum_proposals_per_cycle"] == descriptor.maximum_proposals_per_cycle
            and row["configuration_hash"] == descriptor.configuration_hash.value
        )
        if not expected_values:
            raise RuntimeError(
                "ambiguous stale local strategy-governance row: "
                f"{row['strategy_id']}:{row['version']}"
            )
        plans.append(
            (
                row,
                descriptor,
                {
                    "strategy_id": descriptor.strategy_id,
                    "version": descriptor.version,
                    "authority_before": str(row["authority_state"]),
                    "authority_after": descriptor.authority_state.value,
                    "evidence_before": str(row["evidence_state"]),
                    "evidence_after": descriptor.evidence_state.value,
                    "canonical_hash_before": str(row["canonical_payload_hash"]),
                    "canonical_hash_after": content_digest(descriptor),
                },
            )
        )
    if apply and plans:
        enabled = connection.scalar(
            sa.text(
                "SELECT tgenabled FROM pg_trigger WHERE tgname = "
                "'apex_immutable_strategy_governance_trigger'"
            )
        )
        if enabled != "O":
            raise RuntimeError("strategy-governance immutable trigger is not normally enabled")
        connection.execute(
            sa.text(
                "ALTER TABLE strategy_governance "
                "DISABLE TRIGGER apex_immutable_strategy_governance_trigger"
            )
        )
        for _row, descriptor, _change in plans:
            updated = connection.execute(
                strategy_governance.update()
                .where(
                    strategy_governance.c.strategy_id == descriptor.strategy_id,
                    strategy_governance.c.version == descriptor.version,
                    strategy_governance.c.authority_state == "SHADOW",
                    strategy_governance.c.evidence_state == "SHADOW_READY",
                )
                .values(
                    evidence_state=descriptor.evidence_state.value,
                    canonical_payload_hash=content_digest(descriptor),
                )
            )
            if updated.rowcount != 1:
                raise RuntimeError("local strategy-governance reconciliation lost its row lock")
        connection.execute(
            sa.text(
                "ALTER TABLE strategy_governance "
                "ENABLE TRIGGER apex_immutable_strategy_governance_trigger"
            )
        )
    return tuple(change for _row, _descriptor, change in plans)


def reconcile_local_strategy_governance(
    *, database: str, apply: bool, confirmation: str | None
) -> None:
    """Audited one-time repair for a demonstrably impossible local development state."""

    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    settings = PostgresSettings.from_env()
    if database != LOCAL_GOVERNANCE_DATABASE:
        raise ValueError("local governance reconciliation requires --database apex")
    if (
        settings.host != LOCAL_GOVERNANCE_HOST
        or settings.port != LOCAL_GOVERNANCE_PORT
        or settings.database != LOCAL_GOVERNANCE_DATABASE
        or settings.database == settings.test_database
    ):
        raise ValueError("local governance reconciliation only permits 127.0.0.1:55432/apex")
    if apply and confirmation != LOCAL_GOVERNANCE_CONFIRMATION:
        raise ValueError("local governance reconciliation requires its exact confirmation text")
    if not apply and confirmation is not None:
        raise ValueError("dry-run reconciliation does not accept confirmation text")

    engine = create_postgres_engine(settings)
    try:
        with engine.begin() as connection:
            changes = _local_governance_reconciliation_changes(connection, apply=apply)
    finally:
        engine.dispose()

    report = {
        "report_version": "local-governance-reconciliation-v1",
        "mode": "APPLY" if apply else "DRY_RUN",
        "target": "127.0.0.1:55432/apex",
        "prohibited_value": "SHADOW_READY",
        "changes": changes,
        "changed_row_count": len(changes),
        "idempotent_noop": not changes,
        "created_at": datetime.now(tz=UTC).isoformat(),
    }
    encoded = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    report_dir = ROOT / "artifacts" / "local-governance-reconciliation"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / (
        f"{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}-{'apply' if apply else 'dry-run'}.json"
    )
    report_path.write_bytes(encoded)
    print(
        json.dumps(
            {
                "mode": report["mode"],
                "changed_row_count": report["changed_row_count"],
                "idempotent_noop": report["idempotent_noop"],
                "changes": changes,
                "report_path": str(report_path.relative_to(ROOT)),
                "report_sha256": sha256(encoded).hexdigest(),
            },
            sort_keys=True,
        )
    )


def autopilot_core_demo() -> None:
    _with_capture_database(_autopilot_command("autopilot-core-demo"), migrate_to_head=True)


def paper_portfolio_status(*, campaign_id: str) -> None:
    _with_capture_database(
        _autopilot_command("paper-portfolio-status", "--campaign-id", campaign_id),
        migrate_to_head=True,
    )


def paper_mark_positions(*, campaign_id: str, orderbook_run_id: str) -> None:
    _with_capture_database(
        _autopilot_command(
            "paper-mark-positions",
            "--campaign-id",
            campaign_id,
            "--orderbook-run-id",
            orderbook_run_id,
        ),
        migrate_to_head=True,
    )


def paper_settle_position(*, position_id: str, settlement_fact_id: str) -> None:
    _with_capture_database(
        _autopilot_command(
            "paper-settle-position",
            "--position-id",
            position_id,
            "--settlement-fact-id",
            settlement_fact_id,
        ),
        migrate_to_head=True,
    )


def paper_portfolio_demo() -> None:
    _with_capture_database(_autopilot_command("paper-portfolio-demo"), migrate_to_head=True)


def scan_kalshi_fixture(*, persist: bool) -> None:
    arguments = [_python(), "-m", "apex.cli", "scan-kalshi-fixture"]
    if persist:
        arguments.append("--persist")
    _run(tuple(arguments))


def smoke_kalshi_public(
    *,
    limit: int,
    maximum_pages: int,
    timeout_seconds: int,
    persist: bool,
) -> None:
    arguments = [
        _python(),
        "-m",
        "apex.cli",
        "smoke-kalshi-public",
        "--limit",
        str(limit),
        "--max-pages",
        str(maximum_pages),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if persist:
        arguments.append("--persist")
    _run(tuple(arguments))


def scan_kalshi_consistency_fixture(*, persist: bool) -> None:
    arguments = [_python(), "-m", "apex.cli", "scan-kalshi-consistency-fixture"]
    if persist:
        arguments.append("--persist")
    _run(tuple(arguments))


def smoke_kalshi_consistency(
    *,
    event_limit: int,
    market_limit: int,
    maximum_pages: int,
    timeout_seconds: int,
    persist: bool,
) -> None:
    arguments = [
        _python(),
        "-m",
        "apex.cli",
        "smoke-kalshi-consistency",
        "--event-limit",
        str(event_limit),
        "--market-limit",
        str(market_limit),
        "--max-pages",
        str(maximum_pages),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if persist:
        arguments.append("--persist")
    _run(tuple(arguments))


def compare_kalshi_scans_fixture() -> None:
    _run((_python(), "-m", "apex.cli", "compare-kalshi-scans-fixture"))


def compare_kalshi_scans(*, baseline_run_id: str, current_run_id: str) -> None:
    _run(
        (
            _python(),
            "-m",
            "apex.cli",
            "compare-kalshi-scans",
            "--baseline-run-id",
            baseline_run_id,
            "--current-run-id",
            current_run_id,
        )
    )


def compare_kalshi_latest_scans() -> None:
    _run((_python(), "-m", "apex.cli", "compare-kalshi-latest-scans"))


def summarize_kalshi_event_history_fixture() -> None:
    _run((_python(), "-m", "apex.cli", "summarize-kalshi-event-history-fixture"))


def summarize_kalshi_event_history(
    *,
    event_ticker: str,
    maximum_completed_runs: int,
    first_ingestion_run_id: str | None,
    last_ingestion_run_id: str | None,
    continuity_gap_seconds: int,
) -> None:
    arguments = [
        _python(),
        "-m",
        "apex.cli",
        "summarize-kalshi-event-history",
        "--event-ticker",
        event_ticker,
        "--max-runs",
        str(maximum_completed_runs),
        "--continuity-gap-seconds",
        str(continuity_gap_seconds),
    ]
    if first_ingestion_run_id is not None:
        arguments.extend(("--first-run-id", first_ingestion_run_id))
    if last_ingestion_run_id is not None:
        arguments.extend(("--last-run-id", last_ingestion_run_id))
    _run(tuple(arguments))


def summarize_kalshi_orderbook_history_fixture() -> None:
    _run((_python(), "-m", "apex.cli", "summarize-kalshi-orderbook-history-fixture"))


def summarize_kalshi_orderbook_history(
    *,
    market_ticker: str,
    maximum_observations: int,
    first_orderbook_snapshot_id: str | None,
    last_orderbook_snapshot_id: str | None,
    continuity_gap_seconds: int,
) -> None:
    arguments = [
        _python(),
        "-m",
        "apex.cli",
        "summarize-kalshi-orderbook-history",
        "--market-ticker",
        market_ticker,
        "--max-observations",
        str(maximum_observations),
        "--continuity-gap-seconds",
        str(continuity_gap_seconds),
    ]
    if first_orderbook_snapshot_id is not None:
        arguments.extend(("--first-snapshot-id", first_orderbook_snapshot_id))
    if last_orderbook_snapshot_id is not None:
        arguments.extend(("--last-snapshot-id", last_orderbook_snapshot_id))
    _run(tuple(arguments))


def scan_kalshi_binary_complements_fixture() -> None:
    _run((_python(), "-m", "apex.cli", "scan-kalshi-binary-complements-fixture"))


def smoke_kalshi_binary_complements(
    *,
    market_limit: int,
    depth: int,
    maximum_bundle_contracts: str,
    timeout_seconds: int,
    discover_first: bool,
) -> None:
    arguments = [
        _python(),
        "-m",
        "apex.cli",
        "smoke-kalshi-binary-complements",
        "--market-limit",
        str(market_limit),
        "--depth",
        str(depth),
        "--max-bundle-contracts",
        maximum_bundle_contracts,
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if discover_first:
        arguments.append("--discover-first")
    _run(tuple(arguments))


def capture_kalshi_orderbook_series(
    *,
    market_ticker: str | None,
    latest_open_market: bool,
    discover_first: bool,
    observations: int,
    interval_seconds: int,
    depth: int,
    maximum_bundle_contracts: str,
    timeout_seconds: int,
) -> None:
    arguments = [
        _python(),
        "-m",
        "apex.cli",
        "capture-kalshi-orderbook-series",
        "--observations",
        str(observations),
        "--interval-seconds",
        str(interval_seconds),
        "--depth",
        str(depth),
        "--max-bundle-contracts",
        maximum_bundle_contracts,
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if market_ticker is not None:
        arguments.extend(("--market-ticker", market_ticker))
    elif latest_open_market:
        arguments.append("--latest-open-market")
    if discover_first:
        arguments.append("--discover-first")
    _with_capture_database(tuple(arguments))


def capture_kalshi_orderbook_series_fixture() -> None:
    _run((_python(), "-m", "apex.cli", "capture-kalshi-orderbook-series-fixture"))


def sweep_kalshi_orderbooks(
    *,
    market_count: int,
    maximum_pages: int,
    market_limit: int,
    maximum_enrichments: int,
    observations: int,
    interval_seconds: int,
    depth: int,
    maximum_bundle_contracts: str,
    timeout_seconds: int,
) -> None:
    _with_capture_database(
        (
            _python(),
            "-m",
            "apex.cli",
            "sweep-kalshi-orderbooks",
            "--market-count",
            str(market_count),
            "--max-pages",
            str(maximum_pages),
            "--market-limit",
            str(market_limit),
            "--max-enrichments",
            str(maximum_enrichments),
            "--observations",
            str(observations),
            "--interval-seconds",
            str(interval_seconds),
            "--depth",
            str(depth),
            "--max-bundle-contracts",
            maximum_bundle_contracts,
            "--timeout-seconds",
            str(timeout_seconds),
        )
    )


def sweep_kalshi_orderbooks_fixture() -> None:
    _run((_python(), "-m", "apex.cli", "sweep-kalshi-orderbooks-fixture"))


def scan_kalshi_threshold_ladders_fixture() -> None:
    _run((_python(), "-m", "apex.cli", "scan-kalshi-threshold-ladders-fixture"))


def scan_kalshi_threshold_ladders(
    *,
    scan_id: str | None,
    event_count: int,
    maximum_pages: int,
    market_limit: int,
    maximum_enrichments: int,
    maximum_markets_per_event: int,
    maximum_relationships: int,
    rounds: int,
    interval_seconds: int,
    depth: int,
    maximum_bundle_contracts: str,
    maximum_observation_skew_seconds: int,
    timeout_seconds: int,
) -> None:
    command = [
        _python(),
        "-m",
        "apex.cli",
        "scan-kalshi-threshold-ladders",
        "--event-count",
        str(event_count),
        "--max-pages",
        str(maximum_pages),
        "--market-limit",
        str(market_limit),
        "--max-enrichments",
        str(maximum_enrichments),
        "--max-markets-per-event",
        str(maximum_markets_per_event),
        "--max-relationships",
        str(maximum_relationships),
        "--rounds",
        str(rounds),
        "--interval-seconds",
        str(interval_seconds),
        "--depth",
        str(depth),
        "--max-bundle-contracts",
        maximum_bundle_contracts,
        "--max-observation-skew-seconds",
        str(maximum_observation_skew_seconds),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if scan_id is not None:
        command.extend(("--scan-id", scan_id))
    _with_capture_database(
        tuple(command),
        migrate_to_head=True,
    )


def diagnose_kalshi_capture_selection(
    *, max_pages: int, market_limit: int, timeout_seconds: int
) -> None:
    _with_capture_database(
        (
            _python(),
            "-m",
            "apex.cli",
            "diagnose-kalshi-capture-selection",
            "--max-pages",
            str(max_pages),
            "--market-limit",
            str(market_limit),
            "--timeout-seconds",
            str(timeout_seconds),
        )
    )


def reconcile_kalshi_capture_sessions() -> None:
    _with_capture_database((_python(), "-m", "apex.cli", "reconcile-kalshi-capture-sessions"))


def _research_command(*arguments: str) -> tuple[str, ...]:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for research-only ecCodes execution")
    return (
        uv,
        "run",
        "--with",
        "eccodes",
        "python",
        "-m",
        "research.weather_gfs_feasibility.run",
        *arguments,
    )


def research_gfs_archive_feasibility_fixture() -> None:
    _run(_research_command("fixture"))


def research_gfs_archive_feasibility(
    *,
    series: str,
    sample_dates: int,
    lead_hours: str,
    maximum_requests: int,
    maximum_download_mb: int,
    timeout_seconds: int,
) -> None:
    _run(
        _research_command(
            "real",
            "--series",
            series,
            "--sample-dates",
            str(sample_dates),
            "--lead-hours",
            lead_hours,
            "--max-requests",
            str(maximum_requests),
            "--max-download-mb",
            str(maximum_download_mb),
            "--timeout-seconds",
            str(timeout_seconds),
        )
    )


def _research_weather_edge_command(*arguments: str) -> tuple[str, ...]:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for research-only ecCodes execution")
    return (
        uv,
        "run",
        "--with",
        "eccodes",
        "python",
        "-m",
        "research.weather_gfs_feasibility.edge",
        *arguments,
    )


def research_gfs_weather_edge_fixture() -> None:
    _run(_research_weather_edge_command("fixture"))


def research_gfs_weather_edge(
    *,
    series: str,
    start_date: str,
    end_date: str,
    lead_hours: str,
    max_event_dates: int,
    bootstrap_repetitions: int,
    max_noaa_requests: int,
    max_download_mb: int,
    timeout_seconds: int,
) -> None:
    _run(
        _research_weather_edge_command(
            "real",
            "--series",
            series,
            "--start-date",
            start_date,
            "--end-date",
            end_date,
            "--lead-hours",
            lead_hours,
            "--max-event-dates",
            str(max_event_dates),
            "--bootstrap-repetitions",
            str(bootstrap_repetitions),
            "--max-noaa-requests",
            str(max_noaa_requests),
            "--max-download-mb",
            str(max_download_mb),
            "--timeout-seconds",
            str(timeout_seconds),
        )
    )


def _strategy_lab_service() -> tuple[StrategyLabService, Engine]:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
    from apex.adapters.strategy_lab_postgres import PostgresStrategyLabStore
    from apex.application.strategy_lab import StrategyLabService

    engine = create_postgres_engine(PostgresSettings.from_env())
    return StrategyLabService(
        PostgresStrategyLabStore(engine), LocalStrategyLabArtifactStore()
    ), engine


def _strategy_lab_shadow_service() -> tuple[Any, Engine]:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
    from apex.adapters.strategy_lab_postgres import (
        PostgresStrategyLabShadowStore,
        PostgresStrategyLabStore,
    )
    from apex.adapters.strategy_lab_shadow_models import StrategyLabManagedModelLoader
    from apex.application.strategy_lab_shadow import StrategyLabShadowService

    engine = create_postgres_engine(PostgresSettings.from_env())
    artifacts = LocalStrategyLabArtifactStore()
    return StrategyLabShadowService(
        PostgresStrategyLabStore(engine),
        PostgresStrategyLabShadowStore(engine),
        artifacts,
        StrategyLabManagedModelLoader(),
    ), engine


def _strategy_lab_cutoff(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SystemExit("cutoff must be an ISO-8601 timezone-aware timestamp") from error
    if parsed.tzinfo is None:
        raise SystemExit("cutoff must be an ISO-8601 timezone-aware timestamp")
    return parsed.astimezone(UTC)


def _strategy_lab_json(value: object) -> None:
    print(json.dumps(value, default=str, sort_keys=True))


def broad_equity_universe_demo() -> None:
    from apex.adapters.broad_options_demo import run_broad_equity_universe_demo

    _strategy_lab_json(run_broad_equity_universe_demo())


def broad_equity_discovery_demo() -> None:
    from apex.adapters.broad_options_demo import run_broad_equity_discovery_demo

    _strategy_lab_json(run_broad_equity_discovery_demo())


def options_intelligence_demo() -> None:
    from apex.adapters.broad_options_demo import run_options_intelligence_demo

    _strategy_lab_json(run_options_intelligence_demo())


def _research_evidence_runtime() -> tuple[Any, Any]:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_evidence_postgres import (
        PostgresResearchEvidenceRepository,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    return PostgresResearchEvidenceRepository(engine), engine


def _research_evidence_json(value: object) -> None:
    from apex.domain.values import canonical_json_bytes

    _strategy_lab_json(json.loads(canonical_json_bytes(value)))


def _reported_research_evidence_class(value: object, repository: Any) -> object:
    evidence_class = getattr(value, "evidence_class", None)
    if evidence_class is not None:
        return evidence_class
    checkpoints = getattr(value, "checkpoints", None)
    if checkpoints:
        retained_classes = sorted(
            {checkpoint.observation.evidence_class.value for checkpoint in checkpoints}
        )
        return retained_classes[0] if len(retained_classes) == 1 else retained_classes
    if isinstance(value, dict):
        capture = value.get("capture")
        if capture is not None:
            return capture.evidence_class
        captures = value.get("captures")
        if captures is not None:
            retained_classes = sorted({capture.evidence_class.value for capture in captures})
            return retained_classes or "NOT_APPLICABLE"
    run_id = getattr(value, "run_id", None)
    if run_id is not None:
        run = repository.run(run_id)
        if run is not None:
            return run.evidence_class
    return "NOT_APPLICABLE"


def _clean_git_revision() -> str:
    status = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise SystemExit("research experiment requires a clean committed worktree")
    return subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _retained_nasdaq_capture(repository: Any, capture_id: str) -> tuple[bytes, Any]:
    from apex.adapters.security_reference import (
        NASDAQ_LISTED_PATH,
        parse_nasdaq_symbol_directory,
    )
    from apex.application.research_evidence import ResearchEvidenceNotFound

    capture = repository.capture(capture_id)
    raw = repository.raw_payload(capture_id)
    if capture is None or raw is None:
        raise ResearchEvidenceNotFound("RESUMABLE_CAPTURE_RAW_EVIDENCE_NOT_FOUND")
    parsed = parse_nasdaq_symbol_directory(
        payload=raw,
        source_path=NASDAQ_LISTED_PATH,
        received_at=capture.ingestion_at,
    )
    return raw, parsed


def research_evidence_command(arguments: argparse.Namespace) -> None:
    """Run exact-ID durable research lifecycle commands with no financial imports."""
    from apex.adapters.research_evidence_source import (
        NasdaqDirectoryCapabilityUnavailable,
        NasdaqDirectoryClient,
        NasdaqDirectoryClientError,
    )
    from apex.adapters.security_reference import (
        NASDAQ_LISTED_PATH,
        NASDAQ_TRADER_HOST,
        parse_nasdaq_symbol_directory,
    )
    from apex.application.research_evidence import (
        CaptureNasdaqDirectory,
        ExecuteBroadUniverseExperiment,
        MaterializeResearchDatasetSnapshot,
        RecordResearchExperimentOutcome,
        RegisterResearchHypothesis,
        research_workspace,
    )
    from apex.domain.research_evidence import EvidenceClass

    repository, engine = _research_evidence_runtime()
    now = datetime.now(tz=UTC)
    value: Any
    try:
        if arguments.command == "research-hypothesis-register":
            value = RegisterResearchHypothesis(repository).execute(
                hypothesis_id=arguments.hypothesis_id,
                revision=arguments.revision,
                question=arguments.question,
                motivation=arguments.motivation,
                actor=arguments.actor,
                market_scope=tuple(
                    item.strip() for item in arguments.market_scope.split(",") if item.strip()
                ),
                created_at=now,
                supersedes_version_id=arguments.supersedes_version_id,
            )
        elif arguments.command == "research-hypothesis-inspect":
            value = repository.hypothesis_version(arguments.hypothesis_version_id)
            if value is None:
                raise SystemExit("exact hypothesis version was not found")
        elif arguments.command == "research-capture-start":
            service = CaptureNasdaqDirectory(repository)
            prior = repository.capture(arguments.capture_id)
            requested_at = now
            if prior is not None:
                expected_class = (
                    EvidenceClass.FIXTURE
                    if arguments.mode == "fixture"
                    else EvidenceClass.GENUINE_PUBLIC
                )
                if prior.evidence_class is not expected_class:
                    raise SystemExit("capture identity is bound to a different evidence class")
                if prior.status.value != "RUNNING":
                    value = prior
                else:
                    raw, parsed = _retained_nasdaq_capture(repository, arguments.capture_id)
                    value = service.resume(
                        capture_id=arguments.capture_id,
                        raw_payload=raw,
                        records=parsed.records,
                        resumed_at=now,
                        chunk_size=arguments.chunk_size,
                    )
            elif arguments.mode == "fixture":
                raw = (ROOT / "fixtures" / "broad-research" / "nasdaqlisted.txt").read_bytes()
                parsed = parse_nasdaq_symbol_directory(
                    payload=raw,
                    source_path=NASDAQ_LISTED_PATH,
                    received_at=now,
                )
                value = service.start(
                    capture_id=arguments.capture_id,
                    evidence_class=EvidenceClass.FIXTURE,
                    source_locator="fixture:broad-research/nasdaqlisted.txt",
                    raw_payload=raw,
                    records=parsed.records,
                    requested_at=requested_at,
                    ingested_at=now,
                    chunk_size=arguments.chunk_size,
                    limitations=("PROVIDER_SHAPED_FIXTURE_EVIDENCE",),
                )
            else:
                source_locator = f"https://{NASDAQ_TRADER_HOST}{NASDAQ_LISTED_PATH}"
                try:
                    raw, parsed = NasdaqDirectoryClient().capture(
                        received_at=now,
                        timeout_seconds=arguments.timeout_seconds,
                        maximum_records=arguments.maximum_records,
                    )
                    value = service.start(
                        capture_id=arguments.capture_id,
                        evidence_class=EvidenceClass.GENUINE_PUBLIC,
                        source_locator=source_locator,
                        raw_payload=raw,
                        records=parsed.records,
                        requested_at=requested_at,
                        ingested_at=now,
                        chunk_size=arguments.chunk_size,
                        limitations=parsed.receipt.limitations,
                    )
                except NasdaqDirectoryCapabilityUnavailable as error:
                    value = service.record_unavailable(
                        capture_id=arguments.capture_id,
                        source_locator=source_locator,
                        requested_at=requested_at,
                        recorded_at=now,
                        reason=str(error),
                    )
                except NasdaqDirectoryClientError as error:
                    value = service.record_failed(
                        capture_id=arguments.capture_id,
                        source_locator=source_locator,
                        requested_at=requested_at,
                        recorded_at=now,
                        reason=str(error),
                    )
        elif arguments.command == "research-capture-resume":
            raw, parsed = _retained_nasdaq_capture(repository, arguments.capture_id)
            value = CaptureNasdaqDirectory(repository).resume(
                capture_id=arguments.capture_id,
                raw_payload=raw,
                records=parsed.records,
                resumed_at=now,
                chunk_size=arguments.chunk_size,
            )
        elif arguments.command == "research-capture-inspect":
            capture = repository.capture(arguments.capture_id)
            if capture is None:
                raise SystemExit("exact capture was not found")
            value = {
                "capture": capture,
                "checkpoints": repository.checkpoints(arguments.capture_id),
                "observations": repository.observations(arguments.capture_id),
                "raw_evidence_retained": repository.raw_payload(arguments.capture_id) is not None,
            }
        elif arguments.command == "research-snapshot-materialize":
            value = MaterializeResearchDatasetSnapshot(repository).execute(
                snapshot_id=arguments.snapshot_id,
                component_capture_ids=tuple(
                    item.strip() for item in arguments.capture_ids.split(",") if item.strip()
                ),
                created_at=now,
            )
        elif arguments.command == "research-snapshot-inspect":
            value = repository.snapshot(arguments.snapshot_id)
            if value is None:
                raise SystemExit("exact research dataset snapshot was not found")
        elif arguments.command == "research-experiment-run":
            value = ExecuteBroadUniverseExperiment(repository).execute(
                run_id=arguments.run_id,
                hypothesis_version_id=arguments.hypothesis_version_id,
                snapshot_id=arguments.snapshot_id,
                minimum_eligible_count=arguments.minimum_eligible_count,
                code_revision=_clean_git_revision(),
                executed_at=now,
                deterministic_seed=0,
            )
        elif arguments.command == "research-experiment-replay":
            value = ExecuteBroadUniverseExperiment(repository).replay(arguments.run_id)
        elif arguments.command == "research-experiment-inspect":
            value = repository.run(arguments.run_id)
            if value is None:
                raise SystemExit("exact experiment run was not found")
        elif arguments.command == "research-outcome-record":
            value = RecordResearchExperimentOutcome(repository).execute(
                outcome_id=arguments.outcome_id,
                run_id=arguments.run_id,
                decision=arguments.decision,
                decided_at=now,
                actor=arguments.actor,
                reason=arguments.reason,
            )
        elif arguments.command == "research-outcome-inspect":
            value = repository.outcome(arguments.outcome_id)
            if value is None:
                raise SystemExit("exact experiment outcome was not found")
        elif arguments.command == "research-evidence-workspace":
            value = research_workspace(repository)
        else:
            raise SystemExit(f"unsupported durable research command: {arguments.command}")
        _research_evidence_json(
            {
                "result": value,
                "evidence_class": _reported_research_evidence_class(value, repository),
                "authority": "RESEARCH_ONLY",
                "financial_authority": "NONE",
                "orders": 0,
                "positions": 0,
                "reservations": 0,
                "journals": 0,
                "broker_writes": 0,
            }
        )
    finally:
        engine.dispose()


def _crypto_research_runtime() -> tuple[Any, Any]:
    from apex.adapters.crypto_market_pulse_postgres import (
        PostgresCryptoMarketPulseRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    return PostgresCryptoMarketPulseRepository(engine), engine


def _crypto_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SystemExit("crypto interval must use an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise SystemExit("crypto interval timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def crypto_research_command(arguments: argparse.Namespace) -> None:
    """Run exact-ID historical crypto research commands with authority NONE."""
    from apex.adapters.coinbase_public import (
        CoinbasePublicCandleSource,
        FixtureCoinbaseCandleSource,
    )
    from apex.adapters.coinbase_universe_public import (
        CoinbasePublicUniverseSource,
        FixtureCoinbaseUniverseSource,
    )
    from apex.adapters.crypto_campaign_generic_fixtures import (
        generic_fixture_acceptance,
    )
    from apex.application.crypto_campaign import (
        AttachCampaignDataset,
        AuthorizeHoldoutRelease,
        CaptureSealedHoldout,
        MaterializeSealedHoldout,
        RecordDatasetExposure,
        RegisterCryptoCampaign,
        SealHoldout,
        snapshot_access,
    )
    from apex.application.crypto_campaign_candidate import (
        ApplyCandidateNominationPolicy,
        ExecuteCandidateWindow,
        MaterializeCandidateDossier,
        RecordCandidateOutcome,
        RegisterCandidate,
        RegisterCandidateHypothesis,
        RegisterCandidateProtocol,
        RegisterCandidateStrategy,
    )
    from apex.application.crypto_campaign_candidate_2 import (
        ApplyBreakoutNominationPolicy,
        ExecuteBreakoutWindow,
        MaterializeBreakoutDossier,
        RecordBreakoutOutcome,
        RegisterBreakoutCandidate,
        RegisterBreakoutHypothesis,
        RegisterBreakoutProtocol,
        RegisterBreakoutStrategy,
        candidate_2_workspace,
    )
    from apex.application.crypto_campaign_generic import (
        GenericMaterialComparison,
        campaign_policy_v1_finding,
        generic_workspace,
        retained_strategy_compatibility,
    )
    from apex.application.crypto_challenger import (
        ExecuteCryptoChallengerWindow,
        MaterializeCryptoChallengerDossier,
        RecordCryptoChallengerConclusion,
        RecordCryptoChallengerOutcome,
        RegisterCryptoChallengerProtocol,
        RegisterMonthlyMomentumHypothesis,
        RegisterMonthlyMomentumStrategy,
    )
    from apex.application.crypto_dossier import (
        ExecuteCryptoDisjointReplication,
        MaterializeCryptoEvidenceDossier,
        RecordCryptoDossierConclusion,
        RecordCryptoReplicationOutcome,
        RegisterCryptoReplicationProtocol,
        RegisterFrozenCryptoStrategy,
    )
    from apex.application.crypto_market_pulse import (
        ExecuteCryptoMarketPulseRefresh,
        RegisterCryptoRefreshPlan,
        RunCurrentCryptoRelativeStrength,
        RunCurrentCryptoReversalRegime,
        crypto_market_pulse_workspace,
    )
    from apex.application.crypto_research import (
        CaptureCoinbaseHistory,
        CryptoResearchConflict,
        ExecuteCryptoTrendValidation,
        MaterializeCryptoHistoricalSnapshot,
        RecordCryptoExperimentOutcome,
        RegisterCryptoHypothesis,
    )
    from apex.application.crypto_universe import (
        CaptureCryptoCalibrationHistory,
        CaptureCryptoUniverseObservations,
        CapturePublicCryptoProducts,
        MaterializeCryptoUniverse,
        RunCryptoDiscovery,
        crypto_universe_workspace,
    )
    from apex.domain.crypto_campaign import (
        FIXED_CAMPAIGN_ID,
        CampaignBudgetStatus,
        CampaignDatasetRole,
        DatasetExposureSummary,
        ExposureCategory,
        HoldoutMetadata,
        SnapshotUsePurpose,
        campaign_state,
    )
    from apex.domain.crypto_campaign_generic import (
        GenericCandidateDefinition,
        candidate_definition_mapping,
        compile_candidate_definition,
    )
    from apex.domain.crypto_dossier import NO_APPROVED_CHAMPION
    from apex.domain.crypto_research import COINBASE_PRODUCT
    from apex.domain.crypto_universe import MAX_OBSERVED_PRODUCTS, build_asset_groups
    from apex.domain.research_evidence import EvidenceClass

    repository, engine = _crypto_research_runtime()
    now = datetime.now(tz=UTC)
    network_involved = False
    network_performed = False
    value: Any
    try:
        if arguments.command == "crypto-pulse-plan-register":
            value = RegisterCryptoRefreshPlan(repository).execute(
                plan_id=arguments.plan_id,
                universe_snapshot_id=arguments.snapshot_id,
                actor=arguments.actor,
                created_at=now,
                current_market_evidence=arguments.current_market_evidence,
            )
        elif arguments.command == "crypto-pulse-plan-inspect":
            value = repository.refresh_plan(arguments.plan_id)
            if value is None:
                raise SystemExit("exact crypto market-pulse plan was not found")
        elif arguments.command in {"crypto-pulse-refresh-run", "crypto-pulse-refresh-replay"}:
            universe_source = (
                FixtureCoinbaseUniverseSource()
                if arguments.mode == "fixture"
                else CoinbasePublicUniverseSource()
            )
            network_involved = universe_source.network_access
            result = ExecuteCryptoMarketPulseRefresh(repository, universe_source).execute(
                refresh_id=arguments.refresh_id,
                plan_id=arguments.plan_id,
                as_of=_crypto_timestamp(arguments.as_of),
                requested_at=now,
            )
            network_performed = result.network_requests_performed > 0
            value = result
        elif arguments.command == "crypto-pulse-refresh-inspect":
            refresh = repository.refresh_run(arguments.refresh_id)
            if refresh is None:
                raise SystemExit("exact crypto market-pulse refresh was not found")
            value = {
                "refresh": refresh,
                "events": repository.refresh_events(arguments.refresh_id),
                "checkpoints": repository.pulse_checkpoints(arguments.refresh_id),
            }
        elif arguments.command == "crypto-pulse-checkpoint-inspect":
            value = repository.pulse_checkpoint(arguments.checkpoint_id)
            if value is None:
                raise SystemExit("exact crypto market-pulse checkpoint was not found")
        elif arguments.command == "crypto-pulse-snapshot-inspect":
            value = repository.pulse_snapshot(arguments.pulse_id)
            if value is None:
                raise SystemExit("exact crypto market-pulse snapshot was not found")
        elif arguments.command == "crypto-pulse-lineage-inspect":
            value = repository.opportunity_lineage(arguments.lineage_id)
            if value is None:
                raise SystemExit("exact crypto Opportunity lineage was not found")
        elif arguments.command == "crypto-pulse-summary-inspect":
            value = repository.market_change_summary(arguments.summary_id)
            if value is None:
                raise SystemExit("exact crypto market-change summary was not found")
        elif arguments.command in {
            "crypto-relative-strength-run",
            "crypto-relative-strength-replay",
        }:
            value = RunCurrentCryptoRelativeStrength(repository).execute(
                run_id=arguments.run_id,
                pulse_id=arguments.pulse_id,
                generated_at=now,
            )
        elif arguments.command in {
            "crypto-reversal-regime-run",
            "crypto-reversal-regime-replay",
        }:
            value = RunCurrentCryptoReversalRegime(repository).execute(
                run_id=arguments.run_id,
                pulse_id=arguments.pulse_id,
                generated_at=now,
            )
        elif arguments.command == "crypto-relative-strength-inspect":
            run = repository.discovery_run(arguments.run_id)
            if run is None:
                raise SystemExit("exact current crypto strategy run was not found")
            value = {
                "run": run,
                "opportunities": tuple(
                    item
                    for item in repository.opportunities()
                    if item.discovery_run_id == arguments.run_id
                ),
            }
        elif arguments.command == "crypto-current-strategy-library":
            from apex.adapters.shared_opportunity_sources import (
                RetainedSharedOpportunitySource,
            )

            value = RetainedSharedOpportunitySource(engine).strategy_library()
        elif arguments.command == "crypto-products-capture":
            universe_source = (
                FixtureCoinbaseUniverseSource()
                if arguments.mode == "fixture"
                else CoinbasePublicUniverseSource()
            )
            network_involved = universe_source.network_access
            prior = repository.product_capture(arguments.capture_id)
            network_performed = universe_source.network_access and prior is None
            value = CapturePublicCryptoProducts(repository, universe_source).execute(
                capture_id=arguments.capture_id,
                requested_at=now,
            )
        elif arguments.command == "crypto-products-inspect":
            capture = repository.product_capture(arguments.capture_id)
            if capture is None:
                raise SystemExit("exact crypto public product capture was not found")
            value = {
                "capture": capture,
                "products": repository.products(arguments.capture_id),
            }
        elif arguments.command == "crypto-universe-policy-inspect":
            value = {"policy": crypto_universe_workspace(repository)["policy"]}
        elif arguments.command == "crypto-calibration-history-capture":
            universe_source = (
                FixtureCoinbaseUniverseSource()
                if arguments.mode == "fixture"
                else CoinbasePublicUniverseSource()
            )
            network_involved = universe_source.network_access
            history_result = CaptureCryptoCalibrationHistory(repository, universe_source).execute(
                history_id=arguments.history_id,
                universe_snapshot_id=arguments.snapshot_id,
                interval_start=_crypto_timestamp(arguments.start),
                interval_end=_crypto_timestamp(arguments.end),
                requested_at=now,
            )
            network_performed = history_result.request_count > 0
            value = history_result
        elif arguments.command == "crypto-observations-capture":
            capture = repository.product_capture(arguments.capture_id)
            if capture is None:
                raise SystemExit("exact crypto public product capture was not found")
            universe_source = (
                FixtureCoinbaseUniverseSource()
                if arguments.mode == "fixture"
                else CoinbasePublicUniverseSource()
            )
            if capture.evidence_class is not universe_source.evidence_class:
                raise SystemExit("crypto product and observation evidence classes must match")
            network_involved = universe_source.network_access
            products = repository.products(arguments.capture_id)
            product_by_id = {item.product_id: item for item in products}
            shortlisted_groups = sorted(
                (
                    item
                    for item in build_asset_groups(
                        products=products,
                        observations=(),
                        evidence_class=capture.evidence_class,
                    )
                    if item.history_shortlist_rank is not None
                    and item.selected_product_id is not None
                ),
                key=lambda item: item.history_shortlist_rank or 0,
            )[:MAX_OBSERVED_PRODUCTS]
            expected_observation_ids = tuple(
                (
                    f"{arguments.capture_id}-"
                    f"{product_by_id[item.selected_product_id or ''].product_id.lower()}"
                    "-daily-observation-v1"
                )
                for item in shortlisted_groups
            )
            network_performed = universe_source.network_access and any(
                repository.observation(observation_id) is None
                for observation_id in expected_observation_ids
            )
            value = CaptureCryptoUniverseObservations(repository, universe_source).execute(
                capture_id=arguments.capture_id,
                interval_start=_crypto_timestamp(arguments.start),
                interval_end=_crypto_timestamp(arguments.end),
                requested_at=now,
            )
        elif arguments.command == "crypto-observation-inspect":
            value = repository.observation(arguments.observation_id)
            if value is None:
                raise SystemExit("exact crypto product observation was not found")
        elif arguments.command == "crypto-universe-materialize":
            value = MaterializeCryptoUniverse(repository).execute(
                snapshot_id=arguments.snapshot_id,
                capture_id=arguments.capture_id,
                created_at=now,
            )
        elif arguments.command == "crypto-universe-inspect":
            value = repository.universe_snapshot(arguments.snapshot_id)
            if value is None:
                raise SystemExit("exact crypto universe snapshot was not found")
        elif arguments.command == "crypto-discovery-run":
            value = RunCryptoDiscovery(repository).execute(
                run_id=arguments.run_id,
                snapshot_id=arguments.snapshot_id,
                generated_at=now,
            )
        elif arguments.command == "crypto-discovery-inspect":
            value = repository.discovery_run(arguments.run_id)
            if value is None:
                raise SystemExit("exact crypto discovery run was not found")
        elif arguments.command == "crypto-opportunity-inspect":
            value = repository.opportunity(arguments.opportunity_id)
            if value is None:
                raise SystemExit("exact crypto research Opportunity was not found")
        elif arguments.command in {
            "crypto-candidate-definition-validate",
            "crypto-candidate-definition-compile",
            "crypto-candidate-definition-register",
            "crypto-candidate-definition-compare",
        }:
            path = Path(arguments.definition_file).resolve()
            if not path.is_file() or path.stat().st_size > 128_000:
                raise SystemExit("generic candidate definition file is missing or too large")
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise SystemExit("generic candidate definition is not valid UTF-8 JSON") from error
            if not isinstance(payload, dict):
                raise SystemExit("generic candidate definition must be a JSON object")
            definition = GenericCandidateDefinition.from_mapping(payload)
            if definition.definition_id != arguments.definition_id:
                raise SystemExit("exact generic candidate definition identity does not match")
            plan = compile_candidate_definition(definition)
            if arguments.command == "crypto-candidate-definition-validate":
                value = {
                    "definition": definition,
                    "validated": True,
                    "structured_contract": candidate_definition_mapping(definition),
                }
            elif arguments.command == "crypto-candidate-definition-compile":
                value = {"definition": definition, "compiled_plan": plan}
            elif arguments.command == "crypto-candidate-definition-register":
                if not arguments.idempotency_key.strip():
                    raise SystemExit("generic definition idempotency key is required")
                retained = repository.save_generic_definition(definition)
                retained_plan = repository.save_generic_plan(plan)
                value = {
                    "definition": retained,
                    "compiled_plan": retained_plan,
                    "idempotency_key": arguments.idempotency_key,
                    "candidate_budget_consumed": False,
                    "experiment_budget_consumed": False,
                }
            else:
                value = {
                    "definition": definition,
                    "comparisons": tuple(
                        GenericMaterialComparison.create(
                            definition=definition,
                            prior_strategy_id=item.historical_strategy_id,
                            prior_semantics_hash=item.historical_semantics_hash,
                        )
                        for item in retained_strategy_compatibility()
                    ),
                }
        elif arguments.command == "crypto-candidate-definition-inspect":
            definition = repository.generic_definition(arguments.definition_id)
            if definition is None:
                raise SystemExit("exact generic candidate definition was not found")
            value = {
                "definition": definition,
                "compiled_plan": repository.generic_plan(
                    f"generic-crypto-plan-{definition.semantics_hash[:24]}"
                ),
            }
        elif arguments.command == "crypto-candidate-generic-compatibility":
            value = {
                "items": retained_strategy_compatibility(),
                "historical_records_mutated": False,
            }
        elif arguments.command == "crypto-candidate-generic-fixture-acceptance":
            value = generic_fixture_acceptance()
        elif arguments.command == "crypto-candidate-generic-policy-review":
            campaign = repository.campaign(arguments.campaign_id)
            if campaign is None:
                raise SystemExit("exact crypto campaign was not found")
            value = campaign_policy_v1_finding(
                campaign_question=campaign.question,
                campaign_canonical_hash=campaign.canonical_hash,
                reviewed_at=now,
            )
        elif arguments.command == "crypto-candidate-generic-status":
            value = {
                "generic_operability": generic_workspace(
                    definitions=repository.generic_definitions(),
                    plans=repository.generic_plans(),
                ),
                "production_campaign": candidate_2_workspace(repository),
            }
        elif arguments.command == "crypto-exposure-record":
            value = RecordDatasetExposure(repository).execute(
                exposure_id=arguments.exposure_id,
                snapshot_id=arguments.snapshot_id,
                purpose=arguments.purpose,
                category=ExposureCategory(arguments.category),
                first_known_exposure_at=_crypto_timestamp(arguments.first_known_exposure_at),
                recorded_at=now,
                actor=arguments.actor,
                reason=arguments.reason,
                idempotency_key=arguments.idempotency_key,
                related_hypothesis_ids=tuple(arguments.related_hypothesis_id),
                related_strategy_ids=tuple(arguments.related_strategy_id),
                related_protocol_ids=tuple(arguments.related_protocol_id),
                related_experiment_ids=tuple(arguments.related_experiment_id),
                related_dossier_ids=tuple(arguments.related_dossier_id),
                known_limitations=tuple(arguments.known_limitation),
            )
        elif arguments.command == "crypto-exposure-inspect":
            records = repository.exposures(arguments.snapshot_id)
            value = {
                "records": records,
                "summary": DatasetExposureSummary.from_records(arguments.snapshot_id, records),
            }
        elif arguments.command == "crypto-campaign-register":
            value = RegisterCryptoCampaign(repository).execute(
                campaign_id=arguments.campaign_id,
                actor=arguments.actor,
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-campaign-attach-dataset":
            value = AttachCampaignDataset(repository).execute(
                attachment_id=arguments.attachment_id,
                campaign_id=arguments.campaign_id,
                snapshot_id=arguments.snapshot_id,
                role=CampaignDatasetRole(arguments.role),
                actor=arguments.actor,
                reason=arguments.reason,
                attached_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command in {
            "crypto-campaign-inspect",
            "crypto-campaign-budget",
            "crypto-campaign-nomination-eligibility",
        }:
            campaign = repository.campaign(arguments.campaign_id)
            if campaign is None:
                raise SystemExit("exact crypto research campaign was not found")
            candidates = repository.campaign_candidates(arguments.campaign_id)
            experiments = repository.campaign_experiments(arguments.campaign_id)
            value = {
                "campaign": campaign,
                "state": campaign_state(
                    campaign, repository.campaign_events(arguments.campaign_id)
                ),
                "attachments": repository.campaign_attachments(arguments.campaign_id),
                "budget": CampaignBudgetStatus.calculate(campaign, candidates, experiments),
                "candidates": candidates,
                "experiments": experiments,
                "nomination_eligible": False,
                "nomination_reason": (
                    "NO_REGISTERED_CAMPAIGN_CANDIDATE"
                    if not candidates
                    else "CANDIDATE_REQUIRES_BOTH_POLICY_PASSING_DEVELOPMENT_RUNS"
                ),
            }
        elif arguments.command == "crypto-snapshot-access-check":
            value = snapshot_access(
                repository,
                snapshot_id=arguments.snapshot_id,
                purpose=SnapshotUsePurpose(arguments.purpose),
                campaign_id=arguments.campaign_id,
                candidate_id=arguments.candidate_id,
            )
        elif arguments.command == "crypto-holdout-capture-start":
            source = (
                FixtureCoinbaseCandleSource()
                if arguments.mode == "fixture"
                else CoinbasePublicCandleSource()
            )
            network_involved = source.network_access
            prior = repository.capture(arguments.capture_id)
            network_performed = source.network_access and (
                prior is None or prior.status.value == "RUNNING"
            )
            value = CaptureSealedHoldout(repository, source).start(
                campaign_id=arguments.campaign_id,
                capture_id=arguments.capture_id,
                actor=arguments.actor,
                requested_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-holdout-capture-resume":
            prior = repository.capture(arguments.capture_id)
            if prior is None:
                raise SystemExit("exact crypto holdout capture was not found")
            source = (
                CoinbasePublicCandleSource()
                if prior.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
                else FixtureCoinbaseCandleSource()
            )
            network_involved = source.network_access
            network_performed = source.network_access and prior.status.value == "RUNNING"
            value = CaptureSealedHoldout(repository, source).resume(
                campaign_id=arguments.campaign_id,
                capture_id=arguments.capture_id,
                actor=arguments.actor,
                requested_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-holdout-checkpoints":
            capture = repository.capture(arguments.capture_id)
            if capture is None:
                raise SystemExit("exact crypto holdout capture was not found")
            try:
                repository.candles(arguments.capture_id)
                value = {
                    "capture": capture,
                    "checkpoints": repository.checkpoints(arguments.capture_id),
                    "attempts": repository.attempts(arguments.capture_id),
                    "values_redacted": False,
                }
            except CryptoResearchConflict as error:
                if "SEALED_HOLDOUT" not in str(error):
                    raise
                campaign = repository.campaign(FIXED_CAMPAIGN_ID)
                if campaign is None:
                    raise SystemExit("exact crypto holdout campaign was not found") from error
                value = {
                    "capture": HoldoutMetadata.create(
                        campaign=campaign,
                        capture=capture,
                        snapshot=repository.snapshot(campaign.holdout_snapshot_id),
                        events=repository.holdout_events(campaign.holdout_id),
                    ),
                    "checkpoints": "REDACTED_AFTER_SEAL",
                    "attempts": "REDACTED_AFTER_SEAL",
                    "values_redacted": True,
                    "raw_responses_redacted": True,
                }
        elif arguments.command == "crypto-holdout-materialize":
            value = MaterializeSealedHoldout(repository).execute(
                campaign_id=arguments.campaign_id,
                snapshot_id=arguments.snapshot_id,
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-holdout-seal":
            value = SealHoldout(repository).execute(
                event_id=arguments.event_id,
                exposure_id=arguments.exposure_id,
                attachment_id=arguments.attachment_id,
                campaign_id=arguments.campaign_id,
                snapshot_id=arguments.snapshot_id,
                actor=arguments.actor,
                reason=arguments.reason,
                sealed_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-holdout-inspect":
            campaign = repository.campaign(arguments.campaign_id)
            if campaign is None or campaign.holdout_id != arguments.holdout_id:
                raise SystemExit("exact crypto holdout was not found")
            value = HoldoutMetadata.create(
                campaign=campaign,
                capture=repository.capture(campaign.holdout_capture_id),
                snapshot=repository.snapshot(campaign.holdout_snapshot_id),
                events=repository.holdout_events(campaign.holdout_id),
            )
        elif arguments.command == "crypto-holdout-release-authorize":
            value = AuthorizeHoldoutRelease(repository).execute(
                campaign_event_id=arguments.campaign_event_id,
                holdout_event_id=arguments.holdout_event_id,
                exposure_id=arguments.exposure_id,
                campaign_id=arguments.campaign_id,
                candidate_id=arguments.candidate_id,
                actor=arguments.actor,
                reason=arguments.reason,
                authorized_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-hypothesis-register":
            value = RegisterCryptoHypothesis(repository).execute(
                hypothesis_id=arguments.hypothesis_id,
                revision=arguments.revision,
                actor=arguments.actor,
                created_at=now,
                supersedes_version_id=arguments.supersedes_version_id,
            )
        elif arguments.command == "crypto-hypothesis-inspect":
            hypothesis = repository.hypothesis_version(arguments.hypothesis_version_id)
            specification = repository.hypothesis_specification(arguments.hypothesis_version_id)
            if hypothesis is None or specification is None:
                raise SystemExit("exact crypto hypothesis version was not found")
            value = {"hypothesis": hypothesis, "specification": specification}
        elif arguments.command == "crypto-capture-start":
            if arguments.product != COINBASE_PRODUCT:
                raise SystemExit("crypto capture permits exactly BTC-USD")
            source = (
                FixtureCoinbaseCandleSource()
                if arguments.mode == "fixture"
                else CoinbasePublicCandleSource()
            )
            network_involved = source.network_access
            prior = repository.capture(arguments.capture_id)
            network_performed = source.network_access and (
                prior is None or prior.status.value == "RUNNING"
            )
            value = CaptureCoinbaseHistory(repository, source).start(
                capture_id=arguments.capture_id,
                start=_crypto_timestamp(arguments.start),
                end=_crypto_timestamp(arguments.end),
                requested_at=now,
            )
        elif arguments.command == "crypto-capture-resume":
            prior = repository.capture(arguments.capture_id)
            if prior is None:
                raise SystemExit("exact crypto capture was not found")
            source = (
                CoinbasePublicCandleSource()
                if prior.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
                else FixtureCoinbaseCandleSource()
            )
            network_involved = source.network_access
            network_performed = source.network_access and prior.status.value == "RUNNING"
            value = CaptureCoinbaseHistory(repository, source).resume(
                capture_id=arguments.capture_id,
                requested_at=now,
            )
        elif arguments.command == "crypto-capture-inspect":
            capture = repository.capture(arguments.capture_id)
            if capture is None:
                raise SystemExit("exact crypto capture was not found")
            try:
                candle_count = len(repository.candles(arguments.capture_id))
                attempts = repository.attempts(arguments.capture_id)
                raw_responses: object = sum(
                    repository.raw_response(item.attempt_id) is not None for item in attempts
                )
                values_redacted = False
            except CryptoResearchConflict as error:
                if "SEALED_HOLDOUT" not in str(error):
                    raise
                candle_count = capture.accepted_buckets
                attempts = ()
                raw_responses = "REDACTED"
                values_redacted = True
            reported_capture: object = capture
            campaign = repository.campaign(FIXED_CAMPAIGN_ID)
            if (
                values_redacted
                and campaign is not None
                and campaign.holdout_capture_id == capture.capture_id
            ):
                reported_capture = HoldoutMetadata.create(
                    campaign=campaign,
                    capture=capture,
                    snapshot=repository.snapshot(campaign.holdout_snapshot_id),
                    events=repository.holdout_events(campaign.holdout_id),
                )
            value = {
                "capture": reported_capture,
                "attempts": attempts,
                "checkpoints": ()
                if values_redacted
                else repository.checkpoints(arguments.capture_id),
                "candle_count": candle_count,
                "raw_responses_retained": raw_responses,
                "values_redacted": values_redacted,
                "raw_responses_redacted": values_redacted,
            }
        elif arguments.command == "crypto-snapshot-materialize":
            value = MaterializeCryptoHistoricalSnapshot(repository).execute(
                snapshot_id=arguments.snapshot_id,
                capture_id=arguments.capture_id,
                created_at=now,
            )
        elif arguments.command == "crypto-snapshot-inspect":
            value = repository.snapshot(arguments.snapshot_id)
            if value is None:
                raise SystemExit("exact crypto snapshot was not found")
            campaign = repository.campaign(FIXED_CAMPAIGN_ID)
            if campaign is not None and value.snapshot_id == campaign.holdout_snapshot_id:
                value = HoldoutMetadata.create(
                    campaign=campaign,
                    capture=repository.capture(campaign.holdout_capture_id),
                    snapshot=value,
                    events=repository.holdout_events(campaign.holdout_id),
                )
        elif arguments.command == "crypto-experiment-run":
            value = ExecuteCryptoTrendValidation(repository).execute(
                run_id=arguments.run_id,
                hypothesis_version_id=arguments.hypothesis_version_id,
                snapshot_id=arguments.snapshot_id,
                code_revision=_clean_git_revision(),
                executed_at=now,
            )
        elif arguments.command == "crypto-experiment-replay":
            value = ExecuteCryptoTrendValidation(repository).replay(arguments.run_id)
        elif arguments.command == "crypto-experiment-inspect":
            value = repository.run(arguments.run_id)
            if value is None:
                raise SystemExit("exact crypto experiment run was not found")
        elif arguments.command == "crypto-outcome-record":
            value = RecordCryptoExperimentOutcome(repository).execute(
                outcome_id=arguments.outcome_id,
                run_id=arguments.run_id,
                actor=arguments.actor,
                decided_at=now,
            )
        elif arguments.command == "crypto-outcome-inspect":
            value = repository.outcome(arguments.outcome_id)
            if value is None:
                raise SystemExit("exact crypto experiment outcome was not found")
        elif arguments.command == "crypto-strategy-register":
            value = RegisterFrozenCryptoStrategy(repository).execute(
                specification_id=arguments.specification_id,
                created_at=now,
            )
        elif arguments.command == "crypto-strategy-inspect":
            value = repository.strategy_specification(arguments.specification_id)
            if value is None:
                raise SystemExit("exact crypto strategy specification was not found")
        elif arguments.command == "crypto-strategy-check":
            value = repository.strategy_by_semantics(arguments.semantics_hash)
            if value is None:
                value = {
                    "semantics_hash": arguments.semantics_hash,
                    "status": "UNTESTED_SEMANTICS",
                    "authority": "NONE",
                }
            else:
                value = {
                    "semantics_hash": arguments.semantics_hash,
                    "status": "ALREADY_TESTED",
                    "strategy_specification": value,
                    "authority": "NONE",
                }
        elif arguments.command == "crypto-replication-protocol-register":
            value = RegisterCryptoReplicationProtocol(repository).execute(
                protocol_id=arguments.protocol_id,
                strategy_specification_id=arguments.strategy_specification_id,
                created_at=now,
            )
        elif arguments.command == "crypto-replication-protocol-inspect":
            value = repository.replication_protocol(arguments.protocol_id)
            if value is None:
                raise SystemExit("exact crypto replication protocol was not found")
        elif arguments.command == "crypto-replication-run":
            value = ExecuteCryptoDisjointReplication(repository).execute(
                run_id=arguments.run_id,
                protocol_id=arguments.protocol_id,
                snapshot_id=arguments.snapshot_id,
                code_revision=_clean_git_revision(),
                executed_at=now,
            )
        elif arguments.command == "crypto-replication-replay":
            value = ExecuteCryptoDisjointReplication(repository).replay(arguments.run_id)
        elif arguments.command == "crypto-replication-inspect":
            value = repository.replication_run(arguments.run_id)
            if value is None:
                raise SystemExit("exact crypto replication run was not found")
        elif arguments.command == "crypto-replication-outcome-record":
            value = RecordCryptoReplicationOutcome(repository).execute(
                outcome_id=arguments.outcome_id,
                run_id=arguments.run_id,
                actor=arguments.actor,
                decided_at=now,
            )
        elif arguments.command == "crypto-replication-outcome-inspect":
            value = repository.replication_outcome(arguments.outcome_id)
            if value is None:
                raise SystemExit("exact crypto replication outcome was not found")
        elif arguments.command == "crypto-dossier-materialize":
            value = MaterializeCryptoEvidenceDossier(repository).execute(
                dossier_id=arguments.dossier_id,
                replication_outcome_id=arguments.replication_outcome_id,
                created_at=now,
            )
        elif arguments.command == "crypto-dossier-inspect":
            value = repository.dossier(arguments.dossier_id)
            if value is None:
                raise SystemExit("exact crypto evidence dossier was not found")
        elif arguments.command == "crypto-dossier-conclusion-record":
            value = RecordCryptoDossierConclusion(repository).execute(
                conclusion_id=arguments.conclusion_id,
                dossier_id=arguments.dossier_id,
                actor=arguments.actor,
                decided_at=now,
            )
        elif arguments.command == "crypto-dossier-conclusion-inspect":
            value = repository.dossier_conclusion(arguments.conclusion_id)
            if value is None:
                raise SystemExit("exact crypto dossier conclusion was not found")
        elif arguments.command == "crypto-candidate-hypothesis-register":
            value = RegisterCandidateHypothesis(repository).execute(
                hypothesis_id=arguments.hypothesis_id,
                campaign_id=arguments.campaign_id,
                actor=arguments.actor,
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-hypothesis-inspect":
            hypothesis = repository.hypothesis_version(arguments.hypothesis_version_id)
            specification = repository.candidate_hypothesis(arguments.hypothesis_version_id)
            if hypothesis is None or specification is None:
                raise SystemExit("exact campaign candidate hypothesis was not found")
            value = {"hypothesis": hypothesis, "specification": specification}
        elif arguments.command == "crypto-candidate-strategy-register":
            value = RegisterCandidateStrategy(repository).execute(
                specification_id=arguments.specification_id,
                hypothesis_version_id=arguments.hypothesis_version_id,
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-strategy-inspect":
            strategy = repository.strategy_specification(arguments.specification_id)
            if strategy is None:
                raise SystemExit("exact campaign candidate strategy was not found")
            value = strategy
        elif arguments.command == "crypto-candidate-register":
            value = RegisterCandidate(repository).execute(
                candidate_id=arguments.candidate_id,
                campaign_id=arguments.campaign_id,
                hypothesis_version_id=arguments.hypothesis_version_id,
                strategy_specification_id=arguments.strategy_specification_id,
                actor=arguments.actor,
                registered_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-inspect":
            value = repository.campaign_candidate(arguments.candidate_id)
            if value is None:
                raise SystemExit("exact campaign candidate was not found")
        elif arguments.command == "crypto-candidate-comparison-inspect":
            value = repository.candidate_comparisons(arguments.candidate_id)
            if value is None:
                raise SystemExit("exact campaign candidate comparison was not found")
        elif arguments.command == "crypto-candidate-protocol-register":
            value = RegisterCandidateProtocol(repository).execute(
                protocol_id=arguments.protocol_id,
                campaign_id=arguments.campaign_id,
                candidate_id=arguments.candidate_id,
                code_revision=_clean_git_revision(),
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-protocol-inspect":
            value = repository.candidate_protocol(arguments.protocol_id)
            if value is None:
                raise SystemExit("exact campaign candidate protocol was not found")
        elif arguments.command == "crypto-candidate-window-run":
            value = ExecuteCandidateWindow(repository).execute(
                run_id=arguments.run_id,
                consumption_id=arguments.consumption_id,
                protocol_id=arguments.protocol_id,
                snapshot_id=arguments.snapshot_id,
                code_revision=_clean_git_revision(),
                actor=arguments.actor,
                executed_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-window-replay":
            value = ExecuteCandidateWindow(repository).replay(arguments.run_id)
        elif arguments.command == "crypto-candidate-window-inspect":
            value = repository.candidate_run(arguments.run_id)
            if value is None:
                raise SystemExit("exact campaign candidate run was not found")
        elif arguments.command == "crypto-candidate-decisions-inspect":
            run = repository.candidate_run(arguments.run_id)
            if run is None:
                raise SystemExit("exact campaign candidate run was not found")
            value = run.decisions
        elif arguments.command == "crypto-candidate-metrics-inspect":
            run = repository.candidate_run(arguments.run_id)
            if run is None:
                raise SystemExit("exact campaign candidate run was not found")
            value = {
                "metrics": run.metrics,
                "prior_strategy_comparisons": run.prior_comparisons,
            }
        elif arguments.command == "crypto-candidate-outcome-record":
            value = RecordCandidateOutcome(repository).execute(
                outcome_id=arguments.outcome_id,
                run_id=arguments.run_id,
                actor=arguments.actor,
                decided_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-outcome-inspect":
            value = repository.candidate_outcome(arguments.outcome_id)
            if value is None:
                raise SystemExit("exact campaign candidate outcome was not found")
        elif arguments.command == "crypto-candidate-dossier-materialize":
            value = MaterializeCandidateDossier(repository).execute(
                dossier_id=arguments.dossier_id,
                campaign_id=arguments.campaign_id,
                candidate_id=arguments.candidate_id,
                protocol_id=arguments.protocol_id,
                earlier_outcome_id=arguments.earlier_outcome_id,
                later_outcome_id=arguments.later_outcome_id,
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-dossier-inspect":
            value = repository.candidate_dossier(arguments.dossier_id)
            if value is None:
                raise SystemExit("exact campaign candidate dossier was not found")
        elif arguments.command == "crypto-candidate-nomination-apply":
            value = ApplyCandidateNominationPolicy(repository).execute(
                evaluation_id=arguments.evaluation_id,
                dossier_id=arguments.dossier_id,
                nomination_event_id=arguments.nomination_event_id,
                development_closed_event_id=arguments.development_closed_event_id,
                actor=arguments.actor,
                reason=arguments.reason,
                evaluated_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-nomination-inspect":
            value = repository.candidate_nomination(arguments.evaluation_id)
            if value is None:
                raise SystemExit("exact campaign candidate nomination evaluation was not found")
        elif arguments.command == "crypto-candidate-2-hypothesis-register":
            value = RegisterBreakoutHypothesis(repository).execute(
                hypothesis_id=arguments.hypothesis_id,
                campaign_id=arguments.campaign_id,
                actor=arguments.actor,
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-2-hypothesis-inspect":
            hypothesis = repository.hypothesis_version(arguments.hypothesis_version_id)
            specification = repository.breakout_hypothesis(arguments.hypothesis_version_id)
            if hypothesis is None or specification is None:
                raise SystemExit("exact Candidate 2 hypothesis was not found")
            value = {"hypothesis": hypothesis, "specification": specification}
        elif arguments.command == "crypto-candidate-2-strategy-register":
            value = RegisterBreakoutStrategy(repository).execute(
                specification_id=arguments.specification_id,
                hypothesis_version_id=arguments.hypothesis_version_id,
                actor=arguments.actor,
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-2-strategy-inspect":
            value = repository.strategy_specification(arguments.specification_id)
            if value is None:
                raise SystemExit("exact Candidate 2 strategy was not found")
        elif arguments.command == "crypto-candidate-2-register":
            value = RegisterBreakoutCandidate(repository).execute(
                candidate_id=arguments.candidate_id,
                campaign_id=arguments.campaign_id,
                hypothesis_version_id=arguments.hypothesis_version_id,
                strategy_specification_id=arguments.strategy_specification_id,
                actor=arguments.actor,
                registered_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-2-inspect":
            value = repository.campaign_candidate(arguments.candidate_id)
            if value is None:
                raise SystemExit("exact Candidate 2 registration was not found")
        elif arguments.command == "crypto-candidate-2-comparison-inspect":
            value = repository.breakout_comparisons(arguments.candidate_id)
            if value is None:
                raise SystemExit("exact Candidate 2 comparison was not found")
        elif arguments.command == "crypto-candidate-2-protocol-register":
            value = RegisterBreakoutProtocol(repository).execute(
                protocol_id=arguments.protocol_id,
                campaign_id=arguments.campaign_id,
                candidate_id=arguments.candidate_id,
                code_revision=_clean_git_revision(),
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-2-protocol-inspect":
            value = repository.breakout_protocol(arguments.protocol_id)
            if value is None:
                raise SystemExit("exact Candidate 2 protocol was not found")
        elif arguments.command == "crypto-candidate-2-window-run":
            value = ExecuteBreakoutWindow(repository).execute(
                run_id=arguments.run_id,
                consumption_id=arguments.consumption_id,
                protocol_id=arguments.protocol_id,
                snapshot_id=arguments.snapshot_id,
                code_revision=_clean_git_revision(),
                actor=arguments.actor,
                executed_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-2-window-replay":
            value = ExecuteBreakoutWindow(repository).replay(arguments.run_id)
        elif arguments.command == "crypto-candidate-2-window-inspect":
            value = repository.breakout_run(arguments.run_id)
            if value is None:
                raise SystemExit("exact Candidate 2 run was not found")
        elif arguments.command == "crypto-candidate-2-decisions-inspect":
            run = repository.breakout_run(arguments.run_id)
            if run is None:
                raise SystemExit("exact Candidate 2 run was not found")
            value = run.decisions
        elif arguments.command == "crypto-candidate-2-metrics-inspect":
            run = repository.breakout_run(arguments.run_id)
            if run is None:
                raise SystemExit("exact Candidate 2 run was not found")
            value = {
                "metrics": run.metrics,
                "prior_strategy_comparisons": run.prior_comparisons,
            }
        elif arguments.command == "crypto-candidate-2-outcome-record":
            value = RecordBreakoutOutcome(repository).execute(
                outcome_id=arguments.outcome_id,
                run_id=arguments.run_id,
                actor=arguments.actor,
                decided_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-2-outcome-inspect":
            value = repository.breakout_outcome(arguments.outcome_id)
            if value is None:
                raise SystemExit("exact Candidate 2 outcome was not found")
        elif arguments.command == "crypto-candidate-2-dossier-materialize":
            value = MaterializeBreakoutDossier(repository).execute(
                dossier_id=arguments.dossier_id,
                campaign_id=arguments.campaign_id,
                candidate_id=arguments.candidate_id,
                protocol_id=arguments.protocol_id,
                earlier_outcome_id=arguments.earlier_outcome_id,
                later_outcome_id=arguments.later_outcome_id,
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-2-dossier-inspect":
            value = repository.breakout_dossier(arguments.dossier_id)
            if value is None:
                raise SystemExit("exact Candidate 2 dossier was not found")
        elif arguments.command == "crypto-candidate-2-nomination-apply":
            value = ApplyBreakoutNominationPolicy(repository).execute(
                evaluation_id=arguments.evaluation_id,
                dossier_id=arguments.dossier_id,
                nomination_event_id=arguments.nomination_event_id,
                development_closed_event_id=arguments.development_closed_event_id,
                actor=arguments.actor,
                reason=arguments.reason,
                evaluated_at=now,
                idempotency_key=arguments.idempotency_key,
            )
        elif arguments.command == "crypto-candidate-2-nomination-inspect":
            value = repository.breakout_nomination(arguments.evaluation_id)
            if value is None:
                raise SystemExit("exact Candidate 2 nomination evaluation was not found")
        elif arguments.command in {
            "crypto-candidate-2-status",
            "crypto-candidate-2-holdout-non-use",
        }:
            workspace = candidate_2_workspace(repository)
            value = (
                workspace["holdout_non_use"]
                if arguments.command == "crypto-candidate-2-holdout-non-use"
                else workspace
            )
        elif arguments.command in {
            "crypto-candidate-status",
            "crypto-candidate-holdout-non-use",
        }:
            workspace = candidate_2_workspace(repository)
            value = (
                workspace["holdout_non_use"]
                if arguments.command == "crypto-candidate-holdout-non-use"
                else workspace
            )
        elif arguments.command == "crypto-challenger-hypothesis-register":
            value = RegisterMonthlyMomentumHypothesis(repository).execute(
                hypothesis_id=arguments.hypothesis_id,
                actor=arguments.actor,
                created_at=now,
            )
        elif arguments.command == "crypto-challenger-hypothesis-inspect":
            hypothesis = repository.hypothesis_version(arguments.hypothesis_version_id)
            specification = repository.challenger_hypothesis(arguments.hypothesis_version_id)
            if hypothesis is None or specification is None:
                raise SystemExit("exact crypto challenger hypothesis was not found")
            value = {"hypothesis": hypothesis, "specification": specification}
        elif arguments.command == "crypto-challenger-strategy-register":
            value = RegisterMonthlyMomentumStrategy(repository).execute(
                specification_id=arguments.specification_id,
                hypothesis_version_id=arguments.hypothesis_version_id,
                parent_specification_id=arguments.parent_specification_id,
                created_at=now,
            )
        elif arguments.command in {
            "crypto-challenger-strategy-inspect",
            "crypto-challenger-lineage-inspect",
        }:
            strategy = repository.strategy_specification(arguments.specification_id)
            if strategy is None:
                raise SystemExit("exact crypto challenger strategy was not found")
            parent = repository.strategy_specification(strategy.parent_specification_id or "")
            value = {"strategy": strategy, "parent": parent}
        elif arguments.command == "crypto-challenger-protocol-register":
            value = RegisterCryptoChallengerProtocol(repository).execute(
                protocol_id=arguments.protocol_id,
                hypothesis_version_id=arguments.hypothesis_version_id,
                strategy_specification_id=arguments.strategy_specification_id,
                code_revision=_clean_git_revision(),
                created_at=now,
            )
        elif arguments.command == "crypto-challenger-protocol-inspect":
            value = repository.challenger_protocol(arguments.protocol_id)
            if value is None:
                raise SystemExit("exact crypto challenger protocol was not found")
        elif arguments.command == "crypto-challenger-window-run":
            value = ExecuteCryptoChallengerWindow(repository).execute(
                run_id=arguments.run_id,
                protocol_id=arguments.protocol_id,
                snapshot_id=arguments.snapshot_id,
                code_revision=_clean_git_revision(),
                executed_at=now,
            )
        elif arguments.command == "crypto-challenger-window-replay":
            value = ExecuteCryptoChallengerWindow(repository).replay(arguments.run_id)
        elif arguments.command == "crypto-challenger-window-inspect":
            value = repository.challenger_run(arguments.run_id)
            if value is None:
                raise SystemExit("exact crypto challenger run was not found")
        elif arguments.command == "crypto-challenger-outcome-record":
            value = RecordCryptoChallengerOutcome(repository).execute(
                outcome_id=arguments.outcome_id,
                run_id=arguments.run_id,
                actor=arguments.actor,
                decided_at=now,
            )
        elif arguments.command == "crypto-challenger-outcome-inspect":
            value = repository.challenger_outcome(arguments.outcome_id)
            if value is None:
                raise SystemExit("exact crypto challenger outcome was not found")
        elif arguments.command == "crypto-challenger-dossier-materialize":
            value = MaterializeCryptoChallengerDossier(repository).execute(
                dossier_id=arguments.dossier_id,
                protocol_id=arguments.protocol_id,
                earlier_outcome_id=arguments.earlier_outcome_id,
                later_outcome_id=arguments.later_outcome_id,
                created_at=now,
            )
        elif arguments.command == "crypto-challenger-dossier-inspect":
            value = repository.challenger_dossier(arguments.dossier_id)
            if value is None:
                raise SystemExit("exact crypto challenger dossier was not found")
        elif arguments.command == "crypto-challenger-conclusion-record":
            value = RecordCryptoChallengerConclusion(repository).execute(
                conclusion_id=arguments.conclusion_id,
                dossier_id=arguments.dossier_id,
                actor=arguments.actor,
                decided_at=now,
            )
        elif arguments.command == "crypto-challenger-conclusion-inspect":
            value = repository.challenger_conclusion(arguments.conclusion_id)
            if value is None:
                raise SystemExit("exact crypto challenger conclusion was not found")
        elif arguments.command == "crypto-challenger-compare-prior":
            run = repository.challenger_run(arguments.run_id)
            if run is None:
                raise SystemExit("exact crypto challenger run was not found")
            value = run.prior_comparison
        elif arguments.command == "crypto-approved-champion-status":
            value = {
                "status": NO_APPROVED_CHAMPION,
                "strategy_specifications": len(repository.strategy_specifications()),
                "authority": "NONE",
            }
        elif arguments.command == "crypto-research-workspace":
            value = candidate_2_workspace(repository)
            value["multi_asset_discovery"] = crypto_universe_workspace(repository)
            value["market_pulse"] = crypto_market_pulse_workspace(repository)
        else:
            raise SystemExit(f"unsupported crypto research command: {arguments.command}")
        evidence_class = _reported_research_evidence_class(value, repository)
        challenger_command = arguments.command.startswith(
            ("crypto-challenger-", "crypto-candidate-")
        )
        if challenger_command:
            evidence_class = "HISTORICAL_PUBLIC"
        pulse_command = arguments.command.startswith("crypto-pulse-")
        research_state = (
            value.get("research_state", "REJECTED_RESEARCH")
            if isinstance(value, dict) and not pulse_command
            else "MARKET_PULSE_RESEARCH"
            if pulse_command
            else "CHALLENGER_RESEARCH"
            if challenger_command
            else "REJECTED_RESEARCH"
        )
        _research_evidence_json(
            {
                "result": value,
                "evidence_class": evidence_class,
                "network_access_involved": network_involved,
                "network_request_performed": network_performed,
                "financial_authority": "NONE",
                "execution_authority": "NONE",
                "orders": 0,
                "positions": 0,
                "reservations": 0,
                "broker_writes": 0,
                "financial_journals": 0,
                "paper_eligible": False,
                "live_eligible": False,
                "research_state": research_state,
                "capital_authority": "NONE",
                "opportunity_authority": "NONE",
                "order_authority": "NONE",
                "financial_mutations_possible": False,
            }
        )
    finally:
        engine.dispose()


def _broad_market_runtime() -> tuple[Any, Any, Any, Any, Any]:
    """Engine plus the durable repositories this command family composes."""
    from apex.adapters.broad_equity_postgres import PostgresBroadEquityRepository
    from apex.adapters.options_intelligence_postgres import (
        PostgresOptionsIntelligenceRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_evidence_postgres import PostgresResearchEvidenceRepository
    from apex.adapters.security_reference_postgres import PostgresSecurityReferenceRepository

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        engine,
        PostgresResearchEvidenceRepository(engine),
        PostgresSecurityReferenceRepository(engine),
        PostgresBroadEquityRepository(engine),
        PostgresOptionsIntelligenceRepository(engine),
    )


def _drain_generic_capture(
    service: Any, capture: Any, raw_payload: bytes, records: tuple[Any, ...], chunk_size: int
) -> Any:
    from apex.domain.research_evidence import CaptureStatus

    while capture.status is CaptureStatus.RUNNING:
        capture = service.resume(
            capture_id=capture.capture_id,
            raw_payload=raw_payload,
            records=records,
            resumed_at=datetime.now(tz=UTC),
            chunk_size=chunk_size,
        )
    return capture


def _drain_options_directory_capture(
    service: Any, capture: Any, raw_payload: bytes, rejections: tuple[Any, ...], chunk_size: int
) -> Any:
    """Mirrors ``_drain_generic_capture`` for ``CaptureNasdaqOptionsDirectory``,
    whose ``resume`` takes ``rejections=`` rather than the generic ``records=``.
    """
    from apex.domain.research_evidence import CaptureStatus

    while capture.status is CaptureStatus.RUNNING:
        capture = service.resume(
            capture_id=capture.capture_id,
            raw_payload=raw_payload,
            rejections=rejections,
            resumed_at=datetime.now(tz=UTC),
            chunk_size=chunk_size,
        )
    return capture


def _distinct_contract_count(contracts: Sequence[Any]) -> int:
    """Count contracts genuinely distinct by ``canonical_contract_id``.

    The official Nasdaq options-directory source can legitimately repeat an
    identical option-series row within one capture, and
    ``RecordOptionContracts.execute()``/``save_contracts()`` deliberately
    echo their full input back unchanged rather than the deduplicated set
    actually persisted (see ``save_contracts()``'s docstring and
    ``test_save_contracts_chunks_large_batches_and_deduplicates_in_batch_repeats``).
    Counting that return value directly over-reports how many *distinct*
    contracts a capture actually persisted by exactly the in-call duplicate
    count, so any "contracts retained/persisted" report must count through
    this function instead of taking ``len()`` of the raw return value.
    """
    return len({item.canonical_contract_id for item in contracts})


def _reusable_completed_capture(repository: Any, capture_id: str) -> Any:
    """A COMPLETED capture already retained under this exact deterministic
    capture_id, if any -- ``None`` otherwise (never RUNNING/FAILED/etc.).

    ``broad-equity-reference-capture`` must be safe to invoke more than once
    per day: the official sources it reads can genuinely regenerate within a
    day, and a same-identity re-fetch that observed different bytes
    correctly fails closed (``CAPTURE_SOURCE_CONTENT_CONFLICT``) rather than
    silently overwriting retained evidence. Checking this first turns a
    same-day repeat call into a safe, zero-network-request replay instead of
    an uncontrolled crash.
    """
    from apex.domain.research_evidence import CaptureStatus

    existing = repository.capture(capture_id)
    if existing is not None and existing.status is CaptureStatus.COMPLETED:
        return existing
    return None


def _latest_completed_capture(
    repository: Any, provider: str, *, source_locator: str | None = None
) -> Any:
    from apex.application.research_evidence import latest_completed_capture

    return latest_completed_capture(repository.captures(), provider, source_locator=source_locator)


def _broad_equity_instrument_bars(securities_repository: Any) -> dict[str, tuple[Any, ...]]:
    """Every retained Securities Stage 1 bar, grouped by its own instrument_id.

    This is a distinct identity namespace from security_reference's
    canonical_security_id; no cross-reference between the two is performed by
    this milestone, and that is reported as an explicit limitation.
    """
    bars_by_instrument: dict[str, list[Any]] = {}
    for batch in securities_repository.list_batches():
        _instruments, _sessions, bars, _actions = securities_repository.get_dataset(batch.batch_id)
        for bar in bars:
            bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)
    return {
        key: tuple(sorted(value, key=lambda item: item.period_start))
        for key, value in bars_by_instrument.items()
    }


def broad_equity_research_command(arguments: argparse.Namespace) -> None:
    """Durable, repository-backed broad-equity research command surface.

    Every branch reads and writes only the durable security-reference,
    broad-equity, and options-intelligence repositories (or the pure domain
    functions over evidence already retained there); nothing here fabricates
    state, and a missing exact ID fails closed with ``SystemExit``.
    """
    from apex.adapters.alpaca_market_data import AlpacaMarketDataHttpAdapter
    from apex.adapters.alpaca_reference_capability import check_alpaca_asset_capability
    from apex.adapters.research_evidence_source import (
        NasdaqDirectoryCapabilityUnavailable,
        NasdaqDirectoryClient,
        NasdaqDirectoryClientError,
        SecCompanyTickersCapabilityUnavailable,
        SecCompanyTickersClient,
        SecCompanyTickersClientError,
    )
    from apex.adapters.security_reference import NASDAQ_OTHER_LISTED_PATH
    from apex.application.broad_equity import (
        CreateBroadEquityShadowPlan,
        RecordBroadEquityInclusion,
        RunBroadEquityDiscovery,
    )
    from apex.application.research_evidence import (
        NASDAQ_PROVIDER,
        SEC_PROVIDER,
        CaptureNasdaqDirectory,
        CaptureSecCompanyTickers,
        security_from_observation,
    )
    from apex.application.security_reference import CreateCurrentEquityUniverse
    from apex.domain.broad_equity import (
        BroadDataPolicy,
        DataReadiness,
        assess_security_data_quality,
        calculate_broad_equity_features,
        plan_broad_data_capture,
    )
    from apex.domain.research_evidence import EvidenceClass
    from apex.domain.security_reference import (
        CurrentEquityUniversePolicy,
        reconcile_security_references,
    )

    engine, research_repo, security_repo, broad_repo, _options_repo = _broad_market_runtime()
    try:
        now = datetime.now(tz=UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        command = arguments.command

        if command == "broad-equity-reference-capture":
            today = now.date().isoformat()
            value: dict[str, Any] = {"command": command, "as_of": today}

            # A completed capture already retained under today's exact
            # deterministic capture_id is reused outright rather than
            # re-fetched: the official source can genuinely drift within one
            # day (Nasdaq Trader regenerates its directories intraday), and
            # a same-identity re-fetch that observed different bytes would
            # correctly fail closed (CAPTURE_SOURCE_CONTENT_CONFLICT) rather
            # than silently overwrite retained evidence. Calling this command
            # more than once per day must stay a safe, zero-provider-request
            # replay, matching the idempotent-capture policy used throughout
            # this codebase, not an uncontrolled crash.
            nasdaq_capture_id = f"broad-equity-nasdaq-{today}"
            nasdaq_locator = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
            reusable = _reusable_completed_capture(research_repo, nasdaq_capture_id)
            if reusable is not None:
                value["nasdaq"] = {
                    "capture_id": reusable.capture_id,
                    "status": reusable.status.value,
                    "retained_observations": reusable.retained_observations,
                    "reused": True,
                }
            else:
                try:
                    raw, parsed = NasdaqDirectoryClient().capture(
                        received_at=now, timeout_seconds=20
                    )
                    capture = CaptureNasdaqDirectory(research_repo).start(
                        capture_id=nasdaq_capture_id,
                        evidence_class=EvidenceClass.GENUINE_PUBLIC,
                        source_locator=parsed.receipt.source_locator,
                        raw_payload=raw,
                        records=parsed.records,
                        requested_at=day_start,
                        ingested_at=now,
                        chunk_size=1000,
                    )
                    capture = _drain_generic_capture(
                        CaptureNasdaqDirectory(research_repo), capture, raw, parsed.records, 1000
                    )
                    value["nasdaq"] = {
                        "capture_id": capture.capture_id,
                        "status": capture.status.value,
                        "retained_observations": capture.retained_observations,
                    }
                except (
                    NasdaqDirectoryClientError,
                    NasdaqDirectoryCapabilityUnavailable,
                ) as error:
                    capture = CaptureNasdaqDirectory(research_repo).record_unavailable(
                        capture_id=nasdaq_capture_id,
                        source_locator=nasdaq_locator,
                        requested_at=day_start,
                        recorded_at=now,
                        reason=str(error),
                    )
                    value["nasdaq"] = {
                        "capture_id": capture.capture_id,
                        "status": capture.status.value,
                        "reason": capture.failure_reason,
                    }

            nasdaq_other_capture_id = f"broad-equity-nasdaq-other-{today}"
            nasdaq_other_locator = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
            reusable = _reusable_completed_capture(research_repo, nasdaq_other_capture_id)
            if reusable is not None:
                value["nasdaq_other"] = {
                    "capture_id": reusable.capture_id,
                    "status": reusable.status.value,
                    "retained_observations": reusable.retained_observations,
                    "reused": True,
                }
            else:
                try:
                    raw, parsed = NasdaqDirectoryClient().capture(
                        received_at=now, timeout_seconds=20, source_path=NASDAQ_OTHER_LISTED_PATH
                    )
                    capture = CaptureNasdaqDirectory(research_repo).start(
                        capture_id=nasdaq_other_capture_id,
                        evidence_class=EvidenceClass.GENUINE_PUBLIC,
                        source_locator=parsed.receipt.source_locator,
                        raw_payload=raw,
                        records=parsed.records,
                        requested_at=day_start,
                        ingested_at=now,
                        chunk_size=1000,
                    )
                    capture = _drain_generic_capture(
                        CaptureNasdaqDirectory(research_repo), capture, raw, parsed.records, 1000
                    )
                    value["nasdaq_other"] = {
                        "capture_id": capture.capture_id,
                        "status": capture.status.value,
                        "retained_observations": capture.retained_observations,
                    }
                except (
                    NasdaqDirectoryClientError,
                    NasdaqDirectoryCapabilityUnavailable,
                ) as error:
                    capture = CaptureNasdaqDirectory(research_repo).record_unavailable(
                        capture_id=nasdaq_other_capture_id,
                        source_locator=nasdaq_other_locator,
                        requested_at=day_start,
                        recorded_at=now,
                        reason=str(error),
                    )
                    value["nasdaq_other"] = {
                        "capture_id": capture.capture_id,
                        "status": capture.status.value,
                        "reason": capture.failure_reason,
                    }

            sec_capture_id = f"broad-equity-sec-{today}"
            sec_locator = "https://www.sec.gov/files/company_tickers.json"
            user_agent = os.environ.get(
                "APEX_SEC_USER_AGENT", "ApexBroadMarketResearch/1.0 (research@apex.local)"
            )
            reusable = _reusable_completed_capture(research_repo, sec_capture_id)
            if reusable is not None:
                value["sec"] = {
                    "capture_id": reusable.capture_id,
                    "status": reusable.status.value,
                    "retained_observations": reusable.retained_observations,
                    "reused": True,
                }
            else:
                try:
                    raw, parsed = SecCompanyTickersClient().capture(
                        user_agent=user_agent, received_at=now, timeout_seconds=20
                    )
                    capture = CaptureSecCompanyTickers(research_repo).start(
                        capture_id=sec_capture_id,
                        evidence_class=EvidenceClass.GENUINE_PUBLIC,
                        source_locator=parsed.receipt.source_locator,
                        raw_payload=raw,
                        records=parsed.records,
                        requested_at=day_start,
                        ingested_at=now,
                        chunk_size=1000,
                    )
                    capture = _drain_generic_capture(
                        CaptureSecCompanyTickers(research_repo), capture, raw, parsed.records, 1000
                    )
                    value["sec"] = {
                        "capture_id": capture.capture_id,
                        "status": capture.status.value,
                        "retained_observations": capture.retained_observations,
                    }
                except (
                    SecCompanyTickersClientError,
                    SecCompanyTickersCapabilityUnavailable,
                ) as error:
                    capture = CaptureSecCompanyTickers(research_repo).record_unavailable(
                        capture_id=sec_capture_id,
                        source_locator=sec_locator,
                        requested_at=day_start,
                        recorded_at=now,
                        reason=str(error),
                    )
                    value["sec"] = {
                        "capture_id": capture.capture_id,
                        "status": capture.status.value,
                        "reason": capture.failure_reason,
                    }

            alpaca = check_alpaca_asset_capability()
            value["alpaca"] = {
                "credentials_available": alpaca.credentials_available,
                "unavailable_reason": alpaca.unavailable_reason,
                "records_observed": len(alpaca.capture.records) if alpaca.capture else 0,
            }
            _research_evidence_json(value)
            return

        if command == "broad-equity-universe-create":
            # nasdaqlisted.txt and otherlisted.txt both report
            # provider == NASDAQ_TRADER, so provider alone cannot tell them
            # apart -- their distinct source_locator can, and must be passed
            # explicitly or "latest NASDAQ_TRADER capture" silently picks
            # only one of the two official listing sources.
            nasdaq_capture = _latest_completed_capture(
                research_repo,
                NASDAQ_PROVIDER,
                source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
            )
            nasdaq_other_capture = _latest_completed_capture(
                research_repo,
                NASDAQ_PROVIDER,
                source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
            )
            sec_capture = _latest_completed_capture(research_repo, SEC_PROVIDER)
            captures = tuple(
                item
                for item in (nasdaq_capture, nasdaq_other_capture, sec_capture)
                if item is not None
            )
            if not captures:
                raise SystemExit(
                    "no completed Nasdaq or SEC capture is retained; "
                    "run broad-equity-reference-capture first"
                )
            records = tuple(
                security_from_observation(observation)
                for capture in captures
                for observation in research_repo.observations(capture.capture_id)
            )
            reconciled = reconcile_security_references(records)
            snapshot = CreateCurrentEquityUniverse(security_repo).execute(
                securities=reconciled,
                policy=CurrentEquityUniversePolicy.initial(),
                source_receipt_ids=tuple(sorted({item.source_receipt_id for item in records})),
                as_of_date=now.date(),
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
            # A pure environment-variable check: this command never makes an
            # Alpaca request of its own (it reconciles already-retained
            # Nasdaq/SEC evidence only), so this reports credential presence
            # without ever touching the network, regardless of whether
            # credentials happen to be configured.
            alpaca_credentials_available = AlpacaMarketDataHttpAdapter().credentials_available
            _research_evidence_json(
                {
                    "command": command,
                    "universe_id": snapshot.universe_id,
                    "sources": sorted({item.provider for item in captures}),
                    "source_capture_ids": sorted(item.capture_id for item in captures),
                    "total_considered": snapshot.total_considered,
                    "research_eligible_count": snapshot.research_eligible_count,
                    "execution_capability_known_count": snapshot.execution_capability_known_count,
                    "execution_capability_unknown_count": (
                        snapshot.execution_capability_unknown_count
                    ),
                    "alpaca_credentials_available": alpaca_credentials_available,
                }
            )
            return

        if command in {"broad-equity-universe-status", "broad-equity-universe-report"}:
            universe = security_repo.universe(arguments.universe_id)
            if universe is None:
                # The row may exist but be retained under neither the current
                # nor the one known legacy schema -- distinguish that from a
                # genuinely absent ID rather than reporting both identically.
                blocker = next(
                    (
                        item.compatibility_blocker
                        for item in security_repo.universe_evidence()
                        if item.universe_id == arguments.universe_id
                    ),
                    None,
                )
                if blocker is not None:
                    raise SystemExit(
                        "broad-equity universe is retained but not readable under any "
                        f"known schema: {blocker}"
                    )
                raise SystemExit("exact broad-equity universe was not found")
            if command == "broad-equity-universe-status":
                _research_evidence_json(
                    {
                        "command": command,
                        "universe_id": universe.universe_id,
                        "policy_id": universe.policy_id,
                        "as_of_date": universe.as_of_date,
                        "total_considered": universe.total_considered,
                        "research_eligible_count": universe.research_eligible_count,
                        "research_excluded_count": (
                            universe.total_considered - universe.research_eligible_count
                        ),
                        "research_exclusion_counts": universe.research_exclusion_counts,
                        "security_type_counts": universe.security_type_counts,
                        "execution_capability_counts": universe.execution_capability_counts,
                        "execution_capability_known_count": (
                            universe.execution_capability_known_count
                        ),
                        "execution_capability_unknown_count": (
                            universe.execution_capability_unknown_count
                        ),
                        "identities_requiring_review": len(
                            security_repo.identities_requiring_review()
                        ),
                        "as_of_freshness": universe.as_of_date,
                        "warning": universe.warning,
                    }
                )
            else:
                _research_evidence_json({"command": command, "universe": universe})
            return

        if command == "broad-equity-data-plan":
            universe = security_repo.universe(arguments.universe_id)
            if universe is None:
                raise SystemExit("exact broad-equity universe was not found")
            start_date = date.fromisoformat(arguments.start)
            end_date = date.fromisoformat(arguments.end)
            sessions = max(1, (end_date - start_date).days * 5 // 7)
            data_plan = plan_broad_data_capture(
                universe_id=arguments.universe_id,
                start_date=start_date,
                end_date=end_date,
                symbol_count=max(1, universe.research_eligible_count),
                expected_sessions_per_symbol=sessions,
            )
            _research_evidence_json({"command": command, "plan": data_plan})
            return

        if command == "broad-equity-data-capture":
            from apex.adapters.securities_postgres import PostgresSecuritiesRepository
            from apex.adapters.xnys_calendar import ReviewedXNYSCalendar

            universe = security_repo.universe(arguments.universe_id)
            if universe is None:
                raise SystemExit("exact broad-equity universe was not found")
            start_date = date.fromisoformat(arguments.start)
            end_date = date.fromisoformat(arguments.end)
            bars_by_instrument = _broad_equity_instrument_bars(PostgresSecuritiesRepository(engine))
            calendar = ReviewedXNYSCalendar(start_date=arguments.start, end_date=arguments.end)
            expected_dates = tuple(session.session_date for session in calendar.sessions)
            policy = BroadDataPolicy.create()
            capture_id = arguments.idempotency_key
            qualities = tuple(
                assess_security_data_quality(
                    canonical_security_id=instrument_id,
                    bars=tuple(
                        bar for bar in bars if start_date <= bar.period_start.date() <= end_date
                    ),
                    expected_session_dates=expected_dates,
                    policy=policy,
                )
                for instrument_id, bars in sorted(bars_by_instrument.items())
                if any(start_date <= bar.period_start.date() <= end_date for bar in bars)
            )
            retained = RecordBroadEquityInclusion(broad_repo).execute(
                capture_id=capture_id, qualities=qualities, recorded_at=now
            )
            included_count = sum(
                item.readiness in (DataReadiness.READY, DataReadiness.PARTIAL) for item in retained
            )
            _research_evidence_json(
                {
                    "command": command,
                    "capture_id": capture_id,
                    "instruments_considered": len(qualities),
                    "included_count": included_count,
                    "excluded_count": len(retained) - included_count,
                    "limitations": (
                        "SECURITIES_STAGE1_INSTRUMENT_ID_USED_AS_IDENTITY",
                        "NO_CROSS_REFERENCE_TO_SECURITY_REFERENCE_CANONICAL_ID_PERFORMED",
                    ),
                }
            )
            return

        if command in {"broad-equity-data-status", "broad-equity-data-quality"}:
            qualities = broad_repo.qualities_for_capture(arguments.capture_id)
            if not qualities:
                raise SystemExit("exact broad-equity data capture has no retained quality evidence")
            if command == "broad-equity-data-status":
                by_readiness: dict[str, int] = {}
                for item in qualities:
                    by_readiness[item.readiness.value] = (
                        by_readiness.get(item.readiness.value, 0) + 1
                    )
                _research_evidence_json(
                    {
                        "command": command,
                        "capture_id": arguments.capture_id,
                        "instrument_count": len(qualities),
                        "by_readiness": by_readiness,
                    }
                )
            else:
                _research_evidence_json(
                    {"command": command, "capture_id": arguments.capture_id, "qualities": qualities}
                )
            return

        if command == "broad-equity-discovery-run":
            from apex.adapters.securities_postgres import PostgresSecuritiesRepository

            qualities = broad_repo.qualities_for_capture(arguments.data_capture_id)
            if not qualities:
                raise SystemExit("exact broad-equity data capture has no retained quality evidence")
            bars_by_instrument = _broad_equity_instrument_bars(PostgresSecuritiesRepository(engine))
            included = tuple(
                item
                for item in qualities
                if item.readiness in (DataReadiness.READY, DataReadiness.PARTIAL)
            )
            features = tuple(
                calculate_broad_equity_features(
                    canonical_security_id=quality.canonical_security_id,
                    bars=bars_by_instrument[quality.canonical_security_id],
                    quality=quality,
                )
                for quality in included
                if len(bars_by_instrument.get(quality.canonical_security_id, ())) >= 20
            )
            if not features:
                raise SystemExit(
                    "no included security under this data capture has enough retained "
                    "bars to compute discovery features"
                )
            used_ids = {item.canonical_security_id for item in features}
            run = RunBroadEquityDiscovery(broad_repo).execute(
                universe_id=arguments.universe_id,
                data_capture_id=arguments.data_capture_id,
                features=features,
                qualities=tuple(
                    item for item in included if item.canonical_security_id in used_ids
                ),
                idempotency_key=arguments.idempotency_key,
            )
            _research_evidence_json(
                {
                    "command": command,
                    "run_id": run.run_id,
                    "regime": run.regime.regime.value,
                    "opportunity_count": len(run.opportunities),
                }
            )
            return

        if command == "broad-equity-discovery-report":
            run = broad_repo.discovery(arguments.run_id)
            if run is None:
                raise SystemExit("exact broad-equity discovery run was not found")
            _research_evidence_json({"command": command, "run": run})
            return

        if command == "broad-equity-opportunities":
            run = broad_repo.discovery(arguments.run_id)
            if run is None:
                raise SystemExit("exact broad-equity discovery run was not found")
            limit = max(0, arguments.limit)
            _research_evidence_json(
                {
                    "command": command,
                    "run_id": run.run_id,
                    "opportunities": run.opportunities[:limit],
                    "total_opportunities": len(run.opportunities),
                }
            )
            return

        if command == "broad-equity-shadow-plan-create":
            run = broad_repo.discovery(arguments.run_id)
            if run is None:
                raise SystemExit("exact broad-equity discovery run was not found")
            if (
                run.universe_id != arguments.universe_id
                or run.data_capture_id != arguments.data_capture_id
            ):
                raise SystemExit("supplied universe/data-capture id does not match the exact run")
            plan = CreateBroadEquityShadowPlan(broad_repo).execute(
                discovery_run=run,
                policy_hashes=(run.regime.canonical_hash,),
                top_n=min(5, max(1, len(run.opportunities))),
                decision_at=now,
                forward_horizon_sessions=20,
                data_cutoff=now,
                idempotency_key=arguments.idempotency_key,
            )
            _research_evidence_json({"command": command, "plan": plan})
            return

        if command in {"broad-equity-shadow-plan-status", "broad-equity-shadow-report"}:
            plan = broad_repo.shadow(arguments.plan_id)
            if plan is None:
                raise SystemExit("exact broad-equity shadow plan was not found")
            _research_evidence_json({"command": command, "plan": plan})
            return

        if command in {"broad-equity-shadow-capture", "broad-equity-shadow-resolve"}:
            plan = broad_repo.shadow(arguments.plan_id)
            if plan is None:
                raise SystemExit("exact broad-equity shadow plan was not found")
            _research_evidence_json(
                {
                    "command": command,
                    "plan_id": plan.plan_id,
                    "state": "SHADOW_OUTCOME_CAPTURE_NOT_YET_IMPLEMENTED",
                    "reason": (
                        "Forward XNYS-close outcome evidence is a separate, not-yet-built "
                        "capture; this milestone durably retains only the DRAFT plan."
                    ),
                    "outcome_requirements": plan.outcome_requirements,
                }
            )
            return

        raise SystemExit(f"unsupported broad-equity command {command!r}")
    finally:
        engine.dispose()


def options_intelligence_command(arguments: argparse.Namespace) -> None:
    """Durable, repository-backed options-intelligence command surface.

    Contract-reference evidence only: no quote, Greek, implied-volatility, or
    live-pricing capture is performed by any branch of this command.
    """
    from apex.adapters.options_reference import (
        MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
        NASDAQ_OPTIONS_DIRECTORY_PATH,
        NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL,
        NASDAQ_OPTIONS_HOST,
        alpaca_options_capability,
        occ_public_reference_capability,
    )
    from apex.adapters.options_reference_source import (
        NasdaqOptionsDirectoryCapabilityUnavailable,
        NasdaqOptionsDirectoryClient,
        NasdaqOptionsDirectoryClientError,
    )
    from apex.application.options_intelligence import (
        RecordOptionableUnderlyings,
        RecordOptionContracts,
        RecordOptionProviderCapability,
    )
    from apex.application.research_evidence import CaptureNasdaqOptionsDirectory
    from apex.domain.research_evidence import EvidenceClass

    engine, research_repo, security_repo, _broad_repo, options_repo = _broad_market_runtime()
    try:
        now = datetime.now(tz=UTC)
        command = arguments.command

        if command == "options-intelligence-capability":
            occ = RecordOptionProviderCapability(options_repo).execute(
                capability=occ_public_reference_capability(checked_at=now), recorded_at=now
            )
            alpaca = RecordOptionProviderCapability(options_repo).execute(
                capability=alpaca_options_capability(), recorded_at=now
            )
            _research_evidence_json({"command": command, "occ": occ, "alpaca": alpaca})
            return

        if command == "options-intelligence-reference-capture":
            universe = security_repo.universe(arguments.universe_id)
            if universe is None:
                raise SystemExit("exact broad-equity universe was not found")
            underlying_ids = {
                item.current_symbol: item.canonical_security_id for item in universe.decisions
            }
            options_capture_id = f"broad-equity-options-directory-{now.date().isoformat()}"
            options_locator = f"https://{NASDAQ_OPTIONS_HOST}{NASDAQ_OPTIONS_DIRECTORY_PATH}"
            try:
                raw, parsed = NasdaqOptionsDirectoryClient().capture(
                    received_at=now, underlying_ids=underlying_ids, timeout_seconds=20
                )
            except NasdaqOptionsDirectoryCapabilityUnavailable as error:
                capture = CaptureNasdaqOptionsDirectory(research_repo).record_unavailable(
                    capture_id=options_capture_id,
                    source_locator=options_locator,
                    requested_at=now,
                    recorded_at=now,
                    reason=str(error),
                )
                _research_evidence_json(
                    {
                        "command": command,
                        "universe_id": arguments.universe_id,
                        "capture_id": capture.capture_id,
                        "capture_status": capture.status.value,
                        "capture_bound_bytes": MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
                        "reason": capture.failure_reason,
                        "contracts_retained": 0,
                    }
                )
                return
            except NasdaqOptionsDirectoryClientError as error:
                capture = CaptureNasdaqOptionsDirectory(research_repo).record_failed(
                    capture_id=options_capture_id,
                    source_locator=options_locator,
                    requested_at=now,
                    recorded_at=now,
                    reason=str(error),
                )
                _research_evidence_json(
                    {
                        "command": command,
                        "universe_id": arguments.universe_id,
                        "capture_id": capture.capture_id,
                        "capture_status": capture.status.value,
                        "capture_bound_bytes": MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
                        "reason": capture.failure_reason,
                        "contracts_retained": 0,
                    }
                )
                return
            retained = RecordOptionContracts(options_repo).execute(
                contracts=parsed.contracts, recorded_at=now
            )
            capture = CaptureNasdaqOptionsDirectory(research_repo).start(
                capture_id=options_capture_id,
                evidence_class=EvidenceClass.GENUINE_PUBLIC,
                source_locator=parsed.receipt.source_locator,
                raw_payload=raw,
                rejections=parsed.rejections,
                source_timestamp=parsed.receipt.source_timestamp,
                requested_at=now,
                ingested_at=now,
                chunk_size=1000,
                limitations=(
                    f"SCHEMA_LEVEL:{NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL}",
                    "CONTRACT_LEVEL_RECORDS_RETAINED_IN_OPTIONS_INTELLIGENCE_CONTRACT_TABLE",
                ),
            )
            capture = _drain_options_directory_capture(
                CaptureNasdaqOptionsDirectory(research_repo), capture, raw, parsed.rejections, 1000
            )
            _research_evidence_json(
                {
                    "command": command,
                    "universe_id": arguments.universe_id,
                    "capture_id": capture.capture_id,
                    "capture_status": capture.status.value,
                    "source_locator": capture.source_locator,
                    "options_source_schema_level": NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL,
                    "capture_bound_bytes": MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
                    "actual_payload_bytes": capture.byte_count,
                    "raw_sha256": capture.raw_sha256,
                    "receipt_id": parsed.receipt.receipt_id,
                    "parsed_record_count": parsed.total_rows,
                    "contracts_retained": _distinct_contract_count(retained),
                    "linked_underlying_count": parsed.linked_underlying_count,
                    "unlinked_count": parsed.unlinked_count,
                    "ambiguous_count": parsed.ambiguous_count,
                    "rejection_reason_counts": parsed.rejection_reason_counts,
                }
            )
            return

        if command == "options-intelligence-optionable-underlyings":
            universe = security_repo.universe(arguments.universe_id)
            if universe is None:
                raise SystemExit("exact broad-equity universe was not found")
            decisions = RecordOptionableUnderlyings(options_repo).execute(
                universe_security_ids=tuple(
                    item.canonical_security_id for item in universe.decisions
                ),
                contracts=options_repo.contracts(),
                source_provider="NASDAQ_OPTIONS_DIRECTORY",
                as_of=now.date().isoformat(),
                recorded_at=now,
            )
            _research_evidence_json(
                {
                    "command": command,
                    "universe_id": arguments.universe_id,
                    "considered": len(decisions),
                    "optionable_count": sum(item.optionable for item in decisions),
                }
            )
            return

        if command == "options-intelligence-contracts":
            contracts = options_repo.contracts_for_underlying(arguments.canonical_security_id)
            _research_evidence_json(
                {
                    "command": command,
                    "canonical_security_id": arguments.canonical_security_id,
                    "contracts": contracts,
                }
            )
            return

        raise SystemExit(f"unsupported options-intelligence command {command!r}")
    finally:
        engine.dispose()


def _options_quote_intelligence_runtime() -> tuple[Any, Any, Any]:
    """Engine plus the two durable repositories the genuine options
    quote-chain command family composes: the already-existing stable
    security identity repository (symbol -> stable_security_id resolution,
    reused unmodified) and the new options quote-intelligence repository.

    Runs the identical credential-leak preflight every other provider-
    touching stack builder (``_real_securities_provider_stack``,
    ``_historical_market_data_stack``) already runs -- this family reaches
    a real, credential-gated Alpaca adapter (``AlpacaMarketDataHttpAdapter``,
    via ``AlpacaOptionsChainCaptureClient``/``check_alpaca_options_quote_
    capability``) just as directly as those do, so it must not be the one
    path that skips the check."""
    _assert_no_tracked_provider_credentials()
    from apex.adapters.options_quote_intelligence_postgres import (
        PostgresOptionsQuoteIntelligenceRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.security_identity_postgres import PostgresSecurityIdentityRepository

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        engine,
        PostgresSecurityIdentityRepository(engine),
        PostgresOptionsQuoteIntelligenceRepository(engine),
    )


def options_quote_intelligence_command(arguments: argparse.Namespace) -> None:
    """Bounded, read-mostly command surface for genuine current
    options-chain quote and volatility intelligence (research evidence
    only -- no Order, Strategy, Shared Opportunity, PAPER, or LIVE
    authority is ever created here)."""
    from apex.adapters.alpaca_options_quote_source import (
        AlpacaOptionsChainCaptureClient,
        check_alpaca_options_quote_capability,
        observation_from_alpaca_entry,
    )
    from apex.application.options_quote_intelligence import (
        CreateOptionChainCapturePlan,
        CreateOptionsChainSnapshot,
        CreateOptionsChainSnapshotCommand,
        RecordOptionChainCaptureRun,
        RecordOptionsQuoteProviderCapability,
        ResolveRealizedVolatility,
    )
    from apex.domain.options_intelligence import OptionType
    from apex.domain.options_quote_intelligence import (
        DEFAULT_OPTIONS_COHORT_SYMBOLS,
        IV_RANK_MINIMUM_HISTORY,
        STALENESS_THRESHOLD_SECONDS,
        CaptureRunStatus,
        IvRankStatus,
        OptionChainCaptureBounds,
        OptionChainCapturePlan,
        OptionChainCaptureRun,
        build_assumption_sensitivity_row,
        build_cohort_quote_quality_report,
        build_cross_sectional_summary,
        build_greeks_comparison,
        build_iv_comparison_rows,
        compare_term_structures,
        derive_options_chain_snapshot_id,
        segment_iv_comparison,
        validate_apex_greeks,
    )
    from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1, resolve_cohort

    engine, identity_repo, quote_repo = _options_quote_intelligence_runtime()
    try:
        now = datetime.now(tz=UTC)
        command = arguments.command

        if command == "options-quote-capability-inspect":
            result = check_alpaca_options_quote_capability()
            recorded = RecordOptionsQuoteProviderCapability(quote_repo).execute(capability=result)
            _research_evidence_json(
                {
                    "command": command,
                    "provider": recorded.capability.provider,
                    "credentials_available": recorded.credentials_available,
                    "requests_made": recorded.requests_made,
                    "unavailable_reason": recorded.unavailable_reason,
                    "capability": recorded.capability,
                    "checked_at": recorded.checked_at,
                }
            )
            return

        if command == "options-chain-plan-create":
            identities = identity_repo.latest_stable_identities()
            resolved = resolve_cohort(
                GENUINE_MULTI_ASSET_COHORT_POLICY_V1, stable_identities=identities
            )
            by_symbol = {member.symbol: member for member in resolved.members}
            selected = tuple(
                sorted(
                    (
                        by_symbol[symbol]
                        for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS
                        if symbol in by_symbol
                    ),
                    key=lambda item: item.symbol,
                )
            )
            unresolved = tuple(
                sorted(
                    symbol for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS if symbol not in by_symbol
                )
            )
            bounds = OptionChainCaptureBounds.create()
            plan = OptionChainCapturePlan.create(
                cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
                cohort_policy_hash=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.canonical_hash,
                members=selected,
                unresolved_symbols=unresolved,
                bounds=bounds,
                created_at=now,
            )
            stored = CreateOptionChainCapturePlan(quote_repo).execute(plan=plan)
            _research_evidence_json(
                {
                    "command": command,
                    "plan_id": stored.plan_id,
                    "cohort_policy_id": stored.cohort_policy_id,
                    "declared_symbols": DEFAULT_OPTIONS_COHORT_SYMBOLS,
                    "resolved_member_count": len(stored.members),
                    "resolved_symbols": tuple(item.symbol for item in stored.members),
                    "unresolved_symbols": stored.unresolved_symbols,
                    "bounds": stored.bounds,
                    "provider": stored.provider,
                    "feed": stored.feed,
                    "created_at": stored.created_at,
                }
            )
            return

        if command == "options-chain-plan-inspect":
            plan_id = getattr(arguments, "plan_id", None)
            plan = quote_repo.plan_by_id(plan_id) if plan_id else quote_repo.latest_plan()
            if plan is None:
                raise SystemExit("exact options chain capture plan was not found")
            _research_evidence_json({"command": command, "plan": plan})
            return

        if command == "options-chain-capture":
            plan_id = getattr(arguments, "plan_id", None)
            plan = quote_repo.plan_by_id(plan_id) if plan_id else quote_repo.latest_plan()
            if plan is None:
                raise SystemExit("exact options chain capture plan was not found")
            max_underlyings = getattr(arguments, "max_underlyings", None) or len(plan.members)
            members = plan.members[: max(0, max_underlyings)]
            client = AlpacaOptionsChainCaptureClient()
            requested_at = now
            run_id = OptionChainCaptureRun.derive_run_id(
                plan_id=plan.plan_id, requested_at=requested_at
            )
            if not client.credentials_available:
                run = OptionChainCaptureRun.create(
                    plan_id=plan.plan_id,
                    requested_at=requested_at,
                    completed_at=datetime.now(tz=UTC),
                    status=CaptureRunStatus.BLOCKED_CREDENTIALS_ABSENT,
                    underlyings_requested=len(members),
                    underlyings_captured=0,
                    provider_request_count=0,
                    payload_bytes=0,
                    contracts_considered=0,
                    contracts_quoted=0,
                    snapshot_ids=(),
                    reason="ALPACA_CREDENTIALS_ABSENT",
                )
                assert run.run_id == run_id
                recorded_run = RecordOptionChainCaptureRun(quote_repo).execute(run=run)
                _research_evidence_json(
                    {
                        "command": command,
                        "plan_id": plan.plan_id,
                        "run_id": recorded_run.run_id,
                        "status": recorded_run.status,
                        "underlyings_requested": recorded_run.underlyings_requested,
                        "underlyings_captured": 0,
                        "provider_request_count": 0,
                        "payload_bytes": 0,
                        "reason": recorded_run.reason,
                        "per_underlying": (),
                    }
                )
                return

            from apex.adapters.historical_market_data_postgres import (
                PostgresHistoricalMarketDataRepository,
            )
            from apex.adapters.securities_postgres import PostgresSecuritiesRepository

            rv_resolver = ResolveRealizedVolatility(
                historical_repository=PostgresHistoricalMarketDataRepository(engine),
                evidence=PostgresSecuritiesRepository(engine),
                cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            )
            snapshot_service = CreateOptionsChainSnapshot(quote_repo)
            total_requests = 0
            total_bytes = 0
            total_considered = 0
            total_quoted = 0
            captured_count = 0
            per_underlying: list[dict[str, object]] = []
            # Two passes, deliberately: ``options_chain_snapshot.run_id`` is a
            # foreign key into ``options_chain_capture_run``, so the run row
            # must exist *before* any snapshot referencing it is persisted --
            # but the run row's own content (``snapshot_ids``, counts, final
            # status) is only known once every underlying has been captured.
            # Pass 1 makes every provider request and computes each snapshot's
            # id cheaply via ``derive_options_chain_snapshot_id`` (evidence-
            # only, no IV/Greeks/surface work) without persisting anything;
            # the run row is then saved once with accurate content; pass 2
            # persists each snapshot via the existing idempotent service
            # (which independently recomputes and asserts the identical id).
            as_of = now.date()
            pending: list[tuple[dict[str, object], CreateOptionsChainSnapshotCommand, str]] = []
            for member in members:
                outcome = client.capture_underlying(
                    member=member, bounds=plan.bounds, feed=plan.feed, captured_at=now
                )
                total_requests += outcome.request_count
                total_bytes += outcome.bytes_received
                entry_summary: dict[str, object] = {
                    "symbol": outcome.symbol,
                    "status": outcome.status,
                    "reason": outcome.reason,
                    "contracts_returned": len(outcome.contract_entries),
                    "requests": outcome.request_count,
                    "bytes": outcome.bytes_received,
                }
                if outcome.status == "CAPTURED" and outcome.underlying_reference_price is not None:
                    receipt_id = (
                        outcome.source_receipt_ids[-1] if outcome.source_receipt_ids else "unknown"
                    )
                    observations = []
                    for entry in outcome.contract_entries:
                        built = observation_from_alpaca_entry(
                            entry=entry,
                            underlying_canonical_security_id=member.stable_security_id,
                            underlying_reference_price=outcome.underlying_reference_price,
                            captured_at=now,
                            receipt_id=receipt_id,
                        )
                        if built is not None:
                            observations.append(built[1])
                    total_considered += len(observations)
                    total_quoted += sum(
                        1 for item in observations if item.status.value == "OBSERVED"
                    )
                    realized_volatility = rv_resolver.resolve(
                        stable_security_id=member.stable_security_id, as_of=now
                    )
                    entry_summary["realized_volatility_20_annualized"] = (
                        realized_volatility.realized_volatility_primary
                    )
                    entry_summary["realized_volatility_60_annualized"] = (
                        realized_volatility.realized_volatility_secondary
                    )
                    entry_summary["realized_volatility_status"] = realized_volatility.status
                    entry_summary["realized_volatility_dataset_snapshot_id"] = (
                        realized_volatility.dataset_snapshot_id
                    )
                    snapshot_command = CreateOptionsChainSnapshotCommand(
                        plan=plan,
                        run_id=run_id,
                        member=member,
                        underlying_reference_price=outcome.underlying_reference_price,
                        underlying_price_source=outcome.underlying_price_source or "UNKNOWN",
                        captured_at=now,
                        observations=tuple(observations),
                        as_of=as_of,
                        provider="ALPACA_OPTIONS_MARKET_DATA",
                        source_receipt_ids=outcome.source_receipt_ids,
                        realized_volatility_annualized=(
                            realized_volatility.realized_volatility_primary
                        ),
                        realized_volatility_status=realized_volatility.status,
                    )
                    predicted_snapshot_id = derive_options_chain_snapshot_id(
                        underlying_canonical_security_id=member.stable_security_id,
                        captured_at=now,
                        observations=tuple(observations),
                    )
                    captured_count += 1
                    pending.append((entry_summary, snapshot_command, predicted_snapshot_id))
                per_underlying.append(entry_summary)
            completed_at = datetime.now(tz=UTC)
            if captured_count == 0:
                run_status = CaptureRunStatus.EMPTY
            elif captured_count == len(members):
                run_status = CaptureRunStatus.COMPLETE
            else:
                run_status = CaptureRunStatus.PARTIAL
            run = OptionChainCaptureRun.create(
                plan_id=plan.plan_id,
                requested_at=requested_at,
                completed_at=completed_at,
                status=run_status,
                underlyings_requested=len(members),
                underlyings_captured=captured_count,
                provider_request_count=total_requests,
                payload_bytes=total_bytes,
                contracts_considered=total_considered,
                contracts_quoted=total_quoted,
                snapshot_ids=tuple(item[2] for item in pending),
                reason=None if captured_count else "NO_UNDERLYING_PRODUCED_QUOTABLE_EVIDENCE",
            )
            assert run.run_id == run_id
            recorded_run = RecordOptionChainCaptureRun(quote_repo).execute(run=run)
            for entry_summary, snapshot_command, predicted_snapshot_id in pending:
                snapshot, features = snapshot_service.execute(snapshot_command)
                assert snapshot.snapshot_id == predicted_snapshot_id
                entry_summary["snapshot_id"] = snapshot.snapshot_id
                entry_summary["data_quality_status"] = snapshot.data_quality_status.value
                entry_summary["atm_iv"] = features.atm_iv_nearest_expiration
            _research_evidence_json(
                {
                    "command": command,
                    "plan_id": plan.plan_id,
                    "run_id": recorded_run.run_id,
                    "status": recorded_run.status,
                    "underlyings_requested": recorded_run.underlyings_requested,
                    "underlyings_captured": recorded_run.underlyings_captured,
                    "provider_request_count": recorded_run.provider_request_count,
                    "payload_bytes": recorded_run.payload_bytes,
                    "contracts_considered": recorded_run.contracts_considered,
                    "contracts_quoted": recorded_run.contracts_quoted,
                    "runtime_seconds": recorded_run.runtime_seconds,
                    "snapshot_ids": recorded_run.snapshot_ids,
                    "per_underlying": tuple(per_underlying),
                }
            )
            return

        if command == "options-chain-snapshot-inspect":
            snapshot = quote_repo.snapshot_by_id(arguments.snapshot_id)
            if snapshot is None:
                raise SystemExit("exact options chain snapshot was not found")
            _research_evidence_json({"command": command, "snapshot": snapshot})
            return

        if command == "options-volatility-summary":
            underlying_id = arguments.underlying_canonical_security_id
            snapshot = quote_repo.latest_snapshot_for_underlying(underlying_id)
            if snapshot is None:
                raise SystemExit("no retained options chain snapshot for this underlying")
            features = quote_repo.research_features_by_snapshot(snapshot.snapshot_id)
            history = quote_repo.snapshots_for_underlying(underlying_id)
            term_structure_change = None
            if len(history) >= 2:
                term_structure_change = compare_term_structures(
                    underlying_canonical_security_id=underlying_id,
                    previous=history[-2].volatility_surface,
                    current=snapshot.volatility_surface,
                )
            _research_evidence_json(
                {
                    "command": command,
                    "underlying_canonical_security_id": underlying_id,
                    "underlying_symbol": snapshot.underlying_symbol,
                    "snapshot_id": snapshot.snapshot_id,
                    "captured_at": snapshot.captured_at,
                    "retained_snapshot_count": len(history),
                    "atm_points": snapshot.volatility_surface.atm_points,
                    "term_structure": snapshot.volatility_surface.term_structure,
                    "term_structure_change": term_structure_change,
                    "skew_by_expiration": snapshot.volatility_surface.skew_by_expiration,
                    "iv_rank": snapshot.volatility_surface.iv_rank,
                    "expected_moves": snapshot.expected_moves,
                    "iv_vs_realized": snapshot.iv_vs_realized,
                    "research_features": features,
                    "historical_options_pricing_note": snapshot.historical_options_pricing_note,
                    "authority": snapshot.authority,
                }
            )
            return

        if command == "options-chain-quality-inspect":
            snapshot = quote_repo.snapshot_by_id(arguments.snapshot_id)
            if snapshot is None:
                raise SystemExit("exact options chain snapshot was not found")
            _research_evidence_json(
                {
                    "command": command,
                    "snapshot_id": snapshot.snapshot_id,
                    "data_quality_status": snapshot.data_quality_status,
                    "data_quality_notes": snapshot.data_quality_notes,
                    "contracts_considered": snapshot.contracts_considered,
                    "contracts_quoted": snapshot.contracts_quoted,
                    "contracts_excluded": snapshot.contracts_excluded,
                    "exclusion_reason_counts": snapshot.exclusion_reason_counts,
                    "liquidity_profile": snapshot.liquidity_profile,
                    "arbitrage_flags": snapshot.arbitrage_flags,
                }
            )
            return

        if command == "options-chain-coverage-inspect":
            from apex.adapters.options_research_operations_postgres import (
                PostgresOptionsResearchOperationsRepository,
            )

            plan_id = getattr(arguments, "plan_id", None)
            plan = quote_repo.plan_by_id(plan_id) if plan_id else quote_repo.latest_plan()
            if plan is None:
                raise SystemExit("exact options chain capture plan was not found")
            checkpoint_repo = PostgresOptionsResearchOperationsRepository(engine)
            session_id = getattr(arguments, "session_id", None)
            checkpoint = (
                checkpoint_repo.latest_checkpoint_for_session(
                    plan_id=plan.plan_id, session_id=session_id
                )
                if session_id
                else checkpoint_repo.latest_checkpoint(plan.plan_id)
            )
            if checkpoint is None:
                raise SystemExit(
                    "no retained options forward-evidence checkpoint for this plan/session -- "
                    "run options-research-run-once during a genuine capture window first"
                )
            _research_evidence_json(
                {
                    "command": command,
                    "plan_id": plan.plan_id,
                    "session_id": checkpoint.session_id,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "underlyings_requested_count": checkpoint.underlyings_requested_count,
                    "underlyings_captured_count": checkpoint.underlyings_captured_count,
                    "chain_coverage_quality_counts": checkpoint.chain_coverage_quality_counts,
                    "cap_constrained_underlyings": checkpoint.cap_constrained_underlyings,
                    "note": (
                        "Per-underlying stratified-selection detail (exact expiration/DTE/"
                        "moneyness/call-put breadth) is reviewable in options-research-run-once's "
                        "own output at capture time; this retained checkpoint carries the "
                        "cohort-wide coverage-quality distribution, not a re-derivable per-"
                        "contract selection replay."
                    ),
                }
            )
            return

        if command == "options-quote-validation-report":
            plan_id = getattr(arguments, "plan_id", None)
            plan = quote_repo.plan_by_id(plan_id) if plan_id else quote_repo.latest_plan()
            if plan is None:
                raise SystemExit("exact options chain capture plan was not found")
            members_data: list[tuple[str, Any, tuple[Any, ...]]] = []
            for member in plan.members:
                snapshot = quote_repo.latest_snapshot_for_underlying(member.stable_security_id)
                if snapshot is None:
                    continue
                member_observations = quote_repo.observations_by_ids(snapshot.observation_ids)
                members_data.append((member.symbol, snapshot, member_observations))
            if not members_data:
                raise SystemExit("no retained snapshot exists yet for this plan's cohort")
            as_of = now.date()

            quality_report = build_cohort_quote_quality_report(
                generated_at=now, members=tuple(members_data)
            )
            iv_rows = tuple(
                row
                for symbol, snapshot, member_observations in members_data
                for row in build_iv_comparison_rows(
                    underlying_symbol=symbol, snapshot=snapshot, observations=member_observations
                )
            )
            iv_comparison_segments = segment_iv_comparison(iv_rows)
            greeks_comparisons = tuple(
                build_greeks_comparison(
                    underlying_symbol=symbol, snapshot=snapshot, observations=member_observations
                )
                for symbol, snapshot, member_observations in members_data
            )
            greeks_validations = tuple(
                validate_apex_greeks(
                    underlying_symbol=symbol, snapshot=snapshot, observations=member_observations
                )
                for symbol, snapshot, member_observations in members_data
            )
            cross_sectional = build_cross_sectional_summary(
                generated_at=now,
                snapshots=tuple(snapshot for _symbol, snapshot, _observations in members_data),
            )

            freshness_rows = []
            for symbol, snapshot, member_observations in members_data:
                if not member_observations:
                    continue
                staleness = [item.staleness_seconds for item in member_observations]
                freshness_rows.append(
                    {
                        "underlying_symbol": symbol,
                        "snapshot_captured_at": snapshot.captured_at,
                        "earliest_quote_timestamp": min(
                            item.quote_timestamp for item in member_observations
                        ),
                        "latest_quote_timestamp": max(
                            item.quote_timestamp for item in member_observations
                        ),
                        "ingestion_timestamp": member_observations[0].ingestion_timestamp,
                        "min_staleness_seconds": min(staleness),
                        "max_staleness_seconds": max(staleness),
                        "mean_staleness_seconds": sum(staleness) // len(staleness),
                        "staleness_threshold_seconds": STALENESS_THRESHOLD_SECONDS,
                        "all_within_freshness_threshold": all(
                            item <= STALENESS_THRESHOLD_SECONDS for item in staleness
                        ),
                    }
                )

            representative_symbols = ("SPY", "QQQ", "AAPL")
            sensitivity_rows = []
            for symbol, _snapshot, member_observations in members_data:
                if symbol not in representative_symbols or not member_observations:
                    continue
                nearest_expiration = min(item.contract.expiration for item in member_observations)
                candidates = [
                    item
                    for item in member_observations
                    if item.contract.expiration == nearest_expiration and item.midpoint is not None
                ]
                for option_type in (OptionType.CALL, OptionType.PUT):
                    pool = [item for item in candidates if item.contract.option_type is option_type]
                    if not pool:
                        continue
                    atm = min(
                        pool,
                        key=lambda item: abs(
                            item.underlying_reference_price - item.contract.strike
                        ),
                    )
                    row = build_assumption_sensitivity_row(
                        underlying_symbol=symbol, observation=atm, as_of=as_of
                    )
                    if row is not None:
                        sensitivity_rows.append(row)

            runs = quote_repo.runs_for_plan(plan.plan_id)
            latest_run = runs[-1] if runs else None
            iv_history_states = {
                symbol: snapshot.volatility_surface.iv_rank.status
                for symbol, snapshot, _observations in members_data
            }
            feature_ids = {}
            for symbol, snapshot, _observations in members_data:
                features = quote_repo.research_features_by_snapshot(snapshot.snapshot_id)
                feature_ids[symbol] = features.feature_id if features is not None else None
            baseline = {
                "run_id": latest_run.run_id if latest_run is not None else None,
                "capture_requested_at": latest_run.requested_at if latest_run is not None else None,
                "capture_completed_at": latest_run.completed_at if latest_run is not None else None,
                "underlyings": tuple(symbol for symbol, _s, _o in members_data),
                "snapshot_ids": tuple(snapshot.snapshot_id for _s, snapshot, _o in members_data),
                "quote_counts": {
                    symbol: len(member_observations)
                    for symbol, _snapshot, member_observations in members_data
                },
                "feature_ids": feature_ids,
                "quality_state_by_underlying": {
                    symbol: snapshot.data_quality_status.value
                    for symbol, snapshot, _o in members_data
                },
                "iv_rank_minimum_history_snapshots_required": IV_RANK_MINIMUM_HISTORY,
                "underlyings_with_iv_history_insufficient": tuple(
                    sorted(
                        symbol
                        for symbol, status in iv_history_states.items()
                        if status is IvRankStatus.INSUFFICIENT_HISTORY
                    )
                ),
                "underlyings_with_iv_rank_computed": tuple(
                    sorted(
                        symbol
                        for symbol, status in iv_history_states.items()
                        if status is IvRankStatus.COMPUTED
                    )
                ),
            }

            limitations_report = (
                "FEED_LEVEL_INDICATIVE_NOT_OPRA_CONSOLIDATED: this environment's Alpaca "
                "entitlement was probed and captured only against the zero-cost indicative "
                "feed; separate OPRA consolidated-feed entitlement was not confirmed.",
                "OPEN_INTEREST_UNAVAILABLE: the option_chain_snapshots endpoint schema never "
                "returns open interest on any probed or captured contract (0 of every "
                "genuinely captured observation).",
                "ZERO_DIVIDEND_ASSUMPTION: Apex IV/Greeks use DIVIDEND_YIELD_ASSUMED_ZERO "
                "(source ZERO_ASSUMPTION_UNCONFIRMED_V1) for every underlying; genuine "
                "retained corporate-action dividend evidence "
                "(securities_corporate_action_evidence) was directly confirmed present for "
                "only a subset of the cohort (DIA, IWM, QQQ, SPY, XLE, XLF, XLI, XLK, XLP, "
                "XLV) and absent for the rest (AAPL, AMZN, MSFT, NVDA, XLB, XLC, XLRE, XLU, "
                "XLY) as of this report -- absence of retained evidence is a coverage gap, "
                "not a claim those underlyings pay no dividend.",
                "FIXED_RISK_FREE_RATE_ASSUMPTION: Apex IV/Greeks use the versioned fixed "
                "RISK_FREE_RATE_DEFAULT (5%, FIXED_FLAT_ASSUMPTION_V1); the only retained "
                "rate-adjacent context (total_market_context TREASURY_RATE) is an explicit "
                "TLT *price-direction* proxy, documented as not a yield, and was judged not "
                "semantically valid to substitute as a contemporaneous rate input.",
                "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE: no genuine historical option quote "
                "evidence exists yet; every IV/Greeks/surface figure here is a current-only "
                "estimate.",
                "IV_RANK_SINGLE_SNAPSHOT: every underlying's IV rank is IV_HISTORY_INSUFFICIENT "
                "after this first genuine capture (minimum retained-snapshot count not met); "
                "no IV rank/percentile is fabricated from one observation.",
                "WEEKEND_OR_AFTER_HOURS_CAPTURE_STALENESS: when captured outside a live "
                "session, every quote is correctly classified STALE against the declared "
                "900-second freshness threshold -- see the freshness section, not a defect.",
                "SURVIVORSHIP_AND_POINT_IN_TIME: the options cohort is today's current "
                "membership only, not a verified historical universe; retained equity bars "
                "underlying the realized-volatility comparison carry the same "
                "SURVIVORSHIP_LIMITED/POINT_IN_TIME_UNIVERSE_INCOMPLETE caveats as the "
                "existing historical-data track.",
                "DTE_MATCHED_RV_NOT_IMPLEMENTED: only 20- and 60-session realized-volatility "
                "horizons are computed; a horizon matched to each individual contract's own "
                "DTE is not implemented this milestone (see IV-vs-RV methodology).",
                "DENSE_CHAIN_CONTRACT_CAP_CONCENTRATES_NEAR_TERM: genuine capture evidence "
                "confirmed the per-underlying max_contracts_per_underlying=200 bound, "
                "combined with provider response ordering, is consumed entirely by the "
                "nearest few expirations for the densest chains (e.g. SPY's 200 retained "
                "contracts covered only 4 expirations spanning 3-6 DTE out of the declared "
                "0-60 DTE / 6-expiration policy); thinner chains (e.g. XLRE at 68 contracts) "
                "are not similarly truncated. Term-structure/skew evidence for the densest "
                "names is genuine but currently near-dated-concentrated, not representative "
                "of the full declared window -- a pagination/selection refinement (e.g. a "
                "per-expiration sub-cap), not a bound increase, would be needed to broaden "
                "coverage.",
            )

            _research_evidence_json(
                {
                    "command": command,
                    "plan_id": plan.plan_id,
                    "generated_at": now,
                    "quote_quality": {
                        "underlyings_included": quality_report.underlyings_included,
                        "snapshot_ids": quality_report.snapshot_ids,
                        "overall": quality_report.overall,
                        "by_underlying": quality_report.by_underlying,
                        "by_expiration_bucket": quality_report.by_expiration_bucket,
                        "by_call_put": quality_report.by_call_put,
                    },
                    "freshness": tuple(freshness_rows),
                    "provider_vs_apex_iv": iv_comparison_segments,
                    "provider_vs_apex_greeks": greeks_comparisons,
                    "apex_greeks_validation": greeks_validations,
                    "cross_sectional": cross_sectional,
                    "assumption_sensitivity": tuple(sensitivity_rows),
                    "forward_history_baseline": baseline,
                    "limitations_report": limitations_report,
                }
            )
            return

        raise SystemExit(f"unsupported options-quote-intelligence command {command!r}")
    finally:
        engine.dispose()


def _options_structures_runtime() -> tuple[Any, Any, Any]:
    """Engine plus the already-existing options quote-chain repository
    (read-only reuse of its retained snapshots/observations -- never a
    fresh provider capture) and the new structure-economics repository.
    Touches no provider credential path at all, so this family runs no
    credential-leak preflight (unlike ``_options_quote_intelligence_
    runtime``, which reaches a real Alpaca adapter)."""
    from apex.adapters.options_quote_intelligence_postgres import (
        PostgresOptionsQuoteIntelligenceRepository,
    )
    from apex.adapters.options_structures_postgres import PostgresOptionsStructuresRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        engine,
        PostgresOptionsQuoteIntelligenceRepository(engine),
        PostgresOptionsStructuresRepository(engine),
    )


def options_structures_command(arguments: argparse.Namespace) -> None:
    """Bounded command surface for defined-risk options structure
    economics (research evidence only -- no options order, PAPER order, or
    Shared Opportunity is ever created here; ``authority`` stays ``NONE``
    throughout)."""
    from apex.application.options_structures import (
        BuildStructuresAcrossCohort,
        BuildStructuresAcrossCohortCommand,
        BuildStructuresForUnderlying,
        BuildStructuresForUnderlyingCommand,
        CreateOptionsFeePolicy,
        CreateOptionsSlippagePolicy,
        CreateOptionsStructureResearchPolicy,
        CreateStructurePricingPolicy,
    )
    from apex.domain.options_quote_intelligence import QuoteObservationStatus
    from apex.domain.options_structures import (
        OptionsFeePolicy,
        OptionsSlippagePolicy,
        OptionsStructureResearchPolicy,
        StructurePricingPolicy,
        compare_structure_expressions,
    )

    engine, quote_repo, structures_repo = _options_structures_runtime()
    try:
        now = datetime.now(tz=UTC)
        command = arguments.command

        if command == "options-structure-policy-create":
            pricing_policy = CreateStructurePricingPolicy(structures_repo).execute(
                policy=StructurePricingPolicy.create()
            )
            fee_policy = CreateOptionsFeePolicy(structures_repo).execute(
                policy=OptionsFeePolicy.create()
            )
            slippage_policy = CreateOptionsSlippagePolicy(structures_repo).execute(
                policy=OptionsSlippagePolicy.create()
            )
            overrides: dict[str, Any] = {}
            for flag, field in (
                ("dte_minimum", "dte_minimum"),
                ("dte_maximum", "dte_maximum"),
                ("moneyness_band", "moneyness_band"),
                ("maximum_structures_per_underlying", "maximum_structures_per_underlying"),
                ("maximum_total_structures", "maximum_total_structures"),
            ):
                value = getattr(arguments, flag, None)
                if value is not None:
                    overrides[field] = value
            if getattr(arguments, "allow_stale_quotes_for_research", False):
                overrides["version"] = "STALE_EVIDENCE_ACCEPTANCE_RESEARCH_POLICY_V1"
                overrides["allow_stale_quotes_for_research"] = True
                overrides["minimum_quote_quality"] = (
                    QuoteObservationStatus.OBSERVED,
                    QuoteObservationStatus.ZERO_LIQUIDITY,
                    QuoteObservationStatus.STALE,
                )
            structure_policy = OptionsStructureResearchPolicy.create(
                pricing_policy=pricing_policy,
                fee_policy=fee_policy,
                slippage_policy=slippage_policy,
                created_at=now,
                **overrides,
            )
            stored = CreateOptionsStructureResearchPolicy(structures_repo).execute(
                policy=structure_policy
            )
            _research_evidence_json(
                {
                    "command": command,
                    "policy": stored,
                    "pricing_policy": pricing_policy,
                    "fee_policy": fee_policy,
                    "slippage_policy": slippage_policy,
                }
            )
            return

        if command == "options-structure-policy-inspect":
            policy_id = getattr(arguments, "policy_id", None)
            policy = (
                structures_repo.structure_policy_by_id(policy_id)
                if policy_id
                else structures_repo.latest_structure_policy()
            )
            if policy is None:
                raise SystemExit("exact options structure research policy was not found")
            _research_evidence_json(
                {
                    "command": command,
                    "policy": policy,
                    "pricing_policy": structures_repo.pricing_policy_by_id(
                        policy.pricing_policy_id
                    ),
                    "fee_policy": structures_repo.fee_policy_by_id(policy.fee_policy_id),
                    "slippage_policy": structures_repo.slippage_policy_by_id(
                        policy.slippage_policy_id
                    ),
                }
            )
            return

        if command == "options-structures-build":
            policy_id = getattr(arguments, "policy_id", None)
            structure_policy = (
                structures_repo.structure_policy_by_id(policy_id)
                if policy_id
                else structures_repo.latest_structure_policy()
            )
            if structure_policy is None:
                raise SystemExit(
                    "exact options structure research policy was not found -- "
                    "run options-structure-policy-create first"
                )
            fee_policy = structures_repo.fee_policy_by_id(structure_policy.fee_policy_id)
            slippage_policy = structures_repo.slippage_policy_by_id(
                structure_policy.slippage_policy_id
            )
            if fee_policy is None or slippage_policy is None:
                raise SystemExit(
                    "structure policy references a fee/slippage policy that is missing"
                )
            as_of = now.date()
            quantity = getattr(arguments, "quantity", None) or 1
            underlying_id = getattr(arguments, "underlying_canonical_security_id", None)

            if underlying_id:
                snapshot_id = getattr(arguments, "snapshot_id", None)
                snapshot = (
                    quote_repo.snapshot_by_id(snapshot_id)
                    if snapshot_id
                    else quote_repo.latest_snapshot_for_underlying(underlying_id)
                )
                if snapshot is None:
                    raise SystemExit("no retained options chain snapshot for this underlying")
                observations = quote_repo.observations_by_ids(snapshot.observation_ids)
                single_outcome = BuildStructuresForUnderlying(structures_repo).execute(
                    BuildStructuresForUnderlyingCommand(
                        structure_policy=structure_policy,
                        fee_policy=fee_policy,
                        slippage_policy=slippage_policy,
                        snapshot=snapshot,
                        observations=observations,
                        as_of=as_of,
                        evaluated_at=now,
                        quantity=quantity,
                    )
                )
                _research_evidence_json(
                    {
                        "command": command,
                        "mode": "SINGLE_UNDERLYING",
                        "underlying_canonical_security_id": underlying_id,
                        "source_chain_snapshot_id": snapshot.snapshot_id,
                        "structure_policy_id": structure_policy.policy_id,
                        "research_snapshot_id": (
                            single_outcome.research_snapshot.research_snapshot_id
                        ),
                        "generation_report": single_outcome.research_snapshot.generation_report,
                        "structures_retained": len(single_outcome.structures),
                        "structure_ids": tuple(
                            item.structure_id for item in single_outcome.structures
                        ),
                    }
                )
                return

            plan = quote_repo.latest_plan()
            if plan is None:
                raise SystemExit("no retained options chain capture plan -- nothing to build from")
            members: list[tuple[Any, tuple[Any, ...]]] = []
            skipped_no_snapshot: list[str] = []
            for member in plan.members:
                snapshot = quote_repo.latest_snapshot_for_underlying(member.stable_security_id)
                if snapshot is None:
                    skipped_no_snapshot.append(member.symbol)
                    continue
                observations = quote_repo.observations_by_ids(snapshot.observation_ids)
                members.append((snapshot, observations))
            cohort_outcome = BuildStructuresAcrossCohort(structures_repo).execute(
                BuildStructuresAcrossCohortCommand(
                    structure_policy=structure_policy,
                    fee_policy=fee_policy,
                    slippage_policy=slippage_policy,
                    members=tuple(members),
                    as_of=as_of,
                    evaluated_at=now,
                    quantity=quantity,
                )
            )
            _research_evidence_json(
                {
                    "command": command,
                    "mode": "COHORT",
                    "plan_id": plan.plan_id,
                    "structure_policy_id": structure_policy.policy_id,
                    "underlyings_with_snapshot": len(members),
                    "underlyings_skipped_no_snapshot": tuple(sorted(skipped_no_snapshot)),
                    "underlyings_processed": cohort_outcome.underlyings_processed,
                    "underlyings_skipped_by_total_cap": (
                        cohort_outcome.underlyings_skipped_by_total_cap
                    ),
                    "total_structures_cap_reached": cohort_outcome.total_structures_cap_reached,
                    "research_snapshot_ids": tuple(
                        item.research_snapshot_id for item in cohort_outcome.research_snapshots
                    ),
                    "structures_retained": len(cohort_outcome.structures),
                }
            )
            return

        if command == "options-structures-snapshot-inspect":
            research_snapshot_id = getattr(arguments, "research_snapshot_id", None)
            if research_snapshot_id:
                snapshot = structures_repo.research_snapshot_by_id(research_snapshot_id)
            elif getattr(arguments, "underlying_canonical_security_id", None):
                snapshot = structures_repo.latest_research_snapshot_for_underlying(
                    arguments.underlying_canonical_security_id
                )
            else:
                raise SystemExit(
                    "either --research-snapshot-id or "
                    "--underlying-canonical-security-id is required"
                )
            if snapshot is None:
                raise SystemExit("exact structure research snapshot was not found")
            structures = structures_repo.structures_by_ids(snapshot.retained_structure_ids)
            _research_evidence_json(
                {
                    "command": command,
                    "research_snapshot": snapshot,
                    "retained_structures_summary": tuple(
                        {
                            "structure_id": item.structure_id,
                            "structure_type": item.structure_type,
                            "long_strike": item.long_leg.strike,
                            "short_strike": item.short_leg.strike,
                            "expiration": item.expiration,
                            "maximum_loss": item.economics.maximum_loss,
                            "maximum_profit": item.economics.maximum_profit,
                            "reward_risk_ratio": item.economics.reward_risk_ratio,
                            "quality_label": item.quality.label,
                        }
                        for item in structures
                    ),
                }
            )
            return

        if command == "options-structure-inspect":
            structure = structures_repo.structure_by_id(arguments.structure_id)
            if structure is None:
                raise SystemExit("exact structure was not found")
            features = structures_repo.research_features_by_structure(structure.structure_id)
            _research_evidence_json(
                {"command": command, "structure": structure, "research_features": features}
            )
            return

        if command == "options-structure-scenarios-inspect":
            structure = structures_repo.structure_by_id(arguments.structure_id)
            if structure is None:
                raise SystemExit("exact structure was not found")
            _research_evidence_json(
                {
                    "command": command,
                    "structure_id": structure.structure_id,
                    "underlying_reference_price": structure.underlying_reference_price,
                    "payoff_at_strikes": structure.payoff_at_strikes,
                    "scenarios": structure.scenarios,
                    "terminal_payoff_model_note": structure.terminal_payoff_model_note,
                    "expected_value_state": structure.expected_value_state,
                }
            )
            return

        if command == "options-structure-comparison":
            structure_ids = tuple(arguments.structure_id)
            structures = structures_repo.structures_by_ids(structure_ids)
            if len(structures) != len(structure_ids):
                raise SystemExit("one or more exact structures were not found")
            comparison = compare_structure_expressions(structures)
            _research_evidence_json({"command": command, "comparison": comparison})
            return

        raise SystemExit(f"unsupported options-structure command {command!r}")
    finally:
        engine.dispose()


def _options_research_operations_runtime() -> tuple[Any, Any, Any, Any]:
    """Engine plus the already-existing options quote-chain and structures
    repositories (read-only reuse) and the new forward-evidence-checkpoint
    repository. Touches no provider credential path itself -- only
    ``options-research-run-once`` (which reaches a real Alpaca adapter via
    ``AlpacaOptionsResearchEvidenceCapture``) runs the credential-leak
    preflight, exactly mirroring ``_options_quote_intelligence_runtime``'s
    own distinction from ``_options_structures_runtime``."""
    from apex.adapters.options_quote_intelligence_postgres import (
        PostgresOptionsQuoteIntelligenceRepository,
    )
    from apex.adapters.options_research_operations_postgres import (
        PostgresOptionsResearchOperationsRepository,
    )
    from apex.adapters.options_structures_postgres import PostgresOptionsStructuresRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        engine,
        PostgresOptionsQuoteIntelligenceRepository(engine),
        PostgresOptionsStructuresRepository(engine),
        PostgresOptionsResearchOperationsRepository(engine),
    )


def options_research_operations_command(arguments: argparse.Namespace) -> None:
    """Bounded command surface for Fresh Options Evidence Operations and
    Structure Research Baseline v1 -- governed recurring options-research
    evidence capture, chain/history/quality inspection. Research evidence
    only: no options order, PAPER order, or Shared Opportunity is ever
    created here; ``authority`` stays ``NONE`` throughout, and zero
    provider requests are made by any command except ``options-research-
    run-once`` while genuinely due."""
    from apex.adapters.options_research_capture import (
        AlpacaOptionsResearchEvidenceCapture,
        OptionsResearchEvidenceCaptureCommand,
        resolve_options_capture_due_decision,
    )
    from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
    from apex.application.options_quote_intelligence import ResolveRealizedVolatility
    from apex.application.options_research_operations import ComputeOptionsHistoryReadiness
    from apex.application.options_structures import (
        CreateOptionsFeePolicy,
        CreateOptionsSlippagePolicy,
        CreateOptionsStructureResearchPolicy,
        CreateStructurePricingPolicy,
    )
    from apex.domain.options_research_operations import (
        MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
        ChainCoverageQualityPolicy,
        OptionsCaptureDueStatus,
        OptionsCaptureWindowPolicy,
        project_forward_storage,
    )
    from apex.domain.options_structures import (
        OptionsFeePolicy,
        OptionsSlippagePolicy,
        OptionsStructureResearchPolicy,
        StructurePricingPolicy,
    )
    from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

    engine, quote_repo, structures_repo, checkpoint_repo = _options_research_operations_runtime()
    try:
        now = datetime.now(tz=UTC)
        command = arguments.command

        if command == "options-research-due-inspect":
            plan, window_policy, _sessions, decision = resolve_options_capture_due_decision(
                quote_repository=quote_repo,
                checkpoint_repository=checkpoint_repo,
                as_of=now,
            )
            _research_evidence_json(
                {
                    "command": command,
                    "now": now,
                    "plan_id": None if plan is None else plan.plan_id,
                    "capture_window_policy": window_policy,
                    "status": decision.status,
                    "session_id": decision.session_id,
                    "window_open_at": decision.window_open_at,
                    "window_close_at": decision.window_close_at,
                    "reasons": decision.reasons,
                }
            )
            return

        if command == "options-research-run-once":
            _assert_no_tracked_provider_credentials()
            plan, window_policy, sessions, decision = resolve_options_capture_due_decision(
                quote_repository=quote_repo,
                checkpoint_repository=checkpoint_repo,
                as_of=now,
            )
            if plan is None or decision.status is not OptionsCaptureDueStatus.DUE:
                _research_evidence_json(
                    {
                        "command": command,
                        "ran": False,
                        "plan_id": None if plan is None else plan.plan_id,
                        "status": decision.status,
                        "session_id": decision.session_id,
                        "window_open_at": decision.window_open_at,
                        "window_close_at": decision.window_close_at,
                        "reasons": decision.reasons,
                    }
                )
                return
            pricing_policy = structures_repo.latest_pricing_policy()
            if pricing_policy is None:
                pricing_policy = CreateStructurePricingPolicy(structures_repo).execute(
                    policy=StructurePricingPolicy.create()
                )
            fee_policy = structures_repo.latest_fee_policy()
            if fee_policy is None:
                fee_policy = CreateOptionsFeePolicy(structures_repo).execute(
                    policy=OptionsFeePolicy.create_alpaca_regulatory_evidenced_2026_04_01()
                )
            slippage_policy = structures_repo.latest_slippage_policy()
            if slippage_policy is None:
                slippage_policy = CreateOptionsSlippagePolicy(structures_repo).execute(
                    policy=OptionsSlippagePolicy.create()
                )
            structure_policy = structures_repo.latest_structure_policy()
            if structure_policy is None:
                structure_policy = CreateOptionsStructureResearchPolicy(structures_repo).execute(
                    policy=OptionsStructureResearchPolicy.create(
                        pricing_policy=pricing_policy,
                        fee_policy=fee_policy,
                        slippage_policy=slippage_policy,
                        created_at=now,
                    )
                )
            from apex.adapters.historical_market_data_postgres import (
                PostgresHistoricalMarketDataRepository,
            )
            from apex.adapters.securities_postgres import PostgresSecuritiesRepository

            rv_resolver = ResolveRealizedVolatility(
                historical_repository=PostgresHistoricalMarketDataRepository(engine),
                evidence=PostgresSecuritiesRepository(engine),
                cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            )
            capture = AlpacaOptionsResearchEvidenceCapture(
                quote_repository=quote_repo,
                structures_repository=structures_repo,
                checkpoint_repository=checkpoint_repo,
                rv_resolver=rv_resolver,
            )
            outcome = capture.run(
                OptionsResearchEvidenceCaptureCommand(
                    plan=plan,
                    due=decision,
                    window_policy=window_policy,
                    sessions=sessions,
                    coverage_policy=ChainCoverageQualityPolicy.create(),
                    structure_policy=structure_policy,
                    fee_policy=fee_policy,
                    slippage_policy=slippage_policy,
                    feed_entitlement_state="INDICATIVE_ONLY_OPRA_UNCONFIRMED",
                    now=now,
                )
            )
            _research_evidence_json(
                {
                    "command": command,
                    "ran": True,
                    "checkpoint": outcome.checkpoint,
                    "per_underlying": outcome.per_underlying,
                    "provider_request_count": outcome.provider_request_count,
                }
            )
            return

        if command == "options-forward-checkpoint-inspect":
            plan_id = getattr(arguments, "plan_id", None)
            plan = quote_repo.plan_by_id(plan_id) if plan_id else quote_repo.latest_plan()
            if plan is None:
                raise SystemExit("exact options chain capture plan was not found")
            session_id = getattr(arguments, "session_id", None)
            checkpoint = (
                checkpoint_repo.latest_checkpoint_for_session(
                    plan_id=plan.plan_id, session_id=session_id
                )
                if session_id
                else checkpoint_repo.latest_checkpoint(plan.plan_id)
            )
            if checkpoint is None:
                raise SystemExit("no retained options forward-evidence checkpoint")
            _research_evidence_json({"command": command, "checkpoint": checkpoint})
            return

        if command == "options-history-inspect":
            underlying_id = arguments.underlying_canonical_security_id
            window_policy = OptionsCaptureWindowPolicy.create()
            calendar = ReviewedXNYSCalendar(
                start_date=(now - timedelta(days=400)).date().isoformat(),
                end_date=(now + timedelta(days=5)).date().isoformat(),
            )
            readiness = ComputeOptionsHistoryReadiness(quote_repository=quote_repo).execute(
                underlying_canonical_security_id=underlying_id,
                sessions=calendar.sessions,
                policy=window_policy,
                minimum_required_sessions=MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
            )
            all_snapshots = quote_repo.snapshots_for_underlying(underlying_id)
            _research_evidence_json(
                {
                    "command": command,
                    "underlying_canonical_security_id": underlying_id,
                    "readiness": readiness,
                    "total_retained_snapshot_rows": len(all_snapshots),
                    "first_retained_snapshot_captured_at": (
                        all_snapshots[0].captured_at if all_snapshots else None
                    ),
                    "latest_retained_snapshot_captured_at": (
                        all_snapshots[-1].captured_at if all_snapshots else None
                    ),
                }
            )
            return

        if command == "options-research-quality-inspect":
            plan = quote_repo.latest_plan()
            if plan is None:
                raise SystemExit("no retained options chain capture plan")
            session_id = getattr(arguments, "session_id", None)
            checkpoint = (
                checkpoint_repo.latest_checkpoint_for_session(
                    plan_id=plan.plan_id, session_id=session_id
                )
                if session_id
                else checkpoint_repo.latest_checkpoint(plan.plan_id)
            )
            capability = quote_repo.latest_capability("ALPACA_OPTIONS_MARKET_DATA")
            _plan_for_report, window_policy, sessions, due_decision = (
                resolve_options_capture_due_decision(
                    quote_repository=quote_repo, checkpoint_repository=checkpoint_repo, as_of=now
                )
            )
            readiness_by_underlying = tuple(
                ComputeOptionsHistoryReadiness(quote_repository=quote_repo).execute(
                    underlying_canonical_security_id=member.stable_security_id,
                    sessions=sessions,
                    policy=window_policy,
                    minimum_required_sessions=MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
                )
                for member in plan.members
            )
            readiness_state_counts: dict[str, int] = {}
            for item in readiness_by_underlying:
                readiness_state_counts[item.state.value] = (
                    readiness_state_counts.get(item.state.value, 0) + 1
                )
            _research_evidence_json(
                {
                    "command": command,
                    "plan_id": plan.plan_id,
                    "feed": plan.feed,
                    "feed_entitlement_state": "INDICATIVE_ONLY_OPRA_UNCONFIRMED",
                    "session_timing": {
                        "status": due_decision.status,
                        "session_id": due_decision.session_id,
                        "window_open_at": due_decision.window_open_at,
                        "window_close_at": due_decision.window_close_at,
                        "reasons": due_decision.reasons,
                    },
                    "freshness_threshold_seconds": window_policy.freshness_threshold_seconds,
                    "capability_credentials_available": (
                        capability.credentials_available if capability is not None else None
                    ),
                    "capability_limitations": (
                        capability.capability.limitations if capability is not None else ()
                    ),
                    "opra_entitlement_state": "OPRA_FEED_ENTITLEMENT_NOT_SEPARATELY_CONFIRMED",
                    "open_interest_limitation": "OPEN_INTEREST_UNAVAILABLE",
                    "historical_options_pricing_limitation": (
                        "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE"
                    ),
                    "iv_history_minimum_required": (
                        MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH
                    ),
                    "research_readiness_state_counts": tuple(
                        sorted(readiness_state_counts.items())
                    ),
                    "storage_projection_snapshot_rows": {
                        "basis": (
                            "one options_chain_snapshot row per cohort underlying "
                            "per genuinely captured session (forward-only; no "
                            "historical backfill)"
                        ),
                        "observations_per_session": len(plan.members),
                        "projected": tuple(
                            {
                                "sessions": item.sessions,
                                "projected_observations": item.projected_observations,
                            }
                            for item in project_forward_storage(
                                observations_per_session=len(plan.members)
                            )
                        ),
                    },
                    "latest_checkpoint": checkpoint,
                }
            )
            return

        raise SystemExit(f"unsupported options-research command {command!r}")
    finally:
        engine.dispose()


def _corporate_fundamentals_runtime() -> tuple[Any, Any, Any]:
    """Engine plus the corporate-fundamentals repository and the read-only
    stable-identity repository used only to resolve the genuine cohort's
    already-retained CIK evidence -- never to mutate identity state."""
    from apex.adapters.corporate_fundamentals_postgres import (
        PostgresCorporateFundamentalsRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.security_identity_postgres import PostgresSecurityIdentityRepository

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        engine,
        PostgresCorporateFundamentalsRepository(engine),
        PostgresSecurityIdentityRepository(engine),
    )


def corporate_fundamentals_command(arguments: argparse.Namespace) -> None:
    """Bounded command surface for Corporate Fundamental, Filing, and Event
    Intelligence v1 -- point-in-time official SEC filing/fact research
    evidence only. No Strategy, Opportunity, Order, PAPER order, or LIVE
    authority is ever created here; ``authority`` stays ``NONE``
    throughout, and zero SEC requests are made by any command except
    ``corporate-evidence-capture`` while genuine issuers remain pending.

    ``corporate-market-*`` and ``corporate-calibration-*`` are narrower
    prefixes than this function's own ``corporate-*`` dispatch guard, so
    both are handled first, here, rather than as their own earlier
    top-level ``startswith`` branches -- keeping exactly one
    ``startswith("corporate-")`` guard in the outer dispatcher (see
    ``test_corporate_prefix_does_not_collide_with_any_pre_existing_
    command_family``). ``corporate-calibration-*`` is checked before
    ``corporate-market-*`` because ``corporate-market-recalibrate`` (a
    calibration-depth command) would otherwise match the latter's
    narrower prefix first."""
    if arguments.command.startswith("corporate-calibration-") or arguments.command in (
        "corporate-market-recalibrate",
        "corporate-followup-requalify",
    ):
        corporate_calibration_depth_command(arguments)
        return
    # ``corporate-followup-requalify`` (Corporate Research Calibration Depth
    # v1's requalification of the 13 retained v3 follow-up proposals) is
    # already routed above, before this narrower ``corporate-followup-``
    # prefix guard -- everything else with this prefix belongs to Corporate
    # Calibration-Qualified Regime Follow-Up v1, the bounded evaluation of
    # exactly the one proposal that requalification marked
    # CALIBRATION_QUALIFIED.
    if arguments.command.startswith("corporate-followup-"):
        corporate_regime_followup_command(arguments)
        return
    if arguments.command.startswith("corporate-market-"):
        corporate_market_research_command(arguments)
        return
    from apex.application.corporate_fundamentals import (
        AttachCorporateEvidenceToReplayFrame,
        AttachCorporateEvidenceToReplayFrameCommand,
        BuildCorporateFundamentalSnapshotAsOf,
        BuildCorporateFundamentalSnapshotCommand,
        BuildCorporateReplayFeatureFrame,
        BuildCorporateReplayFeatureFrameCommand,
        ComputeDerivedFundamentalFeaturesAsOf,
        ComputeFilingEventFeaturesAsOf,
        ListEarningsEvents,
        MaterializeCorporateHistoryDataset,
        MaterializeCorporateHistoryDatasetCommand,
        RegisterCorporateEvidenceCohortPlan,
        RegisterCorporateEvidenceCohortPlanCommand,
        issuer_quality_assessments_for_plan,
        resolve_cik_for_security,
    )
    from apex.domain.corporate_fundamentals import (
        CorporateEvidenceCaptureBounds,
        FundamentalConcept,
        LookbackWindow,
        corporate_fundamentals_warnings,
        pending_members,
    )
    from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1, resolve_cohort

    engine, repository, identity_repository = _corporate_fundamentals_runtime()
    try:
        now = datetime.now(tz=UTC)
        command = arguments.command

        def _resolve_plan() -> Any:
            plan_id = getattr(arguments, "plan_id", None)
            plan = repository.plan_by_id(plan_id) if plan_id else repository.latest_plan()
            if plan is None:
                raise SystemExit(
                    "no retained corporate evidence cohort plan -- run "
                    "corporate-evidence-plan-create first"
                )
            return plan

        if command == "corporate-evidence-plan-create":
            stable_identities = identity_repository.latest_stable_identities()
            resolved_cohort = resolve_cohort(
                GENUINE_MULTI_ASSET_COHORT_POLICY_V1, stable_identities=stable_identities
            )
            bounds = CorporateEvidenceCaptureBounds.create()
            plan, _resolution = RegisterCorporateEvidenceCohortPlan(repository=repository).execute(
                RegisterCorporateEvidenceCohortPlanCommand(
                    cohort_policy_id=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id,
                    cohort_policy_hash=GENUINE_MULTI_ASSET_COHORT_POLICY_V1.canonical_hash,
                    resolved_cohort=resolved_cohort,
                    stable_identities=stable_identities,
                    bounds=bounds,
                    created_at=now,
                )
            )
            _research_evidence_json(
                {
                    "command": command,
                    "plan_id": plan.plan_id,
                    "member_count": len(plan.members),
                    "excluded_missing_cik": plan.excluded_missing_cik,
                    "excluded_unresolved": plan.excluded_unresolved,
                    "cohort_unresolved_symbols": resolved_cohort.unresolved_symbols,
                    "cohort_ambiguous_symbols": resolved_cohort.ambiguous_symbols,
                    "bounds": plan.bounds,
                    "warnings": corporate_fundamentals_warnings(),
                }
            )
            return

        if command == "corporate-evidence-plan-inspect":
            # Doubles as the bounded capture-status view (capture progress,
            # remaining issuers, effective lookback): a second, identically-
            # named "corporate-capture-status" command would just be this
            # same read with a different label, so it is not duplicated.
            plan = _resolve_plan()
            checkpoints = repository.checkpoints_for_plan(plan.plan_id)
            processed_ciks = repository.processed_ciks(plan.plan_id)
            remaining = pending_members(plan, processed_ciks=processed_ciks)
            latest_checkpoint = checkpoints[-1] if checkpoints else None
            latest_dataset = repository.latest_dataset_snapshot(plan.plan_id)
            effective_lookback = LookbackWindow.create(
                lookback_years=plan.bounds.lookback_years, reference_date=now.date()
            )
            _research_evidence_json(
                {
                    "command": command,
                    "plan": plan,
                    "checkpoint_count": len(checkpoints),
                    "latest_checkpoint": latest_checkpoint,
                    "processed_issuer_count": len(processed_ciks),
                    "capture_complete": bool(latest_checkpoint and latest_checkpoint.complete),
                    "remaining_issuer_count": len(remaining),
                    "remaining_issuer_symbols": tuple(sorted(m.symbol for m in remaining)),
                    "effective_lookback": effective_lookback,
                    "latest_dataset_snapshot_id": (
                        latest_dataset.dataset_id if latest_dataset is not None else None
                    ),
                }
            )
            return

        if command == "corporate-evidence-capture":
            _assert_no_tracked_provider_credentials()
            from apex.adapters.corporate_evidence_capture import (
                CorporateEvidenceCaptureCommand,
                SecCorporateEvidenceCapture,
            )

            plan = _resolve_plan()
            user_agent = os.environ.get(
                "APEX_SEC_USER_AGENT", "ApexBroadMarketResearch/1.0 (research@apex.local)"
            )
            checkpoint = SecCorporateEvidenceCapture(repository=repository).run(
                CorporateEvidenceCaptureCommand(plan=plan, now=now, user_agent=user_agent)
            )
            _research_evidence_json(
                {"command": command, "plan_id": plan.plan_id, "checkpoint": checkpoint}
            )
            return

        if command == "corporate-filings-inspect":
            stable_security_id = arguments.stable_security_id
            filings = repository.filings_for(stable_security_id)
            _research_evidence_json(
                {
                    "command": command,
                    "stable_security_id": stable_security_id,
                    "filing_count": len(filings),
                    "filings": filings,
                    "earnings_events": ListEarningsEvents(repository=repository).execute(
                        stable_security_id=stable_security_id
                    ),
                }
            )
            return

        if command == "corporate-facts-inspect":
            stable_security_id = arguments.stable_security_id
            facts = repository.facts_for(stable_security_id)
            concept_raw = getattr(arguments, "concept", None)
            if concept_raw:
                concept = FundamentalConcept(concept_raw)
                facts = tuple(item for item in facts if item.concept is concept)
            _research_evidence_json(
                {
                    "command": command,
                    "stable_security_id": stable_security_id,
                    "fact_count": len(facts),
                    "facts": facts,
                }
            )
            return

        if command == "corporate-snapshot-inspect":
            stable_security_id = arguments.stable_security_id
            as_of_raw = getattr(arguments, "as_of", None)
            as_of = _real_data_date(as_of_raw) if as_of_raw else now
            cik = getattr(arguments, "cik", None) or resolve_cik_for_security(
                repository, stable_security_id
            )
            if cik is None:
                raise SystemExit(
                    "no retained CIK evidence for this stable_security_id -- run "
                    "corporate-evidence-capture first, or pass --cik explicitly"
                )
            snapshot = BuildCorporateFundamentalSnapshotAsOf(repository=repository).execute(
                BuildCorporateFundamentalSnapshotCommand(
                    stable_security_id=stable_security_id, cik=cik, as_of=as_of
                )
            )
            _research_evidence_json({"command": command, "snapshot": snapshot})
            return

        if command == "corporate-features-inspect":
            stable_security_id = arguments.stable_security_id
            as_of_raw = getattr(arguments, "as_of", None)
            as_of = _real_data_date(as_of_raw) if as_of_raw else now
            cik = getattr(arguments, "cik", None) or resolve_cik_for_security(
                repository, stable_security_id
            )
            if cik is None:
                raise SystemExit(
                    "no retained CIK evidence for this stable_security_id -- run "
                    "corporate-evidence-capture first, or pass --cik explicitly"
                )
            snapshot, derived = ComputeDerivedFundamentalFeaturesAsOf(
                repository=repository
            ).execute(
                BuildCorporateFundamentalSnapshotCommand(
                    stable_security_id=stable_security_id, cik=cik, as_of=as_of
                )
            )
            filing_features = ComputeFilingEventFeaturesAsOf(repository=repository).execute(
                stable_security_id=stable_security_id, as_of=as_of
            )
            _research_evidence_json(
                {
                    "command": command,
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_quality": snapshot.quality,
                    "derived_features": derived,
                    "filing_event_features": filing_features,
                }
            )
            return

        if command == "corporate-history-quality-inspect":
            plan = _resolve_plan()
            report = repository.latest_quality_report(plan.plan_id)
            assessments = issuer_quality_assessments_for_plan(repository=repository, plan=plan)
            _research_evidence_json(
                {
                    "command": command,
                    "plan_id": plan.plan_id,
                    "latest_persisted_quality_report": report,
                    "current_issuer_assessments": assessments,
                }
            )
            return

        if command == "corporate-dataset-materialize":
            plan = _resolve_plan()
            report, dataset = MaterializeCorporateHistoryDataset(repository=repository).execute(
                MaterializeCorporateHistoryDatasetCommand(plan_id=plan.plan_id, created_at=now)
            )
            _research_evidence_json(
                {"command": command, "quality_report": report, "dataset": dataset}
            )
            return

        if command == "corporate-dataset-inspect":
            dataset_id = getattr(arguments, "dataset_id", None)
            dataset = (
                repository.dataset_snapshot_by_id(dataset_id)
                if dataset_id
                else repository.latest_dataset_snapshot(_resolve_plan().plan_id)
            )
            if dataset is None:
                raise SystemExit("no retained corporate history dataset snapshot")
            _research_evidence_json({"command": command, "dataset": dataset})
            return

        if command == "corporate-replay-attach":
            from apex.adapters.historical_replay_postgres import (
                PostgresHistoricalReplayRepository,
            )

            replay_repository = PostgresHistoricalReplayRepository(engine)
            frame_limit = getattr(arguments, "frame_limit", None) or 2_000
            started = time.monotonic()
            attachments, stats = AttachCorporateEvidenceToReplayFrame(
                repository=repository, frame_source=replay_repository
            ).execute_with_stats(
                AttachCorporateEvidenceToReplayFrameCommand(
                    replay_run_id=arguments.replay_run_id, created_at=now, frame_limit=frame_limit
                )
            )
            runtime_seconds = round(time.monotonic() - started, 3)
            quality_counts: dict[str, int] = {}
            for item in attachments:
                key = item.snapshot_quality.value
                quality_counts[key] = quality_counts.get(key, 0) + 1
            _research_evidence_json(
                {
                    "command": command,
                    "replay_run_id": arguments.replay_run_id,
                    "frame_limit": frame_limit,
                    "runtime_seconds": str(runtime_seconds),
                    "frames_per_second": (
                        str(round(stats.frames_considered / runtime_seconds, 3))
                        if runtime_seconds > 0
                        else None
                    ),
                    "stats": stats,
                    "attachment_count": len(attachments),
                    "attachment_quality_counts": tuple(sorted(quality_counts.items())),
                    # Bounded sample only -- the real 1,515-frame replay
                    # attaches roughly one row per (frame, evidenced
                    # security); dumping every row here would defeat
                    # "bounded output" for no operator benefit. Inspect a
                    # specific one with corporate-replay-feature-inspect.
                    "attachment_sample": attachments[:5],
                }
            )
            return

        if command == "corporate-replay-feature-inspect":
            from apex.adapters.historical_replay_postgres import (
                PostgresHistoricalReplayRepository,
            )

            replay_repository = PostgresHistoricalReplayRepository(engine)
            frame_feature = BuildCorporateReplayFeatureFrame(
                repository=repository, frame_source=replay_repository
            ).execute(
                BuildCorporateReplayFeatureFrameCommand(
                    replay_run_id=arguments.replay_run_id,
                    frame_id=arguments.frame_id,
                    stable_security_id=arguments.stable_security_id,
                )
            )
            _research_evidence_json({"command": command, "feature_frame": frame_feature})
            return

        raise SystemExit(f"unsupported corporate command {command!r}")
    finally:
        engine.dispose()


def corporate_calibration_depth_command(arguments: argparse.Namespace) -> None:
    """Bounded command surface for Corporate Research Calibration Depth v1
    -- methodology calibration over the already-retained Cross-Market
    Research v3 agenda (20 genuine hypotheses, 17 negative controls).
    Never widens, reruns, or rewrites the v3 agenda's own search; every
    command here makes zero provider requests (the shared corporate/
    equity/replay evidence it reads was already retained by earlier,
    separately governed capture and research milestones). ``authority``
    stays ``NONE`` throughout."""

    from apex.adapters.corporate_calibration_depth_postgres import (
        PostgresCorporateCalibrationDepthRepository,
    )
    from apex.adapters.corporate_fundamentals_postgres import (
        PostgresCorporateFundamentalsRepository,
    )
    from apex.adapters.corporate_market_research_postgres import (
        PostgresCorporateMarketResearchRepository,
    )
    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.corporate_calibration_depth import (
        AdvanceCorporateCalibrationRun,
        FinalizeCorporateCalibration,
        RegisterCorporateCalibrationPlan,
        RegisterCorporateCalibrationRun,
        RequalifyCorporateFollowUps,
    )
    from apex.application.corporate_market_research import (
        BuildCorporatePanel,
        split_train_validation_holdout,
    )
    from apex.domain.corporate_calibration_depth import (
        CorporateCalibrationPlan,
        corporate_calibration_depth_warnings,
    )

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresCorporateCalibrationDepthRepository(engine)
    corporate_market_repository = PostgresCorporateMarketResearchRepository(engine)
    corporate_repository = PostgresCorporateFundamentalsRepository(engine)
    methodology_repository = PostgresResearchMethodologyRepository(engine)
    try:
        now = datetime.now(tz=UTC)
        command = arguments.command

        def _resolve_plan() -> CorporateCalibrationPlan:
            calibration_id = getattr(arguments, "calibration_id", None)
            plan = (
                repository.plan_by_id(calibration_id)
                if calibration_id
                else (repository.list_plans() or (None,))[-1]
            )
            if plan is None:
                raise SystemExit(
                    "no retained corporate calibration-depth plan -- run "
                    "corporate-calibration-create first"
                )
            return plan

        def _panel_and_period(plan: CorporateCalibrationPlan) -> tuple[Any, Any]:
            panel = BuildCorporatePanel(
                corporate_repository=corporate_repository,
                historical_repository=PostgresHistoricalMarketDataRepository(engine),
                evidence=PostgresSecuritiesRepository(engine),
                replay_repository=PostgresHistoricalReplayRepository(engine),
            ).execute(
                corporate_dataset_id=plan.corporate_dataset_id,
                equity_snapshot_id=plan.equity_snapshot_id,
                replay_run_id=plan.replay_run_id,
                cross_market_dataset_id=plan.cross_market_dataset_id,
            )
            period_policy = split_train_validation_holdout(
                panel.monthly_dates, replay_run_id=plan.replay_run_id
            )
            if period_policy is None:
                raise SystemExit("CORPORATE_CALIBRATION_DEPTH_PERIOD_POLICY_UNAVAILABLE")
            return panel, period_policy

        if command == "corporate-calibration-create":
            agenda_id = getattr(arguments, "agenda_id", None)
            if not agenda_id:
                agenda = (corporate_market_repository.list_agendas() or (None,))[-1]
                if agenda is None:
                    raise SystemExit(
                        "no retained corporate-market research agenda -- run "
                        "corporate-market-agenda-create first"
                    )
                agenda_id = agenda.agenda_id
            plan = RegisterCorporateCalibrationPlan(
                repository=repository,
                corporate_market_repository=corporate_market_repository,
                methodology=methodology_repository,
            ).execute(agenda_id=agenda_id, registered_at=parsed(arguments.created_at))
            _research_evidence_json(
                {
                    "command": command,
                    "plan": plan,
                    "strata_count": len(plan.stratum_targets),
                    "warnings": corporate_calibration_depth_warnings(),
                }
            )
            return

        if command == "corporate-calibration-inspect":
            plan = _resolve_plan()
            run = repository.run_by_calibration(plan.calibration_id)
            report = repository.report_by_calibration(plan.calibration_id)
            _research_evidence_json(
                {
                    "command": command,
                    "plan": plan,
                    "run": run,
                    "report": report,
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-calibration-strata-inspect":
            plan = _resolve_plan()
            insufficient = tuple(
                item.matched_stratum
                for item in plan.stratum_targets
                if not item.original_sufficient
            )
            _research_evidence_json(
                {
                    "command": command,
                    "calibration_id": plan.calibration_id,
                    "controls_per_stratum": plan.controls_per_stratum,
                    "strata": plan.stratum_targets,
                    "strata_count": len(plan.stratum_targets),
                    "originally_sufficient_count": len(plan.stratum_targets) - len(insufficient),
                    "originally_insufficient_strata": insufficient,
                    "authority": "NONE",
                }
            )
            return

        if command in ("corporate-calibration-run-start", "corporate-calibration-run-resume"):
            plan = _resolve_plan()
            created_at = parsed(arguments.created_at)
            if command == "corporate-calibration-run-start":
                RegisterCorporateCalibrationRun(repository=repository).execute(
                    plan=plan, created_at=created_at
                )
            panel, period_policy = _panel_and_period(plan)
            advancer = AdvanceCorporateCalibrationRun(
                repository=repository,
                methodology=methodology_repository,
                panel=panel,
                period_policy=period_policy,
            )
            steps = getattr(arguments, "steps", None) or 50
            last_result = None
            for _step in range(steps):
                last_result = advancer.execute(plan=plan, created_at=created_at)
                if last_result.run.status.value == "COMPLETE":
                    break
            if last_result is None:
                raise SystemExit("CORPORATE_CALIBRATION_RUN_MADE_NO_PROGRESS")
            _research_evidence_json(
                {
                    "command": command,
                    "calibration_id": plan.calibration_id,
                    "run": last_result.run,
                    "controls_completed": len(last_result.run.completed_spec_ids),
                    "controls_total": len(last_result.run.spec_ids),
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-calibration-run-inspect":
            plan = _resolve_plan()
            run = repository.run_by_calibration(plan.calibration_id)
            if run is None:
                raise SystemExit("CORPORATE_CALIBRATION_RUN_NOT_REGISTERED")
            _research_evidence_json(
                {
                    "command": command,
                    "calibration_id": plan.calibration_id,
                    "run": run,
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-market-recalibrate":
            plan = _resolve_plan()
            run = repository.run_by_calibration(plan.calibration_id)
            if run is None or run.status.value != "COMPLETE":
                raise SystemExit(
                    "CORPORATE_CALIBRATION_RUN_NOT_COMPLETE -- run corporate-calibration-run-start "
                    "/-resume to completion first"
                )
            panel, period_policy = _panel_and_period(plan)
            report = FinalizeCorporateCalibration(
                repository=repository,
                corporate_market_repository=corporate_market_repository,
                methodology=methodology_repository,
                panel=panel,
                period_policy=period_policy,
            ).execute(plan=plan, run=run, created_at=now)
            reassessments = repository.reassessments_for_calibration(plan.calibration_id)
            _research_evidence_json(
                {
                    "command": command,
                    "calibration_id": plan.calibration_id,
                    "report": report,
                    "reassessment_count": len(reassessments),
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-calibration-report-inspect":
            plan = _resolve_plan()
            report = repository.report_by_calibration(plan.calibration_id)
            if report is None:
                raise SystemExit(
                    "no retained calibration-depth report -- run corporate-market-recalibrate first"
                )
            reassessments = repository.reassessments_for_calibration(plan.calibration_id)
            _research_evidence_json(
                {
                    "command": command,
                    "calibration_id": plan.calibration_id,
                    "report": report,
                    "reassessments": reassessments,
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-followup-requalify":
            plan = _resolve_plan()
            reassessments = repository.reassessments_for_calibration(plan.calibration_id)
            if not reassessments:
                raise SystemExit(
                    "no retained reassessments -- run corporate-market-recalibrate first"
                )
            qualifications = RequalifyCorporateFollowUps(
                repository=repository,
                corporate_market_repository=corporate_market_repository,
            ).execute(plan=plan, reassessments=reassessments, created_at=now)
            counts: dict[str, int] = {}
            for item in qualifications:
                counts[item.qualification.value] = counts.get(item.qualification.value, 0) + 1
            _research_evidence_json(
                {
                    "command": command,
                    "calibration_id": plan.calibration_id,
                    "follow_up_qualifications": qualifications,
                    "qualification_distribution": tuple(sorted(counts.items())),
                    "authority": "NONE",
                }
            )
            return

        raise SystemExit(f"unsupported corporate-calibration-depth command {command!r}")
    finally:
        engine.dispose()


def corporate_regime_followup_command(arguments: argparse.Namespace) -> None:
    """Bounded command surface for Corporate Calibration-Qualified Regime
    Follow-Up v1 -- the single ``SPLIT_BY_REGIME`` follow-up
    (``cross_market_follow_up_622f5a0d3af61dcb7b69bd9b``) that Corporate
    Research Calibration Depth v1's requalification marked
    ``CALIBRATION_QUALIFIED``. Post-hoc mechanism/stability investigation,
    never independent replication or forward confirmation; every command
    here makes zero provider requests and ``authority`` stays ``NONE``
    throughout."""

    from apex.adapters.corporate_calibration_depth_postgres import (
        PostgresCorporateCalibrationDepthRepository,
    )
    from apex.adapters.corporate_fundamentals_postgres import (
        PostgresCorporateFundamentalsRepository,
    )
    from apex.adapters.corporate_market_research_postgres import (
        PostgresCorporateMarketResearchRepository,
    )
    from apex.adapters.corporate_regime_followup_postgres import (
        PostgresCorporateRegimeFollowUpRepository,
    )
    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.corporate_market_research import BuildCorporatePanel
    from apex.application.corporate_regime_followup import (
        PARENT_FOLLOW_UP_ID,
        CreateCorporateRegimeFollowUpSpecification,
        RunCorporateRegimeFollowUp,
    )
    from apex.domain.corporate_regime_followup import corporate_regime_followup_warnings

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresCorporateRegimeFollowUpRepository(engine)
    corporate_market_repository = PostgresCorporateMarketResearchRepository(engine)
    methodology_repository = PostgresResearchMethodologyRepository(engine)
    calibration_depth_repository = PostgresCorporateCalibrationDepthRepository(engine)
    corporate_repository = PostgresCorporateFundamentalsRepository(engine)
    panel_builder = BuildCorporatePanel(
        corporate_repository=corporate_repository,
        historical_repository=PostgresHistoricalMarketDataRepository(engine),
        evidence=PostgresSecuritiesRepository(engine),
        replay_repository=PostgresHistoricalReplayRepository(engine),
    )
    try:
        command = arguments.command

        def _resolve_specification() -> Any:
            specification_id = getattr(arguments, "specification_id", None)
            if specification_id:
                specification = repository.specification_by_id(specification_id)
            else:
                follow_up_id = getattr(arguments, "follow_up_id", None) or PARENT_FOLLOW_UP_ID
                specification = repository.specification_by_follow_up(follow_up_id)
            if specification is None:
                raise SystemExit(
                    "no retained corporate regime follow-up specification -- run "
                    "corporate-followup-spec-create first"
                )
            return specification

        if command == "corporate-followup-spec-create":
            follow_up_id = getattr(arguments, "follow_up_id", None) or PARENT_FOLLOW_UP_ID
            specification = CreateCorporateRegimeFollowUpSpecification(
                repository=repository,
                corporate_market_repository=corporate_market_repository,
                methodology=methodology_repository,
                calibration_depth=calibration_depth_repository,
                panel_builder=panel_builder,
            ).execute(follow_up_id=follow_up_id, created_at=parsed(arguments.created_at))
            _research_evidence_json(
                {
                    "command": command,
                    "specification": specification,
                    "warnings": corporate_regime_followup_warnings(),
                }
            )
            return

        if command == "corporate-followup-spec-inspect":
            specification = _resolve_specification()
            _research_evidence_json(
                {"command": command, "specification": specification, "authority": "NONE"}
            )
            return

        if command in ("corporate-followup-run-start", "corporate-followup-run-resume"):
            specification = _resolve_specification()
            result = RunCorporateRegimeFollowUp(
                repository=repository, panel_builder=panel_builder
            ).execute(specification=specification, created_at=parsed(arguments.created_at))
            controls = repository.controls_for_specification(specification.specification_id)
            _research_evidence_json(
                {
                    "command": command,
                    "specification_id": specification.specification_id,
                    "result": result,
                    "null_control_count": len(controls),
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-followup-run-inspect":
            specification = _resolve_specification()
            retained_result = repository.result_by_specification(specification.specification_id)
            if retained_result is None:
                raise SystemExit(
                    "no retained corporate regime follow-up result -- run "
                    "corporate-followup-run-start first"
                )
            controls = repository.controls_for_specification(specification.specification_id)
            _research_evidence_json(
                {
                    "command": command,
                    "specification": specification,
                    "result": retained_result,
                    "null_control_count": len(controls),
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-followup-stability-inspect":
            specification = _resolve_specification()
            retained_result = repository.result_by_specification(specification.specification_id)
            if retained_result is None:
                raise SystemExit(
                    "no retained corporate regime follow-up result -- run "
                    "corporate-followup-run-start first"
                )
            _research_evidence_json(
                {
                    "command": command,
                    "specification_id": specification.specification_id,
                    "primary_interaction_stability": (
                        retained_result.primary_split.interaction_stability
                    ),
                    "secondary_interaction_stability": (
                        retained_result.secondary_split.interaction_stability
                    ),
                    "parent_relationship": retained_result.parent_relationship,
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-followup-calibration-inspect":
            specification = _resolve_specification()
            retained_result = repository.result_by_specification(specification.specification_id)
            if retained_result is None:
                raise SystemExit(
                    "no retained corporate regime follow-up result -- run "
                    "corporate-followup-run-start first"
                )
            primary_controls = repository.controls_for_specification(
                specification.specification_id, role=retained_result.primary_split.role
            )
            secondary_controls = repository.controls_for_specification(
                specification.specification_id, role=retained_result.secondary_split.role
            )
            _research_evidence_json(
                {
                    "command": command,
                    "specification_id": specification.specification_id,
                    "matched_null_stratum": specification.matched_null_stratum,
                    "matched_null_count": specification.matched_null_count,
                    "matched_null_percentile": specification.matched_null_percentile,
                    "primary_interaction": retained_result.primary_split.interaction,
                    "primary_null_control_count": len(primary_controls),
                    "secondary_interaction": retained_result.secondary_split.interaction,
                    "secondary_null_control_count": len(secondary_controls),
                    "multiple_testing_results": retained_result.multiple_testing_results,
                    "multiple_testing_family_size": retained_result.multiple_testing_family_size,
                    "multiple_testing_method": retained_result.multiple_testing_method,
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-followup-conclusion-inspect":
            specification = _resolve_specification()
            retained_result = repository.result_by_specification(specification.specification_id)
            if retained_result is None:
                raise SystemExit(
                    "no retained corporate regime follow-up result -- run "
                    "corporate-followup-run-start first"
                )
            _research_evidence_json(
                {
                    "command": command,
                    "specification_id": specification.specification_id,
                    "post_hoc_status": specification.post_hoc_status,
                    "conclusion": retained_result.conclusion,
                    "conclusion_rationale": retained_result.conclusion_rationale,
                    "evidence_grade": retained_result.evidence_grade,
                    "evidence_grade_rationale": retained_result.evidence_grade_rationale,
                    "primary_incremental": retained_result.primary_split.incremental,
                    "parent_relationship": retained_result.parent_relationship,
                    "independent_confirmation_required": (
                        retained_result.independent_confirmation_required
                    ),
                    "independent_confirmation_reasons": (
                        retained_result.independent_confirmation_reasons
                    ),
                    "knowledge_history": retained_result.knowledge_history,
                    "authority": "NONE",
                }
            )
            return

        raise SystemExit(f"unsupported corporate-followup command {command!r}")
    finally:
        engine.dispose()


def corporate_market_research_command(arguments: argparse.Namespace) -> None:
    """Bounded command surface for Cross-Market Research v3: Fundamental
    and Market Evidence Integration with Forward Options Seam. Controlled
    hypothesis research only -- never strategy generation, never
    execution. ``authority`` stays ``NONE`` throughout, and every command
    here makes zero provider requests (the shared corporate/equity/replay
    evidence it reads was already retained by earlier, separately
    governed capture milestones)."""

    from apex.adapters.corporate_fundamentals_postgres import (
        PostgresCorporateFundamentalsRepository,
    )
    from apex.adapters.corporate_market_research_postgres import (
        PostgresCorporateMarketResearchRepository,
    )
    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.corporate_market_research import (
        AdvanceCorporateAgendaRun,
        BuildCorporatePanel,
        RegisterCorporateAgendaRun,
        RegisterCorporateMarketResearchAgenda,
        RegisterCorporateMarketResearchAgendaCommand,
        build_trial_families,
        classify_hypothesis_calibration,
        compute_contribution_evidence,
        compute_incremental_contribution,
        describe_options_forward_seam,
        reconstruct_hypothesis_outcome,
        split_train_validation_holdout,
    )
    from apex.domain.corporate_fundamentals import count_ambiguous_fact_groups
    from apex.domain.corporate_market_research import corporate_market_research_warnings
    from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresCorporateMarketResearchRepository(engine)
    corporate_repository = PostgresCorporateFundamentalsRepository(engine)
    methodology_repository = PostgresResearchMethodologyRepository(engine)
    try:
        now = datetime.now(tz=UTC)
        command = arguments.command

        def _resolve_agenda() -> Any:
            agenda_id = getattr(arguments, "agenda_id", None)
            agenda = (
                repository.agenda_by_id(agenda_id)
                if agenda_id
                else (repository.list_agendas() or (None,))[-1]
            )
            if agenda is None:
                raise SystemExit(
                    "no retained corporate-market research agenda -- run "
                    "corporate-market-agenda-create first"
                )
            return agenda

        if command == "corporate-market-agenda-create":
            dataset = (
                corporate_repository.dataset_snapshot_by_id(arguments.corporate_dataset_id)
                if arguments.corporate_dataset_id
                else corporate_repository.latest_dataset_snapshot(arguments.plan_id)
                if arguments.plan_id
                else None
            )
            if dataset is None:
                raise SystemExit(
                    "no retained corporate history dataset snapshot -- pass "
                    "--corporate-dataset-id or --plan-id, or run corporate-dataset-materialize"
                )
            agenda = RegisterCorporateMarketResearchAgenda(repository=repository).execute(
                RegisterCorporateMarketResearchAgendaCommand(
                    corporate_dataset_id=dataset.dataset_id,
                    equity_snapshot_id=arguments.equity_snapshot_id,
                    replay_run_id=arguments.replay_run_id,
                    cross_market_dataset_id=arguments.cross_market_dataset_id,
                    cohort_policy_id=(
                        arguments.cohort_policy_id or GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id
                    ),
                    lookback_years=dataset.lookback_years,
                    registered_at=parsed(arguments.created_at),
                )
            )
            _research_evidence_json(
                {
                    "command": command,
                    "agenda": agenda,
                    "warnings": corporate_market_research_warnings(),
                }
            )
            return

        if command == "corporate-market-agenda-inspect":
            agenda = _resolve_agenda()
            agenda_hypotheses = repository.hypotheses_for_agenda(agenda.agenda_id)
            run = repository.agenda_run_by_agenda(agenda.agenda_id)
            quality_report = repository.quality_report_by_agenda(agenda.agenda_id)
            _research_evidence_json(
                {
                    "command": command,
                    "agenda": agenda,
                    "hypothesis_count": len(agenda_hypotheses),
                    "genuine_hypothesis_count": sum(
                        1 for h in agenda_hypotheses if not h.is_negative_control
                    ),
                    "negative_control_count": sum(
                        1 for h in agenda_hypotheses if h.is_negative_control
                    ),
                    "agenda_run": run,
                    "quality_report": quality_report,
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-market-hypotheses-list":
            agenda = _resolve_agenda()
            agenda_hypotheses = repository.hypotheses_for_agenda(agenda.agenda_id)
            hypothesis_families: dict[str, int] = {}
            for h in agenda_hypotheses:
                hypothesis_families[h.family.value] = hypothesis_families.get(h.family.value, 0) + 1
            _research_evidence_json(
                {
                    "command": command,
                    "agenda_id": agenda.agenda_id,
                    "hypotheses": agenda_hypotheses,
                    "families": tuple(sorted(hypothesis_families.items())),
                    "negative_control_count": sum(
                        1 for h in agenda_hypotheses if h.is_negative_control
                    ),
                }
            )
            return

        if command in ("corporate-market-agenda-run-start", "corporate-market-agenda-run-resume"):
            agenda = _resolve_agenda()
            created_at = parsed(arguments.created_at)
            if command == "corporate-market-agenda-run-start":
                RegisterCorporateAgendaRun(repository=repository).execute(
                    agenda=agenda, created_at=created_at
                )
            panel_builder = BuildCorporatePanel(
                corporate_repository=corporate_repository,
                historical_repository=PostgresHistoricalMarketDataRepository(engine),
                evidence=PostgresSecuritiesRepository(engine),
                replay_repository=PostgresHistoricalReplayRepository(engine),
            )
            advancer = AdvanceCorporateAgendaRun(
                repository=repository,
                methodology=methodology_repository,
                panel_builder=panel_builder,
            )
            steps = getattr(arguments, "steps", None) or 50
            last_result = None
            for _step in range(steps):
                last_result = advancer.execute(agenda=agenda, created_at=created_at)
                if last_result.run.status.value == "COMPLETE":
                    break
            if last_result is None:
                raise SystemExit("CORPORATE_MARKET_AGENDA_RUN_MADE_NO_PROGRESS")
            _research_evidence_json(
                {
                    "command": command,
                    "agenda_id": agenda.agenda_id,
                    "agenda_run": last_result.run,
                    "hypotheses_completed": len(last_result.run.completed_hypothesis_ids),
                    "hypotheses_total": len(last_result.run.hypothesis_ids),
                    "quality_report": last_result.quality_report,
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-market-agenda-run-inspect":
            agenda = _resolve_agenda()
            run = repository.agenda_run_by_agenda(agenda.agenda_id)
            if run is None:
                raise SystemExit("CORPORATE_MARKET_AGENDA_RUN_NOT_REGISTERED")
            executions = repository.executions_for_agenda(agenda.agenda_id)
            _research_evidence_json(
                {
                    "command": command,
                    "agenda_id": agenda.agenda_id,
                    "agenda_run": run,
                    "executions_completed": len(executions),
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-market-knowledge-inspect":
            hypothesis_id = getattr(arguments, "hypothesis_id", None)
            entries = (
                repository.knowledge_entries_for_hypothesis(hypothesis_id)
                if hypothesis_id
                else repository.list_knowledge_entries()
            )
            status_counts: dict[str, int] = {}
            for entry in entries:
                status_counts[entry.status.value] = status_counts.get(entry.status.value, 0) + 1
            _research_evidence_json(
                {
                    "command": command,
                    "hypothesis_id": hypothesis_id,
                    "entry_count": len(entries),
                    "entries": entries,
                    "status_distribution": tuple(sorted(status_counts.items())),
                    "contradictions": repository.list_contradictions(),
                }
            )
            return

        if command == "corporate-market-quality-inspect":
            agenda = _resolve_agenda()
            report = repository.quality_report_by_agenda(agenda.agenda_id)
            if report is None:
                raise SystemExit("no retained quality report -- run the agenda to completion first")
            hypotheses_by_id = {
                h.hypothesis_id: h for h in repository.hypotheses_for_agenda(agenda.agenda_id)
            }
            executions = repository.executions_for_agenda(agenda.agenda_id)
            panel = BuildCorporatePanel(
                corporate_repository=corporate_repository,
                historical_repository=PostgresHistoricalMarketDataRepository(engine),
                evidence=PostgresSecuritiesRepository(engine),
                replay_repository=PostgresHistoricalReplayRepository(engine),
            ).execute(
                corporate_dataset_id=agenda.corporate_dataset_id,
                equity_snapshot_id=agenda.equity_snapshot_id,
                replay_run_id=agenda.replay_run_id,
                cross_market_dataset_id=agenda.cross_market_dataset_id,
            )
            period_policy = split_train_validation_holdout(
                panel.monthly_dates, replay_run_id=agenda.replay_run_id
            )
            outcomes = [
                outcome
                for execution in executions
                if (matched_hypothesis := hypotheses_by_id.get(execution.hypothesis_id)) is not None
                and (
                    outcome := reconstruct_hypothesis_outcome(
                        execution, matched_hypothesis, methodology_repository
                    )
                )
                is not None
            ]
            trial_families = build_trial_families(outcomes, panel=panel, created_at=now)
            saved_trial_families = {
                trial_family_key: methodology_repository.trial_family_by_id(
                    candidate.trial_family_id
                )
                for trial_family_key, candidate in trial_families.items()
            }
            resolved_trial_families = {
                k: v for k, v in saved_trial_families.items() if v is not None
            }
            classifications = classify_hypothesis_calibration(
                outcomes, resolved_trial_families, agenda_id=agenda.agenda_id, created_at=now
            )
            grade_counts: dict[str, int] = {}
            insufficient_strata = 0
            for classification in classifications:
                grade_counts[classification.evidence_grade_v2.value] = (
                    grade_counts.get(classification.evidence_grade_v2.value, 0) + 1
                )
                if not classification.matched.sufficient:
                    insufficient_strata += 1
            incremental_results = []
            contribution_evidence = []
            if period_policy is not None:
                for outcome in outcomes:
                    incremental = compute_incremental_contribution(
                        outcome.hypothesis, panel=panel, period_policy=period_policy
                    )
                    if incremental is not None:
                        incremental_results.append(incremental)
                    contribution_evidence.append(
                        compute_contribution_evidence(outcome, panel=panel, incremental=incremental)
                    )
            _research_evidence_json(
                {
                    "command": command,
                    "agenda_id": agenda.agenda_id,
                    "quality_report": report,
                    "hypothesis_count": len(hypotheses_by_id),
                    "calibration_classifications": tuple(classifications),
                    "calibration_evidence_grade_distribution": tuple(sorted(grade_counts.items())),
                    "calibration_insufficient_stratum_count": insufficient_strata,
                    "incremental_contribution_results": tuple(incremental_results),
                    "corporate_contribution_evidence": tuple(contribution_evidence),
                    "excluded_low_quality_security_ids": panel.excluded_low_quality_security_ids,
                    "options_forward_seam": describe_options_forward_seam(
                        reader=None, plan_id=None
                    ),
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-market-panel-diagnostics-inspect":
            # Corporate Research Panel Determinism v1, section 15: bounded
            # operator diagnostics only -- reuses the existing panel
            # builder, fingerprint, and fact-selection primitives rather
            # than adding a new dashboard or a parallel identity system.
            agenda = _resolve_agenda()
            panel = BuildCorporatePanel(
                corporate_repository=corporate_repository,
                historical_repository=PostgresHistoricalMarketDataRepository(engine),
                evidence=PostgresSecuritiesRepository(engine),
                replay_repository=PostgresHistoricalReplayRepository(engine),
            ).execute(
                corporate_dataset_id=agenda.corporate_dataset_id,
                equity_snapshot_id=agenda.equity_snapshot_id,
                replay_run_id=agenda.replay_run_id,
                cross_market_dataset_id=agenda.cross_market_dataset_id,
            )
            observation_count = sum(len(v) for v in panel.features_by_security.values())
            all_facts = corporate_repository.facts_for_securities(panel.usable_security_ids)
            _research_evidence_json(
                {
                    "command": command,
                    "agenda_id": agenda.agenda_id,
                    "corporate_dataset_id": panel.corporate_dataset_id,
                    "replay_run_id": panel.replay_run_id,
                    "cross_market_dataset_id": panel.cross_market_dataset_id,
                    "panel_fingerprint": panel.fingerprint(),
                    "usable_security_count": len(panel.usable_security_ids),
                    "excluded_low_quality_security_count": len(
                        panel.excluded_low_quality_security_ids
                    ),
                    "feature_observation_count": observation_count,
                    "retained_fact_count": len(all_facts),
                    "ambiguous_fact_selection_group_count": count_ambiguous_fact_groups(all_facts),
                    "warnings": panel.warnings,
                    "authority": "NONE",
                }
            )
            return

        if command == "corporate-market-followups-list":
            follow_ups = repository.list_follow_ups()
            kinds: dict[str, int] = {}
            for follow_up in follow_ups:
                kinds[follow_up.kind.value] = kinds.get(follow_up.kind.value, 0) + 1
            _research_evidence_json(
                {
                    "command": command,
                    "follow_up_count": len(follow_ups),
                    "follow_ups": follow_ups,
                    "kinds": tuple(sorted(kinds.items())),
                }
            )
            return

        raise SystemExit(f"unsupported corporate-market-research command {command!r}")
    finally:
        engine.dispose()


def _security_identity_runtime() -> tuple[Any, Any, Any, Any, Any]:
    """Engine plus the durable repositories the stable-identity command
    family composes: research evidence, legacy security reference, options
    intelligence, and the new stable-identity/resolution/relink repository."""
    from apex.adapters.options_intelligence_postgres import (
        PostgresOptionsIntelligenceRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_evidence_postgres import PostgresResearchEvidenceRepository
    from apex.adapters.security_identity_postgres import PostgresSecurityIdentityRepository
    from apex.adapters.security_reference_postgres import PostgresSecurityReferenceRepository

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        engine,
        PostgresResearchEvidenceRepository(engine),
        PostgresSecurityReferenceRepository(engine),
        PostgresOptionsIntelligenceRepository(engine),
        PostgresSecurityIdentityRepository(engine),
    )


def _reconciled_latest_securities(research_repo: Any) -> tuple[Any, ...]:
    """Recompute the latest reconciled security set from already-retained
    Nasdaq/SEC captures only -- the identical zero-network-request
    recomputation ``broad-equity-universe-create`` already performs, reused
    here so stable-identity commands never require their own capture step.
    """
    from apex.application.research_evidence import (
        NASDAQ_PROVIDER,
        SEC_PROVIDER,
        security_from_observation,
    )

    nasdaq_capture = _latest_completed_capture(
        research_repo,
        NASDAQ_PROVIDER,
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    )
    nasdaq_other_capture = _latest_completed_capture(
        research_repo,
        NASDAQ_PROVIDER,
        source_locator="https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    )
    sec_capture = _latest_completed_capture(research_repo, SEC_PROVIDER)
    captures = tuple(
        item for item in (nasdaq_capture, nasdaq_other_capture, sec_capture) if item is not None
    )
    if not captures:
        raise SystemExit(
            "no completed Nasdaq or SEC capture is retained; "
            "run broad-equity-reference-capture first"
        )
    records = tuple(
        security_from_observation(observation)
        for capture in captures
        for observation in research_repo.observations(capture.capture_id)
    )
    return records, captures


def security_identity_command(arguments: argparse.Namespace) -> None:
    """Durable, repository-backed stable-security-identity command surface.

    Builds and relinks entirely from already-retained genuine evidence: no
    branch here performs a Nasdaq/SEC/options-directory network request.
    """
    from apex.application.research_evidence import (
        NASDAQ_OPTIONS_PROVIDER,
        latest_completed_capture,
        option_underlying_rejection_from_observation,
    )
    from apex.application.security_identity import (
        BuildStableIdentityProjection,
        RelinkOptionContracts,
        ResolveLegacyIdentities,
    )
    from apex.domain.options_intelligence import UnderlyingLinkOutcome
    from apex.domain.security_reference import (
        CurrentEquityUniversePolicy,
        reconcile_security_references,
    )

    engine, research_repo, security_repo, options_repo, identity_repo = _security_identity_runtime()
    try:
        now = datetime.now(tz=UTC)
        command = arguments.command

        if command == "security-identity-projection-build":
            records, captures = _reconciled_latest_securities(research_repo)
            reconciled = reconcile_security_references(records)
            projection = BuildStableIdentityProjection(
                security_repository=security_repo, identity_repository=identity_repo
            ).execute(
                securities=reconciled,
                policy=CurrentEquityUniversePolicy.initial(),
                source_receipt_ids=tuple(sorted({item.source_receipt_id for item in records})),
                raw_source_record_count=len(records),
                as_of_date=now.date(),
                created_at=now,
                idempotency_key=arguments.idempotency_key,
            )
            legacy = ResolveLegacyIdentities(
                security_repository=security_repo, identity_repository=identity_repo
            ).execute(latest_securities=reconciled, recorded_at=now)
            _research_evidence_json(
                {
                    "command": command,
                    "universe_id": projection.universe.universe_id,
                    "source_capture_ids": sorted(item.capture_id for item in captures),
                    "raw_source_record_count": projection.raw_source_record_count,
                    "prior_legacy_identity_count": projection.prior_legacy_identity_count,
                    "stable_identity_count": projection.stable_identity_count,
                    "total_considered": projection.universe.total_considered,
                    "research_eligible_count": projection.universe.research_eligible_count,
                    "superseded_count": projection.superseded_count,
                    "same_run_ambiguous_count": projection.ambiguous_count,
                    "review_required_count": projection.review_required_count,
                    "security_type_counts": projection.universe.security_type_counts,
                    "legacy_identity_count": legacy.legacy_identity_count,
                    "legacy_current_count": legacy.current_count,
                    "legacy_alias_count": legacy.alias_count,
                    "legacy_ambiguous_count": legacy.ambiguous_count,
                }
            )
            return

        if command == "security-identity-report":
            latest_universe = None
            for evidence in security_repo.universe_evidence():
                if evidence.snapshot is not None:
                    latest_universe = evidence.snapshot
            _research_evidence_json(
                {
                    "command": command,
                    "latest_universe_id": (
                        latest_universe.universe_id if latest_universe is not None else None
                    ),
                    "stable_identity_count": identity_repo.stable_identity_count(),
                    "resolution_count": identity_repo.resolution_count(),
                    "resolution_state_counts": identity_repo.resolution_state_counts(),
                    "latest_option_linkage_run_id": identity_repo.latest_option_linkage_run_id(),
                    "option_linkage_run_ids": identity_repo.option_linkage_run_ids(),
                }
            )
            return

        if command == "security-identity-ambiguous":
            limit = max(0, arguments.limit)
            _research_evidence_json(
                {
                    "command": command,
                    "limit": limit,
                    "ambiguous": identity_repo.ambiguous_resolutions(limit=limit),
                }
            )
            return

        if command == "security-identity-resolve":
            direct = identity_repo.resolutions_for_old_id(arguments.id)
            reverse = identity_repo.resolutions_to_stable_id(arguments.id)
            _research_evidence_json(
                {
                    "command": command,
                    "id": arguments.id,
                    "as_old_identity": direct,
                    "as_resolved_stable_identity": reverse,
                }
            )
            return

        if command == "security-identity-options-relink":
            latest_universe = None
            for evidence in security_repo.universe_evidence():
                if evidence.snapshot is not None:
                    latest_universe = evidence.snapshot
            if latest_universe is None:
                raise SystemExit(
                    "no stable universe is retained; run security-identity-projection-build first"
                )
            stable_symbol_index = {
                decision.current_symbol: decision.canonical_security_id
                for decision in latest_universe.decisions
            }
            options_capture = latest_completed_capture(
                research_repo.captures(), NASDAQ_OPTIONS_PROVIDER
            )
            previously_rejected: tuple[tuple[str, int], ...] = ()
            if options_capture is not None:
                rejections = tuple(
                    option_underlying_rejection_from_observation(item)
                    for item in research_repo.observations(options_capture.capture_id)
                )
                previously_rejected = tuple(
                    (item.raw_underlying_symbol, item.row_count)
                    for item in rejections
                    if item.outcome is not UnderlyingLinkOutcome.LINKED
                )
            contracts = options_repo.contracts()
            linkages = RelinkOptionContracts(identity_repository=identity_repo).execute(
                contracts=contracts,
                previously_rejected=previously_rejected,
                stable_symbol_index=stable_symbol_index,
                linkage_run_id=arguments.linkage_run_id,
                recorded_at=now,
            )
            linked = sum(1 for item in linkages if item.outcome is UnderlyingLinkOutcome.LINKED)
            unlinked = sum(1 for item in linkages if item.outcome is UnderlyingLinkOutcome.UNLINKED)
            ambiguous = sum(
                1 for item in linkages if item.outcome is UnderlyingLinkOutcome.AMBIGUOUS
            )
            _research_evidence_json(
                {
                    "command": command,
                    "linkage_run_id": arguments.linkage_run_id,
                    "universe_id": latest_universe.universe_id,
                    "contracts_considered": len(contracts),
                    "previously_rejected_symbols_retried": len(previously_rejected),
                    "distinct_raw_symbols": len(linkages),
                    "linked": linked,
                    "unlinked": unlinked,
                    "ambiguous": ambiguous,
                }
            )
            return

        if command == "security-identity-options-linkage-report":
            run_id = arguments.linkage_run_id or identity_repo.latest_option_linkage_run_id()
            if run_id is None:
                raise SystemExit("no options stable-linkage run is retained")
            linkages = identity_repo.option_linkages_for_run(run_id)
            unresolved = tuple(
                item for item in linkages if item.outcome is not UnderlyingLinkOutcome.LINKED
            )
            _research_evidence_json(
                {
                    "command": command,
                    "linkage_run_id": run_id,
                    "distinct_raw_symbols": len(linkages),
                    "linked": sum(
                        1 for item in linkages if item.outcome is UnderlyingLinkOutcome.LINKED
                    ),
                    "unlinked": sum(
                        1 for item in linkages if item.outcome is UnderlyingLinkOutcome.UNLINKED
                    ),
                    "ambiguous": sum(
                        1 for item in linkages if item.outcome is UnderlyingLinkOutcome.AMBIGUOUS
                    ),
                    "unresolved": unresolved,
                }
            )
            return

        raise SystemExit(f"unsupported security-identity command {command!r}")
    finally:
        engine.dispose()


def strategy_lab_demo() -> None:
    from apex.adapters.strategy_lab_synthetic import run_strategy_lab_demo

    _strategy_lab_json(
        run_strategy_lab_demo(
            artifact_root=ROOT / ".local" / "strategy_lab" / "artifacts" / "demo-v1"
        )
    )


def strategy_lab_readiness() -> None:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.strategy_lab_event_markets import EventMarketsStrategyLabAdapter

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        adapter = EventMarketsStrategyLabAdapter(engine)
        adapter.rows(cutoff_at=datetime.now(tz=UTC))
        readiness = adapter.readiness
        _strategy_lab_json(
            {
                "warning": "RESEARCH MODEL — NO STRATEGY OR CAPITAL AUTHORITY",
                "total_candidate_source_rows": readiness.total_candidate_source_rows,
                "resolved_rows": readiness.resolved_rows,
                "configuration_complete_rows": readiness.configuration_complete_rows,
                "distinct_events": readiness.distinct_events,
                "distinct_markets": readiness.distinct_markets,
                "rows_with_all_required_features": readiness.rows_with_all_required_features,
                "rows_excluded_for_unresolved_outcomes": readiness.exclusions.get(
                    "UNRESOLVED_OUTCOME", 0
                ),
                "rows_excluded_for_plan_configuration_mismatch": readiness.exclusions.get(
                    "PLAN_CONFIGURATION_MISMATCH", 0
                ),
                "rows_excluded_for_missing_feature_lineage": readiness.exclusions.get(
                    "MISSING_REQUIRED_FEATURE_OR_LINEAGE", 0
                ),
                "rows_excluded_for_missing_cost_evidence": readiness.exclusions.get(
                    "MISSING_COST_EVIDENCE", 0
                ),
                "excluded": readiness.exclusions,
                "integrity_warnings": readiness.integrity_warnings,
                "minimum_training_ready": readiness.rows_with_all_required_features >= 72
                and readiness.distinct_events >= 20,
                "provider_calls": 0,
            }
        )
    finally:
        engine.dispose()


def strategy_lab_configuration_readiness() -> None:
    """Report retained rows by exact configuration without choosing one."""
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        normalized_market_snapshot,
        shadow_prediction,
    )
    from apex.adapters.strategy_lab_event_markets import EventMarketsStrategyLabAdapter
    from apex.domain.values import content_digest

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        cutoff = datetime.now(tz=UTC)
        with engine.connect() as connection:
            source = tuple(
                connection.execute(
                    sa.select(
                        shadow_prediction.c.strategy_id,
                        shadow_prediction.c.strategy_version,
                        shadow_prediction.c.model_configuration_version,
                        shadow_prediction.c.state,
                        shadow_prediction.c.event_id,
                        normalized_market_snapshot.c.provider_market_ticker,
                    )
                    .outerjoin(
                        normalized_market_snapshot,
                        shadow_prediction.c.market_evidence_id
                        == normalized_market_snapshot.c.snapshot_id,
                    )
                    .where(shadow_prediction.c.decision_at <= cutoff)
                ).mappings()
            )
        grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for item in source:
            grouped[
                (
                    str(item["strategy_id"]),
                    str(item["strategy_version"]),
                    str(item["model_configuration_version"]),
                )
            ].append(item)
        scopes: list[dict[str, object]] = []
        for (strategy_id, implementation_version, model_configuration_version), values in sorted(
            grouped.items()
        ):
            configuration_hash = content_digest(
                {
                    "strategy_id": strategy_id,
                    "strategy_version": implementation_version,
                    "model_configuration_version": model_configuration_version,
                }
            )
            adapter = EventMarketsStrategyLabAdapter(
                engine, strategy_id=strategy_id, strategy_version=implementation_version
            )
            rows = adapter.rows(cutoff_at=cutoff, source_scope=configuration_hash)
            readiness = adapter.readiness
            exclusions = dict(readiness.exclusions)
            source_rows = len(values)
            resolved_rows = sum(item["state"] == "RESOLVED" for item in values)
            scopes.append(
                {
                    "source_strategy_id": strategy_id,
                    "implementation_version": implementation_version,
                    "configuration_hash": configuration_hash,
                    "feature_schema_hash": adapter.schema.canonical_hash,
                    "source_rows": source_rows,
                    "resolved_rows": resolved_rows,
                    "configuration_complete_rows": len(rows),
                    "feature_complete_rows": readiness.rows_with_all_required_features,
                    "distinct_events": len(
                        {str(item["event_id"]) for item in values if item["event_id"]}
                    ),
                    "distinct_markets": len(
                        {
                            str(item["provider_market_ticker"])
                            for item in values
                            if item["provider_market_ticker"]
                        }
                    ),
                    "fold_eligible": len(rows) >= 72
                    and len({row.grouping_id for row in rows}) >= 20,
                    "exclusion_reasons": exclusions,
                    "integrity_warnings": readiness.integrity_warnings,
                    "readiness_state": "ELIGIBLE_FOR_GROUPED_WALK_FORWARD"
                    if len(rows) >= 72 and len({row.grouping_id for row in rows}) >= 20
                    else "INSUFFICIENT_RETAINED_EVIDENCE",
                }
            )
        _strategy_lab_json(
            {
                "warning": "RESEARCH MODEL — NO STRATEGY OR CAPITAL AUTHORITY",
                "cutoff_at": cutoff,
                "scopes": tuple(scopes),
                "automatic_selection": False,
                "provider_calls": 0,
            }
        )
    finally:
        engine.dispose()


def strategy_lab_shadow_demo() -> None:
    from apex.adapters.strategy_lab_shadow_synthetic import run_strategy_lab_shadow_demo

    _strategy_lab_json(
        run_strategy_lab_shadow_demo(
            artifact_root=ROOT / ".local" / "strategy_lab" / "artifacts" / "shadow-demo-v1"
        )
    )


def strategy_lab_request_shadow_review(
    *, candidate_id: str, reason: str, idempotency_key: str
) -> None:
    service, engine = _strategy_lab_shadow_service()
    try:
        _strategy_lab_json(
            service.request_review(
                candidate_id=candidate_id, reason=reason, idempotency_key=idempotency_key
            )
        )
    finally:
        engine.dispose()


def strategy_lab_approve_shadow_deployment(
    *,
    review_id: str,
    role: str,
    candidate_id: str,
    dataset_id: str,
    artifact_sha256: str,
    evaluation_id: str,
    reason: str,
    idempotency_key: str,
) -> None:
    from apex.application.strategy_lab_shadow import DeploymentRole

    service, engine = _strategy_lab_shadow_service()
    try:
        _strategy_lab_json(
            service.approve_review(
                review_id=review_id,
                role=DeploymentRole(role),
                candidate_id=candidate_id,
                dataset_id=dataset_id,
                artifact_sha256=artifact_sha256,
                evaluation_id=evaluation_id,
                reason=reason,
                idempotency_key=idempotency_key,
            )
        )
    finally:
        engine.dispose()


def strategy_lab_run_shadow(
    *, deployment_id: str, adapter: str, cutoff: str, maximum_rows: int, idempotency_key: str
) -> None:
    from apex.adapters.strategy_lab_event_markets import EventMarketsForwardFeatureAdapter

    if adapter != "event-market-retained-v1":
        raise SystemExit("only event-market-retained-v1 is supported")
    service, engine = _strategy_lab_shadow_service()
    try:
        _strategy_lab_json(
            service.run_inference(
                deployment_id=deployment_id,
                adapter=EventMarketsForwardFeatureAdapter(engine),
                cutoff_at=_strategy_lab_cutoff(cutoff),
                maximum_rows=maximum_rows,
                idempotency_key=idempotency_key,
            )
        )
    finally:
        engine.dispose()


def strategy_lab_resolve_shadow(*, deployment_id: str, cutoff: str) -> None:
    from apex.adapters.strategy_lab_event_markets import EventMarketsForwardFeatureAdapter

    service, engine = _strategy_lab_shadow_service()
    try:
        _strategy_lab_json(
            {
                "resolved": service.resolve_predictions(
                    deployment_id=deployment_id,
                    adapter=EventMarketsForwardFeatureAdapter(engine),
                    cutoff_at=_strategy_lab_cutoff(cutoff),
                )
            }
        )
    finally:
        engine.dispose()


def strategy_lab_evaluate_challenger(*, deployment_id: str, cutoff: str) -> None:
    service, engine = _strategy_lab_shadow_service()
    try:
        _strategy_lab_json(
            service.evaluate_challenger(
                deployment_id=deployment_id, cutoff_at=_strategy_lab_cutoff(cutoff)
            )
        )
    finally:
        engine.dispose()


def strategy_lab_assign_champion(
    *, deployment_id: str, reason: str, confirm_deployment: str, idempotency_key: str
) -> None:
    service, engine = _strategy_lab_shadow_service()
    try:
        _strategy_lab_json(
            service.assign_champion(
                deployment_id=deployment_id,
                reason=reason,
                confirm_deployment=confirm_deployment,
                idempotency_key=idempotency_key,
            )
        )
    finally:
        engine.dispose()


def strategy_lab_build_dataset(
    *,
    name: str,
    cutoff: str,
    idempotency_key: str,
    configuration_hash: str | None,
    strategy_id: str | None,
    strategy_version: str | None,
) -> None:
    from apex.adapters.strategy_lab_event_markets import EventMarketsStrategyLabAdapter

    service, engine = _strategy_lab_service()
    try:
        dataset = service.build_dataset(
            adapter=EventMarketsStrategyLabAdapter(
                engine, strategy_id=strategy_id, strategy_version=strategy_version
            ),
            name=name,
            cutoff_at=_strategy_lab_cutoff(cutoff),
            idempotency_key=idempotency_key,
            source_scope=configuration_hash,
        )
        if dataset.artifact is None:
            raise RuntimeError("strategy lab dataset did not produce an artifact")
        _strategy_lab_json(
            {
                "dataset_id": dataset.dataset_id,
                "state": dataset.state.value,
                "rows": dataset.row_count,
                "groups": dataset.grouping_count,
                "artifact_sha256": dataset.artifact.sha256,
            }
        )
    finally:
        engine.dispose()


def strategy_lab_train(
    *,
    dataset_id: str,
    model_family: str,
    calibration: str,
    seed: int,
    idempotency_key: str,
    hyperparameters_json: str,
) -> None:
    from apex.application.strategy_lab import CalibrationMethod, ModelFamily

    try:
        hyperparameters = json.loads(hyperparameters_json)
    except json.JSONDecodeError as error:
        raise SystemExit("hyperparameters JSON must be an object") from error
    if not isinstance(hyperparameters, dict):
        raise SystemExit("hyperparameters JSON must be an object")
    service, engine = _strategy_lab_service()
    try:
        run = service.train_model(
            dataset_id=dataset_id,
            family=ModelFamily(model_family),
            hyperparameters=hyperparameters,
            calibration=CalibrationMethod(calibration),
            seed=seed,
            idempotency_key=idempotency_key,
        )
        _strategy_lab_json(
            {
                "training_run_id": run.training_run_id,
                "state": run.state.value,
                "model_artifact_sha256": run.model_artifact.sha256 if run.model_artifact else None,
            }
        )
    finally:
        engine.dispose()


def strategy_lab_evaluate(*, training_run_id: str) -> None:
    service, engine = _strategy_lab_service()
    try:
        evaluation, candidate = service.evaluate_candidate(training_run_id=training_run_id)
        _strategy_lab_json(
            {
                "evaluation_id": evaluation.evaluation_id,
                "evaluation_state": evaluation.state.value,
                "candidate_id": candidate.candidate_id,
                "candidate_state": candidate.state.value,
                "rejection_reasons": candidate.rejection_reasons,
            }
        )
    finally:
        engine.dispose()


def strategy_lab_report(*, candidate_id: str) -> None:
    service, engine = _strategy_lab_service()
    try:
        _strategy_lab_json(service.candidate_report(candidate_id))
    finally:
        engine.dispose()


def _securities_repository() -> tuple[Any, Any, Engine]:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        PostgresSecuritiesRepository(engine),
        LocalSecuritiesDatasetAdapter(),
        engine,
    )


def _ensure_securities_baselines(repository: Any) -> None:
    from apex.application.securities import initial_securities_strategy_specifications

    for specification in initial_securities_strategy_specifications(
        datetime(2026, 1, 1, tzinfo=UTC)
    ):
        repository.save_specification(specification)


def securities_import_data(*, managed_source: str, dataset_name: str, idempotency_key: str) -> None:
    from apex.application.securities import ImportSecuritiesData, ImportSecuritiesDataCommand

    repository, data, engine = _securities_repository()
    try:
        batch = ImportSecuritiesData(repository=repository, market_data=data).execute(
            ImportSecuritiesDataCommand(
                managed_source=managed_source,
                dataset_name=dataset_name,
                idempotency_key=idempotency_key,
                imported_at=datetime.now(tz=UTC),
            )
        )
        _strategy_lab_json(
            {
                "warning": "SECURITIES SHADOW — NO CAPITAL RESERVED",
                "batch": batch,
            }
        )
    finally:
        engine.dispose()


def _securities_research_command(
    *,
    campaign_id: str,
    strategy_id: str,
    data_batch_id: str,
    start: str,
    end: str,
    idempotency_key: str,
    model_signal_builder: SecuritiesModelSignalBuilder | None = None,
) -> object:
    from apex.adapters.securities_postgres import PostgresSecuritiesCampaignBoundary
    from apex.application.securities import RunSecuritiesResearch, RunSecuritiesResearchCommand

    repository, data, engine = _securities_repository()
    try:
        _ensure_securities_baselines(repository)
        instruments, _sessions, _bars, _actions = repository.get_dataset(data_batch_id)
        universe = tuple(item.instrument_id for item in instruments)
        if not universe:
            raise SystemExit("securities data batch has no instruments")
        service = RunSecuritiesResearch(
            repository=repository,
            campaigns=PostgresSecuritiesCampaignBoundary(engine),
            market_data=data,
            model_signal_builder=model_signal_builder,
        )
        run = service.execute(
            RunSecuritiesResearchCommand(
                campaign_id=campaign_id,
                strategy_id=strategy_id,
                data_batch_id=data_batch_id,
                universe=universe,
                start_date=start,
                end_date=end,
                decision_cutoff=datetime.now(tz=UTC),
                benchmark_instrument_id=universe[0],
                idempotency_key=idempotency_key,
            )
        )
        _strategy_lab_json(
            {
                "warning": "SECURITIES SHADOW — NO CAPITAL RESERVED",
                "research_run": run,
            }
        )
        return run
    finally:
        engine.dispose()


def securities_research(
    *,
    campaign_id: str,
    strategy_id: str,
    data_batch_id: str,
    start: str,
    end: str,
    idempotency_key: str,
) -> None:
    _securities_research_command(
        campaign_id=campaign_id,
        strategy_id=strategy_id,
        data_batch_id=data_batch_id,
        start=start,
        end=end,
        idempotency_key=idempotency_key,
    )


def securities_strategy_lab_research(
    *,
    campaign_id: str,
    deployment_id: str,
    data_batch_id: str,
    start: str,
    end: str,
    idempotency_key: str,
) -> None:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_strategy_lab import StrategyLabSecuritiesSignalBuilder
    from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
    from apex.adapters.strategy_lab_postgres import PostgresStrategyLabShadowStore
    from apex.adapters.strategy_lab_shadow_models import StrategyLabManagedModelLoader
    from apex.domain.securities import SecuritiesStrategyKind, SecuritiesStrategySpecification

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        deployment = PostgresStrategyLabShadowStore(engine).deployment(deployment_id)
    finally:
        engine.dispose()
    if deployment is None:
        raise SystemExit("Strategy Lab deployment was not found")
    repository, _data, repository_engine = _securities_repository()
    try:
        specification = SecuritiesStrategySpecification.create(
            strategy_id=f"securities-strategy-lab-{deployment.deployment_id}",
            kind=SecuritiesStrategyKind.STRATEGY_LAB_MODEL,
            version="1",
            lookback_sessions=20,
            rebalance_every_sessions=1,
            strategy_lab_deployment_id=deployment.deployment_id,
            created_at=datetime.now(tz=UTC),
        )
        repository.save_specification(specification)
    finally:
        repository_engine.dispose()
    _securities_research_command(
        campaign_id=campaign_id,
        strategy_id=specification.strategy_id,
        data_batch_id=data_batch_id,
        start=start,
        end=end,
        idempotency_key=idempotency_key,
        model_signal_builder=StrategyLabSecuritiesSignalBuilder(
            deployment=deployment,
            artifact_store=LocalStrategyLabArtifactStore(),
            model_loader=StrategyLabManagedModelLoader(),
        ),
    )


def securities_conformance(*, research_run_id: str, external_engine: str) -> None:
    from apex.adapters.securities_external import LeanSecuritiesEngineAdapter
    from apex.application.securities import (
        RunSecuritiesConformance,
        RunSecuritiesConformanceCommand,
    )

    repository, _data, engine = _securities_repository()
    try:
        report = RunSecuritiesConformance(
            repository=repository,
            engines=(LeanSecuritiesEngineAdapter(),),
        ).execute(
            RunSecuritiesConformanceCommand(
                research_run_id=research_run_id,
                external_engine=external_engine,
                created_at=datetime.now(tz=UTC),
            )
        )
        _strategy_lab_json(
            {
                "warning": "EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY",
                "report": report,
            }
        )
    finally:
        engine.dispose()


def securities_status(*, campaign_id: str) -> None:
    repository, _data, engine = _securities_repository()
    try:
        runs = repository.list_research_runs(campaign_id=campaign_id)
        reports = tuple(
            report
            for run in runs
            for report in repository.list_conformance_reports(research_run_id=run.research_run_id)
        )
        _strategy_lab_json(
            {
                "warning": "SECURITIES SHADOW — NO CAPITAL RESERVED",
                "campaign_id": campaign_id,
                "data_batch_count": len(repository.list_batches()),
                "instrument_count": len(repository.list_instruments()),
                "specification_count": len(repository.list_specifications()),
                "research_run_count": len(runs),
                "signal_count": sum(
                    len(repository.list_signals(run.research_run_id)) for run in runs
                ),
                "conformance_report_count": len(reports),
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def securities_shadow_demo() -> None:
    from apex.adapters.securities_synthetic import run_securities_shadow_demo

    _strategy_lab_json(run_securities_shadow_demo())


def securities_managed_data_readiness() -> None:
    from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter

    adapter = LocalSecuritiesDatasetAdapter()
    inventory = adapter.inventory()
    canonical = next((item for item in inventory if item["canonical"]), None)
    evidence: dict[str, object] = {
        "date_range": None,
        "intervals": (),
        "adjustment_states": (),
        "instrument_count": 0,
        "corporate_action_coverage": (),
        "suitable_for_real_research": False,
        "suitability_reason": "NO_CANONICAL_MANAGED_DATA",
    }
    if canonical is not None:
        dataset = adapter.load_managed_source(
            managed_source=str(canonical["relative_location"]),
            dataset_name="Provider-free readiness inspection",
        )
        synthetic = any(item.source_identity.startswith("apex-synthetic") for item in dataset.bars)
        evidence = {
            "date_range": (
                min(item.period_start for item in dataset.bars),
                max(item.period_end for item in dataset.bars),
            ),
            "intervals": tuple(sorted({item.interval.value for item in dataset.bars})),
            "adjustment_states": tuple(
                sorted({item.adjustment_state.value for item in dataset.bars})
            ),
            "instrument_count": len(dataset.instruments),
            "corporate_action_coverage": tuple(
                sorted({item.action_type.value for item in dataset.corporate_actions})
            ),
            "suitable_for_real_research": not synthetic,
            "suitability_reason": (
                "SYNTHETIC_ENGINEERING_EVIDENCE_ONLY"
                if synthetic
                else "MANAGED_POINT_IN_TIME_EVIDENCE_AVAILABLE"
            ),
        }
    _strategy_lab_json(
        {
            "warning": "SECURITIES SHADOW — NO CAPITAL RESERVED",
            "managed_root": str(adapter.root),
            "file_count": len(inventory),
            "canonical_file_count": sum(bool(item["canonical"]) for item in inventory),
            "files": inventory,
            **evidence,
            "network_fetch_performed": False,
        }
    )


def _real_securities_provider_stack() -> tuple[
    Engine,
    PostgresRealSecuritiesDataRepository,
    SecuritiesProviderConfiguration,
    AlpacaMarketDataHttpAdapter,
]:
    """Build the strictly data-only provider dependencies for one CLI action.

    The adapter reads credentials only from the current process environment.
    This helper neither copies them to child processes nor serializes them.
    """

    _assert_no_tracked_provider_credentials()
    from apex.adapters.alpaca_market_data import (
        ALPACA_ENDPOINT_POLICY_HASH,
        ALPACA_ENDPOINT_POLICY_ID,
        AlpacaMarketDataHttpAdapter,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.real_securities_postgres import PostgresRealSecuritiesDataRepository
    from apex.application.real_securities_data import (
        CreateRealProviderConfiguration,
        CreateRealProviderConfigurationCommand,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresRealSecuritiesDataRepository(engine)
    configuration = repository.active_configuration()
    if configuration is None:
        configuration = CreateRealProviderConfiguration(repository).execute(
            CreateRealProviderConfigurationCommand(
                environment="LOCAL_PROCESS",
                adapter_version=AlpacaMarketDataHttpAdapter.adapter_version,
                endpoint_policy_id=ALPACA_ENDPOINT_POLICY_ID,
                endpoint_policy_hash=ALPACA_ENDPOINT_POLICY_HASH,
                operator_action_id="local-real-securities-provider-configuration-v1",
                created_at=datetime.now(tz=UTC),
            )
        )
    return engine, repository, configuration, AlpacaMarketDataHttpAdapter()


def _assert_no_tracked_provider_credentials() -> None:
    """Fail closed if a likely provider credential was committed or staged.

    The preflight reports filenames only.  It intentionally never echoes a
    matching value, a line, or an environment-derived fingerprint.
    """

    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    names = sorted({item.decode("utf-8") for item in (*tracked, *staged) if item})
    assignment = re.compile(
        rb"(?im)^\s*(?:export\s+)?APCA_API_(?:KEY_ID|SECRET_KEY)\s*[:=]\s*"
        rb"(?![\"']?(?:\$\{|\$|<|YOUR_|example|placeholder))[^\r\n#]+"
    )
    matches: list[str] = []
    for name in names:
        path = ROOT / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
            continue
        if assignment.search(path.read_bytes()) is not None:
            matches.append(name)
    if matches:
        raise SystemExit(
            "provider credential preflight failed in tracked/staged files: " + ", ".join(matches)
        )


def _real_data_date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(f"{value}T00:00:00+00:00")
    except ValueError as error:
        raise SystemExit("real Securities dates must be ISO YYYY-MM-DD") from error
    return parsed


def _real_provider_output(
    *,
    configuration: SecuritiesProviderConfiguration,
    repository: PostgresRealSecuritiesDataRepository,
) -> dict[str, object]:
    from apex.application.real_securities_data import real_data_warnings

    latest_capability = repository.latest_capability(configuration.configuration_id)
    operations = repository.list_operations()
    return {
        "warnings": real_data_warnings(),
        "provider": configuration.provider.value,
        "adapter_version": configuration.adapter_version,
        "credential_source": "ENVIRONMENT",
        "credential_availability_only": (
            None if latest_capability is None else latest_capability.credentials_available
        ),
        "feed": configuration.feed,
        "endpoint_policy_id": configuration.endpoint_policy_id,
        "endpoint_policy_hash": configuration.endpoint_policy_hash,
        "latest_capability": latest_capability,
        "latest_operation": operations[-1] if operations else None,
        "universe_count": len(repository.list_universes()),
        "financial_authority": False,
        "brokerage_endpoint_calls": 0,
    }


def securities_provider_status(*, provider: str) -> None:
    if provider != "ALPACA_MARKET_DATA_V2":
        raise SystemExit("only ALPACA_MARKET_DATA_V2 is supported")
    from apex.application.real_securities_data import CheckSecuritiesProviderCapabilities

    engine, repository, configuration, adapter = _real_securities_provider_stack()
    try:
        capability = CheckSecuritiesProviderCapabilities(
            repository=repository, provider=adapter
        ).execute(configuration=configuration, checked_at=datetime.now(tz=UTC))
        _strategy_lab_json(
            {
                **_real_provider_output(configuration=configuration, repository=repository),
                "capability": capability,
            }
        )
    finally:
        engine.dispose()


def securities_universe_create(
    *, name: str, symbols: str, policy: str, reason: str, idempotency_key: str
) -> None:
    if policy != "PREDECLARED_FIXED_UNIVERSE":
        raise SystemExit("only PREDECLARED_FIXED_UNIVERSE is supported")
    from apex.application.real_securities_data import (
        CreateSecuritiesUniverseSnapshot,
        CreateSecuritiesUniverseSnapshotCommand,
        real_data_warnings,
    )

    engine, repository, _configuration, _adapter = _real_securities_provider_stack()
    try:
        universe = CreateSecuritiesUniverseSnapshot(repository).execute(
            CreateSecuritiesUniverseSnapshotCommand(
                name=name,
                symbols=tuple(item.strip() for item in symbols.split(",")),
                reason=reason,
                operator_action_id="local-real-securities-universe-v1",
                idempotency_key=idempotency_key,
                frozen_at=datetime.now(tz=UTC),
            )
        )
        _strategy_lab_json(
            {
                "warnings": real_data_warnings(),
                "universe": universe,
                "historical_claim": universe.historical_membership_claim.value,
                "survivorship_state": universe.survivorship_state.value,
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def _real_provider_fetch(
    *,
    provider: str,
    operation_type: str,
    universe_id: str | None,
    start: str | None,
    end: str | None,
    idempotency_key: str,
) -> None:
    if provider != "ALPACA_MARKET_DATA_V2":
        raise SystemExit("only ALPACA_MARKET_DATA_V2 is supported")
    from apex.adapters.alpaca_market_data import LocalAlpacaRawArtifactStore
    from apex.application.real_securities_data import (
        FetchSecuritiesProviderOperation,
        FetchSecuritiesProviderOperationCommand,
        real_data_warnings,
    )
    from apex.domain.real_securities_data import SecuritiesProviderFetchOperationType

    engine, repository, configuration, adapter = _real_securities_provider_stack()
    try:
        parsed_type = SecuritiesProviderFetchOperationType(operation_type)
        universe = None if universe_id is None else repository.universe(universe_id)
        if universe_id is not None and universe is None:
            raise SystemExit("real Securities universe was not found")
        operation = FetchSecuritiesProviderOperation(
            repository=repository,
            provider=adapter,
            artifacts=LocalAlpacaRawArtifactStore(),
        ).execute(
            FetchSecuritiesProviderOperationCommand(
                configuration_id=configuration.configuration_id,
                operation_type=parsed_type,
                symbols=() if universe is None else universe.symbols,
                universe_id=universe_id,
                start_at=None if start is None else _real_data_date(start),
                end_at=None if end is None else _real_data_date(end),
                idempotency_key=idempotency_key,
                requested_at=datetime.now(tz=UTC),
            )
        )
        _strategy_lab_json(
            {
                "warnings": real_data_warnings(),
                "operation": operation,
                "canonical_normalization": "REQUIRES_RETAINED_SECURITIES_CALENDAR_EVIDENCE",
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def securities_provider_assets(*, provider: str, idempotency_key: str) -> None:
    _real_provider_fetch(
        provider=provider,
        operation_type="ASSET_SNAPSHOT",
        universe_id=None,
        start=None,
        end=None,
        idempotency_key=idempotency_key,
    )


def securities_provider_backfill(
    *, provider: str, universe_id: str, feed: str, start: str, end: str, idempotency_key: str
) -> None:
    if feed != "IEX":
        raise SystemExit("the initial real-data provider policy requires IEX")
    _real_provider_fetch(
        provider=provider,
        operation_type="HISTORICAL_DAILY_BARS",
        universe_id=universe_id,
        start=start,
        end=end,
        idempotency_key=idempotency_key,
    )


def securities_provider_actions(
    *, provider: str, universe_id: str, start: str, end: str, idempotency_key: str
) -> None:
    _real_provider_fetch(
        provider=provider,
        operation_type="CORPORATE_ACTIONS",
        universe_id=universe_id,
        start=start,
        end=end,
        idempotency_key=idempotency_key,
    )


def securities_data_quality(*, data_batch_id: str) -> None:
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.real_securities_data import (
        EvaluateSecuritiesDataQuality,
        EvaluateSecuritiesDataQualityCommand,
        real_data_warnings,
    )

    engine, repository, _configuration, _adapter = _real_securities_provider_stack()
    try:
        evaluation = EvaluateSecuritiesDataQuality(
            repository=repository,
            evidence=PostgresSecuritiesRepository(engine),
        ).execute(
            EvaluateSecuritiesDataQualityCommand(
                data_batch_id=data_batch_id,
                created_at=datetime.now(tz=UTC),
            )
        )
        _strategy_lab_json(
            {
                "warnings": real_data_warnings(),
                "quality_evaluation": evaluation,
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def securities_canonicalize_retained_alpaca(
    *,
    historical_operation_id: str,
    identity_operation_id: str | None,
    action_operation_id: str | None,
    idempotency_key: str,
) -> None:
    """Canonicalize already-retained Alpaca evidence without a network call."""

    from apex.adapters.alpaca_canonicalization import RetainedAlpacaCanonicalizer
    from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.real_securities_data import real_data_warnings

    engine, repository, _configuration, _adapter = _real_securities_provider_stack()
    try:
        operations = {item.operation_id: item for item in repository.list_operations()}
        historical = operations.get(historical_operation_id)
        identity = None if identity_operation_id is None else operations.get(identity_operation_id)
        actions = None if action_operation_id is None else operations.get(action_operation_id)
        if historical is None:
            raise SystemExit("retained Alpaca historical operation was not found")
        if identity_operation_id is not None and identity is None:
            raise SystemExit("retained Alpaca identity operation was not found")
        if action_operation_id is not None and actions is None:
            raise SystemExit("retained Alpaca corporate-action operation was not found")
        result = RetainedAlpacaCanonicalizer(
            securities_repository=PostgresSecuritiesRepository(engine),
            dataset=LocalSecuritiesDatasetAdapter(),
        ).canonicalize(
            historical_operation=historical,
            identity_operation=identity,
            action_operation=actions,
            idempotency_key=idempotency_key,
            imported_at=datetime.now(tz=UTC),
        )
        _strategy_lab_json(
            {
                "warnings": real_data_warnings(),
                "canonicalization": result,
                "financial_authority": False,
                "brokerage_endpoint_calls": 0,
            }
        )
    finally:
        engine.dispose()


def securities_real_historical_validation(
    *, data_batch_id: str, universe_id: str, start: str, end: str, idempotency_key: str
) -> None:
    """Run the immutable, fail-closed real-evidence validation gate.

    This command deliberately does not start a baseline, model search, or
    deployment.  It retains the exact reason raw/quality evidence is (or is
    not) eligible for those separately explicit research operations.
    """

    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.real_securities_data import (
        RunSecuritiesHistoricalValidation,
        RunSecuritiesHistoricalValidationCommand,
        real_data_warnings,
    )

    engine, repository, _configuration, _adapter = _real_securities_provider_stack()
    try:
        validation = RunSecuritiesHistoricalValidation(
            repository=repository,
            evidence=PostgresSecuritiesRepository(engine),
        ).execute(
            RunSecuritiesHistoricalValidationCommand(
                data_batch_id=data_batch_id,
                universe_id=universe_id,
                start_at=_real_data_date(start),
                end_at=_real_data_date(end),
                idempotency_key=idempotency_key,
                started_at=datetime.now(tz=UTC),
            )
        )
        _strategy_lab_json(
            {
                "warnings": real_data_warnings(),
                "historical_validation": validation,
                "automatic_baseline": False,
                "automatic_candidate_search": False,
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def _historical_market_data_stack() -> tuple[Engine, Any, Any, Any, Any]:
    """One shared engine wired to every repository a historical-data command
    needs: the new historical-market-data repository, the existing canonical
    Securities repository (bar/instrument store), the existing real-data
    provider repository, and the existing stable-security-identity repository.
    Ensures the active Alpaca provider configuration exists exactly as
    ``_real_securities_provider_stack`` does, against the identical engine.
    """

    _assert_no_tracked_provider_credentials()
    from apex.adapters.alpaca_market_data import (
        ALPACA_ENDPOINT_POLICY_HASH,
        ALPACA_ENDPOINT_POLICY_ID,
        AlpacaMarketDataHttpAdapter,
    )
    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.real_securities_postgres import PostgresRealSecuritiesDataRepository
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.adapters.security_identity_postgres import PostgresSecurityIdentityRepository
    from apex.application.real_securities_data import (
        CreateRealProviderConfiguration,
        CreateRealProviderConfigurationCommand,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    real_repository = PostgresRealSecuritiesDataRepository(engine)
    configuration = real_repository.active_configuration()
    if configuration is None:
        configuration = CreateRealProviderConfiguration(real_repository).execute(
            CreateRealProviderConfigurationCommand(
                environment="LOCAL_PROCESS",
                adapter_version=AlpacaMarketDataHttpAdapter.adapter_version,
                endpoint_policy_id=ALPACA_ENDPOINT_POLICY_ID,
                endpoint_policy_hash=ALPACA_ENDPOINT_POLICY_HASH,
                operator_action_id="local-historical-market-data-provider-configuration-v1",
                created_at=datetime.now(tz=UTC),
            )
        )
    return (
        engine,
        PostgresHistoricalMarketDataRepository(engine),
        PostgresSecuritiesRepository(engine),
        real_repository,
        PostgresSecurityIdentityRepository(engine),
    )


def historical_data_plan_create(
    *,
    symbols: str,
    etf_symbols: str,
    start: str,
    end: str,
    chunk_size: int,
    maximum_instruments: int,
    maximum_requests: int,
    maximum_bytes: int,
    evidence_class: str,
    idempotency_key: str,
) -> None:
    from apex.application.historical_market_data import (
        CreateHistoricalCapturePlan,
        CreateHistoricalCapturePlanCommand,
        historical_market_data_warnings,
    )

    engine, historical_repository, _securities, _real, identity_repository = (
        _historical_market_data_stack()
    )
    try:
        plan = CreateHistoricalCapturePlan(
            repository=historical_repository, identity_source=identity_repository
        ).execute(
            CreateHistoricalCapturePlanCommand(
                symbols=tuple(item.strip() for item in symbols.split(",") if item.strip()),
                etf_symbols=frozenset(
                    item.strip().upper() for item in etf_symbols.split(",") if item.strip()
                ),
                provider="ALPACA_MARKET_DATA_V2",
                start_date=start,
                end_date=end,
                chunk_size=chunk_size,
                maximum_instruments=maximum_instruments,
                maximum_requests=maximum_requests,
                maximum_bytes=maximum_bytes,
                evidence_class=evidence_class,
                operator_action_id="local-historical-data-plan-create-v1",
                idempotency_key=idempotency_key,
                created_at=datetime.now(tz=UTC),
            )
        )
        _strategy_lab_json(
            {
                "warnings": historical_market_data_warnings(),
                "plan": plan,
                "financial_authority": False,
                "brokerage_endpoint_calls": 0,
            }
        )
    finally:
        engine.dispose()


def historical_data_capture_run(*, plan_id: str, ordinal: int | None) -> None:
    from apex.adapters.alpaca_canonicalization import HistoricalAlpacaCanonicalizer
    from apex.adapters.alpaca_market_data import (
        AlpacaMarketDataHttpAdapter,
        LocalAlpacaRawArtifactStore,
    )
    from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter
    from apex.application.historical_market_data import (
        RunHistoricalCaptureChunk,
        RunHistoricalCaptureChunkCommand,
        historical_market_data_warnings,
        plan_summary_state,
    )
    from apex.application.real_securities_data import FetchSecuritiesProviderOperation

    engine, historical_repository, securities_repository, real_repository, _identity = (
        _historical_market_data_stack()
    )
    try:
        configuration = real_repository.active_configuration()
        if configuration is None:
            raise SystemExit("REAL_SECURITIES_PROVIDER_CONFIGURATION_NOT_ACTIVE")
        runner = RunHistoricalCaptureChunk(
            repository=historical_repository,
            fetch_operation=FetchSecuritiesProviderOperation(
                repository=real_repository,
                provider=AlpacaMarketDataHttpAdapter(),
                artifacts=LocalAlpacaRawArtifactStore(),
            ),
            canonicalizer=HistoricalAlpacaCanonicalizer(
                securities_repository=securities_repository,
                dataset=LocalSecuritiesDatasetAdapter(),
            ),
            configuration_id=configuration.configuration_id,
        )
        chunk = runner.execute(
            RunHistoricalCaptureChunkCommand(
                plan_id=plan_id, ordinal=ordinal, requested_at=datetime.now(tz=UTC)
            )
        )
        plan = historical_repository.get_plan(plan_id)
        _strategy_lab_json(
            {
                "warnings": historical_market_data_warnings(),
                "chunk": chunk,
                "plan_summary_state": (
                    None
                    if plan is None
                    else plan_summary_state(repository=historical_repository, plan=plan).value
                ),
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def historical_data_snapshot_materialize(*, plan_id: str) -> None:
    from apex.application.historical_market_data import (
        MaterializeHistoricalDatasetSnapshot,
        MaterializeHistoricalDatasetSnapshotCommand,
        historical_market_data_warnings,
    )

    engine, historical_repository, securities_repository, _real, _identity = (
        _historical_market_data_stack()
    )
    try:
        snapshot = MaterializeHistoricalDatasetSnapshot(
            repository=historical_repository, evidence=securities_repository
        ).execute(
            MaterializeHistoricalDatasetSnapshotCommand(
                plan_id=plan_id, created_at=datetime.now(tz=UTC)
            )
        )
        _strategy_lab_json(
            {
                "warnings": historical_market_data_warnings(),
                "snapshot": snapshot,
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def historical_data_quality_report(*, snapshot_id: str) -> None:
    from apex.application.historical_market_data import (
        EvaluateHistoricalDataQuality,
        EvaluateHistoricalDataQualityCommand,
        historical_market_data_warnings,
    )

    engine, historical_repository, securities_repository, _real, _identity = (
        _historical_market_data_stack()
    )
    try:
        report = EvaluateHistoricalDataQuality(
            repository=historical_repository, evidence=securities_repository
        ).execute(
            EvaluateHistoricalDataQualityCommand(
                snapshot_id=snapshot_id, created_at=datetime.now(tz=UTC)
            )
        )
        _strategy_lab_json(
            {
                "warnings": historical_market_data_warnings(),
                "quality_report": report,
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def historical_data_calculate(
    *, snapshot_id: str, stable_security_id: str, as_of: str, calculation: str, window: int | None
) -> None:
    from apex.application.historical_market_data import (
        HistoricalPointInTimeReplay,
        historical_market_data_warnings,
    )
    from apex.domain.historical_market_data import HistoricalClock

    engine, historical_repository, securities_repository, _real, _identity = (
        _historical_market_data_stack()
    )
    try:
        replay = HistoricalPointInTimeReplay(
            repository=historical_repository, evidence=securities_repository
        )
        value = replay.calculate(
            snapshot_id=snapshot_id,
            stable_security_id=stable_security_id,
            clock=HistoricalClock(as_of=_real_data_date(as_of)),
            calculation=calculation,
            window=window,
        )
        _strategy_lab_json(
            {
                "warnings": historical_market_data_warnings(),
                "calculation": calculation,
                "stable_security_id": stable_security_id,
                "as_of": as_of,
                "value": None if value is None else str(value),
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def historical_data_workspace() -> None:
    from apex.application.historical_market_data import (
        historical_market_data_warnings,
        plan_summary_state,
    )

    engine, historical_repository, _securities, real_repository, _identity = (
        _historical_market_data_stack()
    )
    try:
        plans = historical_repository.list_plans()
        configuration = real_repository.active_configuration()
        capability = (
            None
            if configuration is None
            else real_repository.latest_capability(configuration.configuration_id)
        )
        _strategy_lab_json(
            {
                "warnings": historical_market_data_warnings(),
                "provider_configured": configuration is not None,
                "credential_availability_only": (
                    None if capability is None else capability.credentials_available
                ),
                "plan_count": len(plans),
                "plans": tuple(
                    {
                        "plan_id": plan.plan_id,
                        "evidence_class": plan.evidence_class.value,
                        "universe_count": len(plan.universe),
                        "chunk_count": len(plan.chunk_plan),
                        "start_date": plan.policy.start_date,
                        "end_date": plan.policy.end_date,
                        "summary_state": plan_summary_state(
                            repository=historical_repository, plan=plan
                        ).value,
                        "chunks": historical_repository.list_chunks(plan.plan_id),
                    }
                    for plan in plans
                ),
                "snapshots": historical_repository.list_snapshots(),
                "quality_reports": historical_repository.list_quality_reports(),
                "financial_authority": False,
                "brokerage_endpoint_calls": 0,
            }
        )
    finally:
        engine.dispose()


def genuine_multi_asset_registry_inspect(
    *, hypothesis_version_id: str | None, hypothesis_id: str | None
) -> None:
    """Milestone section 14: one bounded, read-only view resolving every
    canonical genuine multi-asset artifact id live from retained evidence
    -- the cohort policy, its capture plan/snapshot/quality reports, the
    per-instrument quality gate, the cross-market dataset binding, and (when
    the hypothesis ids are supplied) the full replay/governance/trial/walk-
    forward/robustness/conclusion/promotion lineage. Never rerunning any
    network capture; never granting authority."""

    from apex.adapters.genuine_multi_asset_history_postgres import (
        PostgresGenuineMultiAssetHistoryRepository,
    )
    from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository
    from apex.application.genuine_multi_asset_history import resolve_genuine_multi_asset_registry
    from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1

    engine, historical_repository, _securities, _real, _identity = _historical_market_data_stack()
    try:
        registry = resolve_genuine_multi_asset_registry(
            cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
            historical_repository=historical_repository,
            genuine_repository=PostgresGenuineMultiAssetHistoryRepository(engine),
            replay_repository=PostgresHistoricalReplayRepository(engine),
            methodology_repository=PostgresResearchMethodologyRepository(engine),
            hypothesis_version_id=hypothesis_version_id,
            hypothesis_id=hypothesis_id,
        )
        _strategy_lab_json(
            {
                "registry": registry,
                "financial_authority": False,
                "brokerage_endpoint_calls": 0,
            }
        )
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# Point-in-time historical replay engine (bounded commands only -- see
# apex.application.historical_replay / apex.domain.historical_replay)
# --------------------------------------------------------------------------


_REPLAY_LIST_BOUND = 5_000


def _historical_replay_stack() -> tuple[Engine, Any, Any, Any]:
    """One shared engine wired to the replay repository plus the two
    existing repositories every replay command needs: historical
    market-data (dataset snapshot/plan lineage) and the canonical Securities
    repository (point-in-time bar evidence)."""

    from apex.adapters.historical_market_data_postgres import (
        PostgresHistoricalMarketDataRepository,
    )
    from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        engine,
        PostgresHistoricalReplayRepository(engine),
        PostgresHistoricalMarketDataRepository(engine),
        PostgresSecuritiesRepository(engine),
    )


def _replay_calendar(*, calendar_identity: str | None, start_date: str, end_date: str) -> Any:
    if calendar_identity is None:
        return None
    from apex.adapters.xnys_calendar import ReviewedXNYSCalendar

    return ReviewedXNYSCalendar(start_date=start_date, end_date=end_date)


def replay_plan_create(
    *,
    dataset_snapshot_id: str,
    start: str,
    end: str,
    calendar_identity: str | None,
    hypothesis_version_id: str | None,
    idempotency_key: str,
) -> None:
    from apex.application.historical_replay import (
        CreateReplayRun,
        CreateReplayRunCommand,
        historical_replay_warnings,
    )

    engine, replay_repository, historical_repository, _securities = _historical_replay_stack()
    try:
        calendar = _replay_calendar(
            calendar_identity=calendar_identity, start_date=start, end_date=end
        )
        run = CreateReplayRun(
            repository=replay_repository,
            historical_data_repository=historical_repository,
            calendar=calendar,
        ).execute(
            CreateReplayRunCommand(
                dataset_snapshot_id=dataset_snapshot_id,
                start_date=start,
                end_date=end,
                calendar_identity=calendar_identity,
                hypothesis_version_id=hypothesis_version_id,
                operator_action_id="local-replay-plan-create-v1",
                idempotency_key=idempotency_key,
                created_at=datetime.now(tz=UTC),
            )
        )
        _research_evidence_json(
            {"warnings": historical_replay_warnings(), "run": run, "financial_authority": False}
        )
    finally:
        engine.dispose()


def _replay_step(
    *,
    command_name: str,
    replay_run_id: str,
    maximum_steps: int,
    benchmark_stable_security_id: str | None,
    crypto_capture_id: str | None,
) -> None:
    from apex.application.historical_replay import (
        HistoricalReplayEngine,
        ResumeReplayRun,
        StartReplayRun,
        StepReplayRunCommand,
        historical_replay_warnings,
    )

    engine, replay_repository, historical_repository, securities_repository = (
        _historical_replay_stack()
    )
    try:
        run = replay_repository.get_run(replay_run_id)
        if run is None:
            raise SystemExit("REPLAY_RUN_NOT_FOUND")
        calendar = _replay_calendar(
            calendar_identity=run.calendar_identity,
            start_date=run.start_date,
            end_date=run.end_date,
        )
        crypto_evidence = None
        if crypto_capture_id is not None:
            from apex.adapters.combined_crypto_historical_evidence import (
                COMBINED_GENUINE_BTC_CAPTURES,
                CombinedCryptoHistoricalEvidenceSource,
            )
            from apex.adapters.crypto_research_postgres import PostgresCryptoResearchRepository

            crypto_evidence = CombinedCryptoHistoricalEvidenceSource(
                source=PostgresCryptoResearchRepository(engine),
                combined_captures=COMBINED_GENUINE_BTC_CAPTURES,
            )
        replay_engine = HistoricalReplayEngine(
            repository=replay_repository,
            historical_data_repository=historical_repository,
            evidence=securities_repository,
            calendar=calendar,
            crypto_evidence=crypto_evidence,
        )
        step_command = StepReplayRunCommand(
            replay_run_id=replay_run_id,
            maximum_steps=maximum_steps,
            requested_at=datetime.now(tz=UTC),
            benchmark_stable_security_id=benchmark_stable_security_id,
            crypto_capture_id=crypto_capture_id,
        )
        service = (
            StartReplayRun(repository=replay_repository, engine=replay_engine)
            if command_name == "start"
            else ResumeReplayRun(repository=replay_repository, engine=replay_engine)
        )
        result = service.execute(step_command)
        _research_evidence_json(
            {"warnings": historical_replay_warnings(), "run": result, "financial_authority": False}
        )
    finally:
        engine.dispose()


def replay_run_start(
    *,
    replay_run_id: str,
    maximum_steps: int,
    benchmark_stable_security_id: str | None,
    crypto_capture_id: str | None,
) -> None:
    _replay_step(
        command_name="start",
        replay_run_id=replay_run_id,
        maximum_steps=maximum_steps,
        benchmark_stable_security_id=benchmark_stable_security_id,
        crypto_capture_id=crypto_capture_id,
    )


def replay_run_resume(
    *,
    replay_run_id: str,
    maximum_steps: int,
    benchmark_stable_security_id: str | None,
    crypto_capture_id: str | None,
) -> None:
    _replay_step(
        command_name="resume",
        replay_run_id=replay_run_id,
        maximum_steps=maximum_steps,
        benchmark_stable_security_id=benchmark_stable_security_id,
        crypto_capture_id=crypto_capture_id,
    )


def replay_run_inspect(*, replay_run_id: str) -> None:
    from apex.application.historical_replay import historical_replay_warnings

    engine, replay_repository, _historical_repository, _securities = _historical_replay_stack()
    try:
        run = replay_repository.get_run(replay_run_id)
        if run is None:
            raise SystemExit("REPLAY_RUN_NOT_FOUND")
        checkpoints = replay_repository.list_checkpoints(replay_run_id)
        _research_evidence_json(
            {
                "warnings": historical_replay_warnings(),
                "run": run,
                "checkpoint_count": len(checkpoints),
                "latest_checkpoint": checkpoints[-1] if checkpoints else None,
                "research_period_policy": replay_repository.research_period_policy(replay_run_id),
                "experiment_links": replay_repository.experiment_links(replay_run_id),
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def replay_frame_inspect(
    *, frame_id: str | None, replay_run_id: str | None, session_date: str | None
) -> None:
    from apex.application.historical_replay import historical_replay_warnings

    engine, replay_repository, _historical_repository, _securities = _historical_replay_stack()
    try:
        frame = None
        if frame_id is not None:
            frame = replay_repository.frame_by_id(frame_id)
        elif replay_run_id is not None and session_date is not None:
            frame = next(
                (
                    item
                    for item in replay_repository.list_frames(
                        replay_run_id, limit=_REPLAY_LIST_BOUND
                    )
                    if item.session_date == session_date
                ),
                None,
            )
        else:
            raise SystemExit(
                "REPLAY_FRAME_INSPECT_REQUIRES_EITHER_FRAME_ID_OR_RUN_ID_AND_SESSION_DATE"
            )
        if frame is None:
            raise SystemExit("REPLAY_FEATURE_FRAME_NOT_FOUND")
        _research_evidence_json(
            {"warnings": historical_replay_warnings(), "frame": frame, "financial_authority": False}
        )
    finally:
        engine.dispose()


def replay_evaluation_list(*, replay_run_id: str, limit: int) -> None:
    from apex.application.historical_replay import historical_replay_warnings

    engine, replay_repository, _historical_repository, _securities = _historical_replay_stack()
    try:
        bounded_limit = min(limit, _REPLAY_LIST_BOUND)
        evaluations = replay_repository.list_evaluations(replay_run_id, limit=bounded_limit)
        _research_evidence_json(
            {
                "warnings": historical_replay_warnings(),
                "replay_run_id": replay_run_id,
                "evaluation_count": len(evaluations),
                "evaluations": evaluations,
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def historical_research_summary(*, replay_run_id: str) -> None:
    from apex.application.historical_replay import (
        build_historical_research_summary,
        historical_replay_warnings,
    )

    engine, replay_repository, _historical_repository, _securities = _historical_replay_stack()
    try:
        summary = build_historical_research_summary(
            repository=replay_repository, replay_run_id=replay_run_id
        )
        _research_evidence_json(
            {
                "warnings": historical_replay_warnings(),
                "summary": summary,
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def replay_experiment_construct(*, replay_run_id: str, code_revision: str, actor: str) -> None:
    """Not part of the bounded section-17 CLI list, but the historical
    experiment lineage (section 11) is reachable through the identical
    ``research-experiment-inspect``/``research-outcome-inspect`` commands
    already registered below once constructed here."""

    from apex.adapters.research_evidence_postgres import PostgresResearchEvidenceRepository
    from apex.application.historical_replay import (
        ConstructReplayExperiment,
        ConstructReplayExperimentCommand,
        historical_replay_warnings,
    )

    engine, replay_repository, historical_repository, _securities = _historical_replay_stack()
    try:
        research_evidence_repository = PostgresResearchEvidenceRepository(engine)
        link = ConstructReplayExperiment(
            repository=replay_repository,
            historical_data_repository=historical_repository,
            research_evidence=research_evidence_repository,
        ).execute(
            ConstructReplayExperimentCommand(
                replay_run_id=replay_run_id,
                code_revision=code_revision,
                actor=actor,
                decided_at=datetime.now(tz=UTC),
            )
        )
        _research_evidence_json(
            {"warnings": historical_replay_warnings(), "link": link, "financial_authority": False}
        )
    finally:
        engine.dispose()


def _research_methodology_stack() -> tuple[Engine, Any, Any]:
    """One shared engine wired to the research-methodology repository plus
    the existing replay repository every methodology command reads
    already-retained evidence from."""

    from apex.adapters.historical_replay_postgres import PostgresHistoricalReplayRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_methodology_postgres import PostgresResearchMethodologyRepository

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        engine,
        PostgresResearchMethodologyRepository(engine),
        PostgresHistoricalReplayRepository(engine),
    )


def _methodology_json(value: object) -> None:
    from apex.domain.research_methodology import research_methodology_warnings

    payload = {"warnings": research_methodology_warnings(), "financial_authority": False}
    if isinstance(value, dict):
        payload.update(value)
    _research_evidence_json(payload)


def research_policy_create(
    *,
    hypothesis_version_id: str,
    dataset_snapshot_id: str,
    replay_run_id: str,
    intent: str,
    train_start: str | None,
    train_end: str | None,
    validation_start: str | None,
    validation_end: str | None,
    holdout_start: str | None,
    holdout_end: str | None,
    holdout_locked: bool,
    minimum_sample_count: int,
    revision: int,
    supersedes_governance_policy_id: str | None,
) -> None:
    from apex.application.research_methodology import (
        DeclareGovernancePolicyCommand,
        DeclareResearchGovernancePolicy,
    )
    from apex.domain.historical_replay import (
        ResearchPeriodDeclaration,
        ResearchPeriodPolicy,
        ResearchPeriodRole,
    )

    periods = []
    if train_start is not None and train_end is not None:
        periods.append(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, train_start, train_end, False)
        )
    if validation_start is not None and validation_end is not None:
        periods.append(
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, validation_start, validation_end, False
            )
        )
    if holdout_start is not None and holdout_end is not None:
        periods.append(
            ResearchPeriodDeclaration(
                ResearchPeriodRole.HOLDOUT, holdout_start, holdout_end, holdout_locked
            )
        )
    if not periods:
        raise SystemExit("RESEARCH_POLICY_CREATE_REQUIRES_AT_LEAST_ONE_DECLARED_PERIOD")
    period_policy = ResearchPeriodPolicy.create(
        replay_run_id=replay_run_id,
        periods=tuple(periods),
        minimum_sample_count=minimum_sample_count,
        created_at=datetime.now(tz=UTC),
    )
    engine, repository, _replay = _research_methodology_stack()
    try:
        policy = DeclareResearchGovernancePolicy(repository=repository).execute(
            DeclareGovernancePolicyCommand(
                hypothesis_version_id=hypothesis_version_id,
                dataset_snapshot_id=dataset_snapshot_id,
                replay_run_id=replay_run_id,
                intent=intent,
                period_policy=period_policy,
                revision=revision,
                supersedes_governance_policy_id=supersedes_governance_policy_id,
                declared_at=datetime.now(tz=UTC),
            )
        )
        _methodology_json({"governance_policy": policy})
    finally:
        engine.dispose()


def research_policy_inspect(*, governance_policy_id: str) -> None:
    engine, repository, _replay = _research_methodology_stack()
    try:
        policy = repository.governance_policy_by_id(governance_policy_id)
        if policy is None:
            raise SystemExit("GOVERNANCE_POLICY_NOT_FOUND")
        exposures = repository.holdout_exposures_for_policy(governance_policy_id)
        _methodology_json(
            {
                "governance_policy": policy,
                "holdout_exposure_count": len(exposures),
                "holdout_exposures": exposures,
            }
        )
    finally:
        engine.dispose()


def research_holdout_expose(
    *, governance_policy_id: str, actor: str, purpose: str, consumed_trial_id: str | None
) -> None:
    from apex.application.research_methodology import (
        RecordHoldoutExposure,
        RecordHoldoutExposureCommand,
    )

    engine, repository, _replay = _research_methodology_stack()
    try:
        event = RecordHoldoutExposure(repository=repository).execute(
            RecordHoldoutExposureCommand(
                governance_policy_id=governance_policy_id,
                exposed_at=datetime.now(tz=UTC),
                actor=actor,
                purpose=purpose,
                consumed_trial_id=consumed_trial_id,
            )
        )
        _methodology_json({"holdout_exposure": event})
    finally:
        engine.dispose()


def research_trial_create(
    *,
    hypothesis_version_id: str,
    dataset_snapshot_id: str,
    replay_run_id: str,
    phase: str,
    market_scope: str,
    method_id: str,
    method_version: str,
    parameter_name: str,
    parameter_value: str,
    configuration_origin: str,
    candidates_examined_count: int,
    session_date_start: str,
    session_date_end: str,
    benchmark_kind: str | None,
    governance_policy_id: str | None,
    outcome_horizon_sessions: int,
    bootstrap_seed: int | None,
    hac_lag: int | None,
) -> None:
    from apex.application.research_methodology import (
        RecordResearchTrial,
        RecordResearchTrialCommand,
    )
    from apex.domain.research_methodology import ResearchConfiguration

    configuration = ResearchConfiguration.create(
        method_id=method_id,
        method_version=method_version,
        parameter_values=((parameter_name, parameter_value),),
        origin=configuration_origin,
        candidates_examined_count=candidates_examined_count,
    )
    engine, repository, replay_repository = _research_methodology_stack()
    try:
        trial = RecordResearchTrial(
            repository=repository, historical_replay=replay_repository
        ).execute(
            RecordResearchTrialCommand(
                hypothesis_version_id=hypothesis_version_id,
                dataset_snapshot_id=dataset_snapshot_id,
                replay_run_id=replay_run_id,
                phase=phase,
                market_scope=tuple(market_scope.split(",")),
                configuration=configuration,
                method_id=method_id,
                session_date_start=session_date_start,
                session_date_end=session_date_end,
                benchmark_kind=benchmark_kind,
                created_at=datetime.now(tz=UTC),
                governance_policy_id=governance_policy_id,
                outcome_horizon_sessions=outcome_horizon_sessions,
                bootstrap_seed=bootstrap_seed,
                hac_lag=hac_lag,
            )
        )
        _methodology_json({"trial": trial})
    finally:
        engine.dispose()


def research_trial_inspect(*, trial_id: str) -> None:
    engine, repository, _replay = _research_methodology_stack()
    try:
        trial = repository.trial_by_id(trial_id)
        if trial is None:
            raise SystemExit("RESEARCH_TRIAL_NOT_FOUND")
        _methodology_json({"trial": trial})
    finally:
        engine.dispose()


def research_trial_family_create(
    *,
    hypothesis_id: str,
    trial_ids: str,
    candidate_count: int,
    asset_count: int,
    outcome_definition_count: int,
    regime_slice_count: int,
    selection_method: str,
    correction_method: str,
) -> None:
    from apex.application.research_methodology import (
        BuildResearchTrialFamily,
        BuildTrialFamilyCommand,
    )

    engine, repository, _replay = _research_methodology_stack()
    try:
        family = BuildResearchTrialFamily(repository=repository).execute(
            BuildTrialFamilyCommand(
                hypothesis_id=hypothesis_id,
                trial_ids=tuple(trial_ids.split(",")),
                candidate_count=candidate_count,
                asset_count=asset_count,
                outcome_definition_count=outcome_definition_count,
                regime_slice_count=regime_slice_count,
                selection_method=selection_method,
                correction_method=correction_method,
                created_at=datetime.now(tz=UTC),
            )
        )
        _methodology_json({"trial_family": family})
    finally:
        engine.dispose()


def research_trial_family_inspect(*, trial_family_id: str) -> None:
    engine, repository, _replay = _research_methodology_stack()
    try:
        family = repository.trial_family_by_id(trial_family_id)
        if family is None:
            raise SystemExit("TRIAL_FAMILY_NOT_FOUND")
        _methodology_json({"trial_family": family})
    finally:
        engine.dispose()


def walk_forward_create(
    *,
    replay_run_id: str,
    method_id: str,
    outcome_metric_id: str,
    training_window_sessions: int,
    validation_window_sessions: int,
    test_window_sessions: int,
    step_sessions: int,
    window_mode: str,
    minimum_samples: int,
    cost_information_state: str,
    start_date: str | None,
    end_date: str | None,
    hypothesis_version_id: str | None,
    idempotency_key: str,
) -> None:
    from apex.application.research_methodology import (
        CreateWalkForwardRun,
        CreateWalkForwardRunCommand,
    )

    engine, repository, replay_repository = _research_methodology_stack()
    try:
        run = replay_repository.get_run(replay_run_id)
        if run is None:
            raise SystemExit("REPLAY_RUN_NOT_FOUND")
        wf_run = CreateWalkForwardRun(
            repository=repository, historical_replay=replay_repository
        ).execute(
            CreateWalkForwardRunCommand(
                replay_run_id=replay_run_id,
                method_id=method_id,
                feature_policy_hash=run.feature_policy_hash,
                outcome_metric_id=outcome_metric_id,
                training_window_sessions=training_window_sessions,
                validation_window_sessions=validation_window_sessions,
                test_window_sessions=test_window_sessions,
                step_sessions=step_sessions,
                window_mode=window_mode,
                minimum_samples=minimum_samples,
                cost_information_state=cost_information_state,
                start_date=start_date or run.start_date,
                end_date=end_date or run.end_date,
                hypothesis_version_id=hypothesis_version_id,
                operator_action_id="local-walk-forward-create-v1",
                idempotency_key=idempotency_key,
                created_at=datetime.now(tz=UTC),
            )
        )
        _methodology_json({"walk_forward_run": wf_run})
    finally:
        engine.dispose()


def _walk_forward_step(
    *,
    command_name: str,
    walk_forward_run_id: str,
    maximum_folds: int,
    market_scope: str,
    method_id: str,
    method_version: str,
    parameter_name: str,
    parameter_value: str,
    configuration_origin: str,
    candidates_examined_count: int,
    benchmark_kind: str | None,
) -> None:
    from apex.application.research_methodology import (
        ResumeWalkForwardRun,
        StartWalkForwardRun,
        StepWalkForwardRunCommand,
        WalkForwardEngine,
    )
    from apex.domain.research_methodology import ResearchConfiguration

    configuration = ResearchConfiguration.create(
        method_id=method_id,
        method_version=method_version,
        parameter_values=((parameter_name, parameter_value),),
        origin=configuration_origin,
        candidates_examined_count=candidates_examined_count,
    )
    engine, repository, replay_repository = _research_methodology_stack()
    try:
        wf_engine = WalkForwardEngine(repository=repository, historical_replay=replay_repository)
        service = (
            StartWalkForwardRun(repository=repository, engine=wf_engine)
            if command_name == "start"
            else ResumeWalkForwardRun(repository=repository, engine=wf_engine)
        )
        result = service.execute(
            StepWalkForwardRunCommand(
                walk_forward_run_id=walk_forward_run_id,
                maximum_folds=maximum_folds,
                market_scope=tuple(market_scope.split(",")),
                configuration=configuration,
                benchmark_kind=benchmark_kind,
                requested_at=datetime.now(tz=UTC),
            )
        )
        _methodology_json({"walk_forward_run": result})
    finally:
        engine.dispose()


def walk_forward_run_start(
    *,
    walk_forward_run_id: str,
    maximum_folds: int,
    market_scope: str,
    method_id: str,
    method_version: str,
    parameter_name: str,
    parameter_value: str,
    configuration_origin: str,
    candidates_examined_count: int,
    benchmark_kind: str | None,
) -> None:
    _walk_forward_step(
        command_name="start",
        walk_forward_run_id=walk_forward_run_id,
        maximum_folds=maximum_folds,
        market_scope=market_scope,
        method_id=method_id,
        method_version=method_version,
        parameter_name=parameter_name,
        parameter_value=parameter_value,
        configuration_origin=configuration_origin,
        candidates_examined_count=candidates_examined_count,
        benchmark_kind=benchmark_kind,
    )


def walk_forward_run_resume(
    *,
    walk_forward_run_id: str,
    maximum_folds: int,
    market_scope: str,
    method_id: str,
    method_version: str,
    parameter_name: str,
    parameter_value: str,
    configuration_origin: str,
    candidates_examined_count: int,
    benchmark_kind: str | None,
) -> None:
    _walk_forward_step(
        command_name="resume",
        walk_forward_run_id=walk_forward_run_id,
        maximum_folds=maximum_folds,
        market_scope=market_scope,
        method_id=method_id,
        method_version=method_version,
        parameter_name=parameter_name,
        parameter_value=parameter_value,
        configuration_origin=configuration_origin,
        candidates_examined_count=candidates_examined_count,
        benchmark_kind=benchmark_kind,
    )


def walk_forward_inspect(*, walk_forward_run_id: str) -> None:
    from apex.application.research_methodology import build_walk_forward_summary

    engine, repository, _replay = _research_methodology_stack()
    try:
        run = repository.get_walk_forward_run(walk_forward_run_id)
        if run is None:
            raise SystemExit("WALK_FORWARD_RUN_NOT_FOUND")
        folds = repository.folds_for_run(walk_forward_run_id)
        summary = None
        if folds:
            try:
                summary = build_walk_forward_summary(
                    repository=repository, walk_forward_run_id=walk_forward_run_id
                )
            except Exception:
                summary = None
        _methodology_json(
            {
                "walk_forward_run": run,
                "fold_count": len(folds),
                "folds": folds,
                "summary": summary,
            }
        )
    finally:
        engine.dispose()


def robustness_run_create(
    *, base_trial_id: str, momentum_lookback_windows: str, momentum_positive_threshold: str
) -> None:
    from decimal import Decimal

    from apex.application.research_methodology import (
        RunRobustnessChecks,
        RunRobustnessChecksCommand,
    )

    engine, repository, replay_repository = _research_methodology_stack()
    try:
        robustness = RunRobustnessChecks(
            repository=repository, historical_replay=replay_repository
        ).execute(
            RunRobustnessChecksCommand(
                base_trial_id=base_trial_id,
                momentum_lookback_windows=tuple(
                    int(item) for item in momentum_lookback_windows.split(",")
                ),
                momentum_positive_threshold=Decimal(momentum_positive_threshold),
                created_at=datetime.now(tz=UTC),
            )
        )
        _methodology_json({"robustness_check_run": robustness})
    finally:
        engine.dispose()


def robustness_run_inspect(*, robustness_run_id: str) -> None:
    engine, repository, _replay = _research_methodology_stack()
    try:
        robustness = repository.robustness_run_by_id(robustness_run_id)
        if robustness is None:
            raise SystemExit("ROBUSTNESS_CHECK_RUN_NOT_FOUND")
        _methodology_json({"robustness_check_run": robustness})
    finally:
        engine.dispose()


def research_conclusion_create(
    *,
    hypothesis_version_id: str,
    dataset_snapshot_id: str,
    replay_run_id: str,
    training_trial_id: str | None,
    validation_trial_id: str | None,
    holdout_trial_id: str | None,
    walk_forward_run_id: str | None,
    robustness_run_id: str | None,
    trial_family_id: str | None,
    point_in_time_limitations: str,
    rationale: str,
    sample_size_phase: str,
) -> None:
    from apex.application.research_methodology import (
        BuildConclusionCommand,
        BuildResearchConclusion,
    )
    from apex.domain.research_methodology import ResearchPhase

    engine, repository, _replay = _research_methodology_stack()
    try:
        conclusion = BuildResearchConclusion(repository=repository).execute(
            BuildConclusionCommand(
                hypothesis_version_id=hypothesis_version_id,
                dataset_snapshot_id=dataset_snapshot_id,
                replay_run_id=replay_run_id,
                training_trial_id=training_trial_id,
                validation_trial_id=validation_trial_id,
                holdout_trial_id=holdout_trial_id,
                walk_forward_run_id=walk_forward_run_id,
                robustness_run_id=robustness_run_id,
                trial_family_id=trial_family_id,
                point_in_time_limitations=(
                    tuple(point_in_time_limitations.split(",")) if point_in_time_limitations else ()
                ),
                rationale=rationale,
                created_at=datetime.now(tz=UTC),
                sample_size_phase=ResearchPhase(sample_size_phase),
            )
        )
        _methodology_json({"conclusion": conclusion})
    finally:
        engine.dispose()


def research_conclusion_inspect(*, conclusion_id: str) -> None:
    engine, repository, _replay = _research_methodology_stack()
    try:
        conclusion = repository.conclusion_by_id(conclusion_id)
        if conclusion is None:
            raise SystemExit("RESEARCH_CONCLUSION_NOT_FOUND")
        promotions = repository.promotion_evaluations_for_conclusion(conclusion_id)
        _methodology_json({"conclusion": conclusion, "promotion_evaluations": promotions})
    finally:
        engine.dispose()


def research_conclusion_list(*, hypothesis_version_id: str) -> None:
    engine, repository, _replay = _research_methodology_stack()
    try:
        conclusions = repository.conclusions_for_hypothesis(hypothesis_version_id)
        _methodology_json(
            {
                "hypothesis_version_id": hypothesis_version_id,
                "conclusion_count": len(conclusions),
                "conclusions": conclusions,
            }
        )
    finally:
        engine.dispose()


def research_audit_inspect(*, hypothesis_version_id: str) -> None:
    from apex.application.research_methodology import build_research_audit_trail

    engine, repository, _replay = _research_methodology_stack()
    try:
        trail = build_research_audit_trail(
            repository=repository, hypothesis_version_id=hypothesis_version_id
        )
        _methodology_json({"audit_trail": trail})
    finally:
        engine.dispose()


def promotion_evaluate(
    *, conclusion_id: str, require_holdout_consumed: bool, lookahead_violation: bool
) -> None:
    from apex.application.research_methodology import EvaluatePromotion, EvaluatePromotionCommand
    from apex.domain.research_methodology import PromotionPolicy

    engine, repository, _replay = _research_methodology_stack()
    try:
        evaluation = EvaluatePromotion(repository=repository).execute(
            EvaluatePromotionCommand(
                conclusion_id=conclusion_id,
                policy=PromotionPolicy(require_holdout_consumed=require_holdout_consumed),
                lookahead_violation=lookahead_violation,
                created_at=datetime.now(tz=UTC),
            )
        )
        _methodology_json({"promotion_evaluation": evaluation})
    finally:
        engine.dispose()


def promotion_evaluate_production(*, conclusion_id: str, lookahead_violation: bool) -> None:
    """The one canonical entry point for a real PAPER-candidate promotion
    decision: always ``APEX_PRODUCTION_PROMOTION_POLICY``
    (``require_holdout_consumed=True``), never the caller-configurable
    generic policy ``promotion-evaluate`` uses. There is no flag to weaken
    this -- a caller who genuinely wants a different (e.g. exploratory-only)
    evaluation uses ``promotion-evaluate`` instead."""

    from apex.application.research_methodology import EvaluatePromotion, EvaluatePromotionCommand
    from apex.domain.research_methodology import APEX_PRODUCTION_PROMOTION_POLICY

    engine, repository, _replay = _research_methodology_stack()
    try:
        evaluation = EvaluatePromotion(repository=repository).execute(
            EvaluatePromotionCommand(
                conclusion_id=conclusion_id,
                policy=APEX_PRODUCTION_PROMOTION_POLICY,
                lookahead_violation=lookahead_violation,
                created_at=datetime.now(tz=UTC),
            )
        )
        _methodology_json({"promotion_evaluation": evaluation})
    finally:
        engine.dispose()


def securities_genuine_strategy_dataset_build(
    *,
    raw_batch_id: str,
    derived_batch_id: str,
    universe_id: str,
    cutoff: str,
    idempotency_key: str,
) -> None:
    """Build one read-only genuine-data Strategy Lab dataset from retained evidence."""

    from apex.adapters.securities_baselines_postgres import PostgresSecuritiesBaselineRepository
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.adapters.securities_strategy_lab_dataset_postgres import (
        PostgresSecuritiesDirectionDatasetRepository,
    )
    from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
    from apex.adapters.strategy_lab_postgres import PostgresStrategyLabStore
    from apex.application.securities_strategy_lab_dataset import (
        BuildGenuineSecuritiesDailyDirectionDataset,
    )
    from apex.application.strategy_lab import StrategyLabService

    try:
        cutoff_at = datetime.fromisoformat(cutoff)
    except ValueError as error:
        raise SystemExit("cutoff must be an ISO-8601 timezone-aware timestamp") from error
    if cutoff_at.tzinfo is None:
        raise SystemExit("cutoff must be an ISO-8601 timezone-aware timestamp")
    engine, real_repository, _configuration, _adapter = _real_securities_provider_stack()
    try:
        universe = real_repository.universe(universe_id)
        if universe is None:
            raise SystemExit("real Securities universe was not found")
        evidence = PostgresSecuritiesRepository(engine)
        baselines = PostgresSecuritiesBaselineRepository(engine)
        derived_batch = evidence.get_batch(derived_batch_id)
        scorecards = tuple(
            item
            for item in baselines.scorecards()
            if item.data_batch_hash == derived_batch.canonical_hash
        )
        reports = tuple(
            item
            for item in baselines.reports()
            if item.data_batch_hash == derived_batch.canonical_hash
        )
        result = BuildGenuineSecuritiesDailyDirectionDataset(
            evidence=evidence,
            strategy_service=StrategyLabService(
                PostgresStrategyLabStore(engine), LocalStrategyLabArtifactStore()
            ),
            artifacts=LocalStrategyLabArtifactStore(),
            repository=PostgresSecuritiesDirectionDatasetRepository(engine),
        ).execute(
            raw_batch_id=raw_batch_id,
            derived_batch_id=derived_batch_id,
            universe=universe,
            cutoff_at=cutoff_at,
            idempotency_key=idempotency_key,
            baseline_scorecards=scorecards,
            simulator_reports=reports,
        )
        _strategy_lab_json(
            {
                "warning": "SECURITIES SHADOW — NO CAPITAL RESERVED",
                "dataset": result,
                "financial_authority": False,
                "candidate_created": False,
                "experiment_created": False,
            }
        )
    finally:
        engine.dispose()


def securities_genuine_candidate_evaluate(
    *,
    dataset_id: str,
    validation_run_id: str,
    idempotency_key: str,
) -> None:
    """Run one bounded, genuine-evidence candidate search and retain its blockers."""

    from dataclasses import replace

    from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime
    from apex.adapters.securities_baselines_postgres import PostgresSecuritiesBaselineRepository
    from apex.adapters.securities_strategy_lab_dataset_postgres import (
        PostgresSecuritiesDirectionDatasetRepository,
    )
    from apex.application.real_securities_data import (
        CreateSecuritiesForwardValidationPlan,
        CreateSecuritiesForwardValidationPlanCommand,
    )
    from apex.application.research_integrity import OpenResearchExperimentFamily
    from apex.domain.candidate_generation import (
        ObjectiveIdentity,
        initial_strategy_lab_search_space,
    )
    from apex.domain.real_securities_data import (
        SecuritiesHistoricalValidationRecommendation,
        SecuritiesHistoricalValidationRun,
    )
    from apex.domain.research_integrity import ResearchFrequency
    from apex.domain.values import content_digest

    runtime = CandidateGenerationRuntime.open(local_root=ROOT / ".local")
    try:
        dataset = PostgresSecuritiesDirectionDatasetRepository(runtime.engine).dataset(dataset_id)
        if dataset is None:
            raise SystemExit("genuine Securities dataset was not found")
        snapshot = runtime.strategy_store.dataset(dataset.strategy_lab_dataset_id)
        if snapshot is None or snapshot.canonical_result_hash is None:
            raise SystemExit("genuine Strategy Lab dataset snapshot is not ready")
        engine, real_repository, configuration, _adapter = _real_securities_provider_stack()
        try:
            validation = next(
                (
                    item
                    for item in real_repository.list_validations()
                    if item.validation_run_id == validation_run_id
                ),
                None,
            )
            if validation is None:
                raise SystemExit("genuine historical validation run was not found")
            if (
                validation.market_data_batch_id != dataset.derived_batch_id
                or validation.universe_id != dataset.universe_id
            ):
                raise SystemExit("genuine validation does not match the dataset lineage")
            baselines = PostgresSecuritiesBaselineRepository(engine)
            scorecards = tuple(
                item
                for item in baselines.scorecards()
                if item.scorecard_id in dataset.baseline_scorecard_ids
            )
            reports = tuple(
                item
                for item in baselines.reports()
                if item.report_id in dataset.simulator_conformance_ids
            )
            material_mismatches = tuple(
                item for item in reports if item.classification.value == "MATERIAL_MISMATCH"
            )
            if len(scorecards) != 6 or len(reports) != 6 or len(material_mismatches) != 5:
                raise SystemExit("genuine baseline/conformance evidence is incomplete")
            search_space = initial_strategy_lab_search_space()
            family = OpenResearchExperimentFamily(runtime.research_repository).execute(
                name="genuine-securities-candidate-evaluation-v0.1",
                vertical="SECURITIES",
                research_family="GENUINE_DAILY_DIRECTION_CANDIDATE_EVALUATION",
                research_question=(
                    "Do either predeclared bounded daily-direction model families retain "
                    "walk-forward Brier evidence under the exact genuine dataset?"
                ),
                target_label_identity="SECURITIES_DAILY_DIRECTION_TOTAL_RETURN_5_SESSION_V1",
                benchmark_identities=("NAIVE_PREVALENCE_BASELINE",),
                data_scope_identities=(
                    dataset.dataset_id,
                    dataset.raw_batch_id,
                    dataset.derived_batch_id,
                    dataset.universe_id,
                    *dataset.baseline_scorecard_ids,
                    *dataset.simulator_conformance_ids,
                ),
                data_scope_hashes=(
                    snapshot.canonical_result_hash,
                    dataset.raw_batch_hash,
                    dataset.derived_batch_hash,
                    dataset.universe_hash,
                    *dataset.baseline_scorecard_hashes,
                    *dataset.simulator_conformance_hashes,
                ),
                universe_hash=dataset.universe_hash,
                period_start=dataset.start_date,
                period_end=dataset.end_date,
                cutoff_at=dataset.cutoff_at,
                feature_schema_hashes=(dataset.feature_schema_hash,),
                permitted_families=(
                    "logistic-regression-probability-v1",
                    "histogram-gradient-boosting-probability-v1",
                ),
                parameter_space={
                    "parameters": search_space.payload,
                    "dataset_manifest_hash": dataset.canonical_result_hash,
                    "label_definition_hash": dataset.label_definition_hash,
                    "fold_hashes": tuple(item.canonical_hash for item in dataset.folds),
                    "holdout_group_hash": content_digest(dataset.holdout_group_ids),
                    "objective": "BRIER",
                    "conformance_report_hashes": dataset.simulator_conformance_hashes,
                },
                maximum_trials=16,
                maximum_feature_variants=1,
                maximum_model_variants=2,
                maximum_simulator_variants=3,
                evaluation_policy_id="SECURITIES_GENUINE_CANDIDATE_EVALUATION_V0_1",
                evaluation_policy_payload={
                    "objective": "BRIER",
                    "holdout": "UNTOUCHED_FINAL_ONLY",
                    "walk_forward": "FROZEN_THREE_FOLD_GROUPED_V1",
                    "minimum_psr": "0.95",
                    "minimum_dsr": "0.80",
                    "maximum_pbo": "0.25",
                },
                cost_execution_assumptions={
                    "identity": "NO_EXECUTION_COSTS_RESEARCH_SIGNAL_V1",
                    "long_signal_threshold": "0.50",
                    "authority": "NONE",
                },
                frequency=ResearchFrequency.DAILY,
                operator_action_id=f"genuine-candidate-evaluation:{idempotency_key}",
                operator_reason="bounded genuine Securities candidate evaluation",
                idempotency_key=f"{idempotency_key}:family",
            )
            plan = runtime.create_plan(
                experiment_id=family.experiment_id,
                name="genuine-securities-candidate-evaluation-v0.1",
                dataset_id=snapshot.dataset_id,
                feature_schema_id=snapshot.schema.schema_id,
                specification="strategy-lab-initial-bounded-v1",
                objective=ObjectiveIdentity.BRIER,
                maximum_trials=16,
                seed=20260728,
                reason="bounded genuine Securities candidate evaluation",
                idempotency_key=f"{idempotency_key}:search-plan",
            )
            run, decision, bindings = runtime.run_genuine_search(
                search_plan_id=plan.search_plan_id,
                requested_trials=16,
                idempotency_key=f"{idempotency_key}:search-run",
            )
            reasons = tuple(
                sorted(
                    set(
                        (
                            "MATERIAL_SIMULATOR_MISMATCH",
                            *decision.hard_gate_reason_codes,
                        )
                    )
                )
            )
            linkage_target = validation
            if validation.state == "BLOCKED_INTEGRITY":
                quality = next(
                    (
                        item
                        for item in real_repository.list_quality()
                        if item.evaluation_id == validation.quality_evaluation_id
                    ),
                    None,
                )
                universe = real_repository.universe(dataset.universe_id)
                if quality is None or universe is None:
                    raise SystemExit("genuine validation linkage evidence is unavailable")
                requested = SecuritiesHistoricalValidationRun.request(
                    configuration=configuration,
                    universe=universe,
                    quality=quality,
                    start_at=validation.start_at,
                    end_at=validation.end_at,
                    cutoff_at=validation.cutoff_at,
                    price_adjustment_state=validation.price_adjustment_state,
                    idempotency_key=f"{idempotency_key}:validation-linkage",
                    started_at=datetime.now(tz=UTC),
                )
                linkage_target = real_repository.save_reserved_validation(requested)
            linked = real_repository.transition_validation(
                replace(
                    linkage_target,
                    scorecard_ids=dataset.baseline_scorecard_ids,
                    simulator_conformance_ids=dataset.simulator_conformance_ids,
                    dataset_id=snapshot.dataset_id,
                    dataset_hash=snapshot.canonical_result_hash,
                    experiment_id=family.experiment_id,
                    experiment_hash=family.canonical_input_hash,
                    candidate_search_run_id=run.search_run_id,
                    multiple_testing_evaluation_id=decision.multiple_testing_evaluation_id,
                    state="BLOCKED_INTEGRITY",
                    recommendation=SecuritiesHistoricalValidationRecommendation.BLOCKED_SIMULATOR_MISMATCH,
                    reason_codes=reasons,
                    completed_at=datetime.now(tz=UTC),
                    canonical_result_hash=content_digest(
                        {
                            "input": linkage_target.canonical_input_hash,
                            "dataset": dataset.canonical_result_hash,
                            "experiment": family.canonical_input_hash,
                            "search": run.canonical_result_hash,
                            "recommendation": "BLOCKED_SIMULATOR_MISMATCH",
                            "reasons": reasons,
                        }
                    ),
                )
            )
            forward = CreateSecuritiesForwardValidationPlan(real_repository).execute(
                CreateSecuritiesForwardValidationPlanCommand(
                    universe_id=dataset.universe_id,
                    feature_schema_id=snapshot.schema.schema_id,
                    feature_schema_hash=snapshot.schema.canonical_hash,
                    prediction_horizon_sessions=5,
                    reason="Draft-only genuine candidate forward validation; no candidate attached",
                    idempotency_key=f"{idempotency_key}:forward-draft",
                    created_at=datetime.now(tz=UTC),
                )
            )
            _strategy_lab_json(
                {
                    "warning": "SECURITIES SHADOW — NO CAPITAL RESERVED",
                    "dataset": dataset,
                    "family": family,
                    "search_plan": plan,
                    "search_run": run,
                    "decision": decision,
                    "trial_bindings": bindings,
                    "validation": linked,
                    "forward_validation_plan": forward,
                    "financial_authority": False,
                    "deployment_authority": False,
                }
            )
        finally:
            engine.dispose()
    finally:
        runtime.close()


def strategy_intelligence_demo() -> None:
    """Materialize the read-only intelligence projection from retained genuine evidence."""

    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.adapters.securities_strategy_intelligence_postgres import (
        PostgresStrategyIntelligenceRepository,
    )
    from apex.adapters.securities_strategy_lab_dataset_postgres import (
        PostgresSecuritiesDirectionDatasetRepository,
    )
    from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
    from apex.application.securities_strategy_intelligence import (
        BuildGenuineSecuritiesStrategyIntelligence,
        StrategyIntelligenceApplicationError,
    )

    migrate()
    engine, _real_repository, _configuration, _adapter = _real_securities_provider_stack()
    try:
        datasets = PostgresSecuritiesDirectionDatasetRepository(engine).datasets()
        if not datasets:
            raise SystemExit("a retained genuine Securities dataset is required")
        dataset = datasets[-1]
        service = BuildGenuineSecuritiesStrategyIntelligence(
            evidence=PostgresSecuritiesRepository(engine),
            artifacts=LocalStrategyLabArtifactStore(),
            repository=PostgresStrategyIntelligenceRepository(engine),
        )
        key = "securities-strategy-intelligence-v1"
        run = service.execute(dataset=dataset, idempotency_key=key)
        replay = service.execute(dataset=dataset, idempotency_key=key)
        try:
            service.execute(dataset=dataset, idempotency_key=key, blocking_reasons=("CONFLICT",))
        except StrategyIntelligenceApplicationError as error:
            conflict = str(error)
        else:  # pragma: no cover - a conflicting replay is a non-negotiable proof
            raise SystemExit("strategy intelligence conflicting replay was accepted")
        _strategy_lab_json(
            {
                "warning": "HISTORICAL RESEARCH SNAPSHOT — NOT A CURRENT MARKET RECOMMENDATION",
                "run": run,
                "exact_replay": replay.run_id == run.run_id,
                "conflicting_replay_rejected": conflict,
                "provider_calls": 0,
                "brokerage_calls": 0,
                "financial_mutations": 0,
                "deployments": 0,
                "champion_assignments": 0,
                "paper_authority": False,
                "live_authority": False,
            }
        )
    finally:
        engine.dispose()


def ensemble_meta_strategy_demo() -> None:
    """Build a retained, read-only ensemble from the latest intelligence run."""
    from dataclasses import replace

    from apex.adapters.securities_ensemble_postgres import PostgresEnsembleRepository
    from apex.adapters.securities_strategy_intelligence_postgres import (
        PostgresStrategyIntelligenceRepository,
    )
    from apex.adapters.strategy_lab_artifacts import LocalStrategyLabArtifactStore
    from apex.application.securities_ensemble import (
        BuildSecuritiesEnsemble,
        EnsembleApplicationError,
    )

    migrate()
    engine, _repository, _configuration, _adapter = _real_securities_provider_stack()
    try:
        intelligence = PostgresStrategyIntelligenceRepository(engine).runs()
        if not intelligence:
            raise SystemExit("a retained Strategy Intelligence run is required")
        service = BuildSecuritiesEnsemble(
            artifacts=LocalStrategyLabArtifactStore(), repository=PostgresEnsembleRepository(engine)
        )
        run = service.execute(
            intelligence=intelligence[-1], idempotency_key="securities-ensemble-meta-strategy-v1"
        )
        replay = service.execute(
            intelligence=intelligence[-1], idempotency_key="securities-ensemble-meta-strategy-v1"
        )
        try:
            service.execute(
                intelligence=replace(intelligence[-1], canonical_result_hash="f" * 64),
                idempotency_key="securities-ensemble-meta-strategy-v1",
            )
        except EnsembleApplicationError as error:
            conflict = str(error)
        else:
            raise SystemExit("ensemble conflicting replay was accepted")
        _strategy_lab_json(
            {
                "warning": "HISTORICAL META-STRATEGY RESEARCH — NOT A CURRENT RECOMMENDATION",
                "run": run,
                "exact_replay": replay.run_id == run.run_id,
                "conflicting_replay": conflict,
                "provider_calls": 0,
                "broker_calls": 0,
                "financial_mutations": 0,
                "deployments": 0,
                "champion_assignments": 0,
                "paper_authority": False,
                "live_authority": False,
            }
        )
    finally:
        engine.dispose()


def sustained_real_shadow_demo() -> None:
    """Provider-shaped fixture proof for the observation-only lifecycle."""
    from decimal import Decimal

    from apex.application.sustained_shadow import (
        InMemorySustainedShadowRepository,
        SustainedShadowService,
    )
    from apex.domain.sustained_shadow import SustainedShadowPlan

    repo = InMemorySustainedShadowRepository()
    service = SustainedShadowService(repo)
    draft = service.create(
        SustainedShadowPlan.draft(
            universe_id="fixture-16",
            universe_hash="a" * 64,
            feature_schema_hash="b" * 64,
            intelligence_run_id="fixture-intelligence",
            ensemble_run_id="fixture-ensemble",
            blockers=("PBO_ABOVE_MAXIMUM", "MATERIAL_SIMULATOR_MISMATCH"),
            operator_action_id="fixture-operator",
            idempotency_key="sustained-shadow-fixture",
        )
    )
    active = service.activate(draft, draft.plan_id)
    rows = tuple(
        (f"ETF-{index:02d}", Decimal("0.50"), Decimal("0.01"), Decimal("0.10"), "c" * 64)
        for index in range(16)
    )
    captured = service.capture(
        active,
        session_id="fixture-xnys-completed",
        received_at=datetime.now(tz=UTC),
        capture_hash="d" * 64,
        rows=rows,
    )
    outage = service.capture(
        repo.plans[active.plan_id],
        session_id="fixture-next",
        received_at=datetime.now(tz=UTC),
        capture_hash="d" * 64,
        rows=rows[:15],
    )
    _strategy_lab_json(
        {
            "evidence_type": "PROVIDER_SHAPED_FIXTURE_NOT_LIVE",
            "plan": repo.plans[active.plan_id],
            "capture": captured,
            "outage_or_partial": outage,
            "prediction_count": len(repo.predictions(active.plan_id)),
            "outcome_state": "UNRESOLVED_HORIZON",
            "broker_calls": 0,
            "financial_orders": 0,
            "positions": 0,
            "reservations": 0,
            "ledger_mutations": 0,
            "deployments": 0,
            "champion_assignments": 0,
            "paper_authority": False,
            "live_authority": False,
        }
    )


def securities_shadow_plan_create(*, idempotency_key: str) -> None:
    from apex.adapters.securities_ensemble_postgres import PostgresEnsembleRepository
    from apex.adapters.securities_strategy_intelligence_postgres import (
        PostgresStrategyIntelligenceRepository,
    )
    from apex.adapters.securities_strategy_lab_dataset_postgres import (
        PostgresSecuritiesDirectionDatasetRepository,
    )
    from apex.adapters.sustained_shadow_postgres import PostgresSustainedShadowRepository
    from apex.application.sustained_shadow import SustainedShadowService
    from apex.domain.sustained_shadow import SustainedShadowPlan

    migrate()
    engine, real, _configuration, _adapter = _real_securities_provider_stack()
    try:
        universe = next(
            (x for x in reversed(real.list_universes()) if x.state.value == "READY"), None
        )
        datasets = PostgresSecuritiesDirectionDatasetRepository(engine).datasets()
        intelligence = PostgresStrategyIntelligenceRepository(engine).runs()
        ensemble = PostgresEnsembleRepository(engine).runs()
        # The frozen schema is a research-policy artifact; a recovered
        # observation universe may be a new immutable snapshot with the same
        # exact fixed symbols and must not be falsely represented as the old
        # dataset's universe identity.
        dataset = datasets[-1] if datasets else None
        if universe is None or dataset is None or not intelligence or not ensemble:
            raise SystemExit("retained universe, intelligence, and ensemble evidence are required")
        plan = SustainedShadowService(PostgresSustainedShadowRepository(engine)).create(
            SustainedShadowPlan.draft(
                universe_id=universe.universe_id,
                universe_hash=universe.canonical_result_hash,
                feature_schema_hash=dataset.feature_schema_hash,
                intelligence_run_id=intelligence[-1].run_id,
                ensemble_run_id=ensemble[-1].run_id,
                blockers=("PBO_ABOVE_MAXIMUM", "MATERIAL_SIMULATOR_MISMATCH"),
                operator_action_id="cli-sustained-shadow",
                idempotency_key=idempotency_key,
            )
        )
        _strategy_lab_json(
            {
                "plan": plan,
                "state": "DRAFT",
                "activation": "EXACT_PLAN_ID_CONFIRMATION_REQUIRED",
                "authority": "OBSERVATION_ONLY",
            }
        )
    finally:
        engine.dispose()


def securities_shadow_plan_activate(*, plan_id: str, confirm_plan_id: str) -> None:
    from apex.adapters.sustained_shadow_postgres import PostgresSustainedShadowRepository
    from apex.application.sustained_shadow import SustainedShadowService

    migrate()
    engine, _real, _configuration, _adapter = _real_securities_provider_stack()
    try:
        repo = PostgresSustainedShadowRepository(engine)
        plan = repo.plan(plan_id)
        if plan is None:
            raise SystemExit("sustained SHADOW plan was not found")
        _strategy_lab_json(
            {
                "plan": SustainedShadowService(repo).activate(plan, confirm_plan_id),
                "authority": "OBSERVATION_ONLY",
            }
        )
    finally:
        engine.dispose()


def securities_shadow_plan_v2_create(*, parent_plan_id: str, idempotency_key: str) -> None:
    """Create an isolated DRAFT successor without copying predictions or watermark."""
    from apex.adapters.sustained_shadow_postgres import PostgresSustainedShadowRepository
    from apex.application.sustained_shadow import SustainedShadowService
    from apex.domain.sustained_shadow import SustainedShadowPlan

    migrate()
    engine, _real, _configuration, _adapter = _real_securities_provider_stack()
    try:
        repository = PostgresSustainedShadowRepository(engine)
        parent = repository.plan(parent_plan_id)
        if parent is None:
            raise SystemExit("parent sustained SHADOW plan was not found")
        plan = SustainedShadowService(repository).create(
            SustainedShadowPlan.draft(
                universe_id=parent.universe_id,
                universe_hash=parent.universe_hash,
                feature_schema_hash=parent.feature_schema_hash,
                intelligence_run_id=parent.intelligence_run_id,
                ensemble_run_id=parent.ensemble_run_id,
                blockers=("PBO_ABOVE_MAXIMUM",),
                operator_action_id=(
                    "sustained-shadow-v2:research_multiple_testing_evaluation_"
                    "52dcac81266e634343d1330c"
                ),
                idempotency_key=idempotency_key,
                parent_plan_id=parent.plan_id,
            )
        )
        _strategy_lab_json(
            {
                "plan": plan,
                "state": "DRAFT",
                "parent_plan_id": parent.plan_id,
                "copied_predictions": 0,
                "copied_watermark": False,
                "authority": "OBSERVATION_ONLY",
            }
        )
    finally:
        engine.dispose()


def _sustained_shadow_plan(
    plan_id: str,
) -> tuple[Engine, PostgresSustainedShadowRepository, SustainedShadowPlan]:
    from apex.adapters.sustained_shadow_postgres import PostgresSustainedShadowRepository

    engine, _real, _configuration, _adapter = _real_securities_provider_stack()
    repo = PostgresSustainedShadowRepository(engine)
    plan = repo.plan(plan_id)
    if plan is None:
        engine.dispose()
        raise SystemExit("sustained SHADOW plan was not found")
    return engine, repo, plan


def securities_shadow_plan_pause(*, plan_id: str, reason: str) -> None:
    from apex.application.sustained_shadow import SustainedShadowService

    migrate()
    engine, repo, plan = _sustained_shadow_plan(plan_id)
    try:
        _strategy_lab_json(
            {
                "plan": SustainedShadowService(repo).pause(plan, reason),
                "authority": "OBSERVATION_ONLY",
            }
        )
    finally:
        engine.dispose()


def securities_shadow_plan_resume(*, plan_id: str, confirm_plan_id: str) -> None:
    from apex.application.sustained_shadow import SustainedShadowService

    migrate()
    engine, repo, plan = _sustained_shadow_plan(plan_id)
    try:
        _strategy_lab_json(
            {
                "plan": SustainedShadowService(repo).resume(plan, confirm_plan_id),
                "authority": "OBSERVATION_ONLY",
            }
        )
    finally:
        engine.dispose()


def securities_shadow_plan_revoke(*, plan_id: str, confirm_plan_id: str) -> None:
    from apex.application.sustained_shadow import SustainedShadowService

    migrate()
    engine, repo, plan = _sustained_shadow_plan(plan_id)
    try:
        _strategy_lab_json(
            {
                "plan": SustainedShadowService(repo).revoke(plan, confirm_plan_id),
                "authority": "OBSERVATION_ONLY",
            }
        )
    finally:
        engine.dispose()


def securities_shadow_status(*, plan_id: str) -> None:
    from apex.application.sustained_shadow import SustainedShadowService

    migrate()
    engine, repo, plan = _sustained_shadow_plan(plan_id)
    try:
        _strategy_lab_json(
            {
                "warning": "REAL SHADOW RECORDS WHAT APEX WOULD HAVE DECIDED — IT DOES NOT TRADE",
                "blocked_warning": "BLOCKED HISTORICAL RESEARCH REMAINS BLOCKED",
                "forward_authority_warning": (
                    "FORWARD EVIDENCE DOES NOT GRANT PAPER OR LIVE AUTHORITY"
                ),
                "report": SustainedShadowService(repo).report(plan),
            }
        )
    finally:
        engine.dispose()


def securities_shadow_resolve(*, plan_id: str, cutoff: str, emit: bool = True) -> int:
    """A one-pass resolver deliberately requires genuine return lineage, not CLI values."""
    from apex.application.sustained_shadow import SustainedShadowService

    migrate()
    engine, repo, plan = _sustained_shadow_plan(plan_id)
    try:
        parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        results = SustainedShadowService(repo).resolve(plan, cutoff=parsed, realized_returns={})
        if emit:
            _strategy_lab_json(
                {
                    "plan_id": plan.plan_id,
                    "outcomes": results,
                    "reason": "GENUINE_TOTAL_RETURN_LINEAGE_REQUIRED",
                    "authority": "OBSERVATION_ONLY",
                }
            )
        return sum(item.state.value == "RESOLVED" for item in results)
    finally:
        engine.dispose()


def securities_shadow_worker(*, plan_id: str | None, emit: bool = True) -> Any:
    """One bounded pass over one retained, completed IEX capture."""
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.adapters.securities_strategy_lab_dataset_postgres import (
        PostgresSecuritiesDirectionDatasetRepository,
    )
    from apex.application.securities_strategy_intelligence import (
        StrategyIntelligenceApplicationError,
        build_forward_strategy_intelligence_rows,
    )
    from apex.application.sustained_shadow import ShadowWorkerResult, SustainedShadowService
    from apex.domain.sustained_shadow import ShadowCaptureState, SustainedShadowState
    from apex.domain.values import content_digest

    migrate()
    engine, real, _configuration, _adapter = _real_securities_provider_stack()
    try:
        from apex.adapters.sustained_shadow_postgres import PostgresSustainedShadowRepository

        repo = PostgresSustainedShadowRepository(engine)
        plans = (repo.plan(plan_id),) if plan_id else repo.plans()
        active = tuple(x for x in plans if x is not None and x.state is SustainedShadowState.ACTIVE)
        if len(active) != 1:
            raise SystemExit("exactly one ACTIVE sustained SHADOW plan is required")
        plan = active[0]
        datasets = PostgresSecuritiesDirectionDatasetRepository(engine).datasets()
        dataset = datasets[-1] if datasets else None
        operations = tuple(
            item
            for item in real.list_operations()
            if item.universe_id == plan.universe_id
            and item.operation_type.value == "HISTORICAL_DAILY_BARS"
            and item.state.value == "COMPLETED"
            and item.end_at is not None
        )
        operation = (
            max(
                operations,
                key=lambda item: item.end_at or datetime.min.replace(tzinfo=UTC),
            )
            if operations
            else None
        )
        if dataset is None or operation is None:
            result = ShadowWorkerResult(
                plan.plan_id,
                ShadowCaptureState.PARTIAL,
                None,
                0,
                plan.watermark,
                "RETAINED_GENUINE_CAPTURE_OR_DATASET_REQUIRED",
            )
        else:
            assert operation.end_at is not None
            evidence = PostgresSecuritiesRepository(engine)
            recovered_batch_id = operation.normalized_batch_id
            if recovered_batch_id is None:
                recovered = next(
                    (
                        item
                        for item in reversed(evidence.list_batches())
                        if item.instrument_count == 16
                        and item.session_count >= 61
                        and item.dataset_name == "genuine-alpaca-iex-raw-fixed-universe-v1"
                        and item.last_period_end.date() == operation.end_at.date()
                    ),
                    None,
                )
                recovered_batch_id = None if recovered is None else recovered.batch_id
            if recovered_batch_id is None:
                result = ShadowWorkerResult(
                    plan.plan_id,
                    ShadowCaptureState.PARTIAL,
                    None,
                    0,
                    plan.watermark,
                    "RECOVERED_CANONICAL_BATCH_LINK_UNAVAILABLE",
                )
                if emit:
                    _strategy_lab_json(
                        {
                            "worker": result,
                            "plan": plan,
                            "provider_calls": 0,
                            "authority": "OBSERVATION_ONLY",
                            "financial_mutations": 0,
                        }
                    )
                return result
            _one, recovered_sessions, recovered_bars, _two = evidence.get_dataset(
                recovered_batch_id
            )
            historical_sessions, historical_bars = recovered_sessions, recovered_bars
            captured_sessions, captured_bars = recovered_sessions, recovered_bars
            target = max(captured_sessions, key=lambda item: item.session_date)
            if plan.watermark == target.session_id:
                result = ShadowWorkerResult(
                    plan.plan_id,
                    ShadowCaptureState.NO_NEW_SESSION,
                    target.session_id,
                    0,
                    plan.watermark,
                    "NO_NEW_SESSION",
                )
            else:
                try:
                    rows = build_forward_strategy_intelligence_rows(
                        dataset=dataset,
                        historical_sessions=historical_sessions,
                        historical_bars=historical_bars,
                        captured_sessions=captured_sessions,
                        captured_bars=captured_bars,
                        target_session_id=target.session_id,
                    )
                except StrategyIntelligenceApplicationError as error:
                    result = ShadowWorkerResult(
                        plan.plan_id,
                        ShadowCaptureState.PARTIAL,
                        target.session_id,
                        0,
                        plan.watermark,
                        str(error),
                    )
                else:
                    result = SustainedShadowService(repo).capture(
                        plan,
                        session_id=target.session_id,
                        received_at=datetime.now(tz=UTC),
                        capture_hash=content_digest(
                            (
                                operation.raw_artifact_set_hash,
                                evidence.get_batch(recovered_batch_id).canonical_hash,
                                "RECOVERED_CANONICAL_BATCH_LINK_V1",
                            )
                        ),
                        rows=rows,
                    )
        if emit:
            _strategy_lab_json(
                {
                    "worker": result,
                    "plan": plan,
                    "provider_calls": 0,
                    "authority": "OBSERVATION_ONLY",
                    "financial_mutations": 0,
                }
            )
        return result
    finally:
        engine.dispose()


def securities_shadow_daily(
    *, plan_id: str, no_provider: bool, keep_db_running: bool, as_json: bool
) -> None:
    """One bounded daily operation; capture remains delegated to the existing worker."""
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.adapters.sustained_shadow_operations_postgres import (
        PostgresSustainedShadowOperationsRepository,
    )
    from apex.application.sustained_shadow_operations import RunSecuritiesShadowDailyOperation
    from apex.domain.sustained_shadow_operations import SecuritiesShadowSchedulePolicy

    running = subprocess.run(
        (_docker(), "compose", "-f", str(ROOT / "compose.yaml"), "ps", "-q", "postgres"),
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    started_here = not bool(running)
    if started_here:
        db_up()
    try:
        migrate()
        engine, real, _configuration, _adapter = _real_securities_provider_stack()
        try:
            from apex.adapters.sustained_shadow_postgres import PostgresSustainedShadowRepository

            plan = PostgresSustainedShadowRepository(engine).plan(plan_id)
            if plan is None:
                raise SystemExit("sustained SHADOW plan was not found")
            operations = tuple(
                item
                for item in real.list_operations()
                if item.universe_id == plan.universe_id
                and item.operation_type.value == "HISTORICAL_DAILY_BARS"
                and item.state.value == "COMPLETED"
                and item.end_at is not None
            )
            operation = max(
                operations,
                key=lambda item: item.end_at or datetime.min.replace(tzinfo=UTC),
                default=None,
            )
            if operation is None:
                raise SystemExit("retained completed Securities operation is required")
            operation_end = operation.end_at
            if operation_end is None:
                raise SystemExit("retained completed Securities operation end is required")
            evidence = PostgresSecuritiesRepository(engine)
            batch_id = operation.normalized_batch_id
            if batch_id is None:
                batch_id = next(
                    (
                        item.batch_id
                        for item in reversed(evidence.list_batches())
                        if item.instrument_count == 16
                        and item.session_count >= 61
                        and item.dataset_name == "genuine-alpaca-iex-raw-fixed-universe-v1"
                        and item.last_period_end.date() == operation_end.date()
                    ),
                    None,
                )
            if batch_id is None:
                raise SystemExit("retained canonical Securities batch is required")
            _one, sessions, _bars, _two = evidence.get_dataset(batch_id)
            target = max(sessions, key=lambda item: item.closes_at)
            policy = SecuritiesShadowSchedulePolicy.create(
                plan_id=plan_id,
                calendar_identity=f"{target.calendar_identity}:{target.calendar_version}",
            )
        finally:
            engine.dispose()
        now = datetime.now(tz=UTC)
        operation_engine, _real, _configuration, _adapter = _real_securities_provider_stack()
        try:
            operation_result = RunSecuritiesShadowDailyOperation(
                repository=PostgresSustainedShadowOperationsRepository(operation_engine),
                worker_once=lambda: securities_shadow_worker(plan_id=plan_id, emit=False),
                resolve_once=lambda: securities_shadow_resolve(
                    plan_id=plan_id, cutoff=now.isoformat(), emit=False
                ),
            ).execute(
                policy=policy,
                session_id=target.session_id,
                due_at=policy.due_at(target.closes_at),
                owner="local-securities-shadow-daily",
                token=f"daily-{now.isoformat()}",
                now=now,
            )
        finally:
            operation_engine.dispose()
        worker = operation_result.worker
        result = {
            "plan_id": plan_id,
            "operation_job": operation_result.job,
            "recommendation": operation_result.recommendation,
            "capture_state": worker.state.value if worker is not None else None,
            "predictions_created": worker.prediction_count if worker is not None else 0,
            "watermark": worker.watermark if worker is not None else plan.watermark,
            "outcomes_resolved": operation_result.outcomes_resolved,
            "provider_requested": not no_provider,
            "provider_calls": 0,
            "authority": "OBSERVATION_ONLY",
        }
        if as_json:
            _strategy_lab_json(result)
        else:
            print(
                "daily SHADOW operation "
                f"state={result['capture_state']} predictions={result['predictions_created']} "
                f"watermark={result['watermark']} authority=OBSERVATION_ONLY"
            )
    finally:
        if started_here and not keep_db_running:
            db_down(volumes=False)


def securities_forward_plan_create(
    *,
    provider: str,
    universe_id: str,
    feature_schema_id: str,
    prediction_horizon_sessions: int,
    reason: str,
    idempotency_key: str,
) -> None:
    if provider != "ALPACA_MARKET_DATA_V2":
        raise SystemExit("only ALPACA_MARKET_DATA_V2 is supported")
    if not reason:
        raise SystemExit("an explicit forward research reason is required")
    from apex.adapters.postgres import strategy_lab_feature_schema
    from apex.application.real_securities_data import (
        CreateSecuritiesForwardValidationPlan,
        CreateSecuritiesForwardValidationPlanCommand,
        real_data_warnings,
    )

    engine, repository, _configuration, _adapter = _real_securities_provider_stack()
    try:
        with engine.connect() as connection:
            schema_hash = connection.scalar(
                sa.select(strategy_lab_feature_schema.c.canonical_hash).where(
                    strategy_lab_feature_schema.c.schema_id == feature_schema_id
                )
            )
        if schema_hash is None:
            raise SystemExit("forward plan feature schema was not found")
        plan = CreateSecuritiesForwardValidationPlan(repository).execute(
            CreateSecuritiesForwardValidationPlanCommand(
                universe_id=universe_id,
                feature_schema_id=feature_schema_id,
                feature_schema_hash=str(schema_hash),
                prediction_horizon_sessions=prediction_horizon_sessions,
                reason=reason,
                idempotency_key=idempotency_key,
                created_at=datetime.now(tz=UTC),
            )
        )
        _strategy_lab_json(
            {
                "warnings": real_data_warnings(),
                "forward_plan": plan,
                "state": "DRAFT",
                "activation": "REQUIRES_EXACT_EXISTING_OPERATOR_CONFIRMATION",
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def securities_real_validation_report(*, validation_run_id: str) -> None:
    engine, repository, configuration, _adapter = _real_securities_provider_stack()
    try:
        validation = next(
            (
                item
                for item in repository.list_validations()
                if item.validation_run_id == validation_run_id
            ),
            None,
        )
        if validation is None:
            raise SystemExit("real Securities historical validation run was not found")
        _strategy_lab_json(
            {
                **_real_provider_output(configuration=configuration, repository=repository),
                "historical_validation": validation,
                "claim": validation.historical_claim_classification.value,
                "recommendation": validation.recommendation.value,
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def securities_forward_capture(*, plan_id: str) -> None:
    """Refuse a capture unless a separately confirmed plan is ACTIVE.

    No command in this milestone activates a draft implicitly.  This preserves
    the prediction-before-outcome boundary until the existing operator
    confirmation workflow supplies an exact activation.
    """

    engine, repository, _configuration, _adapter = _real_securities_provider_stack()
    try:
        plan = next(
            (item for item in repository.list_forward_plans() if item.plan_id == plan_id), None
        )
        if plan is None:
            raise SystemExit("real Securities forward plan was not found")
        if plan.state.value != "ACTIVE":
            raise SystemExit(
                "forward capture is blocked: DRAFT plan requires exact existing operator "
                "confirmation"
            )
        raise SystemExit("forward capture requires a retained active real-data canonical batch")
    finally:
        engine.dispose()


def securities_real_data_readiness() -> None:
    engine, repository, configuration, _adapter = _real_securities_provider_stack()
    try:
        operations = repository.list_operations()
        completed = tuple(item for item in operations if item.state.value == "COMPLETED")
        _strategy_lab_json(
            {
                **_real_provider_output(configuration=configuration, repository=repository),
                "operation_count": len(operations),
                "completed_operation_count": len(completed),
                "normalized_batch_count": sum(
                    item.normalized_batch_id is not None for item in completed
                ),
                "historical_validation_count": len(repository.list_validations()),
                "forward_plan_count": len(repository.list_forward_plans()),
                "readiness": "REQUIRES_RETAINED_CALENDAR_AND_CANONICAL_BATCH",
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def real_securities_data_contract_demo() -> None:
    """Exercise the provider-shaped fixture contract without provider access.

    This is deliberately not an actual Alpaca run.  It demonstrates that the
    production normalizer requires independently retained calendar evidence and
    preserves the fixture/live distinction in its only output.
    """

    from apex.adapters.alpaca_market_data import ProviderRawPage
    from apex.adapters.alpaca_securities_normalizer import AlpacaSecuritiesEvidenceNormalizer
    from apex.adapters.securities_data import RetainedSecuritiesCalendar
    from apex.adapters.securities_synthetic import synthetic_securities_dataset

    instruments, sessions, _bars, _actions = synthetic_securities_dataset()
    instrument = instruments[0]
    session = sessions[0]
    payload = json.dumps(
        {
            "bars": {
                instrument.symbol: [
                    {
                        "t": f"{session.session_date}T00:00:00Z",
                        "o": "10.00",
                        "h": "11.00",
                        "l": "9.00",
                        "c": "10.50",
                        "v": "100",
                    }
                ]
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")
    page = ProviderRawPage(
        endpoint_id="historical_daily_bars",
        query={
            "symbols": instrument.symbol,
            "timeframe": "1Day",
            "feed": "iex",
            "adjustment": "raw",
        },
        status=200,
        headers={"x-request-id": "fixture-provider-request"},
        body=payload,
        received_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
        page_ordinal=1,
        next_page_token_hash=None,
        provider_request_id="fixture-provider-request",
        retry_delays_seconds=("0.000000",),
    )
    evidence = AlpacaSecuritiesEvidenceNormalizer().normalize_bars(
        pages=(page,),
        instruments=(instrument,),
        calendar=RetainedSecuritiesCalendar(sessions),
        calendar_identity=session.calendar_identity,
    )
    _strategy_lab_json(
        {
            "evidence_type": "PROVIDER_CONTRACT_FIXTURE_NOT_LIVE",
            "warnings": (
                "REAL MARKET DATA DOES NOT IMPLY A REAL TRADING EDGE",
                "IEX DATA IS SINGLE-VENUE COVERAGE, NOT THE FULL CONSOLIDATED MARKET",
                "RETROSPECTIVELY FETCHED CORPORATE ACTIONS ARE NOT POINT-IN-TIME ANNOUNCEMENTS",
                "HISTORICAL RESULTS DO NOT GRANT PAPER OR LIVE AUTHORITY",
            ),
            "credential_required_state": "ALPACA_CREDENTIALS_REQUIRED",
            "authenticated_fixture_capability": "READY_FIXTURE_ONLY",
            "endpoint_policy": "ALPACA_MARKET_DATA_V2_IEX_GET_ONLY_V1",
            "fixture_pagination": "BOUNDARY_TESTED",
            "fixture_rate_limit_retry": "BOUNDARY_TESTED",
            "fixture_authentication_rejection": "NO_RETRY_TESTED",
            "raw_artifact": "IMMUTABLE_AND_REDACTED_TESTED",
            "fixed_universe": "PREDECLARED_FIXED_UNIVERSE_TESTED",
            "current_asset_snapshot": "CURRENT_SNAPSHOT_ONLY_NEVER_HISTORICAL",
            "normalized_fixture_bars": len(evidence.bars),
            "derived_availability_policy": "DERIVED_EOD_AVAILABLE_V1",
            "forward_availability_policy": "OBSERVED_PROVIDER_RECEIPT_V1",
            "fixture_quality": "MISSING_EVIDENCE_BLOCKS_WITHOUT_FORWARD_FILL",
            "overlap_revision": "RETAIN_NEW_EVIDENCE_DO_NOT_OVERWRITE",
            "historical_validation": "NOT_ACTUAL_PROVIDER_EVIDENCE",
            "candidate_search": "NOT_AUTOMATICALLY_DEPLOYED",
            "forward_capture": "NO_AUTOMATIC_ACTIVATION",
            "provider_outage": "FAIL_CLOSED",
            "financial_orders": 0,
            "financial_positions": 0,
            "reservations": 0,
            "settlements": 0,
            "ledger_mutations": 0,
            "paper_authority": False,
            "live_authority": False,
        }
    )


def securities_lean_availability() -> None:
    from apex.adapters.securities_external import LeanSecuritiesEngineAdapter

    available, version = LeanSecuritiesEngineAdapter().availability()
    _strategy_lab_json(
        {
            "warning": "EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY",
            "engine": "LEAN",
            "available": available,
            "version": version,
            "executed": False,
        }
    )


def _securities_paper_repository() -> tuple[
    PostgresSecuritiesPaperRepository,
    PostgresSecuritiesPaperCampaignBoundary,
    Engine,
]:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import (
        PostgresSecuritiesPaperCampaignBoundary,
        PostgresSecuritiesPaperRepository,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        PostgresSecuritiesPaperRepository(engine),
        PostgresSecuritiesPaperCampaignBoundary(engine),
        engine,
    )


def securities_paper_run(
    *,
    campaign_id: str,
    trial_id: str,
    strategy_id: str,
    data_batch_id: str,
    start: str,
    end: str,
    idempotency_key: str,
) -> None:
    from decimal import ROUND_FLOOR, Decimal

    from apex.adapters.postgres import postgres_evidence_review_store
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.securities_paper import (
        BuildSecuritiesPaperOpportunities,
        BuildSecuritiesPaperOpportunityCommand,
        CreateSecuritiesPaperRun,
        CreateSecuritiesPaperRunCommand,
        ExecuteSecuritiesPaperOpportunityCommand,
        RunSecuritiesPaperExecution,
        complete_run,
    )
    from apex.domain.securities import SecuritiesSignalDirection
    from apex.domain.securities_paper import (
        NO_BROKER_WARNING,
        SECURITIES_PAPER_WARNING,
        SecuritiesExecutionPolicy,
    )
    from apex.domain.values import ContentHash, Quantity

    repository, campaigns, engine = _securities_paper_repository()
    stage_one = PostgresSecuritiesRepository(engine)
    review_store = postgres_evidence_review_store(engine)
    try:
        requested_at = datetime.now(tz=UTC)
        trial = review_store.reconcile_trial(
            trial_id,
            now=requested_at,
        )
        descriptor = review_store.descriptor(strategy_id)
        batch = stage_one.get_batch(data_batch_id)
        research_runs = tuple(
            item
            for item in stage_one.list_research_runs(campaign_id=campaign_id)
            if item.data_batch_id == data_batch_id
            and stage_one.get_specification(item.specification_id).strategy_id == strategy_id
        )
        if not research_runs:
            raise SystemExit(
                "No exact retained Securities research run matches the requested binding"
            )
        research_run = research_runs[-1]
        signals = tuple(
            item
            for item in stage_one.list_signals(research_run.research_run_id)
            if item.direction is SecuritiesSignalDirection.LONG
            and start <= item.signal_at.date().isoformat() <= end
        )
        _instruments, _sessions, bars, _actions = stage_one.get_dataset(batch.batch_id)
        by_instrument: dict[str, list[Any]] = defaultdict(list)
        for bar in bars:
            by_instrument[bar.instrument_id].append(bar)
        for values in by_instrument.values():
            values.sort(key=lambda item: item.period_start)
        policy = SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=requested_at)
        run = CreateSecuritiesPaperRun(
            repository=repository,
            campaigns=campaigns,
        ).execute(
            CreateSecuritiesPaperRunCommand(
                campaign_id=campaign_id,
                trial=trial,
                descriptor=descriptor,
                data_batch_id=batch.batch_id,
                data_batch_hash=ContentHash(batch.canonical_hash),
                policy=policy,
                start_date=start,
                end_date=end,
                signal_count=len(signals),
                idempotency_key=idempotency_key,
                requested_at=requested_at,
            )
        )
        builder = BuildSecuritiesPaperOpportunities(repository)
        executor = RunSecuritiesPaperExecution(
            repository=repository,
            campaigns=campaigns,
        )
        instruments = {item.instrument_id: item for item in stage_one.list_instruments()}
        results = []
        maximum = min(
            len(signals),
            max(
                0,
                trial.limits.maximum_approved_opportunities - trial.usage.approved_opportunities,
            ),
            max(
                0,
                trial.limits.maximum_paper_orders - trial.usage.paper_orders,
            ),
        )
        for ordinal, signal in enumerate(signals[:maximum]):
            eligible = next(
                (
                    item
                    for item in by_instrument.get(signal.instrument_id, ())
                    if item.period_start > signal.signal_at and item.available_at > signal.signal_at
                ),
                None,
            )
            if eligible is None:
                continue
            cash = campaigns.snapshot(campaign_id).settled_cash.amount
            raw_quantity = min(
                policy.maximum_quantity,
                cash * abs(signal.target_weight) / signal.reference_close,
            )
            quantity_value = (raw_quantity / policy.quantity_increment).to_integral_value(
                rounding=ROUND_FLOOR
            ) * policy.quantity_increment
            if quantity_value < policy.minimum_quantity:
                continue
            opportunity = builder.execute(
                BuildSecuritiesPaperOpportunityCommand(
                    run_id=run.run_id,
                    signal=signal,
                    next_bar=eligible,
                    requested_quantity=Quantity(
                        Decimal(quantity_value),
                        policy.quantity_increment,
                    ),
                    policy=policy,
                    holding_horizon_sessions=5,
                )
            )
            current_trial = review_store.reconcile_trial(
                trial.authorization_id,
                now=datetime.now(tz=UTC),
            )
            if not current_trial.active_at(signal.signal_at):
                break
            results.append(
                executor.execute(
                    ExecuteSecuritiesPaperOpportunityCommand(
                        run_id=run.run_id,
                        opportunity=opportunity,
                        signal=signal,
                        next_bar=eligible,
                        policy=policy,
                        trial=current_trial,
                        strategy_authority_decision_id=(descriptor.last_evidence_decision),
                        symbol=instruments[signal.instrument_id].symbol,
                        idempotency_key=f"{idempotency_key}:order:{ordinal}",
                        expires_at=eligible.period_end,
                    )
                )
            )
        current = repository.run(run.run_id)
        has_open_positions = any(
            item.run_id == run.run_id and item.open_quantity.value > 0
            for item in repository.positions(campaign_id)
        )
        retained_run = (
            current
            if has_open_positions
            else complete_run(
                current,
                repository=repository,
                completed_at=datetime.now(tz=UTC),
            )
        )
        _strategy_lab_json(
            {
                "warnings": (SECURITIES_PAPER_WARNING, NO_BROKER_WARNING),
                "run": retained_run,
                "execution_count": len(results),
                "status": repository.status(campaign_id),
            }
        )
    finally:
        engine.dispose()


def securities_mark(*, campaign_id: str, as_of: str) -> None:
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.securities_paper import MarkSecuritiesPositions
    from apex.domain.securities_paper import (
        NO_BROKER_WARNING,
        SECURITIES_PAPER_WARNING,
        SecuritiesExecutionPolicy,
    )

    moment = _strategy_lab_cutoff(as_of)
    repository, _campaigns, engine = _securities_paper_repository()
    stage_one = PostgresSecuritiesRepository(engine)
    try:
        policy = SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=moment)
        service = MarkSecuritiesPositions(repository)
        marks = []
        for position in repository.positions(campaign_id):
            if position.open_quantity.value == 0:
                continue
            run = repository.run(position.run_id)
            _instruments, _sessions, bars, _actions = stage_one.get_dataset(run.data_batch_id)
            eligible = tuple(
                item
                for item in bars
                if item.instrument_id == position.instrument_id
                and item.available_at <= moment
                and (position.last_mark_at is None or item.available_at > position.last_mark_at)
            )
            if eligible:
                marks.append(
                    service.execute(
                        position_id=position.position_id,
                        bar=max(eligible, key=lambda item: item.available_at),
                        policy=policy,
                        as_of=moment,
                    )
                )
        _strategy_lab_json(
            {
                "warnings": (SECURITIES_PAPER_WARNING, NO_BROKER_WARNING),
                "marks": marks,
                "status": repository.status(campaign_id),
            }
        )
    finally:
        engine.dispose()


def securities_exit(
    *,
    campaign_id: str,
    as_of: str,
    reason: str,
    idempotency_key: str,
) -> None:
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.securities_paper import (
        ExitSecuritiesPositionCommand,
        ExitSecuritiesPositions,
        complete_run,
    )
    from apex.domain.securities_paper import (
        NO_BROKER_WARNING,
        SECURITIES_PAPER_WARNING,
        SecuritiesExecutionPolicy,
        SecuritiesExitReason,
        SecuritiesPaperRunState,
    )
    from apex.domain.values import Quantity

    moment = _strategy_lab_cutoff(as_of)
    exit_reason = SecuritiesExitReason(reason)
    repository, _campaigns, engine = _securities_paper_repository()
    stage_one = PostgresSecuritiesRepository(engine)
    try:
        policy = SecuritiesExecutionPolicy.next_bar_cross_v1(created_at=moment)
        service = ExitSecuritiesPositions(repository)
        results = []
        for position in repository.positions(campaign_id):
            if position.open_quantity.value == 0:
                continue
            run = repository.run(position.run_id)
            _instruments, _sessions, bars, _actions = stage_one.get_dataset(run.data_batch_id)
            next_bar = next(
                (
                    item
                    for item in sorted(bars, key=lambda item: item.period_start)
                    if item.instrument_id == position.instrument_id and item.period_start > moment
                ),
                None,
            )
            if next_bar is None:
                continue
            results.append(
                service.execute(
                    ExitSecuritiesPositionCommand(
                        position_id=position.position_id,
                        reason=exit_reason,
                        decision_at=moment,
                        next_bar=next_bar,
                        requested_quantity=Quantity(
                            position.open_quantity.value,
                            position.open_quantity.increment,
                        ),
                        policy=policy,
                        source_identity=f"operator:{idempotency_key}",
                        idempotency_key=(f"{idempotency_key}:{position.position_id}"),
                        expires_at=next_bar.period_end,
                    )
                )
            )
        for run_id in sorted({item.decision.run_id for item in results}):
            current = repository.run(run_id)
            if current.state is SecuritiesPaperRunState.RUNNING and not any(
                item.run_id == run_id and item.open_quantity.value > 0
                for item in repository.positions(campaign_id)
            ):
                complete_run(
                    current,
                    repository=repository,
                    completed_at=datetime.now(tz=UTC),
                )
        _strategy_lab_json(
            {
                "warnings": (SECURITIES_PAPER_WARNING, NO_BROKER_WARNING),
                "exits": results,
                "status": repository.status(campaign_id),
            }
        )
    finally:
        engine.dispose()


def securities_apply_actions(*, campaign_id: str, as_of: str) -> None:
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.securities_paper import ApplySecuritiesCorporateActions
    from apex.domain.securities_paper import (
        NO_BROKER_WARNING,
        SECURITIES_PAPER_WARNING,
    )

    moment = _strategy_lab_cutoff(as_of)
    repository, _campaigns, engine = _securities_paper_repository()
    stage_one = PostgresSecuritiesRepository(engine)
    try:
        service = ApplySecuritiesCorporateActions(repository)
        results = []
        for position in repository.positions(campaign_id):
            if position.open_quantity.value == 0:
                continue
            run = repository.run(position.run_id)
            _instruments, _sessions, _bars, actions = stage_one.get_dataset(run.data_batch_id)
            for action in actions:
                if action.instrument_id == position.instrument_id and action.available_at <= moment:
                    results.append(
                        service.execute(
                            position_id=position.position_id,
                            action=action,
                            applied_at=moment,
                            quantity_increment=position.open_quantity.increment,
                        )
                    )
        _strategy_lab_json(
            {
                "warnings": (SECURITIES_PAPER_WARNING, NO_BROKER_WARNING),
                "applications": results,
                "status": repository.status(campaign_id),
            }
        )
    finally:
        engine.dispose()


def securities_paper_status(*, campaign_id: str) -> None:
    from apex.domain.securities_paper import (
        NO_BROKER_WARNING,
        SECURITIES_PAPER_WARNING,
    )

    repository, _campaigns, engine = _securities_paper_repository()
    try:
        _strategy_lab_json(
            {
                "warnings": (SECURITIES_PAPER_WARNING, NO_BROKER_WARNING),
                "status": repository.status(campaign_id),
                "positions": repository.positions(campaign_id),
            }
        )
    finally:
        engine.dispose()


def securities_paper_conformance(
    *,
    paper_run_id: str,
    external_engine: str,
) -> None:
    from apex.adapters.postgres import (
        securities_fill_evidence,
        securities_paper_order,
    )
    from apex.adapters.securities_paper_external import LeanSecuritiesPaperAdapter
    from apex.application.securities_paper import BuildSecuritiesPaperConformance
    from apex.domain.securities_paper import EXTERNAL_ENGINE_WARNING

    repository, _campaigns, engine = _securities_paper_repository()
    try:
        run = repository.run(paper_run_id)
        with engine.connect() as connection:
            order_ids = tuple(
                connection.scalars(
                    sa.select(securities_paper_order.c.order_id)
                    .where(securities_paper_order.c.run_id == paper_run_id)
                    .order_by(securities_paper_order.c.order_id)
                )
            )
            fill_ids = tuple(
                connection.scalars(
                    sa.select(securities_fill_evidence.c.fill_id)
                    .where(
                        securities_fill_evidence.c.run_id == paper_run_id,
                        securities_fill_evidence.c.quantity > 0,
                    )
                    .order_by(securities_fill_evidence.c.fill_id)
                )
            )
        events = tuple(("ORDER", str(item)) for item in order_ids) + tuple(
            ("FILL", str(item)) for item in fill_ids
        )
        adapter = LeanSecuritiesPaperAdapter()
        external = adapter.unavailable_run(
            apex_run=run,
            request_hash=run.canonical_input_hash,
        )
        report = BuildSecuritiesPaperConformance(repository).execute(
            run=run,
            apex_events=events,
            external=external,
        )
        _strategy_lab_json(
            {
                "warning": EXTERNAL_ENGINE_WARNING,
                "requested_engine": external_engine,
                "external_run": external,
                "report": report,
            }
        )
    finally:
        engine.dispose()


def securities_paper_demo() -> None:
    from apex.adapters.securities_paper_synthetic import run_securities_paper_demo

    _strategy_lab_json(run_securities_paper_demo())


def securities_baselines() -> None:
    from apex.domain.securities_baselines import (
        BASELINE_RESEARCH_WARNING,
        CANONICAL_BASELINE_WARNING,
        canonical_securities_baselines,
    )

    _strategy_lab_json(
        {
            "warnings": (CANONICAL_BASELINE_WARNING, BASELINE_RESEARCH_WARNING),
            "baselines": canonical_securities_baselines(datetime.now(tz=UTC)),
            "financial_authority": False,
        }
    )


def _securities_baseline_runtime() -> tuple[Any, Any, Any, tuple[Any, ...]]:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_baseline_simulators import (
        ApexPaperSimulatorAdapter,
        ApexResearchSimulatorAdapter,
        DeterministicConformanceFixtureAdapter,
        LeanCanonicalSecuritiesAdapter,
        VectorbtSecuritiesAdapter,
    )
    from apex.adapters.securities_baselines_postgres import (
        PostgresSecuritiesBaselineRepository,
    )
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository

    engine = create_postgres_engine(PostgresSettings.from_env())
    simulators = (
        ApexResearchSimulatorAdapter(),
        ApexPaperSimulatorAdapter(),
        VectorbtSecuritiesAdapter(ROOT / ".local" / "securities" / "simulator-results"),
        LeanCanonicalSecuritiesAdapter(),
        DeterministicConformanceFixtureAdapter(),
    )
    return (
        PostgresSecuritiesBaselineRepository(engine),
        PostgresSecuritiesRepository(engine),
        engine,
        simulators,
    )


def securities_baseline_run(
    *,
    baseline_id: str,
    campaign_id: str,
    data_batch_id: str,
    start: str,
    end: str,
    simulator: str,
    idempotency_key: str,
) -> None:
    from apex.adapters.securities_postgres import PostgresSecuritiesCampaignBoundary
    from apex.application.securities_baselines import (
        RegisterCanonicalSecuritiesBaselines,
        RunCanonicalSecuritiesBaseline,
        RunCanonicalSecuritiesBaselineCommand,
    )
    from apex.domain.securities_baselines import SecuritiesSimulatorIdentity

    repository, data, engine, simulators = _securities_baseline_runtime()
    try:
        PostgresSecuritiesCampaignBoundary(engine).assert_research_allowed(campaign_id)
        RegisterCanonicalSecuritiesBaselines(repository).execute(created_at=datetime.now(tz=UTC))
        _instruments, _sessions, bars, _actions = data.get_dataset(data_batch_id)
        universe = tuple(sorted({item.instrument_id for item in bars}))
        now = datetime.now(tz=UTC)
        run = RunCanonicalSecuritiesBaseline(
            repository=repository,
            data=data,
            simulators=simulators,
        ).execute(
            RunCanonicalSecuritiesBaselineCommand(
                campaign_id=campaign_id,
                baseline_id=baseline_id,
                data_batch_id=data_batch_id,
                universe=universe,
                start_date=start,
                end_date=end,
                cutoff=max(item.available_at for item in bars),
                simulator=SecuritiesSimulatorIdentity(simulator),
                idempotency_key=idempotency_key,
                started_at=now,
                completed_at=now,
            )
        )
        _strategy_lab_json(run)
    finally:
        engine.dispose()


def securities_reconcile_retained_request(*, report_id: str, idempotency_key: str) -> None:
    """Run one corrected vectorbt replacement from a retained canonical request."""
    from apex.application.securities_baselines import (
        ReconcileSecuritiesSimulationFromRetainedRequest,
    )
    from apex.domain.securities_baselines import SecuritiesSimulatorIdentity

    repository, data, engine, simulators = _securities_baseline_runtime()
    try:
        simulator = next(
            item
            for item in simulators
            if item.simulator_identity is SecuritiesSimulatorIdentity.VECTORBT
        )
        run, report = ReconcileSecuritiesSimulationFromRetainedRequest(
            repository=repository, data=data, simulator=simulator
        ).execute(report_id=report_id, idempotency_key=idempotency_key, now=datetime.now(tz=UTC))
        original, request = repository.retained_request(report_id)
        _strategy_lab_json(
            {
                "retained_request_hash": request.canonical_hash,
                "reference_apex_run": original.reference_run_id,
                "original_report_id": report_id,
                "original_vectorbt_run": original.comparison_run_id,
                "replacement_vectorbt_run": run.simulation_run_id,
                "replacement_report": report.report_id,
                "classification": report.classification.value,
                "remaining_differences": report.reason_codes,
                "financial_authority": False,
            }
        )
    finally:
        engine.dispose()


def securities_baseline_suite(
    *,
    campaign_id: str,
    data_batch_id: str,
    start: str,
    end: str,
    simulator_names: str,
    idempotency_key: str,
) -> None:
    from apex.adapters.securities_postgres import PostgresSecuritiesCampaignBoundary
    from apex.application.securities_baselines import (
        CompareCanonicalSecuritiesSimulators,
        RegisterCanonicalSecuritiesBaselines,
        RunCanonicalSecuritiesBaseline,
        RunCanonicalSecuritiesBaselineCommand,
    )
    from apex.domain.securities_baselines import (
        CanonicalSecuritiesSimulationRequest,
        SecuritiesSimulationState,
        SecuritiesSimulatorIdentity,
        build_baseline_scorecard,
        research_trial_summary,
        scorecard_conformance,
    )

    repository, data, engine, simulators = _securities_baseline_runtime()
    try:
        PostgresSecuritiesCampaignBoundary(engine).assert_research_allowed(campaign_id)
        baselines = RegisterCanonicalSecuritiesBaselines(repository).execute(
            created_at=datetime.now(tz=UTC)
        )
        _instruments, _sessions, bars, _actions = data.get_dataset(data_batch_id)
        universe = tuple(sorted({item.instrument_id for item in bars}))
        cutoff = max(item.available_at for item in bars)
        selected = tuple(
            SecuritiesSimulatorIdentity(item.strip())
            for item in simulator_names.split(",")
            if item.strip()
        )
        if not selected:
            raise SystemExit("At least one simulator identity is required")
        now = datetime.now(tz=UTC)
        service = RunCanonicalSecuritiesBaseline(
            repository=repository,
            data=data,
            simulators=simulators,
        )
        runs = []
        requests = {}
        for baseline in baselines:
            request = CanonicalSecuritiesSimulationRequest.create(
                baseline=baseline,
                data_batch_id=data_batch_id,
                data_batch_hash=data.get_batch(data_batch_id).canonical_hash,
                universe=universe,
                start_date=start,
                end_date=end,
                cutoff=cutoff,
            )
            requests[baseline.baseline_id] = request
            for simulator_identity in selected:
                runs.append(
                    service.execute(
                        RunCanonicalSecuritiesBaselineCommand(
                            campaign_id=campaign_id,
                            baseline_id=baseline.baseline_id,
                            data_batch_id=data_batch_id,
                            universe=universe,
                            start_date=start,
                            end_date=end,
                            cutoff=cutoff,
                            simulator=simulator_identity,
                            idempotency_key=(
                                f"{idempotency_key}:{baseline.family.value}:"
                                f"{simulator_identity.value}"
                            ),
                            started_at=now,
                            completed_at=now,
                        )
                    )
                )
        comparer = CompareCanonicalSecuritiesSimulators(repository)
        reports = []
        for baseline in baselines:
            scoped = [item for item in runs if item.baseline_id == baseline.baseline_id]
            reference = next(
                (
                    item
                    for item in scoped
                    if item.simulator_identity is SecuritiesSimulatorIdentity.APEX_RESEARCH
                ),
                None,
            )
            if reference is None:
                continue
            for comparison in scoped:
                if comparison is not reference:
                    reports.append(
                        comparer.execute(
                            reference=reference,
                            comparison=comparison,
                        )
                    )
            if reference.state is SecuritiesSimulationState.COMPLETED:
                conformance_state, integrity_warnings = scorecard_conformance(
                    tuple(
                        report for report in reports if report.baseline_id == baseline.baseline_id
                    )
                )
                scorecard = build_baseline_scorecard(
                    baseline=baseline,
                    request=requests[baseline.baseline_id],
                    run=reference,
                    conformance_state=conformance_state,
                    integrity_warnings=integrity_warnings,
                )
                repository.save_scorecard(scorecard)
        _strategy_lab_json(
            {
                "runs": runs,
                "reports": reports,
                "scorecards": repository.scorecards(),
                "trial_ledger": research_trial_summary(
                    repository.runs(), baselines, repository.reports()
                ),
            }
        )
    finally:
        engine.dispose()


def securities_multi_simulator_conformance(
    *,
    baseline_id: str,
    data_batch_id: str,
    start: str,
    end: str,
) -> None:
    repository, _data, engine, _simulators = _securities_baseline_runtime()
    try:
        runs = tuple(
            item
            for item in repository.runs()
            if item.baseline_id == baseline_id
            and item.data_batch_id == data_batch_id
            and item.start_date == start
            and item.end_date == end
        )
        _strategy_lab_json(
            {
                "runs": runs,
                "reports": tuple(
                    item for item in repository.reports() if item.baseline_id == baseline_id
                ),
            }
        )
    finally:
        engine.dispose()


def securities_baseline_scorecards(*, data_batch_id: str) -> None:
    repository, _data, engine, _simulators = _securities_baseline_runtime()
    try:
        _strategy_lab_json(
            tuple(item for item in repository.scorecards() if item.data_batch_id == data_batch_id)
        )
    finally:
        engine.dispose()


def securities_simulator_status() -> None:
    from apex.adapters.securities_baselines_synthetic import (
        securities_simulator_status as status,
    )

    _strategy_lab_json(status())


def securities_baselines_conformance_demo() -> None:
    from apex.adapters.securities_baselines_synthetic import (
        run_securities_baselines_conformance_demo,
    )

    _strategy_lab_json(run_securities_baselines_conformance_demo())


def securities_research_trial_summary() -> None:
    from apex.adapters.securities_baselines_synthetic import (
        run_securities_baselines_conformance_demo,
    )

    result = run_securities_baselines_conformance_demo()
    _strategy_lab_json(result["trial_ledger"])


def _research_integrity_runtime() -> tuple[Any, Any, Any]:
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_integrity_artifacts import (
        LocalResearchReturnArtifactStore,
    )
    from apex.adapters.research_integrity_postgres import (
        PostgresResearchIntegrityRepository,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    return (
        PostgresResearchIntegrityRepository(engine),
        LocalResearchReturnArtifactStore(),
        engine,
    )


def research_experiment_create(
    *,
    name: str,
    vertical: str,
    data_batch_id: str,
    universe_id: str,
    start: str,
    end: str,
    maximum_trials: int,
    specification: str,
    reason: str,
    idempotency_key: str,
) -> None:
    from apex.adapters.securities_postgres import PostgresSecuritiesRepository
    from apex.application.research_integrity import OpenResearchExperimentFamily
    from apex.domain.research_integrity import ResearchFrequency
    from apex.domain.values import content_digest

    if specification != "canonical-securities-baselines-v1":
        raise SystemExit("specification must be the managed ID canonical-securities-baselines-v1")
    repository, _artifacts, engine = _research_integrity_runtime()
    try:
        batch = PostgresSecuritiesRepository(engine).get_batch(data_batch_id)
        family = OpenResearchExperimentFamily(repository).execute(
            name=name,
            vertical=vertical,
            research_family="CANONICAL_SECURITIES_BASELINES",
            research_question=(
                "Which predeclared canonical Securities baseline remains robust "
                "after costs and research-selection controls?"
            ),
            target_label_identity="daily-total-return-v1",
            benchmark_identities=("CASH", "BUY_AND_HOLD"),
            data_scope_identities=(data_batch_id,),
            data_scope_hashes=(batch.canonical_hash,),
            universe_hash=content_digest({"managed_universe_id": universe_id}),
            period_start=start,
            period_end=end,
            cutoff_at=datetime.now(tz=UTC),
            feature_schema_hashes=(),
            permitted_families=(
                "CASH",
                "BUY_AND_HOLD",
                "EQUAL_WEIGHT_REBALANCE",
                "TIME_SERIES_MOMENTUM",
                "CROSS_SECTIONAL_MOMENTUM",
                "SHORT_HORIZON_MEAN_REVERSION",
                "PAIRS_DISTANCE_MEAN_REVERSION",
                "VOLATILITY_TARGETED_LONG_ONLY",
            ),
            parameter_space={
                "managed_specification": specification,
                "baseline_specification": "eight-canonical-v1",
                "simulator": ("APEX_RESEARCH", "VECTORBT", "LEAN"),
            },
            maximum_trials=maximum_trials,
            maximum_feature_variants=1,
            maximum_model_variants=min(8, maximum_trials),
            maximum_simulator_variants=min(3, maximum_trials),
            evaluation_policy_id="RESEARCH_SELECTION_INTEGRITY_POLICY_V1",
            evaluation_policy_payload={
                "minimum_psr": "0.95",
                "minimum_dsr": "0.80",
                "maximum_pbo": "0.25",
            },
            cost_execution_assumptions={
                "source": "canonical-securities-baseline-request",
                "alignment": "exact",
            },
            frequency=ResearchFrequency.DAILY,
            operator_action_id=f"cli:{idempotency_key}",
            operator_reason=reason,
            idempotency_key=idempotency_key,
        )
        _strategy_lab_json(family)
    finally:
        engine.dispose()


def research_overfitting_evaluate(*, experiment_id: str, cutoff: str) -> None:
    from apex.application.research_integrity import ResearchIntegrityService

    repository, artifacts, engine = _research_integrity_runtime()
    try:
        evaluation = ResearchIntegrityService(repository, artifacts).evaluate(
            experiment_id=experiment_id,
            cutoff_at=datetime.fromisoformat(cutoff),
        )
        _strategy_lab_json(evaluation)
    finally:
        engine.dispose()


def research_overfitting_report(*, experiment_id: str) -> None:
    repository, _artifacts, engine = _research_integrity_runtime()
    try:
        family = repository.family(experiment_id)
        if family is None:
            raise SystemExit("research experiment family was not found")
        trials = repository.trials(experiment_id)
        _strategy_lab_json(
            {
                "warnings": (
                    "BEST OBSERVED RESULT MAY REFLECT RESEARCH SELECTION",
                    "RESEARCH ELIGIBILITY IS NOT STRATEGY OR CAPITAL AUTHORITY",
                ),
                "family": family,
                "budget": {
                    "declared": family.maximum_trials,
                    "consumed": len(trials),
                    "remaining": family.maximum_trials - len(trials),
                },
                "trials": trials,
                "selection_events": repository.selections(experiment_id),
                "evaluations": repository.evaluations(experiment_id),
            }
        )
    finally:
        engine.dispose()


def research_trials(*, experiment_id: str) -> None:
    repository, _artifacts, engine = _research_integrity_runtime()
    try:
        trials = repository.trials(experiment_id)
        _strategy_lab_json(
            {
                "experiment_id": experiment_id,
                "trial_count": len(trials),
                "items": tuple(
                    {
                        "trial": item,
                        "metric": repository.metric(item.trial_id),
                        "artifacts": repository.artifacts(item.trial_id),
                    }
                    for item in trials
                ),
            }
        )
    finally:
        engine.dispose()


def research_overfitting_demo() -> None:
    from apex.adapters.research_integrity_demo import research_overfitting_demo as run

    _strategy_lab_json(run())


def research_integrity_readiness() -> None:
    """Report retained readiness without provider access or winner selection."""

    simulator_root = ROOT / ".local" / "securities" / "simulator-results"
    strategy_lab_root = ROOT / ".local" / "strategy_lab" / "artifacts"
    return_root = ROOT / ".local" / "research-integrity" / "return-series"
    managed_counts = {
        "securities_simulator_artifacts": len(tuple(simulator_root.rglob("*.json")))
        if simulator_root.exists()
        else 0,
        "strategy_lab_artifacts": len(tuple(strategy_lab_root.rglob("*.*")))
        if strategy_lab_root.exists()
        else 0,
        "research_return_series_artifacts": len(tuple(return_root.rglob("*.jsonl")))
        if return_root.exists()
        else 0,
    }
    database_state = "STOPPED_OR_UNAVAILABLE"
    family_groupings: tuple[object, ...] = ()
    trial_count = 0
    blocked_count = 0
    unavailable_count = 0
    engine = None
    try:
        repository, _artifacts, engine = _research_integrity_runtime()
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
        database_state = "AVAILABLE"
        families = repository.families()
        family_groupings = tuple(
            {
                "experiment_id": item.experiment_id,
                "vertical": item.vertical,
                "research_family": item.research_family,
                "frequency": item.frequency.value,
                "trial_count": len(repository.trials(item.experiment_id)),
            }
            for item in families
        )
        retained = tuple(
            trial for family in families for trial in repository.trials(family.experiment_id)
        )
        trial_count = len(retained)
        blocked_count = sum(item.integrity_blocked for item in retained)
        unavailable_count = sum(
            item.failure_reason is not None and "UNAVAILABLE" in item.failure_reason
            for item in retained
        )
    except Exception as error:
        database_state = f"STOPPED_OR_UNAVAILABLE:{type(error).__name__}"
    finally:
        if engine is not None:
            engine.dispose()
    aligned_artifacts = managed_counts["research_return_series_artifacts"]
    _strategy_lab_json(
        {
            "warning": "RESEARCH ELIGIBILITY IS NOT STRATEGY OR CAPITAL AUTHORITY",
            "database_state": database_state,
            "candidate_experiment_family_groupings": family_groupings,
            "retained_trial_count": trial_count,
            "unavailable_trials": unavailable_count,
            "integrity_blocked_trials": blocked_count,
            "managed_artifact_inventory": managed_counts,
            "supports_psr": trial_count > 0 and aligned_artifacts > 0,
            "supports_dsr": trial_count >= 2 and aligned_artifacts > 0,
            "supports_effective_trial_count": trial_count >= 2 and aligned_artifacts > 0,
            "supports_cscv_pbo": trial_count >= 3 and aligned_artifacts > 0,
            "missing_evidence": (
                "PostgreSQL must be available to inspect durable experiment families"
                if database_state != "AVAILABLE"
                else "Compatible aligned return evidence may still be absent"
            ),
            "actual_integrity_decision_possible": database_state == "AVAILABLE"
            and trial_count >= 3
            and aligned_artifacts > 0,
            "winner_selected_or_promoted": False,
        }
    )


def research_trial_ledger_summary() -> None:
    return_root = ROOT / ".local" / "research-integrity" / "return-series"
    _strategy_lab_json(
        {
            "warning": "TESTING MORE VARIANTS INCREASES THE CHANCE OF A FALSE WINNER",
            "managed_return_artifact_count": len(tuple(return_root.rglob("*.jsonl")))
            if return_root.exists()
            else 0,
            "database_required_for_durable_trial_rows": True,
            "trial_deletion_supported": False,
            "failed_trials_hidden_by_default": False,
            "authority_granted": False,
        }
    )


def research_operations_command(arguments: argparse.Namespace) -> None:
    """Exact-ID governed research reads and explicit agenda materialization."""

    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.research_operations_postgres import (
        PostgresResearchOperationsRepository,
        PostgresRetainedResearchKnowledgeSource,
    )
    from apex.adapters.research_operations_sources import RetainedResearchAgendaSource
    from apex.application.research_operations import (
        CancelQueuedResearchExperiment,
        EnqueueResearchExperiment,
        GenerateResearchProposals,
        RefreshResearchAgenda,
        research_operations_workspace,
        research_review_queue,
    )
    from apex.domain.research_operations import ResearchProposalOrigin

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresResearchOperationsRepository(engine)
    try:
        if arguments.command == "research-operations-agenda-refresh":
            as_of = datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00"))
            result = RefreshResearchAgenda(repository).execute(
                facts=RetainedResearchAgendaSource(engine).facts(as_of=as_of)
            )
            value: object = {
                "agenda_items": result.items,
                "replayed_item_ids": result.replayed_item_ids,
                "proposals_created": 0,
                "proposals_approved": 0,
                "experiments_run": 0,
                "authority": "NONE",
                "live_authority": "NONE",
            }
        elif arguments.command == "research-operations-proposals-generate":
            as_of = datetime.fromisoformat(arguments.as_of.replace("Z", "+00:00"))
            source = RetainedResearchAgendaSource(engine)
            generated_result = GenerateResearchProposals(
                repository, PostgresRetainedResearchKnowledgeSource(engine)
            ).execute(
                generator_run_id=arguments.generator_run_id,
                generated_at=as_of,
                actor="operator:cli",
                context=source.generation_context(agenda_items=repository.agenda_items()),
                maximum_proposals=arguments.maximum_proposals,
                origin=ResearchProposalOrigin.SYSTEM_HEURISTIC,
            )
            value = {
                "generator_run": generated_result.generator_run,
                "proposals": generated_result.proposals,
                "priorities": generated_result.priorities,
                "feasibilities": generated_result.feasibilities,
                "idempotent_replay": generated_result.idempotent_replay,
                "proposals_approved": 0,
                "experiments_run": 0,
                "provider_requests": 0,
                "paper_authority": "NONE",
                "live_authority": "NONE",
            }
        elif arguments.command == "research-operations-generator-run-inspect":
            value = repository.generator_run(arguments.generator_run_id)
            if value is None:
                raise SystemExit("RESEARCH_GENERATOR_RUN_NOT_FOUND")
        elif arguments.command == "research-operations-priority-inspect":
            value = repository.proposal_priority(arguments.proposal_id)
            if value is None:
                raise SystemExit("RESEARCH_PROPOSAL_PRIORITY_NOT_FOUND")
        elif arguments.command == "research-operations-feasibility-inspect":
            value = repository.proposal_feasibility(arguments.proposal_id)
            if value is None:
                raise SystemExit("RESEARCH_PROPOSAL_FEASIBILITY_NOT_FOUND")
        elif arguments.command == "research-operations-review-queue":
            value = research_review_queue(
                repository, PostgresRetainedResearchKnowledgeSource(engine)
            )
        elif arguments.command == "research-operations-experiment-enqueue":
            value = EnqueueResearchExperiment(repository).execute(
                queue_id=arguments.queue_id,
                proposal_id=arguments.proposal_id,
                run_id=arguments.run_id,
                actor="operator:cli",
                idempotency_key=arguments.idempotency_key,
                enqueued_at=datetime.now(tz=UTC),
            )
        elif arguments.command == "research-operations-queue-inspect":
            item = repository.queue_item(arguments.queue_id)
            if item is None:
                raise SystemExit("RESEARCH_QUEUE_ITEM_NOT_FOUND")
            value = {"item": item, "events": repository.queue_events(arguments.queue_id)}
        elif arguments.command == "research-operations-queue-cancel":
            value = CancelQueuedResearchExperiment(repository).execute(
                queue_id=arguments.queue_id,
                actor="operator:cli",
                reason=arguments.reason,
                idempotency_key=arguments.idempotency_key,
                canceled_at=datetime.now(tz=UTC),
            )
        elif arguments.command == "research-operations-queue-execute":
            from apex.adapters.counterfactual_outcome_postgres import (
                PostgresCounterfactualOutcomeRepository,
            )
            from apex.adapters.first_generated_research_method_sources import (
                PostgresCurrentOpportunityOperationalEvidenceSource,
            )
            from apex.adapters.research_integrity_postgres import (
                PostgresResearchIntegrityRepository,
            )
            from apex.adapters.research_operations_executor import (
                ExistingResearchLedgerExecutor,
                FirstGeneratedOperationalMethodExecutor,
                GovernedResearchExecutor,
                RetainedOutcomeAggregateExperimentExecutor,
            )
            from apex.adapters.securities_structure_outcome_postgres import (
                PostgresSecuritiesStructureOutcomeRepository,
            )
            from apex.application.research_operations import ExecuteApprovedResearchQueueItem

            value = ExecuteApprovedResearchQueueItem(
                repository,
                GovernedResearchExecutor(
                    ledger=ExistingResearchLedgerExecutor(
                        PostgresResearchIntegrityRepository(engine)
                    ),
                    operational=FirstGeneratedOperationalMethodExecutor(
                        repository,
                        PostgresCurrentOpportunityOperationalEvidenceSource(engine),
                    ),
                    outcome_aggregate=RetainedOutcomeAggregateExperimentExecutor(
                        counterfactual_outcome=PostgresCounterfactualOutcomeRepository(engine),
                        securities_structure_outcome=(
                            PostgresSecuritiesStructureOutcomeRepository(engine)
                        ),
                    ),
                ),
                PostgresRetainedResearchKnowledgeSource(engine),
            ).execute(
                queue_id=arguments.queue_id,
                actor="operator:cli",
                idempotency_key=arguments.idempotency_key,
                executed_at=datetime.now(tz=UTC),
            )
        elif arguments.command == "research-operations-recommendation-inspect":
            value = next(
                (
                    item
                    for item in repository.recommendations()
                    if item.recommendation_id == arguments.recommendation_id
                ),
                None,
            )
            if value is None:
                raise SystemExit("RESEARCH_RECOMMENDATION_NOT_FOUND")
        elif arguments.command == "research-operations-proposal-inspect":
            proposal = repository.proposal(arguments.proposal_id)
            if proposal is None:
                raise SystemExit("RESEARCH_PROPOSAL_NOT_FOUND")
            workspace = research_operations_workspace(
                repository, PostgresRetainedResearchKnowledgeSource(engine)
            )
            value = next(
                item
                for item in cast(tuple[dict[str, object], ...], workspace["proposals"])
                if cast(Any, item["proposal"]).proposal_id == arguments.proposal_id
            )
        else:
            value = research_operations_workspace(
                repository, PostgresRetainedResearchKnowledgeSource(engine)
            )
        _strategy_lab_json(value)
    finally:
        engine.dispose()


def research_operations_isolated_acceptance() -> None:
    from apex.adapters.research_operations_acceptance import (
        run_isolated_research_queue_acceptance,
    )

    _strategy_lab_json(run_isolated_research_queue_acceptance())


def first_generated_research_method_acceptance() -> None:
    from apex.adapters.first_generated_research_method_acceptance import (
        run_genuine_first_generated_method_acceptance,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _strategy_lab_json(run_genuine_first_generated_method_acceptance(engine))
    finally:
        engine.dispose()


def generated_crypto_challenger_acceptance() -> None:
    from apex.adapters.generated_crypto_challenger_acceptance import (
        run_generated_crypto_challenger_acceptance,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _strategy_lab_json(run_generated_crypto_challenger_acceptance(engine))
    finally:
        engine.dispose()


def secure_live_readiness_acceptance() -> None:
    from apex.adapters.live_readiness_acceptance import (
        run_genuine_live_readiness_acceptance,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _research_evidence_json(run_genuine_live_readiness_acceptance(engine))
    finally:
        engine.dispose()


def secure_live_readiness_fixture_acceptance() -> None:
    from apex.adapters.live_readiness_acceptance import (
        run_fixture_live_readiness_acceptance,
    )

    _research_evidence_json(run_fixture_live_readiness_acceptance())


def operational_reliability_acceptance() -> None:
    from apex.adapters.operational_reliability_acceptance import (
        run_genuine_operational_reliability_acceptance,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    settings = PostgresSettings.from_env()
    engine = create_postgres_engine(settings)
    try:
        _research_evidence_json(
            run_genuine_operational_reliability_acceptance(
                engine,
                settings=settings,
                repository_root=ROOT,
            )
        )
    finally:
        engine.dispose()


def operational_reliability_fixture_acceptance() -> None:
    from apex.adapters.operational_reliability_acceptance import (
        run_fixture_operational_failure_acceptance,
    )

    _research_evidence_json(run_fixture_operational_failure_acceptance())


def operational_reliability_workspace() -> None:
    from apex.adapters.operational_reliability_postgres import (
        PostgresOperationalReliabilityRepository,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.operational_reliability import operational_workspace

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _research_evidence_json(
            operational_workspace(PostgresOperationalReliabilityRepository(engine))
        )
    finally:
        engine.dispose()


def coinbase_read_only_credentials_absent_acceptance() -> None:
    from apex.adapters.coinbase_read_only_acceptance import (
        run_genuine_coinbase_credentials_absent_acceptance,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _research_evidence_json(run_genuine_coinbase_credentials_absent_acceptance(engine))
    finally:
        engine.dispose()


def coinbase_read_only_workspace() -> None:
    from apex.adapters.coinbase_read_only_postgres import PostgresCoinbaseReadOnlyRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.coinbase_read_only import coinbase_read_only_workspace as workspace

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _research_evidence_json(workspace(PostgresCoinbaseReadOnlyRepository(engine)))
    finally:
        engine.dispose()


def kalshi_read_only_credentials_absent_acceptance() -> None:
    from apex.adapters.kalshi_read_only_acceptance import (
        run_genuine_kalshi_credentials_absent_acceptance,
    )
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _research_evidence_json(run_genuine_kalshi_credentials_absent_acceptance(engine))
    finally:
        engine.dispose()


def kalshi_read_only_workspace() -> None:
    from apex.adapters.kalshi_read_only_postgres import PostgresKalshiReadOnlyRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.kalshi_read_only import kalshi_read_only_workspace as workspace

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _research_evidence_json(workspace(PostgresKalshiReadOnlyRepository(engine)))
    finally:
        engine.dispose()


def live_preflight_genuine_acceptance() -> None:
    from apex.adapters.live_preflight_acceptance import run_genuine_zero_intent_acceptance
    from apex.adapters.live_preflight_postgres import PostgresLivePreflightRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _research_evidence_json(
            run_genuine_zero_intent_acceptance(PostgresLivePreflightRepository(engine))
        )
    finally:
        engine.dispose()


def live_preflight_fixture_acceptance() -> None:
    from apex.adapters.live_preflight_acceptance import run_fixture_live_preflight_acceptance

    _research_evidence_json(run_fixture_live_preflight_acceptance())


def live_preflight_workspace() -> None:
    from apex.adapters.live_preflight_postgres import PostgresLivePreflightRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.live_preflight import live_preflight_workspace as workspace

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        _research_evidence_json(
            workspace(
                PostgresLivePreflightRepository(engine),
                as_of=datetime.now(tz=UTC),
            )
        )
    finally:
        engine.dispose()


def _live_readiness_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SystemExit("LIVE_READINESS_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return parsed.astimezone(UTC)


def live_readiness_command(arguments: argparse.Namespace) -> None:
    from apex.adapters.live_readiness_postgres import PostgresLiveReadinessRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.live_readiness import (
        CredentialsAbsentCapabilityInspector,
        ExportLiveReadinessEvidence,
        InspectProviderCapability,
        MaterializeLiveReadiness,
        RegisterCredentialReference,
        SetExecutionControl,
        live_readiness_workspace,
        safe_json_payload,
    )
    from apex.domain.live_readiness import (
        AccountEnvironment,
        ExecutionControlState,
        LiveReadinessInputs,
        LiveRiskPolicy,
        PermissionClass,
        Provider,
        SecretBackend,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    repository = PostgresLiveReadinessRepository(engine)
    try:
        command = arguments.command
        value: object
        if command == "credential-reference-register":
            value = RegisterCredentialReference(repository).execute(
                reference_id=arguments.reference_id,
                version=arguments.version,
                provider=Provider(arguments.provider),
                account_environment=AccountEnvironment(arguments.environment),
                account_purpose=arguments.account_purpose,
                required_permissions=tuple(PermissionClass(item) for item in arguments.permission),
                secret_backend=SecretBackend(arguments.secret_backend),
                backend_reference_id=arguments.backend_reference_id,
                actor=arguments.actor,
                reason=arguments.reason,
                confirmation=arguments.confirmation,
                idempotency_key=arguments.idempotency_key,
                created_at=_live_readiness_time(arguments.at),
                previous_reference_id=arguments.previous_reference_id,
            )
        elif command == "credential-reference-inspect":
            value = repository.credential_reference(arguments.reference_id)
        elif command == "provider-capability-inspect":
            provider = Provider(arguments.provider)
            value = InspectProviderCapability(
                repository, CredentialsAbsentCapabilityInspector(provider)
            ).execute(
                snapshot_id=arguments.snapshot_id,
                account_scope_id=arguments.account_scope_id,
                account_environment=AccountEnvironment(arguments.environment),
                credential_reference_id=arguments.reference_id,
                inspected_at=_live_readiness_time(arguments.at),
                request_limit=arguments.request_limit,
                actor=arguments.actor,
                reason=arguments.reason,
                confirmation=arguments.confirmation,
                idempotency_key=arguments.idempotency_key,
            )
        elif command == "live-risk-policy-inspect":
            value = repository.risk_policy(arguments.policy_id)
        elif command == "execution-control-inspect":
            values = repository.control_events(arguments.control_id)
            value = {"events": values, "current": None if not values else values[-1]}
        elif command == "execution-control-set":
            value = SetExecutionControl(repository).execute(
                event_id=arguments.event_id,
                control_id=arguments.control_id,
                state=ExecutionControlState(arguments.state),
                actor=arguments.actor,
                reason=arguments.reason,
                confirmation=arguments.confirmation,
                idempotency_key=arguments.idempotency_key,
                effective_at=_live_readiness_time(arguments.at),
            )
        elif command == "backup-readiness-inspect":
            value = repository.backup_readiness(arguments.record_id)
        elif command == "live-readiness-materialize":
            expected = f"MATERIALIZE_LIVE_READINESS:{arguments.assessment_id}"
            if (
                arguments.confirmation != expected
                or not arguments.reason.strip()
                or not arguments.idempotency_key.strip()
            ):
                raise SystemExit("LIVE_READINESS_EXACT_CONFIRMATION_REQUIRED")
            provider = Provider(arguments.provider)
            at = _live_readiness_time(arguments.at)
            capability = repository.latest_capability(provider, arguments.account_scope_id)
            controls = repository.control_events(arguments.control_id)
            if not controls:
                raise SystemExit("EXECUTION_CONTROL_NOT_FOUND")
            policy = repository.risk_policy(arguments.policy_id)
            if policy is None:
                if arguments.policy_id != "LIVE_RISK_POLICY_V1":
                    raise SystemExit("LIVE_RISK_POLICY_NOT_FOUND")
                policy = LiveRiskPolicy.v1(created_at=at)
            value = MaterializeLiveReadiness(repository).execute(
                assessment_id=arguments.assessment_id,
                decision_id=arguments.decision_id,
                policy=policy,
                inputs=LiveReadinessInputs(
                    venue=provider,
                    account_scope_id=arguments.account_scope_id,
                    capability=capability,
                    strategy_or_method_id=None,
                    paper_sample_count=0,
                    fill_count=0,
                    settlement_count=0,
                    total_live_capital=Decimal("0"),
                    retained_cash_ratio=Decimal("1"),
                    per_opportunity_exposure=Decimal("0"),
                    per_strategy_exposure=Decimal("0"),
                    per_instrument_exposure=Decimal("0"),
                    per_venue_exposure=Decimal("0"),
                    open_order_count=0,
                    open_position_count=0,
                    daily_realized_loss=Decimal("0"),
                    daily_modeled_loss=Decimal("0"),
                    drawdown_ratio=Decimal("0"),
                    strategy_health_state="INSUFFICIENT_SAMPLE",
                    reconciliation_quality="UNAVAILABLE",
                    unresolved_reconciliation_severity=1,
                    operating_cycle_health="UNAVAILABLE",
                    provider_health="UNAVAILABLE",
                    backup=repository.latest_backup_readiness(),
                    control=controls[-1],
                    qualifying_opportunity=False,
                    explicit_operator_review=True,
                    assessed_at=at,
                ),
                actor=arguments.actor,
            )
        elif command == "live-readiness-inspect":
            value = repository.assessment(arguments.assessment_id)
        elif command == "live-readiness-blockers":
            assessment = repository.assessment(arguments.assessment_id)
            value = (
                None
                if assessment is None
                else {
                    "assessment_id": assessment.assessment_id,
                    "recommendation": assessment.recommendation,
                    "blockers": assessment.unresolved_blockers,
                }
            )
        elif command == "live-readiness-export":
            expected = f"EXPORT_LIVE_READINESS:{arguments.export_id}"
            if (
                arguments.confirmation != expected
                or not arguments.actor.strip()
                or not arguments.reason.strip()
                or not arguments.idempotency_key.strip()
            ):
                raise SystemExit("LIVE_READINESS_EXPORT_EXACT_CONFIRMATION_REQUIRED")
            assessment = repository.assessment(arguments.assessment_id)
            if assessment is None:
                raise SystemExit("LIVE_READINESS_ASSESSMENT_NOT_FOUND")
            value = ExportLiveReadinessEvidence(repository).execute(
                export_id=arguments.export_id,
                assessment_id=arguments.assessment_id,
                categories=(
                    ("PROVIDER_CAPABILITIES", 1 if assessment.capability_snapshot_id else 0),
                    ("READINESS_ASSESSMENTS", 1),
                    ("RISK_DECISIONS", 1),
                    ("EXECUTION_CONTROLS", 1),
                    ("ORDERS_AND_FILLS", 0),
                    ("POSITIONS_AND_SETTLEMENTS", 0),
                    ("RECONCILIATION", 0),
                    ("JOURNALS", 0),
                    ("AUTHORITY_HISTORY", 0),
                    ("BACKUP_READINESS", 1 if repository.latest_backup_readiness() else 0),
                ),
                evidence_ids=(assessment.assessment_id,),
                evidence_hashes=(assessment.canonical_hash,),
                paper_records=0,
                generated_at=_live_readiness_time(arguments.at),
            )
        else:
            value = live_readiness_workspace(repository)
        _research_evidence_json(safe_json_payload(value))
    finally:
        engine.dispose()


def candidate_search_create(
    *,
    experiment_id: str,
    name: str,
    dataset_id: str,
    feature_schema_id: str,
    specification: str,
    objective: str,
    maximum_trials: int,
    seed: int,
    reason: str,
    idempotency_key: str,
) -> None:
    from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime
    from apex.domain.candidate_generation import ObjectiveIdentity

    runtime = CandidateGenerationRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(
            runtime.create_plan(
                experiment_id=experiment_id,
                name=name,
                dataset_id=dataset_id,
                feature_schema_id=feature_schema_id,
                specification=specification,
                objective=ObjectiveIdentity(objective),
                maximum_trials=maximum_trials,
                seed=seed,
                reason=reason,
                idempotency_key=idempotency_key,
            )
        )
    finally:
        runtime.close()


def candidate_search_run(
    *, search_plan_id: str, requested_trials: int, idempotency_key: str
) -> None:
    from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime

    runtime = CandidateGenerationRuntime.open(local_root=ROOT / ".local")
    try:
        run, decision, bindings = runtime.run_search(
            search_plan_id=search_plan_id,
            requested_trials=requested_trials,
            idempotency_key=idempotency_key,
        )
        _strategy_lab_json(
            {
                "run": run,
                "decision": decision,
                "bindings": bindings,
                "authority": "NONE",
            }
        )
    finally:
        runtime.close()


def candidate_search_recover(*, search_run_id: str) -> None:
    from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime

    runtime = CandidateGenerationRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(
            {
                "search_run_id": search_run_id,
                "reconciliation_reason_codes": runtime.recover(search_run_id),
                "status": runtime.status(search_run_id),
            }
        )
    finally:
        runtime.close()


def candidate_search_cancel(*, search_run_id: str, reason: str) -> None:
    from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime

    runtime = CandidateGenerationRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.cancel(search_run_id=search_run_id, reason=reason))
    finally:
        runtime.close()


def candidate_search_status(*, search_run_id: str) -> None:
    from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime

    runtime = CandidateGenerationRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.status(search_run_id))
    finally:
        runtime.close()


def candidate_search_report(*, search_run_id: str) -> None:
    from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime

    runtime = CandidateGenerationRuntime.open(local_root=ROOT / ".local")
    try:
        status = runtime.status(search_run_id)
        bindings = status["bindings"]
        assert isinstance(bindings, tuple)
        _strategy_lab_json(
            {
                **status,
                "trial_state_totals": {
                    state: sum(item.objective_state.value == state for item in bindings)
                    for state in (
                        "PROPOSED",
                        "TRAINING",
                        "COMPLETED",
                        "PRUNED",
                        "FAILED",
                        "REJECTED_INTEGRITY",
                        "CANCELED",
                    )
                },
                "deployment_created": False,
                "champion_assigned": False,
            }
        )
    finally:
        runtime.close()


def mlflow_reconcile(*, experiment_id: str) -> None:
    from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime

    runtime = CandidateGenerationRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.reconcile_mlflow(experiment_id))
    finally:
        runtime.close()


def automated_search_readiness() -> None:
    from apex.adapters.candidate_generation_operations import CandidateGenerationRuntime

    runtime = CandidateGenerationRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.readiness())
    finally:
        runtime.close()


_MLFLOW_HOST = "127.0.0.1"
_MLFLOW_PORT = 5500


def _mlflow_paths() -> tuple[Path, Path, Path, Path]:
    root = ROOT / ".local" / "mlflow"
    return (
        root,
        root / "runtime" / "server.pid",
        root / "runtime" / "server.log",
        root / "backend" / "mlflow.db",
    )


def _mlflow_pid() -> int | None:
    _root, pid_path, _log_path, _backend = _mlflow_paths()
    try:
        value = int(pid_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(value, 0)
        process_state = Path(f"/proc/{value}/stat").read_text(encoding="utf-8").split()[2]
        command_line = Path(f"/proc/{value}/cmdline").read_bytes().replace(b"\0", b" ")
    except (OSError, FileNotFoundError):
        pid_path.unlink(missing_ok=True)
        return None
    if process_state == "Z":
        pid_path.unlink(missing_ok=True)
        return None
    if b"mlflow" not in command_line or b"127.0.0.1" not in command_line:
        raise RuntimeError("recorded MLflow PID does not identify the managed loopback server")
    return value


def _mlflow_healthy() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{_MLFLOW_HOST}:{_MLFLOW_PORT}/health", timeout=1
        ) as response:
            return int(response.status) == 200
    except (OSError, urllib.error.URLError):
        return False


def mlflow_up() -> None:
    root, pid_path, log_path, backend = _mlflow_paths()
    if _mlflow_pid() is not None:
        mlflow_status()
        return
    artifacts = root / "artifacts"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    backend.parent.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    temporary_root = root / "runtime" / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    server_environment = _environment()
    server_environment.update(
        {
            "MLFLOW_SERVER_ENABLE_JOB_EXECUTION": "false",
            "TMPDIR": str(temporary_root),
            "TEMP": str(temporary_root),
            "TMP": str(temporary_root),
        }
    )
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            (
                _python(),
                "-m",
                "mlflow",
                "server",
                "--host",
                _MLFLOW_HOST,
                "--port",
                str(_MLFLOW_PORT),
                "--backend-store-uri",
                f"sqlite:///{backend}",
                "--default-artifact-root",
                artifacts.resolve().as_uri(),
                "--workers",
                "1",
                f"--gunicorn-opts=--preload --worker-tmp-dir {temporary_root}",
            ),
            cwd=ROOT,
            env=server_environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    for _attempt in range(40):
        if process.poll() is not None:
            pid_path.unlink(missing_ok=True)
            raise RuntimeError("managed MLflow server exited during startup")
        if _mlflow_healthy():
            mlflow_status()
            return
        time.sleep(0.25)
    mlflow_down()
    raise RuntimeError("managed MLflow server did not become healthy within 10 seconds")


def mlflow_down() -> None:
    _root, _pid_path, _log_path, _backend = _mlflow_paths()
    process_id = _mlflow_pid()
    if process_id is None:
        _strategy_lab_json({"state": "STOPPED", "loopback_url": None})
        return
    os.kill(process_id, signal.SIGTERM)
    for _attempt in range(40):
        if _mlflow_pid() is None:
            _strategy_lab_json({"state": "STOPPED", "loopback_url": None})
            return
        time.sleep(0.25)
    raise RuntimeError("managed MLflow server did not stop within 10 seconds")


def mlflow_status() -> None:
    from apex.adapters.candidate_generation_tools import LocalMLflowMirrorAdapter

    process_id = _mlflow_pid()
    healthy = process_id is not None and _mlflow_healthy()
    _strategy_lab_json(
        {
            "state": "HEALTHY" if healthy else "STOPPED",
            "pid": process_id,
            "mlflow_version": LocalMLflowMirrorAdapter.runtime_version,
            "tracking_mode": "LOCAL_SQLITE_TRACKING",
            "bind_host": _MLFLOW_HOST,
            "loopback_url": (f"http://{_MLFLOW_HOST}:{_MLFLOW_PORT}" if healthy else None),
            "remote_tracking_enabled": False,
            "authority": "NONE",
        }
    )


def external_paper_status() -> None:
    """Read-only deterministic-fixture readiness; no credentials are inspected."""
    from apex.adapters.external_paper_fixture import DeterministicPaperBrokerFixture

    fixture = DeterministicPaperBrokerFixture(scenarios={}, now=datetime.now(tz=UTC))
    _strategy_lab_json(
        {
            "environment": "FIXTURE_PAPER",
            "adapter": fixture.identity,
            "capability_state": "READY",
            "credentials_available": False,
            "authentication_state": "NOT_APPLICABLE",
            "account_read": False,
            "order_submit": True,
            "order_read": True,
            "cancel": True,
            "position_read": False,
            "activity_read": False,
            "reason_codes": ("DETERMINISTIC_NO_NETWORK_FIXTURE",),
            "warnings": (
                "PAPER EXECUTION IS SIMULATED",
                "EXTERNAL BROKER PAPER STATE IS EVIDENCE, NOT AUTOMATIC APEX TRUTH",
                "LIVE TRADING IS NOT ENABLED",
            ),
            "actual_order_submission": False,
            "network_calls": fixture.network_calls,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_readiness() -> None:
    """Perform only bounded authenticated PAPER reads, never order submission."""
    from apex.adapters.alpaca_paper_broker import AlpacaPaperBroker
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.external_paper import CheckAlpacaPaperReadiness

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        result = CheckAlpacaPaperReadiness(
            repository=PostgresExternalPaperRepository(engine),
            broker=AlpacaPaperBroker(),
        ).execute(now=datetime.now(tz=UTC))
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "PAPER",
            "adapter": "ALPACA_PAPER_BROKER_V1",
            "capability": result.capability,
            "evidence_ids": result.evidence_ids,
            "endpoint_states": result.endpoint_states,
            "current_state": result.capability.state.value,
            "blockers": result.capability.reason_codes,
            "next_action": (
                "PREPARE_ENGINEERING_ACCEPTANCE_ONLY"
                if result.capability.state.value == "READY"
                else "RESOLVE_PAPER_READINESS_BLOCKER"
            ),
            "network_operation_performed": result.capability.credentials_available,
            "actual_paper_order_submitted": False,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_acceptance_create(
    *,
    symbol: str,
    idempotency_key: str,
    broker: object | None = None,
    now: datetime | None = None,
) -> None:
    """Prepare one persisted acceptance instruction and stop before submission."""
    from apex.adapters.alpaca_paper_broker import AlpacaPaperBroker
    from apex.adapters.external_paper_postgres import (
        PostgresAlpacaPaperAcceptanceAuthorityAdapter,
        PostgresExternalPaperRepository,
    )
    from apex.adapters.postgres import (
        PostgresSettings,
        create_postgres_engine,
        paper_trial_authorization,
        securities_paper_authorization,
        securities_paper_opportunity,
    )
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import (
        CheckAlpacaPaperReadiness,
        CreatePersistentAlpacaPaperAcceptanceWorkflow,
    )

    approved_symbols = frozenset(
        {
            "SPY",
            "QQQ",
            "DIA",
            "IWM",
            "EFA",
            "EEM",
            "TLT",
            "LQD",
            "GLD",
            "USO",
            "XLF",
            "XLK",
            "XLE",
            "XLV",
            "XLI",
            "XLP",
        }
    )
    if symbol not in approved_symbols or not idempotency_key:
        raise SystemExit("EXTERNAL_PAPER_ACCEPTANCE_SYMBOL_OR_IDEMPOTENCY_INVALID")
    checked_at = now or datetime.now(tz=UTC)
    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        external = PostgresExternalPaperRepository(engine)
        readiness = CheckAlpacaPaperReadiness(
            repository=external, broker=broker or AlpacaPaperBroker()
        ).execute(now=checked_at)
        network_reads = len(readiness.evidence_ids)
        state = readiness.capability.state.value
        if state == "CREDENTIALS_REQUIRED":
            _strategy_lab_json(
                {
                    "environment": "ALPACA_PAPER",
                    "purpose": "ENGINEERING_ACCEPTANCE_ONLY",
                    "symbol": symbol,
                    "current_state": state,
                    "state": state,
                    "blockers": readiness.capability.reason_codes,
                    "network_reads_performed": 0,
                    "actual_paper_order_submitted": False,
                    "live_authority": "NONE",
                    "next_action": "CONFIGURE_PRIVATE_SEPARATE_PAPER_CREDENTIALS",
                }
            )
            return
        if state != "READY":
            _strategy_lab_json(
                {
                    "environment": "ALPACA_PAPER",
                    "purpose": "ENGINEERING_ACCEPTANCE_ONLY",
                    "symbol": symbol,
                    "current_state": state,
                    "state": state,
                    "blockers": readiness.capability.reason_codes,
                    "network_reads_performed": network_reads,
                    "actual_paper_order_submitted": False,
                    "live_authority": "NONE",
                    "next_action": "RESOLVE_PAPER_READINESS_BLOCKER",
                }
            )
            return
        securities = PostgresSecuritiesPaperRepository(engine)
        result = CreatePersistentAlpacaPaperAcceptanceWorkflow(
            repository=external,
            securities_repository=securities,
            authority_port=PostgresAlpacaPaperAcceptanceAuthorityAdapter(engine),
        ).execute(
            symbol=symbol,
            idempotency_key=idempotency_key,
            readiness=readiness,
            now=checked_at,
        )
        preflight = external.latest_acceptance_preflight(result.instruction.instruction_id)
        order = securities.order(result.binding.internal_order_id)
        with engine.connect() as connection:
            authorization = (
                connection.execute(
                    securities_paper_authorization.select().where(
                        securities_paper_authorization.c.authorization_id == order.authorization_id
                    )
                )
                .mappings()
                .one()
            )
            opportunity = (
                connection.execute(
                    securities_paper_opportunity.select().where(
                        securities_paper_opportunity.c.opportunity_id == order.opportunity_id
                    )
                )
                .mappings()
                .one()
            )
            trial = (
                connection.execute(
                    paper_trial_authorization.select().where(
                        paper_trial_authorization.c.authorization_id == result.acceptance.trial_id
                    )
                )
                .mappings()
                .one()
            )
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "purpose": "ENGINEERING_ACCEPTANCE_ONLY",
            "acceptance_id": result.acceptance.acceptance_id,
            "configuration_id": trial["strategy_configuration_hash"],
            "campaign_id": result.acceptance.campaign_id,
            "strategy_authority_id": trial["operator_action_id"],
            "paper_trial_authorization_id": result.acceptance.trial_id,
            "external_paper_authorization_id": result.acceptance.authorization_id,
            "instruction_id": result.instruction.instruction_id,
            "risk_decision_id": authorization["risk_decision_id"],
            "risk_reservation_id": result.acceptance.reservation_id,
            "internal_order_id": result.acceptance.internal_order_id,
            "binding_id": result.acceptance.binding_id,
            "preflight_id": (
                f"{preflight.instruction_id}:{preflight.canonical_hash}" if preflight else None
            ),
            "symbol": result.instruction.symbol,
            "side": result.instruction.side,
            "quantity": result.instruction.quantity,
            "order_type": result.instruction.order_type,
            "time_in_force": result.instruction.time_in_force,
            "estimated_price": opportunity["reference_price"],
            "estimated_simulated_notional": opportunity["intended_exposure"],
            "maximum_notional": result.instruction.maximum_notional,
            "expiration": result.instruction.expires_at,
            "preflight_checked_at": preflight.checked_at if preflight else None,
            "preflight_freshness_expires_at": (
                preflight.freshness_expires_at if preflight else None
            ),
            "state": result.preflight.state,
            "confirmation": result.preflight.confirmation,
            "blockers": result.preflight.blockers,
            "next_action": "REVIEW_AND_TYPE_EXACT_CONFIRMATION",
            "network_reads_performed": network_reads,
            "actual_paper_order_submitted": False,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_acceptance_status(*, campaign_id: str) -> None:
    """Read one exact retained engineering-acceptance Campaign; no broker I/O."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        acceptance = repository.acceptance_for_campaign(campaign_id)
        if acceptance is None:
            raise SystemExit("ALPACA_PAPER_ACCEPTANCE_CAMPAIGN_NOT_FOUND")
        preflight = repository.latest_acceptance_preflight(acceptance.instruction_id)
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "purpose": "ENGINEERING_ACCEPTANCE_ONLY",
            "acceptance": acceptance,
            "preflight": preflight,
            "network_reads_performed": 0,
            "actual_paper_order_submitted": False,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_acceptance_instruction(*, instruction_id: str) -> None:
    """Read exact retained acceptance instruction state; no broker I/O."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        acceptance = repository.acceptance_for_instruction(instruction_id)
        if acceptance is None:
            raise SystemExit("ALPACA_PAPER_ACCEPTANCE_INSTRUCTION_NOT_FOUND")
        instruction = repository.instruction(instruction_id)
        binding = repository.binding_for_instruction(instruction_id)
        preflight = repository.latest_acceptance_preflight(instruction_id)
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "purpose": "ENGINEERING_ACCEPTANCE_ONLY",
            "acceptance": acceptance,
            "instruction": instruction,
            "binding": binding,
            "preflight": preflight,
            "network_reads_performed": 0,
            "actual_paper_order_submitted": False,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_preflight(*, instruction_id: str) -> None:
    """Run fresh PAPER reads and preflight; never reserve or submit an Order."""
    from apex.adapters.alpaca_paper_broker import AlpacaPaperBroker
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import (
        CheckAlpacaPaperReadiness,
        PreflightAlpacaPaperInstruction,
    )

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        readiness = CheckAlpacaPaperReadiness(
            repository=repository, broker=AlpacaPaperBroker()
        ).execute(now=datetime.now(tz=UTC))
        result = PreflightAlpacaPaperInstruction(
            repository=repository, securities_repository=PostgresSecuritiesPaperRepository(engine)
        ).execute(instruction_id=instruction_id, readiness=readiness, now=datetime.now(tz=UTC))
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "PAPER",
            "adapter": "ALPACA_PAPER_BROKER_V1",
            "instruction_id": result.instruction_id,
            "current_state": result.state,
            "blockers": result.blockers,
            "next_action": "TYPE_EXACT_OPERATOR_CONFIRMATION"
            if result.confirmation
            else "RESOLVE_PREFLIGHT_BLOCKERS",
            "confirmation": result.confirmation,
            "network_operation_performed": readiness.capability.credentials_available,
            "actual_paper_order_submitted": False,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_submit(*, instruction_id: str, confirmation: str) -> None:
    """Future-only confirmed PAPER submission; never invoked in this task."""
    from apex.adapters.alpaca_paper_broker import AlpacaPaperBroker
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import (
        CheckAlpacaPaperReadiness,
        ExternalPaperApplicationError,
        PreflightAlpacaPaperInstruction,
        SubmitAuthorizedPaperInstruction,
    )

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        now = datetime.now(tz=UTC)
        readiness = CheckAlpacaPaperReadiness(
            repository=repository, broker=AlpacaPaperBroker()
        ).execute(now=now)
        preflight = PreflightAlpacaPaperInstruction(
            repository=repository, securities_repository=PostgresSecuritiesPaperRepository(engine)
        ).execute(instruction_id=instruction_id, readiness=readiness, now=now)
        if preflight.confirmation is None or confirmation != preflight.confirmation:
            raise SystemExit("EXACT_OPERATOR_CONFIRMATION_REQUIRED")
        submitted = SubmitAuthorizedPaperInstruction(
            repository=repository, broker=AlpacaPaperBroker(allow_submission=True)
        ).execute(instruction_id=instruction_id, capability=readiness.capability, now=now)
    except ExternalPaperApplicationError as error:
        raise SystemExit(str(error)) from error
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "PAPER",
            "adapter": "ALPACA_PAPER_BROKER_V1",
            "instruction": submitted.instruction,
            "state": submitted.instruction.state.value,
            "blockers": submitted.instruction.reason_code,
            "next_action": "SYNCHRONIZE_ALPACA_PAPER_INSTRUCTION",
            "network_operation_performed": True,
            "actual_paper_order_submitted": submitted.evidence is not None,
            "live_authority": "NONE",
        }
    )


def external_paper_instruction_create(
    *,
    authorization_id: str,
    symbol: str,
    side: str,
    quantity: str,
    maximum_notional: str,
    expires_at: str,
    idempotency_key: str,
) -> None:
    """Persist one bounded External PAPER instruction; this never submits it."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.external_paper import CreateAuthorizedExternalPaperInstruction
    from apex.domain.external_paper import ExternalPaperInstruction

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        authorization = repository.authorization(authorization_id)
        if authorization is None:
            raise SystemExit("EXTERNAL_PAPER_AUTHORIZATION_NOT_FOUND")
        instruction = CreateAuthorizedExternalPaperInstruction(repository=repository).execute(
            instruction=ExternalPaperInstruction.authorized(
                authorization=authorization,
                symbol=symbol,
                side=side,
                quantity=quantity,
                maximum_notional=maximum_notional,
                expires_at=_strategy_lab_cutoff(expires_at),
                idempotency_key=idempotency_key,
            ),
            now=datetime.now(tz=UTC),
        )
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "instruction": instruction,
            "state": instruction.state.value,
            "next_action": "PREPARE_INTERNAL_EXECUTION",
            "broker_submission": False,
            "live_authority": "NONE",
        }
    )


def external_paper_reduce(
    *,
    instruction_id: str,
    position_id: str,
    decision_at: str,
    earliest_fill_at: str,
    expires_at: str,
) -> None:
    """Prepare a bounded SELL reduction locally; it never submits to a broker."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import PrepareAuthorizedExternalPaperReduction

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        prepared = PrepareAuthorizedExternalPaperReduction(
            repository=PostgresExternalPaperRepository(engine),
            securities_repository=PostgresSecuritiesPaperRepository(engine),
        ).execute(
            instruction_id=instruction_id,
            position_id=position_id,
            decision_at=_strategy_lab_cutoff(decision_at),
            earliest_fill_at=_strategy_lab_cutoff(earliest_fill_at),
            expires_at=_strategy_lab_cutoff(expires_at),
        )
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "instruction_id": instruction_id,
            "binding": prepared.binding,
            "internal_order": prepared.order,
            "state": "PREPARED",
            "next_action": "SUBMIT_ONLY_AFTER_SEPARATE_READY_CAPABILITY_CHECK",
            "broker_submission": False,
            "live_authority": "NONE",
        }
    )


def external_paper_prepare(*, instruction_id: str, binding_id: str) -> None:
    """Replay one retained internal binding; this never reaches a broker."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import (
        ExternalPaperApplicationError,
        PrepareAuthorizedExternalPaperExecution,
    )

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        binding = repository.binding_for_instruction(instruction_id)
        if binding is None or binding.binding_id != binding_id:
            raise SystemExit("EXTERNAL_PAPER_BINDING_NOT_FOUND")
        prepared = PrepareAuthorizedExternalPaperExecution(
            repository=repository,
            securities_repository=PostgresSecuritiesPaperRepository(engine),
        ).execute(binding=binding, now=datetime.now(tz=UTC))
    except ExternalPaperApplicationError as error:
        raise SystemExit(str(error)) from error
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "instruction_id": instruction_id,
            "binding": prepared,
            "state": prepared.state.value,
            "blocker": None,
            "next_action": "SUBMIT_FIXTURE_INSTRUCTION",
            "broker_submission": False,
            "live_authority": "NONE",
        }
    )


def external_paper_submit(*, instruction_id: str) -> None:
    """Submit through the deterministic no-network fixture only."""
    from apex.adapters.external_paper_fixture import DeterministicPaperBrokerFixture
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.external_paper import SubmitAuthorizedPaperInstruction, ready_capability

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    now = datetime.now(tz=UTC)
    try:
        fixture = DeterministicPaperBrokerFixture(scenarios={}, now=now)
        result = SubmitAuthorizedPaperInstruction(
            repository=PostgresExternalPaperRepository(engine), broker=fixture
        ).execute(
            instruction_id=instruction_id,
            capability=ready_capability(now),
            now=now,
        )
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "instruction": result.instruction,
            "evidence": result.evidence,
            "state": result.instruction.state.value,
            "blocker": result.instruction.reason_code,
            "next_action": "SYNCHRONIZE_FIXTURE_INSTRUCTION",
            "broker_submission": "FIXTURE_ONLY",
            "live_authority": "NONE",
        }
    )


def external_paper_sync(*, instruction_id: str) -> None:
    """Synchronize one exact instruction through the no-network fixture."""
    from apex.adapters.external_paper_fixture import DeterministicPaperBrokerFixture
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import SyncExternalPaperInstruction

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        instruction, reconciliation = SyncExternalPaperInstruction(
            repository=PostgresExternalPaperRepository(engine),
            broker=DeterministicPaperBrokerFixture(scenarios={}, now=datetime.now(tz=UTC)),
            securities_repository=PostgresSecuritiesPaperRepository(engine),
        ).execute(instruction_id=instruction_id)
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "instruction": instruction,
            "reconciliation": reconciliation,
            "state": instruction.state.value,
            "blocker": reconciliation.required_action,
            "next_action": "VALIDATE_RETAINED_FILL_OR_RECONCILE",
            "broker_read": "FIXTURE_ONLY",
            "live_authority": "NONE",
        }
    )


def alpaca_paper_sync(
    *, instruction_id: str, broker: object | None = None, emit: bool = True
) -> object:
    """Read one exact real Alpaca PAPER instruction; this command never writes a broker order."""
    from apex.adapters.alpaca_paper_broker import AlpacaPaperBroker
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.external_paper import SynchronizeAlpacaPaperInstruction

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        result = SynchronizeAlpacaPaperInstruction(
            repository=PostgresExternalPaperRepository(engine),
            broker=broker if broker is not None else AlpacaPaperBroker(),  # type: ignore[arg-type]
        ).execute(instruction_id=instruction_id)
    finally:
        engine.dispose()
    payload = {
        "environment": "ALPACA_PAPER",
        "adapter": "ALPACA_PAPER_BROKER_V1",
        "instruction_id": result.instruction.instruction_id,
        "campaign_id": result.instruction.campaign_id,
        "broker_order_id": result.instruction.broker_order_id,
        "symbol": result.instruction.symbol,
        "side": result.instruction.side,
        "quantity": str(result.instruction.quantity),
        "apex_instruction_state": result.instruction.state.value,
        "broker_order_state": result.broker_order_state,
        "cumulative_filled_quantity": result.cumulative_filled_quantity,
        "remaining_quantity": result.remaining_quantity,
        "average_fill_price": result.average_fill_price,
        "retained_order_observation_ids": result.order_observation_ids,
        "retained_fill_activity_observation_ids": result.fill_activity_observation_ids,
        "retained_position_observation_ids": result.position_observation_ids,
        "retained_account_observation_ids": result.account_observation_ids,
        "x_request_id_evidence": result.request_ids,
        "blockers": result.blockers,
        "next_action": result.next_action,
        "network_reads_performed": result.network_reads,
        "broker_writes_performed": result.broker_writes,
        "financial_mutations": result.financial_mutations,
        "actual_paper_orders_submitted_by_this_command": result.actual_paper_orders_submitted,
        "live_authority": result.live_authority,
    }
    if emit:
        _strategy_lab_json(payload)
    return result


def alpaca_paper_status(*, instruction_id: str, refresh: bool = False) -> None:
    """Read one exact retained genuine PAPER instruction without broker I/O."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository

    if refresh:
        alpaca_paper_sync(instruction_id=instruction_id, emit=False)
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        external = PostgresExternalPaperRepository(engine)
        securities = PostgresSecuritiesPaperRepository(engine)
        instruction = external.instruction(instruction_id)
        if instruction is None:
            raise SystemExit("EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND")
        acceptance = external.acceptance_for_instruction(instruction_id)
        evidence = external.evidence_for_instruction(instruction_id)
        validated = external.validated_fills_for_instruction(instruction_id)
        binding = external.binding_for_instruction(instruction_id)
        order = (
            securities.order(binding.internal_order_id)
            if binding is not None and securities.has_order(binding.internal_order_id)
            else None
        )
        position = (
            securities.position(order.position_id)
            if order is not None and order.position_id is not None
            else None
        )
        reports = external.reconciliations_for_instruction(instruction_id)
        latest_order = next(
            (item for item in reversed(evidence) if item.observation_type == "ORDER_STATUS"), None
        )
    finally:
        engine.dispose()
    filled = latest_order.sanitized_payload.get("filled_qty") if latest_order else None
    remaining = str(instruction.quantity - Decimal(filled)) if filled is not None else None
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "adapter": "ALPACA_PAPER_BROKER_V1",
            "acceptance_campaign": acceptance,
            "campaign_id": instruction.campaign_id,
            "instruction": instruction,
            "broker_order_id": instruction.broker_order_id,
            "apex_state": instruction.state.value,
            "latest_retained_broker_state": latest_order.sanitized_payload.get("status")
            if latest_order
            else None,
            "cumulative_filled_quantity": filled,
            "remaining_quantity": remaining,
            "retained_order_observations": tuple(
                item for item in evidence if item.observation_type == "ORDER_STATUS"
            ),
            "retained_fill_observations": tuple(
                item for item in evidence if item.observation_type == "FILL_ACTIVITY_ITEM"
            ),
            "retained_position_observations": tuple(
                item for item in evidence if item.observation_type == "POSITION_SNAPSHOT_ITEM"
            ),
            "retained_account_observations": tuple(
                item for item in evidence if item.observation_type == "ACCOUNT_READINESS"
            ),
            "validated_fills": tuple(
                {"fill": fill, "application": external.fill_application(fill.validated_fill_id)}
                for fill in validated
            ),
            "internal_order": order,
            "position": position,
            "reconciliations": reports,
            "kill_state": "KILLED"
            if external.external_paper_killed(instruction.campaign_id)
            else "OPEN",
            "required_next_action": (
                "VALIDATE_RETAINED_FILL"
                if filled not in (None, "0") and not validated
                else "RECONCILE_RETAINED_STATE"
                if reports
                else "WAIT_AND_SYNCHRONIZE_AGAIN"
            ),
            "network_reads_performed": 0,
            "refresh_performed": refresh,
            "actual_paper_orders_submitted": 0,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_validate_fill(*, instruction_id: str, evidence_id: str) -> None:
    """Validate exactly one retained genuine Alpaca PAPER fill; no broker I/O or accounting."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.external_paper import ValidateAlpacaPaperFill

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        validated = ValidateAlpacaPaperFill(
            repository=PostgresExternalPaperRepository(engine)
        ).execute(instruction_id=instruction_id, evidence_id=evidence_id)
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "instruction_id": instruction_id,
            "evidence_id": evidence_id,
            "validated_fill": validated,
            "state": validated.state.value,
            "next_action": "APPLY_VALIDATED_RETAINED_FILL",
            "network_reads_performed": 0,
            "financial_mutations": 0,
            "actual_paper_orders_submitted": 0,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_apply_fills(*, instruction_id: str) -> None:
    """Apply only validated genuine evidence through the shared atomic accounting kernel."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import ProcessAlpacaPaperFills

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        result = ProcessAlpacaPaperFills(
            repository=PostgresExternalPaperRepository(engine),
            securities_repository=PostgresSecuritiesPaperRepository(engine),
        ).execute(instruction_id=instruction_id)
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "instruction_id": result.instruction_id,
            "validated_fill_ids": result.validated_fill_ids,
            "applied_fill_ids": result.applied_fill_ids,
            "blocker": result.blocker,
            "next_action": "RECONCILE_RETAINED_STATE"
            if result.blocker is None
            else "RESOLVE_FILL_BLOCKER",
            "network_reads_performed": 0,
            "actual_paper_orders_submitted": 0,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_reconcile(*, instruction_id: str) -> None:
    """Report exact retained genuine-PAPER reconciliation state without broker I/O."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        instruction = repository.instruction(instruction_id)
        if instruction is None:
            raise SystemExit("EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND")
        reports = repository.reconciliations_for_instruction(instruction_id)
        fills = repository.validated_fills_for_instruction(instruction_id)
    finally:
        engine.dispose()
    latest = reports[-1] if reports else None
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "instruction_id": instruction_id,
            "broker_order_id": instruction.broker_order_id,
            "reconciliation": latest,
            "validated_fills": fills,
            "classification": latest.classification.value if latest else "RECONCILIATION_REQUIRED",
            "next_action": latest.required_action
            if latest
            else "SYNCHRONIZE_ALPACA_PAPER_INSTRUCTION",
            "broker_reads_performed": 0,
            "actual_paper_orders_submitted": 0,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_process(*, instruction_id: str, apply_confirmation: str | None) -> None:
    """Bounded cycle: sync, validate retained fills, and apply only by exact confirmation."""
    from apex.adapters.alpaca_paper_broker import AlpacaPaperBroker
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import (
        ProcessAlpacaPaperFills,
        SynchronizeAlpacaPaperInstruction,
        ValidateAlpacaPaperFill,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        synced = SynchronizeAlpacaPaperInstruction(
            repository=repository, broker=AlpacaPaperBroker()
        ).execute(instruction_id=instruction_id)
        validated = []
        for evidence_id in (*synced.order_observation_ids, *synced.fill_activity_observation_ids):
            evidence = repository.broker_evidence(evidence_id)
            if evidence is None or evidence.sanitized_payload.get("filled_qty") in (None, "0"):
                continue
            validated.append(
                ValidateAlpacaPaperFill(repository=repository).execute(
                    instruction_id=instruction_id, evidence_id=evidence_id
                )
            )
        result = None
        confirmation = f"APPLY_ALPACA_PAPER_RETAINED_FILLS:{instruction_id}"
        if apply_confirmation is not None:
            if apply_confirmation != confirmation:
                raise SystemExit("EXACT_OPERATOR_CONFIRMATION_REQUIRED")
            result = ProcessAlpacaPaperFills(
                repository=repository,
                securities_repository=PostgresSecuritiesPaperRepository(engine),
            ).execute(instruction_id=instruction_id)
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "instruction_id": instruction_id,
            "synchronization": synced,
            "validated_fills": tuple(validated),
            "application": result,
            "apply_confirmation": confirmation,
            "next_action": "TYPE_EXACT_APPLY_CONFIRMATION"
            if result is None and validated
            else "WAIT_AND_SYNCHRONIZE_AGAIN",
            "actual_paper_orders_submitted": 0,
            "live_authority": "NONE",
        }
    )


def alpaca_paper_cancel(*, instruction_id: str, confirmation: str) -> None:
    """Cancel one exact genuine PAPER order only after fresh readiness and confirmation."""
    from apex.adapters.alpaca_paper_broker import AlpacaPaperBroker
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.external_paper import (
        CancelExternalPaperInstruction,
        CheckAlpacaPaperReadiness,
        ExternalPaperApplicationError,
    )

    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        instruction = repository.instruction(instruction_id)
        binding = repository.binding_for_instruction(instruction_id)
        if instruction is None or instruction.broker_order_id is None or binding is None:
            raise SystemExit("ALPACA_PAPER_DURABLE_ORDER_BINDING_REQUIRED")
        expected = f"CANCEL_ALPACA_PAPER:{instruction_id}:{instruction.broker_order_id}"
        if confirmation != expected:
            raise SystemExit("EXACT_OPERATOR_CONFIRMATION_REQUIRED")
        readiness = CheckAlpacaPaperReadiness(
            repository=repository, broker=AlpacaPaperBroker()
        ).execute(now=datetime.now(tz=UTC))
        if readiness.capability.state.value != "READY":
            raise SystemExit("ALPACA_PAPER_FRESH_READINESS_REQUIRED")
        evidence = CancelExternalPaperInstruction(
            repository=repository, broker=AlpacaPaperBroker()
        ).execute(instruction_id=instruction_id)
    except ExternalPaperApplicationError as error:
        raise SystemExit(str(error)) from error
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "instruction_id": instruction_id,
            "cancellation_evidence": evidence,
            "next_action": "SYNCHRONIZE_ALPACA_PAPER_INSTRUCTION",
            "live_authority": "NONE",
        }
    )


def alpaca_paper_close(*, instruction_id: str, position_id: str, confirmation: str) -> None:
    """Prepare one exact risk-reducing close; a separate reviewed submission remains required."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import PrepareAuthorizedExternalPaperReduction

    expected = f"CLOSE_ALPACA_PAPER:{instruction_id}:{position_id}"
    if confirmation != expected:
        raise SystemExit("EXACT_OPERATOR_CONFIRMATION_REQUIRED")
    now = datetime.now(tz=UTC)
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        result = PrepareAuthorizedExternalPaperReduction(
            repository=PostgresExternalPaperRepository(engine),
            securities_repository=PostgresSecuritiesPaperRepository(engine),
        ).execute(
            instruction_id=instruction_id,
            position_id=position_id,
            decision_at=now,
            earliest_fill_at=now + timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
        )
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "environment": "ALPACA_PAPER",
            "prepared_close": result,
            "broker_submission": False,
            "next_action": "SEPARATE_REVIEWED_PAPER_SUBMISSION_REQUIRED",
            "live_authority": "NONE",
        }
    )


def external_paper_cancel(*, instruction_id: str) -> None:
    """Request one deterministic-fixture cancellation for an exact instruction."""
    from apex.adapters.external_paper_fixture import DeterministicPaperBrokerFixture
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.external_paper import CancelExternalPaperInstruction

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        evidence = CancelExternalPaperInstruction(
            repository=PostgresExternalPaperRepository(engine),
            broker=DeterministicPaperBrokerFixture(scenarios={}, now=datetime.now(tz=UTC)),
        ).execute(instruction_id=instruction_id)
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "instruction_id": instruction_id,
            "evidence": evidence,
            "state": "CANCELLATION_RETAINED",
            "blocker": None,
            "next_action": "SYNCHRONIZE_FIXTURE_CANCELLATION",
            "broker_cancel": "FIXTURE_ONLY",
            "live_authority": "NONE",
        }
    )


def external_paper_apply_fills(*, instruction_id: str) -> None:
    """Apply only already-retained validated fills; this has no broker I/O."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import ProcessExternalPaperFills

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        result = ProcessExternalPaperFills(
            repository=PostgresExternalPaperRepository(engine),
            securities_repository=PostgresSecuritiesPaperRepository(engine),
        ).execute(instruction_ids=(instruction_id,))[0]
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "instruction_id": result.instruction_id,
            "validated_fill_ids": result.validated_fill_ids,
            "applied_fill_ids": result.applied_fill_ids,
            "blocker": result.blocker,
            "next_action": (
                "RECONCILE_RETAINED_BROKER_OBSERVATION"
                if result.blocker is None
                else "RESOLVE_RETAINED_FILL_BLOCKER"
            ),
            "broker_submission": False,
            "broker_read": False,
            "live_authority": "NONE",
        }
    )


def external_paper_validate_fills(*, instruction_id: str, evidence_id: str) -> None:
    """Validate one retained broker observation without broker I/O."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.external_paper import ValidateExternalPaperFill

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        evidence = repository.broker_evidence(evidence_id)
        if evidence is None or evidence.instruction_id != instruction_id:
            raise SystemExit("EXTERNAL_PAPER_EVIDENCE_NOT_FOUND")
        validated = ValidateExternalPaperFill(repository=repository).execute(evidence=evidence)
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "instruction_id": instruction_id,
            "evidence_id": evidence_id,
            "validated_fill": validated,
            "state": validated.state.value,
            "next_action": "APPLY_VALIDATED_FILL",
            "broker_submission": False,
            "broker_read": False,
            "live_authority": "NONE",
        }
    )


def external_paper_reconcile(*, instruction_id: str) -> None:
    """Display immutable reconciliation reports without broker I/O."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        repository = PostgresExternalPaperRepository(engine)
        if repository.instruction(instruction_id) is None:
            raise SystemExit("EXTERNAL_PAPER_INSTRUCTION_NOT_FOUND")
        reports = repository.reconciliations_for_instruction(instruction_id)
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "instruction_id": instruction_id,
            "reconciliations": reports,
            "state": "RETAINED_RECONCILIATION_REPORT",
            "next_action": (
                "RETAINED_RECONCILIATION_AVAILABLE"
                if reports
                else "SYNCHRONIZE_THROUGH_APPROVED_BROKER_BOUNDARY"
            ),
            "broker_submission": False,
            "broker_read": False,
            "live_authority": "NONE",
        }
    )


def external_paper_kill(*, campaign_id: str, reason: str, confirmation: str) -> None:
    """Durably block new External PAPER openings for one Campaign."""
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.external_paper import KillExternalPaper

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        replayed = KillExternalPaper(repository=PostgresExternalPaperRepository(engine)).execute(
            campaign_id=campaign_id,
            reason=reason,
            confirmation_text=confirmation,
            operator_action_id=f"cli:external-paper-kill:{campaign_id}",
        )
    finally:
        engine.dispose()
    _strategy_lab_json(
        {
            "campaign_id": campaign_id,
            "state": "KILLED",
            "replayed": replayed,
            "next_action": "SYNC_OR_REDUCE_EXISTING_EXTERNAL_PAPER_STATE",
            "live_authority": "NONE",
        }
    )


def external_paper_reconciliation_demo() -> None:
    from apex.adapters.external_paper_demo import external_paper_reconciliation_demo as run

    _strategy_lab_json(run())


def alpaca_paper_acceptance_demo() -> None:
    from apex.adapters.external_paper_demo import alpaca_paper_acceptance_demo as run

    _strategy_lab_json(run())


def external_paper_flatten(*, campaign_id: str, reason: str, confirmation: str) -> None:
    from apex.adapters.external_paper_fixture import DeterministicPaperBrokerFixture
    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.adapters.securities_paper_postgres import PostgresSecuritiesPaperRepository
    from apex.application.external_paper import FlattenExternalPaperCampaign

    migrate()
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        result = FlattenExternalPaperCampaign(
            repository=PostgresExternalPaperRepository(engine),
            broker=DeterministicPaperBrokerFixture(scenarios={}, now=datetime.now(tz=UTC)),
            securities_repository=PostgresSecuritiesPaperRepository(engine),
        ).execute(
            campaign_id=campaign_id,
            reason=reason,
            confirmation_text=confirmation,
            operator_action_id=f"cli:external-paper-flatten:{campaign_id}",
        )
    finally:
        engine.dispose()
    _strategy_lab_json(result)


def bounded_candidate_generation_demo() -> None:
    from apex.adapters.candidate_generation_demo import (
        bounded_candidate_generation_demo as run,
    )

    migrate()
    _strategy_lab_json(run(local_root=ROOT / ".local"))


def batch_learning_plan_create(
    *,
    name: str,
    source_scope: str,
    feature_schema_id: str,
    search_template: str,
    minimum_new_groups: int,
    minimum_new_observations: int,
    maximum_trials_per_cycle: int,
    maximum_cycles: int,
    reason: str,
    idempotency_key: str,
) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(
            runtime.create_plan(
                name=name,
                source_scope=source_scope,
                feature_schema_id=feature_schema_id,
                search_template=search_template,
                minimum_new_groups=minimum_new_groups,
                minimum_new_observations=minimum_new_observations,
                maximum_trials_per_cycle=maximum_trials_per_cycle,
                maximum_cycles=maximum_cycles,
                reason=reason,
                idempotency_key=idempotency_key,
            )
        )
    finally:
        runtime.close()


def batch_learning_plan_control(
    *,
    plan_id: str,
    action: str,
    reason: str,
    confirm_plan_id: str | None = None,
    confirm_plan_hash: str | None = None,
) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        result = (
            runtime.activate(
                plan_id=plan_id,
                confirm_plan_id=confirm_plan_id or "",
                confirm_plan_hash=confirm_plan_hash or "",
            )
            if action == "activate"
            else runtime.control(plan_id=plan_id, action=action, reason=reason)
        )
        _strategy_lab_json(result)
    finally:
        runtime.close()


def batch_learning_trigger(*, plan_id: str, idempotency_key: str) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.trigger(plan_id=plan_id, idempotency_key=idempotency_key))
    finally:
        runtime.close()


def batch_learning_cycle_run(*, plan_id: str, idempotency_key: str) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _window, cycle, _decision = runtime.trigger(
            plan_id=plan_id, idempotency_key=idempotency_key
        )
        _strategy_lab_json(runtime.run_cycle(cycle_id=cycle.cycle_id))
    finally:
        runtime.close()


def batch_learning_cycle_recover(*, cycle_id: str) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.recover(cycle_id=cycle_id))
    finally:
        runtime.close()


def batch_learning_cycle_cancel(*, cycle_id: str, reason: str) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.cancel(cycle_id=cycle_id, reason=reason))
    finally:
        runtime.close()


def batch_learning_attach_challenger(
    *, cycle_id: str, deployment_id: str, idempotency_key: str
) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(
            runtime.attach(
                cycle_id=cycle_id,
                deployment_id=deployment_id,
                idempotency_key=idempotency_key,
            )
        )
    finally:
        runtime.close()


def batch_learning_forward_infer(*, window_id: str, cutoff: str) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(
            runtime.infer(
                window_id=window_id,
                cutoff_at=datetime.fromisoformat(cutoff.replace("Z", "+00:00")),
            )
        )
    finally:
        runtime.close()


def batch_learning_forward_evaluate(*, window_id: str, cutoff: str) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(
            runtime.resolve_and_evaluate(
                window_id=window_id,
                cutoff_at=datetime.fromisoformat(cutoff.replace("Z", "+00:00")),
            )
        )
    finally:
        runtime.close()


def batch_learning_status(*, plan_id: str) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.status(plan_id=plan_id))
    finally:
        runtime.close()


def batch_learning_report(*, cycle_id: str) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.report(cycle_id=cycle_id))
    finally:
        runtime.close()


def batch_learning_worker(*, plan_id: str | None) -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.worker_once(plan_id=plan_id))
    finally:
        runtime.close()


def batch_learning_readiness() -> None:
    from apex.adapters.batch_learning_operations import BatchLearningRuntime

    runtime = BatchLearningRuntime.open(local_root=ROOT / ".local")
    try:
        _strategy_lab_json(runtime.readiness())
    finally:
        runtime.close()


def automated_batch_learning_demo() -> None:
    from apex.adapters.batch_learning_demo import automated_batch_learning_demo as run

    migrate()
    _strategy_lab_json(run(local_root=ROOT / ".local"))


def portfolio_command_center_demo() -> None:
    """Demonstrate that research attractiveness cannot create financial authority."""
    from apex.application.portfolio_intelligence import (
        InMemoryPortfolioIntelligenceRepository,
        RunPortfolioIntelligence,
        command_center,
    )
    from apex.domain.portfolio_intelligence import (
        AuthorityCeiling,
        OpportunityMaturity,
        OpportunitySource,
        PortfolioOpportunity,
    )

    blocked = PortfolioOpportunity.create(
        source=OpportunitySource.SECURITIES_META_STRATEGY,
        source_id="retained-genuine-ensemble",
        instrument_id="SPY",
        expected_net_return="0.01",
        positive_probability="0.60",
        downside="-0.02",
        uncertainty="0.20",
        liquidity="1",
        maturity=OpportunityMaturity.INTEGRITY_BLOCKED,
        authority_ceiling=AuthorityCeiling.RESEARCH_ONLY,
        blockers=("PBO_ABOVE_MAXIMUM",),
    )
    cash = PortfolioOpportunity.create(
        source=OpportunitySource.CASH,
        source_id="cash",
        instrument_id="USD",
        expected_net_return="0",
        positive_probability="1",
        downside="0",
        uncertainty="0",
        liquidity="1",
        maturity=OpportunityMaturity.FORWARD_SHADOW_EARLY,
        authority_ceiling=AuthorityCeiling.RESEARCH_ONLY,
    )
    run = RunPortfolioIntelligence(InMemoryPortfolioIntelligenceRepository()).execute(
        opportunities=(blocked, cash), idempotency_key="portfolio-command-center-demo-v1"
    )
    _strategy_lab_json(
        command_center(
            portfolio=run,
            shadow={"authority": "OBSERVATION_ONLY", "blocker": "PBO_ABOVE_MAXIMUM"},
        )
    )


def _runtime_state_path() -> Path:
    path = ROOT / ".local" / "runtime" / "shadow-runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _runtime_state() -> dict[str, object]:
    path = _runtime_state_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _database_running() -> bool:
    result = subprocess.run(
        (_docker(), "compose", "-f", str(ROOT / "compose.yaml"), "ps", "-q", "postgres"),
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def securities_shadow_scheduler_run_due(*, plan_id: str) -> None:
    """A scheduler tick delegates to the same bounded daily operation."""
    securities_shadow_daily(plan_id=plan_id, no_provider=True, keep_db_running=True, as_json=True)


def securities_shadow_scheduler_start(*, plan_id: str) -> None:
    state = _runtime_state()
    pid = state.get("scheduler_pid")
    if isinstance(pid, int):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 0)
            raise SystemExit("owned SHADOW scheduler is already running")
    command = (
        _python(),
        str(ROOT / "scripts" / "dev.py"),
        "securities-shadow-scheduler-loop",
        "--plan-id",
        plan_id,
    )
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=_environment(),
        stdout=(ROOT / ".local" / "runtime" / "shadow-scheduler.log").open("a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    state.update(
        {
            "scheduler_pid": process.pid,
            "plan_id": plan_id,
            "started_at": datetime.now(tz=UTC).isoformat(),
        }
    )
    _runtime_state_path().write_text(json.dumps(state, sort_keys=True))
    _strategy_lab_json({"scheduler": "STARTED", "pid": process.pid, "plan_id": plan_id})


def securities_shadow_scheduler_loop(*, plan_id: str) -> None:
    """Local foreground loop; no cron, systemd, cloud, or remote worker."""
    while True:
        securities_shadow_scheduler_run_due(plan_id=plan_id)
        time.sleep(60)


def securities_shadow_scheduler_stop() -> None:
    state = _runtime_state()
    pid = state.get("scheduler_pid")
    if isinstance(pid, int):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
    _runtime_state_path().unlink(missing_ok=True)
    _strategy_lab_json({"scheduler": "STOPPED"})


def securities_shadow_scheduler_status() -> None:
    state = _runtime_state()
    pid = state.get("scheduler_pid")
    running = False
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            running = True
        except ProcessLookupError:
            pass
    _strategy_lab_json({"scheduler": "RUNNING" if running else "STOPPED", "state": state})


def apex_runtime_up(*, plan_id: str) -> None:
    state = _runtime_state()
    started_db = not _database_running()
    if started_db:
        db_up()
    state["database_owned"] = started_db
    _runtime_state_path().write_text(json.dumps(state, sort_keys=True))
    securities_shadow_scheduler_start(plan_id=plan_id)


def apex_runtime_down() -> None:
    state = _runtime_state()
    securities_shadow_scheduler_stop()
    if state.get("database_owned") is True:
        db_down(volumes=False)
    _strategy_lab_json(
        {"runtime": "STOPPED", "database_stopped": state.get("database_owned") is True}
    )


def apex_runtime_status() -> None:
    state = _runtime_state()
    _strategy_lab_json(
        {"database": "RUNNING" if _database_running() else "STOPPED", "state": state}
    )


def portfolio_intelligence_current(*, plan_id: str, idempotency_key: str) -> None:
    """Normalize retained SHADOW predictions into a non-executable current run."""
    from apex.adapters.portfolio_intelligence_postgres import (
        PostgresPortfolioIntelligenceRepository,
    )
    from apex.adapters.sustained_shadow_postgres import PostgresSustainedShadowRepository
    from apex.application.portfolio_intelligence import RunPortfolioIntelligence, command_center
    from apex.domain.portfolio_intelligence import (
        AuthorityCeiling,
        OpportunityMaturity,
        OpportunitySource,
        PortfolioOpportunity,
    )

    migrate()
    engine, _real, _configuration, _adapter = _real_securities_provider_stack()
    try:
        shadows = PostgresSustainedShadowRepository(engine)
        plan = shadows.plan(plan_id)
        if plan is None:
            raise SystemExit("sustained SHADOW plan was not found")
        opportunities = tuple(
            PortfolioOpportunity.create(
                source=OpportunitySource.SECURITIES_META_STRATEGY,
                source_id=plan.ensemble_run_id,
                instrument_id=prediction.instrument_id,
                expected_net_return=str(prediction.expected_net_return),
                positive_probability=str(prediction.probability),
                downside=str(min(prediction.expected_net_return, Decimal())),
                uncertainty=str(prediction.uncertainty),
                liquidity="1",
                maturity=OpportunityMaturity.INTEGRITY_BLOCKED,
                authority_ceiling=AuthorityCeiling.RESEARCH_ONLY,
                blockers=plan.blockers,
            )
            for prediction in shadows.predictions(plan.plan_id)
        )
        cash = PortfolioOpportunity.create(
            source=OpportunitySource.CASH,
            source_id="usd-cash-v1",
            instrument_id="USD",
            expected_net_return="0",
            positive_probability="1",
            downside="0",
            uncertainty="0",
            liquidity="1",
            maturity=OpportunityMaturity.FORWARD_SHADOW_EARLY,
            authority_ceiling=AuthorityCeiling.RESEARCH_ONLY,
        )
        run = RunPortfolioIntelligence(PostgresPortfolioIntelligenceRepository(engine)).execute(
            opportunities=(*opportunities, cash), idempotency_key=idempotency_key
        )
        _strategy_lab_json(
            command_center(
                portfolio=run,
                shadow={
                    "plan_id": plan.plan_id,
                    "authority": "OBSERVATION_ONLY",
                    "blockers": plan.blockers,
                },
            )
        )
    finally:
        engine.dispose()


def portfolio_mandate_command(arguments: argparse.Namespace) -> None:
    """Bounded command surface for Normalized Portfolio State + Investment
    Mandate v1: ``portfolio-snapshot-*`` builds/inspects provider-neutral
    normalized portfolio truth from already-retained Crypto/Kalshi/
    Securities PAPER evidence (and, optionally, already-retrieved genuine
    External PAPER (Alpaca) broker evidence supplied by evidence id -- this
    command never itself performs a provider read); ``investment-mandate-*``
    authors/inspects a durable, versioned Investment Mandate and
    deterministically evaluates a retained Opportunity against it. No
    Strategy, Order, PAPER order, reservation, or LIVE authority is ever
    created here; ``authority`` stays ``NONE`` throughout, and zero provider
    requests are made by any command in this family."""

    from apex.adapters.external_paper_postgres import PostgresExternalPaperRepository
    from apex.adapters.investment_mandate_postgres import PostgresInvestmentMandateRepository
    from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
    from apex.adapters.portfolio_state_sources import PostgresPortfolioStateSource
    from apex.adapters.postgres import PostgresSettings, create_postgres_engine
    from apex.application.investment_mandate import (
        CreateInvestmentMandate,
        EvaluateOpportunityAgainstMandate,
    )
    from apex.application.portfolio_state import (
        BuildPortfolioSnapshot,
        ExternalPaperAccountEvidence,
        PortfolioSourceSelection,
    )
    from apex.domain.investment_mandate import (
        DEFAULT_MANDATE_LINEAGE_ID,
        AutonomyRequirement,
        InvestmentMandateError,
    )
    from apex.domain.values import deterministic_id

    def parsed(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    command = arguments.command
    engine = create_postgres_engine(PostgresSettings.from_env())
    try:
        if command == "portfolio-snapshot-build":
            as_of = parsed(arguments.as_of) if arguments.as_of else datetime.now(tz=UTC)
            external_accounts: tuple[ExternalPaperAccountEvidence, ...] = ()
            if arguments.external_paper_account_id:
                external_repo = PostgresExternalPaperRepository(engine)
                account_evidence = (
                    external_repo.broker_evidence(arguments.external_paper_account_evidence_id)
                    if arguments.external_paper_account_evidence_id
                    else None
                )
                position_evidence = tuple(
                    evidence
                    for evidence_id in arguments.external_paper_position_evidence_id
                    if (evidence := external_repo.broker_evidence(evidence_id)) is not None
                )
                external_accounts = (
                    ExternalPaperAccountEvidence(
                        account_id=arguments.external_paper_account_id,
                        account_evidence=account_evidence,
                        position_evidence=position_evidence,
                    ),
                )
            selection = PortfolioSourceSelection(
                crypto_account_ids=tuple(arguments.crypto_account_id),
                kalshi_account_ids=tuple(arguments.kalshi_account_id),
                securities_campaign_ids=tuple(arguments.securities_campaign_id),
                external_paper_accounts=external_accounts,
            )
            snapshot_id = arguments.snapshot_id or f"portfolio-snapshot-{as_of.isoformat()}"
            idempotency_key = arguments.idempotency_key or deterministic_id(
                "portfolio_snapshot_build_request", (snapshot_id, selection)
            )
            service = BuildPortfolioSnapshot(
                PostgresPortfolioSnapshotRepository(engine), PostgresPortfolioStateSource(engine)
            )
            build_result = service.execute(
                snapshot_id=snapshot_id,
                as_of=as_of,
                created_at=datetime.now(tz=UTC),
                idempotency_key=idempotency_key,
                selection=selection,
            )
            _research_evidence_json(
                {
                    "command": command,
                    "snapshot": build_result.snapshot,
                    "account_balances": build_result.account_balances,
                    "positions": build_result.positions,
                    "idempotent_replay": build_result.idempotent_replay,
                    "authority": "NONE",
                }
            )
            return

        if command == "portfolio-snapshot-inspect":
            portfolio_repository = PostgresPortfolioSnapshotRepository(engine)
            snapshot = (
                portfolio_repository.portfolio_snapshot(arguments.snapshot_id)
                if arguments.snapshot_id
                else portfolio_repository.latest_portfolio_snapshot()
            )
            if snapshot is None:
                _research_evidence_json(
                    {
                        "command": command,
                        "snapshot": None,
                        "warnings": ("NO_PORTFOLIO_SNAPSHOT_RETAINED",),
                        "authority": "NONE",
                    }
                )
                return
            _research_evidence_json(
                {
                    "command": command,
                    "snapshot": snapshot,
                    "account_balances": portfolio_repository.portfolio_account_balances(
                        snapshot.snapshot_id
                    ),
                    "positions": portfolio_repository.portfolio_positions(snapshot.snapshot_id),
                    "authority": "NONE",
                }
            )
            return

        if command == "investment-mandate-create":
            mandate_repository = PostgresInvestmentMandateRepository(engine)
            effective_at = parsed(arguments.effective_at)
            created_at = parsed(arguments.created_at) if arguments.created_at else effective_at
            lineage_id = arguments.lineage_id or DEFAULT_MANDATE_LINEAGE_ID
            idempotency_key = arguments.idempotency_key or deterministic_id(
                "investment_mandate_create_request",
                (lineage_id, effective_at, arguments.total_capital_allocated),
            )
            create_result = CreateInvestmentMandate(mandate_repository).execute(
                idempotency_key=idempotency_key,
                lineage_id=lineage_id,
                effective_at=effective_at,
                created_at=created_at,
                total_capital_allocated=arguments.total_capital_allocated,
                permitted_venues=tuple(arguments.permitted_venue),
                permitted_market_families=tuple(arguments.permitted_market_family),
                disallowed_market_families=tuple(arguments.disallowed_market_family),
                maximum_aggregate_exposure_fraction=arguments.maximum_aggregate_exposure_fraction,
                maximum_per_instrument_fraction=arguments.maximum_per_instrument_fraction,
                maximum_venue_concentration_fraction=arguments.maximum_venue_concentration_fraction,
                maximum_market_family_concentration_fraction=(
                    arguments.maximum_market_family_concentration_fraction
                ),
                leverage_permitted=arguments.leverage_permitted,
                short_exposure_permitted=arguments.short_exposure_permitted,
                options_permitted=arguments.options_permitted,
                options_defined_risk_only=not arguments.options_undefined_risk_permitted,
                minimum_liquidity_state=arguments.minimum_liquidity_state,
                maximum_capital_lockup_days=arguments.maximum_capital_lockup_days,
                autonomy_requirement=AutonomyRequirement(arguments.autonomy_requirement),
                notes=arguments.notes,
            )
            _research_evidence_json(
                {
                    "command": command,
                    "mandate": create_result.mandate,
                    "idempotent_replay": create_result.idempotent_replay,
                    "authority": "NONE",
                }
            )
            return

        if command == "investment-mandate-inspect":
            mandate_repository = PostgresInvestmentMandateRepository(engine)
            lineage_id = arguments.lineage_id or DEFAULT_MANDATE_LINEAGE_ID
            as_of = parsed(arguments.as_of) if arguments.as_of else datetime.now(tz=UTC)
            mandate = (
                mandate_repository.mandate(arguments.mandate_id)
                if arguments.mandate_id
                else mandate_repository.active_mandate(lineage_id, as_of=as_of)
            )
            if mandate is None:
                _research_evidence_json(
                    {
                        "command": command,
                        "lineage_id": lineage_id,
                        "as_of": as_of,
                        "mandate": None,
                        "warnings": ("NO_ACTIVE_MANDATE_FOR_LINEAGE",),
                        "authority": "NONE",
                    }
                )
                return
            latest = mandate_repository.latest_mandate_version(mandate.lineage_id)
            _research_evidence_json(
                {
                    "command": command,
                    "as_of": as_of,
                    "mandate": mandate,
                    "is_latest_version": latest is not None
                    and latest.mandate_id == mandate.mandate_id,
                    "authority": "NONE",
                }
            )
            return

        if command == "investment-mandate-evaluate":
            mandate_repository = PostgresInvestmentMandateRepository(engine)
            evaluated_at = (
                parsed(arguments.evaluated_at) if arguments.evaluated_at else datetime.now(tz=UTC)
            )
            lineage_id = arguments.lineage_id or DEFAULT_MANDATE_LINEAGE_ID
            idempotency_key = arguments.idempotency_key or deterministic_id(
                "mandate_evaluation_request",
                (
                    lineage_id,
                    arguments.opportunity_id,
                    evaluated_at,
                    arguments.portfolio_snapshot_id,
                ),
            )
            try:
                evaluate_result = EvaluateOpportunityAgainstMandate(mandate_repository).execute(
                    idempotency_key=idempotency_key,
                    opportunity_id=arguments.opportunity_id,
                    evaluated_at=evaluated_at,
                    mandate_lineage_id=lineage_id,
                    portfolio_snapshot_id=arguments.portfolio_snapshot_id,
                )
            except InvestmentMandateError as error:
                # A missing mandate, portfolio snapshot, or Opportunity is an
                # honest, anticipatable operator-facing outcome -- report it
                # structured, exactly like every other unknown/unavailable
                # fact in this family, rather than surfacing a traceback.
                _research_evidence_json(
                    {
                        "command": command,
                        "evaluation": None,
                        "lineage_id": lineage_id,
                        "opportunity_id": arguments.opportunity_id,
                        "evaluated_at": evaluated_at,
                        "warnings": (str(error),),
                        "authority": "NONE",
                    }
                )
                return
            _research_evidence_json(
                {
                    "command": command,
                    "evaluation": evaluate_result.evaluation,
                    "idempotent_replay": evaluate_result.idempotent_replay,
                    "authority": "NONE",
                }
            )
            return

        raise SystemExit(f"unsupported portfolio/mandate command {command!r}")
    finally:
        engine.dispose()


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(description="Apex development commands")
    subparsers = command_parser.add_subparsers(dest="command", required=True)
    commands = (
        "bootstrap",
        "db-up",
        "db-identity",
        "format",
        "lint",
        "migrate",
        "test",
        "test-offline",
        "test-persistence",
        "typecheck",
        "verify-core",
        "operator-api",
        "operator-ui",
        "operator-build",
        "operator-app",
        "operator-demo-seed",
        "operator-console-demo",
        "evidence-review-paper-trial-demo",
        "strategy-lab-demo",
        "strategy-lab-readiness",
        "strategy-lab-configuration-readiness",
        "strategy-lab-shadow-demo",
        "securities-shadow-demo",
        "securities-managed-data-readiness",
        "securities-real-data-readiness",
        "real-securities-data-contract-demo",
        "securities-lean-availability",
        "securities-paper-demo",
        "securities-baselines",
        "securities-simulator-status",
        "securities-baselines-conformance-demo",
        "securities-research-trial-summary",
        "research-overfitting-demo",
        "research-integrity-readiness",
        "research-trial-ledger-summary",
        "automated-search-readiness",
        "bounded-candidate-generation-demo",
        "automated-batch-learning-demo",
        "portfolio-command-center-demo",
        "batch-learning-readiness",
        "strategy-intelligence-demo",
        "ensemble-meta-strategy-demo",
        "sustained-real-shadow-demo",
        "broad-equity-universe-demo",
        "broad-equity-discovery-demo",
        "options-intelligence-demo",
        "mlflow-up",
        "mlflow-down",
        "mlflow-status",
    )
    for command in commands:
        subparsers.add_parser(command)
    verify_persistence_parser = subparsers.add_parser("verify-persistence")
    verify_persistence_parser.add_argument("--pytest-arg", action="append", default=[])
    research_hypothesis_register = subparsers.add_parser("research-hypothesis-register")
    research_hypothesis_register.add_argument("--hypothesis-id", required=True)
    research_hypothesis_register.add_argument("--revision", type=int, required=True)
    research_hypothesis_register.add_argument("--question", required=True)
    research_hypothesis_register.add_argument("--motivation", required=True)
    research_hypothesis_register.add_argument("--actor", required=True)
    research_hypothesis_register.add_argument("--market-scope", required=True)
    research_hypothesis_register.add_argument("--supersedes-version-id")
    research_hypothesis_inspect = subparsers.add_parser("research-hypothesis-inspect")
    research_hypothesis_inspect.add_argument("--hypothesis-version-id", required=True)
    research_capture_start = subparsers.add_parser("research-capture-start")
    research_capture_start.add_argument("--capture-id", required=True)
    research_capture_start.add_argument("--mode", choices=("fixture", "genuine"), required=True)
    research_capture_start.add_argument("--chunk-size", type=int, required=True)
    research_capture_start.add_argument("--timeout-seconds", type=int, default=10)
    research_capture_start.add_argument("--maximum-records", type=int, default=20_000)
    research_capture_resume = subparsers.add_parser("research-capture-resume")
    research_capture_resume.add_argument("--capture-id", required=True)
    research_capture_resume.add_argument("--chunk-size", type=int, required=True)
    research_capture_inspect = subparsers.add_parser("research-capture-inspect")
    research_capture_inspect.add_argument("--capture-id", required=True)
    research_snapshot_materialize = subparsers.add_parser("research-snapshot-materialize")
    research_snapshot_materialize.add_argument("--snapshot-id", required=True)
    research_snapshot_materialize.add_argument("--capture-ids", required=True)
    research_snapshot_inspect = subparsers.add_parser("research-snapshot-inspect")
    research_snapshot_inspect.add_argument("--snapshot-id", required=True)
    research_experiment_run = subparsers.add_parser("research-experiment-run")
    research_experiment_run.add_argument("--run-id", required=True)
    research_experiment_run.add_argument("--hypothesis-version-id", required=True)
    research_experiment_run.add_argument("--snapshot-id", required=True)
    research_experiment_run.add_argument("--minimum-eligible-count", type=int, required=True)
    research_experiment_replay = subparsers.add_parser("research-experiment-replay")
    research_experiment_replay.add_argument("--run-id", required=True)
    research_experiment_inspect = subparsers.add_parser("research-experiment-inspect")
    research_experiment_inspect.add_argument("--run-id", required=True)
    research_outcome_record = subparsers.add_parser("research-outcome-record")
    research_outcome_record.add_argument("--outcome-id", required=True)
    research_outcome_record.add_argument("--run-id", required=True)
    research_outcome_record.add_argument(
        "--decision",
        choices=("REJECTED", "DEFERRED", "PROMOTION_CANDIDATE"),
        required=True,
    )
    research_outcome_record.add_argument("--actor", required=True)
    research_outcome_record.add_argument("--reason", required=True)
    research_outcome_inspect = subparsers.add_parser("research-outcome-inspect")
    research_outcome_inspect.add_argument("--outcome-id", required=True)
    subparsers.add_parser("research-evidence-workspace")
    crypto_exposure_record = subparsers.add_parser("crypto-exposure-record")
    crypto_exposure_record.add_argument("--exposure-id", required=True)
    crypto_exposure_record.add_argument("--snapshot-id", required=True)
    crypto_exposure_record.add_argument("--purpose", required=True)
    crypto_exposure_record.add_argument(
        "--category",
        choices=(
            "DEVELOPMENT_EXPOSED",
            "VALIDATION_EXPOSED",
            "REPLICATION_EXPOSED",
            "OPERATOR_EXPOSED",
            "DOSSIER_EXPOSED",
            "SEALED_HOLDOUT",
            "HOLDOUT_RELEASED",
            "HOLDOUT_EVALUATED",
            "INVALIDATED",
        ),
        required=True,
    )
    crypto_exposure_record.add_argument("--first-known-exposure-at", required=True)
    crypto_exposure_record.add_argument("--actor", required=True)
    crypto_exposure_record.add_argument("--reason", required=True)
    crypto_exposure_record.add_argument("--idempotency-key", required=True)
    for related in (
        "hypothesis",
        "strategy",
        "protocol",
        "experiment",
        "dossier",
    ):
        crypto_exposure_record.add_argument(f"--related-{related}-id", action="append", default=[])
    crypto_exposure_record.add_argument("--known-limitation", action="append", default=[])
    crypto_exposure_inspect = subparsers.add_parser("crypto-exposure-inspect")
    crypto_exposure_inspect.add_argument("--snapshot-id", required=True)
    crypto_campaign_register = subparsers.add_parser("crypto-campaign-register")
    crypto_campaign_register.add_argument("--campaign-id", required=True)
    crypto_campaign_register.add_argument("--actor", required=True)
    crypto_campaign_register.add_argument("--idempotency-key", required=True)
    crypto_campaign_attach = subparsers.add_parser("crypto-campaign-attach-dataset")
    crypto_campaign_attach.add_argument("--attachment-id", required=True)
    crypto_campaign_attach.add_argument("--campaign-id", required=True)
    crypto_campaign_attach.add_argument("--snapshot-id", required=True)
    crypto_campaign_attach.add_argument("--role", choices=("DEVELOPMENT", "HOLDOUT"), required=True)
    crypto_campaign_attach.add_argument("--actor", required=True)
    crypto_campaign_attach.add_argument("--reason", required=True)
    crypto_campaign_attach.add_argument("--idempotency-key", required=True)
    for command in (
        "crypto-campaign-inspect",
        "crypto-campaign-budget",
        "crypto-campaign-nomination-eligibility",
    ):
        campaign_read = subparsers.add_parser(command)
        campaign_read.add_argument("--campaign-id", required=True)
    crypto_snapshot_access = subparsers.add_parser("crypto-snapshot-access-check")
    crypto_snapshot_access.add_argument("--snapshot-id", required=True)
    crypto_snapshot_access.add_argument(
        "--purpose",
        choices=(
            "DEVELOPMENT",
            "RETROSPECTIVE_COMPARISON",
            "REPLAY",
            "INDEPENDENT_VALIDATION",
            "HOLDOUT_EVALUATION",
            "METADATA_INSPECTION",
        ),
        required=True,
    )
    crypto_snapshot_access.add_argument("--campaign-id")
    crypto_snapshot_access.add_argument("--candidate-id")
    crypto_holdout_start = subparsers.add_parser("crypto-holdout-capture-start")
    crypto_holdout_start.add_argument("--campaign-id", required=True)
    crypto_holdout_start.add_argument("--capture-id", required=True)
    crypto_holdout_start.add_argument("--mode", choices=("fixture", "genuine"), required=True)
    crypto_holdout_start.add_argument("--actor", required=True)
    crypto_holdout_start.add_argument("--idempotency-key", required=True)
    crypto_holdout_resume = subparsers.add_parser("crypto-holdout-capture-resume")
    crypto_holdout_resume.add_argument("--campaign-id", required=True)
    crypto_holdout_resume.add_argument("--capture-id", required=True)
    crypto_holdout_resume.add_argument("--actor", required=True)
    crypto_holdout_resume.add_argument("--idempotency-key", required=True)
    crypto_holdout_checkpoints = subparsers.add_parser("crypto-holdout-checkpoints")
    crypto_holdout_checkpoints.add_argument("--capture-id", required=True)
    crypto_holdout_materialize = subparsers.add_parser("crypto-holdout-materialize")
    crypto_holdout_materialize.add_argument("--campaign-id", required=True)
    crypto_holdout_materialize.add_argument("--snapshot-id", required=True)
    crypto_holdout_materialize.add_argument("--idempotency-key", required=True)
    crypto_holdout_seal = subparsers.add_parser("crypto-holdout-seal")
    crypto_holdout_seal.add_argument("--event-id", required=True)
    crypto_holdout_seal.add_argument("--exposure-id", required=True)
    crypto_holdout_seal.add_argument("--attachment-id", required=True)
    crypto_holdout_seal.add_argument("--campaign-id", required=True)
    crypto_holdout_seal.add_argument("--snapshot-id", required=True)
    crypto_holdout_seal.add_argument("--actor", required=True)
    crypto_holdout_seal.add_argument("--reason", required=True)
    crypto_holdout_seal.add_argument("--idempotency-key", required=True)
    crypto_holdout_inspect = subparsers.add_parser("crypto-holdout-inspect")
    crypto_holdout_inspect.add_argument("--campaign-id", required=True)
    crypto_holdout_inspect.add_argument("--holdout-id", required=True)
    crypto_holdout_release = subparsers.add_parser("crypto-holdout-release-authorize")
    crypto_holdout_release.add_argument("--campaign-event-id", required=True)
    crypto_holdout_release.add_argument("--holdout-event-id", required=True)
    crypto_holdout_release.add_argument("--exposure-id", required=True)
    crypto_holdout_release.add_argument("--campaign-id", required=True)
    crypto_holdout_release.add_argument("--candidate-id", required=True)
    crypto_holdout_release.add_argument("--actor", required=True)
    crypto_holdout_release.add_argument("--reason", required=True)
    crypto_holdout_release.add_argument("--idempotency-key", required=True)
    crypto_hypothesis_register = subparsers.add_parser("crypto-hypothesis-register")
    crypto_hypothesis_register.add_argument("--hypothesis-id", required=True)
    crypto_hypothesis_register.add_argument("--revision", type=int, required=True)
    crypto_hypothesis_register.add_argument("--actor", required=True)
    crypto_hypothesis_register.add_argument("--supersedes-version-id")
    crypto_hypothesis_inspect = subparsers.add_parser("crypto-hypothesis-inspect")
    crypto_hypothesis_inspect.add_argument("--hypothesis-version-id", required=True)
    crypto_capture_start = subparsers.add_parser("crypto-capture-start")
    crypto_capture_start.add_argument("--capture-id", required=True)
    crypto_capture_start.add_argument("--mode", choices=("fixture", "genuine"), required=True)
    crypto_capture_start.add_argument("--product", required=True)
    crypto_capture_start.add_argument("--start", required=True)
    crypto_capture_start.add_argument("--end", required=True)
    crypto_capture_resume = subparsers.add_parser("crypto-capture-resume")
    crypto_capture_resume.add_argument("--capture-id", required=True)
    crypto_capture_inspect = subparsers.add_parser("crypto-capture-inspect")
    crypto_capture_inspect.add_argument("--capture-id", required=True)
    crypto_snapshot_materialize = subparsers.add_parser("crypto-snapshot-materialize")
    crypto_snapshot_materialize.add_argument("--snapshot-id", required=True)
    crypto_snapshot_materialize.add_argument("--capture-id", required=True)
    crypto_snapshot_inspect = subparsers.add_parser("crypto-snapshot-inspect")
    crypto_snapshot_inspect.add_argument("--snapshot-id", required=True)
    crypto_experiment_run = subparsers.add_parser("crypto-experiment-run")
    crypto_experiment_run.add_argument("--run-id", required=True)
    crypto_experiment_run.add_argument("--hypothesis-version-id", required=True)
    crypto_experiment_run.add_argument("--snapshot-id", required=True)
    crypto_experiment_replay = subparsers.add_parser("crypto-experiment-replay")
    crypto_experiment_replay.add_argument("--run-id", required=True)
    crypto_experiment_inspect = subparsers.add_parser("crypto-experiment-inspect")
    crypto_experiment_inspect.add_argument("--run-id", required=True)
    crypto_outcome_record = subparsers.add_parser("crypto-outcome-record")
    crypto_outcome_record.add_argument("--outcome-id", required=True)
    crypto_outcome_record.add_argument("--run-id", required=True)
    crypto_outcome_record.add_argument("--actor", required=True)
    crypto_outcome_inspect = subparsers.add_parser("crypto-outcome-inspect")
    crypto_outcome_inspect.add_argument("--outcome-id", required=True)
    crypto_strategy_register = subparsers.add_parser("crypto-strategy-register")
    crypto_strategy_register.add_argument("--specification-id", required=True)
    crypto_strategy_inspect = subparsers.add_parser("crypto-strategy-inspect")
    crypto_strategy_inspect.add_argument("--specification-id", required=True)
    crypto_strategy_check = subparsers.add_parser("crypto-strategy-check")
    crypto_strategy_check.add_argument("--semantics-hash", required=True)
    crypto_replication_protocol_register = subparsers.add_parser(
        "crypto-replication-protocol-register"
    )
    crypto_replication_protocol_register.add_argument("--protocol-id", required=True)
    crypto_replication_protocol_register.add_argument("--strategy-specification-id", required=True)
    crypto_replication_protocol_inspect = subparsers.add_parser(
        "crypto-replication-protocol-inspect"
    )
    crypto_replication_protocol_inspect.add_argument("--protocol-id", required=True)
    crypto_replication_run = subparsers.add_parser("crypto-replication-run")
    crypto_replication_run.add_argument("--run-id", required=True)
    crypto_replication_run.add_argument("--protocol-id", required=True)
    crypto_replication_run.add_argument("--snapshot-id", required=True)
    crypto_replication_replay = subparsers.add_parser("crypto-replication-replay")
    crypto_replication_replay.add_argument("--run-id", required=True)
    crypto_replication_inspect = subparsers.add_parser("crypto-replication-inspect")
    crypto_replication_inspect.add_argument("--run-id", required=True)
    crypto_replication_outcome_record = subparsers.add_parser("crypto-replication-outcome-record")
    crypto_replication_outcome_record.add_argument("--outcome-id", required=True)
    crypto_replication_outcome_record.add_argument("--run-id", required=True)
    crypto_replication_outcome_record.add_argument("--actor", required=True)
    crypto_replication_outcome_inspect = subparsers.add_parser("crypto-replication-outcome-inspect")
    crypto_replication_outcome_inspect.add_argument("--outcome-id", required=True)
    crypto_dossier_materialize = subparsers.add_parser("crypto-dossier-materialize")
    crypto_dossier_materialize.add_argument("--dossier-id", required=True)
    crypto_dossier_materialize.add_argument("--replication-outcome-id", required=True)
    crypto_dossier_inspect = subparsers.add_parser("crypto-dossier-inspect")
    crypto_dossier_inspect.add_argument("--dossier-id", required=True)
    crypto_dossier_conclusion_record = subparsers.add_parser("crypto-dossier-conclusion-record")
    crypto_dossier_conclusion_record.add_argument("--conclusion-id", required=True)
    crypto_dossier_conclusion_record.add_argument("--dossier-id", required=True)
    crypto_dossier_conclusion_record.add_argument("--actor", required=True)
    crypto_dossier_conclusion_inspect = subparsers.add_parser("crypto-dossier-conclusion-inspect")
    crypto_dossier_conclusion_inspect.add_argument("--conclusion-id", required=True)
    for command in (
        "crypto-candidate-definition-validate",
        "crypto-candidate-definition-compile",
        "crypto-candidate-definition-register",
        "crypto-candidate-definition-compare",
    ):
        generic_definition_command = subparsers.add_parser(command)
        generic_definition_command.add_argument("--definition-id", required=True)
        generic_definition_command.add_argument("--definition-file", required=True)
        if command == "crypto-candidate-definition-register":
            generic_definition_command.add_argument("--idempotency-key", required=True)
    generic_definition_inspect = subparsers.add_parser("crypto-candidate-definition-inspect")
    generic_definition_inspect.add_argument("--definition-id", required=True)
    subparsers.add_parser("crypto-candidate-generic-compatibility")
    subparsers.add_parser("crypto-candidate-generic-fixture-acceptance")
    generic_policy_review = subparsers.add_parser("crypto-candidate-generic-policy-review")
    generic_policy_review.add_argument("--campaign-id", required=True)
    subparsers.add_parser("crypto-candidate-generic-status")
    candidate_hypothesis_register = subparsers.add_parser("crypto-candidate-hypothesis-register")
    candidate_hypothesis_register.add_argument("--hypothesis-id", required=True)
    candidate_hypothesis_register.add_argument("--campaign-id", required=True)
    candidate_hypothesis_register.add_argument("--actor", required=True)
    candidate_hypothesis_register.add_argument("--idempotency-key", required=True)
    candidate_hypothesis_inspect = subparsers.add_parser("crypto-candidate-hypothesis-inspect")
    candidate_hypothesis_inspect.add_argument("--hypothesis-version-id", required=True)
    candidate_strategy_register = subparsers.add_parser("crypto-candidate-strategy-register")
    candidate_strategy_register.add_argument("--specification-id", required=True)
    candidate_strategy_register.add_argument("--hypothesis-version-id", required=True)
    candidate_strategy_register.add_argument("--idempotency-key", required=True)
    candidate_strategy_inspect = subparsers.add_parser("crypto-candidate-strategy-inspect")
    candidate_strategy_inspect.add_argument("--specification-id", required=True)
    candidate_register = subparsers.add_parser("crypto-candidate-register")
    candidate_register.add_argument("--candidate-id", required=True)
    candidate_register.add_argument("--campaign-id", required=True)
    candidate_register.add_argument("--hypothesis-version-id", required=True)
    candidate_register.add_argument("--strategy-specification-id", required=True)
    candidate_register.add_argument("--actor", required=True)
    candidate_register.add_argument("--idempotency-key", required=True)
    for command in (
        "crypto-candidate-inspect",
        "crypto-candidate-comparison-inspect",
    ):
        candidate_read = subparsers.add_parser(command)
        candidate_read.add_argument("--candidate-id", required=True)
    candidate_protocol_register = subparsers.add_parser("crypto-candidate-protocol-register")
    candidate_protocol_register.add_argument("--protocol-id", required=True)
    candidate_protocol_register.add_argument("--campaign-id", required=True)
    candidate_protocol_register.add_argument("--candidate-id", required=True)
    candidate_protocol_register.add_argument("--idempotency-key", required=True)
    candidate_protocol_inspect = subparsers.add_parser("crypto-candidate-protocol-inspect")
    candidate_protocol_inspect.add_argument("--protocol-id", required=True)
    candidate_window_run = subparsers.add_parser("crypto-candidate-window-run")
    candidate_window_run.add_argument("--run-id", required=True)
    candidate_window_run.add_argument("--consumption-id", required=True)
    candidate_window_run.add_argument("--protocol-id", required=True)
    candidate_window_run.add_argument("--snapshot-id", required=True)
    candidate_window_run.add_argument("--actor", required=True)
    candidate_window_run.add_argument("--idempotency-key", required=True)
    for command in (
        "crypto-candidate-window-replay",
        "crypto-candidate-window-inspect",
        "crypto-candidate-decisions-inspect",
        "crypto-candidate-metrics-inspect",
    ):
        candidate_window_read = subparsers.add_parser(command)
        candidate_window_read.add_argument("--run-id", required=True)
    candidate_outcome_record = subparsers.add_parser("crypto-candidate-outcome-record")
    candidate_outcome_record.add_argument("--outcome-id", required=True)
    candidate_outcome_record.add_argument("--run-id", required=True)
    candidate_outcome_record.add_argument("--actor", required=True)
    candidate_outcome_record.add_argument("--idempotency-key", required=True)
    candidate_outcome_inspect = subparsers.add_parser("crypto-candidate-outcome-inspect")
    candidate_outcome_inspect.add_argument("--outcome-id", required=True)
    candidate_dossier_materialize = subparsers.add_parser("crypto-candidate-dossier-materialize")
    candidate_dossier_materialize.add_argument("--dossier-id", required=True)
    candidate_dossier_materialize.add_argument("--campaign-id", required=True)
    candidate_dossier_materialize.add_argument("--candidate-id", required=True)
    candidate_dossier_materialize.add_argument("--protocol-id", required=True)
    candidate_dossier_materialize.add_argument("--earlier-outcome-id", required=True)
    candidate_dossier_materialize.add_argument("--later-outcome-id", required=True)
    candidate_dossier_materialize.add_argument("--idempotency-key", required=True)
    candidate_dossier_inspect = subparsers.add_parser("crypto-candidate-dossier-inspect")
    candidate_dossier_inspect.add_argument("--dossier-id", required=True)
    candidate_nomination_apply = subparsers.add_parser("crypto-candidate-nomination-apply")
    candidate_nomination_apply.add_argument("--evaluation-id", required=True)
    candidate_nomination_apply.add_argument("--dossier-id", required=True)
    candidate_nomination_apply.add_argument("--nomination-event-id", required=True)
    candidate_nomination_apply.add_argument("--development-closed-event-id", required=True)
    candidate_nomination_apply.add_argument("--actor", required=True)
    candidate_nomination_apply.add_argument("--reason", required=True)
    candidate_nomination_apply.add_argument("--idempotency-key", required=True)
    candidate_nomination_inspect = subparsers.add_parser("crypto-candidate-nomination-inspect")
    candidate_nomination_inspect.add_argument("--evaluation-id", required=True)
    subparsers.add_parser("crypto-candidate-status")
    subparsers.add_parser("crypto-candidate-holdout-non-use")
    candidate_2_hypothesis_register = subparsers.add_parser(
        "crypto-candidate-2-hypothesis-register"
    )
    candidate_2_hypothesis_register.add_argument("--hypothesis-id", required=True)
    candidate_2_hypothesis_register.add_argument("--campaign-id", required=True)
    candidate_2_hypothesis_register.add_argument("--actor", required=True)
    candidate_2_hypothesis_register.add_argument("--idempotency-key", required=True)
    candidate_2_hypothesis_inspect = subparsers.add_parser("crypto-candidate-2-hypothesis-inspect")
    candidate_2_hypothesis_inspect.add_argument("--hypothesis-version-id", required=True)
    candidate_2_strategy_register = subparsers.add_parser("crypto-candidate-2-strategy-register")
    candidate_2_strategy_register.add_argument("--specification-id", required=True)
    candidate_2_strategy_register.add_argument("--hypothesis-version-id", required=True)
    candidate_2_strategy_register.add_argument("--actor", required=True)
    candidate_2_strategy_register.add_argument("--idempotency-key", required=True)
    candidate_2_strategy_inspect = subparsers.add_parser("crypto-candidate-2-strategy-inspect")
    candidate_2_strategy_inspect.add_argument("--specification-id", required=True)
    candidate_2_register = subparsers.add_parser("crypto-candidate-2-register")
    candidate_2_register.add_argument("--candidate-id", required=True)
    candidate_2_register.add_argument("--campaign-id", required=True)
    candidate_2_register.add_argument("--hypothesis-version-id", required=True)
    candidate_2_register.add_argument("--strategy-specification-id", required=True)
    candidate_2_register.add_argument("--actor", required=True)
    candidate_2_register.add_argument("--idempotency-key", required=True)
    for command in (
        "crypto-candidate-2-inspect",
        "crypto-candidate-2-comparison-inspect",
    ):
        candidate_2_read = subparsers.add_parser(command)
        candidate_2_read.add_argument("--candidate-id", required=True)
    candidate_2_protocol_register = subparsers.add_parser("crypto-candidate-2-protocol-register")
    candidate_2_protocol_register.add_argument("--protocol-id", required=True)
    candidate_2_protocol_register.add_argument("--campaign-id", required=True)
    candidate_2_protocol_register.add_argument("--candidate-id", required=True)
    candidate_2_protocol_register.add_argument("--idempotency-key", required=True)
    candidate_2_protocol_inspect = subparsers.add_parser("crypto-candidate-2-protocol-inspect")
    candidate_2_protocol_inspect.add_argument("--protocol-id", required=True)
    candidate_2_window_run = subparsers.add_parser("crypto-candidate-2-window-run")
    candidate_2_window_run.add_argument("--run-id", required=True)
    candidate_2_window_run.add_argument("--consumption-id", required=True)
    candidate_2_window_run.add_argument("--protocol-id", required=True)
    candidate_2_window_run.add_argument("--snapshot-id", required=True)
    candidate_2_window_run.add_argument("--actor", required=True)
    candidate_2_window_run.add_argument("--idempotency-key", required=True)
    for command in (
        "crypto-candidate-2-window-replay",
        "crypto-candidate-2-window-inspect",
        "crypto-candidate-2-decisions-inspect",
        "crypto-candidate-2-metrics-inspect",
    ):
        candidate_2_window_read = subparsers.add_parser(command)
        candidate_2_window_read.add_argument("--run-id", required=True)
    candidate_2_outcome_record = subparsers.add_parser("crypto-candidate-2-outcome-record")
    candidate_2_outcome_record.add_argument("--outcome-id", required=True)
    candidate_2_outcome_record.add_argument("--run-id", required=True)
    candidate_2_outcome_record.add_argument("--actor", required=True)
    candidate_2_outcome_record.add_argument("--idempotency-key", required=True)
    candidate_2_outcome_inspect = subparsers.add_parser("crypto-candidate-2-outcome-inspect")
    candidate_2_outcome_inspect.add_argument("--outcome-id", required=True)
    candidate_2_dossier_materialize = subparsers.add_parser(
        "crypto-candidate-2-dossier-materialize"
    )
    candidate_2_dossier_materialize.add_argument("--dossier-id", required=True)
    candidate_2_dossier_materialize.add_argument("--campaign-id", required=True)
    candidate_2_dossier_materialize.add_argument("--candidate-id", required=True)
    candidate_2_dossier_materialize.add_argument("--protocol-id", required=True)
    candidate_2_dossier_materialize.add_argument("--earlier-outcome-id", required=True)
    candidate_2_dossier_materialize.add_argument("--later-outcome-id", required=True)
    candidate_2_dossier_materialize.add_argument("--idempotency-key", required=True)
    candidate_2_dossier_inspect = subparsers.add_parser("crypto-candidate-2-dossier-inspect")
    candidate_2_dossier_inspect.add_argument("--dossier-id", required=True)
    candidate_2_nomination_apply = subparsers.add_parser("crypto-candidate-2-nomination-apply")
    candidate_2_nomination_apply.add_argument("--evaluation-id", required=True)
    candidate_2_nomination_apply.add_argument("--dossier-id", required=True)
    candidate_2_nomination_apply.add_argument("--nomination-event-id", required=True)
    candidate_2_nomination_apply.add_argument("--development-closed-event-id", required=True)
    candidate_2_nomination_apply.add_argument("--actor", required=True)
    candidate_2_nomination_apply.add_argument("--reason", required=True)
    candidate_2_nomination_apply.add_argument("--idempotency-key", required=True)
    candidate_2_nomination_inspect = subparsers.add_parser("crypto-candidate-2-nomination-inspect")
    candidate_2_nomination_inspect.add_argument("--evaluation-id", required=True)
    subparsers.add_parser("crypto-candidate-2-status")
    subparsers.add_parser("crypto-candidate-2-holdout-non-use")
    challenger_hypothesis_register = subparsers.add_parser("crypto-challenger-hypothesis-register")
    challenger_hypothesis_register.add_argument("--hypothesis-id", required=True)
    challenger_hypothesis_register.add_argument("--actor", required=True)
    challenger_hypothesis_inspect = subparsers.add_parser("crypto-challenger-hypothesis-inspect")
    challenger_hypothesis_inspect.add_argument("--hypothesis-version-id", required=True)
    challenger_strategy_register = subparsers.add_parser("crypto-challenger-strategy-register")
    challenger_strategy_register.add_argument("--specification-id", required=True)
    challenger_strategy_register.add_argument("--hypothesis-version-id", required=True)
    challenger_strategy_register.add_argument("--parent-specification-id", required=True)
    for command in (
        "crypto-challenger-strategy-inspect",
        "crypto-challenger-lineage-inspect",
    ):
        challenger_strategy_read = subparsers.add_parser(command)
        challenger_strategy_read.add_argument("--specification-id", required=True)
    challenger_protocol_register = subparsers.add_parser("crypto-challenger-protocol-register")
    challenger_protocol_register.add_argument("--protocol-id", required=True)
    challenger_protocol_register.add_argument("--hypothesis-version-id", required=True)
    challenger_protocol_register.add_argument("--strategy-specification-id", required=True)
    challenger_protocol_inspect = subparsers.add_parser("crypto-challenger-protocol-inspect")
    challenger_protocol_inspect.add_argument("--protocol-id", required=True)
    challenger_window_run = subparsers.add_parser("crypto-challenger-window-run")
    challenger_window_run.add_argument("--run-id", required=True)
    challenger_window_run.add_argument("--protocol-id", required=True)
    challenger_window_run.add_argument("--snapshot-id", required=True)
    for command in (
        "crypto-challenger-window-replay",
        "crypto-challenger-window-inspect",
        "crypto-challenger-compare-prior",
    ):
        challenger_window_read = subparsers.add_parser(command)
        challenger_window_read.add_argument("--run-id", required=True)
    challenger_outcome_record = subparsers.add_parser("crypto-challenger-outcome-record")
    challenger_outcome_record.add_argument("--outcome-id", required=True)
    challenger_outcome_record.add_argument("--run-id", required=True)
    challenger_outcome_record.add_argument("--actor", required=True)
    challenger_outcome_inspect = subparsers.add_parser("crypto-challenger-outcome-inspect")
    challenger_outcome_inspect.add_argument("--outcome-id", required=True)
    challenger_dossier_materialize = subparsers.add_parser("crypto-challenger-dossier-materialize")
    challenger_dossier_materialize.add_argument("--dossier-id", required=True)
    challenger_dossier_materialize.add_argument("--protocol-id", required=True)
    challenger_dossier_materialize.add_argument("--earlier-outcome-id", required=True)
    challenger_dossier_materialize.add_argument("--later-outcome-id", required=True)
    challenger_dossier_inspect = subparsers.add_parser("crypto-challenger-dossier-inspect")
    challenger_dossier_inspect.add_argument("--dossier-id", required=True)
    challenger_conclusion_record = subparsers.add_parser("crypto-challenger-conclusion-record")
    challenger_conclusion_record.add_argument("--conclusion-id", required=True)
    challenger_conclusion_record.add_argument("--dossier-id", required=True)
    challenger_conclusion_record.add_argument("--actor", required=True)
    challenger_conclusion_inspect = subparsers.add_parser("crypto-challenger-conclusion-inspect")
    challenger_conclusion_inspect.add_argument("--conclusion-id", required=True)
    crypto_products_capture = subparsers.add_parser("crypto-products-capture")
    crypto_products_capture.add_argument("--capture-id", required=True)
    crypto_products_capture.add_argument("--mode", choices=("fixture", "genuine"), required=True)
    crypto_products_inspect = subparsers.add_parser("crypto-products-inspect")
    crypto_products_inspect.add_argument("--capture-id", required=True)
    crypto_pulse_plan_register = subparsers.add_parser("crypto-pulse-plan-register")
    crypto_pulse_plan_register.add_argument("--plan-id", required=True)
    crypto_pulse_plan_register.add_argument("--snapshot-id", required=True)
    crypto_pulse_plan_register.add_argument("--actor", required=True)
    crypto_pulse_plan_register.add_argument(
        "--current-market-evidence",
        action="store_true",
        help="register additive v2 public top-of-book/depth evidence",
    )
    crypto_pulse_plan_inspect = subparsers.add_parser("crypto-pulse-plan-inspect")
    crypto_pulse_plan_inspect.add_argument("--plan-id", required=True)
    for pulse_command in ("crypto-pulse-refresh-run", "crypto-pulse-refresh-replay"):
        crypto_pulse_refresh = subparsers.add_parser(pulse_command)
        crypto_pulse_refresh.add_argument("--refresh-id", required=True)
        crypto_pulse_refresh.add_argument("--plan-id", required=True)
        crypto_pulse_refresh.add_argument("--as-of", required=True)
        crypto_pulse_refresh.add_argument("--mode", choices=("fixture", "genuine"), required=True)
    crypto_pulse_refresh_inspect = subparsers.add_parser("crypto-pulse-refresh-inspect")
    crypto_pulse_refresh_inspect.add_argument("--refresh-id", required=True)
    crypto_pulse_checkpoint_inspect = subparsers.add_parser("crypto-pulse-checkpoint-inspect")
    crypto_pulse_checkpoint_inspect.add_argument("--checkpoint-id", required=True)
    crypto_pulse_snapshot_inspect = subparsers.add_parser("crypto-pulse-snapshot-inspect")
    crypto_pulse_snapshot_inspect.add_argument("--pulse-id", required=True)
    crypto_pulse_lineage_inspect = subparsers.add_parser("crypto-pulse-lineage-inspect")
    crypto_pulse_lineage_inspect.add_argument("--lineage-id", required=True)
    crypto_pulse_summary_inspect = subparsers.add_parser("crypto-pulse-summary-inspect")
    crypto_pulse_summary_inspect.add_argument("--summary-id", required=True)
    for command in ("crypto-relative-strength-run", "crypto-relative-strength-replay"):
        current_strategy = subparsers.add_parser(command)
        current_strategy.add_argument("--run-id", required=True)
        current_strategy.add_argument("--pulse-id", required=True)
    for command in ("crypto-reversal-regime-run", "crypto-reversal-regime-replay"):
        current_strategy = subparsers.add_parser(command)
        current_strategy.add_argument("--run-id", required=True)
        current_strategy.add_argument("--pulse-id", required=True)
    crypto_current_inspect = subparsers.add_parser("crypto-relative-strength-inspect")
    crypto_current_inspect.add_argument("--run-id", required=True)
    subparsers.add_parser("crypto-current-strategy-library")
    subparsers.add_parser("crypto-universe-policy-inspect")
    crypto_calibration_capture = subparsers.add_parser("crypto-calibration-history-capture")
    crypto_calibration_capture.add_argument("--history-id", required=True)
    crypto_calibration_capture.add_argument("--snapshot-id", required=True)
    crypto_calibration_capture.add_argument("--mode", choices=("fixture", "genuine"), required=True)
    crypto_calibration_capture.add_argument("--start", required=True)
    crypto_calibration_capture.add_argument("--end", required=True)
    crypto_observations_capture = subparsers.add_parser("crypto-observations-capture")
    crypto_observations_capture.add_argument("--capture-id", required=True)
    crypto_observations_capture.add_argument(
        "--mode", choices=("fixture", "genuine"), required=True
    )
    crypto_observations_capture.add_argument("--start", required=True)
    crypto_observations_capture.add_argument("--end", required=True)
    crypto_observation_inspect = subparsers.add_parser("crypto-observation-inspect")
    crypto_observation_inspect.add_argument("--observation-id", required=True)
    crypto_universe_materialize = subparsers.add_parser("crypto-universe-materialize")
    crypto_universe_materialize.add_argument("--snapshot-id", required=True)
    crypto_universe_materialize.add_argument("--capture-id", required=True)
    crypto_universe_inspect = subparsers.add_parser("crypto-universe-inspect")
    crypto_universe_inspect.add_argument("--snapshot-id", required=True)
    crypto_discovery_run = subparsers.add_parser("crypto-discovery-run")
    crypto_discovery_run.add_argument("--run-id", required=True)
    crypto_discovery_run.add_argument("--snapshot-id", required=True)
    crypto_discovery_inspect = subparsers.add_parser("crypto-discovery-inspect")
    crypto_discovery_inspect.add_argument("--run-id", required=True)
    crypto_opportunity_inspect = subparsers.add_parser("crypto-opportunity-inspect")
    crypto_opportunity_inspect.add_argument("--opportunity-id", required=True)
    subparsers.add_parser("crypto-approved-champion-status")
    subparsers.add_parser("crypto-research-workspace")
    subparsers.add_parser("broad-equity-reference-capture")
    broad_universe_create = subparsers.add_parser("broad-equity-universe-create")
    broad_universe_create.add_argument("--idempotency-key", required=True)
    broad_universe_status = subparsers.add_parser("broad-equity-universe-status")
    broad_universe_status.add_argument("--universe-id", required=True)
    broad_universe_report = subparsers.add_parser("broad-equity-universe-report")
    broad_universe_report.add_argument("--universe-id", required=True)
    for command in ("broad-equity-data-plan", "broad-equity-data-capture"):
        broad_data = subparsers.add_parser(command)
        broad_data.add_argument("--universe-id", required=True)
        broad_data.add_argument("--start", required=True)
        broad_data.add_argument("--end", required=True)
        if command.endswith("capture"):
            broad_data.add_argument("--idempotency-key", required=True)
    broad_data_status = subparsers.add_parser("broad-equity-data-status")
    broad_data_status.add_argument("--capture-id", required=True)
    broad_data_quality = subparsers.add_parser("broad-equity-data-quality")
    broad_data_quality.add_argument("--capture-id", required=True)
    broad_discovery = subparsers.add_parser("broad-equity-discovery-run")
    broad_discovery.add_argument("--universe-id", required=True)
    broad_discovery.add_argument("--data-capture-id", required=True)
    broad_discovery.add_argument("--idempotency-key", required=True)
    broad_discovery_report = subparsers.add_parser("broad-equity-discovery-report")
    broad_discovery_report.add_argument("--run-id", required=True)
    broad_opportunities = subparsers.add_parser("broad-equity-opportunities")
    broad_opportunities.add_argument("--run-id", required=True)
    broad_opportunities.add_argument("--limit", type=int, required=True)
    broad_shadow_create = subparsers.add_parser("broad-equity-shadow-plan-create")
    broad_shadow_create.add_argument("--universe-id", required=True)
    broad_shadow_create.add_argument("--data-capture-id", required=True)
    broad_shadow_create.add_argument("--run-id", required=True)
    broad_shadow_create.add_argument("--idempotency-key", required=True)
    for command in (
        "broad-equity-shadow-plan-status",
        "broad-equity-shadow-capture",
        "broad-equity-shadow-resolve",
        "broad-equity-shadow-report",
    ):
        broad_shadow = subparsers.add_parser(command)
        broad_shadow.add_argument("--plan-id", required=True)
    subparsers.add_parser("options-intelligence-capability")
    for command in (
        "options-intelligence-reference-capture",
        "options-intelligence-optionable-underlyings",
    ):
        options_universe_command = subparsers.add_parser(command)
        options_universe_command.add_argument("--universe-id", required=True)
    options_contracts = subparsers.add_parser("options-intelligence-contracts")
    options_contracts.add_argument("--canonical-security-id", required=True)
    subparsers.add_parser("options-quote-capability-inspect")
    subparsers.add_parser("options-chain-plan-create")
    oq_plan_inspect = subparsers.add_parser("options-chain-plan-inspect")
    oq_plan_inspect.add_argument("--plan-id", required=False, default=None)
    oq_capture = subparsers.add_parser("options-chain-capture")
    oq_capture.add_argument("--plan-id", required=False, default=None)
    oq_capture.add_argument("--max-underlyings", type=int, required=False, default=None)
    oq_snapshot_inspect = subparsers.add_parser("options-chain-snapshot-inspect")
    oq_snapshot_inspect.add_argument("--snapshot-id", required=True)
    oq_vol_summary = subparsers.add_parser("options-volatility-summary")
    oq_vol_summary.add_argument("--underlying-canonical-security-id", required=True)
    oq_quality_inspect = subparsers.add_parser("options-chain-quality-inspect")
    oq_quality_inspect.add_argument("--snapshot-id", required=True)
    oq_validation_report = subparsers.add_parser("options-quote-validation-report")
    oq_validation_report.add_argument("--plan-id", required=False, default=None)
    ost_policy_create = subparsers.add_parser("options-structure-policy-create")
    ost_policy_create.add_argument("--dte-minimum", type=int, default=None)
    ost_policy_create.add_argument("--dte-maximum", type=int, default=None)
    ost_policy_create.add_argument("--moneyness-band", default=None)
    ost_policy_create.add_argument("--maximum-structures-per-underlying", type=int, default=None)
    ost_policy_create.add_argument("--maximum-total-structures", type=int, default=None)
    ost_policy_create.add_argument(
        "--allow-stale-quotes-for-research",
        action="store_true",
        default=False,
        help=(
            "Explicit, non-default research opt-in (milestone section 24): "
            "lets candidate generation proceed against known-stale quote "
            "evidence -- e.g. to prove structure construction/economics "
            "work when no fresher capture is available -- instead of the "
            "default hard rejection. Every resulting structure is still "
            "independently labeled UNUSABLE with an explicit QUOTE_STALE "
            "reason; never presented as current or executable."
        ),
    )
    ost_policy_inspect = subparsers.add_parser("options-structure-policy-inspect")
    ost_policy_inspect.add_argument("--policy-id", default=None)
    ost_build = subparsers.add_parser("options-structures-build")
    ost_build.add_argument("--underlying-canonical-security-id", default=None)
    ost_build.add_argument("--policy-id", default=None)
    ost_build.add_argument("--snapshot-id", default=None)
    ost_build.add_argument("--quantity", type=int, default=None)
    ost_snapshot_inspect = subparsers.add_parser("options-structures-snapshot-inspect")
    ost_snapshot_inspect.add_argument("--research-snapshot-id", default=None)
    ost_snapshot_inspect.add_argument("--underlying-canonical-security-id", default=None)
    ost_inspect = subparsers.add_parser("options-structure-inspect")
    ost_inspect.add_argument("--structure-id", required=True)
    ost_scenarios = subparsers.add_parser("options-structure-scenarios-inspect")
    ost_scenarios.add_argument("--structure-id", required=True)
    ost_comparison = subparsers.add_parser("options-structure-comparison")
    ost_comparison.add_argument("--structure-id", action="append", required=True)
    subparsers.add_parser("options-research-due-inspect")
    subparsers.add_parser("options-research-run-once")
    occov_inspect = subparsers.add_parser("options-chain-coverage-inspect")
    occov_inspect.add_argument("--plan-id", required=False, default=None)
    occov_inspect.add_argument("--session-id", required=False, default=None)
    ofc_inspect = subparsers.add_parser("options-forward-checkpoint-inspect")
    ofc_inspect.add_argument("--plan-id", required=False, default=None)
    ofc_inspect.add_argument("--session-id", required=False, default=None)
    oh_inspect = subparsers.add_parser("options-history-inspect")
    oh_inspect.add_argument("--underlying-canonical-security-id", required=True)
    orq_inspect = subparsers.add_parser("options-research-quality-inspect")
    orq_inspect.add_argument("--plan-id", required=False, default=None)
    orq_inspect.add_argument("--session-id", required=False, default=None)
    subparsers.add_parser("corporate-evidence-plan-create")
    cep_inspect = subparsers.add_parser("corporate-evidence-plan-inspect")
    cep_inspect.add_argument("--plan-id", required=False, default=None)
    cec_capture = subparsers.add_parser("corporate-evidence-capture")
    cec_capture.add_argument("--plan-id", required=False, default=None)
    cf_filings = subparsers.add_parser("corporate-filings-inspect")
    cf_filings.add_argument("--stable-security-id", required=True)
    cf_facts = subparsers.add_parser("corporate-facts-inspect")
    cf_facts.add_argument("--stable-security-id", required=True)
    cf_facts.add_argument("--concept", required=False, default=None)
    cf_snapshot = subparsers.add_parser("corporate-snapshot-inspect")
    cf_snapshot.add_argument("--stable-security-id", required=True)
    cf_snapshot.add_argument("--cik", required=False, default=None)
    cf_snapshot.add_argument("--as-of", required=False, default=None)
    cf_features = subparsers.add_parser("corporate-features-inspect")
    cf_features.add_argument("--stable-security-id", required=True)
    cf_features.add_argument("--cik", required=False, default=None)
    cf_features.add_argument("--as-of", required=False, default=None)
    cf_quality = subparsers.add_parser("corporate-history-quality-inspect")
    cf_quality.add_argument("--plan-id", required=False, default=None)
    cf_dataset_materialize = subparsers.add_parser("corporate-dataset-materialize")
    cf_dataset_materialize.add_argument("--plan-id", required=False, default=None)
    cf_dataset_inspect = subparsers.add_parser("corporate-dataset-inspect")
    cf_dataset_inspect.add_argument("--dataset-id", required=False, default=None)
    cf_dataset_inspect.add_argument("--plan-id", required=False, default=None)
    cf_replay_attach = subparsers.add_parser("corporate-replay-attach")
    cf_replay_attach.add_argument("--replay-run-id", required=True)
    cf_replay_attach.add_argument("--frame-limit", required=False, default=None, type=int)
    cf_replay_feature = subparsers.add_parser("corporate-replay-feature-inspect")
    cf_replay_feature.add_argument("--replay-run-id", required=True)
    cf_replay_feature.add_argument("--frame-id", required=True)
    cf_replay_feature.add_argument("--stable-security-id", required=True)

    cmr3_agenda_create = subparsers.add_parser("corporate-market-agenda-create")
    cmr3_agenda_create.add_argument("--corporate-dataset-id", required=False, default=None)
    cmr3_agenda_create.add_argument("--plan-id", required=False, default=None)
    cmr3_agenda_create.add_argument("--equity-snapshot-id", required=True)
    cmr3_agenda_create.add_argument("--replay-run-id", required=True)
    cmr3_agenda_create.add_argument("--cross-market-dataset-id", required=True)
    cmr3_agenda_create.add_argument("--cohort-policy-id", required=False, default=None)
    cmr3_agenda_create.add_argument("--created-at", required=True)
    cmr3_agenda_inspect = subparsers.add_parser("corporate-market-agenda-inspect")
    cmr3_agenda_inspect.add_argument("--agenda-id", required=False, default=None)
    cmr3_hypotheses_list = subparsers.add_parser("corporate-market-hypotheses-list")
    cmr3_hypotheses_list.add_argument("--agenda-id", required=False, default=None)
    cmr3_run_commands = ("corporate-market-agenda-run-start", "corporate-market-agenda-run-resume")
    for cmr3_run_command in cmr3_run_commands:
        cmr3_run_step = subparsers.add_parser(cmr3_run_command)
        cmr3_run_step.add_argument("--agenda-id", required=False, default=None)
        cmr3_run_step.add_argument("--steps", required=False, default=None, type=int)
        cmr3_run_step.add_argument("--created-at", required=True)
    cmr3_run_inspect = subparsers.add_parser("corporate-market-agenda-run-inspect")
    cmr3_run_inspect.add_argument("--agenda-id", required=False, default=None)
    cmr3_knowledge_inspect = subparsers.add_parser("corporate-market-knowledge-inspect")
    cmr3_knowledge_inspect.add_argument("--hypothesis-id", required=False, default=None)
    cmr3_quality_inspect = subparsers.add_parser("corporate-market-quality-inspect")
    cmr3_quality_inspect.add_argument("--agenda-id", required=False, default=None)
    cmr3_panel_diagnostics = subparsers.add_parser("corporate-market-panel-diagnostics-inspect")
    cmr3_panel_diagnostics.add_argument("--agenda-id", required=False, default=None)
    subparsers.add_parser("corporate-market-followups-list")

    ccd_create = subparsers.add_parser("corporate-calibration-create")
    ccd_create.add_argument("--agenda-id", required=False, default=None)
    ccd_create.add_argument("--created-at", required=True)
    ccd_inspect = subparsers.add_parser("corporate-calibration-inspect")
    ccd_inspect.add_argument("--calibration-id", required=False, default=None)
    ccd_strata_inspect = subparsers.add_parser("corporate-calibration-strata-inspect")
    ccd_strata_inspect.add_argument("--calibration-id", required=False, default=None)
    ccd_run_commands = ("corporate-calibration-run-start", "corporate-calibration-run-resume")
    for ccd_run_command in ccd_run_commands:
        ccd_run_step = subparsers.add_parser(ccd_run_command)
        ccd_run_step.add_argument("--calibration-id", required=False, default=None)
        ccd_run_step.add_argument("--steps", required=False, default=None, type=int)
        ccd_run_step.add_argument("--created-at", required=True)
    ccd_run_inspect = subparsers.add_parser("corporate-calibration-run-inspect")
    ccd_run_inspect.add_argument("--calibration-id", required=False, default=None)
    ccd_recalibrate = subparsers.add_parser("corporate-market-recalibrate")
    ccd_recalibrate.add_argument("--calibration-id", required=False, default=None)
    ccd_report_inspect = subparsers.add_parser("corporate-calibration-report-inspect")
    ccd_report_inspect.add_argument("--calibration-id", required=False, default=None)
    ccd_followup_requalify = subparsers.add_parser("corporate-followup-requalify")
    ccd_followup_requalify.add_argument("--calibration-id", required=False, default=None)

    crf_spec_create = subparsers.add_parser("corporate-followup-spec-create")
    crf_spec_create.add_argument("--follow-up-id", required=False, default=None)
    crf_spec_create.add_argument("--created-at", required=True)
    crf_spec_inspect = subparsers.add_parser("corporate-followup-spec-inspect")
    crf_spec_inspect.add_argument("--follow-up-id", required=False, default=None)
    crf_spec_inspect.add_argument("--specification-id", required=False, default=None)
    crf_run_commands = ("corporate-followup-run-start", "corporate-followup-run-resume")
    for crf_run_command in crf_run_commands:
        crf_run_step = subparsers.add_parser(crf_run_command)
        crf_run_step.add_argument("--specification-id", required=False, default=None)
        crf_run_step.add_argument("--follow-up-id", required=False, default=None)
        crf_run_step.add_argument("--created-at", required=True)
    crf_inspect_commands = (
        "corporate-followup-run-inspect",
        "corporate-followup-stability-inspect",
        "corporate-followup-calibration-inspect",
        "corporate-followup-conclusion-inspect",
    )
    for crf_inspect_command in crf_inspect_commands:
        crf_inspect = subparsers.add_parser(crf_inspect_command)
        crf_inspect.add_argument("--specification-id", required=False, default=None)
        crf_inspect.add_argument("--follow-up-id", required=False, default=None)

    identity_build = subparsers.add_parser("security-identity-projection-build")
    identity_build.add_argument("--idempotency-key", required=True)
    subparsers.add_parser("security-identity-report")
    identity_ambiguous = subparsers.add_parser("security-identity-ambiguous")
    identity_ambiguous.add_argument("--limit", type=int, default=50)
    identity_resolve = subparsers.add_parser("security-identity-resolve")
    identity_resolve.add_argument("--id", required=True)
    identity_relink = subparsers.add_parser("security-identity-options-relink")
    identity_relink.add_argument("--linkage-run-id", required=True)
    identity_linkage_report = subparsers.add_parser("security-identity-options-linkage-report")
    identity_linkage_report.add_argument("--linkage-run-id", default=None)
    lab_dataset = subparsers.add_parser("strategy-lab-build-dataset")
    lab_dataset.add_argument("--adapter", required=True, choices=("event-market-v1",))
    lab_dataset.add_argument("--name", required=True)
    lab_dataset.add_argument("--cutoff", required=True)
    lab_dataset.add_argument("--idempotency-key", required=True)
    lab_dataset.add_argument("--strategy-id")
    lab_dataset.add_argument("--strategy-version")
    lab_dataset.add_argument("--configuration-hash")
    lab_train = subparsers.add_parser("strategy-lab-train")
    lab_train.add_argument("--dataset-id", required=True)
    lab_train.add_argument(
        "--model-family",
        required=True,
        choices=(
            "logistic-regression-probability-v1",
            "histogram-gradient-boosting-probability-v1",
        ),
    )
    lab_train.add_argument("--calibration", required=True, choices=("none", "platt", "isotonic"))
    lab_train.add_argument("--seed", type=int, required=True)
    lab_train.add_argument("--idempotency-key", required=True)
    lab_train.add_argument("--hyperparameters-json", default="{}")
    lab_evaluate = subparsers.add_parser("strategy-lab-evaluate")
    lab_evaluate.add_argument("--training-run-id", required=True)
    lab_report = subparsers.add_parser("strategy-lab-report")
    lab_report.add_argument("--candidate-id", required=True)
    lab_shadow_request = subparsers.add_parser("strategy-lab-request-shadow-review")
    lab_shadow_request.add_argument("--candidate-id", required=True)
    lab_shadow_request.add_argument("--reason", required=True)
    lab_shadow_request.add_argument("--idempotency-key", required=True)
    lab_shadow_approve = subparsers.add_parser("strategy-lab-approve-shadow-deployment")
    lab_shadow_approve.add_argument("--review-id", required=True)
    lab_shadow_approve.add_argument("--role", choices=("CHALLENGER", "CHAMPION"), required=True)
    lab_shadow_approve.add_argument("--confirm-candidate", required=True)
    lab_shadow_approve.add_argument("--confirm-dataset", required=True)
    lab_shadow_approve.add_argument("--confirm-artifact-sha256", required=True)
    lab_shadow_approve.add_argument("--confirm-evaluation", required=True)
    lab_shadow_approve.add_argument("--reason", required=True)
    lab_shadow_approve.add_argument("--idempotency-key", required=True)
    lab_shadow_run = subparsers.add_parser("strategy-lab-run-shadow")
    lab_shadow_run.add_argument("--deployment-id", required=True)
    lab_shadow_run.add_argument("--adapter", required=True, choices=("event-market-retained-v1",))
    lab_shadow_run.add_argument("--cutoff", required=True)
    lab_shadow_run.add_argument("--maximum-rows", type=int, required=True)
    lab_shadow_run.add_argument("--idempotency-key", required=True)
    lab_shadow_resolve = subparsers.add_parser("strategy-lab-resolve-shadow")
    lab_shadow_resolve.add_argument("--deployment-id", required=True)
    lab_shadow_resolve.add_argument("--cutoff", required=True)
    lab_shadow_evaluate = subparsers.add_parser("strategy-lab-evaluate-challenger")
    lab_shadow_evaluate.add_argument("--deployment-id", required=True)
    lab_shadow_evaluate.add_argument("--cutoff", required=True)
    lab_shadow_champion = subparsers.add_parser("strategy-lab-assign-champion")
    lab_shadow_champion.add_argument("--deployment-id", required=True)
    lab_shadow_champion.add_argument("--reason", required=True)
    lab_shadow_champion.add_argument("--confirm-deployment", required=True)
    lab_shadow_champion.add_argument("--idempotency-key", required=True)
    securities_import = subparsers.add_parser("securities-import-data")
    securities_import.add_argument("--managed-source", required=True)
    securities_import.add_argument("--dataset-name", required=True)
    securities_import.add_argument("--idempotency-key", required=True)
    provider_status = subparsers.add_parser("securities-provider-status")
    provider_status.add_argument("--provider", required=True, choices=("ALPACA_MARKET_DATA_V2",))
    provider_universe = subparsers.add_parser("securities-universe-create")
    provider_universe.add_argument("--name", required=True)
    provider_universe.add_argument("--symbols", required=True)
    provider_universe.add_argument(
        "--policy", required=True, choices=("PREDECLARED_FIXED_UNIVERSE",)
    )
    provider_universe.add_argument("--reason", required=True)
    provider_universe.add_argument("--idempotency-key", required=True)
    provider_assets = subparsers.add_parser("securities-provider-assets")
    provider_assets.add_argument("--provider", required=True, choices=("ALPACA_MARKET_DATA_V2",))
    provider_assets.add_argument("--idempotency-key", required=True)
    provider_backfill = subparsers.add_parser("securities-provider-backfill")
    provider_backfill.add_argument("--provider", required=True, choices=("ALPACA_MARKET_DATA_V2",))
    provider_backfill.add_argument("--universe-id", required=True)
    provider_backfill.add_argument("--feed", required=True, choices=("IEX",))
    provider_backfill.add_argument("--start", required=True)
    provider_backfill.add_argument("--end", required=True)
    provider_backfill.add_argument("--idempotency-key", required=True)
    provider_actions = subparsers.add_parser("securities-provider-actions")
    provider_actions.add_argument("--provider", required=True, choices=("ALPACA_MARKET_DATA_V2",))
    provider_actions.add_argument("--universe-id", required=True)
    provider_actions.add_argument("--start", required=True)
    provider_actions.add_argument("--end", required=True)
    provider_actions.add_argument("--idempotency-key", required=True)
    real_quality = subparsers.add_parser("securities-data-quality")
    real_quality.add_argument("--data-batch-id", required=True)
    canonicalize_alpaca = subparsers.add_parser("securities-canonicalize-retained-alpaca")
    canonicalize_alpaca.add_argument("--historical-operation-id", required=True)
    canonicalize_alpaca.add_argument("--identity-operation-id")
    canonicalize_alpaca.add_argument("--action-operation-id")
    canonicalize_alpaca.add_argument("--idempotency-key", required=True)
    real_validation = subparsers.add_parser("securities-real-historical-validation")
    real_validation.add_argument("--data-batch-id", required=True)
    real_validation.add_argument("--universe-id", required=True)
    real_validation.add_argument("--start", required=True)
    real_validation.add_argument("--end", required=True)
    real_validation.add_argument("--idempotency-key", required=True)
    real_validation_report = subparsers.add_parser("securities-real-validation-report")
    real_validation_report.add_argument("--validation-run-id", required=True)
    historical_plan_create = subparsers.add_parser("historical-data-plan-create")
    historical_plan_create.add_argument("--symbols", required=True)
    historical_plan_create.add_argument("--etf-symbols", default="")
    historical_plan_create.add_argument("--start", required=True)
    historical_plan_create.add_argument("--end", required=True)
    historical_plan_create.add_argument("--chunk-size", type=int, required=True)
    historical_plan_create.add_argument("--maximum-instruments", type=int, required=True)
    historical_plan_create.add_argument("--maximum-requests", type=int, required=True)
    historical_plan_create.add_argument("--maximum-bytes", type=int, required=True)
    historical_plan_create.add_argument(
        "--evidence-class",
        required=True,
        choices=("FIXTURE", "GENUINE_PUBLIC", "HISTORICAL_PUBLIC"),
    )
    historical_plan_create.add_argument("--idempotency-key", required=True)
    historical_capture_run = subparsers.add_parser("historical-data-capture-run")
    historical_capture_run.add_argument("--plan-id", required=True)
    historical_capture_run.add_argument("--ordinal", type=int, default=None)
    historical_snapshot_materialize = subparsers.add_parser("historical-data-snapshot-materialize")
    historical_snapshot_materialize.add_argument("--plan-id", required=True)
    historical_quality_report = subparsers.add_parser("historical-data-quality-report")
    historical_quality_report.add_argument("--snapshot-id", required=True)
    historical_calculate = subparsers.add_parser("historical-data-calculate")
    historical_calculate.add_argument("--snapshot-id", required=True)
    historical_calculate.add_argument("--stable-security-id", required=True)
    historical_calculate.add_argument("--as-of", required=True)
    historical_calculate.add_argument(
        "--calculation",
        required=True,
        choices=(
            "simple_return",
            "log_return",
            "max_drawdown",
            "rolling_volatility",
            "rolling_liquidity",
        ),
    )
    historical_calculate.add_argument("--window", type=int, default=None)
    subparsers.add_parser("historical-data-workspace")
    genuine_registry_parser = subparsers.add_parser("genuine-multi-asset-registry-inspect")
    genuine_registry_parser.add_argument("--hypothesis-version-id", default=None)
    genuine_registry_parser.add_argument("--hypothesis-id", default=None)
    replay_plan_create_parser = subparsers.add_parser("replay-plan-create")
    replay_plan_create_parser.add_argument("--dataset-snapshot-id", required=True)
    replay_plan_create_parser.add_argument("--start", required=True)
    replay_plan_create_parser.add_argument("--end", required=True)
    replay_plan_create_parser.add_argument("--calendar-identity", default=None)
    replay_plan_create_parser.add_argument("--hypothesis-version-id", default=None)
    replay_plan_create_parser.add_argument("--idempotency-key", required=True)
    for command_name in ("replay-run-start", "replay-run-resume"):
        replay_step_parser = subparsers.add_parser(command_name)
        replay_step_parser.add_argument("--replay-run-id", required=True)
        replay_step_parser.add_argument("--maximum-steps", type=int, required=True)
        replay_step_parser.add_argument("--benchmark-stable-security-id", default=None)
        replay_step_parser.add_argument("--crypto-capture-id", default=None)
    replay_run_inspect_parser = subparsers.add_parser("replay-run-inspect")
    replay_run_inspect_parser.add_argument("--replay-run-id", required=True)
    replay_frame_inspect_parser = subparsers.add_parser("replay-frame-inspect")
    replay_frame_inspect_parser.add_argument("--frame-id", default=None)
    replay_frame_inspect_parser.add_argument("--replay-run-id", default=None)
    replay_frame_inspect_parser.add_argument("--session-date", default=None)
    replay_evaluation_list_parser = subparsers.add_parser("replay-evaluation-list")
    replay_evaluation_list_parser.add_argument("--replay-run-id", required=True)
    replay_evaluation_list_parser.add_argument("--limit", type=int, default=100)
    historical_research_summary_parser = subparsers.add_parser("historical-research-summary")
    historical_research_summary_parser.add_argument("--replay-run-id", required=True)
    replay_experiment_construct_parser = subparsers.add_parser("replay-experiment-construct")
    replay_experiment_construct_parser.add_argument("--replay-run-id", required=True)
    replay_experiment_construct_parser.add_argument("--code-revision", required=True)
    replay_experiment_construct_parser.add_argument("--actor", required=True)
    research_policy_create_parser = subparsers.add_parser("research-policy-create")
    research_policy_create_parser.add_argument("--hypothesis-version-id", required=True)
    research_policy_create_parser.add_argument("--dataset-snapshot-id", required=True)
    research_policy_create_parser.add_argument("--replay-run-id", required=True)
    research_policy_create_parser.add_argument(
        "--intent", required=True, choices=("EXPLORATORY_ONLY", "PROMOTION_TRACK")
    )
    research_policy_create_parser.add_argument("--train-start", default=None)
    research_policy_create_parser.add_argument("--train-end", default=None)
    research_policy_create_parser.add_argument("--validation-start", default=None)
    research_policy_create_parser.add_argument("--validation-end", default=None)
    research_policy_create_parser.add_argument("--holdout-start", default=None)
    research_policy_create_parser.add_argument("--holdout-end", default=None)
    research_policy_create_parser.add_argument("--holdout-locked", action="store_true")
    research_policy_create_parser.add_argument("--minimum-sample-count", type=int, default=30)
    research_policy_create_parser.add_argument("--revision", type=int, default=1)
    research_policy_create_parser.add_argument("--supersedes-governance-policy-id", default=None)
    research_policy_inspect_parser = subparsers.add_parser("research-policy-inspect")
    research_policy_inspect_parser.add_argument("--governance-policy-id", required=True)
    research_holdout_expose_parser = subparsers.add_parser("research-holdout-expose")
    research_holdout_expose_parser.add_argument("--governance-policy-id", required=True)
    research_holdout_expose_parser.add_argument("--actor", required=True)
    research_holdout_expose_parser.add_argument("--purpose", required=True)
    research_holdout_expose_parser.add_argument("--consumed-trial-id", default=None)
    research_trial_create_parser = subparsers.add_parser("research-trial-create")
    research_trial_create_parser.add_argument("--hypothesis-version-id", required=True)
    research_trial_create_parser.add_argument("--dataset-snapshot-id", required=True)
    research_trial_create_parser.add_argument("--replay-run-id", required=True)
    research_trial_create_parser.add_argument(
        "--phase",
        required=True,
        choices=("EXPLORATORY", "VALIDATION", "LOCKED_HOLDOUT", "WALK_FORWARD", "FORWARD_PAPER"),
    )
    research_trial_create_parser.add_argument("--market-scope", required=True)
    research_trial_create_parser.add_argument(
        "--method-id",
        required=True,
        choices=(
            "BREADTH_VS_SUBSEQUENT_RETURN_V1",
            "VOLATILITY_REGIME_VS_SUBSEQUENT_DISPERSION_V1",
            "CRYPTO_EQUITY_CONTEXT_COMPARISON_V1",
        ),
    )
    research_trial_create_parser.add_argument("--method-version", default="1")
    research_trial_create_parser.add_argument("--parameter-name", default="lookback_sessions")
    research_trial_create_parser.add_argument("--parameter-value", default="20")
    research_trial_create_parser.add_argument(
        "--configuration-origin",
        required=True,
        choices=(
            "FIXED_A_PRIORI",
            "EXPLORATORY",
            "SELECTED_USING_TRAINING",
            "SELECTED_USING_VALIDATION",
        ),
    )
    research_trial_create_parser.add_argument("--candidates-examined-count", type=int, default=1)
    research_trial_create_parser.add_argument("--session-date-start", required=True)
    research_trial_create_parser.add_argument("--session-date-end", required=True)
    research_trial_create_parser.add_argument(
        "--benchmark-kind",
        default=None,
        choices=(
            "UNCONDITIONAL_SUBSEQUENT_RETURN",
            "MARKET_BENCHMARK",
            "EQUAL_WEIGHT_COHORT",
            "RANDOM_DATE_BASELINE",
            "PRIOR_REGIME_DISTRIBUTION",
        ),
    )
    research_trial_create_parser.add_argument("--governance-policy-id", default=None)
    research_trial_create_parser.add_argument("--outcome-horizon-sessions", type=int, default=30)
    research_trial_create_parser.add_argument("--bootstrap-seed", type=int, default=None)
    research_trial_create_parser.add_argument("--hac-lag", type=int, default=None)
    research_trial_inspect_parser = subparsers.add_parser("research-trial-inspect")
    research_trial_inspect_parser.add_argument("--trial-id", required=True)
    research_trial_family_create_parser = subparsers.add_parser("research-trial-family-create")
    research_trial_family_create_parser.add_argument("--hypothesis-id", required=True)
    research_trial_family_create_parser.add_argument("--trial-ids", required=True)
    research_trial_family_create_parser.add_argument("--candidate-count", type=int, required=True)
    research_trial_family_create_parser.add_argument("--asset-count", type=int, default=1)
    research_trial_family_create_parser.add_argument(
        "--outcome-definition-count", type=int, default=1
    )
    research_trial_family_create_parser.add_argument("--regime-slice-count", type=int, default=1)
    research_trial_family_create_parser.add_argument(
        "--selection-method", required=True, choices=("PREDECLARED_SINGLE", "BEST_OF_FAMILY")
    )
    research_trial_family_create_parser.add_argument(
        "--correction-method",
        default="NONE",
        choices=("NONE", "BONFERRONI", "BENJAMINI_HOCHBERG_FDR"),
    )
    research_trial_family_inspect_parser = subparsers.add_parser("research-trial-family-inspect")
    research_trial_family_inspect_parser.add_argument("--trial-family-id", required=True)
    walk_forward_create_parser = subparsers.add_parser("walk-forward-create")
    walk_forward_create_parser.add_argument("--replay-run-id", required=True)
    walk_forward_create_parser.add_argument(
        "--method-id",
        required=True,
        choices=(
            "BREADTH_VS_SUBSEQUENT_RETURN_V1",
            "VOLATILITY_REGIME_VS_SUBSEQUENT_DISPERSION_V1",
            "CRYPTO_EQUITY_CONTEXT_COMPARISON_V1",
        ),
    )
    walk_forward_create_parser.add_argument(
        "--outcome-metric-id", default="SUBSEQUENT_BENCHMARK_RETURN_20_SESSION_V1"
    )
    walk_forward_create_parser.add_argument("--training-window-sessions", type=int, required=True)
    walk_forward_create_parser.add_argument("--validation-window-sessions", type=int, default=0)
    walk_forward_create_parser.add_argument("--test-window-sessions", type=int, required=True)
    walk_forward_create_parser.add_argument("--step-sessions", type=int, required=True)
    walk_forward_create_parser.add_argument(
        "--window-mode", required=True, choices=("EXPANDING", "ROLLING")
    )
    walk_forward_create_parser.add_argument("--minimum-samples", type=int, default=10)
    walk_forward_create_parser.add_argument(
        "--cost-information-state",
        default="COST_DATA_UNAVAILABLE_NO_TRANSACTION_COST_MODEL_IN_DESCRIPTIVE_RESEARCH_METHODS",
    )
    walk_forward_create_parser.add_argument("--start-date", default=None)
    walk_forward_create_parser.add_argument("--end-date", default=None)
    walk_forward_create_parser.add_argument("--hypothesis-version-id", default=None)
    walk_forward_create_parser.add_argument("--idempotency-key", required=True)
    for command_name in ("walk-forward-run-start", "walk-forward-run-resume"):
        walk_forward_step_parser = subparsers.add_parser(command_name)
        walk_forward_step_parser.add_argument("--walk-forward-run-id", required=True)
        walk_forward_step_parser.add_argument("--maximum-folds", type=int, required=True)
        walk_forward_step_parser.add_argument("--market-scope", required=True)
        walk_forward_step_parser.add_argument(
            "--method-id",
            required=True,
            choices=(
                "BREADTH_VS_SUBSEQUENT_RETURN_V1",
                "VOLATILITY_REGIME_VS_SUBSEQUENT_DISPERSION_V1",
                "CRYPTO_EQUITY_CONTEXT_COMPARISON_V1",
            ),
        )
        walk_forward_step_parser.add_argument("--method-version", default="1")
        walk_forward_step_parser.add_argument("--parameter-name", default="lookback_sessions")
        walk_forward_step_parser.add_argument("--parameter-value", default="20")
        walk_forward_step_parser.add_argument("--configuration-origin", default="FIXED_A_PRIORI")
        walk_forward_step_parser.add_argument("--candidates-examined-count", type=int, default=1)
        walk_forward_step_parser.add_argument(
            "--benchmark-kind",
            default=None,
            choices=(
                "UNCONDITIONAL_SUBSEQUENT_RETURN",
                "MARKET_BENCHMARK",
                "EQUAL_WEIGHT_COHORT",
                "RANDOM_DATE_BASELINE",
                "PRIOR_REGIME_DISTRIBUTION",
            ),
        )
    walk_forward_inspect_parser = subparsers.add_parser("walk-forward-inspect")
    walk_forward_inspect_parser.add_argument("--walk-forward-run-id", required=True)
    robustness_run_create_parser = subparsers.add_parser("robustness-run-create")
    robustness_run_create_parser.add_argument("--base-trial-id", required=True)
    robustness_run_create_parser.add_argument("--momentum-lookback-windows", default="5,20,60")
    robustness_run_create_parser.add_argument("--momentum-positive-threshold", default="0")
    robustness_run_inspect_parser = subparsers.add_parser("robustness-run-inspect")
    robustness_run_inspect_parser.add_argument("--robustness-run-id", required=True)
    research_conclusion_create_parser = subparsers.add_parser("research-conclusion-create")
    research_conclusion_create_parser.add_argument("--hypothesis-version-id", required=True)
    research_conclusion_create_parser.add_argument("--dataset-snapshot-id", required=True)
    research_conclusion_create_parser.add_argument("--replay-run-id", required=True)
    research_conclusion_create_parser.add_argument("--training-trial-id", default=None)
    research_conclusion_create_parser.add_argument("--validation-trial-id", default=None)
    research_conclusion_create_parser.add_argument("--holdout-trial-id", default=None)
    research_conclusion_create_parser.add_argument("--walk-forward-run-id", default=None)
    research_conclusion_create_parser.add_argument("--robustness-run-id", default=None)
    research_conclusion_create_parser.add_argument("--trial-family-id", default=None)
    research_conclusion_create_parser.add_argument("--point-in-time-limitations", default="")
    research_conclusion_create_parser.add_argument("--rationale", required=True)
    research_conclusion_create_parser.add_argument(
        "--sample-size-phase", default="VALIDATION", choices=("EXPLORATORY", "VALIDATION")
    )
    research_conclusion_inspect_parser = subparsers.add_parser("research-conclusion-inspect")
    research_conclusion_inspect_parser.add_argument("--conclusion-id", required=True)
    research_conclusion_list_parser = subparsers.add_parser("research-conclusion-list")
    research_conclusion_list_parser.add_argument("--hypothesis-version-id", required=True)
    research_audit_inspect_parser = subparsers.add_parser("research-audit-inspect")
    research_audit_inspect_parser.add_argument("--hypothesis-version-id", required=True)
    promotion_evaluate_parser = subparsers.add_parser("promotion-evaluate")
    promotion_evaluate_parser.add_argument("--conclusion-id", required=True)
    promotion_evaluate_parser.add_argument("--require-holdout-consumed", action="store_true")
    promotion_evaluate_parser.add_argument("--lookahead-violation", action="store_true")
    promotion_evaluate_production_parser = subparsers.add_parser("promotion-evaluate-production")
    promotion_evaluate_production_parser.add_argument("--conclusion-id", required=True)
    promotion_evaluate_production_parser.add_argument("--lookahead-violation", action="store_true")
    genuine_dataset = subparsers.add_parser("securities-genuine-strategy-dataset-build")
    genuine_dataset.add_argument("--raw-batch-id", required=True)
    genuine_dataset.add_argument("--derived-batch-id", required=True)
    genuine_dataset.add_argument("--universe-id", required=True)
    genuine_dataset.add_argument("--cutoff", required=True)
    genuine_dataset.add_argument("--idempotency-key", required=True)
    genuine_candidate = subparsers.add_parser("securities-genuine-candidate-evaluate")
    genuine_candidate.add_argument("--dataset-id", required=True)
    genuine_candidate.add_argument("--validation-run-id", required=True)
    genuine_candidate.add_argument("--idempotency-key", required=True)
    forward_plan = subparsers.add_parser("securities-forward-plan-create")
    forward_plan.add_argument("--provider", required=True, choices=("ALPACA_MARKET_DATA_V2",))
    forward_plan.add_argument("--universe-id", required=True)
    forward_plan.add_argument("--feature-schema-id", required=True)
    forward_plan.add_argument("--prediction-horizon-sessions", required=True, type=int)
    forward_plan.add_argument("--reason", required=True)
    forward_plan.add_argument("--idempotency-key", required=True)
    forward_capture = subparsers.add_parser("securities-forward-capture")
    forward_capture.add_argument("--plan-id", required=True)
    shadow_plan_create = subparsers.add_parser("securities-shadow-plan-create")
    shadow_plan_create.add_argument("--idempotency-key", required=True)
    shadow_plan_v2_create = subparsers.add_parser("securities-shadow-plan-v2-create")
    shadow_plan_v2_create.add_argument("--parent-plan-id", required=True)
    shadow_plan_v2_create.add_argument("--idempotency-key", required=True)
    shadow_plan_activate = subparsers.add_parser("securities-shadow-plan-activate")
    shadow_plan_activate.add_argument("--plan-id", required=True)
    shadow_plan_activate.add_argument("--confirm-plan-id", required=True)
    shadow_plan_pause = subparsers.add_parser("securities-shadow-plan-pause")
    shadow_plan_pause.add_argument("--plan-id", required=True)
    shadow_plan_pause.add_argument("--reason", required=True)
    shadow_plan_resume = subparsers.add_parser("securities-shadow-plan-resume")
    shadow_plan_resume.add_argument("--plan-id", required=True)
    shadow_plan_resume.add_argument("--confirm-plan-id", required=True)
    shadow_plan_revoke = subparsers.add_parser("securities-shadow-plan-revoke")
    shadow_plan_revoke.add_argument("--plan-id", required=True)
    shadow_plan_revoke.add_argument("--confirm-plan-id", required=True)
    sustained_worker = subparsers.add_parser("securities-shadow-worker")
    sustained_worker.add_argument("--once", action="store_true", required=True)
    sustained_worker.add_argument("--plan-id")
    sustained_daily = subparsers.add_parser("securities-shadow-daily")
    sustained_daily.add_argument("--plan-id", required=True)
    sustained_daily.add_argument("--no-provider", action="store_true")
    sustained_daily.add_argument("--keep-db-running", action="store_true")
    sustained_daily.add_argument("--json", action="store_true")
    scheduler_start = subparsers.add_parser("securities-shadow-scheduler-start")
    scheduler_start.add_argument("--plan-id", required=True)
    scheduler_loop = subparsers.add_parser("securities-shadow-scheduler-loop")
    scheduler_loop.add_argument("--plan-id", required=True)
    scheduler_run_due = subparsers.add_parser("securities-shadow-scheduler-run-due")
    scheduler_run_due.add_argument("--plan-id", required=True)
    subparsers.add_parser("securities-shadow-scheduler-stop")
    subparsers.add_parser("securities-shadow-scheduler-status")
    runtime_up = subparsers.add_parser("apex-runtime-up")
    runtime_up.add_argument("--plan-id", required=True)
    subparsers.add_parser("apex-runtime-down")
    subparsers.add_parser("apex-runtime-status")
    portfolio_current = subparsers.add_parser("portfolio-intelligence-current")
    portfolio_current.add_argument("--plan-id", required=True)
    portfolio_current.add_argument("--idempotency-key", required=True)
    portfolio_snapshot_build = subparsers.add_parser("portfolio-snapshot-build")
    portfolio_snapshot_build.add_argument("--as-of", required=False, default=None)
    portfolio_snapshot_build.add_argument("--snapshot-id", required=False, default=None)
    portfolio_snapshot_build.add_argument("--idempotency-key", required=False, default=None)
    portfolio_snapshot_build.add_argument(
        "--crypto-account-id", action="append", default=[], dest="crypto_account_id"
    )
    portfolio_snapshot_build.add_argument(
        "--kalshi-account-id", action="append", default=[], dest="kalshi_account_id"
    )
    portfolio_snapshot_build.add_argument(
        "--securities-campaign-id", action="append", default=[], dest="securities_campaign_id"
    )
    portfolio_snapshot_build.add_argument(
        "--external-paper-account-id", required=False, default=None
    )
    portfolio_snapshot_build.add_argument(
        "--external-paper-account-evidence-id", required=False, default=None
    )
    portfolio_snapshot_build.add_argument(
        "--external-paper-position-evidence-id",
        action="append",
        default=[],
        dest="external_paper_position_evidence_id",
    )
    portfolio_snapshot_inspect = subparsers.add_parser("portfolio-snapshot-inspect")
    portfolio_snapshot_inspect.add_argument("--snapshot-id", required=False, default=None)
    mandate_create = subparsers.add_parser("investment-mandate-create")
    mandate_create.add_argument("--lineage-id", required=False, default=None)
    mandate_create.add_argument("--effective-at", required=True)
    mandate_create.add_argument("--created-at", required=False, default=None)
    mandate_create.add_argument("--idempotency-key", required=False, default=None)
    mandate_create.add_argument("--total-capital-allocated", required=False, default="0")
    mandate_create.add_argument(
        "--permitted-venue", action="append", default=[], dest="permitted_venue"
    )
    mandate_create.add_argument(
        "--permitted-market-family", action="append", default=[], dest="permitted_market_family"
    )
    mandate_create.add_argument(
        "--disallowed-market-family", action="append", default=[], dest="disallowed_market_family"
    )
    mandate_create.add_argument(
        "--maximum-aggregate-exposure-fraction", required=False, default="0"
    )
    mandate_create.add_argument("--maximum-per-instrument-fraction", required=False, default="0")
    mandate_create.add_argument(
        "--maximum-venue-concentration-fraction", required=False, default="0"
    )
    mandate_create.add_argument(
        "--maximum-market-family-concentration-fraction", required=False, default="0"
    )
    mandate_create.add_argument("--leverage-permitted", action="store_true")
    mandate_create.add_argument("--short-exposure-permitted", action="store_true")
    mandate_create.add_argument("--options-permitted", action="store_true")
    mandate_create.add_argument("--options-undefined-risk-permitted", action="store_true")
    mandate_create.add_argument("--minimum-liquidity-state", required=False, default="SUFFICIENT")
    mandate_create.add_argument(
        "--maximum-capital-lockup-days", required=False, default=None, type=int
    )
    mandate_create.add_argument(
        "--autonomy-requirement",
        required=False,
        default="OPERATOR_APPROVAL_REQUIRED",
        choices=["AUTONOMOUS_WITHIN_LIMITS", "OPERATOR_APPROVAL_REQUIRED"],
    )
    mandate_create.add_argument("--notes", required=False, default="")
    mandate_inspect = subparsers.add_parser("investment-mandate-inspect")
    mandate_inspect.add_argument("--lineage-id", required=False, default=None)
    mandate_inspect.add_argument("--as-of", required=False, default=None)
    mandate_inspect.add_argument("--mandate-id", required=False, default=None)
    mandate_evaluate = subparsers.add_parser("investment-mandate-evaluate")
    mandate_evaluate.add_argument("--opportunity-id", required=True)
    mandate_evaluate.add_argument("--evaluated-at", required=False, default=None)
    mandate_evaluate.add_argument("--lineage-id", required=False, default=None)
    mandate_evaluate.add_argument("--portfolio-snapshot-id", required=False, default=None)
    mandate_evaluate.add_argument("--idempotency-key", required=False, default=None)
    sustained_resolve = subparsers.add_parser("securities-shadow-resolve")
    sustained_resolve.add_argument("--plan-id", required=True)
    sustained_resolve.add_argument("--cutoff", required=True)
    sustained_status = subparsers.add_parser("securities-shadow-status")
    sustained_status.add_argument("--plan-id", required=True)
    sustained_report = subparsers.add_parser("securities-shadow-report")
    sustained_report.add_argument("--plan-id", required=True)
    securities_run = subparsers.add_parser("securities-research")
    securities_run.add_argument("--campaign-id", required=True)
    securities_run.add_argument("--strategy-id", required=True)
    securities_run.add_argument("--data-batch-id", required=True)
    securities_run.add_argument("--start", required=True)
    securities_run.add_argument("--end", required=True)
    securities_run.add_argument("--idempotency-key", required=True)
    securities_lab_run = subparsers.add_parser("securities-strategy-lab-research")
    securities_lab_run.add_argument("--campaign-id", required=True)
    securities_lab_run.add_argument("--deployment-id", required=True)
    securities_lab_run.add_argument("--data-batch-id", required=True)
    securities_lab_run.add_argument("--start", required=True)
    securities_lab_run.add_argument("--end", required=True)
    securities_lab_run.add_argument("--idempotency-key", required=True)
    securities_conformance_parser = subparsers.add_parser("securities-conformance")
    securities_conformance_parser.add_argument("--research-run-id", required=True)
    securities_conformance_parser.add_argument(
        "--external-engine", required=True, choices=("LEAN",)
    )
    securities_status_parser = subparsers.add_parser("securities-status")
    securities_status_parser.add_argument("--campaign-id", required=True)
    securities_paper_run_parser = subparsers.add_parser("securities-paper-run")
    securities_paper_run_parser.add_argument("--campaign-id", required=True)
    securities_paper_run_parser.add_argument("--trial-id", required=True)
    securities_paper_run_parser.add_argument("--strategy-id", required=True)
    securities_paper_run_parser.add_argument("--data-batch-id", required=True)
    securities_paper_run_parser.add_argument("--start", required=True)
    securities_paper_run_parser.add_argument("--end", required=True)
    securities_paper_run_parser.add_argument("--idempotency-key", required=True)
    securities_mark_parser = subparsers.add_parser("securities-mark")
    securities_mark_parser.add_argument("--campaign-id", required=True)
    securities_mark_parser.add_argument("--as-of", required=True)
    securities_exit_parser = subparsers.add_parser("securities-exit")
    securities_exit_parser.add_argument("--campaign-id", required=True)
    securities_exit_parser.add_argument("--as-of", required=True)
    securities_exit_parser.add_argument(
        "--reason",
        required=True,
        choices=(
            "FIXED_HORIZON",
            "STRATEGY_FLAT",
            "TERMINAL_RUN_DATE",
            "CAMPAIGN_KILL",
            "RISK_REDUCTION",
            "DELISTING",
            "OPERATOR_EXIT",
        ),
    )
    securities_exit_parser.add_argument("--idempotency-key", required=True)
    securities_actions_parser = subparsers.add_parser("securities-apply-actions")
    securities_actions_parser.add_argument("--campaign-id", required=True)
    securities_actions_parser.add_argument("--as-of", required=True)
    securities_paper_status_parser = subparsers.add_parser("securities-paper-status")
    securities_paper_status_parser.add_argument("--campaign-id", required=True)
    baseline_run = subparsers.add_parser("securities-baseline-run")
    baseline_run.add_argument("--baseline-id", required=True)
    baseline_run.add_argument("--campaign-id", required=True)
    baseline_run.add_argument("--data-batch-id", required=True)
    baseline_run.add_argument("--start", required=True)
    baseline_run.add_argument("--end", required=True)
    baseline_run.add_argument(
        "--simulator",
        required=True,
        choices=("APEX_RESEARCH", "APEX_PAPER", "VECTORBT", "LEAN"),
    )
    baseline_run.add_argument("--idempotency-key", required=True)
    retained_reconcile = subparsers.add_parser("securities-reconcile-retained-request")
    retained_reconcile.add_argument("--conformance-report-id", required=True)
    retained_reconcile.add_argument("--idempotency-key", required=True)
    baseline_suite = subparsers.add_parser("securities-baseline-suite")
    baseline_suite.add_argument("--campaign-id", required=True)
    baseline_suite.add_argument("--data-batch-id", required=True)
    baseline_suite.add_argument("--start", required=True)
    baseline_suite.add_argument("--end", required=True)
    baseline_suite.add_argument("--simulators", required=True)
    baseline_suite.add_argument("--idempotency-key", required=True)
    multi_conformance = subparsers.add_parser("securities-multi-simulator-conformance")
    multi_conformance.add_argument("--baseline-id", required=True)
    multi_conformance.add_argument("--data-batch-id", required=True)
    multi_conformance.add_argument("--start", required=True)
    multi_conformance.add_argument("--end", required=True)
    baseline_scorecards = subparsers.add_parser("securities-baseline-scorecards")
    baseline_scorecards.add_argument("--data-batch-id", required=True)
    research_create = subparsers.add_parser("research-experiment-create")
    research_create.add_argument("--name", required=True)
    research_create.add_argument("--vertical", required=True, choices=("SECURITIES",))
    research_create.add_argument("--data-batch-id", required=True)
    research_create.add_argument("--universe-id", required=True)
    research_create.add_argument("--start", required=True)
    research_create.add_argument("--end", required=True)
    research_create.add_argument("--maximum-trials", required=True, type=int)
    research_create.add_argument("--specification", required=True)
    research_create.add_argument("--reason", required=True)
    research_create.add_argument("--idempotency-key", required=True)
    research_evaluate = subparsers.add_parser("research-overfitting-evaluate")
    research_evaluate.add_argument("--experiment-id", required=True)
    research_evaluate.add_argument("--cutoff", required=True)
    research_report = subparsers.add_parser("research-overfitting-report")
    research_report.add_argument("--experiment-id", required=True)
    research_trials_parser = subparsers.add_parser("research-trials")
    research_trials_parser.add_argument("--experiment-id", required=True)
    subparsers.add_parser("research-operations-workspace")
    subparsers.add_parser("research-operations-isolated-acceptance")
    subparsers.add_parser("first-generated-research-method-acceptance")
    subparsers.add_parser("generated-crypto-challenger-acceptance")
    subparsers.add_parser("secure-live-readiness-acceptance")
    subparsers.add_parser("secure-live-readiness-fixture-acceptance")
    subparsers.add_parser("operational-reliability-acceptance")
    subparsers.add_parser("operational-reliability-fixture-acceptance")
    subparsers.add_parser("operational-reliability-workspace")
    subparsers.add_parser("coinbase-read-only-credentials-absent-acceptance")
    subparsers.add_parser("coinbase-read-only-workspace")
    subparsers.add_parser("kalshi-read-only-credentials-absent-acceptance")
    subparsers.add_parser("kalshi-read-only-workspace")
    subparsers.add_parser("live-preflight-genuine-acceptance")
    subparsers.add_parser("live-preflight-fixture-acceptance")
    subparsers.add_parser("live-preflight-workspace")
    subparsers.add_parser("live-readiness-workspace")
    credential_register = subparsers.add_parser("credential-reference-register")
    credential_register.add_argument("--reference-id", required=True)
    credential_register.add_argument("--version", type=int, required=True)
    credential_register.add_argument(
        "--provider", choices=("COINBASE", "KALSHI", "ALPACA"), required=True
    )
    credential_register.add_argument("--environment", choices=("PAPER", "LIVE"), required=True)
    credential_register.add_argument("--account-purpose", required=True)
    credential_register.add_argument(
        "--permission",
        action="append",
        choices=("PUBLIC_DATA", "PRIVATE_VIEW", "ORDER_PREVIEW", "TRADE", "TRANSFER", "WITHDRAWAL"),
        required=True,
    )
    credential_register.add_argument(
        "--secret-backend",
        choices=(
            "ENVIRONMENT_REFERENCE",
            "LOCAL_IGNORED_FILE_REFERENCE",
            "OPERATING_SYSTEM_SECRET_REFERENCE",
        ),
        required=True,
    )
    credential_register.add_argument("--backend-reference-id", required=True)
    credential_register.add_argument("--previous-reference-id")
    credential_register.add_argument("--actor", required=True)
    credential_register.add_argument("--reason", required=True)
    credential_register.add_argument("--confirmation", required=True)
    credential_register.add_argument("--idempotency-key", required=True)
    credential_register.add_argument("--at", required=True)
    credential_inspect = subparsers.add_parser("credential-reference-inspect")
    credential_inspect.add_argument("--reference-id", required=True)
    capability_inspect = subparsers.add_parser("provider-capability-inspect")
    capability_inspect.add_argument("--snapshot-id", required=True)
    capability_inspect.add_argument(
        "--provider", choices=("COINBASE", "KALSHI", "ALPACA"), required=True
    )
    capability_inspect.add_argument("--account-scope-id", required=True)
    capability_inspect.add_argument("--environment", choices=("PAPER", "LIVE"), required=True)
    capability_inspect.add_argument("--reference-id")
    capability_inspect.add_argument("--request-limit", type=int, default=0, choices=range(0, 6))
    capability_inspect.add_argument("--actor", required=True)
    capability_inspect.add_argument("--reason", required=True)
    capability_inspect.add_argument("--confirmation", required=True)
    capability_inspect.add_argument("--idempotency-key", required=True)
    capability_inspect.add_argument("--at", required=True)
    risk_policy_inspect = subparsers.add_parser("live-risk-policy-inspect")
    risk_policy_inspect.add_argument("--policy-id", required=True)
    control_inspect = subparsers.add_parser("execution-control-inspect")
    control_inspect.add_argument("--control-id", required=True)
    control_set = subparsers.add_parser("execution-control-set")
    control_set.add_argument("--event-id", required=True)
    control_set.add_argument("--control-id", required=True)
    control_set.add_argument(
        "--state",
        choices=("DISABLED", "PAPER_ONLY", "LIVE_BLOCKED", "EMERGENCY_STOP"),
        required=True,
    )
    control_set.add_argument("--actor", required=True)
    control_set.add_argument("--reason", required=True)
    control_set.add_argument("--confirmation", required=True)
    control_set.add_argument("--idempotency-key", required=True)
    control_set.add_argument("--at", required=True)
    backup_inspect = subparsers.add_parser("backup-readiness-inspect")
    backup_inspect.add_argument("--record-id", required=True)
    readiness_materialize = subparsers.add_parser("live-readiness-materialize")
    readiness_materialize.add_argument("--assessment-id", required=True)
    readiness_materialize.add_argument("--decision-id", required=True)
    readiness_materialize.add_argument("--policy-id", required=True)
    readiness_materialize.add_argument(
        "--provider", choices=("COINBASE", "KALSHI", "ALPACA"), required=True
    )
    readiness_materialize.add_argument("--account-scope-id", required=True)
    readiness_materialize.add_argument("--control-id", required=True)
    readiness_materialize.add_argument("--actor", required=True)
    readiness_materialize.add_argument("--reason", required=True)
    readiness_materialize.add_argument("--confirmation", required=True)
    readiness_materialize.add_argument("--idempotency-key", required=True)
    readiness_materialize.add_argument("--at", required=True)
    readiness_inspect = subparsers.add_parser("live-readiness-inspect")
    readiness_inspect.add_argument("--assessment-id", required=True)
    blockers_inspect = subparsers.add_parser("live-readiness-blockers")
    blockers_inspect.add_argument("--assessment-id", required=True)
    readiness_export = subparsers.add_parser("live-readiness-export")
    readiness_export.add_argument("--export-id", required=True)
    readiness_export.add_argument("--assessment-id", required=True)
    readiness_export.add_argument("--actor", required=True)
    readiness_export.add_argument("--reason", required=True)
    readiness_export.add_argument("--confirmation", required=True)
    readiness_export.add_argument("--idempotency-key", required=True)
    readiness_export.add_argument("--at", required=True)
    research_agenda_refresh = subparsers.add_parser("research-operations-agenda-refresh")
    research_agenda_refresh.add_argument("--as-of", required=True)
    research_generation = subparsers.add_parser("research-operations-proposals-generate")
    research_generation.add_argument("--generator-run-id", required=True)
    research_generation.add_argument("--as-of", required=True)
    research_generation.add_argument("--maximum-proposals", type=int, default=3, choices=(1, 2, 3))
    research_generator_inspect = subparsers.add_parser("research-operations-generator-run-inspect")
    research_generator_inspect.add_argument("--generator-run-id", required=True)
    research_proposal_inspect = subparsers.add_parser("research-operations-proposal-inspect")
    research_proposal_inspect.add_argument("--proposal-id", required=True)
    research_priority_inspect = subparsers.add_parser("research-operations-priority-inspect")
    research_priority_inspect.add_argument("--proposal-id", required=True)
    research_feasibility_inspect = subparsers.add_parser("research-operations-feasibility-inspect")
    research_feasibility_inspect.add_argument("--proposal-id", required=True)
    subparsers.add_parser("research-operations-review-queue")
    research_enqueue = subparsers.add_parser("research-operations-experiment-enqueue")
    research_enqueue.add_argument("--queue-id", required=True)
    research_enqueue.add_argument("--proposal-id", required=True)
    research_enqueue.add_argument("--run-id", required=True)
    research_enqueue.add_argument("--idempotency-key", required=True)
    research_queue_inspect = subparsers.add_parser("research-operations-queue-inspect")
    research_queue_inspect.add_argument("--queue-id", required=True)
    research_queue_cancel = subparsers.add_parser("research-operations-queue-cancel")
    research_queue_cancel.add_argument("--queue-id", required=True)
    research_queue_cancel.add_argument("--reason", required=True)
    research_queue_cancel.add_argument("--idempotency-key", required=True)
    research_queue_execute = subparsers.add_parser("research-operations-queue-execute")
    research_queue_execute.add_argument("--queue-id", required=True)
    research_queue_execute.add_argument("--idempotency-key", required=True)
    research_recommendation = subparsers.add_parser("research-operations-recommendation-inspect")
    research_recommendation.add_argument("--recommendation-id", required=True)
    candidate_create = subparsers.add_parser("candidate-search-create")
    candidate_create.add_argument("--experiment-id", required=True)
    candidate_create.add_argument("--name", required=True)
    candidate_create.add_argument("--dataset-id", required=True)
    candidate_create.add_argument("--feature-schema-id", required=True)
    candidate_create.add_argument("--specification", required=True)
    candidate_create.add_argument(
        "--objective",
        required=True,
        choices=("BRIER", "LOG_LOSS", "COST_ADJUSTED_EXCESS_RETURN"),
    )
    candidate_create.add_argument("--maximum-trials", type=int, required=True)
    candidate_create.add_argument("--seed", type=int, required=True)
    candidate_create.add_argument("--reason", required=True)
    candidate_create.add_argument("--idempotency-key", required=True)
    candidate_run = subparsers.add_parser("candidate-search-run")
    candidate_run.add_argument("--search-plan-id", required=True)
    candidate_run.add_argument("--requested-trials", type=int, required=True)
    candidate_run.add_argument("--idempotency-key", required=True)
    candidate_recover = subparsers.add_parser("candidate-search-recover")
    candidate_recover.add_argument("--search-run-id", required=True)
    candidate_cancel = subparsers.add_parser("candidate-search-cancel")
    candidate_cancel.add_argument("--search-run-id", required=True)
    candidate_cancel.add_argument("--reason", required=True)
    candidate_status = subparsers.add_parser("candidate-search-status")
    candidate_status.add_argument("--search-run-id", required=True)
    candidate_report = subparsers.add_parser("candidate-search-report")
    candidate_report.add_argument("--search-run-id", required=True)
    mirror_reconcile = subparsers.add_parser("mlflow-reconcile")
    mirror_reconcile.add_argument("--experiment-id", required=True)
    batch_plan = subparsers.add_parser("batch-learning-plan-create")
    batch_plan.add_argument("--name", required=True)
    batch_plan.add_argument("--source-scope", required=True)
    batch_plan.add_argument("--feature-schema-id", required=True)
    batch_plan.add_argument("--search-template", required=True)
    batch_plan.add_argument("--minimum-new-groups", type=int, required=True)
    batch_plan.add_argument("--minimum-new-observations", type=int, required=True)
    batch_plan.add_argument("--maximum-trials-per-cycle", type=int, required=True)
    batch_plan.add_argument("--maximum-cycles", type=int, required=True)
    batch_plan.add_argument("--reason", required=True)
    batch_plan.add_argument("--idempotency-key", required=True)
    batch_activate = subparsers.add_parser("batch-learning-plan-activate")
    batch_activate.add_argument("--plan-id", required=True)
    batch_activate.add_argument("--confirm-plan-id", required=True)
    batch_activate.add_argument("--confirm-plan-hash", required=True)
    batch_activate.add_argument("--reason", required=True)
    for action in ("pause", "resume", "revoke"):
        batch_control = subparsers.add_parser(f"batch-learning-plan-{action}")
        batch_control.add_argument("--plan-id", required=True)
        batch_control.add_argument("--reason", required=True)
    batch_trigger = subparsers.add_parser("batch-learning-trigger")
    batch_trigger.add_argument("--plan-id", required=True)
    batch_trigger.add_argument("--idempotency-key", required=True)
    batch_cycle = subparsers.add_parser("batch-learning-cycle-run")
    batch_cycle.add_argument("--plan-id", required=True)
    batch_cycle.add_argument("--idempotency-key", required=True)
    batch_recover = subparsers.add_parser("batch-learning-cycle-recover")
    batch_recover.add_argument("--cycle-id", required=True)
    batch_cancel = subparsers.add_parser("batch-learning-cycle-cancel")
    batch_cancel.add_argument("--cycle-id", required=True)
    batch_cancel.add_argument("--reason", required=True)
    batch_attach = subparsers.add_parser("batch-learning-attach-challenger")
    batch_attach.add_argument("--cycle-id", required=True)
    batch_attach.add_argument("--deployment-id", required=True)
    batch_attach.add_argument("--idempotency-key", required=True)
    batch_infer = subparsers.add_parser("batch-learning-forward-infer")
    batch_infer.add_argument("--window-id", required=True)
    batch_infer.add_argument("--cutoff", required=True)
    batch_evaluate = subparsers.add_parser("batch-learning-forward-evaluate")
    batch_evaluate.add_argument("--window-id", required=True)
    batch_evaluate.add_argument("--cutoff", required=True)
    batch_status = subparsers.add_parser("batch-learning-status")
    batch_status.add_argument("--plan-id", required=True)
    batch_report = subparsers.add_parser("batch-learning-report")
    batch_report.add_argument("--cycle-id", required=True)
    batch_worker = subparsers.add_parser("batch-learning-worker")
    batch_worker.add_argument("--once", action="store_true", required=True)
    batch_worker.add_argument("--plan-id")
    securities_paper_conformance_parser = subparsers.add_parser("securities-paper-conformance")
    securities_paper_conformance_parser.add_argument(
        "--paper-run-id",
        required=True,
    )
    securities_paper_conformance_parser.add_argument(
        "--external-engine",
        required=True,
        choices=("LEAN",),
    )
    strategy_snapshot = subparsers.add_parser("strategy-evidence-snapshot")
    strategy_snapshot.add_argument("--strategy-id", required=True)
    strategy_snapshot.add_argument("--policy-id", required=True)
    strategy_snapshot.add_argument("--cutoff", required=True)
    strategy_review = subparsers.add_parser("request-strategy-review")
    strategy_review.add_argument("--strategy-id", required=True)
    strategy_review.add_argument("--campaign-id", required=True)
    strategy_review.add_argument("--snapshot-id", required=True)
    strategy_review.add_argument("--reason", required=True)
    strategy_review.add_argument("--idempotency-key", required=True)
    review_status = subparsers.add_parser("strategy-review-status")
    review_status.add_argument("--review-id", required=True)
    paper_trial = subparsers.add_parser("approve-paper-trial")
    paper_trial.add_argument("--review-id", required=True)
    paper_trial.add_argument("--campaign-id", required=True)
    paper_trial.add_argument("--starts-at", required=True)
    paper_trial.add_argument("--expires-at", required=True)
    paper_trial.add_argument("--maximum-cycles", required=True)
    paper_trial.add_argument("--maximum-orders", required=True)
    paper_trial.add_argument("--maximum-positions", required=True)
    paper_trial.add_argument("--maximum-loss", required=True)
    paper_trial.add_argument("--confirmation-strategy", required=True)
    paper_trial.add_argument("--confirmation-campaign", required=True)
    paper_trial.add_argument("--confirmation-snapshot", required=True)
    paper_trial.add_argument("--idempotency-key", required=True)
    down = subparsers.add_parser("db-down")
    down.add_argument("--volumes", action="store_true")
    demo = subparsers.add_parser("demo-core")
    demo.add_argument(
        "--fixture",
        required=True,
        choices=(
            "golden",
            "invariant-rejected",
            "no-proposal",
            "partial-fill-canceled",
            "partial-fill-canceled-settled-no",
            "partial-fill-canceled-settled-void",
            "policy-rejected",
            "unfilled-expired",
        ),
    )
    demo.add_argument("--capital", required=True)
    demo.add_argument("--seed", required=True)
    subparsers.add_parser("autopilot-strategies")
    autopilot_status_parser = subparsers.add_parser("autopilot-status")
    autopilot_status_parser.add_argument("--campaign-id", required=True)
    autopilot_cycle_parser = subparsers.add_parser("autopilot-cycle")
    autopilot_cycle_parser.add_argument("--campaign-id", required=True)
    autopilot_cycle_parser.add_argument("--mode", choices=("shadow", "paper"), default="shadow")
    autopilot_cycle_parser.add_argument("--idempotency-key", required=True)
    autopilot_cycle_parser.add_argument("--maximum-strategies", type=int, default=25)
    autopilot_cycle_parser.add_argument("--maximum-proposals", type=int, default=100)
    autopilot_cycle_parser.add_argument("--deadline-seconds", type=int, default=30)
    autopilot_cycle_parser.add_argument("--operator-note", default="")
    for action in ("pause", "resume", "kill"):
        autopilot_control_parser = subparsers.add_parser(f"autopilot-{action}")
        autopilot_control_parser.add_argument("--campaign-id", required=True)
        autopilot_control_parser.add_argument("--reason", required=True)
        autopilot_control_parser.add_argument("--idempotency-key", required=True)
    subparsers.add_parser("autopilot-core-demo")
    subparsers.add_parser("paper-portfolio-demo")
    subparsers.add_parser("strategy-shadow-demo")
    subparsers.add_parser("orderbook-microstructure-shadow-demo")
    subparsers.add_parser("market-dynamics-shadow-demo")
    multi_cycle_parser = subparsers.add_parser("multi-strategy-shadow-cycle")
    multi_cycle_parser.add_argument("--campaign-id", required=True)
    multi_cycle_parser.add_argument("--idempotency-key", required=True)
    multi_cycle_parser.add_argument("--market-limit", type=int, default=20)
    multi_cycle_parser.add_argument("--maximum-predictions", type=int, default=100)
    multi_cycle_parser.add_argument("--maximum-opportunities", type=int, default=40)
    multi_session_parser = subparsers.add_parser("multi-strategy-shadow-session")
    multi_session_parser.add_argument("--campaign-id", required=True)
    multi_session_parser.add_argument("--iterations", type=int, required=True)
    multi_session_parser.add_argument("--interval-seconds", type=int, required=True)
    multi_session_parser.add_argument("--market-limit", type=int, required=True)
    multi_session_parser.add_argument("--max-pages", type=int, required=True)
    multi_session_parser.add_argument("--max-enrichments", type=int, required=True)
    multi_session_parser.add_argument("--orderbook-depth", type=int, required=True)
    multi_reconcile_parser = subparsers.add_parser("reconcile-multi-strategy-shadow-session")
    multi_reconcile_parser.add_argument("--session-id", required=True)
    subparsers.add_parser("multi-strategy-shadow-demo")
    create_plan_parser = subparsers.add_parser("create-shadow-plan")
    create_plan_parser.add_argument("--campaign-id", required=True)
    create_plan_parser.add_argument("--name", required=True)
    create_plan_parser.add_argument("--iterations", type=int, default=12)
    create_plan_parser.add_argument("--interval-seconds", type=int, default=300)
    create_plan_parser.add_argument("--market-count", type=int, default=10)
    create_plan_parser.add_argument("--max-pages", type=int, default=3)
    create_plan_parser.add_argument("--max-enrichments", type=int, default=30)
    create_plan_parser.add_argument("--max-markets-per-event", type=int, default=2)
    create_plan_parser.add_argument("--max-markets-per-series", type=int, default=4)
    create_plan_parser.add_argument("--orderbook-depth", type=int, default=5)
    create_plan_parser.add_argument("--market-tickers")
    create_plan_parser.add_argument("--idempotency-key", required=True)
    atomic_plan_parser = subparsers.add_parser("discover-create-shadow-plan")
    atomic_plan_parser.add_argument("--campaign-id", required=True)
    atomic_plan_parser.add_argument("--name", required=True)
    atomic_plan_parser.add_argument("--window-start", required=True)
    atomic_plan_parser.add_argument("--window-end", required=True)
    atomic_plan_parser.add_argument("--iterations", required=True, type=int)
    atomic_plan_parser.add_argument("--interval-seconds", required=True, type=int)
    atomic_plan_parser.add_argument("--target-markets", required=True, type=int)
    atomic_plan_parser.add_argument("--minimum-markets", required=True, type=int)
    atomic_plan_parser.add_argument("--minimum-events", required=True, type=int)
    atomic_plan_parser.add_argument("--max-pages", required=True, type=int)
    atomic_plan_parser.add_argument("--max-inspected-markets", required=True, type=int)
    atomic_plan_parser.add_argument("--max-enrichments", required=True, type=int)
    atomic_plan_parser.add_argument("--max-markets-per-event", required=True, type=int)
    atomic_plan_parser.add_argument("--max-markets-per-series", required=True, type=int)
    atomic_plan_parser.add_argument("--orderbook-depth", required=True, type=int)
    atomic_plan_parser.add_argument("--max-orderbook-preflights", type=int, default=80)
    atomic_plan_parser.add_argument(
        "--microstructure-configuration-version",
        default="orderbook-microstructure-shadow-v1",
    )
    atomic_plan_parser.add_argument("--idempotency-key", required=True)
    atomic_plan_parser.add_argument("--exclude-plan-id", action="append", default=[])
    atomic_plan_parser.add_argument(
        "--exclude-prior-plan-markets", action=argparse.BooleanOptionalAction, default=True
    )
    atomic_plan_parser.add_argument("--exclude-prior-plan-events", action="store_true")
    atomic_plan_parser.add_argument(
        "--exclude-active-plan-markets", action=argparse.BooleanOptionalAction, default=True
    )
    run_plan_parser = subparsers.add_parser("run-shadow-plan")
    run_plan_parser.add_argument("--plan-id", required=True)
    run_plan_parser.add_argument("--once", action="store_true")
    worker_parser = subparsers.add_parser("shadow-worker")
    worker_parser.add_argument("--plan-id")
    worker_parser.add_argument("--once", action="store_true")
    worker_parser.add_argument("--poll-seconds", type=int, default=5)
    worker_parser.add_argument("--maximum-plans", type=int, default=1)
    outcome_parser = subparsers.add_parser("collect-shadow-outcomes")
    outcome_parser.add_argument("--campaign-id", required=True)
    outcome_parser.add_argument(
        "--market-limit", "--maximum-markets", dest="market_limit", type=int, default=100
    )
    performance_parser = subparsers.add_parser("strategy-performance")
    performance_parser.add_argument("--campaign-id", required=True)
    performance_parser.add_argument("--strategy-id")
    performance_parser.add_argument("--plan-id")
    subparsers.add_parser("shadow-operations-demo")
    create_shadow_campaign_parser = subparsers.add_parser("create-shadow-campaign")
    create_shadow_campaign_parser.add_argument("--name", required=True)
    create_shadow_campaign_parser.add_argument("--capital", required=True)
    create_shadow_campaign_parser.add_argument("--maximum-loss", required=True)
    create_shadow_campaign_parser.add_argument("--expires-in-days", type=int, required=True)
    create_shadow_campaign_parser.add_argument("--idempotency-key", required=True)
    reconcile_governance_parser = subparsers.add_parser("reconcile-local-strategy-governance")
    reconcile_governance_parser.add_argument("--database", required=True, choices=("apex",))
    reconcile_governance_parser.add_argument("--apply", action="store_true")
    reconcile_governance_parser.add_argument("--confirmation")
    paper_status_parser = subparsers.add_parser("paper-portfolio-status")
    paper_status_parser.add_argument("--campaign-id", required=True)
    paper_mark_parser = subparsers.add_parser("paper-mark-positions")
    paper_mark_parser.add_argument("--campaign-id", required=True)
    paper_mark_parser.add_argument("--orderbook-run-id", required=True)
    paper_settle_parser = subparsers.add_parser("paper-settle-position")
    paper_settle_parser.add_argument("--position-id", required=True)
    paper_settle_parser.add_argument("--settlement-fact-id", required=True)
    live_shadow_parser = subparsers.add_parser("live-shadow-session")
    live_shadow_parser.add_argument("--campaign-id", required=True)
    live_shadow_parser.add_argument("--strategy-id", required=True)
    live_shadow_parser.add_argument("--iterations", type=int, required=True)
    live_shadow_parser.add_argument("--interval-seconds", type=int, required=True)
    live_shadow_parser.add_argument("--market-limit", type=int, required=True)
    live_shadow_parser.add_argument("--maximum-pages", type=int, default=1)
    live_shadow_parser.add_argument("--maximum-enrichments", type=int, default=20)
    live_shadow_parser.add_argument("--observations-per-market", type=int, default=3)
    live_shadow_parser.add_argument("--orderbook-depth", type=int, default=5)
    fixture_scan = subparsers.add_parser("scan-kalshi-fixture")
    fixture_scan.add_argument("--persist", action="store_true")
    smoke = subparsers.add_parser("smoke-kalshi-public")
    smoke.add_argument("--limit", type=int, default=5)
    smoke.add_argument("--max-pages", type=int, default=1)
    smoke.add_argument("--timeout-seconds", type=int, default=10)
    smoke.add_argument("--persist", action="store_true")
    consistency_fixture = subparsers.add_parser("scan-kalshi-consistency-fixture")
    consistency_fixture.add_argument("--persist", action="store_true")
    consistency = subparsers.add_parser("smoke-kalshi-consistency")
    consistency.add_argument("--event-limit", type=int, default=5)
    consistency.add_argument("--market-limit", type=int, default=25)
    consistency.add_argument("--max-pages", type=int, default=1)
    consistency.add_argument("--timeout-seconds", type=int, default=10)
    consistency.add_argument("--persist", action="store_true")
    subparsers.add_parser("compare-kalshi-scans-fixture")
    comparison = subparsers.add_parser("compare-kalshi-scans")
    comparison.add_argument("--baseline-run-id", required=True)
    comparison.add_argument("--current-run-id", required=True)
    subparsers.add_parser("compare-kalshi-latest-scans")
    subparsers.add_parser("summarize-kalshi-event-history-fixture")
    history = subparsers.add_parser("summarize-kalshi-event-history")
    history.add_argument("--event-ticker", required=True)
    history.add_argument("--max-runs", type=int, default=20)
    history.add_argument("--first-run-id")
    history.add_argument("--last-run-id")
    history.add_argument("--continuity-gap-seconds", type=int, default=1800)
    subparsers.add_parser("summarize-kalshi-orderbook-history-fixture")
    orderbook_history = subparsers.add_parser("summarize-kalshi-orderbook-history")
    orderbook_history.add_argument("--market-ticker", required=True)
    orderbook_history.add_argument("--max-observations", type=int, default=20)
    orderbook_history.add_argument("--first-snapshot-id")
    orderbook_history.add_argument("--last-snapshot-id")
    orderbook_history.add_argument("--continuity-gap-seconds", type=int, default=300)
    subparsers.add_parser("scan-kalshi-binary-complements-fixture")
    complements = subparsers.add_parser("smoke-kalshi-binary-complements")
    complements.add_argument("--market-limit", type=int, default=20)
    complements.add_argument("--depth", type=int, default=5)
    complements.add_argument("--max-bundle-contracts", default="10")
    complements.add_argument("--timeout-seconds", type=int, default=10)
    complements.add_argument("--discover-first", action="store_true")
    subparsers.add_parser("capture-kalshi-orderbook-series-fixture")
    capture = subparsers.add_parser("capture-kalshi-orderbook-series")
    capture_group = capture.add_mutually_exclusive_group(required=True)
    capture_group.add_argument("--market-ticker")
    capture_group.add_argument("--latest-open-market", action="store_true")
    capture.add_argument("--discover-first", action="store_true")
    capture.add_argument("--observations", type=int, default=3)
    capture.add_argument("--interval-seconds", type=int, default=10)
    capture.add_argument("--depth", type=int, default=5)
    capture.add_argument("--max-bundle-contracts", default="10")
    capture.add_argument("--timeout-seconds", type=int, default=10)
    subparsers.add_parser("sweep-kalshi-orderbooks-fixture")
    sweep = subparsers.add_parser("sweep-kalshi-orderbooks")
    sweep.add_argument("--market-count", type=int, default=12)
    sweep.add_argument("--max-pages", type=int, default=3)
    sweep.add_argument("--market-limit", type=int, default=300)
    sweep.add_argument("--max-enrichments", type=int, default=40)
    sweep.add_argument("--observations", type=int, default=3)
    sweep.add_argument("--interval-seconds", type=int, default=5)
    sweep.add_argument("--depth", type=int, default=5)
    sweep.add_argument("--max-bundle-contracts", default="10")
    sweep.add_argument("--timeout-seconds", type=int, default=10)
    subparsers.add_parser("scan-kalshi-threshold-ladders-fixture")
    threshold_ladders = subparsers.add_parser("scan-kalshi-threshold-ladders")
    threshold_ladders.add_argument("--scan-id")
    threshold_ladders.add_argument("--event-count", type=int, default=5)
    threshold_ladders.add_argument("--max-pages", type=int, default=3)
    threshold_ladders.add_argument("--market-limit", type=int, default=300)
    threshold_ladders.add_argument("--max-enrichments", type=int, default=40)
    threshold_ladders.add_argument("--max-markets-per-event", type=int, default=8)
    threshold_ladders.add_argument("--max-relationships", type=int, default=100)
    threshold_ladders.add_argument("--rounds", type=int, default=3)
    threshold_ladders.add_argument("--interval-seconds", type=int, default=10)
    threshold_ladders.add_argument("--depth", type=int, default=5)
    threshold_ladders.add_argument("--max-bundle-contracts", default="10")
    threshold_ladders.add_argument("--max-observation-skew-seconds", type=int, default=5)
    threshold_ladders.add_argument("--timeout-seconds", type=int, default=10)
    diagnostic = subparsers.add_parser("diagnose-kalshi-capture-selection")
    diagnostic.add_argument("--max-pages", type=int, default=3)
    diagnostic.add_argument("--market-limit", type=int, default=300)
    diagnostic.add_argument("--timeout-seconds", type=int, default=10)
    subparsers.add_parser("reconcile-kalshi-capture-sessions")
    subparsers.add_parser("external-paper-status")
    subparsers.add_parser("alpaca-paper-readiness")
    subparsers.add_parser("alpaca-paper-acceptance-demo")
    alpaca_paper_preflight_parser = subparsers.add_parser("alpaca-paper-preflight")
    alpaca_paper_preflight_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_submit_parser = subparsers.add_parser("alpaca-paper-submit")
    alpaca_paper_submit_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_submit_parser.add_argument("--confirmation", required=True)
    alpaca_paper_sync_parser = subparsers.add_parser("alpaca-paper-sync")
    alpaca_paper_sync_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_status_parser = subparsers.add_parser("alpaca-paper-status")
    alpaca_paper_status_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_status_parser.add_argument("--refresh", action="store_true")
    alpaca_paper_validate_parser = subparsers.add_parser("alpaca-paper-validate-fill")
    alpaca_paper_validate_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_validate_parser.add_argument("--evidence-id", required=True)
    alpaca_paper_apply_parser = subparsers.add_parser("alpaca-paper-apply-fills")
    alpaca_paper_apply_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_reconcile_parser = subparsers.add_parser("alpaca-paper-reconcile")
    alpaca_paper_reconcile_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_process_parser = subparsers.add_parser("alpaca-paper-process")
    alpaca_paper_process_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_process_parser.add_argument("--apply-confirmation")
    alpaca_paper_cancel_parser = subparsers.add_parser("alpaca-paper-cancel")
    alpaca_paper_cancel_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_cancel_parser.add_argument("--confirmation", required=True)
    alpaca_paper_close_parser = subparsers.add_parser("alpaca-paper-close")
    alpaca_paper_close_parser.add_argument("--instruction-id", required=True)
    alpaca_paper_close_parser.add_argument("--position-id", required=True)
    alpaca_paper_close_parser.add_argument("--confirmation", required=True)
    alpaca_paper_acceptance_parser = subparsers.add_parser("alpaca-paper-acceptance-create")
    alpaca_paper_acceptance_parser.add_argument("--symbol", required=True)
    alpaca_paper_acceptance_parser.add_argument("--idempotency-key", required=True)
    alpaca_paper_acceptance_status_parser = subparsers.add_parser("alpaca-paper-acceptance-status")
    alpaca_paper_acceptance_status_parser.add_argument("--campaign-id", required=True)
    alpaca_paper_acceptance_instruction_parser = subparsers.add_parser(
        "alpaca-paper-acceptance-instruction"
    )
    alpaca_paper_acceptance_instruction_parser.add_argument("--instruction-id", required=True)
    subparsers.add_parser("external-paper-reconciliation-demo")
    external_paper_instruction_parser = subparsers.add_parser("external-paper-instruction-create")
    external_paper_instruction_parser.add_argument("--authorization-id", required=True)
    external_paper_instruction_parser.add_argument("--symbol", required=True)
    external_paper_instruction_parser.add_argument("--side", choices=("BUY", "SELL"), required=True)
    external_paper_instruction_parser.add_argument("--quantity", required=True)
    external_paper_instruction_parser.add_argument("--maximum-notional", required=True)
    external_paper_instruction_parser.add_argument("--expires-at", required=True)
    external_paper_instruction_parser.add_argument("--idempotency-key", required=True)
    external_paper_reduce_parser = subparsers.add_parser("external-paper-reduce")
    external_paper_reduce_parser.add_argument("--instruction-id", required=True)
    external_paper_reduce_parser.add_argument("--position-id", required=True)
    external_paper_reduce_parser.add_argument("--decision-at", required=True)
    external_paper_reduce_parser.add_argument("--earliest-fill-at", required=True)
    external_paper_reduce_parser.add_argument("--expires-at", required=True)
    external_paper_prepare_parser = subparsers.add_parser("external-paper-prepare")
    external_paper_prepare_parser.add_argument("--instruction-id", required=True)
    external_paper_prepare_parser.add_argument("--binding-id", required=True)
    external_paper_submit_parser = subparsers.add_parser("external-paper-submit")
    external_paper_submit_parser.add_argument("--instruction-id", required=True)
    external_paper_sync_parser = subparsers.add_parser("external-paper-sync")
    external_paper_sync_parser.add_argument("--instruction-id", required=True)
    external_paper_cancel_parser = subparsers.add_parser("external-paper-cancel")
    external_paper_cancel_parser.add_argument("--instruction-id", required=True)
    external_paper_apply_fills_parser = subparsers.add_parser("external-paper-apply-fills")
    external_paper_apply_fills_parser.add_argument("--instruction-id", required=True)
    external_paper_validate_fills_parser = subparsers.add_parser("external-paper-validate-fills")
    external_paper_validate_fills_parser.add_argument("--instruction-id", required=True)
    external_paper_validate_fills_parser.add_argument("--evidence-id", required=True)
    external_paper_reconcile_parser = subparsers.add_parser("external-paper-reconcile")
    external_paper_reconcile_parser.add_argument("--instruction-id", required=True)
    external_paper_kill_parser = subparsers.add_parser("external-paper-kill")
    external_paper_kill_parser.add_argument("--campaign-id", required=True)
    external_paper_kill_parser.add_argument("--reason", required=True)
    external_paper_kill_parser.add_argument("--confirmation", required=True)
    external_paper_flatten_parser = subparsers.add_parser("external-paper-flatten")
    external_paper_flatten_parser.add_argument("--campaign-id", required=True)
    external_paper_flatten_parser.add_argument("--reason", required=True)
    external_paper_flatten_parser.add_argument("--confirmation", required=True)
    subparsers.add_parser("research-gfs-archive-feasibility-fixture")
    gfs_research = subparsers.add_parser("research-gfs-archive-feasibility")
    gfs_research.add_argument("--series", required=True)
    gfs_research.add_argument("--sample-dates", type=int, required=True)
    gfs_research.add_argument("--lead-hours", required=True)
    gfs_research.add_argument("--max-requests", type=int, required=True)
    gfs_research.add_argument("--max-download-mb", type=int, required=True)
    gfs_research.add_argument("--timeout-seconds", type=int, default=10)
    subparsers.add_parser("research-gfs-weather-edge-fixture")
    weather_edge = subparsers.add_parser("research-gfs-weather-edge")
    weather_edge.add_argument("--series", required=True)
    weather_edge.add_argument("--start-date", required=True)
    weather_edge.add_argument("--end-date", required=True)
    weather_edge.add_argument("--lead-hours", required=True)
    weather_edge.add_argument("--max-event-dates", type=int, required=True)
    weather_edge.add_argument("--bootstrap-repetitions", type=int, required=True)
    weather_edge.add_argument("--max-noaa-requests", type=int, required=True)
    weather_edge.add_argument("--max-download-mb", type=int, required=True)
    weather_edge.add_argument("--timeout-seconds", type=int, default=10)
    return command_parser


def main() -> None:
    arguments = parser().parse_args()
    actions = {
        "bootstrap": bootstrap,
        "db-up": db_up,
        "db-identity": db_identity,
        "format": format_code,
        "lint": lint,
        "migrate": migrate,
        "typecheck": typecheck,
        "test": test,
        "test-offline": test_offline,
        "test-persistence": test_persistence,
        "verify-core": verify_core,
        "operator-api": operator_api,
        "operator-ui": operator_ui,
        "operator-build": operator_build,
        "operator-app": operator_app,
        "operator-demo-seed": operator_demo_seed,
        "operator-console-demo": operator_console_demo,
        "evidence-review-paper-trial-demo": evidence_review_paper_trial_demo,
        "strategy-lab-demo": strategy_lab_demo,
        "strategy-lab-readiness": strategy_lab_readiness,
        "strategy-lab-configuration-readiness": strategy_lab_configuration_readiness,
        "strategy-lab-shadow-demo": strategy_lab_shadow_demo,
        "securities-shadow-demo": securities_shadow_demo,
        "securities-managed-data-readiness": securities_managed_data_readiness,
        "securities-real-data-readiness": securities_real_data_readiness,
        "real-securities-data-contract-demo": real_securities_data_contract_demo,
        "securities-lean-availability": securities_lean_availability,
        "securities-paper-demo": securities_paper_demo,
        "securities-baselines": securities_baselines,
        "securities-simulator-status": securities_simulator_status,
        "securities-baselines-conformance-demo": securities_baselines_conformance_demo,
        "securities-research-trial-summary": securities_research_trial_summary,
        "research-overfitting-demo": research_overfitting_demo,
        "research-integrity-readiness": research_integrity_readiness,
        "research-trial-ledger-summary": research_trial_ledger_summary,
        "automated-search-readiness": automated_search_readiness,
        "bounded-candidate-generation-demo": bounded_candidate_generation_demo,
        "research-operations-isolated-acceptance": research_operations_isolated_acceptance,
        "first-generated-research-method-acceptance": (first_generated_research_method_acceptance),
        "generated-crypto-challenger-acceptance": generated_crypto_challenger_acceptance,
        "secure-live-readiness-acceptance": secure_live_readiness_acceptance,
        "secure-live-readiness-fixture-acceptance": (secure_live_readiness_fixture_acceptance),
        "operational-reliability-acceptance": operational_reliability_acceptance,
        "operational-reliability-fixture-acceptance": (operational_reliability_fixture_acceptance),
        "operational-reliability-workspace": operational_reliability_workspace,
        "coinbase-read-only-credentials-absent-acceptance": (
            coinbase_read_only_credentials_absent_acceptance
        ),
        "coinbase-read-only-workspace": coinbase_read_only_workspace,
        "kalshi-read-only-credentials-absent-acceptance": (
            kalshi_read_only_credentials_absent_acceptance
        ),
        "kalshi-read-only-workspace": kalshi_read_only_workspace,
        "live-preflight-genuine-acceptance": live_preflight_genuine_acceptance,
        "live-preflight-fixture-acceptance": live_preflight_fixture_acceptance,
        "live-preflight-workspace": live_preflight_workspace,
        "automated-batch-learning-demo": automated_batch_learning_demo,
        "portfolio-command-center-demo": portfolio_command_center_demo,
        "batch-learning-readiness": batch_learning_readiness,
        "strategy-intelligence-demo": strategy_intelligence_demo,
        "ensemble-meta-strategy-demo": ensemble_meta_strategy_demo,
        "sustained-real-shadow-demo": sustained_real_shadow_demo,
        "broad-equity-universe-demo": broad_equity_universe_demo,
        "broad-equity-discovery-demo": broad_equity_discovery_demo,
        "options-intelligence-demo": options_intelligence_demo,
        "mlflow-up": mlflow_up,
        "mlflow-down": mlflow_down,
        "mlflow-status": mlflow_status,
        "external-paper-status": external_paper_status,
        "alpaca-paper-readiness": alpaca_paper_readiness,
        "alpaca-paper-acceptance-demo": alpaca_paper_acceptance_demo,
        "external-paper-reconciliation-demo": external_paper_reconciliation_demo,
    }
    if arguments.command in {
        "research-hypothesis-register",
        "research-hypothesis-inspect",
        "research-capture-start",
        "research-capture-resume",
        "research-capture-inspect",
        "research-snapshot-materialize",
        "research-snapshot-inspect",
        "research-experiment-run",
        "research-experiment-replay",
        "research-experiment-inspect",
        "research-outcome-record",
        "research-outcome-inspect",
        "research-evidence-workspace",
    }:
        research_evidence_command(arguments)
        return
    if arguments.command in {
        "live-readiness-workspace",
        "credential-reference-register",
        "credential-reference-inspect",
        "provider-capability-inspect",
        "live-risk-policy-inspect",
        "execution-control-inspect",
        "execution-control-set",
        "backup-readiness-inspect",
        "live-readiness-materialize",
        "live-readiness-inspect",
        "live-readiness-blockers",
        "live-readiness-export",
    }:
        live_readiness_command(arguments)
        return
    if arguments.command in {
        "research-operations-workspace",
        "research-operations-agenda-refresh",
        "research-operations-proposal-inspect",
        "research-operations-proposals-generate",
        "research-operations-generator-run-inspect",
        "research-operations-priority-inspect",
        "research-operations-feasibility-inspect",
        "research-operations-review-queue",
        "research-operations-experiment-enqueue",
        "research-operations-queue-inspect",
        "research-operations-queue-cancel",
        "research-operations-queue-execute",
        "research-operations-recommendation-inspect",
    }:
        research_operations_command(arguments)
        return
    if arguments.command.startswith("crypto-candidate-"):
        crypto_research_command(arguments)
        return
    if arguments.command in {
        "crypto-exposure-record",
        "crypto-exposure-inspect",
        "crypto-campaign-register",
        "crypto-campaign-attach-dataset",
        "crypto-campaign-inspect",
        "crypto-campaign-budget",
        "crypto-campaign-nomination-eligibility",
        "crypto-snapshot-access-check",
        "crypto-holdout-capture-start",
        "crypto-holdout-capture-resume",
        "crypto-holdout-checkpoints",
        "crypto-holdout-materialize",
        "crypto-holdout-seal",
        "crypto-holdout-inspect",
        "crypto-holdout-release-authorize",
        "crypto-hypothesis-register",
        "crypto-hypothesis-inspect",
        "crypto-capture-start",
        "crypto-capture-resume",
        "crypto-capture-inspect",
        "crypto-snapshot-materialize",
        "crypto-snapshot-inspect",
        "crypto-experiment-run",
        "crypto-experiment-replay",
        "crypto-experiment-inspect",
        "crypto-outcome-record",
        "crypto-outcome-inspect",
        "crypto-strategy-register",
        "crypto-strategy-inspect",
        "crypto-strategy-check",
        "crypto-replication-protocol-register",
        "crypto-replication-protocol-inspect",
        "crypto-replication-run",
        "crypto-replication-replay",
        "crypto-replication-inspect",
        "crypto-replication-outcome-record",
        "crypto-replication-outcome-inspect",
        "crypto-dossier-materialize",
        "crypto-dossier-inspect",
        "crypto-dossier-conclusion-record",
        "crypto-dossier-conclusion-inspect",
        "crypto-challenger-hypothesis-register",
        "crypto-challenger-hypothesis-inspect",
        "crypto-challenger-strategy-register",
        "crypto-challenger-strategy-inspect",
        "crypto-challenger-lineage-inspect",
        "crypto-challenger-protocol-register",
        "crypto-challenger-protocol-inspect",
        "crypto-challenger-window-run",
        "crypto-challenger-window-replay",
        "crypto-challenger-window-inspect",
        "crypto-challenger-outcome-record",
        "crypto-challenger-outcome-inspect",
        "crypto-challenger-dossier-materialize",
        "crypto-challenger-dossier-inspect",
        "crypto-challenger-conclusion-record",
        "crypto-challenger-conclusion-inspect",
        "crypto-challenger-compare-prior",
        "crypto-pulse-plan-register",
        "crypto-pulse-plan-inspect",
        "crypto-pulse-refresh-run",
        "crypto-pulse-refresh-replay",
        "crypto-pulse-refresh-inspect",
        "crypto-pulse-checkpoint-inspect",
        "crypto-pulse-snapshot-inspect",
        "crypto-pulse-lineage-inspect",
        "crypto-pulse-summary-inspect",
        "crypto-relative-strength-run",
        "crypto-relative-strength-replay",
        "crypto-relative-strength-inspect",
        "crypto-reversal-regime-run",
        "crypto-reversal-regime-replay",
        "crypto-current-strategy-library",
        "crypto-calibration-history-capture",
        "crypto-products-capture",
        "crypto-products-inspect",
        "crypto-universe-policy-inspect",
        "crypto-observations-capture",
        "crypto-observation-inspect",
        "crypto-universe-materialize",
        "crypto-universe-inspect",
        "crypto-discovery-run",
        "crypto-discovery-inspect",
        "crypto-opportunity-inspect",
        "crypto-approved-champion-status",
        "crypto-research-workspace",
    }:
        crypto_research_command(arguments)
        return
    if arguments.command.startswith("broad-equity-") and not arguments.command.endswith("-demo"):
        broad_equity_research_command(arguments)
        return
    if arguments.command.startswith("options-intelligence-") and not arguments.command.endswith(
        "-demo"
    ):
        options_intelligence_command(arguments)
        return
    if arguments.command.startswith(("options-quote-", "options-chain-", "options-volatility-")):
        options_quote_intelligence_command(arguments)
        return
    if arguments.command.startswith("options-structure"):
        options_structures_command(arguments)
        return
    if arguments.command.startswith(("options-research-", "options-forward-", "options-history-")):
        options_research_operations_command(arguments)
        return
    if arguments.command.startswith("corporate-"):
        corporate_fundamentals_command(arguments)
        return
    if arguments.command.startswith(("portfolio-snapshot-", "investment-mandate-")):
        portfolio_mandate_command(arguments)
        return
    if arguments.command.startswith("security-identity-"):
        security_identity_command(arguments)
        return
    if arguments.command == "db-down":
        db_down(volumes=arguments.volumes)
        return
    if arguments.command == "alpaca-paper-preflight":
        alpaca_paper_preflight(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "alpaca-paper-submit":
        alpaca_paper_submit(
            instruction_id=arguments.instruction_id,
            confirmation=arguments.confirmation,
        )
        return
    if arguments.command == "alpaca-paper-sync":
        alpaca_paper_sync(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "alpaca-paper-status":
        alpaca_paper_status(instruction_id=arguments.instruction_id, refresh=arguments.refresh)
        return
    if arguments.command == "alpaca-paper-validate-fill":
        alpaca_paper_validate_fill(
            instruction_id=arguments.instruction_id, evidence_id=arguments.evidence_id
        )
        return
    if arguments.command == "alpaca-paper-apply-fills":
        alpaca_paper_apply_fills(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "alpaca-paper-reconcile":
        alpaca_paper_reconcile(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "alpaca-paper-process":
        alpaca_paper_process(
            instruction_id=arguments.instruction_id,
            apply_confirmation=arguments.apply_confirmation,
        )
        return
    if arguments.command == "alpaca-paper-cancel":
        alpaca_paper_cancel(
            instruction_id=arguments.instruction_id, confirmation=arguments.confirmation
        )
        return
    if arguments.command == "alpaca-paper-close":
        alpaca_paper_close(
            instruction_id=arguments.instruction_id,
            position_id=arguments.position_id,
            confirmation=arguments.confirmation,
        )
        return
    if arguments.command == "alpaca-paper-acceptance-create":
        alpaca_paper_acceptance_create(
            symbol=arguments.symbol,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "alpaca-paper-acceptance-status":
        alpaca_paper_acceptance_status(campaign_id=arguments.campaign_id)
        return
    if arguments.command == "alpaca-paper-acceptance-instruction":
        alpaca_paper_acceptance_instruction(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "external-paper-kill":
        external_paper_kill(
            campaign_id=arguments.campaign_id,
            reason=arguments.reason,
            confirmation=arguments.confirmation,
        )
        return
    if arguments.command == "external-paper-instruction-create":
        external_paper_instruction_create(
            authorization_id=arguments.authorization_id,
            symbol=arguments.symbol,
            side=arguments.side,
            quantity=arguments.quantity,
            maximum_notional=arguments.maximum_notional,
            expires_at=arguments.expires_at,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "external-paper-reduce":
        external_paper_reduce(
            instruction_id=arguments.instruction_id,
            position_id=arguments.position_id,
            decision_at=arguments.decision_at,
            earliest_fill_at=arguments.earliest_fill_at,
            expires_at=arguments.expires_at,
        )
        return
    if arguments.command == "external-paper-prepare":
        external_paper_prepare(
            instruction_id=arguments.instruction_id,
            binding_id=arguments.binding_id,
        )
        return
    if arguments.command == "external-paper-submit":
        external_paper_submit(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "external-paper-sync":
        external_paper_sync(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "external-paper-cancel":
        external_paper_cancel(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "external-paper-apply-fills":
        external_paper_apply_fills(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "external-paper-validate-fills":
        external_paper_validate_fills(
            instruction_id=arguments.instruction_id,
            evidence_id=arguments.evidence_id,
        )
        return
    if arguments.command == "external-paper-reconcile":
        external_paper_reconcile(instruction_id=arguments.instruction_id)
        return
    if arguments.command == "external-paper-flatten":
        external_paper_flatten(
            campaign_id=arguments.campaign_id,
            reason=arguments.reason,
            confirmation=arguments.confirmation,
        )
        return
    if arguments.command == "verify-persistence":
        verify_persistence(pytest_args=arguments.pytest_arg)
        return
    if arguments.command == "strategy-lab-build-dataset":
        strategy_lab_build_dataset(
            name=arguments.name,
            cutoff=arguments.cutoff,
            idempotency_key=arguments.idempotency_key,
            configuration_hash=arguments.configuration_hash,
            strategy_id=arguments.strategy_id,
            strategy_version=arguments.strategy_version,
        )
        return
    if arguments.command == "securities-genuine-strategy-dataset-build":
        securities_genuine_strategy_dataset_build(
            raw_batch_id=arguments.raw_batch_id,
            derived_batch_id=arguments.derived_batch_id,
            universe_id=arguments.universe_id,
            cutoff=arguments.cutoff,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-genuine-candidate-evaluate":
        securities_genuine_candidate_evaluate(
            dataset_id=arguments.dataset_id,
            validation_run_id=arguments.validation_run_id,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "strategy-lab-train":
        strategy_lab_train(
            dataset_id=arguments.dataset_id,
            model_family=arguments.model_family,
            calibration=arguments.calibration,
            seed=arguments.seed,
            idempotency_key=arguments.idempotency_key,
            hyperparameters_json=arguments.hyperparameters_json,
        )
        return
    if arguments.command == "strategy-lab-evaluate":
        strategy_lab_evaluate(training_run_id=arguments.training_run_id)
        return
    if arguments.command == "strategy-lab-report":
        strategy_lab_report(candidate_id=arguments.candidate_id)
        return
    if arguments.command == "strategy-lab-request-shadow-review":
        strategy_lab_request_shadow_review(
            candidate_id=arguments.candidate_id,
            reason=arguments.reason,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "strategy-lab-approve-shadow-deployment":
        strategy_lab_approve_shadow_deployment(
            review_id=arguments.review_id,
            role=arguments.role,
            candidate_id=arguments.confirm_candidate,
            dataset_id=arguments.confirm_dataset,
            artifact_sha256=arguments.confirm_artifact_sha256,
            evaluation_id=arguments.confirm_evaluation,
            reason=arguments.reason,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "strategy-lab-run-shadow":
        strategy_lab_run_shadow(
            deployment_id=arguments.deployment_id,
            adapter=arguments.adapter,
            cutoff=arguments.cutoff,
            maximum_rows=arguments.maximum_rows,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "strategy-lab-resolve-shadow":
        strategy_lab_resolve_shadow(deployment_id=arguments.deployment_id, cutoff=arguments.cutoff)
        return
    if arguments.command == "strategy-lab-evaluate-challenger":
        strategy_lab_evaluate_challenger(
            deployment_id=arguments.deployment_id, cutoff=arguments.cutoff
        )
        return
    if arguments.command == "strategy-lab-assign-champion":
        strategy_lab_assign_champion(
            deployment_id=arguments.deployment_id,
            reason=arguments.reason,
            confirm_deployment=arguments.confirm_deployment,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-import-data":
        securities_import_data(
            managed_source=arguments.managed_source,
            dataset_name=arguments.dataset_name,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-provider-status":
        securities_provider_status(provider=arguments.provider)
        return
    if arguments.command == "securities-universe-create":
        securities_universe_create(
            name=arguments.name,
            symbols=arguments.symbols,
            policy=arguments.policy,
            reason=arguments.reason,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-provider-assets":
        securities_provider_assets(
            provider=arguments.provider,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-provider-backfill":
        securities_provider_backfill(
            provider=arguments.provider,
            universe_id=arguments.universe_id,
            feed=arguments.feed,
            start=arguments.start,
            end=arguments.end,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-provider-actions":
        securities_provider_actions(
            provider=arguments.provider,
            universe_id=arguments.universe_id,
            start=arguments.start,
            end=arguments.end,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-data-quality":
        securities_data_quality(data_batch_id=arguments.data_batch_id)
        return
    if arguments.command == "securities-canonicalize-retained-alpaca":
        securities_canonicalize_retained_alpaca(
            historical_operation_id=arguments.historical_operation_id,
            identity_operation_id=arguments.identity_operation_id,
            action_operation_id=arguments.action_operation_id,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-real-historical-validation":
        securities_real_historical_validation(
            data_batch_id=arguments.data_batch_id,
            universe_id=arguments.universe_id,
            start=arguments.start,
            end=arguments.end,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-real-validation-report":
        securities_real_validation_report(validation_run_id=arguments.validation_run_id)
        return
    if arguments.command == "historical-data-plan-create":
        historical_data_plan_create(
            symbols=arguments.symbols,
            etf_symbols=arguments.etf_symbols,
            start=arguments.start,
            end=arguments.end,
            chunk_size=arguments.chunk_size,
            maximum_instruments=arguments.maximum_instruments,
            maximum_requests=arguments.maximum_requests,
            maximum_bytes=arguments.maximum_bytes,
            evidence_class=arguments.evidence_class,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "historical-data-capture-run":
        historical_data_capture_run(plan_id=arguments.plan_id, ordinal=arguments.ordinal)
        return
    if arguments.command == "historical-data-snapshot-materialize":
        historical_data_snapshot_materialize(plan_id=arguments.plan_id)
        return
    if arguments.command == "historical-data-quality-report":
        historical_data_quality_report(snapshot_id=arguments.snapshot_id)
        return
    if arguments.command == "historical-data-calculate":
        historical_data_calculate(
            snapshot_id=arguments.snapshot_id,
            stable_security_id=arguments.stable_security_id,
            as_of=arguments.as_of,
            calculation=arguments.calculation,
            window=arguments.window,
        )
        return
    if arguments.command == "historical-data-workspace":
        historical_data_workspace()
        return
    if arguments.command == "genuine-multi-asset-registry-inspect":
        genuine_multi_asset_registry_inspect(
            hypothesis_version_id=arguments.hypothesis_version_id,
            hypothesis_id=arguments.hypothesis_id,
        )
        return
    if arguments.command == "replay-plan-create":
        replay_plan_create(
            dataset_snapshot_id=arguments.dataset_snapshot_id,
            start=arguments.start,
            end=arguments.end,
            calendar_identity=arguments.calendar_identity,
            hypothesis_version_id=arguments.hypothesis_version_id,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command in ("replay-run-start", "replay-run-resume"):
        (replay_run_start if arguments.command == "replay-run-start" else replay_run_resume)(
            replay_run_id=arguments.replay_run_id,
            maximum_steps=arguments.maximum_steps,
            benchmark_stable_security_id=arguments.benchmark_stable_security_id,
            crypto_capture_id=arguments.crypto_capture_id,
        )
        return
    if arguments.command == "replay-run-inspect":
        replay_run_inspect(replay_run_id=arguments.replay_run_id)
        return
    if arguments.command == "replay-frame-inspect":
        replay_frame_inspect(
            frame_id=arguments.frame_id,
            replay_run_id=arguments.replay_run_id,
            session_date=arguments.session_date,
        )
        return
    if arguments.command == "replay-evaluation-list":
        replay_evaluation_list(replay_run_id=arguments.replay_run_id, limit=arguments.limit)
        return
    if arguments.command == "historical-research-summary":
        historical_research_summary(replay_run_id=arguments.replay_run_id)
        return
    if arguments.command == "replay-experiment-construct":
        replay_experiment_construct(
            replay_run_id=arguments.replay_run_id,
            code_revision=arguments.code_revision,
            actor=arguments.actor,
        )
        return
    if arguments.command == "research-policy-create":
        research_policy_create(
            hypothesis_version_id=arguments.hypothesis_version_id,
            dataset_snapshot_id=arguments.dataset_snapshot_id,
            replay_run_id=arguments.replay_run_id,
            intent=arguments.intent,
            train_start=arguments.train_start,
            train_end=arguments.train_end,
            validation_start=arguments.validation_start,
            validation_end=arguments.validation_end,
            holdout_start=arguments.holdout_start,
            holdout_end=arguments.holdout_end,
            holdout_locked=arguments.holdout_locked,
            minimum_sample_count=arguments.minimum_sample_count,
            revision=arguments.revision,
            supersedes_governance_policy_id=arguments.supersedes_governance_policy_id,
        )
        return
    if arguments.command == "research-policy-inspect":
        research_policy_inspect(governance_policy_id=arguments.governance_policy_id)
        return
    if arguments.command == "research-holdout-expose":
        research_holdout_expose(
            governance_policy_id=arguments.governance_policy_id,
            actor=arguments.actor,
            purpose=arguments.purpose,
            consumed_trial_id=arguments.consumed_trial_id,
        )
        return
    if arguments.command == "research-trial-create":
        research_trial_create(
            hypothesis_version_id=arguments.hypothesis_version_id,
            dataset_snapshot_id=arguments.dataset_snapshot_id,
            replay_run_id=arguments.replay_run_id,
            phase=arguments.phase,
            market_scope=arguments.market_scope,
            method_id=arguments.method_id,
            method_version=arguments.method_version,
            parameter_name=arguments.parameter_name,
            parameter_value=arguments.parameter_value,
            configuration_origin=arguments.configuration_origin,
            candidates_examined_count=arguments.candidates_examined_count,
            session_date_start=arguments.session_date_start,
            session_date_end=arguments.session_date_end,
            benchmark_kind=arguments.benchmark_kind,
            governance_policy_id=arguments.governance_policy_id,
            outcome_horizon_sessions=arguments.outcome_horizon_sessions,
            bootstrap_seed=arguments.bootstrap_seed,
            hac_lag=arguments.hac_lag,
        )
        return
    if arguments.command == "research-trial-inspect":
        research_trial_inspect(trial_id=arguments.trial_id)
        return
    if arguments.command == "research-trial-family-create":
        research_trial_family_create(
            hypothesis_id=arguments.hypothesis_id,
            trial_ids=arguments.trial_ids,
            candidate_count=arguments.candidate_count,
            asset_count=arguments.asset_count,
            outcome_definition_count=arguments.outcome_definition_count,
            regime_slice_count=arguments.regime_slice_count,
            selection_method=arguments.selection_method,
            correction_method=arguments.correction_method,
        )
        return
    if arguments.command == "research-trial-family-inspect":
        research_trial_family_inspect(trial_family_id=arguments.trial_family_id)
        return
    if arguments.command == "walk-forward-create":
        walk_forward_create(
            replay_run_id=arguments.replay_run_id,
            method_id=arguments.method_id,
            outcome_metric_id=arguments.outcome_metric_id,
            training_window_sessions=arguments.training_window_sessions,
            validation_window_sessions=arguments.validation_window_sessions,
            test_window_sessions=arguments.test_window_sessions,
            step_sessions=arguments.step_sessions,
            window_mode=arguments.window_mode,
            minimum_samples=arguments.minimum_samples,
            cost_information_state=arguments.cost_information_state,
            start_date=arguments.start_date,
            end_date=arguments.end_date,
            hypothesis_version_id=arguments.hypothesis_version_id,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command in ("walk-forward-run-start", "walk-forward-run-resume"):
        (
            walk_forward_run_start
            if arguments.command == "walk-forward-run-start"
            else walk_forward_run_resume
        )(
            walk_forward_run_id=arguments.walk_forward_run_id,
            maximum_folds=arguments.maximum_folds,
            market_scope=arguments.market_scope,
            method_id=arguments.method_id,
            method_version=arguments.method_version,
            parameter_name=arguments.parameter_name,
            parameter_value=arguments.parameter_value,
            configuration_origin=arguments.configuration_origin,
            candidates_examined_count=arguments.candidates_examined_count,
            benchmark_kind=arguments.benchmark_kind,
        )
        return
    if arguments.command == "walk-forward-inspect":
        walk_forward_inspect(walk_forward_run_id=arguments.walk_forward_run_id)
        return
    if arguments.command == "robustness-run-create":
        robustness_run_create(
            base_trial_id=arguments.base_trial_id,
            momentum_lookback_windows=arguments.momentum_lookback_windows,
            momentum_positive_threshold=arguments.momentum_positive_threshold,
        )
        return
    if arguments.command == "robustness-run-inspect":
        robustness_run_inspect(robustness_run_id=arguments.robustness_run_id)
        return
    if arguments.command == "research-conclusion-create":
        research_conclusion_create(
            hypothesis_version_id=arguments.hypothesis_version_id,
            dataset_snapshot_id=arguments.dataset_snapshot_id,
            replay_run_id=arguments.replay_run_id,
            training_trial_id=arguments.training_trial_id,
            validation_trial_id=arguments.validation_trial_id,
            holdout_trial_id=arguments.holdout_trial_id,
            walk_forward_run_id=arguments.walk_forward_run_id,
            robustness_run_id=arguments.robustness_run_id,
            trial_family_id=arguments.trial_family_id,
            point_in_time_limitations=arguments.point_in_time_limitations,
            rationale=arguments.rationale,
            sample_size_phase=arguments.sample_size_phase,
        )
        return
    if arguments.command == "research-conclusion-inspect":
        research_conclusion_inspect(conclusion_id=arguments.conclusion_id)
        return
    if arguments.command == "research-conclusion-list":
        research_conclusion_list(hypothesis_version_id=arguments.hypothesis_version_id)
        return
    if arguments.command == "research-audit-inspect":
        research_audit_inspect(hypothesis_version_id=arguments.hypothesis_version_id)
        return
    if arguments.command == "promotion-evaluate":
        promotion_evaluate(
            conclusion_id=arguments.conclusion_id,
            require_holdout_consumed=arguments.require_holdout_consumed,
            lookahead_violation=arguments.lookahead_violation,
        )
        return
    if arguments.command == "promotion-evaluate-production":
        promotion_evaluate_production(
            conclusion_id=arguments.conclusion_id,
            lookahead_violation=arguments.lookahead_violation,
        )
        return
    if arguments.command == "securities-forward-plan-create":
        securities_forward_plan_create(
            provider=arguments.provider,
            universe_id=arguments.universe_id,
            feature_schema_id=arguments.feature_schema_id,
            prediction_horizon_sessions=arguments.prediction_horizon_sessions,
            reason=arguments.reason,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-forward-capture":
        securities_forward_capture(plan_id=arguments.plan_id)
        return
    if arguments.command == "securities-shadow-plan-create":
        securities_shadow_plan_create(idempotency_key=arguments.idempotency_key)
        return
    if arguments.command == "securities-shadow-plan-v2-create":
        securities_shadow_plan_v2_create(
            parent_plan_id=arguments.parent_plan_id, idempotency_key=arguments.idempotency_key
        )
        return
    if arguments.command == "securities-shadow-plan-activate":
        securities_shadow_plan_activate(
            plan_id=arguments.plan_id, confirm_plan_id=arguments.confirm_plan_id
        )
        return
    if arguments.command == "securities-shadow-plan-pause":
        securities_shadow_plan_pause(plan_id=arguments.plan_id, reason=arguments.reason)
        return
    if arguments.command == "securities-shadow-plan-resume":
        securities_shadow_plan_resume(
            plan_id=arguments.plan_id, confirm_plan_id=arguments.confirm_plan_id
        )
        return
    if arguments.command == "securities-shadow-plan-revoke":
        securities_shadow_plan_revoke(
            plan_id=arguments.plan_id, confirm_plan_id=arguments.confirm_plan_id
        )
        return
    if arguments.command == "securities-shadow-worker":
        securities_shadow_worker(plan_id=arguments.plan_id)
        return
    if arguments.command == "securities-shadow-daily":
        securities_shadow_daily(
            plan_id=arguments.plan_id,
            no_provider=arguments.no_provider,
            keep_db_running=arguments.keep_db_running,
            as_json=arguments.json,
        )
        return
    if arguments.command == "securities-shadow-scheduler-start":
        securities_shadow_scheduler_start(plan_id=arguments.plan_id)
        return
    if arguments.command == "securities-shadow-scheduler-loop":
        securities_shadow_scheduler_loop(plan_id=arguments.plan_id)
        return
    if arguments.command == "securities-shadow-scheduler-run-due":
        securities_shadow_scheduler_run_due(plan_id=arguments.plan_id)
        return
    if arguments.command == "securities-shadow-scheduler-stop":
        securities_shadow_scheduler_stop()
        return
    if arguments.command == "securities-shadow-scheduler-status":
        securities_shadow_scheduler_status()
        return
    if arguments.command == "apex-runtime-up":
        apex_runtime_up(plan_id=arguments.plan_id)
        return
    if arguments.command == "apex-runtime-down":
        apex_runtime_down()
        return
    if arguments.command == "apex-runtime-status":
        apex_runtime_status()
        return
    if arguments.command == "portfolio-intelligence-current":
        portfolio_intelligence_current(
            plan_id=arguments.plan_id, idempotency_key=arguments.idempotency_key
        )
        return
    if arguments.command == "securities-shadow-resolve":
        securities_shadow_resolve(plan_id=arguments.plan_id, cutoff=arguments.cutoff)
        return
    if arguments.command in {"securities-shadow-status", "securities-shadow-report"}:
        securities_shadow_status(plan_id=arguments.plan_id)
        return
    if arguments.command == "securities-research":
        securities_research(
            campaign_id=arguments.campaign_id,
            strategy_id=arguments.strategy_id,
            data_batch_id=arguments.data_batch_id,
            start=arguments.start,
            end=arguments.end,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-strategy-lab-research":
        securities_strategy_lab_research(
            campaign_id=arguments.campaign_id,
            deployment_id=arguments.deployment_id,
            data_batch_id=arguments.data_batch_id,
            start=arguments.start,
            end=arguments.end,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-conformance":
        securities_conformance(
            research_run_id=arguments.research_run_id,
            external_engine=arguments.external_engine,
        )
        return
    if arguments.command == "securities-status":
        securities_status(campaign_id=arguments.campaign_id)
        return
    if arguments.command == "securities-paper-run":
        securities_paper_run(
            campaign_id=arguments.campaign_id,
            trial_id=arguments.trial_id,
            strategy_id=arguments.strategy_id,
            data_batch_id=arguments.data_batch_id,
            start=arguments.start,
            end=arguments.end,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-mark":
        securities_mark(
            campaign_id=arguments.campaign_id,
            as_of=arguments.as_of,
        )
        return
    if arguments.command == "securities-exit":
        securities_exit(
            campaign_id=arguments.campaign_id,
            as_of=arguments.as_of,
            reason=arguments.reason,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-apply-actions":
        securities_apply_actions(
            campaign_id=arguments.campaign_id,
            as_of=arguments.as_of,
        )
        return
    if arguments.command == "securities-paper-status":
        securities_paper_status(campaign_id=arguments.campaign_id)
        return
    if arguments.command == "securities-paper-conformance":
        securities_paper_conformance(
            paper_run_id=arguments.paper_run_id,
            external_engine=arguments.external_engine,
        )
        return
    if arguments.command == "securities-baseline-run":
        securities_baseline_run(
            baseline_id=arguments.baseline_id,
            campaign_id=arguments.campaign_id,
            data_batch_id=arguments.data_batch_id,
            start=arguments.start,
            end=arguments.end,
            simulator=arguments.simulator,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-reconcile-retained-request":
        securities_reconcile_retained_request(
            report_id=arguments.conformance_report_id,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-baseline-suite":
        securities_baseline_suite(
            campaign_id=arguments.campaign_id,
            data_batch_id=arguments.data_batch_id,
            start=arguments.start,
            end=arguments.end,
            simulator_names=arguments.simulators,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "securities-multi-simulator-conformance":
        securities_multi_simulator_conformance(
            baseline_id=arguments.baseline_id,
            data_batch_id=arguments.data_batch_id,
            start=arguments.start,
            end=arguments.end,
        )
        return
    if arguments.command == "securities-baseline-scorecards":
        securities_baseline_scorecards(data_batch_id=arguments.data_batch_id)
        return
    if arguments.command == "research-experiment-create":
        research_experiment_create(
            name=arguments.name,
            vertical=arguments.vertical,
            data_batch_id=arguments.data_batch_id,
            universe_id=arguments.universe_id,
            start=arguments.start,
            end=arguments.end,
            maximum_trials=arguments.maximum_trials,
            specification=arguments.specification,
            reason=arguments.reason,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "research-overfitting-evaluate":
        research_overfitting_evaluate(
            experiment_id=arguments.experiment_id,
            cutoff=arguments.cutoff,
        )
        return
    if arguments.command == "research-overfitting-report":
        research_overfitting_report(experiment_id=arguments.experiment_id)
        return
    if arguments.command == "research-trials":
        research_trials(experiment_id=arguments.experiment_id)
        return
    if arguments.command == "candidate-search-create":
        candidate_search_create(
            experiment_id=arguments.experiment_id,
            name=arguments.name,
            dataset_id=arguments.dataset_id,
            feature_schema_id=arguments.feature_schema_id,
            specification=arguments.specification,
            objective=arguments.objective,
            maximum_trials=arguments.maximum_trials,
            seed=arguments.seed,
            reason=arguments.reason,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "candidate-search-run":
        candidate_search_run(
            search_plan_id=arguments.search_plan_id,
            requested_trials=arguments.requested_trials,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "candidate-search-recover":
        candidate_search_recover(search_run_id=arguments.search_run_id)
        return
    if arguments.command == "candidate-search-cancel":
        candidate_search_cancel(
            search_run_id=arguments.search_run_id,
            reason=arguments.reason,
        )
        return
    if arguments.command == "candidate-search-status":
        candidate_search_status(search_run_id=arguments.search_run_id)
        return
    if arguments.command == "candidate-search-report":
        candidate_search_report(search_run_id=arguments.search_run_id)
        return
    if arguments.command == "mlflow-reconcile":
        mlflow_reconcile(experiment_id=arguments.experiment_id)
        return
    if arguments.command == "batch-learning-plan-create":
        batch_learning_plan_create(
            name=arguments.name,
            source_scope=arguments.source_scope,
            feature_schema_id=arguments.feature_schema_id,
            search_template=arguments.search_template,
            minimum_new_groups=arguments.minimum_new_groups,
            minimum_new_observations=arguments.minimum_new_observations,
            maximum_trials_per_cycle=arguments.maximum_trials_per_cycle,
            maximum_cycles=arguments.maximum_cycles,
            reason=arguments.reason,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "batch-learning-plan-activate":
        batch_learning_plan_control(
            plan_id=arguments.plan_id,
            action="activate",
            reason=arguments.reason,
            confirm_plan_id=arguments.confirm_plan_id,
            confirm_plan_hash=arguments.confirm_plan_hash,
        )
        return
    if arguments.command in {
        "batch-learning-plan-pause",
        "batch-learning-plan-resume",
        "batch-learning-plan-revoke",
    }:
        batch_learning_plan_control(
            plan_id=arguments.plan_id,
            action=arguments.command.removeprefix("batch-learning-plan-"),
            reason=arguments.reason,
        )
        return
    if arguments.command == "batch-learning-trigger":
        batch_learning_trigger(plan_id=arguments.plan_id, idempotency_key=arguments.idempotency_key)
        return
    if arguments.command == "batch-learning-cycle-run":
        batch_learning_cycle_run(
            plan_id=arguments.plan_id, idempotency_key=arguments.idempotency_key
        )
        return
    if arguments.command == "batch-learning-cycle-recover":
        batch_learning_cycle_recover(cycle_id=arguments.cycle_id)
        return
    if arguments.command == "batch-learning-cycle-cancel":
        batch_learning_cycle_cancel(cycle_id=arguments.cycle_id, reason=arguments.reason)
        return
    if arguments.command == "batch-learning-attach-challenger":
        batch_learning_attach_challenger(
            cycle_id=arguments.cycle_id,
            deployment_id=arguments.deployment_id,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "batch-learning-forward-infer":
        batch_learning_forward_infer(window_id=arguments.window_id, cutoff=arguments.cutoff)
        return
    if arguments.command == "batch-learning-forward-evaluate":
        batch_learning_forward_evaluate(window_id=arguments.window_id, cutoff=arguments.cutoff)
        return
    if arguments.command == "batch-learning-status":
        batch_learning_status(plan_id=arguments.plan_id)
        return
    if arguments.command == "batch-learning-report":
        batch_learning_report(cycle_id=arguments.cycle_id)
        return
    if arguments.command == "batch-learning-worker":
        batch_learning_worker(plan_id=arguments.plan_id)
        return
    if arguments.command in {
        "strategy-evidence-snapshot",
        "request-strategy-review",
        "strategy-review-status",
        "approve-paper-trial",
    }:
        forwarded = [arguments.command]
        for name, value in vars(arguments).items():
            if name == "command" or value is None:
                continue
            flag = "--" + name.replace("_", "-")
            forwarded.extend((flag, str(value)))
        _run((_python(), "-m", "apex.cli", *forwarded))
        return
    if arguments.command == "demo-core":
        demo_core(arguments.fixture, arguments.capital, arguments.seed)
        return
    if arguments.command == "autopilot-strategies":
        autopilot_strategies()
        return
    if arguments.command == "autopilot-status":
        autopilot_status(campaign_id=arguments.campaign_id)
        return
    if arguments.command == "autopilot-cycle":
        autopilot_cycle(
            campaign_id=arguments.campaign_id,
            mode=arguments.mode,
            idempotency_key=arguments.idempotency_key,
            maximum_strategies=arguments.maximum_strategies,
            maximum_proposals=arguments.maximum_proposals,
            deadline_seconds=arguments.deadline_seconds,
            operator_note=arguments.operator_note,
        )
        return
    if arguments.command in ("autopilot-pause", "autopilot-resume", "autopilot-kill"):
        autopilot_control(
            campaign_id=arguments.campaign_id,
            action=arguments.command.removeprefix("autopilot-"),
            reason=arguments.reason,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "autopilot-core-demo":
        autopilot_core_demo()
        return
    if arguments.command == "paper-portfolio-demo":
        paper_portfolio_demo()
        return
    if arguments.command == "strategy-shadow-demo":
        strategy_shadow_demo()
        return
    if arguments.command == "orderbook-microstructure-shadow-demo":
        orderbook_microstructure_shadow_demo()
        return
    if arguments.command == "market-dynamics-shadow-demo":
        market_dynamics_shadow_demo()
        return
    if arguments.command == "multi-strategy-shadow-cycle":
        multi_strategy_shadow_cycle(
            campaign_id=arguments.campaign_id,
            idempotency_key=arguments.idempotency_key,
            market_limit=arguments.market_limit,
            maximum_predictions=arguments.maximum_predictions,
            maximum_opportunities=arguments.maximum_opportunities,
        )
        return
    if arguments.command == "multi-strategy-shadow-session":
        multi_strategy_shadow_session(
            campaign_id=arguments.campaign_id,
            iterations=arguments.iterations,
            interval_seconds=arguments.interval_seconds,
            market_limit=arguments.market_limit,
            maximum_pages=arguments.max_pages,
            maximum_enrichments=arguments.max_enrichments,
            orderbook_depth=arguments.orderbook_depth,
        )
        return
    if arguments.command == "reconcile-multi-strategy-shadow-session":
        reconcile_multi_strategy_shadow_session(session_id=arguments.session_id)
        return
    if arguments.command == "multi-strategy-shadow-demo":
        multi_strategy_shadow_demo()
        return
    if arguments.command == "create-shadow-plan":
        create_shadow_plan(
            campaign_id=arguments.campaign_id,
            name=arguments.name,
            iterations=arguments.iterations,
            interval_seconds=arguments.interval_seconds,
            market_count=arguments.market_count,
            maximum_pages=arguments.max_pages,
            maximum_enrichments=arguments.max_enrichments,
            maximum_markets_per_event=arguments.max_markets_per_event,
            maximum_markets_per_series=arguments.max_markets_per_series,
            orderbook_depth=arguments.orderbook_depth,
            idempotency_key=arguments.idempotency_key,
            market_tickers=arguments.market_tickers,
        )
        return
    if arguments.command == "discover-create-shadow-plan":
        discover_create_shadow_plan(
            campaign_id=arguments.campaign_id,
            name=arguments.name,
            window_start=arguments.window_start,
            window_end=arguments.window_end,
            iterations=arguments.iterations,
            interval_seconds=arguments.interval_seconds,
            target_markets=arguments.target_markets,
            minimum_markets=arguments.minimum_markets,
            minimum_events=arguments.minimum_events,
            maximum_pages=arguments.max_pages,
            maximum_inspected_markets=arguments.max_inspected_markets,
            maximum_enrichments=arguments.max_enrichments,
            maximum_markets_per_event=arguments.max_markets_per_event,
            maximum_markets_per_series=arguments.max_markets_per_series,
            orderbook_depth=arguments.orderbook_depth,
            idempotency_key=arguments.idempotency_key,
            maximum_orderbook_preflights=arguments.max_orderbook_preflights,
            microstructure_configuration_version=arguments.microstructure_configuration_version,
            prior_plan_ids=arguments.exclude_plan_id,
            exclude_prior_plan_markets=arguments.exclude_prior_plan_markets,
            exclude_prior_plan_events=arguments.exclude_prior_plan_events,
            exclude_active_plan_markets=arguments.exclude_active_plan_markets,
        )
        return
    if arguments.command == "run-shadow-plan":
        run_shadow_plan(plan_id=arguments.plan_id)
        return
    if arguments.command == "shadow-worker":
        shadow_worker(
            plan_id=arguments.plan_id,
            once=arguments.once,
            poll_seconds=arguments.poll_seconds,
            maximum_plans=arguments.maximum_plans,
        )
        return
    if arguments.command == "collect-shadow-outcomes":
        collect_shadow_outcomes(
            campaign_id=arguments.campaign_id,
            maximum_markets=arguments.market_limit,
        )
        return
    if arguments.command == "strategy-performance":
        strategy_performance(
            campaign_id=arguments.campaign_id,
            strategy_id=arguments.strategy_id,
            plan_id=arguments.plan_id,
        )
        return
    if arguments.command == "shadow-operations-demo":
        shadow_operations_demo()
        return
    if arguments.command == "create-shadow-campaign":
        create_shadow_campaign(
            name=arguments.name,
            capital=arguments.capital,
            maximum_loss=arguments.maximum_loss,
            expires_in_days=arguments.expires_in_days,
            idempotency_key=arguments.idempotency_key,
        )
        return
    if arguments.command == "reconcile-local-strategy-governance":
        reconcile_local_strategy_governance(
            database=arguments.database,
            apply=arguments.apply,
            confirmation=arguments.confirmation,
        )
        return
    if arguments.command == "paper-portfolio-status":
        paper_portfolio_status(campaign_id=arguments.campaign_id)
        return
    if arguments.command == "paper-mark-positions":
        paper_mark_positions(
            campaign_id=arguments.campaign_id,
            orderbook_run_id=arguments.orderbook_run_id,
        )
        return
    if arguments.command == "paper-settle-position":
        paper_settle_position(
            position_id=arguments.position_id,
            settlement_fact_id=arguments.settlement_fact_id,
        )
        return
    if arguments.command == "live-shadow-session":
        live_shadow_session(
            campaign_id=arguments.campaign_id,
            strategy_id=arguments.strategy_id,
            iterations=arguments.iterations,
            interval_seconds=arguments.interval_seconds,
            market_limit=arguments.market_limit,
            maximum_pages=arguments.maximum_pages,
            maximum_enrichments=arguments.maximum_enrichments,
            observations_per_market=arguments.observations_per_market,
            orderbook_depth=arguments.orderbook_depth,
        )
        return
    if arguments.command == "scan-kalshi-fixture":
        scan_kalshi_fixture(persist=arguments.persist)
        return
    if arguments.command == "smoke-kalshi-public":
        smoke_kalshi_public(
            limit=arguments.limit,
            maximum_pages=arguments.max_pages,
            timeout_seconds=arguments.timeout_seconds,
            persist=arguments.persist,
        )
        return
    if arguments.command == "scan-kalshi-consistency-fixture":
        scan_kalshi_consistency_fixture(persist=arguments.persist)
        return
    if arguments.command == "smoke-kalshi-consistency":
        smoke_kalshi_consistency(
            event_limit=arguments.event_limit,
            market_limit=arguments.market_limit,
            maximum_pages=arguments.max_pages,
            timeout_seconds=arguments.timeout_seconds,
            persist=arguments.persist,
        )
        return
    if arguments.command == "compare-kalshi-scans-fixture":
        compare_kalshi_scans_fixture()
        return
    if arguments.command == "compare-kalshi-scans":
        compare_kalshi_scans(
            baseline_run_id=arguments.baseline_run_id,
            current_run_id=arguments.current_run_id,
        )
        return
    if arguments.command == "compare-kalshi-latest-scans":
        compare_kalshi_latest_scans()
        return
    if arguments.command == "summarize-kalshi-event-history-fixture":
        summarize_kalshi_event_history_fixture()
        return
    if arguments.command == "summarize-kalshi-event-history":
        summarize_kalshi_event_history(
            event_ticker=arguments.event_ticker,
            maximum_completed_runs=arguments.max_runs,
            first_ingestion_run_id=arguments.first_run_id,
            last_ingestion_run_id=arguments.last_run_id,
            continuity_gap_seconds=arguments.continuity_gap_seconds,
        )
        return
    if arguments.command == "summarize-kalshi-orderbook-history-fixture":
        summarize_kalshi_orderbook_history_fixture()
        return
    if arguments.command == "summarize-kalshi-orderbook-history":
        summarize_kalshi_orderbook_history(
            market_ticker=arguments.market_ticker,
            maximum_observations=arguments.max_observations,
            first_orderbook_snapshot_id=arguments.first_snapshot_id,
            last_orderbook_snapshot_id=arguments.last_snapshot_id,
            continuity_gap_seconds=arguments.continuity_gap_seconds,
        )
        return
    if arguments.command == "scan-kalshi-binary-complements-fixture":
        scan_kalshi_binary_complements_fixture()
        return
    if arguments.command == "smoke-kalshi-binary-complements":
        smoke_kalshi_binary_complements(
            market_limit=arguments.market_limit,
            depth=arguments.depth,
            maximum_bundle_contracts=arguments.max_bundle_contracts,
            timeout_seconds=arguments.timeout_seconds,
            discover_first=arguments.discover_first,
        )
        return
    if arguments.command == "capture-kalshi-orderbook-series-fixture":
        capture_kalshi_orderbook_series_fixture()
        return
    if arguments.command == "capture-kalshi-orderbook-series":
        capture_kalshi_orderbook_series(
            market_ticker=arguments.market_ticker,
            latest_open_market=arguments.latest_open_market,
            discover_first=arguments.discover_first,
            observations=arguments.observations,
            interval_seconds=arguments.interval_seconds,
            depth=arguments.depth,
            maximum_bundle_contracts=arguments.max_bundle_contracts,
            timeout_seconds=arguments.timeout_seconds,
        )
        return
    if arguments.command == "sweep-kalshi-orderbooks-fixture":
        sweep_kalshi_orderbooks_fixture()
        return
    if arguments.command == "sweep-kalshi-orderbooks":
        sweep_kalshi_orderbooks(
            market_count=arguments.market_count,
            maximum_pages=arguments.max_pages,
            market_limit=arguments.market_limit,
            maximum_enrichments=arguments.max_enrichments,
            observations=arguments.observations,
            interval_seconds=arguments.interval_seconds,
            depth=arguments.depth,
            maximum_bundle_contracts=arguments.max_bundle_contracts,
            timeout_seconds=arguments.timeout_seconds,
        )
        return
    if arguments.command == "scan-kalshi-threshold-ladders-fixture":
        scan_kalshi_threshold_ladders_fixture()
        return
    if arguments.command == "scan-kalshi-threshold-ladders":
        scan_kalshi_threshold_ladders(
            scan_id=arguments.scan_id,
            event_count=arguments.event_count,
            maximum_pages=arguments.max_pages,
            market_limit=arguments.market_limit,
            maximum_enrichments=arguments.max_enrichments,
            maximum_markets_per_event=arguments.max_markets_per_event,
            maximum_relationships=arguments.max_relationships,
            rounds=arguments.rounds,
            interval_seconds=arguments.interval_seconds,
            depth=arguments.depth,
            maximum_bundle_contracts=arguments.max_bundle_contracts,
            maximum_observation_skew_seconds=arguments.max_observation_skew_seconds,
            timeout_seconds=arguments.timeout_seconds,
        )
        return
    if arguments.command == "diagnose-kalshi-capture-selection":
        diagnose_kalshi_capture_selection(
            max_pages=arguments.max_pages,
            market_limit=arguments.market_limit,
            timeout_seconds=arguments.timeout_seconds,
        )
        return
    if arguments.command == "reconcile-kalshi-capture-sessions":
        reconcile_kalshi_capture_sessions()
        return
    if arguments.command == "research-gfs-archive-feasibility-fixture":
        research_gfs_archive_feasibility_fixture()
        return
    if arguments.command == "research-gfs-archive-feasibility":
        research_gfs_archive_feasibility(
            series=arguments.series,
            sample_dates=arguments.sample_dates,
            lead_hours=arguments.lead_hours,
            maximum_requests=arguments.max_requests,
            maximum_download_mb=arguments.max_download_mb,
            timeout_seconds=arguments.timeout_seconds,
        )
        return
    if arguments.command == "research-gfs-weather-edge-fixture":
        research_gfs_weather_edge_fixture()
        return
    if arguments.command == "research-gfs-weather-edge":
        research_gfs_weather_edge(
            series=arguments.series,
            start_date=arguments.start_date,
            end_date=arguments.end_date,
            lead_hours=arguments.lead_hours,
            max_event_dates=arguments.max_event_dates,
            bootstrap_repetitions=arguments.bootstrap_repetitions,
            max_noaa_requests=arguments.max_noaa_requests,
            max_download_mb=arguments.max_download_mb,
            timeout_seconds=arguments.timeout_seconds,
        )
        return
    actions[arguments.command]()


if __name__ == "__main__":
    main()
