"""Repeatable Strategy PAPER Evidence v1: pure domain types and decision
functions for a BOUNDED, REPEATABLE strategy-driven PAPER trial lifecycle --
the mechanism that replaces the prior "at most one lifetime trial per
strategy/instrument" dead-end (``domain.strategy_paper_trial.
recurring_strategy_paper_trial_seed``'s own documented limitation) with an
explicit, evidence-oriented model that can accumulate n>1 bounded PAPER
outcome observations for the same qualifying strategy/instrument over
elapsed time.

This module owns zero I/O and imports nothing from ``apex.application``/
``apex.adapters`` -- matching every other pure-decision domain module in
this codebase (``domain.schema_compatibility``, ``domain.
counterfactual_followup``'s ``determine_followup_due_state``). It DOES
import ``domain.crypto_paper`` (for ``adopt_legacy_trial``'s two input
types) and ``domain.values`` -- both ordinary domain-to-domain imports, the
same layering ``domain.strategy_paper_trial`` (this module's sibling)
already uses.

## What this module deliberately does NOT do

It never authorizes, constructs, previews, submits, fills, or reconciles a
PAPER order -- that remains exclusively ``domain.crypto_paper``'s existing,
unmodified, side-symmetric (BUY/SELL) machinery, driven by
``application.strategy_paper_trial.ExecuteStrategyPaperTrial.place_order``/
``.close_position``. This module answers only three narrow, pure questions
an orchestrator (``adapters.operating_cycle_tasks._execute_strategy_paper_
trial``) needs to drive that machinery correctly and safely:

1. What is this repeated trial's own deterministic identity, distinct from
   the strategy definition's identity, the instrument's identity, and the
   account's identity (Part 5A of this milestone's brief)?
2. Has an open trial reached its bounded PAPER evidence horizon yet
   (``evaluate_trial_horizon``)?
3. Is a new trial (the next generation) allowed to begin yet, given the
   prior generation's terminal outcome and cooldown (``evaluate_retrial_
   eligibility``)?

## Trial identity (Part 5A)

A ``StrategyPaperTrialRecord.trial_id`` folds in exactly ``(strategy_id,
strategy_version, instrument_id, generation)`` -- the same three
identity components ``recurring_strategy_paper_trial_seed`` already uses,
plus one new integer, ``generation``, that seed deliberately excluded (see
that function's own docstring: "Continuous re-signaling ... would need a
richer per-signal identity"). This is that richer identity. It remains
distinct from the strategy definition's own identity (``StrategyDefinition.
strategy_id`` folds in no generation), the bare instrument id, and the
account id (a process-wide constant, never part of any identity payload in
this whole subsystem -- see ``domain.strategy_paper_trial.
recurring_strategy_paper_trial_seed``'s own identical convention). Folding
in ``generation`` rather than a wall-clock timestamp keeps the id
deterministic and idempotent under scheduler replay: the SAME generation
number, computed the SAME way (``next_trial_generation``, a pure function
of already-retained trial rows), always produces the SAME ``trial_id`` --
never a fresh one just because a replay happened on a different tick.

## Bounded evidence horizon, not a strategy redefinition (Part 6)

``StrategyPaperTrialRecord.horizon_days``/``.horizon_target_at`` are a
PAPER-EVIDENCE-LAYER concept, deliberately never written onto
``StrategyDefinition.rule_parameters.maximum_holding_days`` (BUY_AND_HOLD's
own canonical horizon stays ``DAILY_SIGNAL_INDEFINITE_HOLD``/``None``,
untouched, forever -- see ``domain.strategy_definition``'s own validation,
which forces exactly this for ``StrategyRuleKind.BUY_AND_HOLD``). The
distinction stays explicit everywhere this module is used: the CANONICAL
STRATEGY says "buy and hold indefinitely"; the PAPER EVIDENCE TRIAL says
"observe one bounded window of that indefinite signal, then close it and
start measuring the next window." ``STRATEGY_PAPER_TRIAL_HORIZON_DAYS``
mirrors ``domain.strategy_definition.MINIMUM_PROMOTION_SAMPLE_SIZE`` (30)
-- both are Apex's own existing judgment about how many days of evidence
constitute one meaningful observation window -- rather than inventing an
unrelated new constant.

## Terminal reason boundary (Part 6/8)

``StrategyPaperTrialTerminalReason`` has exactly two members today, both
in scope for THIS milestone (neither is the reserved future boundary
described below): ``TRIAL_HORIZON_EXIT`` (the trial genuinely acquired PAPER
exposure and this milestone's own bounded evidence window elapsed -- closed
through a real SELL) and ``OPENING_ORDER_EXPIRED_UNFILLED`` (the trial's
OWN opening BUY order expired before ever filling -- see ``domain.
crypto_paper.expire_paper_order``'s existing, unmodified 4-hour-validity
mechanics -- so no PAPER exposure was ever acquired and there is nothing to
sell; the trial closes with no economic outcome, ``realized_paper_pnl=
None``, and ``closing_authorization_id``/``closing_order_id`` equal to its
OWN ``opening_authorization_id``/``opening_order_id`` rather than a new
authorization/order that would never legitimately exist). Without this
second reason, a trial whose opening order simply never filled would stay
"active" forever (no closure row could ever be honestly written for it),
permanently blocking every later generation for that slot -- a genuine
correctness requirement of Part 5F ("the same strategy/instrument may begin
another trial"), not an optional enhancement. Neither reason produced by
this milestone is ``STRATEGY_SIGNAL_EXIT`` (a genuine strategy-generated
exit signal) -- a later milestone (Signal-Driven PAPER Position Management
v1, if and when the current recurring-cycle open-position signal-
reevaluation gap this milestone's own investigation found -- see ``docs/
REPEATABLE_STRATEGY_PAPER_EVIDENCE_V1.md`` -- is closed) adds that as a
THIRD member, plus the matching migration/adapter CHECK-constraint value --
the exact 3-place "add one allow-listed value" idiom ``domain.strategy_
evaluation_history``'s own ``_LIFECYCLE_STATES``/``_QUALIFICATIONS`` already
established. Every caller of this module already branches on the enum
value, never on "did a closure exist" alone, so that addition is additive,
never a silent behavior change here.

## Cooldown, not proof of independence (Part 5E)

``StrategyPaperTrialClosureRecord.next_eligible_at`` enforces the smallest
explicit separation needed to stop a same-cycle or immediately-following-
cycle close-then-reopen loop -- ``STRATEGY_PAPER_TRIAL_COOLDOWN_DAYS = 1``,
mirroring ``domain.counterfactual_followup.SHORT_HORIZON_DAYS``'s identical
"smallest day-scoped unit that survives a same-day multi-tick replay"
convention (``_execute_strategy_paper_trial`` re-enters many times across
one UTC day via ``awaits_intraday_continuation``, but its own ``as_of``
stays constant across that whole day -- a 1-day cooldown is therefore
already enough to guarantee no same-day reopen, without inventing a
sub-day wall-clock timer hidden in adapter code). This is OPERATIONAL
separation between trials, not a claim that successive trials are
statistically independent samples -- see ``STRATEGY_PAPER_TRIAL_LIFECYCLE_
WARNING`` below and ``docs/REPEATABLE_STRATEGY_PAPER_EVIDENCE_V1.md``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.crypto_paper import (
    CryptoPaperAuthorization,
    CryptoPaperOrder,
    CryptoPaperPreview,
    PaperOrderState,
)
from apex.domain.strategy_paper_trial_evidence_coverage import TrialEvidenceCoverage
from apex.domain.values import content_digest, deterministic_id

STRATEGY_PAPER_TRIAL_LIFECYCLE_WARNING = (
    "STRATEGY PAPER TRIAL LIFECYCLE -- BOUNDED, REPEATABLE PAPER EVIDENCE "
    "ONLY; A TRIAL HORIZON CLOSE IS NOT A STRATEGY-GENERATED SIGNAL, AND "
    "REPEATED TRIALS ARE OPERATIONALLY SEPARATED, NEVER CLAIMED STATISTICALLY "
    "INDEPENDENT; NO LIVE AUTHORITY"
)

#: Named, versioned policy identity -- retained on every record this module
#: builds, matching the codebase-wide "every evidence row names the policy
#: that produced it" convention (``INTERNAL_STRATEGY_EVALUATION_POLICY_V1``,
#: ``COUNTERFACTUAL_OUTCOME_POLICY_V2``, ...). Bump the trailing version
#: only if the horizon/cooldown/identity RULES themselves change; retained
#: rows always keep the policy version that was actually in force when they
#: were built (never rewritten).
STRATEGY_PAPER_TRIAL_LIFECYCLE_POLICY_V1 = "STRATEGY_PAPER_TRIAL_LIFECYCLE_POLICY_V1"

#: See module docstring "Bounded evidence horizon" section for why this
#: mirrors ``domain.strategy_definition.MINIMUM_PROMOTION_SAMPLE_SIZE``
#: rather than an unrelated new number.
STRATEGY_PAPER_TRIAL_HORIZON_DAYS = 30

#: See module docstring "Cooldown" section.
STRATEGY_PAPER_TRIAL_COOLDOWN_DAYS = 1


class StrategyPaperTrialLifecycleError(ValueError):
    """A trial-lifecycle input violated an evidence/identity invariant.
    Fails closed -- never silently coerces or guesses."""


class StrategyPaperTrialTerminalReason(StrEnum):
    """See module docstring "Terminal reason boundary" section. Two members
    today, neither a strategy-generated signal -- do not assume every
    future value is time-based, or that every closure represents genuine
    realized PAPER exposure, when branching on this in new code."""

    TRIAL_HORIZON_EXIT = "TRIAL_HORIZON_EXIT"
    OPENING_ORDER_EXPIRED_UNFILLED = "OPENING_ORDER_EXPIRED_UNFILLED"


@dataclass(frozen=True, slots=True)
class StrategyPaperTrialRecord:
    """One immutable row per genuinely distinct bounded PAPER trial
    generation for one ``(strategy_id, strategy_version, instrument_id)``
    slot. Written exactly once, at open time -- never mutated; a trial's
    later closure is a SEPARATE record, ``StrategyPaperTrialClosureRecord``,
    referencing this one by ``trial_id`` (mirrors ``domain.strategy_
    evaluation_history``'s own evaluation/promotion two-table split: an
    open-ended stream of one kind of immutable fact, plus a second,
    narrower immutable-event table for the one further thing that can
    happen to it).

    ``opening_realized_pnl_baseline`` exists because ``domain.crypto_paper.
    CryptoPaperPosition.realized_paper_pnl`` is CUMULATIVE across that
    position row's entire lifetime -- and a position row is reused/reopened
    across trial generations for the same ``(account_id, product_id)``
    (``apply_retained_evidence`` reopens a ``"CLOSED"`` position on the next
    BUY rather than minting a new ``position_id``), so it is NOT, by
    itself, one generation's own realized outcome. A caller captures this
    field from the position's own ``realized_paper_pnl`` at the EXACT
    moment this trial opens (``Decimal("0")`` the first time a product is
    ever traded); at close time, this generation's OWN realized PAPER P&L
    is ``position.realized_paper_pnl (at close) - opening_realized_pnl_
    baseline`` -- see ``adapters.operating_cycle_tasks`` for where this
    subtraction actually happens."""

    trial_id: str
    strategy_id: str
    strategy_version: int
    instrument_id: str
    account_id: str
    generation: int
    opened_at: datetime
    opening_authorization_id: str
    opening_order_id: str
    opening_realized_pnl_baseline: Decimal
    horizon_days: int
    horizon_target_at: datetime
    cooldown_days: int
    policy_version: str
    legacy_adopted: bool
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class StrategyPaperTrialClosureRecord:
    """One immutable row per genuinely distinct trial closure event. At
    most one ever exists per ``trial_id`` (enforced at the persistence
    layer by a UNIQUE constraint on ``trial_id`` -- see ``migrations/
    versions/0111_strategy_paper_trials.py`` -- so "does a closure
    row exist for this trial_id" is always an unambiguous, race-safe
    active/terminal test, never a "most recent of several" resolution).

    Trial Evidence Coverage Disclosure v1: the ``coverage_*`` fields are a
    flattened ``domain.strategy_paper_trial_evidence_coverage.
    TrialEvidenceCoverage`` (see that module for what each one means and
    how it is computed) -- flattened rather than nested to match this
    record's own existing all-scalar-fields shape (mirrors ``adapters.
    strategy_paper_trial_lifecycle_postgres``'s explicit "plain scalar
    columns, no JSON payload" idiom). Purely descriptive: nothing anywhere
    in this module reads them back to make a decision -- see
    ``build_strategy_paper_trial_closure_record``'s own docstring.
    Deliberately NOT a complete flattening: ``TrialEvidenceCoverage`` has no
    ``material_gap`` field to flatten in the first place -- see that
    module's own "Materiality is derived, never persisted" section for why
    an interpretive threshold judgment stays out of this immutable record
    entirely, derived only at read-model/display time instead."""

    closure_id: str
    trial_id: str
    strategy_id: str
    instrument_id: str
    closed_at: datetime
    terminal_reason: StrategyPaperTrialTerminalReason
    closing_authorization_id: str
    closing_order_id: str
    realized_paper_pnl: Decimal | None
    next_eligible_at: datetime
    coverage_window_duration_seconds: int
    coverage_nominal_days: int
    coverage_covered_days: int
    coverage_qualifying_observation_count: int
    coverage_largest_gap_seconds: int
    coverage_policy_version: str
    canonical_hash: str
    authority: str = "NONE"


def strategy_paper_trial_id(
    *, strategy_id: str, strategy_version: int, instrument_id: str, generation: int
) -> str:
    """The deterministic identity of one trial generation -- see module
    docstring "Trial identity" section. Pure function of already-known
    facts; a caller re-derives the identical id on every scheduler replay."""

    return deterministic_id(
        "strategy_paper_trial_generation",
        (strategy_id, strategy_version, instrument_id, generation),
    )


def promoted_trial_slot_version(*, definition_version: int) -> int:
    """The ``strategy_version`` a trial slot uses once a freshly
    reconstructed recurring-catalog candidate (always ``version=1``,
    ``RESEARCH_ONLY`` -- see ``domain.strategy_benchmark.
    canonical_strategy_benchmarks``) is promoted. ``domain.strategy_
    definition.next_version`` always increments ``version`` by exactly 1
    (``RESEARCH_ONLY`` exists only at ``version=1``; the very next state,
    ``PAPER_ELIGIBLE``, is therefore always ``version=2``) -- a pure,
    deterministic fact about that promotion path, not a guess, and not
    dependent on THIS cycle's own promotion actually succeeding.

    Two independent callers need the IDENTICAL value and must never drift
    apart: ``adapters.operating_cycle_tasks._execute_strategy_paper_trial``
    (computed for every canonical candidate, including ones that do not
    promote this cycle -- closing/adopting a slot must never depend on
    fresh re-qualification) and the Operator Console read model (which
    looks up a slot's durable trial lineage starting from the identical
    fresh, un-promoted catalog definition, never a persisted one)."""

    return definition_version + 1


def build_strategy_paper_trial_record(
    *,
    strategy_id: str,
    strategy_version: int,
    instrument_id: str,
    account_id: str,
    generation: int,
    opened_at: datetime,
    opening_authorization_id: str,
    opening_order_id: str,
    opening_realized_pnl_baseline: Decimal,
    horizon_days: int = STRATEGY_PAPER_TRIAL_HORIZON_DAYS,
    cooldown_days: int = STRATEGY_PAPER_TRIAL_COOLDOWN_DAYS,
    legacy_adopted: bool = False,
) -> StrategyPaperTrialRecord:
    """Build one immutable ``StrategyPaperTrialRecord``. Never persists
    anything itself -- ``application.strategy_paper_trial_lifecycle.
    RecordStrategyPaperTrialOpened`` does that, idempotently, against
    whatever this function returns."""

    if generation < 1:
        raise StrategyPaperTrialLifecycleError("STRATEGY_PAPER_TRIAL_GENERATION_INVALID")
    if horizon_days < 1:
        raise StrategyPaperTrialLifecycleError("STRATEGY_PAPER_TRIAL_HORIZON_INVALID")
    if cooldown_days < 0:
        raise StrategyPaperTrialLifecycleError("STRATEGY_PAPER_TRIAL_COOLDOWN_INVALID")
    trial_id = strategy_paper_trial_id(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        instrument_id=instrument_id,
        generation=generation,
    )
    horizon_target_at = opened_at + timedelta(days=horizon_days)
    identity_payload = (
        trial_id,
        opened_at,
        opening_authorization_id,
        opening_order_id,
        str(opening_realized_pnl_baseline),
        horizon_days,
        cooldown_days,
        legacy_adopted,
    )
    return StrategyPaperTrialRecord(
        trial_id=trial_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        instrument_id=instrument_id,
        account_id=account_id,
        generation=generation,
        opened_at=opened_at,
        opening_authorization_id=opening_authorization_id,
        opening_order_id=opening_order_id,
        opening_realized_pnl_baseline=opening_realized_pnl_baseline,
        horizon_days=horizon_days,
        horizon_target_at=horizon_target_at,
        cooldown_days=cooldown_days,
        policy_version=STRATEGY_PAPER_TRIAL_LIFECYCLE_POLICY_V1,
        legacy_adopted=legacy_adopted,
        canonical_hash=content_digest(identity_payload),
    )


def adopt_legacy_trial(
    *,
    strategy_id: str,
    strategy_version: int,
    instrument_id: str,
    account_id: str,
    authorization: CryptoPaperAuthorization,
    order: CryptoPaperOrder,
    opening_realized_pnl_baseline: Decimal,
    horizon_days: int = STRATEGY_PAPER_TRIAL_HORIZON_DAYS,
    cooldown_days: int = STRATEGY_PAPER_TRIAL_COOLDOWN_DAYS,
) -> StrategyPaperTrialRecord:
    """Recognize a PRE-EXISTING, already-open PAPER position (placed before
    this milestone's trial-lifecycle mechanism existed, under the old
    lifetime-only ``recurring_strategy_paper_trial_seed`` authorization) as
    this slot's genuine generation-1 trial -- see ``docs/REPEATABLE_
    STRATEGY_PAPER_EVIDENCE_V1.md``'s "Existing production position"
    section for the full compatibility rationale this function implements.

    Every field this builds from is a REAL, ALREADY-RETAINED fact:
    ``opened_at`` is the real order's own ``submitted_at`` (never a
    fabricated or estimated date), ``opening_authorization_id``/
    ``opening_order_id`` are the real, pre-existing deterministic ids the
    old lifetime-authorization path already produced, and
    ``opening_realized_pnl_baseline`` is the real position's own
    ``realized_paper_pnl`` as retained AT ADOPTION TIME (never assumed to
    be zero, even though it genuinely is zero for the one real position
    this function was written to adopt). Only ``horizon_days``/
    ``cooldown_days`` are NEW -- this milestone's own policy, applied
    prospectively from today onward, never claimed to have governed the
    position's original entry decision. ``legacy_adopted=True`` always, so
    this honest distinction remains visible on the record forever.

    Fails closed if ``authorization``/``order`` do not actually belong to
    the strategy/instrument being adopted -- this function recognizes an
    existing fact, it never constructs one from inputs that do not already
    cohere."""

    if authorization.strategy_or_discovery_id != strategy_id:
        raise StrategyPaperTrialLifecycleError("STRATEGY_PAPER_TRIAL_LEGACY_STRATEGY_MISMATCH")
    if order.authorization_id != authorization.authorization_id:
        raise StrategyPaperTrialLifecycleError("STRATEGY_PAPER_TRIAL_LEGACY_AUTHORIZATION_MISMATCH")
    if order.product_id != instrument_id:
        raise StrategyPaperTrialLifecycleError("STRATEGY_PAPER_TRIAL_LEGACY_INSTRUMENT_MISMATCH")
    return build_strategy_paper_trial_record(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        instrument_id=instrument_id,
        account_id=account_id,
        generation=1,
        opened_at=order.submitted_at,
        opening_authorization_id=authorization.authorization_id,
        opening_order_id=order.order_id,
        opening_realized_pnl_baseline=opening_realized_pnl_baseline,
        horizon_days=horizon_days,
        cooldown_days=cooldown_days,
        legacy_adopted=True,
    )


def build_strategy_paper_trial_closure_record(
    *,
    trial: StrategyPaperTrialRecord,
    closed_at: datetime,
    closing_authorization_id: str,
    closing_order_id: str,
    realized_paper_pnl: Decimal | None,
    coverage: TrialEvidenceCoverage,
    terminal_reason: StrategyPaperTrialTerminalReason = (
        StrategyPaperTrialTerminalReason.TRIAL_HORIZON_EXIT
    ),
) -> StrategyPaperTrialClosureRecord:
    """Build one immutable ``StrategyPaperTrialClosureRecord`` for a trial
    that has genuinely reached its evidence horizon. Never persists itself
    -- ``application.strategy_paper_trial_lifecycle.
    RecordStrategyPaperTrialClosed`` does that, idempotently.

    Trial Evidence Coverage Disclosure v1: ``coverage`` is a caller-supplied
    ``domain.strategy_paper_trial_evidence_coverage.TrialEvidenceCoverage``,
    already computed over exactly ``[trial.opened_at, closed_at]`` for
    ``trial.instrument_id`` -- this function never computes it itself (it
    owns no repository access) and never validates that the caller used the
    right window; it only flattens the already-built value onto the
    record's own ``coverage_*`` fields and folds it into the identity
    payload below. Folding it into ``canonical_hash`` is what makes closure
    replay safe: ``evaluate_trial_evidence_coverage`` is pure and
    deterministic given the same retained observations, so a genuine same-
    invocation replay (the only shape replay can take here -- see this
    record's own module docstring "Cooldown" section on why a trial can
    never be reconsidered for closing once ``latest_closure`` is non-``None``)
    always recomputes an identical ``coverage`` and therefore an identical
    hash; a caller passing a genuinely different coverage value for the same
    ``closure_id`` is exactly the kind of conflict ``RecordStrategyPaperTrialClosed``
    is designed to reject, not silently accept."""

    if closed_at < trial.opened_at:
        raise StrategyPaperTrialLifecycleError("STRATEGY_PAPER_TRIAL_CLOSURE_BEFORE_OPEN")
    closure_id = f"{trial.trial_id}-closure"
    next_eligible_at = closed_at + timedelta(days=trial.cooldown_days)
    identity_payload = (
        closure_id,
        trial.trial_id,
        closed_at,
        terminal_reason.value,
        closing_authorization_id,
        closing_order_id,
        None if realized_paper_pnl is None else str(realized_paper_pnl),
        coverage.window_duration_seconds,
        coverage.nominal_days,
        coverage.covered_days,
        coverage.qualifying_observation_count,
        coverage.largest_gap_seconds,
        coverage.policy_version,
    )
    return StrategyPaperTrialClosureRecord(
        closure_id=closure_id,
        trial_id=trial.trial_id,
        strategy_id=trial.strategy_id,
        instrument_id=trial.instrument_id,
        closed_at=closed_at,
        terminal_reason=terminal_reason,
        closing_authorization_id=closing_authorization_id,
        closing_order_id=closing_order_id,
        realized_paper_pnl=realized_paper_pnl,
        next_eligible_at=next_eligible_at,
        coverage_window_duration_seconds=coverage.window_duration_seconds,
        coverage_nominal_days=coverage.nominal_days,
        coverage_covered_days=coverage.covered_days,
        coverage_qualifying_observation_count=coverage.qualifying_observation_count,
        coverage_largest_gap_seconds=coverage.largest_gap_seconds,
        coverage_policy_version=coverage.policy_version,
        canonical_hash=content_digest(identity_payload),
    )


def build_unfilled_expiry_closure_record(
    *, trial: StrategyPaperTrialRecord, closed_at: datetime, coverage: TrialEvidenceCoverage
) -> StrategyPaperTrialClosureRecord:
    """The one legitimate way ``OPENING_ORDER_EXPIRED_UNFILLED`` is ever
    built -- see module docstring "Terminal reason boundary" section.
    ``closing_authorization_id``/``closing_order_id`` are deliberately the
    trial's OWN opening ids (never a new authorization/order, since none
    was legitimately created) and ``realized_paper_pnl`` is deliberately
    ``None`` (no exposure was ever acquired, so there is no economic
    outcome to report -- never fabricated as ``0``). ``coverage`` is still
    computed and retained even though no PAPER exposure was ever acquired
    here -- the trial's own nominal window (and whatever market evidence
    was or was not retained across it) is a well-defined fact independent
    of whether its opening order ever filled."""

    return build_strategy_paper_trial_closure_record(
        trial=trial,
        closed_at=closed_at,
        closing_authorization_id=trial.opening_authorization_id,
        closing_order_id=trial.opening_order_id,
        realized_paper_pnl=None,
        coverage=coverage,
        terminal_reason=StrategyPaperTrialTerminalReason.OPENING_ORDER_EXPIRED_UNFILLED,
    )


class TrialHorizonState(StrEnum):
    """Result of ``evaluate_trial_horizon`` -- whether an open trial has
    reached its bounded evidence window yet."""

    BEFORE_HORIZON = "BEFORE_HORIZON"
    AT_OR_AFTER_HORIZON = "AT_OR_AFTER_HORIZON"


def evaluate_trial_horizon(
    *, trial: StrategyPaperTrialRecord, as_of: datetime
) -> TrialHorizonState:
    """Pure, total, deterministic -- mirrors ``domain.counterfactual_
    followup.determine_followup_due_state``'s own ``target_time``
    comparison shape (``as_of >= target``), reused here as the load-bearing
    precedent for "elapsed calendar days since a real anchor date" rather
    than inventing a new comparison convention."""

    return (
        TrialHorizonState.AT_OR_AFTER_HORIZON
        if as_of >= trial.horizon_target_at
        else TrialHorizonState.BEFORE_HORIZON
    )


class TrialRetrialEligibility(StrEnum):
    """Result of ``evaluate_retrial_eligibility`` -- whether a genuinely
    NEW trial generation may begin for a slot right now. Qualification
    itself (does the strategy still clear ``strategy_evaluation.
    promote_to_paper_eligible`` under THIS cycle's fresh evaluation) is
    deliberately not this enum's concern -- that remains the existing,
    unmodified, separately-invoked gate; a caller checks both."""

    NO_PRIOR_TRIAL = "NO_PRIOR_TRIAL"
    ACTIVE_TRIAL_BLOCKS_RETRIAL = "ACTIVE_TRIAL_BLOCKS_RETRIAL"
    COOLDOWN_NOT_YET_ELAPSED = "COOLDOWN_NOT_YET_ELAPSED"
    ELIGIBLE_FOR_NEXT_GENERATION = "ELIGIBLE_FOR_NEXT_GENERATION"


def evaluate_retrial_eligibility(
    *,
    latest_trial: StrategyPaperTrialRecord | None,
    latest_closure: StrategyPaperTrialClosureRecord | None,
    as_of: datetime,
) -> TrialRetrialEligibility:
    """Pure, total, deterministic. ``latest_trial``/``latest_closure`` are
    the slot's own most-recent-by-``generation`` trial and (if any) its
    matching closure -- a caller sources both from ``application.
    strategy_paper_trial_lifecycle`` read methods, never guesses "latest"
    itself. A trial with NO matching closure is, by construction (at most
    one closure per ``trial_id``, DB-enforced), still ACTIVE -- this
    function never needs a separate "is this trial still open" input."""

    if latest_trial is None:
        return TrialRetrialEligibility.NO_PRIOR_TRIAL
    if latest_closure is None or latest_closure.trial_id != latest_trial.trial_id:
        return TrialRetrialEligibility.ACTIVE_TRIAL_BLOCKS_RETRIAL
    if as_of < latest_closure.next_eligible_at:
        return TrialRetrialEligibility.COOLDOWN_NOT_YET_ELAPSED
    return TrialRetrialEligibility.ELIGIBLE_FOR_NEXT_GENERATION


def next_trial_generation(*, trials: Sequence[StrategyPaperTrialRecord]) -> int:
    """The next generation number for a slot: one more than the highest
    ``generation`` already retained for it, or ``1`` if none exists yet.
    Safe to call even while an ACTIVE (unclosed) trial exists -- callers
    are expected to check ``evaluate_retrial_eligibility`` first and only
    reach this once that returns ``ELIGIBLE_FOR_NEXT_GENERATION`` (or
    ``NO_PRIOR_TRIAL``), at which point every existing row for the slot is
    already terminal by construction (see that function's own docstring),
    so gaps/gaming are not a structural concern here."""

    return max((item.generation for item in trials), default=0) + 1


# ---- PAPER Trial Closure Readiness v1: resumable horizon-close attempts ----
#
# The horizon close is AUTHORIZE -> CONSTRUCT -> PREVIEW -> SUBMIT (four
# separate short transactions) and a LATER tick's FILL. The original
# orchestration recognized "close already underway" by the close
# AUTHORIZATION's mere existence and finalized only on one fixed order id, so
# a crash between AUTHORIZE and SUBMIT, a preview/submit rejection after
# AUTHORIZE, or a SELL that expired unfilled left the trial ACTIVE forever.
#
# The lifecycle is now classified from persisted economic state alone
# (authorizations, previews, orders, the position) -- never an ad hoc flag:
#
# * attempt 1 keeps the original seed ``{trial_id}-close`` (so the normal
#   path's ids and closure hash are byte-identical to before); attempt N >= 2
#   uses ``{trial_id}-close-{N}``. Ids are matched EXACTLY, and N is derived
#   only from retained authorizations, contiguously.
# * the latest attempt is RESUMABLE while its authorization is unexpired and
#   no order exists and nothing it already produced is dead: the SAME
#   attempt continues from its first missing step (never re-authorized).
# * an order that exists is never resubmitted: OPEN/PARTIALLY_FILLED (or any
#   other non-terminal state) is IN_FLIGHT -- Phase F fills or expires it.
# * FILLED with the position flat is FILLED -- the only state that finalizes.
# * everything else is DEAD (EXPIRED/CANCELED/REJECTED order, FILLED with a
#   residual position, a rejected or expired preview, an expired
#   authorization with no order). A new attempt is permitted only once the
#   dead attempt's authorization has itself expired, so at most ONE unexpired
#   close authorization exists per trial at any instant; it is sized by the
#   CURRENT open position (a partial fill's residual, never the original).

_CLOSE_ORDER_IN_FLIGHT_STATES = frozenset(
    {
        PaperOrderState.PREVIEWED,
        PaperOrderState.SUBMITTED,
        PaperOrderState.OPEN,
        PaperOrderState.PARTIALLY_FILLED,
        PaperOrderState.CANCEL_REQUESTED,
        PaperOrderState.RECONCILIATION_REQUIRED,
    }
)


def close_attempt_seed(*, trial_id: str, attempt_number: int) -> str:
    if attempt_number < 1:
        raise StrategyPaperTrialLifecycleError("STRATEGY_PAPER_TRIAL_CLOSE_ATTEMPT_INVALID")
    if attempt_number == 1:
        return f"{trial_id}-close"
    return f"{trial_id}-close-{attempt_number}"


class CloseAttemptState(StrEnum):
    NO_ATTEMPT = "NO_ATTEMPT"
    RESUMABLE = "RESUMABLE"
    IN_FLIGHT = "IN_FLIGHT"
    FILLED = "FILLED"
    DEAD_AWAITING_AUTHORIZATION_EXPIRY = "DEAD_AWAITING_AUTHORIZATION_EXPIRY"
    DEAD_RETRYABLE = "DEAD_RETRYABLE"


@dataclass(frozen=True, slots=True)
class CloseAttemptAssessment:
    """``attempt_number``/``authorization_id``/``order_id`` describe the
    LATEST retained attempt (0/None when there is none). ``action_seed`` is
    the only seed a caller may pass to the close placement this tick --
    the latest attempt's own seed when RESUMABLE, attempt 1's for
    NO_ATTEMPT, attempt N+1's for DEAD_RETRYABLE, and ``None`` otherwise
    (nothing may be placed)."""

    state: CloseAttemptState
    attempt_number: int
    authorization_id: str | None
    order_id: str | None
    action_seed: str | None
    detail: str


def classify_close_attempts(
    *,
    trial_id: str,
    authorizations: Sequence[CryptoPaperAuthorization],
    previews: Sequence[CryptoPaperPreview],
    orders: Sequence[CryptoPaperOrder],
    position_base_quantity: Decimal,
    evaluated_at: datetime,
) -> CloseAttemptAssessment:
    """Pure, total, deterministic -- see the section comment above."""

    authorization_by_id = {item.authorization_id: item for item in authorizations}
    attempt = 0
    while (
        f"{close_attempt_seed(trial_id=trial_id, attempt_number=attempt + 1)}-authorization"
        in authorization_by_id
    ):
        attempt += 1
    if attempt == 0:
        return CloseAttemptAssessment(
            CloseAttemptState.NO_ATTEMPT,
            0,
            None,
            None,
            close_attempt_seed(trial_id=trial_id, attempt_number=1),
            "NO_CLOSE_ATTEMPT",
        )
    seed = close_attempt_seed(trial_id=trial_id, attempt_number=attempt)
    authorization = authorization_by_id[f"{seed}-authorization"]
    order_id = f"{seed}-order"
    order = next((item for item in orders if item.order_id == order_id), None)
    preview = next((item for item in previews if item.preview_id == f"{seed}-preview"), None)

    def result(
        state: CloseAttemptState, detail: str, action: str | None = None
    ) -> CloseAttemptAssessment:
        return CloseAttemptAssessment(
            state,
            attempt,
            authorization.authorization_id,
            None if order is None else order.order_id,
            action,
            detail,
        )

    if order is not None:
        if order.state in _CLOSE_ORDER_IN_FLIGHT_STATES:
            return result(CloseAttemptState.IN_FLIGHT, f"ORDER_{order.state.value}")
        if order.state is PaperOrderState.FILLED and position_base_quantity <= 0:
            return result(CloseAttemptState.FILLED, "ORDER_FILLED_POSITION_FLAT")
        detail = (
            "ORDER_FILLED_RESIDUAL_POSITION"
            if order.state is PaperOrderState.FILLED
            else f"ORDER_{order.state.value}"
        )
    elif evaluated_at >= authorization.expires_at:
        detail = "AUTHORIZATION_EXPIRED_WITHOUT_ORDER"
    elif preview is not None and preview.rejection_reasons:
        detail = "PREVIEW_REJECTED"
    elif preview is not None and evaluated_at >= preview.expires_at:
        detail = "PREVIEW_EXPIRED_WITHOUT_ORDER"
    else:
        return result(CloseAttemptState.RESUMABLE, "AUTHORIZATION_OPEN_NO_ORDER", seed)
    if evaluated_at < authorization.expires_at:
        return result(CloseAttemptState.DEAD_AWAITING_AUTHORIZATION_EXPIRY, detail)
    return result(
        CloseAttemptState.DEAD_RETRYABLE,
        detail,
        close_attempt_seed(trial_id=trial_id, attempt_number=attempt + 1),
    )
