# Engineering Decisions

Each entry records a decision in ADR style: context, decision, tradeoffs,
and where to verify it in the repository. File paths are relative to the
repository root.

## ADR 1. Decimal-only money and prices

**Context.** Binary floating point cannot represent most decimal prices and
fees exactly, and rounding drift silently breaks ledger balance.

**Decision.** All money, price, quantity, fee, probability and risk values are
`Decimal`. `parse_decimal` accepts canonical fixed-point strings, integers or
finite `Decimal` values and rejects `float`, `bool`, NaN, infinity and
exponent notation. Authoritative values serialize as normalized strings;
fixtures reject JSON numeric tokens for authoritative fields.

**Tradeoffs.** More verbose than floats, slower for numeric research, and
every I/O boundary needs explicit parsing. Rounding must be declared (for
example a quantity is floored to the increment, never rounded up).

**Evidence.** `src/apex/domain/values.py` (`parse_decimal`);
`tests/property/test_financial_invariants.py` (canonical round-trip creates no
value, quantity flooring never rounds up, settlement journals balance);
`tests/persistence/test_postgres_security.py::test_schema_has_no_approximate_authoritative_numeric_or_scope_expansion`
scans the schema for approximate numeric columns.

## ADR 2. Append-only evidence tables enforced in the database

**Context.** Research conclusions are only trustworthy if the evidence behind
them cannot be edited after the fact, including by application bugs.

**Decision.** Evidence tables are immutable. Each migration that adds an
evidence family installs row-level triggers via `immutable_trigger_ddl`, and
CHECK constraints pin `authority = 'NONE'` on research families. Corrections
are new rows that reference the old one, never updates. Downgrades refuse
while retained evidence exists, so a rollback cannot destroy history.

**Tradeoffs.** Storage only grows; corrections need explicit supersession
records; a mutation that is genuinely required (rare backfills) must disable
the trigger inside a narrow, documented migration step.

**Evidence.** `src/apex/adapters/postgres.py` (`immutable_trigger_ddl`);
`migrations/versions/`; `tests/persistence/test_postgres_security.py`
(`test_authorization_rows_are_immutable_after_commit`,
`test_execution_events_are_immutable_at_the_database_boundary`);
`tests/persistence/test_postgres_migrations.py`.

## ADR 3. Content-addressed identities and idempotent replay

**Context.** Unattended jobs get retried, restarted and interrupted. A retry
must never duplicate a reservation, order or evidence row.

**Decision.** Identities are deterministic functions of canonical content:
canonical JSON (sorted keys, no whitespace, finite numbers only) hashed with
SHA-256 (`canonical_json_bytes`, `content_digest`, `deterministic_id`). The
same input yields the same ID and row; a conflicting payload under an existing
ID raises an identity-conflict error instead of overwriting. Identities
deliberately exclude volatile fields (wall-clock timestamps, history-dependent
features) when replays must resolve to the same row.

**Tradeoffs.** Choosing what belongs in an identity is subtle: too much and
replays mint spurious duplicates; too little and genuinely different facts
collide. Several fixes in the project history were exactly this class of bug.

**Evidence.** `src/apex/domain/values.py`;
`tests/persistence/test_postgres_concurrency.py`
(`test_concurrent_same_request_returns_one_idempotent_bundle`);
`tests/persistence/test_postgres_partial_fill_concurrency.py`
(`test_concurrent_identical_fill_creates_one_event_position_and_journal_pair`);
`tests/property/test_financial_invariants.py` (duplicate and conflicting
events are atomic).

## ADR 4. Fail-closed schema-compatibility guard

**Context.** Code can land before the database migration it needs. A worker
running against a behind schema crashes repeatedly with a raw SQL error deep
inside a task.

**Decision.** The application declares the exact schema revision it requires
(`REQUIRED_SCHEMA_REVISION`). Before dispatching any task, each unattended
worker reads the database's single `alembic_version` row and classifies it:
compatible, behind, ahead, or unknown. Anything other than an exact match
refuses to run with an explicit `SCHEMA_REVISION_*` error. The classification
is a pure domain function; the adapter only reads the revision. A separate
status distinguishes "cannot connect" from "schema mismatch".

**Tradeoffs.** Exact equality is strict: there is no rolling-deploy window.
That is acceptable because migrations form one linear chain and both workers
ship from one checkout. A deploy must migrate first.

**Evidence.** `src/apex/domain/schema_compatibility.py`;
`src/apex/adapters/schema_compatibility_postgres.py`;
`tests/test_schema_compatibility.py` (including that the declared revision
equals the real Alembic head);
`tests/persistence/test_postgres_schema_compatibility.py` (both workers fail
closed end to end when the database is behind; connection failure is
distinguishable from mismatch).

## ADR 5. Fixed lock ordering and row locks for financial state

**Context.** Concurrent authorization, execution and settlement can
over-reserve capacity or deadlock if they take row locks in different orders.

**Decision.** State-changing paths lock rows with `SELECT ... FOR UPDATE` in
one fixed order: Campaign, then Order, then RiskReservation, then Position.
Transitions assert they still hold the lock. Reservation conservation (for
example a partial fill converting a reservation into position risk plus a
smaller order reservation) is exact in `Decimal`.

**Tradeoffs.** Serializes work per campaign; lock-order discipline must be
maintained by convention and caught by concurrency tests rather than by the
type system.

**Evidence.** `src/apex/adapters/postgres.py` (`with_for_update` call sites
and the "lost its row lock" guards);
`tests/persistence/test_postgres_concurrency.py`,
`test_postgres_partial_fill_concurrency.py`,
`test_postgres_unfilled_concurrency.py`;
`tests/property/test_financial_invariants.py` (risk conservation).

## ADR 6. Point-in-time information sets and no look-ahead

**Context.** Backtests and replays are worthless if a decision can see data
that was not yet available at its cutoff.

**Decision.** Every source record carries event time, availability time,
schema version and content hash. A decision receives only an immutable
`InformationSet` containing records with `available_at` no later than the
cutoff. Strategies cannot reach settlement data, fixtures, repositories or
the filesystem. Reusable feature calculations call `assert_no_lookahead`.
Settlement is loaded only after cancellation and must match the exact
instrument, rule ID, rule content hash and resolution source. Execution-time
freshness is rechecked by the risk kernel against a sealed maximum that
campaign policy cannot configure or disable. Retrospective provider history
is labelled as such, not as vintage point-in-time data.

**Tradeoffs.** More plumbing, and honest limits (for example survivorship is
flagged explicitly where the universe is today's cohort applied backwards).

**Evidence.** `src/apex/domain/market.py` (`InformationSet`,
`InformationSetBuilder`); `src/apex/domain/historical_market_data.py`
(`assert_no_lookahead`, `PointInTimeError`); `tests/test_historical_market_data.py`;
`tests/property/test_financial_invariants.py`
(`test_campaign_policy_changes_cannot_suppress_operational_staleness`);
`tests/security/test_no_live_path.py` (cross-file tampering fails closed after
rehash).

## ADR 7. Authority `NONE` and a prohibited LIVE tier

**Context.** A research platform that also touches brokers needs a structural
guarantee that research output cannot become an order.

**Decision.** Research and decision records are constructed with
`authority = "NONE"` and database CHECK constraints pin it. Execution
authority is a separate subsystem: tiers SHADOW, INTERNAL PAPER, EXTERNAL
PAPER, with LIVE prohibited. The risk kernel refuses to authorize outside
`SIMULATION_ONLY`. Credential-backed access has its own permission ladder in
which authentication never implies a dangerous permission. The only live gate
(`ExecutionControlState.LIVE_PILOT_ARMED`) is not reachable from any
operational path.

**Tradeoffs.** Many records carry a field that is always `NONE`; adding any
real execution capability would be a deliberate, reviewed change touching
tests that currently forbid it.

**Evidence.** `src/apex/domain/live_readiness.py`;
`src/apex/domain/decisions.py` (`RiskKernel`);
`tests/security/test_no_live_path.py` (CLI rejects live and execution modes);
`tests/security/test_architecture_boundaries.py` (exact per-adapter import
allowlists); [SAFETY_MODEL.md](SAFETY_MODEL.md).

## ADR 8. A bounded one-shot worker under systemd, not a workflow engine

**Context.** The system needs recurring, recoverable operation on a single
machine without adding a distributed scheduler.

**Decision.** A systemd user timer launches `operating-cycle-worker --once`
every five minutes. The process evaluates what is due from retained state,
executes at most one cycle, records a lease and heartbeat stream, and exits.
All due-work logic is in the application layer (`plan_due_work`,
`ExecuteOperatingCycle`), so the timer is only a trigger and the same command
works when run by hand. Re-invocation reconciles an already-retained cycle
with current reality instead of replaying a frozen result, and finishes
finalization if an earlier crash left it incomplete.

**Tradeoffs.** No horizontal scaling, no automatic restart policy, and
availability depends on the host. A workflow engine would add moving parts
that a single-operator system does not need.

**Evidence.** `deploy/`; `src/apex/domain/operating_cycle.py`;
`src/apex/application/operating_cycle.py`;
`src/apex/application/operational_reliability.py` (`SupervisedOperatingRunner`);
`tests/test_operating_cycle.py`; `tests/persistence/test_postgres_operating_cycle.py`;
`tests/persistence/test_postgres_operational_reliability.py`.

## ADR 9. Deterministic, hash-bound synthetic fixtures

**Context.** Evaluators need inputs that are reproducible, reviewable and
safe, and tests must prove tampering is detected.

**Decision.** Eight offline outcomes are defined by committed fixtures with
registered IDs and content hashes. The reader accepts only registered IDs (no
paths, no URLs), verifies hashes, and rejects hidden execution fields and JSON
numeric tokens. Fixture text is untrusted data and is never executed or
rendered. Demos run with outbound networking disabled and must produce
byte-identical stdout across runs.

**Tradeoffs.** Fixtures are synthetic and small; they prove correctness of
the pipeline, not market behavior.

**Evidence.** `src/apex/adapters/fixtures.py` (`FixtureReader`,
`REGISTERED_FIXTURE_IDS`); `fixtures/`;
`tests/security/test_no_live_path.py`
(`test_registered_fixture_ids_include_only_the_eight_offline_outcomes`,
`test_demo_is_deterministic_with_external_networking_disabled`,
`test_cli_rejects_fixture_paths`).

## ADR 10. Isolated per-worktree verification

**Context.** Concurrent checkouts shared one fixed Postgres port and one test
database name, so persistence verification from one checkout could silently
run against another's database.

**Decision.** `scripts/dev-worktree-env.sh` derives a deterministic Compose
project name, host port and test database name from the checkout's absolute
path, so `verify-persistence` always targets a database it owns. The
`db-identity` command prints the resolved target without mutating anything.
`verify-persistence` recreates its `*_test` database from `template0` and
migrates to head each run.

**Tradeoffs.** A small shell helper and a hash-to-port mapping with linear
probing; ports are not guaranteed stable across paths.

**Evidence.** `docs/WORKTREE_VERIFICATION.md`; `scripts/dev.py`
(`db-identity`, `verify-persistence`); `tests/persistence/conftest.py`.
