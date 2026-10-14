# Operations and Reliability

Apex is designed to run unattended on a single machine as a series of short,
bounded, idempotent worker invocations. This document describes the mechanisms,
the failure modes they address, and the tests that exercise them. It describes
a local, single-operator deployment, not a redundant production service.

## Design principles

1. **The host only triggers; the application decides.** Scheduling, due-work
   and dependency logic live in `src/apex/domain/operating_cycle.py` and
   `src/apex/application/operating_cycle.py`. A systemd timer merely launches
   `operating-cycle-worker --once`; the same command works by hand.
2. **Bounded work.** Each invocation evaluates what is due, runs at most one
   cycle, and exits. Provider calls have explicit request and byte budgets and
   no automatic retries.
3. **Idempotent by identity.** Cycle, task and evidence identities are
   deterministic ([ENGINEERING_DECISIONS.md](ENGINEERING_DECISIONS.md), ADR 3),
   so re-running a completed cycle reuses retained results instead of
   duplicating work.
4. **Fail closed.** Missing capability, mismatched schema, unreachable
   database or ambiguous state produce an explicit, retained failure or a
   refusal to run, never a silent no-op or partial guess.

## Trigger layer (systemd user timers)

The `deploy/` directory contains example `Type=oneshot` service units paired
with timers. Generalised shape:

```ini
# ~/.config/systemd/user/apex-operating-cycle-worker.service
[Service]
Type=oneshot
WorkingDirectory=<repo>
ExecStart=uv run python -m apex.cli operating-cycle-worker \
  --plan-id <plan-id> --runner-id <runner-id> \
  --run-instance-id systemd-<utc-timestamp> --once
Restart=no

# ~/.config/systemd/user/apex-operating-cycle-worker.timer
[Timer]
OnCalendar=*:0/5
Persistent=true
RandomizedDelaySec=15s
```

- `Persistent=true` runs a missed tick once on resume rather than skipping it.
- There is deliberately no restart-on-failure: the next timer tick is the
  retry, and a failed invocation leaves a visible failed unit and journal
  entry.
- A second, independent timer/service pair runs a one-minute bounded
  observation worker with its own runner identity, so the two cannot contend
  for each other's lock.
- User-level timers need `loginctl enable-linger <user>` to run without an
  interactive login. On WSL2 the timer also depends on the VM running.
  Paths such as `%h` and `<repo>` above are placeholders; adapt them.

## Leases, heartbeats and single-instance execution

`SupervisedOperatingRunner` (`src/apex/application/operational_reliability.py`)
retains an append-only stream of runner heartbeats. Each heartbeat records a
lease expiry (`LEASE_SECONDS = 180`) and the next due time
(`EVALUATION_CADENCE_SECONDS = 300`). A second instance cannot claim a runner
whose lease is live; a stale lease can be taken over after expiry. Heartbeat
rows are immutable.

Evidence: `tests/test_operational_reliability.py::test_runner_prevents_duplicates_and_recovers_after_stop_or_stale_lease`;
`tests/persistence/test_postgres_operational_reliability.py::test_runner_claim_is_single_instance_and_immutable`.

## Task isolation and dependency propagation

The cycle runs an ordered set of tasks (`TASK_ORDER`). Each task result is
retained with a disposition (completed, reused, not due, blocked,
unavailable, failed) and machine-readable reasons. A failure blocks only the
tasks that declare a dependency on it; independent tasks still run. Several
tasks deliberately declare no dependencies so an unrelated failure cannot
starve them.

Evidence: `tests/test_operating_cycle.py` (for example
`test_unavailable_source_blocks_only_dependents_and_brief_remains_due`,
`test_crypto_market_pulse_failure_blocks_exactly_its_six_real_dependents`,
`test_partial_failure_replay_conflict_pause_and_resume`).

## Crash recovery and re-evaluation

A cycle is persisted before its finalization (events, brief, health). If a
process dies in between, the next invocation reconciles the retained cycle with
current state and completes the missing finalization rather than treating the
cycle as permanently unrecoverable. A task whose due-ness can only become true
later the same day is not frozen as "not due"; later invocations re-observe it.
Backdated production cycles are refused.

Evidence: `tests/persistence/test_postgres_operating_cycle.py`, for example
`test_execute_operating_cycle_resumes_safely_after_a_simulated_crash_during_options_capture`,
`test_portfolio_snapshot_refresh_task_is_idempotent_and_restart_safe`,
`test_crypto_universe_refresh_resumes_safely_after_a_crash_between_capture_and_materialize`
and `test_executor_reaches_genuine_options_due_window_using_executed_at_not_as_of`.

## Schema-compatibility guard

Both unattended workers check the database's single `alembic_version` row
against `REQUIRED_SCHEMA_REVISION` before dispatching any task. Behind, ahead
and unknown schemas, and plain connection failures, each produce a distinct
explicit refusal. This turns "code deployed before its migration" from a
repeating crash into a single clear stop.

Deployment order for a change that adds a migration: apply the migration
first, then update the code the workers run (the guard enforces it). See
`docs/MIGRATION_COMPATIBILITY_GUARD_V1.md` if it is included in this release;
otherwise `src/apex/domain/schema_compatibility.py` documents the contract.

Evidence: `tests/test_schema_compatibility.py`;
`tests/persistence/test_postgres_schema_compatibility.py`.

## Backup and restore verification

`PostgresLogicalBackupDrill` (`src/apex/adapters/operational_backup.py`)
produces a logical `pg_dump` into a path constrained to stay inside the
repository root, records a manifest with the schema revision and per-table row
counts, then restores into a disposable database and verifies the restored
counts. It never replaces the active database. Backup readiness is recorded as
evidence and feeds the readiness assessment.

Evidence: `tests/persistence/test_postgres_operational_reliability.py`
(`test_genuine_backup_runner_restart_kill_switch_and_readiness_replay`). This
test shells out to `docker compose exec`, so it needs the isolated Compose
project described in [TESTING_AND_REPRODUCIBILITY.md](TESTING_AND_REPRODUCIBILITY.md).

## Alerts

`LocalAlertOutbox` retains each alert delivery locally with a closed category
vocabulary and redaction of secret values (`contains_secret_values` is
recorded and asserted false). The worker raises alerts only for genuinely new
cycle completions with a failed task, a missed capture window, or repeated
failures, and stays silent for expected not-due cycles. A category not in the
vocabulary raises rather than being swallowed.

Evidence: `tests/test_operational_reliability.py::test_alert_outbox_redacts_secrets_and_retains_delivery`;
the alert-candidate tests in `tests/test_operating_cycle.py`.

## Kill switch

`ExecutionControlState` (`src/apex/domain/live_readiness.py`) includes
`EMERGENCY_STOP`. A deterministic kill-switch drill checks that an emergency
stop blocks submissions and does not grant any live authority.

Evidence: `tests/test_operational_reliability.py::test_emergency_stop_blocks_submissions_without_granting_live`.

## Failure modes handled

| Failure | Behaviour | Evidence |
|---|---|---|
| Duplicate invocation / overlapping timers | Lease prevents a second live claimant; identities make replays reuse results | `test_runner_claim_is_single_instance_and_immutable` |
| Process crash mid-cycle | Next invocation completes finalization; no duplicate rows | `tests/persistence/test_postgres_operating_cycle.py` |
| Task failure | Only declared dependents blocked; failure retained | `tests/test_operating_cycle.py` |
| Database behind or ahead of code | Worker refuses with `SCHEMA_REVISION_*` | `tests/persistence/test_postgres_schema_compatibility.py` |
| Database or container unreachable | Distinct connection-failure status; visible failed unit; next tick recovers | `test_connection_failure_is_distinguishable_from_a_schema_mismatch` |
| Missing credentials | Only the dependent task reports unavailable; never a silent success | `test_operating_cycle_tasks_are_bounded_and_credentials_absent_is_not_due` |
| Provider error | Retained as an explicit outcome; no automatic retry loop | operating-cycle task tests |
| Concurrent writers to financial state | Row locks in a fixed order; exact conservation | `tests/persistence/test_postgres_*concurrency.py` |

## Incident-driven hardening

Several reliability features exist because a real failure exposed a gap. They
are listed as engineering lessons, without environment specifics.

- **Code ahead of schema.** Application code that required a new table was
  running while the database had not yet been migrated, and a worker crashed
  on every tick with a low-level SQL error. The fix was the exact-revision
  guard above, with a regression test that reproduces that shape.
- **Dependency outage.** When the container runtime was unreachable, workers
  failed closed with an explicit error and recovered on the next tick once
  the database returned, with no duplicated work. The lesson recorded was to
  distinguish "cannot connect" from "schema mismatch" in reporting.
- **Unreachable window.** A time-window task could never become due because the
  cycle identity pinned its clock to a day boundary. The fix threads true
  wall-clock time to the due-check only, leaving day-scoped cadence rules
  unchanged, and adds re-evaluation within the day.
- **Silent alert loss.** An over-broad exception handler swallowed a rejected
  alert category, so a real failure produced no alert. The fix added the
  categories, narrowed the handler, and added a test that round-trips every
  producible category through the real vocabulary.
- **Identity too narrow or too wide.** Persistence identities that omitted an
  as-of scope collided on re-observation; identities that included volatile
  fields minted spurious duplicates. Both were corrected and covered by
  replay tests.
- **Shared test infrastructure.** Concurrent checkouts that shared one
  database port made persistence tests ambiguous; verification now uses a
  per-checkout isolated identity (ADR 10).

## Honest boundaries

Single host, single operator; availability depends on the machine and its
container runtime being up; no automatic restart policy; no redundancy.
Backups are local logical dumps verified by disposable restore, not an offsite
disaster-recovery plan.
