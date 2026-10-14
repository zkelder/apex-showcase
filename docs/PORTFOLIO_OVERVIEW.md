# Portfolio Overview

## What Apex is

Apex is a Python 3.12 / PostgreSQL platform for market research and
paper-trading **simulation**. It captures bounded evidence, retains it in
append-only tables, evaluates hypotheses and strategies under point-in-time
rules, keeps both accepted and rejected decisions as replayable evidence, and
runs unattended as short idempotent workers. It is built so that research can
run freely while real-money execution does not exist as a code path.

It is engineering evidence only: no claim of trading edge or profitability,
and no live trading.

## Skills evidenced, mapped to the repository

| Skill | Concrete evidence |
|---|---|
| Typed Python | Python 3.12, `mypy --strict` over `src` and `scripts/dev.py`, `ruff`; `pyproject.toml`; pure domain layer with frozen dataclasses (`src/apex/domain/`) |
| Software architecture | Domain / application / adapters layering enforced by AST-based tests (`tests/security/test_architecture_boundaries.py`) |
| PostgreSQL | Linear reversible Alembic chain (`migrations/versions/`), triggers for immutability (`immutable_trigger_ddl`), CHECK constraints, row locks in fixed order, retained-evidence downgrade guards |
| Data integrity | Canonical JSON and SHA-256 content addressing (`src/apex/domain/values.py`); `Decimal`-only money; double-entry ledger (`src/apex/domain/ledger.py`); property tests (`tests/property/`) |
| Docker | Compose PostgreSQL with healthcheck and limits (`compose.yaml`); per-checkout isolated verification identity (`scripts/dev-worktree-env.sh`, `docs/WORKTREE_VERIFICATION.md`) |
| Linux and unattended operation | systemd user timers and one-shot services (`deploy/`); linger, journald, `Persistent=` catch-up; WSL2 caveats documented |
| Fault tolerance | Lease/heartbeat runner, idempotent cycles, crash-resumable finalization, dependency-scoped failure isolation (`src/apex/application/operational_reliability.py`, `src/apex/application/operating_cycle.py`, matching tests) |
| Fail-closed operations | Schema-compatibility guard for workers (`src/apex/domain/schema_compatibility.py`) with end-to-end tests against a behind database |
| Controlled execution | Authority `NONE`, tiered model, prohibited LIVE, per-adapter import allowlists, no-live-path tests (`docs/SAFETY_MODEL.md`) |
| Reproducibility | Deterministic hash-bound fixtures, byte-identical reports under socket denial (`tests/security/test_no_live_path.py`) |
| Testing discipline | Layered suites: unit, Hypothesis properties, security, real-PostgreSQL persistence, concurrency, frontend Vitest |
| Full-stack delivery | FastAPI operator API (`src/apex/operator_api.py`) and a React/TypeScript/Vite console (`frontend/`) |
| Engineering judgment | Written decisions with tradeoffs (`docs/ENGINEERING_DECISIONS.md`); honest limitations (`docs/LIMITATIONS_AND_FUTURE_WORK.md`); incident-driven fixes (`docs/OPERATIONS_AND_RELIABILITY.md`) |

## Resume / portfolio blurbs

**About 60 words**

> Built Apex, a typed Python/PostgreSQL research and paper-trading simulation
> platform. Evidence lives in append-only, trigger-protected tables with
> content-addressed IDs; money is Decimal-exact with a double-entry ledger.
> Runs unattended via systemd timers with leases, idempotent replay, and a
> schema-compatibility fail-closed guard. Execution authority is structurally
> absent, and tests enforce it. Strict typing, property tests, deterministic fixtures.

**About 120 words**

> Apex is a research and paper-trading simulation platform I designed and
> built end to end in Python 3.12 and PostgreSQL. A strict domain,
> application, adapter layering is enforced by AST-based tests, and the
> financial core uses Decimal-only values, a double-entry ledger, and a risk
> kernel with non-bypassable invariants. Evidence is stored in append-only
> tables protected by database triggers, with content-addressed identities
> that make retries and crash recovery idempotent. A systemd-driven
> operating cycle runs bounded one-shot workers with leases, heartbeats,
> dependency-scoped failure isolation, backup and restore verification, and
> a worker guard that refuses to run against a mismatched schema. A tiered
> authority model keeps research separate from execution, with live trading
> prohibited. It includes a React operator console and a reproducible,
> fixture-driven test suite.

## 3-minute demo storyboard

Total target: 3:00. Record from the static demo (`https://zkelder.dev/apex/demo/`) and a
terminal.

| # | Time | Shot | On-screen text | Narration |
|---|---|---|---|---|
| 1 | 0:00-0:15 | Repository README top, then banner of the demo | "Apex: research and paper-trading simulation" / "Engineering evidence only" | "Apex is a Python and PostgreSQL platform for research and paper-trading simulation. It is engineering evidence, not a trading system: there is no live execution path." |
| 2 | 0:15-0:40 | Mermaid architecture diagram | "domain -> application -> adapters" | "The core is layered. Domain rules are pure and import nothing but the standard library. A test parses the code and fails the build if a layer reaches across its boundary." |
| 3 | 0:40-1:10 | Terminal: `demo-core --fixture golden` twice, then `cmp` | "Deterministic: same input, identical bytes" | "Eight offline outcomes run from synthetic fixtures. Running one twice with networking disabled gives byte-identical canonical reports, and every ID is a content hash." |
| 4 | 1:10-1:35 | Terminal: run the invariant-rejected and partial-fill fixtures | "Rejected decisions are evidence too" | "A stale-data rejection is retained with its reasons and creates no order. A partial fill converts exactly the filled share of risk, and cancellation releases the rest." |
| 5 | 1:35-2:05 | Static demo: the Deterministic outcomes tab (open "Partial fill, settled NO" to show the ledger), then the Operator Console tab (System, Campaigns, Portfolio, Ledger) with the banner visible | "SYNTHETIC DEMONSTRATION DATA - NOT LIVE OPERATIONS" | "The console reads a retained-evidence snapshot built from the same fixtures. Everything is read-only and labelled synthetic." |
| 6 | 2:05-2:30 | Editor: migration with immutable trigger; test showing the trigger rejecting an update | "Append-only, enforced by the database" | "Evidence tables are immutable at the database level. A correction is a new row, never an update." |
| 7 | 2:30-2:50 | `deploy/` timer unit, then the schema-compatibility test | "Fails closed on schema mismatch" | "In unattended operation a timer launches bounded one-shot workers. Before doing any work, each worker checks that the database schema exactly matches the code, and refuses to run otherwise." |
| 8 | 2:50-3:00 | Safety model table | "LIVE: prohibited" | "Authority is structurally absent, and tests enforce it. That is the point: autonomy for research, none for capital." |

### Screen-capture instructions for the static demo

1. Build and serve the static demo locally (`cd frontend && npm ci && npm run build:demo`, then
   `python3 -m http.server 4173 --directory dist-demo`) or open `https://zkelder.dev/apex/demo/`, in a
   clean browser profile. `scripts/demo/regenerate_demo_data.sh` regenerates the underlying data.
2. Use a 1920x1080 window at 100 percent zoom, the default dark theme, no extensions,
   bookmarks bar hidden. Confirm the synthetic-data banner is visible on every
   page before recording.
3. Capture with any screen recorder at 30 fps. Record the terminal shots with
   a large font (at least 18 pt) and a neutral prompt; run commands from the
   repository root.
4. For stills, capture `docs/images/console-home.png`,
   `docs/images/console-opportunities.png`, `docs/images/console-decision-ledger.png`
   and `docs/images/console-operations.png`.
5. Review each frame for personal paths, hostnames, usernames and tokens
   before publishing.
