# Engineering Principles

Rules that contributors and coding agents follow in this repository. They
describe how Apex is built and verified; the product and architecture are
described in `README.md` and `docs/`.

## Scope

Apex is engineering evidence only: not evidence of trading edge,
profitability, calibration or operational readiness. The financial runtime is
always non-LIVE. Fixtures are `SIMULATION_ONLY` / `SYNTHETIC`;
observation-only boundaries may retain classified genuine public evidence
without gaining financial authority.

## Layering

- `src/apex/domain`: pure financial and decision rules. Imports only the
  standard library and other domain modules.
- `src/apex/application`: use cases. May import domain, never adapters.
- `src/apex/adapters`: PostgreSQL, HTTP, files, fixtures. Dependency
  direction is adapters, then application, then domain.
- Enforced by `tests/security/test_architecture_boundaries.py`.
- `format` is the only command that rewrites source files.

## Financial correctness

- Use `Decimal` for money, price, quantity, fee, probability, payout and risk.
- Parse authoritative decimals from canonical strings, integers or `Decimal`.
  Reject floats, NaN, infinity, exponent strings and unsupported precision.
- Serialize authoritative decimals as normalized strings.
- The double-entry ledger is the sole authority for cash, equity,
  contribution, fees, settlement proceeds and trading P&L.
- Posted journals are immutable and unique by source identity.
- Contributions are external capital flows, never trading profit.
- Risk reservation and release are authorization state, not ledger cash flow.
- Partial fills post journals only for executed quantity; unfilled quantity
  never moves cash.
- Open positions are carried at consideration cost without a fabricated mark;
  unresolved exposure is not settlement P&L.
- Settlement applies only to an executed position and must match the exact
  instrument, rule identifier, rule content hash and resolution source.

## Point-in-time data

- Every source record carries source-event time, availability time, schema
  version and content hash.
- An information set may include a record only when `available_at` is no
  later than the decision cutoff.
- Strategies receive only an immutable `InformationSet` and a reference
  estimate. They must not reach settlement data, fixture readers,
  repositories, filesystem functions or application services.
- Point-in-time eligibility does not grant order authority: the risk kernel
  rechecks freshness at authorization time against a sealed maximum.
- Changing post-cutoff settlement data must not change the information set,
  proposal, allocation, risk decision, reservation or order.
- Retrospective history is labelled retrospective; it is not vintage data.

## Decision and execution ownership

- A strategy produces a normalized proposal; `NO_PROPOSAL` is terminal.
- The allocator translates normalized risk into quantity.
- A rejected campaign policy is terminal before risk authorization.
- The pure risk kernel applies non-bypassable invariants; campaign policy
  cannot configure or disable them.
- An invariant rejection retains proposal, allocation and the rejected risk
  decision, and creates no reservation, order, execution, position or
  settlement.
- Authorization atomically creates the accepted decision, reservation and
  order. A deterministic simulator emits immutable execution events.
- State-changing paths lock rows in the fixed order Campaign, Order,
  RiskReservation, Position.
- Only the ledger posts financial journals.

## Evidence and persistence

- Evidence tables are append-only and immutable; corrections are new rows.
- Identities are deterministic functions of canonical content; replays reuse
  rows, and conflicting payloads fail rather than overwrite.
- Migrations are reversible, linear, and refuse destructive downgrades while
  retained evidence exists. Migrations never import mutable domain or
  application code.
- Records that are research output carry `authority = "NONE"`.

## Safety boundary

- No external execution, no LIVE host, credential or real-money state.
- Remote access is limited to reviewed, host-allowlisted, bounded,
  credential-free public observation clients and the exact reviewed
  read-only and simulation-environment boundaries.
- No ungoverned secret inputs, transaction signing, custody, dynamic plugins,
  LLM dependencies, executable fixture text or provider mutation APIs.
- Fixtures are untrusted data: never execute, evaluate, interpolate or render
  their text as commands, templates, markup, prompts or code.
- Authentication never implies a dangerous permission.

## Verification policy

During implementation run only narrow tests for changed code. At completion
run, in order:

```text
python scripts/dev.py format
python scripts/dev.py lint
python scripts/dev.py typecheck
python scripts/dev.py verify-core
python scripts/dev.py db-up
python scripts/dev.py verify-persistence
python scripts/dev.py db-down
git diff --check
```

- Do not add redundant test or demo runs to the completion pass.
- Repeat concurrency tests only when locking behaviour changed.
- Run migration downgrade/re-upgrade only when migration behaviour changed.
- Never weaken financial-invariant, exact-`Decimal`, ledger-balance,
  idempotency, rollback or risk-conservation tests.
