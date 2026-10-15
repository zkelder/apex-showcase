# Testing and Reproducibility

## Test layers

| Layer | Location | Needs | Purpose |
|---|---|---|---|
| Unit and application | `tests/test_*.py` | nothing | Domain rules, application use cases with in-memory repositories, CLI surface |
| Property-based | `tests/property/test_financial_invariants.py` | nothing | Hypothesis-generated checks of money and risk invariants |
| Integration (offline) | `tests/integration/test_vertical_slice.py` | nothing | The decision pipeline end to end on fixtures |
| Security and boundary | `tests/security/` | nothing | Architecture import rules, no-live-path, fixture tamper resistance |
| Persistence | `tests/persistence/` (marker `postgres`) | PostgreSQL | Migrations, immutability triggers, atomicity, concurrency, schema guard, backup drill |
| Frontend | `frontend/src/*.test.tsx` | Node | Operator Console components (Vitest, Testing Library) |

`pyproject.toml` sets `addopts = "--strict-config --strict-markers -m 'not postgres'"`,
so the default run never touches a database.

## What the property tests assert

Examples from `tests/property/test_financial_invariants.py`: quantity
flooring never rounds up; probability vectors stay exactly bounded; funding is
never recorded as trading profit; settlement journals balance; allocation
never exceeds authorized loss or visible quantity; capital scaling is
monotone until a named constraint binds; canonical decimal round-trip
creates no value; staleness rejection cannot be suppressed by campaign
policy; partial-fill risk is conserved exactly for decimal increments;
duplicate and conflicting events are financially atomic.

## Offline, socket-denied suite

`python scripts/dev.py verify-core` runs `lint`, `typecheck` (`mypy --strict`)
and `test-offline`. `test-offline` (`scripts/dev.py`) starts pytest in a
process where `socket.create_connection`, `getaddrinfo`, `connect`,
`connect_ex` and `sendto` are replaced with functions that raise, and sets
`APEX_OFFLINE=1`. Any test or code that attempts outbound networking fails.
The security suite additionally runs the CLI as a subprocess with networking
disabled and compares outputs.

## Persistence suite and isolated Compose identity

`python scripts/dev.py verify-persistence` drops and recreates the `*_test`
database from `template0`, runs `alembic upgrade head`, and runs
`pytest -m postgres tests/persistence`. It is the only command that needs
Docker.

To keep checkouts from interfering with each other, `scripts/dev-worktree-env.sh`
derives a deterministic Compose project name, a host port in a fixed range
(with probing if taken) and a test database name from the checkout's
absolute path; values are cached in an ignored `.env`. `db-identity` prints
the resolved target without changing anything. Rationale and edge cases:
`docs/WORKTREE_VERIFICATION.md`.

One persistence test (`test_genuine_backup_runner_restart_kill_switch_and_readiness_replay`)
shells out to `docker compose exec`, so it requires the checkout's own
Compose project to be genuinely running; run `db-up` first.

Persistence tests cover, among others: immutability at the database boundary
(`test_postgres_security.py`), migration up/down/up behavior including
retained-evidence downgrade guards (`test_postgres_migrations.py`), concurrent
authorization and fills (`test_postgres_concurrency.py`,
`test_postgres_partial_fill_concurrency.py`,
`test_postgres_unfilled_concurrency.py`), the schema-compatibility guard
(`test_postgres_schema_compatibility.py`), and operating-cycle restart safety
(`test_postgres_operating_cycle.py`).

## Tiered verification policy

- **During implementation:** run only the narrow tests for changed code.
- **At completion:** `format`, `lint`, `typecheck`, `verify-core`, `db-up`,
  `verify-persistence`, `db-down`, then `git diff --check`.
- **Conditional:** repeat concurrency tests only if locking changed; run
  migration downgrade/re-upgrade only if migration behaviour changed;
  reserve full dependency and secret audits for major milestones.
- **Never weaken** financial invariants, exact `Decimal` checks, ledger
  balancing, idempotency, rollback or risk-conservation tests.

## Determinism and canonical hashes

- Reports are canonical JSON: sorted keys, `,` and `:` separators, UTF-8,
  finite numbers only (`canonical_json_bytes`, `src/apex/domain/values.py`).
- Content hashes are SHA-256 of those bytes (`content_digest`); entity IDs are
  `namespace_` plus the first 24 hex characters (`deterministic_id`).
- `tests/security/test_no_live_path.py::test_demo_is_deterministic_with_external_networking_disabled`
  runs each of the eight fixtures twice with networking disabled and asserts
  identical stdout and stderr, and that the printed report equals its own
  canonical re-serialization.
- Fixtures are hash-bound; tampering with any file, even after re-hashing one
  file, fails closed on cross-file bindings.

## Reproduce it

```bash
uv sync --all-extras
uv run python scripts/dev.py demo-core --fixture golden --capital 1000.00 --seed 20260716 > a.json
uv run python scripts/dev.py demo-core --fixture golden --capital 1000.00 --seed 20260716 > b.json
cmp a.json b.json && echo identical
uv run python scripts/dev.py verify-core
# optional, needs Docker:
uv run python scripts/dev.py db-up && uv run python scripts/dev.py verify-persistence && uv run python scripts/dev.py db-down
# frontend:
cd frontend && npm ci && npm run check && npm test
```

### Regenerating the static demo data

The hosted demo is backed by a synthetic snapshot generated from the
repository's own fixtures against a throwaway database. Regenerate it with:

```bash
scripts/demo/regenerate_demo_data.sh
```

The output is deterministic and contains no production values.

### Recorded results

Recorded 2026-10-14 on a clean checkout: `ruff` format/lint and `mypy --strict` clean; `verify-core` **3675 passed**; `verify-persistence` **946 passed, 5 skipped, 0 failed** against an isolated PostgreSQL 17 container migrated from empty to head (`0118`). The five skips are acceptance tests that report themselves skipped when no retained real market-data history exists in the database, which is the case for a fresh isolated database.

Frontend: typecheck clean, `npm audit` reports 0 vulnerabilities, and **101 Vitest tests pass** on the current snapshot. That count was 98 at the backend run above, 99 after a mobile table-overflow fix, and 101 after the guided demo-entry tests. None of those commits changed backend code (`git diff` over `src`, `tests` and `migrations` is empty), so the backend figures still describe this snapshot.

Counts are pytest and Vitest results: parametrized and property-based cases are counted individually, so they are test results and not independent scenarios. They are results from this repository's own commands, not a claim of trading performance.
