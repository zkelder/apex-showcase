"""Manual, local-only benchmark for the Live Readiness / Live Preflight reads.

Not part of ``verify-core``/``verify-persistence``/pytest -- this is a
reproducible measurement tool for the Operator Console Live Readiness
Performance v1 fix, kept as the "reproducible benchmark command" a future
session can run directly against the real retained ``apex`` database (never
``apex_test``) to reconfirm both routes' real-scale latency, mirroring
``benchmark_home_read_model.py``'s/``benchmark_followups_read_model.py``'s
own role for the two sibling performance fixes.

Measures both:
  * ``GET /api/v1/operator-console/live-readiness``
  * ``GET /api/v1/operator-console/live-preflight``

Like ``benchmark_home_read_model.py`` (and unlike the narrower
``benchmark_followups_read_model.py``), this drives the real, unmodified
routes through a genuine ASGI request/response cycle -- via
``apex.operator_api.create_app()`` and a minimal, stdlib-only raw ASGI call
(no test-client dependency is installed in this environment).
``/live-readiness`` alone composes five distinct subsystems (live readiness,
operational reliability, Coinbase/Kalshi private read-only, live preflight);
hand-copying that assembly would itself become an unmaintained second
implementation liable to drift from the real route. Going through the real
FastAPI route dispatch instead means this benchmark can never silently
diverge from what the endpoints actually do.

Read-only: makes no provider request (zero credentials are read from the
environment by anything on this path) and performs no write -- every
repository call reachable from these two routes is a plain read. Connects
using the same env-driven ``PostgresSettings`` every other read-only tool in
this repository uses, and refuses to run against anything except whatever
database ``APEX_POSTGRES_DB`` (default ``apex``) actually names.

Usage (against the real retained database, from the repository root):

    PYTHONPATH=src python3 scripts/benchmark_live_readiness_read_model.py

Optional: pass a wall-clock bound in seconds (default 150) as the sole
argument -- the script aborts and reports whatever it collected so far
rather than ever running unbounded:

    PYTHONPATH=src python3 scripts/benchmark_live_readiness_read_model.py 200

What it measures, per route:
  - wall-clock duration
  - total SQL statements issued, broken down by table
  - call counts for the repository methods this milestone's investigation
    identified as relevant:
      * ``PostgresLivePreflightRepository.intents()``/``invalidations()``
        (the confirmed defect: an intents() x invalidations() N+1 where each
        call was an unfiltered full-table scan of ``live_intent_invalidation``)
      * ``PostgresOperationalReliabilityRepository.latest_runner_heartbeat()``
        (bounded follow-on audit finding: fetches every retained heartbeat
        for the runner id to take the last one)
      * ``PostgresLiveReadinessRepository.latest_capability()`` (previously
        flagged, low-priority: an unfiltered full-table scan of
        ``provider_account_capability_snapshot``, called once per Provider)
  - the resulting HTTP status and response byte length, as a correctness
    sanity check alongside the timing
"""

from __future__ import annotations

import asyncio
import resource
import signal
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.live_preflight_postgres import PostgresLivePreflightRepository
from apex.adapters.live_readiness_postgres import PostgresLiveReadinessRepository
from apex.adapters.operational_reliability_postgres import (
    PostgresOperationalReliabilityRepository,
)
from apex.adapters.postgres import PostgresSettings, create_postgres_engine

_BOUND_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 150

_call_counts: Counter[str] = Counter()
_sql_statement_count = 0
_sql_by_table: Counter[str] = Counter()
_KNOWN_TABLES = (
    "live_intent_proposal",
    "live_intent_invalidation",
    "live_intent_eligibility_decision",
    "live_internal_preflight",
    "provider_preview_approval",
    "provider_preview_capture",
    "provider_preview_attempt",
    "live_preflight_assessment",
    "provider_account_capability_snapshot",
    "provider_credential_reference",
    "live_risk_policy",
    "execution_control_event",
    "backup_readiness_record",
    "live_readiness_assessment",
    "operational_runner_heartbeat",
    "operational_deployment_profile",
    "operational_backup_manifest",
    "operational_restore_verification",
    "operational_recovery_attempt",
    "operational_alert_delivery",
    "operational_health_projection",
    "operational_kill_switch_drill",
    "coinbase_read_only_capability_capture",
    "coinbase_read_only_capability_refresh",
    "kalshi_read_only_capability_capture",
    "kalshi_read_only_capability_refresh",
)


def _count_calls(cls: type, method_name: str, label: str) -> None:
    original = getattr(cls, method_name)

    def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _call_counts[label] += 1
        return original(self, *args, **kwargs)

    setattr(cls, method_name, wrapper)


_count_calls(PostgresLivePreflightRepository, "intents", "live_preflight.intents()")
_count_calls(
    PostgresLivePreflightRepository, "invalidations", "live_preflight.invalidations(id) [N+1 if >1]"
)
_count_calls(
    PostgresOperationalReliabilityRepository,
    "latest_runner_heartbeat",
    "operational.latest_runner_heartbeat()",
)
_count_calls(
    PostgresLiveReadinessRepository, "latest_capability", "live_readiness.latest_capability() [x3]"
)


def _sql_listener(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
    global _sql_statement_count
    _sql_statement_count += 1
    lowered = statement.strip().lower()
    for table in _KNOWN_TABLES:
        if table in lowered:
            _sql_by_table[table] += 1
            return
    _sql_by_table["OTHER"] += 1


class _BoundExceeded(Exception):
    pass


def _on_alarm(signum, frame):  # type: ignore[no-untyped-def]
    raise _BoundExceeded(f"exceeded the {_BOUND_SECONDS}s bound")


async def _call_asgi(app, path: str) -> tuple[int, bytes]:
    """Minimal, stdlib-only raw ASGI GET -- a genuine ASGI request/response
    cycle through the real route function (no test-client dependency is
    installed in this environment; ``create_app()`` adds no middleware, so
    a bare GET scope is sufficient)."""

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
        "scheme": "http",
        "state": {},
    }
    body_sent = False

    async def receive():  # type: ignore[no-untyped-def]
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    status_holder: list[int] = []
    body_parts: list[bytes] = []

    async def send(message):  # type: ignore[no-untyped-def]
        if message["type"] == "http.response.start":
            status_holder.append(message["status"])
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    await app(scope, receive, send)
    return (status_holder[0] if status_holder else -1), b"".join(body_parts)


def _measure(app, path: str) -> None:
    global _sql_statement_count
    _sql_statement_count = 0
    _sql_by_table.clear()
    _call_counts.clear()

    rusage_before = resource.getrusage(resource.RUSAGE_SELF)
    wall_before = time.perf_counter()
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(_BOUND_SECONDS)
    status = None
    body = b""
    outcome = "COMPLETED"
    try:
        status, body = asyncio.run(_call_asgi(app, path))
    except _BoundExceeded as exc:
        outcome = f"ABORTED: {exc}"
    finally:
        signal.alarm(0)
    wall_after = time.perf_counter()
    rusage_after = resource.getrusage(resource.RUSAGE_SELF)

    print("=" * 78)
    print(f"route: GET {path}")
    print(f"outcome: {outcome}")
    print(f"wall_clock_seconds: {wall_after - wall_before:.4f}")
    cpu_seconds = (rusage_after.ru_utime - rusage_before.ru_utime) + (
        rusage_after.ru_stime - rusage_before.ru_stime
    )
    print(f"cpu_seconds: {cpu_seconds:.4f}")
    print(f"rss_delta_kb: {rusage_after.ru_maxrss - rusage_before.ru_maxrss}")
    print(f"http_status: {status}")
    print(f"response_body_bytes: {len(body)}")
    print(f"total_sql_statements: {_sql_statement_count}")
    for table, count in _sql_by_table.most_common():
        print(f"  sql[{table}]: {count}")
    print("-- repository call counts --")
    for label, count in _call_counts.most_common():
        print(f"  calls[{label}]: {count}")


def main() -> None:
    settings = PostgresSettings.from_env()
    print(f"connecting read-only to {settings.host}:{settings.port}/{settings.database}")
    if settings.database == settings.test_database:
        print("refusing to benchmark against a *_test database -- use the real retained database")
        raise SystemExit(1)

    # Sanity ping before constructing the (much heavier) FastAPI app.
    probe_engine: Engine = create_postgres_engine(settings)
    with probe_engine.connect() as connection:
        connection.execute(sa.text("SELECT 1"))
    probe_engine.dispose()

    # Attach at the Engine *class* level so it applies no matter which
    # engine instance create_app() constructs internally.
    sa.event.listen(Engine, "before_cursor_execute", _sql_listener)

    from apex.operator_api import create_app

    print("constructing app (real create_app(), zero re-implementation)...")
    app = create_app()

    # Cold call for each route (first hit in this process), then two warm
    # repeats each so cold-vs-warm variance is visible without needing a
    # separate subprocess per call.
    for path in (
        "/api/v1/operator-console/live-readiness",
        "/api/v1/operator-console/live-preflight",
    ):
        print("#" * 78)
        print(f"COLD: {path}")
        _measure(app, path)
        for repeat in range(2):
            print(f"WARM[{repeat}]: {path}")
            _measure(app, path)

    # Also fetch a few retained-corpus size facts for context, using the
    # same probe-style short-lived connection.
    with create_postgres_engine(settings).connect() as connection:
        for label, table in (
            ("live_intent_proposal", "live_intent_proposal"),
            ("live_intent_invalidation", "live_intent_invalidation"),
            ("operational_runner_heartbeat", "operational_runner_heartbeat"),
            ("provider_account_capability_snapshot", "provider_account_capability_snapshot"),
        ):
            count = connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar()
            print(f"corpus[{label}]: {count}")


if __name__ == "__main__":
    main()
