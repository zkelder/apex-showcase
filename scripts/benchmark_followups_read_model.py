"""Manual, local-only benchmark for GET /api/v1/operator-console/followups.

Not part of ``verify-core``/``verify-persistence``/pytest -- this is a
reproducible measurement tool for the Operator Console Follow-Up
Performance v1 fix, kept as the "reproducible benchmark command" a future
session can run directly against the real retained ``apex`` database
(never ``apex_test``) to reconfirm the endpoint's real-scale latency.

Read-only: every call this script makes is one of the documented read-only
repository methods ``operator_console_followups`` itself calls (see
``apex/operator_api.py``). It never invokes ``SyncFollowUpObservations``,
``AssessFollowUpEvidenceQuality``, ``followup-sync``, or any other write
path, and connects using the same env-driven ``PostgresSettings`` every
other read-only tool in this repository uses -- so it will refuse to run
against anything except whatever database ``APEX_POSTGRES_DB`` (default
``apex``) actually names, same as any other direct-Postgres tool here.

Usage (against the real retained database, from the repository root):

    PYTHONPATH=src python3 scripts/benchmark_followups_read_model.py

Optional: pass a wall-clock bound in seconds (default 30) as the sole
argument -- the script aborts and reports whatever it collected so far
rather than ever running unbounded:

    PYTHONPATH=src python3 scripts/benchmark_followups_read_model.py 60

What it measures, mirroring ``operator_console_followups``'s exact call
sequence (the real, unmodified ``PlanFollowUpObservations``/
``_CryptoFollowUpPriceSource`` classes throughout -- only their three
dependencies are the same request-scoped wrappers the route itself now
uses):
  - wall-clock duration and CPU time (``resource.getrusage``)
  - process RSS delta
  - total SQL statements issued, broken down by table
  - call counts for the repository methods this milestone's investigation
    identified as the previous bottlenecks
  - the resulting FollowUpPlan's own state counts, as a correctness sanity
    check alongside the timing
"""

from __future__ import annotations

import resource
import signal
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sqlalchemy as sa

from apex.adapters.counterfactual_followup_postgres import (
    PostgresCounterfactualFollowUpRepository,
    _CryptoFollowUpPriceSource,
)
from apex.adapters.counterfactual_ledger_postgres import (
    PostgresCounterfactualLedgerRepository,
)
from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
from apex.adapters.operating_cycle_postgres import PostgresOperatingCycleRepository
from apex.adapters.postgres import PostgresSettings, create_postgres_engine
from apex.adapters.shared_opportunity_postgres import (
    PostgresSharedOpportunityRepository,
)
from apex.application.counterfactual_followup import (
    PlanFollowUpObservations,
    plan_current_followup_selection,
)
from apex.application.operator_console_evidence import build_operator_followups
from apex.domain.counterfactual_followup import (
    CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION,
    CURRENT_FOLLOWUP_POLICY_VERSIONS,
)
from apex.operator_api import (
    _MemoizedCryptoUniverseObservations,
    _PrefetchedDecisions,
    _PrefetchedProjections,
)

_BOUND_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 30

_call_counts: Counter[str] = Counter()
_sql_statement_count = 0
_sql_by_table: Counter[str] = Counter()


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
_count_calls(
    PostgresSharedOpportunityRepository,
    "projections_by_id",
    "shared_opportunity.projections_by_id()",
)


def _sql_listener(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
    global _sql_statement_count
    _sql_statement_count += 1
    lowered = statement.strip().lower()
    for table in (
        "counterfactual_decision",
        "decision_followup_observation",
        "followup_evidence_quality_assessment",
        "followup_scan_cursor",
        "crypto_product_observation",
        "shared_opportunity_projection",
        "apex_operating_cycle_run",
        "apex_operating_task_result",
    ):
        if table in lowered:
            _sql_by_table[table] += 1
            return
    _sql_by_table["OTHER"] += 1


class _BoundExceeded(Exception):
    pass


def _on_alarm(signum, frame):  # type: ignore[no-untyped-def]
    raise _BoundExceeded(f"exceeded the {_BOUND_SECONDS}s bound")


def main() -> None:
    settings = PostgresSettings.from_env()
    print(f"connecting read-only to {settings.host}:{settings.port}/{settings.database}")
    if settings.database == settings.test_database:
        print("refusing to benchmark against a *_test database -- use the real retained database")
        raise SystemExit(1)

    engine = create_postgres_engine(settings)
    sa.event.listen(engine, "before_cursor_execute", _sql_listener)

    counterfactual_ledger_repository = PostgresCounterfactualLedgerRepository(engine)
    counterfactual_followup_repository = PostgresCounterfactualFollowUpRepository(engine)
    shared_opportunity_repository = PostgresSharedOpportunityRepository(engine)
    operating_cycle_repository = PostgresOperatingCycleRepository(engine)
    crypto_universe_repository = PostgresCryptoUniverseRepository(engine)

    def latest_effective_followup_cycle_task() -> dict[str, object] | None:
        cycles = sorted(operating_cycle_repository.cycles(), key=lambda item: item.created_at)
        for cycle in reversed(cycles[-15:]):
            for row in operating_cycle_repository.task_results(cycle.cycle_id):
                if getattr(
                    row.task_type, "value", row.task_type
                ) == "COUNTERFACTUAL_FOLLOWUP_REFRESH" and getattr(
                    row.status, "value", row.status
                ) in ("COMPLETED", "REUSED"):
                    return {
                        "status": getattr(row.status, "value", row.status),
                        "warning_codes": list(row.warning_codes),
                        "completed_at": row.completed_at,
                    }
        return None

    rusage_before = resource.getrusage(resource.RUSAGE_SELF)
    wall_before = time.perf_counter()
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(_BOUND_SECONDS)
    value = None
    plan = None
    try:
        observations = counterfactual_followup_repository.observations()
        already_observed_ids = {item.observation_id for item in observations}
        decisions = counterfactual_ledger_repository.decisions()
        decisions_by_id = {item.decision_id: item for item in decisions}
        evidence_assessments_by_observation_id = {
            item.observation_id: item
            for item in counterfactual_followup_repository.assessments(
                assessment_policy_version=CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION
            )
        }
        projections_by_id = shared_opportunity_repository.projections_by_id(
            {item.subject_projection_id for item in decisions}
        )
        plan = plan_current_followup_selection(
            PlanFollowUpObservations(
                _PrefetchedDecisions(decisions),
                _PrefetchedProjections(projections_by_id),
                _CryptoFollowUpPriceSource(
                    _MemoizedCryptoUniverseObservations(crypto_universe_repository)
                ),
            ),
            as_of=datetime.now(tz=UTC),
            already_observed=already_observed_ids,
        )
        plan_summary = {
            "as_of": plan.as_of,
            "policy_version": " · ".join(plan.policy_versions),
            "policy_versions": list(plan.policy_versions),
            "considered_decisions": plan.considered_decisions,
            "considered_pairs": plan.considered_pairs,
            "state_counts": plan.state_counts,
            "state_counts_by_venue": plan.state_counts_by_venue,
        }
        scan_cursors = [
            cursor
            for policy_version in CURRENT_FOLLOWUP_POLICY_VERSIONS
            if (
                cursor := counterfactual_followup_repository.scan_cursor_diagnostics(
                    policy_version=policy_version
                )
            )
            is not None
        ]
        coverage = counterfactual_followup_repository.coverage(
            decisions=decisions, observations=observations
        )
        value = build_operator_followups(
            observations,
            plan.entries,
            decisions_by_id=decisions_by_id,
            coverage=coverage,
            plan_summary=plan_summary,
            plan_policy_versions=plan.policy_versions,
            latest_cycle_task=latest_effective_followup_cycle_task(),
            scan_cursors=scan_cursors,
            kalshi_request_budget=20,
            evidence_assessments_by_observation_id=evidence_assessments_by_observation_id,
        )
        outcome = "COMPLETED"
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
    print(f"total_sql_statements: {_sql_statement_count}")
    for table, count in _sql_by_table.most_common():
        print(f"  sql[{table}]: {count}")
    for label, count in _call_counts.most_common():
        print(f"  calls[{label}]: {count}")
    if plan is not None:
        print(f"plan.considered_decisions: {plan.considered_decisions}")
        print(f"plan.considered_pairs: {plan.considered_pairs}")
        print(f"plan.state_counts: {plan.state_counts}")
    if value is not None:
        print(f"response items total: {value.get('total')}")

    engine.dispose()


if __name__ == "__main__":
    main()
