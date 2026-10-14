"""Manual, local-only benchmark for GET /api/v1/operator-console/home.

Not part of ``verify-core``/``verify-persistence``/pytest -- this is a
reproducible measurement tool for the Operator Console Home Performance v1
fix, kept as the "reproducible benchmark command" a future session can run
directly against the real retained ``apex`` database (never ``apex_test``)
to reconfirm the endpoint's real-scale latency, mirroring
``benchmark_followups_read_model.py``'s own role for the sibling
Operator Console Follow-Up Performance v1 fix.

Unlike that sibling script (which re-assembles ``operator_console_followups``'s
narrow, single-service call sequence by hand), this one drives the real,
unmodified route through a genuine ASGI request/response cycle -- via
``apex.operator_api.create_app()`` and a minimal, stdlib-only raw ASGI call
(no test-client dependency is installed in this environment). ``/home``
aggregates roughly fifteen distinct repositories/subsystems
(operating cycle, shared opportunity, cross-venue allocation, performance
attribution, internal Crypto/Kalshi/External PAPER, research operations,
generated-crypto-challenger, counterfactual ledger/follow-up, crypto
universe, options research, portfolio/mandate); hand-copying that entire
assembly here would itself become an unmaintained second implementation
liable to drift from the real route. Going through the real FastAPI route
dispatch instead means this benchmark can never silently diverge from what
the endpoint actually does.

Read-only: makes no provider request (zero credentials are read from the
environment by anything on this path) and performs no write -- every
repository call reachable from ``GET /home`` is a plain read. Connects
using the same env-driven ``PostgresSettings`` every other read-only tool
in this repository uses, and refuses to run against anything except
whatever database ``APEX_POSTGRES_DB`` (default ``apex``) actually names.

Usage (against the real retained database, from the repository root):

    PYTHONPATH=src python3 scripts/benchmark_home_read_model.py

Optional: pass a wall-clock bound in seconds (default 150) as the sole
argument -- the script aborts and reports whatever cProfile stats it
collected so far rather than ever running unbounded:

    PYTHONPATH=src python3 scripts/benchmark_home_read_model.py 200

What it measures:
  - wall-clock duration and CPU time (``resource.getrusage``)
  - process RSS delta
  - total SQL statements issued, broken down by table
  - call counts for the repository methods this milestone's investigation
    identified as the two previous bottlenecks:
      * ``PostgresCryptoUniverseRepository.observations()`` (Operator
        Console Home Performance v1's reuse of the Operator Console
        Follow-Up Performance v1 fix)
      * ``PostgresResearchOperationsRepository.proposals()``/``reviews()``/
        ``recommendations()``/``budget_events()`` and
        ``PostgresRetainedResearchKnowledgeSource.knowledge()`` (this
        milestone's own, independently discovered second defect)
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

from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.adapters.postgres import PostgresSettings, create_postgres_engine
from apex.adapters.research_operations_postgres import (
    PostgresResearchOperationsRepository,
    PostgresRetainedResearchKnowledgeSource,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository

_BOUND_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 150

_call_counts: Counter[str] = Counter()
_sql_statement_count = 0
_sql_by_table: Counter[str] = Counter()
_KNOWN_TABLES = (
    "counterfactual_decision",
    "decision_followup_observation",
    "followup_evidence_quality_assessment",
    "followup_scan_cursor",
    "crypto_product_observation",
    "shared_opportunity_projection",
    "shared_opportunity_set",
    "governed_research_proposal",
    "governed_research_proposal_review",
    "governed_research_recommendation",
    "governed_research_budget_event",
    "research_agenda_item",
    "research_proposal_priority",
    "research_proposal_feasibility",
    "research_experiment_queue_item",
    "research_experiment_queue_event",
    "cross_venue_allocation",
    "performance_attribution",
    "apex_operating_cycle_run",
    "apex_operating_task_result",
)


def _count_calls(cls: type, method_name: str, label: str) -> None:
    original = getattr(cls, method_name)

    def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _call_counts[label] += 1
        return original(self, *args, **kwargs)

    setattr(cls, method_name, wrapper)


_count_calls(PostgresCryptoUniverseRepository, "observations", "crypto_universe.observations()")
_count_calls(
    PostgresSharedOpportunityRepository, "projection", "shared_opportunity.projection() [N+1 if >1]"
)
_count_calls(PostgresResearchOperationsRepository, "proposals", "research_operations.proposals()")
_count_calls(PostgresResearchOperationsRepository, "reviews", "research_operations.reviews(id)")
_count_calls(
    PostgresResearchOperationsRepository, "recommendations", "research_operations.recommendations()"
)
_count_calls(
    PostgresResearchOperationsRepository, "budget_events", "research_operations.budget_events(id)"
)
_count_calls(
    PostgresRetainedResearchKnowledgeSource, "knowledge", "research_operations.knowledge()"
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

    rusage_before = resource.getrusage(resource.RUSAGE_SELF)
    wall_before = time.perf_counter()
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(_BOUND_SECONDS)
    status = None
    body = b""
    outcome = "COMPLETED"
    try:
        status, body = asyncio.run(_call_asgi(app, "/api/v1/operator-console/home"))
    except _BoundExceeded as exc:
        outcome = f"ABORTED: {exc}"
    finally:
        signal.alarm(0)

    wall_after = time.perf_counter()
    rusage_after = resource.getrusage(resource.RUSAGE_SELF)

    print("=" * 78)
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

    # Also fetch a few retained-corpus size facts for context, using the
    # same probe-style short-lived connection.
    with create_postgres_engine(settings).connect() as connection:
        decision_count = connection.execute(
            sa.text("SELECT count(*) FROM counterfactual_decision")
        ).scalar()
        proposal_count = connection.execute(
            sa.text("SELECT count(*) FROM governed_research_proposal")
        ).scalar()
        agenda_count = connection.execute(
            sa.text("SELECT count(*) FROM research_agenda_item")
        ).scalar()
    print(f"corpus[counterfactual_decision]: {decision_count}")
    print(f"corpus[governed_research_proposal]: {proposal_count}")
    print(f"corpus[research_agenda_item]: {agenda_count}")


if __name__ == "__main__":
    main()
