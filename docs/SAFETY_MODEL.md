# Safety Model

Apex separates **research autonomy** from **capital authority**. The platform
can capture evidence, rank opportunities, evaluate mandates and run internal
paper simulations continuously, while the subsystem that could ever stand
between a finding and a real order remains structurally closed. This document
states what is architecturally absent, how that is enforced, and the
tiers that exist.

> Apex is engineering evidence only. It does not claim trading edge,
> profitability or readiness for live trading, and the financial runtime is
> always non-LIVE.

## Authority tiers

| Tier | Meaning | Status |
|---|---|---|
| `SHADOW` | Research only; hypothetical outputs, no simulated exposure | Implemented |
| `INTERNAL PAPER` | Local deterministic simulation in Apex's own ledger; no external effect | Implemented |
| `EXTERNAL PAPER` | A provider's simulation environment, long-only, reviewed host, separate credentials, exact bounded authority, explicit operator confirmation | Narrow reviewed adapter; not enabled by default and never reached by setup, tests or demos |
| `LIVE` | Real-money execution | **Prohibited**: no code path |

## Two orthogonal mechanisms plus a gate

1. **Sentinel authority.** Domain records are constructed with
   `authority = "NONE"` (and equivalent `capital_authority`,
   `financial_authority` fields). Database CHECK constraints on research
   families pin the value, so even raw SQL cannot record a higher authority.
2. **Named ceiling ladder** (`AuthorityCeiling`, `src/apex/domain/portfolio_intelligence.py`):
   `NONE`, `RESEARCH_ONLY`, `SHADOW_ONLY`, `PAPER_REVIEW_ONLY`, `PAPER`,
   `LIVE_REVIEW_ONLY`, `LIVE`. An opportunity whose ceiling is below `PAPER`
   or that carries any blocker contributes zero to a permitted research
   allocation.
3. **Execution control state** (`ExecutionControlState`,
   `src/apex/domain/live_readiness.py`): `DISABLED`, `PAPER_ONLY`,
   `LIVE_BLOCKED`, `LIVE_PILOT_ARMED`, `EMERGENCY_STOP`. The live risk
   evaluation blocks unless the state is exactly `LIVE_PILOT_ARMED`, and no
   operational path sets it; it is reachable only from isolated test
   fixtures. Even then, provider submission code does not exist.

Provider credentials have a separate permission ladder (`PermissionClass`:
`PUBLIC_DATA`, `PRIVATE_VIEW`, `ORDER_PREVIEW`, `TRADE`, `TRANSFER`,
`WITHDRAWAL`). Authentication alone never implies a dangerous permission; each
class must be established independently.

## What is architecturally absent

- Any LIVE provider host or credential handling for real-money accounts.
- Order placement, cancellation, transfer, withdrawal, funding, margin,
  leverage, shorting, derivatives or custody against a real-money account.
- Transaction signing and digital-asset custody.
- Dynamic plugin loading, LLM dependencies, executable fixture text.
- An automatic escalation path between tiers.

The only outbound path that can carry an order targets a provider's own
simulation environment (a hardcoded PAPER base URL in
`src/apex/adapters/alpaca_paper_broker.py`). A preview-only client for one
provider's non-executing estimate endpoint exists and has no order,
cancellation or transfer method (`src/apex/adapters/coinbase_provider_preview.py`).
Neither can move real capital.

Data access is similarly bounded: public observation clients are
unauthenticated, GET-only, host-allowlisted, request- and byte-capped, with no
automatic retry; private read-only clients (when configured) use a fixed GET
allowlist with ephemeral request-bound authentication and redaction before
hashing or persistence.

## Enforcement

| Guarantee | Test |
|---|---|
| Layers cannot import across their boundary; domain is pure | `tests/security/test_architecture_boundaries.py` |
| Each HTTP-capable adapter may import only its exact allowed modules | same file, `*_IMPORT_ROOTS` constants and `test_typed_dependency_exceptions_are_exact_and_local` |
| No dynamic loading or future-capability packages | `test_runtime_has_no_dynamic_loading_or_future_capability_packages` |
| Only registered fixture IDs load; paths and URLs are rejected | `tests/security/test_no_live_path.py` (`test_cli_rejects_fixture_paths`) |
| CLI rejects live or execution modes and writes nothing | `test_cli_rejects_live_or_external_execution_modes` |
| Fixture tampering fails closed even after re-hashing | `tests/security/test_no_live_path.py` (many `*_after_rehash` tests) |
| New persistence code has no remote or live capability | `tests/persistence/test_postgres_security.py::test_new_persistence_code_contains_no_remote_or_live_execution_capability` |
| Live emergency stop blocks submissions without granting live | `tests/test_operational_reliability.py` |
| Live readiness and preflight stay non-executing | `tests/test_live_readiness.py`, `tests/test_live_preflight.py` |

## Fail-closed defaults

- The default investment mandate allocates zero capital and disallows every
  market family, so an absent mandate never reads as permissive
  (`InvestmentMandate.default_v1`, `src/apex/domain/investment_mandate.py`).
- Mandate evaluation rejects on any unknown fact rather than passing
  (`evaluate_mandate`).
- The risk kernel (`RiskKernel`, `src/apex/domain/decisions.py`) refuses to
  authorize unless the runtime capability is `SIMULATION_ONLY`, and its
  operational invariants (such as market-data freshness at authorization
  time) cannot be configured away by campaign policy.

## Fixtures and inputs are untrusted

Fixtures are data, never code. They are never executed, evaluated,
interpolated or rendered as templates, markup or prompts. The reader accepts
only registered IDs, verifies content hashes, rejects authoritative JSON
numbers and hidden execution fields, and checks cross-file bindings.
