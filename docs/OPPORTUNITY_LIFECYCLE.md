# Opportunity Lifecycle

*Verified against migration head `0103`, primary `main` commit `f8eac3e` (2026-08-19); reconciled against migration head `0104`, primary `main` commit `052e956` (2026-08-22) — see the new "Venues" section below. Reconciled again against migration head `0106`, primary `main` commit `c3f110f` (2026-09-01) — nothing in this file's eligibility/ranking/reason-code model changed; `SharedOpportunityProjection.paper_eligible` (consumed downstream by the strategy-PAPER path) is a pre-existing field, not new. See PAPER Operations & Observability v1 and [System Mental Model](SYSTEM_MENTAL_MODEL.md#strategy-evaluation-and-paper-execution) for that downstream consumer. Source: `src/apex/domain/shared_opportunity.py`. See docs/README.md for the full documentation index.*

## Candidate vs. Opportunity

Apex's codebase uses "candidate" in **two unrelated senses** — worth being precise about, since conflating them is an easy mistake:

1. **An opportunity candidate** — a provider/venue-normalized projection of some underlying strategy or scan's output (a `SharedOpportunityProjection`), before it has cleared the eligibility gate. This is the sense used throughout this document.
2. **A search candidate** — in `candidate_generation.py`, a bounded ML hyperparameter proposal for Strategy Lab's model training (Optuna-style). Unrelated to opportunity evaluation.

A `SharedOpportunityProjection` becomes a real **Opportunity** — i.e., something the ranking/mandate/portfolio machinery will actually consider — only once it clears the eligibility gate below.

## Venues

Three venues feed this lifecycle today: `CRYPTO`, `KALSHI`, and (added migration `0104`) `SECURITIES` — Apex's defined-risk options-structure economics research, bridged in without any new scanner, strategy, or execution surface. All three normalize into the identical `SharedOpportunityProjection` shape and pass through the same eligibility/ranking/counterfactual pipeline below; `SECURITIES` is a new source feeding this pipeline, not a special case inside it. `SECURITIES` projections carry no comparable-value/probability model, so by construction they can reach `ELIGIBLE` but never clear the ranking policy's genuine-EV gate (see Ranking, below) — they stay `RESEARCH_ONLY`, never `ACTIONABLE`. Independently, the active `InvestmentMandate` does not permit securities execution, so a `SECURITIES` opportunity cannot reach a live or paper allocation from the mandate side either.

## The eligibility gate

```
SharedOpportunityProjection --> evaluate_eligibility() --> EligibilityState
```

`EligibilityState` has exactly four members:

| State | Meaning |
|---|---|
| `ELIGIBLE` | Cleared every check; tagged `ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY` — eligibility is not a claim of edge |
| `REJECTED` | Evaluated, and priced/quality out on a specific reason (evidence class, staleness, cost/liquidity/loss/capital completeness, or a disqualifying warning) |
| `EXPIRED` | The projection's own time boundary passed before it could be acted on |
| `BLOCKED_MISSING_EVIDENCE` | A genuine evidence gap — the projection could not even be evaluated |

**`REJECTED` and `BLOCKED_MISSING_EVIDENCE` look similar from a distance ("no opportunity") but mean very different things.** `REJECTED` means the gate had what it needed and made a call. `BLOCKED_MISSING_EVIDENCE` means the gate didn't have enough to call at all — a genuine capture/coverage gap worth investigating. See "What 'no opportunity' actually means" below.

Four **disqualifying warnings** force rejection regardless of any other component: `SOURCE_DATA_INVALID`, `SOURCE_LINEAGE_INVALID`, `LIQUIDITY_INSUFFICIENT`, `UNKNOWN_SOURCE_AUTHORITY`.

## Ranking

Only `ELIGIBLE` projections are passed into `prioritize()`, a 10-component weighted score:

| Component | Weight |
|---|---|
| Comparable value | 0.25 |
| Downside | 0.15 |
| Cost | 0.15 |
| Liquidity | 0.10 |
| Lockup | 0.10 |
| Freshness | 0.05 |
| Evidence | 0.05 |
| Uncertainty | 0.05 |
| Source health | 0.05 |
| Statistical qualification | 0.05 |

The output is a `PriorityResult` carrying a `PriorityTier`:

- `ACTIONABLE` — cleared the opportunity-cost floor on **genuine** economic magnitude (not a proxy estimate)
- `RESEARCH_ONLY` — remains fully visible and ranked, but magnitude was a proxy, missing, or at/below the floor

Tier is explanatory, **never** a filter: "orthogonal to rank/score... never reorders or deletes." The default and fail-closed value is `RESEARCH_ONLY` — persistence never defaults a missing value to `ACTIONABLE`.

A `SharedOpportunitySet` rolls everything up: every `ELIGIBLE` projection, ranked and scored; every `expired_projection_ids` and `unavailable_projection_ids` (= `BLOCKED_MISSING_EVIDENCE`) tracked separately; and a human-readable `OpportunityExplanation` generated even for candidates that were *not* prioritized, e.g. *"Not prioritized: state=...; reasons=...; no hypothetical allocation."*

## Reason codes

Every rejection carries an origin-specific raw reason code, preserved verbatim forever (`CounterfactualDecision.raw_reason_codes`). A separate, versioned interpretation layer normalizes these into a bounded **12-family taxonomy** (`ReasonFamily`, in `counterfactual_classification.py`) for cross-origin comparison — this is a derived read, never a replacement for the raw codes:

`EVIDENCE_INCOMPLETE` · `STALE` · `LIQUIDITY` · `ECONOMICS` · `RESEARCH_CONFIDENCE` · `MANDATE` · `RISK` · `CAPACITY` · `COMPETITIVE_SELECTION` · `PROVIDER_UNAVAILABLE` · `OPERATIONAL_FAILURE` · `OTHER_EXPLICIT`

Two frozen classifier versions exist (V1, V2); an unrecognized raw code always falls through to `OTHER_EXPLICIT` rather than being force-fit into a family — "new/unrecognized codes are never silently mis-classified into a false-precision family."

## Scarcity

There is no dedicated "scarcity" enum. Competitive loss is expressed through the `COMPETITIVE_SELECTION` reason family, e.g. the raw code `OUTSIDE_TOP_RANKED_BOUND` (crypto's fixed top-3 slate). A 2026-08-19 retained-evidence audit of 252 Kalshi decisions found that most apparent scarcity was **not** a missing-evidence problem: only 28% (binary-complement) and 7% (threshold-bundle) of zero-candidate cases were genuinely empty-book — the rest reflected markets that were already efficiently priced (full depth on both sides, top-of-book already at or inside breakeven). A subsequent fix to Kalshi's binary-complement candidate selection (activity-ranked instead of alphabetical, wider discovery breadth) measured up to a ~183x improvement in the aggregate open interest of selected candidates on retained-corpus replay. This is cited as a dated illustration of how "scarcity" gets diagnosed in practice, not a live guarantee about current candidate volume.

## What "no opportunity" actually means

A cycle producing zero `ACTIONABLE` opportunities — or even zero `ELIGIBLE` opportunities — is not automatically a problem. Distinguish:

- **Zero because `REJECTED`** — the gate evaluated real evidence and correctly declined it (e.g., the market was already efficiently priced, cost/liquidity didn't clear the bar). As of 2026-08-19, this was the dominant, expected state for CRYPTO/KALSHI specifically: essentially all currently-evaluated crypto/Kalshi opportunities were genuinely `REJECTED`, and across 18 observed operating cycles only one opportunity had ever reached `ELIGIBLE` there (a smoketest fixture, not organic evidence). Since SECURITIES joined this lifecycle (migration `0104`, see [Venues](#venues) above), that specific illustration no longer generalizes to the whole pipeline: SECURITIES projections reach `ELIGIBLE` routinely, by construction, because the source has no probability model to fail a quality check on — they simply stay `RESEARCH_ONLY` rather than `ACTIONABLE`. **Both patterns reflect a conservative, working-as-intended gate, not a broken pipeline.**
- **Zero because `BLOCKED_MISSING_EVIDENCE`** — a real coverage/capture gap. This is the state worth investigating; see Troubleshooting.
- **Zero because `EXPIRED`** — evidence arrived, but too late to act on. Worth watching if it happens repeatedly (possible cadence/timing issue), not alarming as an isolated event.

See the Operator Guide for the operator-facing version of this same guidance.
