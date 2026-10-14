# Authority and Safety Model

*Verified against migration head `0103`, primary `main` commit `f8eac3e` (2026-08-19); reconciled against migration head `0104`, primary `main` commit `052e956` (2026-08-22) — the SECURITIES venue bridge added in that migration introduces zero new authority anywhere in the diff (confirmed against its own commit message), so nothing in this file's model changed. Reconciled again against migration head `0106`, primary `main` commit `c3f110f` (2026-09-01) — Live Market Observation and the new strategy-PAPER path (see "Summary posture" below) likewise introduce zero new authority ceiling: every row either path produces is still `authority = "NONE"` at the domain-decision level or the pre-existing `"PAPER_ONLY"` at the ledger/account level; the model below is otherwise unchanged. Sources: `src/apex/domain/portfolio_intelligence.py`, `live_readiness.py`, `live_preflight.py`, `decisions.py`, `investment_mandate.py`, and a repo-wide static search of `src/apex/` for order-placement code paths. Reconciled again against migration head `0111` (2026-09-03, Repeatable Strategy PAPER Evidence v1 — commit lands with this same reconciliation note, so its own hash cannot be self-quoted here; see the migration-0111 commit on `main` for the exact revision) — the new bounded, repeatable trial-generation model (including its first genuine exercise of the existing, previously-unused SELL order path) likewise introduces zero new authority ceiling: both new tables carry a DB-level `CHECK (authority = 'NONE')` constraint, and the SELL path reuses the identical `domain.crypto_paper` order machinery, `"PAPER_ONLY"`/`"NONE"` fields unchanged, that the pre-existing BUY path already used. See docs/README.md for the full documentation index.*

The single most important thing to understand about Apex: **research autonomy and capital authority are two separate systems, gated independently.** The recurring operating cycle can run itself indefinitely, generate opportunities, rank them, and evaluate them against a mandate — all without ever touching capital authority, because the domain layer that does all of that has no capital-authority concept wired into it at all. A completely different, narrower subsystem stands between any research finding and real money, and today that subsystem has never been armed.

## Two authority mechanisms exist — know both

**1. The pervasive sentinel** (used almost everywhere): nearly every record Apex's domain logic produces carries a plain field — `authority`, `capital_authority`, `financial_authority`, or similar — and it is **hardcoded to the literal string `"NONE"`** at every construction site across the domain layer (`CounterfactualDecision`, `SharedOpportunityProjection`, `InvestmentMandate`, `MandateEvaluation`, `PortfolioSnapshot`, `OperatingCycleRun`, and dozens more). This is the dominant pattern: the overwhelming majority of what Apex computes is, by construction, incapable of representing anything but `NONE`.

**2. The named ladder** (`AuthorityCeiling`, in `portfolio_intelligence.py`, scoped to that module's research-portfolio normalization):

```
NONE  →  RESEARCH_ONLY  →  SHADOW_ONLY  →  PAPER_REVIEW_ONLY  →  PAPER  →  LIVE_REVIEW_ONLY  →  LIVE
```

An opportunity's contribution to a permitted research allocation is zeroed out whenever its `authority_ceiling` is `NONE`, `RESEARCH_ONLY`, or `SHADOW_ONLY`, or whenever it carries any blocker at all.

Neither mechanism is itself what stands between Apex and a live order. That's a third, separate subsystem:

## The actual live gate: `ExecutionControlState`

```
DISABLED  →  PAPER_ONLY  →  LIVE_BLOCKED  →  LIVE_PILOT_ARMED  →  EMERGENCY_STOP
```

This state lives in `live_readiness.py` and is the operator-controlled switch that a live risk evaluation checks. `evaluate_live_risk()` blocks with `LIVE_PILOT_NOT_ARMED` unless the state is *exactly* `LIVE_PILOT_ARMED` — and no live code path today sets it there; it is reachable only from isolated test fixtures. A companion `ReadinessRecommendation` (`LIVE_NOT_READY`, `LIVE_REVIEW_REQUIRED`, `LIVE_PILOT_ELIGIBLE`, `LIVE_SUSPENDED`) and `PreflightRecommendation` (topping out at `READY_FOR_LIVE_REVIEW` — a human review gate, not an execution gate) sit alongside it. As of this writing, genuine state is `LIVE_NOT_READY` / `LIVE_BLOCKED` everywhere it has been checked.

A `LiveRiskPolicy` with roughly 30 distinct blocker checks exists and is exercised in tests — but it gates a door (`LIVE_PILOT_ARMED`) that nothing in live operation currently opens.

## Provider write authority

Credential-backed provider access is gated by its own ladder, `PermissionClass`: `PUBLIC_DATA` → `PRIVATE_VIEW` → `ORDER_PREVIEW` → `TRADE` → `TRANSFER` → `WITHDRAWAL`. The load-bearing design principle, quoted directly from `docs/ARCHITECTURE.md`: **"authentication alone never implies a dangerous permission."** Having valid credentials for a provider does not, by itself, grant Apex any ability to trade, transfer, or withdraw — each permission class must be independently and explicitly established.

## NONE means NONE — the concrete enforcement points

This isn't just a naming convention; several places in the code actively refuse to proceed without it:

- `RiskKernel.evaluate()` (`decisions.py:527`) refuses to authorize anything unless `operational_invariants.runtime_capability == "SIMULATION_ONLY"` — the entire strategy/allocation/risk pipeline is inert outside simulation.
- `InvestmentMandate.default_v1()` is explicitly the "conservative, zero-capital, nothing-permitted" starting mandate — *"Apex genuinely holds zero user-allocated capital today. This default keeps every mandate evaluation `REJECTED` until an operator explicitly authors a mandate with real values."*
- `evaluate_mandate()` fails closed on any unknown fact (unresolved exposure/cash figures always become a rejection reason, never a silent pass).
- The systemd worker's `ExecStart` runs with a hardcoded authority dict — `capital_authority`, `order_authority`, `broker_authority`, `financial_mutation_capability` all `NONE`, `live_eligible: False` — baked into the one command that runs unattended, not something an operator has to remember to pass.
- Every module in the counterfactual/follow-up/outcome/mandate/portfolio/opportunity family carries its own explicit warning constant (e.g. `MANDATE_WARNING = "MANDATE EVALUATION IS RESEARCH/PAPER GUIDANCE ONLY — NO ORDER OR LIVE AUTHORITY"`, `SHARED_OPPORTUNITY_WARNING`, `PORTFOLIO_STATE_WARNING`, `LIVE_READINESS_WARNING`, `LIVE_PREFLIGHT_WARNING`) — this is a deliberate, repeated pattern across the codebase, not a one-off disclaimer.

## Observation can continue while execution remains forbidden

This is the system's founding design principle, stated in the very first lines of the root `README.md`: *"Observation may be broad; execution authority remains narrow, explicit, and separately governed."* Nothing about running the recurring cycle, generating opportunities, ranking them, evaluating a mandate, or accumulating counterfactual/outcome evidence requires — or grants — any capital authority. Apex can be, and today is, maximally active on the research side while being maximally restricted on the capital side.

## The one nuance worth being precise about

A repo-wide static search of `src/apex/` for order-placement functions (`place_order`, `submit_order`, `create_order` definitions) and for any *live* (non-paper, non-preview) provider host found no live order-execution code path. Two things came up that a careful reviewer should know about exactly, rather than have summarized away:

- **`AlpacaPaperBroker`** (`src/apex/adapters/alpaca_paper_broker.py`) does perform a real `POST /v2/orders` HTTP call — but hardcoded to `https://paper-api.alpaca.markets` (Alpaca's own sandbox). Alpaca's live host (`api.alpaca.markets`) does not appear anywhere in the codebase. This is wired into real dev-tooling commands (`alpaca-paper-submit`, etc.) and is the one order-submission path in the whole system that leaves the local process and hits a real external provider's order endpoint — against that provider's own paper environment, not real capital.
- **`ApprovedCoinbasePreviewClient`** (`src/apex/adapters/coinbase_provider_preview.py`) makes a genuine authenticated request to Coinbase's real production host, but to Coinbase's own **non-executing preview/estimate** endpoint (`/orders/preview`), not an order-placement endpoint. Its own docstring states it is "never an order client," and it is not wired into any CLI command found in this audit.

Neither path can move real capital. Both are named here explicitly because "no live path exists" is a stronger and more checkable claim than "everything is simulated," and a technically sophisticated reviewer should be able to verify it themselves at these two file paths.

## Summary posture

| Layer | Current state |
|---|---|
| Research / evidence / ranking / mandate | Fully active, authority `NONE` throughout, runs unattended |
| Internal PAPER ledgers (crypto, Kalshi) | Implemented, Postgres-only, no external effect. The crypto ledger now carries two materially distinct accounts — an original cross-venue path (cash-only, zero positions to date) and a newer strategy-driven path (`STRATEGY_PAPER_TRIAL_EXECUTION`, one genuine open position) — see [Product Overview](PRODUCT_OVERVIEW.md#precision-notes-as-requested-by-this-documentation-pass). Neither reaches beyond Postgres. |
| External paper broker (Alpaca sandbox) | Implemented, real external calls, sandbox-only |
| Coinbase preview client | Implemented, real authenticated call, non-executing, not wired into any command |
| `LIVE_PILOT_ARMED` | Reachable only from isolated test fixtures — never from live operation |
| Live order execution | No code path found anywhere in `src/apex/` |
