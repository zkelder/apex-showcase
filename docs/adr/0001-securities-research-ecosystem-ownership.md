# ADR 0001: Securities research ecosystem ownership

- Status: Accepted
- Date: 2026-07-26
- Scope: Securities research, PAPER, canonical baselines, and simulator evidence

## Decision

Apex remains the authority for:

- immutable dataset, calendar, corporate-action, configuration, model, and
  signal lineage;
- research specification and Strategy Lab deployment identity;
- Campaign state, kill state, and every future capital/risk boundary;
- all financial truth, governance decisions, operator actions, and audit
  evidence;
- the canonical external-engine request/result protocol and final conformance
  classification.

The only broker-facing exception is a separately governed `EXTERNAL PAPER`
Operations tier. It can use one reviewed simulated PAPER host and separate
PAPER credentials for bounded account/order/position/activity evidence and
idempotent, operator-confirmed long-only instruction submission over the fixed
Securities universe. Exact Campaign, PaperTrial, policy, data, risk, and cash
bindings are necessary; Apex reserves and records before I/O, retains sanitized
immutable evidence, validates fills before applying financial effects, and
blocks on ambiguity or unknown activity. Broker state is evidence, not
automatic Apex financial truth. Setup, test, demo, and readiness paths submit
no order. LIVE hosts, credentials, account access, real-money state,
funding/transfers, configuration mutation, automatic escalation, and capital
increases remain prohibited.

LEAN may later run as an independent backtesting engine for exchange calendars,
corporate-action application, strategy execution, and order/fill simulation.
Its result is evidence for comparison. It cannot create or mutate Apex
Campaigns, risk reservations, orders, positions, settlements, journals,
governance, Strategy Lab deployments, or capital authority.

Stage 1 freezes a `LeanSecuritiesEngineAdapter` protocol and checks local runtime
availability. It does not start LEAN. Canonical Baselines later adds one pinned,
bounded vectorbt adapter over retained local bars. Vectorbt never downloads data
or receives database, provider, broker, or financial credentials. A
deterministic fixture runner proves protocol and conformance paths without being
represented as actual LEAN or vectorbt.

## Deferred ecosystem roles

The following projects have evaluation roles only and are not dependencies or
integrations in Stage 1:

| Project | Possible later evaluation | Explicit decision point |
| --- | --- | --- |
| Qlib | research dataset and model-workflow comparison | after managed data and conformance evidence are stable |
| vectorbt | bounded local portfolio implementation | integrated for canonical baseline comparison; output remains evidence |
| Optuna | bounded hyperparameter study | integrated through seeded ask/tell only after Apex freezes the family and owns every trial |
| MLflow | experiment metadata interoperability | integrated as a local mirror while Apex remains the authoritative lineage ledger |
| cvxportfolio / PyPortfolioOpt | portfolio research comparison | after a governed portfolio-research milestone |
| DVC | artifact transport/version comparison | when local managed artifact volume requires it |
| Feast | feature materialization comparison | only if offline/online parity becomes a concrete product need |

No project above may select a model automatically, promote a champion, retrain
continuously, allocate capital, or gain provider or execution access.

## Consequences

- Securities market data enters only through a bounded managed local root.
- Raw bars and corporate actions remain separate immutable evidence.
- Research SHORT is an analytical direction only.
- Apex computes and retains its own transparent baselines and metrics.
- Every simulator receives one hash-bound canonical request, and conformance
  covers signals, transactions, and results rather than final return alone.
- A material mismatch blocks authoritative Strategy Lab comparison but does not
  transfer authority or automatically reject a candidate.
- Every attempted baseline/simulator run remains a research trial, including
  unavailable and materially mismatched output.
- Bounded experiment families now predeclare research degrees of freedom and
  retain every attempted Strategy Lab, Securities, baseline, and simulator
  identity in an immutable ordinal ledger. Existing run records remain the
  authoritative result evidence.
- PSR, DSR, effective-trial, CSCV/PBO, stability, and concentration results are
  research-selection evidence only. A failed family-bound integrity evaluation
  blocks a new Strategy Lab eligibility decision but never promotes, deploys,
  or allocates capital.
- Bounded candidate generation reserves every trial in Apex, runs only the two
  existing managed model families at concurrency one, and treats Optuna state as
  subordinate. Local MLflow records carry `apex.authority = NONE`; losing the
  mirror cannot change Apex evidence, decisions, model identity, or authority.
- External discrepancies are classified as `CONFORMANT`,
  `EXPLAINED_DIFFERENCE`, `MATERIAL_MISMATCH`, `ENGINE_UNAVAILABLE`, or
  `INSUFFICIENT_COMPARABLE_OUTPUT`.
- The permanent operator warnings are
  `SECURITIES SHADOW — NO CAPITAL RESERVED` and
  `EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY`.
