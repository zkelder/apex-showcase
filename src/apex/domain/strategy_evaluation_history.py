"""Strategy Evaluation History & Promotion Lineage v1.

Durable, replay-safe evaluation and promotion-lineage evidence for the
strategies Apex's own recurring operating cycle evaluates every cycle --
closing the gap left after External Strategy Evaluation v1 (migration
``0109``) gave only OPERATOR-REGISTERED external strategies a durable
evaluation trail. Bounded archaeology for this milestone confirmed
``domain.strategy_definition.StrategyDefinition``/``domain.strategy_
evaluation.StrategyEvaluationResult`` remain entirely ephemeral for the
live recurring ``STRATEGY_PAPER_TRIAL_EXECUTION`` catalog: recomputed
fresh every dispatch from ``domain.strategy_benchmark.canonical_strategy_
benchmarks`` and never persisted anywhere -- so today Apex cannot durably
answer "why is this strategy in its current lifecycle state" for the one
catalog that actually places genuine PAPER trades.

A vocabulary caveat worth stating precisely, since this module's own name
and the milestone's brief both say "internal": every candidate in
``canonical_strategy_benchmarks`` is, by ``domain.strategy_definition.
StrategySourceKind``, labeled ``EXTERNAL`` (a published convention --
buy-and-hold, moving-average trend, time-series momentum, mean reversion).
There is no strategy anywhere in this codebase using ``StrategySourceKind.
INTERNAL``/``StrategyRuleKind.INTERNAL_METHOD_REFERENCE`` today (confirmed
by grep: zero callers of ``domain.strategy_evaluation.evaluate_internal_
strategy_from_health``/``evaluate_internal_strategy_from_counterfactual_
outcome`` outside this codebase's own test suite). "Internal" throughout
this module means "belongs to Apex's OWN recurring operating-cycle
catalog," as distinct from "OPERATOR-REGISTERED via the External Strategy
Evaluation v1 CLI/Console" -- an orthogonal axis to ``StrategySourceKind``,
not a synonym for ``StrategySourceKind.INTERNAL``. This module's own types
are named ``Internal...`` to mirror the milestone's own vocabulary and to
read unambiguously against the existing ``External...`` types, while this
docstring keeps the distinction from ``StrategySourceKind`` explicit.

Design relationship to migration ``0109`` (``domain.external_strategy``):
bounded archaeology found the existing ``external_strategy_definition``/
``external_strategy_evaluation`` tables intentionally scoped to
OPERATOR-REGISTERED strategies (composite PK keyed by a definition version
an operator explicitly registered via ``RegisterExternalStrategy``). The
recurring catalog's own ``StrategyDefinition`` objects are never registered
anywhere -- they are reconstructed byte-identically every cycle straight
from reviewed source code (``canonical_strategy_benchmarks``), always
``version=1``/``RESEARCH_ONLY`` (``StrategyDefinition.canonical_hash``
excludes ``created_at``, so re-calling ``canonical_strategy_benchmarks`` on
any day yields the identical four ``strategy_id``s). Reusing
``external_strategy_definition`` for these would conflate two genuinely
different governance/actor semantics an operator relies on staying
distinct (Part 8/14 of this milestone): an operator-registered strategy
the operator alone can promote, versus Apex's own autonomous recurring
catalog. So this module adds its own small, purpose-fit persistence
(``adapters.strategy_evaluation_history_postgres``) rather than writing
into ``external_strategy_*`` -- but reuses every EXISTING mechanism it
can: the real, unmodified ``domain.external_strategy.classify_
qualification``/``ExternalStrategyQualification`` (source-agnostic despite
living in that module -- it operates on any ``StrategyEvaluationResult``),
and the real, unmodified ``domain.strategy_evaluation.promote_to_paper_
eligible`` gate (see ``application.strategy_evaluation_history.
PromoteInternalStrategy``). One coherent conceptual model, two physically
distinct table pairs -- exactly the outcome the milestone brief asked for
when consolidation would force incompatible semantics together.

``InternalStrategyEvaluationRecord`` is one immutable, content-addressed
evaluation event: exactly which recurring-catalog strategy/version was
evaluated, against exactly which retained evidence window (``capture_id``
+ the day-scoped ``as_of`` the recurring cycle itself uses), under exactly
which named/versioned policy, producing exactly which candidate result and
lifecycle-qualification outcome. Deterministic identity (Part 6): folds in
``strategy_id``/``strategy_version``/``as_of``/``evaluation_policy_
version``/``candidate.canonical_hash`` -- the same "evidence content,
never wall-clock-only" discipline ``domain.external_strategy.build_
external_strategy_evaluation_record`` already uses, deliberately NOT
including ``cycle_id`` (operational run lineage, captured on the row but
never part of its identity -- two dispatches within the SAME UTC day
carry the SAME ``cycle_id`` already, per the recurring cycle's own
day-scoped ``cycle_id`` convention, so this would be redundant with
``as_of`` in the ordinary case and actively wrong in the pathological one:
identity must track evidence content, not which process happened to
compute it).

``InternalStrategyPromotionRecord`` is one immutable, EXPLICIT promotion
lineage event -- never written by the recurring cycle itself (see
``adapters.operating_cycle_tasks._execute_strategy_paper_trial``, which
never imports ``application.strategy_evaluation_history.
PromoteInternalStrategy``). The recurring cycle's own per-cycle in-memory
``promote_to_paper_eligible`` call (unchanged by this milestone) is a
GATE CHECK deciding whether a candidate may be traded THIS cycle -- it has
never durably mutated any strategy's lifecycle state (the reconstructed
``StrategyDefinition`` is discarded at the end of every dispatch), so
"would qualify" and "is currently being traded" can both be true while the
durable operational lifecycle state honestly remains ``RESEARCH_ONLY``
until an operator explicitly invokes ``PromoteInternalStrategy`` -- see
``current_lifecycle_state`` below and Part 14 of the milestone brief this
module implements (current state vs. latest recommendation must never be
conflated).

Authority ``NONE`` throughout. Nothing in this module can create an
Opportunity, Order, Position, PAPER trial, provider request, or LIVE
authority -- see ``application.strategy_evaluation_history``'s own module
docstring for the structural claim that neither service here ever imports
``application.strategy_paper_trial``/``application.crypto_paper``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from apex.domain.external_strategy import ExternalStrategyQualification, classify_qualification
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyLifecycleState,
    StrategyRuleKind,
    StrategySourceKind,
)
from apex.domain.strategy_evaluation import StrategyEvaluationResult
from apex.domain.values import content_digest, deterministic_id

STRATEGY_EVALUATION_HISTORY_WARNING = (
    "STRATEGY EVALUATION HISTORY -- IMMUTABLE EVIDENCE ONLY, NOT LIVE AUTHORITY"
)

# Versioned, named contracts -- matches the "named + versioned policy"
# convention this codebase already uses throughout (``EXTERNAL_STRATEGY_
# EVALUATION_POLICY_V1``, ``SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1``,
# ...). A genuine change to how a recurring-catalog candidate is evaluated
# or how an internal promotion is gated must bump these, never silently
# redefine the same version string in place.
INTERNAL_STRATEGY_EVALUATION_POLICY_V1 = "INTERNAL_STRATEGY_EVALUATION_POLICY_V1"
INTERNAL_STRATEGY_PROMOTION_POLICY_V1 = "INTERNAL_STRATEGY_PROMOTION_POLICY_V1"

# Part 15 (Freshness/stale evaluation): the recurring catalog's own
# evaluation identity is day-scoped (folds in ``as_of``, which the
# recurring cycle holds constant across every tick within one UTC day --
# see module docstring), so a genuinely healthy strategy mints at most one
# NEW retained evaluation row per UTC day, never one per 5-minute tick.
# Reuses the SAME "2x the evidence's own natural cadence" multiplier
# ``domain.operating_cycle.compute_paper_continuation_health`` already
# applies to reconciliation freshness (there, 2x the 5-minute tick
# cadence) -- applied here to THIS evidence's own actual cadence (once per
# UTC day) rather than that tick-level constant, which governs a
# genuinely different, every-tick write path (reconciliation), not this
# once-per-day evaluation-identity write path.
INTERNAL_STRATEGY_EVALUATION_FRESHNESS_WINDOW = timedelta(days=2)


class StrategyEvaluationHistoryError(ValueError):
    """A strategy evaluation-history or promotion-lineage record violated a
    determinism/evidence invariant. Fails closed."""


@dataclass(frozen=True, slots=True)
class InternalStrategyEvaluationRecord:
    """One immutable, content-addressed evaluation event for one recurring
    operating-cycle catalog candidate. See module docstring for full
    rationale, field-by-field mapping to milestone Part 5, and the
    deterministic-identity contract (Part 6)."""

    evaluation_id: str
    strategy_id: str
    strategy_version: int
    source_kind: StrategySourceKind
    venue: SharedVenue
    instrument_ids: tuple[str, ...]
    rule_kind: StrategyRuleKind
    cycle_id: str
    as_of: datetime
    capture_id: str
    evaluation_policy_version: str
    evaluated_at: datetime
    candidate: StrategyEvaluationResult
    qualification: ExternalStrategyQualification
    lifecycle_state_before: StrategyLifecycleState
    lifecycle_state_recommended_after: StrategyLifecycleState
    canonical_hash: str
    authority: str = "NONE"


def build_internal_strategy_evaluation_record(
    *,
    definition: StrategyDefinition,
    candidate: StrategyEvaluationResult,
    cycle_id: str,
    as_of: datetime,
    capture_id: str,
    evaluated_at: datetime,
) -> InternalStrategyEvaluationRecord:
    """Pure constructor. ``candidate`` must be the EXACT ``StrategyEvaluationResult``
    the caller already computed elsewhere (Part 7: "the durable record should
    reflect the exact result the recurring task actually used, not a second
    recomputation performed just for persistence") -- this function never
    re-runs a backtest or re-derives evidence, it only classifies and
    packages an already-computed result.

    ``lifecycle_state_before`` is read directly off ``definition`` (always
    ``RESEARCH_ONLY`` for today's recurring catalog -- see module
    docstring); ``lifecycle_state_recommended_after`` is derived from
    ``classify_qualification`` (the real, unmodified gate classifier this
    module reuses from ``domain.external_strategy``), never a separately
    re-implemented threshold.
    """

    if candidate.strategy_id != definition.strategy_id:
        raise StrategyEvaluationHistoryError(
            "candidate.strategy_id does not match the given definition"
        )
    if candidate.strategy_version != definition.version:
        raise StrategyEvaluationHistoryError(
            "candidate.strategy_version does not match the given definition"
        )
    if not cycle_id.strip():
        raise StrategyEvaluationHistoryError("cycle_id is required")
    if not capture_id.strip():
        raise StrategyEvaluationHistoryError("capture_id is required")

    qualification = classify_qualification(candidate)
    lifecycle_state_recommended_after = (
        StrategyLifecycleState.PAPER_ELIGIBLE
        if qualification is ExternalStrategyQualification.WOULD_QUALIFY_FOR_PAPER_ELIGIBLE
        else StrategyLifecycleState.RESEARCH_ONLY
    )
    # Deterministic identity (Part 6): strategy identity/version, policy
    # version, the day-scoped as_of cutoff, and the evidence content itself
    # (candidate.canonical_hash, which already transitively folds in every
    # signal/close price the backtest actually used -- see domain.strategy_
    # benchmark.StrategyBacktestResult.canonical_hash). Deliberately
    # excludes cycle_id/evaluated_at (operational run lineage, not evidence
    # identity -- see module docstring) and excludes capture_id from the
    # HASH specifically (candidate.canonical_hash already transitively
    # covers "which evidence" more precisely than a bookkeeping id would;
    # capture_id is retained on the row for lineage/query convenience only,
    # mirroring how domain.external_strategy folds capture_id into ITS OWN
    # identity because that module has no candidate.canonical_hash
    # equivalent binding it as tightly -- there, capture_id IS the evidence
    # anchor; here, candidate.canonical_hash already is).
    identity_payload: tuple[object, ...] = (
        definition.strategy_id,
        definition.version,
        as_of,
        INTERNAL_STRATEGY_EVALUATION_POLICY_V1,
        candidate.canonical_hash,
    )
    evaluation_id = deterministic_id("internal_strategy_evaluation", identity_payload)
    canonical_hash = content_digest(identity_payload)
    return InternalStrategyEvaluationRecord(
        evaluation_id=evaluation_id,
        strategy_id=definition.strategy_id,
        strategy_version=definition.version,
        source_kind=definition.source_kind,
        venue=definition.venue,
        instrument_ids=definition.instrument_ids,
        rule_kind=definition.rule_kind,
        cycle_id=cycle_id,
        as_of=as_of,
        capture_id=capture_id,
        evaluation_policy_version=INTERNAL_STRATEGY_EVALUATION_POLICY_V1,
        evaluated_at=evaluated_at,
        candidate=candidate,
        qualification=qualification,
        lifecycle_state_before=definition.lifecycle_state,
        lifecycle_state_recommended_after=lifecycle_state_recommended_after,
        canonical_hash=canonical_hash,
    )


@dataclass(frozen=True, slots=True)
class InternalStrategyPromotionRecord:
    """One immutable, EXPLICIT promotion lineage event -- see module
    docstring. Never written automatically; only
    ``application.strategy_evaluation_history.PromoteInternalStrategy``
    constructs one, and only in response to a direct caller-invoked action.
    """

    promotion_id: str
    strategy_id: str
    strategy_version: int
    evaluation_id: str
    lifecycle_state_before: StrategyLifecycleState
    lifecycle_state_after: StrategyLifecycleState
    promoted_at: datetime
    promotion_policy_version: str
    actor: str
    canonical_hash: str
    authority: str = "NONE"


def build_internal_strategy_promotion_record(
    *,
    strategy_id: str,
    strategy_version: int,
    evaluation_id: str,
    lifecycle_state_before: StrategyLifecycleState,
    lifecycle_state_after: StrategyLifecycleState,
    promoted_at: datetime,
    actor: str,
) -> InternalStrategyPromotionRecord:
    """Pure constructor. Deterministic identity (Part 6, extended to
    promotion the same way evaluation identity works): folds in
    ``strategy_id``/``evaluation_id``/the promotion policy version --
    re-invoking promotion against the SAME justifying evaluation is
    replay-safe (idempotent, never a duplicate contradictory row), while
    promoting against a DIFFERENT (later, fresher) evaluation_id for the
    same strategy_id genuinely mints a new, distinct promotion event --
    a legitimate re-affirmation trail, not a conflict (see ``current_
    lifecycle_state`` below: the LATEST promotion always governs)."""

    if not actor.strip():
        raise StrategyEvaluationHistoryError("actor is required")
    if not evaluation_id.strip():
        raise StrategyEvaluationHistoryError("evaluation_id is required")
    identity_payload: tuple[object, ...] = (
        strategy_id,
        evaluation_id,
        INTERNAL_STRATEGY_PROMOTION_POLICY_V1,
    )
    promotion_id = deterministic_id("internal_strategy_promotion", identity_payload)
    canonical_hash = content_digest(
        (*identity_payload, lifecycle_state_before.value, lifecycle_state_after.value)
    )
    return InternalStrategyPromotionRecord(
        promotion_id=promotion_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        evaluation_id=evaluation_id,
        lifecycle_state_before=lifecycle_state_before,
        lifecycle_state_after=lifecycle_state_after,
        promoted_at=promoted_at,
        promotion_policy_version=INTERNAL_STRATEGY_PROMOTION_POLICY_V1,
        actor=actor,
        canonical_hash=canonical_hash,
    )


def current_lifecycle_state(
    promotions: Sequence[InternalStrategyPromotionRecord],
) -> StrategyLifecycleState:
    """The durable OPERATIONAL lifecycle state for one recurring-catalog
    strategy_id (Part 14): ``RESEARCH_ONLY`` unless at least one explicit
    promotion event is retained, in which case the MOST RECENT promotion's
    own ``lifecycle_state_after`` -- entirely derived from this append-only
    promotion history, never a second mutable state column anywhere. Never
    conflated with ``InternalStrategyEvaluationRecord.
    lifecycle_state_recommended_after`` (what CURRENT EVIDENCE suggests,
    which may legitimately differ -- e.g. a strategy explicitly promoted on
    strong historical evidence whose latest evaluation now recommends
    RESEARCH_ONLY; this module never auto-demotes, matching Part 8's
    instruction not to invent demotion semantics the codebase does not
    already support)."""

    if not promotions:
        return StrategyLifecycleState.RESEARCH_ONLY
    latest = max(promotions, key=lambda item: (item.promoted_at, item.promotion_id))
    return latest.lifecycle_state_after


class InternalStrategyEvaluationFreshness(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    NEVER_EVALUATED = "NEVER_EVALUATED"


def evaluate_freshness(
    latest_evaluated_at: datetime | None, *, now: datetime
) -> InternalStrategyEvaluationFreshness:
    """Part 15: distinguish freshly-evaluated / stale / never-retained.
    ``latest_evaluated_at`` is ``None`` when no ``InternalStrategyEvaluationRecord``
    is retained for a strategy_id at all (never conflated with a strategy
    that HAS retained history but hasn't been re-evaluated in a while)."""

    if latest_evaluated_at is None:
        return InternalStrategyEvaluationFreshness.NEVER_EVALUATED
    if now - latest_evaluated_at <= INTERNAL_STRATEGY_EVALUATION_FRESHNESS_WINDOW:
        return InternalStrategyEvaluationFreshness.FRESH
    return InternalStrategyEvaluationFreshness.STALE
