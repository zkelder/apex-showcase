"""Deterministic planning and idempotent bounded synchronization of Decision
Follow-Up Observations from already-retained ``CounterfactualDecision`` rows.

Two services, mirroring ``application.counterfactual_classification``'s own
split and its reasons for staying CLI-triggered rather than auto-wired into
the recurring operating cycle (Part 12): re-running either is always safe,
but adding a new moving part to the cycle that currently protects the
options-forward capture window is deliberately not this milestone's call to
make.

* ``PlanFollowUpObservations`` -- read-only, makes no provider request and
  writes nothing.  Classifies every ``(decision, horizon)`` pair into its
  ``FollowUpDueState`` so an operator can see, before anything is attempted,
  how many observations are due, not yet due, unsupported, or already
  retained (Part 15/20's "report before execution").
* ``SyncFollowUpObservations`` -- performs at most ``sample_limit`` genuine
  observation attempts (Part 16's provider-request budget), skipping
  everything not currently ``DUE`` at zero cost.  A KALSHI attempt makes one
  or two bounded, read-only, unauthenticated public GETs (see
  ``KalshiFollowUpMarketSource``); a CRYPTO attempt makes zero provider
  requests -- it only ever reads already-retained bars (Part 7: v1
  deliberately does not trigger new crypto capture; that stays the existing
  ``CRYPTO_UNIVERSE_REFRESH`` cycle's job).  Both venues save at most once
  per ``(decision, horizon)``: the repository is checked *before* any read
  is attempted, so a successful genuine observation is never re-attempted,
  and a decision's *first* genuine read becomes its permanent record for
  that horizon -- never silently re-rolled on a later sync (Part 10/13).

Counterfactual Follow-Up Starvation Fix v1 adds one more shared idea both
services now depend on: a CRYPTO ``DUE`` pair's baseline reference can be
*provably, permanently* unavailable rather than merely not-yet-available --
see ``domain.counterfactual_followup.FollowUpDueState.
PERMANENTLY_UNAVAILABLE`` for the proof and ``_crypto_baseline_reference``
below for the one shared, zero-provider-request lookup both services use to
apply it, always *before* any bounded attempt-budget or provider-request
budget is spent on a pair that could never have produced genuine evidence in
the first place.

Counterfactual Follow-Up Long-Run Fairness v1 closes a second, independent
starvation path a final-review audit of the fix above found: classifying a
pair ``PERMANENTLY_UNAVAILABLE`` costs zero *attempt* budget, but every
pair ``SyncFollowUpObservations.execute`` looks at -- ``OBSERVED``,
``NOT_YET_DUE``, ``UNSUPPORTED``, ``PERMANENTLY_UNAVAILABLE``, transient
``BLOCKED_UNAVAILABLE``, and genuinely attempted alike -- still costs one
unit of ``candidate_scan_limit``, and the scan always walked the identical
oldest-decision-first order starting from the single oldest decision, every
cycle, with nothing to remember where the previous cycle actually stopped.
Any oldest-sorting prefix that is not itself a fresh genuine attempt --
whether permanently blocked, already observed, or simply not yet due for a
long declared horizon -- therefore consumed the *scan* budget identically on
every cycle forever once that prefix alone exceeded ``candidate_scan_limit``,
regardless of how generous a constant ``candidate_scan_limit`` was given: the
exact same defect the fix above closed for ``sample_limit``, merely at a
larger, still-finite threshold and via a different budget.  ``candidate_scan_
limit`` being "generous" only delays this, it never prevents it, because
``OBSERVED`` pairs alone accumulate at the front of this same oldest-first
order for as long as Apex keeps operating -- healthy, expected growth that
nonetheless erodes the same fixed bound over time.

The fix is a durable, monotonically-advancing scan cursor (``FollowUpScan
Position``, ``FollowUpRepository.scan_position``/``.save_scan_position`` --
see their own docstrings): each ``SyncFollowUpObservations.execute`` call
resumes its oldest-decision-first walk immediately after wherever the
previous *completed* cycle actually stopped, wrapping back to the true
oldest decision once the newest is passed, rather than unconditionally
restarting at the oldest decision every time.  This is a ring, not a
re-prioritization: every decision is still visited in the same relative
oldest-first order every lap, so whenever the full backlog already fits
inside one cycle's budget (true of every retained production cycle to date),
completing a full lap always returns the cursor to a position equivalent to
"start over at the oldest decision" and this is behaviorally identical to
the fix above's own original shape.  It only changes behavior once a bounded
scan can no longer finish a full lap in one cycle -- exactly the case that
matters, and exactly the case ``candidate_scan_limit`` alone could not
handle at any fixed value.  See ``_rotate_from_cursor`` and
``SyncFollowUpObservations.execute`` for the mechanism and
``tests/test_counterfactual_followup.py``'s own long-run, multi-cycle
regression proof for why this specific shape (and not merely a larger
constant) is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from apex.application.kalshi_resolution import (
    InMemoryKalshiResolutionFactRepository,
    KalshiResolutionFactRepository,
)
from apex.domain.counterfactual_followup import (
    CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE,
    CRYPTO_DECISION_INPUT_REFERENCE_PERMANENTLY_UNAVAILABLE,
    CRYPTO_V3_WINDOW_DERIVATION_DISAGREEMENT,
    CRYPTO_V3_WINDOW_END_MEASUREMENT_UNAVAILABLE,
    CRYPTO_V3_WINDOW_START_MEASUREMENT_UNAVAILABLE,
    CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION,
    CURRENT_FOLLOWUP_POLICY_VERSIONS,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    FOLLOWUP_POLICY_V1,
    FOLLOWUP_POLICY_V3,
    KNOWN_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSIONS,
    KNOWN_FOLLOWUP_POLICY_VERSIONS,
    CounterfactualFollowUpError,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpDueState,
    FollowUpEvidenceDefectType,
    FollowUpEvidenceQualityAssessment,
    FollowUpEvidenceQualityVerdict,
    FollowUpHorizonKind,
    SecuritiesTerminalEvidenceTiming,
    assess_followup_evidence_quality,
    build_crypto_price_movement_observation,
    build_kalshi_market_status_observation,
    build_securities_structure_terminal_observation,
    current_followup_policy_venues,
    determine_followup_due_state,
    first_daily_close_boundary_at_or_after,
    followup_evidence_quality_policy_supports_venue,
    followup_observation_id,
    followup_policy_version_supports_venue,
    forward_measurement_window,
)
from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.crypto_market_pulse import completed_utc_day
from apex.domain.kalshi_resolution import (
    KALSHI_RESOLUTION_FACT_SOURCE,
    KalshiResolutionState,
    build_kalshi_resolution_fact,
)
from apex.domain.options_structures import StructureType
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import ValueValidationError, parse_decimal

DEFAULT_SAMPLE_LIMIT = 25
# Counterfactual Follow-Up Starvation Fix v1: a second, independent, much
# larger bound on how many candidates one call will even look at -- see
# ``SyncFollowUpObservations.execute``'s own docstring for why this alone is
# only a per-invocation wall-clock safety net, not a starvation-freedom
# guarantee at any fixed value.  Counterfactual Follow-Up Long-Run Fairness
# v1's durable scan cursor (``FollowUpScanPosition``) is what actually makes
# later work reachable regardless of how large the oldest-sorting
# non-actionable prefix grows -- see the module docstring.
DEFAULT_CANDIDATE_SCAN_LIMIT = 2000
DEFAULT_TIMEOUT_SECONDS = 10
ALL_HORIZON_KINDS: tuple[FollowUpHorizonKind, ...] = (
    FollowUpHorizonKind.SHORT_HORIZON,
    FollowUpHorizonKind.STRATEGY_HORIZON,
)


def _baseline_kinds_for(
    *, venue: SharedVenue, policy_version: str
) -> tuple[FollowUpBaselineKind, ...]:
    """Which ``FollowUpBaselineKind`` values a decision is followed up under,
    for one policy version.  The one place this venue/policy -> baseline-kind
    mapping is decided (Part: pre-deployment staleness-semantics audit) --
    every caller in this module composes this instead of hardcoding the set.

    ``FOLLOWUP_POLICY_V1`` never had this axis: exactly one kind
    (``DECISION_TIME``), for either venue -- unchanged forever, matching
    every already-retained V1 row.  Under any later policy, CRYPTO gets both
    kinds (the projection->decision gap is real and can span multiple days
    for a since-gone-stale projection -- see ``domain.counterfactual_
    followup.FollowUpBaselineKind`` for why neither answers the other's
    question); KALSHI still only ever gets ``DECISION_TIME`` -- a live
    public read has no separate "origin evidence" reading to distinguish in
    v1, and this module does not introduce new provider history merely to
    manufacture one."""

    if policy_version == FOLLOWUP_POLICY_V1:
        return (FollowUpBaselineKind.DECISION_TIME,)
    if venue is SharedVenue.CRYPTO:
        return (FollowUpBaselineKind.ORIGIN_EVIDENCE, FollowUpBaselineKind.DECISION_TIME)
    return (FollowUpBaselineKind.DECISION_TIME,)


def _policy_anchor_at(
    *,
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection | None,
    baseline_kind: FollowUpBaselineKind,
) -> datetime | None:
    """The *raw* instant a ``baseline_kind`` anchors to -- ``decision.
    evaluated_at`` for ``DECISION_TIME``, the subject's own
    ``SharedOpportunityProjection.generated_at`` for ``ORIGIN_EVIDENCE``.
    ``None`` only in the ``ORIGIN_EVIDENCE`` case where no subject
    projection is retained at all.

    Split out from ``_horizon_anchor_at`` by Counterfactual Forward Outcome
    Temporal Integrity v1: under ``FOLLOWUP_POLICY_V3`` the raw anchor and
    the instant the horizon is measured from are genuinely two different
    facts (the latter is the first daily-close boundary at or *after* the
    former), and both are retained on a V3 row.  Under every earlier policy
    version they coincide, so this function returns exactly what
    ``_horizon_anchor_at`` always returned for those versions."""

    if baseline_kind is FollowUpBaselineKind.ORIGIN_EVIDENCE:
        return None if projection is None else projection.generated_at
    return decision.evaluated_at


def _horizon_anchor_at(
    *,
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection | None,
    baseline_kind: FollowUpBaselineKind,
    policy_version: str,
) -> datetime | None:
    """The instant a ``(horizon_kind, baseline_kind)`` pair's ``target_time``
    is computed from.  ``DECISION_TIME`` is always available (``decision.
    evaluated_at`` is a required field on every retained decision);
    ``ORIGIN_EVIDENCE`` is ``None`` when no subject projection is retained
    at all -- ``determine_followup_due_state`` reports that as
    ``UNSUPPORTED``, never a crash (Part: ``FollowUpDueState.UNSUPPORTED``
    semantics).

    ``FOLLOWUP_POLICY_V1``/``V2``: exactly the raw policy anchor, unchanged
    forever -- every already-retained row of either reproduces the identical
    ``target_time`` it always had.

    ``FOLLOWUP_POLICY_V3``: the first daily-close boundary at or *after* the
    raw anchor (W0).  Returning W0 here -- rather than teaching the pure
    ``determine_followup_due_state`` a third concept -- is what makes that
    function's own unchanged ``target_time = anchor + horizon_days`` come
    out as exactly W1, and therefore makes a V3 pair ``DUE`` precisely once
    its economic window has genuinely closed.  ``domain.counterfactual_
    followup.first_daily_close_boundary_at_or_after`` is deliberately a
    ceiling and deliberately *not* ``crypto_market_pulse.completed_utc_day``
    -- see ``FOLLOWUP_POLICY_V3``'s own block for why that distinction is
    the entire pre-anchor-movement correction."""

    anchor = _policy_anchor_at(
        decision=decision, projection=projection, baseline_kind=baseline_kind
    )
    if anchor is None or policy_version != FOLLOWUP_POLICY_V3:
        return anchor
    return first_daily_close_boundary_at_or_after(anchor)


def _validate_policy_version(policy_version: str) -> None:
    """Both services below accept an explicit ``policy_version`` rather than
    reading ``domain.counterfactual_followup``'s own current per-venue
    policy selection themselves (Part: "must not ambiguously switch behavior based on
    whatever constant happens to be imported as latest" -- the composition
    root, not this module, decides what "current" means at each call site;
    see ``adapters.operating_cycle_tasks`` and ``cli`` for where that
    explicit choice is actually made).  This only rejects a genuinely
    unrecognized string -- a caller bug -- never silently substitutes a
    different version."""

    if policy_version not in KNOWN_FOLLOWUP_POLICY_VERSIONS:
        raise CounterfactualFollowUpError("COUNTERFACTUAL_FOLLOWUP_POLICY_VERSION_UNKNOWN")


class FollowUpSourceUnavailable(RuntimeError):
    """Raised by a source Protocol implementation when a genuine, bounded
    attempt to gather follow-up evidence did not succeed (missing retained
    data, provider error, malformed response, ...).  Always caught by
    ``SyncFollowUpObservations`` and turned into a ``BLOCKED_UNAVAILABLE``
    entry -- never allowed to abort the whole bounded run.  ``request_count``
    lets a genuine provider-touching source disclose how many bounded
    requests it actually made before giving up (Part 16); it defaults to 0
    for sources -- like the local crypto bar reader -- that never touch a
    provider at all."""

    def __init__(self, message: str, *, request_count: int = 0) -> None:
        super().__init__(message)
        self.request_count = request_count


class DecisionSource(Protocol):
    def all_decisions(self) -> tuple[CounterfactualDecision, ...]: ...


class SharedOpportunitySubjectSource(Protocol):
    def projection(self, projection_id: str) -> SharedOpportunityProjection | None: ...


@dataclass(frozen=True, slots=True)
class CryptoPriceLookup:
    close: Decimal
    source_observation_id: str
    evidence_class: EvidenceClass
    bucket_start: datetime
    # The retained candle's own ``bucket_start`` this close actually came
    # from -- *not* necessarily the boundary a caller queried with (``daily_
    # close`` only ever returns an exact match, so those coincide, but
    # ``latest_close_before`` can walk back further than one day when the
    # immediately-preceding bucket is genuinely missing, e.g. a provider gap
    # -- see its own docstring).  Required (never optional/defaulted) so a
    # caller resolving a *target* bucket from a *baseline* lookup (Part:
    # target-bucket-from-baseline-candle, not from a re-derived boundary --
    # see ``SyncFollowUpObservations._build_crypto``) always measures the
    # true number of calendar days actually spanned by the two retained
    # candles it read, never the number of days a gap-free assumption would
    # have implied.


class CryptoPriceSource(Protocol):
    def daily_close(self, *, product_id: str, bucket_start: datetime) -> CryptoPriceLookup | None:
        """Return the retained daily close for the UTC-midnight bucket
        exactly equal to ``bucket_start``, or ``None`` if no such bucket is
        retained -- never the nearest available bucket (Part 5).  Keyed by
        ``SharedOpportunityProjection.product_or_market_id`` (``CryptoProductObservation.
        product_id``, always populated) rather than ``instrument_id``/
        ``canonical_asset_id`` (defaults to ``""`` on some retained rows).

        The lookup *mechanism* (exact-bucket match, fail closed, never a
        silent nearest-available substitution) is used unchanged by every
        policy version -- but which ``bucket_start`` a caller asks for is
        not: ``FOLLOWUP_POLICY_V1`` always asks for
        ``completed_utc_day(decision.evaluated_at)`` (its own frozen,
        hindsight-leaking ``reference_price_at_baseline`` bucket, and also
        every version's ``reference_price_at_target`` bucket); a policy after
        V1 asks for a *different*, baseline-candle-relative bucket to resolve
        ``reference_price_at_target`` (Part: target-bucket-from-baseline-
        candle -- see ``SyncFollowUpObservations._build_crypto`` for why a
        bucket merely ``horizon_days`` after ``completed_utc_day(target_
        time)``'s naive computation would silently compare closes more than
        ``horizon_days`` calendar days apart)."""

    def daily_close_as_of(
        self, *, product_id: str, bucket_start: datetime, measurement_as_of: datetime
    ) -> CryptoPriceLookup | None:
        """Counterfactual Forward Outcome Temporal Integrity v1 --
        ``FOLLOWUP_POLICY_V3``'s one and only endpoint lookup, used for
        *both* the window-start and window-end buckets.

        Return the retained daily close for the UTC-midnight bucket exactly
        equal to ``bucket_start``, considering only evidence Apex genuinely
        possessed by ``measurement_as_of``; ``None`` when nothing qualifies.

        Three predicates, all required, all applied **before** any ranking
        or tie-breaking (the ordering in which they are applied is itself
        part of the contract -- see ``FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES``'s
        own incident trace for what happens when an eligibility check is
        applied *after* a "latest wins" tie-break):

        1. exact market bucket (``candle.bucket_start == bucket_start``) --
           never the nearest available bucket, never a backward walk;
        2. eligible evidence class (``is_followup_evidence_eligible`` --
           the same fail-closed allowlist every other CRYPTO follow-up
           lookup already enforces);
        3. ``batch.completed_at <= measurement_as_of`` -- the availability
           bound that makes a V3 measurement reproducible.

        Then an **explicit total ordering**: the greatest
        ``(completed_at, observation_id)``.  Deliberately a total order
        rather than ``daily_close``'s ``max(..., key=completed_at)``, which
        is only a *partial* one: two retained batches sharing a
        ``completed_at`` resolve there by scan order, which is not a
        property this repository guarantees or should depend on.
        ``observation_id`` is content-addressed (``domain.values.
        deterministic_id``), so the tie-break is deterministic, stable
        across processes, and independent of insertion order.

        Why this is not simply ``daily_close`` with one more predicate:
        ``daily_close`` is frozen ``FOLLOWUP_POLICY_V1``/``V2`` behavior and
        its own docstring correctly explains why it deliberately has *no*
        availability constraint for those versions' forward target.  V3's
        whole correction is that a forward measurement without a declared
        availability bound is not reproducible.  Giving the two versions
        two separate, separately-documented methods keeps V1/V2 byte-for-
        byte reproducible while V3 gets the semantics it needs -- rather
        than widening one method until neither version's contract is
        legible.

        Zero provider requests -- purely local reads over already-retained
        evidence, exactly like its two siblings."""

    def latest_close_before(self, *, product_id: str, before: datetime) -> CryptoPriceLookup | None:
        """Return the retained daily close whose ``bucket_start`` is the
        latest one strictly before ``before``, or ``None`` if no retained
        bucket qualifies -- or if the only qualifying bucket(s) were not
        themselves *genuinely available* to Apex by ``before`` (Part:
        availability-as-of correctness, below).  Must never return a bucket
        at or after ``before`` -- that is one of two properties
        ``FOLLOWUP_POLICY_V2`` actually depends on (Part: hindsight-leakage
        regression) -- Counterfactual Crypto Follow-Up Point-in-Time
        Semantics v2's own baseline resolution for
        ``reference_price_at_baseline``, for *both* ``FollowUpBaselineKind``
        values it supports: called with the subject's own
        ``completed_utc_day(projection.generated_at)`` for
        ``ORIGIN_EVIDENCE``, or with ``completed_utc_day(decision.
        evaluated_at)`` for ``DECISION_TIME`` (the same boundary V1 used,
        but resolved as "latest genuinely retained close before it" rather
        than V1's leaking exact-bucket lookup).  Deliberately not a
        fixed-offset ("``before`` minus one day") exact-bucket lookup: it
        selects whatever the actual latest retained evidence was, which is
        provably identical to what ``domain.crypto_universe.
        run_discovery``/``run_relative_strength``/``run_reversal_regime``
        themselves read as ``observation.candles[-1]`` when the subject's
        own economics were computed, without assuming a particular provider
        cadence.

        The *second* property this method must hold, found by a pre-
        deployment availability-as-of audit of this exact milestone (zero
        genuine V2 rows retained at the time -- direct correction to V2
        itself, not a new policy version, for the same reason the target-
        bucket fix below is not either): a candle's ``bucket_start`` is
        purely an *economic* label (which trading day the close belongs to)
        and proves nothing about *when Apex came to possess that candle* --
        a batch backfilled (``EvidenceClass.HISTORICAL_PUBLIC``) or
        recaptured after ``before`` can still carry an old ``bucket_start``.
        Selecting such a batch's candle merely because its ``bucket_start``
        is historical would leak hindsight just as surely as selecting a
        same-day bucket does -- Apex did not genuinely know that close as of
        ``before``, regardless of what economic day it describes.  A
        qualifying candle must therefore also come from a retained
        ``CryptoProductObservation`` batch whose own ``completed_at``
        (capture-completion wall-clock time -- the finest-grained
        "genuinely possessed as of" fact this repository retains for a
        crypto batch; see ``domain.crypto_universe.CryptoProductObservation``)
        is at-or-before ``before``.  Never inferred from ``bucket_start``
        alone (Part: availability-as-of correctness)."""


@dataclass(frozen=True, slots=True)
class KalshiMarketRead:
    market_status: str
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    settlement_result: str | None
    settlement_source: str | None
    source_endpoint_identity: str
    request_count: int
    source_limitations: tuple[str, ...] = ()
    # Kalshi Settlement & Resolution Evidence Maturation v1: additional raw
    # fields needed to build a ``domain.kalshi_resolution.
    # KalshiMarketResolutionFact`` from this exact read, without a second
    # live request. All optional/default-``None`` because every pre-
    # existing call site -- multi-leg event reads (no single ticker to
    # report), and every test double across this module's own test suites
    # -- legitimately never populates them; only a genuine single-market
    # read (``KalshiFollowUpPublicMarketSource._read_single_market``)
    # populates all five. ``settlement_result`` above stays exactly what it
    # always was: the raw, ungated provider ``result`` string, as read --
    # gating it to only-when-authoritative is ``SyncFollowUpObservations``'
    # job (Part 4/9), not this struct's or this adapter's.
    market_ticker: str | None = None
    event_ticker: str | None = None
    settlement_value_dollars: Decimal | None = None
    settlement_ts: datetime | None = None
    close_time: datetime | None = None


class KalshiMarketSource(Protocol):
    def read_market_status(
        self,
        *,
        subject_source_record_type: str,
        instrument_id: str,
        product_or_market_id: str,
        timeout_seconds: int,
    ) -> KalshiMarketRead:
        """Perform one bounded, read-only, unauthenticated genuine Kalshi
        public read and return its parsed result, including exactly how many
        bounded requests it genuinely made (Part 16 -- at most two: an event
        lookup and a single-market fallback).  Must raise
        ``FollowUpSourceUnavailable`` (never return a sentinel) on any
        failure -- HTTP error, malformed response, or a ticker this policy
        cannot resolve -- and must set that exception's own ``request_count``
        just as accurately."""


@dataclass(frozen=True, slots=True)
class SecuritiesPriceLookup:
    underlying_reference_price: Decimal
    source_snapshot_id: str
    captured_at: datetime


class SecuritiesUnderlyingPriceSource(Protocol):
    def earliest_reference_price_at_or_after(
        self, *, underlying_canonical_security_id: str, at_or_after: datetime
    ) -> SecuritiesPriceLookup | None:
        """Return the earliest already-retained ``domain.options_quote_
        intelligence.OptionsChainSnapshot`` for this underlying whose own
        ``captured_at`` is at or after ``at_or_after``, or ``None`` if none is
        yet retained -- never the nearest-available snapshot, never a live
        provider request (Part 5/7: this module never triggers new options
        capture; that stays ``OPTIONS_RESEARCH_EVIDENCE_CAPTURE``'s own job,
        exactly like CRYPTO's analogous boundary with
        ``CRYPTO_UNIVERSE_REFRESH``)."""


@dataclass(frozen=True, slots=True)
class _SecuritiesStructureBaseline:
    """Every fact ``SyncFollowUpObservations._build_securities`` needs about
    a structure's own decision-time construction, read back from its
    ``SharedOpportunityProjection.source_details`` -- Securities Research ->
    Shared Opportunity Recurring Bridge v1's producer
    (``adapters.shared_opportunity_sources.RetainedSharedOpportunitySource.
    securities_projections``) is the one place that stamps these keys; see
    its own docstring for the exact contract. Unlike CRYPTO/KALSHI, the
    *baseline* side needs no separate repository lookup at all: the
    projection already retained by ``SharedOpportunitySubjectSource`` is
    itself the single already-retained record of the structure's
    decision-time construction."""

    underlying_canonical_security_id: str
    structure_type: StructureType
    long_strike: Decimal
    short_strike: Decimal
    multiplier: Decimal
    quantity: int
    initial_cash_flow: Decimal
    underlying_reference_price_at_decision: Decimal
    source_chain_snapshot_id: str


def _securities_structure_baseline_from_projection(
    projection: SharedOpportunityProjection,
) -> _SecuritiesStructureBaseline | None:
    """``None`` if any required ``source_details`` key is missing or
    malformed -- fails closed, exactly like a missing CRYPTO baseline bucket,
    never a fabricated default (Part 5)."""

    details = dict(projection.source_details)
    try:
        return _SecuritiesStructureBaseline(
            underlying_canonical_security_id=details["underlying_canonical_security_id"],
            structure_type=StructureType(details["structure_type"]),
            long_strike=parse_decimal(details["long_strike"]),
            short_strike=parse_decimal(details["short_strike"]),
            multiplier=parse_decimal(details["multiplier"]),
            quantity=int(details["quantity"]),
            # Only field that is genuinely signed: negative for a debit
            # structure (capital paid out), positive for a credit (premium
            # received) -- see ``domain.options_structures.
            # structure_initial_cash_flow``, whose output this key stores.
            initial_cash_flow=parse_decimal(details["initial_cash_flow"], allow_negative=True),
            underlying_reference_price_at_decision=parse_decimal(
                details["underlying_reference_price_at_decision"]
            ),
            source_chain_snapshot_id=details["source_chain_snapshot_id"],
        )
    except (KeyError, ValueError, ArithmeticError, ValueValidationError):
        return None


@dataclass(frozen=True, slots=True)
class FollowUpScanPosition:
    """Counterfactual Follow-Up Long-Run Fairness v1.  A durable resume
    marker for ``SyncFollowUpObservations``' own bounded walk --
    structurally nothing like a ``DecisionFollowUpObservation``: no venue,
    no policy content, no measured payload, no ``canonical_hash``, never
    evidence, never a judgment, never reported to an operator as if it were
    either.  It answers exactly one question, "which ``(decision,
    horizon_kind, baseline_kind)`` candidate, in the same deterministic
    order every call already walks, was the last one a bounded scan
    examined -- regardless of that candidate's outcome", so the next call
    can resume immediately after it instead of unconditionally restarting
    at the single oldest decision's first candidate (Part: the actual
    long-run starvation path this closes -- see the module docstring and
    ``_rotate_from_cursor``).

    Granular per *candidate*, not per *decision*: a decision contributes up
    to four candidates (two horizon kinds x up to two baseline kinds), and
    anchoring the cursor to a candidate that was actually examined -- never
    only to "this decision is done" -- means even a scan bounded so tightly
    it cannot finish a single decision's own candidates in one cycle (an
    adversarial, never-production, but not-disallowed configuration) still
    makes genuine, monotonic, one-candidate-at-a-time progress instead of
    re-examining the same first candidate of the same oldest decision
    forever.  ``decision_id`` alone already uniquely identifies a decision
    (a content-addressed identity, see ``domain.counterfactual_ledger``), so
    no separate ``evaluated_at`` component is needed to make this position
    unambiguous."""

    decision_id: str
    horizon_kind: str
    baseline_kind: str


class FollowUpRepository(Protocol):
    def observation(self, observation_id: str) -> DecisionFollowUpObservation | None: ...

    def save_observation(self, *, observation: DecisionFollowUpObservation) -> bool:
        """Persist idempotently; return ``True`` on idempotent replay of an
        already-identical retained row, ``False`` on a genuine new write."""

    def scan_position(self, *, policy_version: str) -> FollowUpScanPosition | None:
        """Counterfactual Follow-Up Long-Run Fairness v1.  ``None`` means no
        completed scan has ever recorded a position for this
        ``policy_version`` -- the very first call ever made, or any call
        made before this fairness mechanism existed -- and
        ``SyncFollowUpObservations.execute`` starts at the single oldest
        decision's first candidate, exactly as every call always has.
        Scoped per ``policy_version`` for the same reason every other
        identity in this module is (Part: V1/V2 coexistence) -- an ad hoc
        ``FOLLOWUP_POLICY_V1`` sync (see ``cli.py``'s own explicit
        ``--policy-version`` override) must never advance or be blocked by
        the recurring cycle's own ``FOLLOWUP_POLICY_V2`` cursor, or vice
        versa."""

    def save_scan_position(self, *, policy_version: str, position: FollowUpScanPosition) -> None:
        """Persist the new resume marker.  Idempotent upsert, last writer
        for a given ``policy_version`` wins -- ``execute`` calls this at
        most once per invocation, always after every genuine write for that
        same cycle has already been attempted, so a crash between the last
        ``save_observation`` and this call merely leaves the cursor one
        cycle stale (self-healing: the next cycle re-examines, at most, the
        same already-bounded amount of work again -- every write this
        module makes is already idempotent, see ``save_observation`` --
        never a correctness hazard, only bounded, rare, wasted re-scanning)."""


@dataclass(frozen=True, slots=True)
class _FollowUpCandidate:
    """One ``(decision, horizon_kind, baseline_kind)`` triple -- exactly
    what ``SyncFollowUpObservations.execute``'s bounded walk and
    ``PlanFollowUpObservations.execute``'s unbounded one have each always
    considered, now materialized once as a flat, ordered sequence so a
    single loop (and a single, candidate-granular cursor) can walk it
    instead of three nested loops with no position of their own to
    remember. Not persisted, not evidence -- a private orchestration
    value, scoped to one ``execute`` call."""

    decision: CounterfactualDecision
    horizon_kind: FollowUpHorizonKind
    baseline_kind: FollowUpBaselineKind


def _venue_in_scope(venue: SharedVenue, venues: tuple[SharedVenue, ...] | None) -> bool:
    """Counterfactual Forward Outcome Temporal Integrity v1.  ``venues is
    None`` means "every venue" -- the frozen default, so every pre-existing
    caller (this module's own tests, the CLI's explicit ``--policy-version``
    override, every historical invocation) keeps its exact original
    behavior.  A non-``None`` tuple scopes one run to exactly those venues.

    Scope belongs to the *selection*, never to the policy version:
    ``FOLLOWUP_POLICY_V2`` is still perfectly capable of observing CRYPTO
    and must stay so (every already-retained V2 CRYPTO row was produced that
    way and must remain reproducible).  What changed is only which venues
    the *current* selection routes to it -- see ``domain.counterfactual_
    followup.current_followup_policy_venues``, which is what the composition
    roots pass here."""

    return venues is None or venue in venues


def _all_candidates(
    decisions: tuple[CounterfactualDecision, ...],
    *,
    policy_version: str,
    venues: tuple[SharedVenue, ...] | None = None,
) -> tuple[_FollowUpCandidate, ...]:
    """Every candidate, in the exact deterministic oldest-decision-first
    order this module has always used -- ``decisions`` must already be
    sorted by ``(evaluated_at, decision_id)``.  Pure; performs no I/O (the
    per-decision ``expected_holding_days``/projection reads still happen
    lazily, per candidate, in ``SyncFollowUpObservations.execute`` itself,
    not here).

    A decision whose venue a policy version cannot structurally support
    (``followup_policy_version_supports_venue`` -- today exactly
    ``FOLLOWUP_POLICY_V3`` outside CRYPTO) is excluded unconditionally, not
    merely by caller convention: no orchestration should ever be able to
    schedule an attempt that the domain builder and the database would both
    reject.  ``venues`` is the separate, caller-supplied *selection* scope
    -- see ``_venue_in_scope``."""

    return tuple(
        _FollowUpCandidate(
            decision=decision, horizon_kind=horizon_kind, baseline_kind=baseline_kind
        )
        for decision in decisions
        if _venue_in_scope(decision.venue, venues)
        and followup_policy_version_supports_venue(
            policy_version=policy_version, venue=decision.venue
        )
        for horizon_kind in ALL_HORIZON_KINDS
        for baseline_kind in _baseline_kinds_for(
            venue=decision.venue, policy_version=policy_version
        )
    )


def _rotate_from_cursor(
    candidates: tuple[_FollowUpCandidate, ...],
    cursor: FollowUpScanPosition | None,
) -> tuple[_FollowUpCandidate, ...]:
    """Counterfactual Follow-Up Long-Run Fairness v1.  Returns a ring
    rotation of ``candidates`` (see ``_all_candidates``) starting
    immediately after ``cursor`` -- every candidate is still visited
    exactly once per full lap, in the same relative oldest-first order,
    merely starting from a different point and wrapping back to the true
    oldest decision's first candidate once the newest is passed.

    ``cursor is None`` (no completed scan has ever recorded a position for
    this policy version) or an empty ``candidates`` both return
    ``candidates`` unchanged -- start at the true oldest, exactly as every
    call always has.  A ``cursor`` naming a candidate that no longer
    appears in ``candidates`` (there is no delete/edit path for a
    ``CounterfactualDecision`` or its applicable horizon/baseline kinds in
    this repository, so this should not arise, but nothing here assumes it
    cannot) is treated the same as ``cursor is None``: start over at the
    true oldest, rather than raising or silently skipping the entire
    backlog."""

    if cursor is None or not candidates:
        return candidates
    for index, candidate in enumerate(candidates):
        if (
            candidate.decision.decision_id == cursor.decision_id
            and candidate.horizon_kind.value == cursor.horizon_kind
            and candidate.baseline_kind.value == cursor.baseline_kind
        ):
            resume_index = index + 1
            if resume_index >= len(candidates):
                return candidates
            return candidates[resume_index:] + candidates[:resume_index]
    return candidates


def _crypto_baseline_reference(
    *,
    crypto_prices: CryptoPriceSource,
    product_id: str,
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection,
    policy_version: str,
    baseline_kind: FollowUpBaselineKind,
) -> CryptoPriceLookup | None:
    """Resolve ``reference_price_at_baseline``'s source lookup.  See
    ``domain.counterfactual_followup``'s ``FOLLOWUP_POLICY_V2`` comment
    block and ``FollowUpBaselineKind`` for the full trace/proof.

    Module-level (Counterfactual Follow-Up Starvation Fix v1) -- not a
    method -- specifically so it is the *one* place both
    ``PlanFollowUpObservations`` (read-only, to classify a ``DUE`` CRYPTO
    pair's baseline as genuinely retryable or provably ``PERMANENTLY_
    UNAVAILABLE`` -- see ``FollowUpDueState.PERMANENTLY_UNAVAILABLE`` --
    entirely at zero provider cost) and ``SyncFollowUpObservations`` (to
    resolve the identical fact once, *before* deciding whether a pair
    should spend any of its bounded attempt-budget at all) share, rather
    than each re-implementing or silently drifting from the other.

    ``FOLLOWUP_POLICY_V1`` (frozen, always ``baseline_kind=DECISION_
    TIME``, and confirmed by this milestone's original audit to be
    genuinely wrong for CRYPTO): the exact bucket at
    ``completed_utc_day(decision.evaluated_at)`` -- the decision's own
    evaluation day, which the subject's own economics never actually
    priced against.  Missing is always structurally *transient* here (that
    exact day's close may simply not have been captured yet) -- never
    permanent, and this function's own caller must never treat it as such
    (Part: V1 immutability -- this fix changes nothing about V1's own,
    already-shipped, semantics).

    Any later policy version resolves the latest retained daily close
    strictly before the boundary its ``baseline_kind`` selects:
    ``completed_utc_day(projection.generated_at)`` for
    ``ORIGIN_EVIDENCE`` (the subject's own generation boundary, proven
    equal to the pulse ``as_of`` that bounded the candle window the
    subject's economics were actually computed from), or
    ``completed_utc_day(decision.evaluated_at)`` for ``DECISION_TIME``
    (the same boundary V1 used, but now via "latest genuinely retained
    close before it" instead of V1's leaking exact-bucket lookup).  Never
    a bucket at or after its own boundary (Part: hindsight-leakage
    regression).  Missing here is always structurally *permanent* --
    see ``FollowUpDueState.PERMANENTLY_UNAVAILABLE`` for the full proof."""

    if policy_version == FOLLOWUP_POLICY_V1:
        decision_bucket = completed_utc_day(decision.evaluated_at)
        return crypto_prices.daily_close(product_id=product_id, bucket_start=decision_bucket)
    if policy_version == FOLLOWUP_POLICY_V3:
        # Counterfactual Forward Outcome Temporal Integrity v1: under V3
        # this lookup no longer produces a *measurement* endpoint at all --
        # it produces the decision-input reference (see ``_crypto_decision_
        # input_reference``).  It is still resolved here, by the same shared
        # function both services call, for exactly the reason this function
        # is shared at all: its absence is still the one provably permanent
        # CRYPTO condition, so both ``PlanFollowUpObservations`` and
        # ``SyncFollowUpObservations`` must classify it identically, at zero
        # provider cost, before any attempt budget is spent.  V3's own two
        # measurement endpoints are resolved separately, and only after a
        # pair has genuinely been admitted (see ``SyncFollowUpObservations.
        # _build_crypto``).
        return _crypto_decision_input_reference(
            crypto_prices=crypto_prices,
            decision=decision,
            projection=projection,
            baseline_kind=baseline_kind,
        )
    boundary = completed_utc_day(
        projection.generated_at
        if baseline_kind is FollowUpBaselineKind.ORIGIN_EVIDENCE
        else decision.evaluated_at
    )
    return crypto_prices.latest_close_before(product_id=product_id, before=boundary)


def _crypto_permanently_unavailable_reason(policy_version: str) -> str:
    """The one place the provably-permanent CRYPTO reason string is chosen,
    so ``PlanFollowUpObservations`` and ``SyncFollowUpObservations`` can
    never report the same condition differently."""

    if policy_version == FOLLOWUP_POLICY_V3:
        return CRYPTO_DECISION_INPUT_REFERENCE_PERMANENTLY_UNAVAILABLE
    return CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE


def _crypto_decision_input_reference(
    *,
    crypto_prices: CryptoPriceSource,
    decision: CounterfactualDecision,
    projection: SharedOpportunityProjection,
    baseline_kind: FollowUpBaselineKind,
) -> CryptoPriceLookup | None:
    """``FOLLOWUP_POLICY_V3``'s *decision-input reference* -- "what historical
    price evidence could Apex actually see at the policy anchor?"

    Deliberately the exact same, already-audited mechanism
    ``FOLLOWUP_POLICY_V2`` uses for its baseline (``latest_close_before``
    at ``completed_utc_day(anchor)``, which is both availability-gated on
    ``batch.completed_at <= before`` and evidence-eligibility filtered) --
    because for *this* question V2's mechanism was always right.  What V2
    got wrong was using the answer as a *measurement endpoint*; V3 keeps the
    answer and retains it as disclosure only.  It is never either endpoint
    of ``subsequent_return`` (``build_crypto_price_movement_observation``
    computes that exclusively from the two window closes, and a V3 row says
    so in its own ``data_limitations``).

    Point-in-time by construction and legitimately stale: it answers what
    Apex could see, which for a decision evaluated against a since-gone-
    stale projection can genuinely be days old.  Baseline staleness is
    therefore derivable by a reader from ``policy_anchor_at`` and the
    retained ``decision_input_reference_bucket_start`` -- never redundantly
    persisted as a duration this module computed.

    Its boundary is a *frozen past instant* (a pure function of the anchor),
    so -- exactly as for V2 -- once no retained batch for this product
    satisfies the gate, none ever will: absence here is provably permanent,
    which is what preserves Counterfactual Follow-Up Starvation Fix v1's
    guarantee unchanged under V3.  That is also why it is not additionally
    gated on ``measurement_as_of``: the gate it already applies
    (``completed_at <= completed_utc_day(anchor)``) is strictly tighter than
    ``completed_at <= measurement_as_of`` for every pair this module will
    ever build (a pair is only ``DUE`` once ``measurement_as_of >= W1 >= W0
    >= anchor > completed_utc_day(anchor)``), so adding it would be a
    redundant predicate that can never change an answer."""

    anchor = _policy_anchor_at(
        decision=decision, projection=projection, baseline_kind=baseline_kind
    )
    if anchor is None:
        return None
    return crypto_prices.latest_close_before(
        product_id=projection.product_or_market_id, before=completed_utc_day(anchor)
    )


@dataclass(frozen=True, slots=True)
class FollowUpPlanEntry:
    decision_id: str
    venue: SharedVenue
    horizon_kind: FollowUpHorizonKind
    baseline_kind: FollowUpBaselineKind
    state: FollowUpDueState
    target_time: datetime | None
    reasons: tuple[str, ...]
    # Counterfactual Forward Outcome Temporal Integrity v1: which policy
    # version produced this entry.  Always equal to its own plan's
    # ``policy_version`` -- redundant while one plan is read alone, and
    # required the moment two plans of the current per-venue selection are
    # shown together (``plan_current_followup_selection``), where an entry
    # with no version of its own would be genuinely ambiguous.  Defaulted so
    # every existing construction site and test stays unchanged.
    policy_version: str = FOLLOWUP_POLICY_V1


@dataclass(frozen=True, slots=True)
class FollowUpPlan:
    as_of: datetime
    policy_version: str
    considered_decisions: int
    considered_pairs: int
    state_counts: tuple[tuple[str, int], ...]
    state_counts_by_venue: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    entries: tuple[FollowUpPlanEntry, ...]


class PlanFollowUpObservations:
    """Read-only.  Makes no provider request, writes nothing.  Safe to call
    as often as desired -- see the module docstring.

    ``crypto_prices`` (Counterfactual Follow-Up Starvation Fix v1) is the
    same zero-provider-request, local-only ``CryptoPriceSource``
    ``SyncFollowUpObservations`` already depends on for the identical fact
    -- required, not optional, so this plan can honestly distinguish a
    genuinely retryable CRYPTO ``DUE`` baseline from one already provably
    ``FollowUpDueState.PERMANENTLY_UNAVAILABLE`` (Part: "do not mislabel
    permanently impossible work as ordinary healthy DUE backlog").  Every
    read it performs is the same already-accepted local scan over
    already-retained evidence this module's own docstring already promises
    elsewhere -- still zero provider requests, still writes nothing."""

    def __init__(
        self,
        decisions: DecisionSource,
        subjects: SharedOpportunitySubjectSource,
        crypto_prices: CryptoPriceSource,
    ) -> None:
        self._decisions = decisions
        self._subjects = subjects
        self._crypto_prices = crypto_prices

    def execute(
        self,
        *,
        as_of: datetime,
        already_observed: set[str] | None = None,
        policy_version: str = FOLLOWUP_POLICY_V1,
        venues: tuple[SharedVenue, ...] | None = None,
    ) -> FollowUpPlan:
        """``policy_version`` selects which ``(decision, horizon, baseline)``
        identity space this plan reports against (Part: "existing V1
        observations must not falsely satisfy V2 backlog") -- defaults to
        ``FOLLOWUP_POLICY_V1`` for the same reason the domain builders do
        (see their docstrings); callers that want "the current backlog"
        must pass ``domain.counterfactual_followup.
        current_followup_policy_version(venue)`` explicitly -- or, for the
        whole current selection at once, use ``plan_current_followup_
        selection``.  ``considered_pairs``
        is always "decisions x horizons x applicable baseline kinds under
        this one policy version" (``_baseline_kinds_for``) -- never summed
        across versions -- so running this once per known version and
        adding the totals together would double-count; that is deliberately
        not something this method does for you.

        Counterfactual Follow-Up Starvation Fix v1: a pair the pure
        ``determine_followup_due_state`` reports ``DUE`` is re-classified
        ``PERMANENTLY_UNAVAILABLE`` here whenever it is CRYPTO, its policy
        version's baseline mechanism is the availability-gated
        ``latest_close_before`` (every version after ``FOLLOWUP_POLICY_V1``),
        and its subject projection is retained (Part: ``SUBJECT_PROJECTION_
        NOT_RETAINED`` is foreign-key-prevented for every genuine
        Postgres-backed decision and stays exactly as reported by
        ``determine_followup_due_state`` alone -- out of scope for this
        fix).  This never changes ``considered_pairs``/``considered_
        decisions`` -- an entry is still appended for every pair either
        way -- only which ``FollowUpDueState`` that entry carries."""

        _validate_policy_version(policy_version)
        observed_ids = already_observed or set()
        entries: list[FollowUpPlanEntry] = []
        state_counts: dict[str, int] = {}
        state_counts_by_venue: dict[str, dict[str, int]] = {}
        decisions = sorted(
            self._decisions.all_decisions(), key=lambda item: (item.evaluated_at, item.decision_id)
        )
        for decision in decisions:
            if not _venue_in_scope(decision.venue, venues):
                continue
            if not followup_policy_version_supports_venue(
                policy_version=policy_version, venue=decision.venue
            ):
                continue
            projection = self._subjects.projection(decision.subject_projection_id)
            expected_holding_days = None if projection is None else projection.expected_holding_days
            for horizon_kind in ALL_HORIZON_KINDS:
                for baseline_kind in _baseline_kinds_for(
                    venue=decision.venue, policy_version=policy_version
                ):
                    observation_id = followup_observation_id(
                        decision_id=decision.decision_id,
                        policy_version=policy_version,
                        horizon_kind=horizon_kind,
                        baseline_kind=baseline_kind,
                    )
                    result = determine_followup_due_state(
                        horizon_kind=horizon_kind,
                        horizon_anchor_at=_horizon_anchor_at(
                            decision=decision,
                            projection=projection,
                            baseline_kind=baseline_kind,
                            policy_version=policy_version,
                        ),
                        expected_holding_days=expected_holding_days,
                        as_of=as_of,
                        already_observed=observation_id in observed_ids,
                    )
                    state = result.state
                    reasons = result.reasons
                    if (
                        state is FollowUpDueState.DUE
                        and decision.venue is SharedVenue.CRYPTO
                        and policy_version != FOLLOWUP_POLICY_V1
                        and projection is not None
                    ):
                        at_baseline = _crypto_baseline_reference(
                            crypto_prices=self._crypto_prices,
                            product_id=projection.product_or_market_id,
                            decision=decision,
                            projection=projection,
                            policy_version=policy_version,
                            baseline_kind=baseline_kind,
                        )
                        if at_baseline is None:
                            state = FollowUpDueState.PERMANENTLY_UNAVAILABLE
                            # Counterfactual Forward Outcome Temporal
                            # Integrity v1: the permanence *proof* is
                            # identical under V2 and V3 (a frozen, anchor-
                            # derived boundary), but the missing fact is
                            # structurally different -- V2's measurement
                            # baseline versus V3's decision-input disclosure
                            # -- so the two never share a reason string.
                            reasons = (_crypto_permanently_unavailable_reason(policy_version),)
                    entries.append(
                        FollowUpPlanEntry(
                            decision_id=decision.decision_id,
                            venue=decision.venue,
                            horizon_kind=horizon_kind,
                            baseline_kind=baseline_kind,
                            state=state,
                            target_time=result.target_time,
                            reasons=reasons,
                            policy_version=policy_version,
                        )
                    )
                    state_counts[state.value] = state_counts.get(state.value, 0) + 1
                    venue_bucket = state_counts_by_venue.setdefault(decision.venue.value, {})
                    venue_bucket[state.value] = venue_bucket.get(state.value, 0) + 1
        return FollowUpPlan(
            as_of=as_of,
            policy_version=policy_version,
            considered_decisions=len(decisions),
            considered_pairs=len(entries),
            state_counts=tuple(sorted(state_counts.items())),
            state_counts_by_venue=tuple(
                (venue, tuple(sorted(counts.items())))
                for venue, counts in sorted(state_counts_by_venue.items())
            ),
            entries=tuple(entries),
        )


@dataclass(frozen=True, slots=True)
class FollowUpSelectionPlan:
    """Counterfactual Forward Outcome Temporal Integrity v1.  The merged
    read-only view of the *current per-venue policy selection* -- one
    ``FollowUpPlan`` per distinct current policy version, plus the summed
    cross-version totals the operator console and CLI diagnostics actually
    display.

    Merging is honest here, and only here, precisely because the selection
    partitions venues: no decision appears under two of these plans, so the
    totals are genuine sums rather than the double-count
    ``PlanFollowUpObservations.execute``'s own docstring warns about when
    adding two *arbitrary* versions' plans together.  ``plans`` is retained
    alongside the merged figures so a caller that needs one version's own
    unmerged view never has to re-derive it."""

    as_of: datetime
    policy_versions: tuple[str, ...]
    policy_version_by_venue: tuple[tuple[str, str], ...]
    plans: tuple[FollowUpPlan, ...]
    considered_decisions: int
    considered_pairs: int
    state_counts: tuple[tuple[str, int], ...]
    state_counts_by_venue: tuple[tuple[str, tuple[tuple[str, int], ...]], ...]
    entries: tuple[FollowUpPlanEntry, ...]


def plan_current_followup_selection(
    service: PlanFollowUpObservations,
    *,
    as_of: datetime,
    already_observed: set[str] | None = None,
) -> FollowUpSelectionPlan:
    """Run ``service`` once per distinct policy version in the current
    per-venue selection, each scoped to that version's own venues, and
    merge the results.

    This is the single replacement for every former read of the removed
    global ``CURRENT_FOLLOWUP_POLICY_VERSION`` constant on a *diagnostic* surface
    (the operator console's Home and Follow-Up pages, the CLI's operating-
    plan diagnostic, the read-model benchmark).  Those surfaces structurally
    show one backlog view; before this milestone one version genuinely was
    the whole answer, and now the selection is -- so they ask for the
    selection rather than silently picking one of its versions and hiding
    the other's venues."""

    plans = tuple(
        service.execute(
            as_of=as_of,
            already_observed=already_observed,
            policy_version=policy_version,
            venues=current_followup_policy_venues(policy_version),
        )
        for policy_version in CURRENT_FOLLOWUP_POLICY_VERSIONS
    )
    state_counts: dict[str, int] = {}
    state_counts_by_venue: dict[str, dict[str, int]] = {}
    for plan in plans:
        for state, count in plan.state_counts:
            state_counts[state] = state_counts.get(state, 0) + count
        for venue, counts in plan.state_counts_by_venue:
            bucket = state_counts_by_venue.setdefault(venue, {})
            for state, count in counts:
                bucket[state] = bucket.get(state, 0) + count
    # ``considered_decisions`` is deliberately the count of DISTINCT
    # decisions across the selection, never the sum of each plan's own
    # figure: the selection partitions venues, so summing would be correct
    # today, but counting distinct ids stays correct even if a future
    # selection ever routed one venue to two versions (Part: "must not
    # accidentally report decisions x policy as decisions" -- the same rule
    # ``coverage``'s own cross-version breakdown already states).
    considered_decisions = len({entry.decision_id for plan in plans for entry in plan.entries})
    return FollowUpSelectionPlan(
        as_of=as_of,
        policy_versions=CURRENT_FOLLOWUP_POLICY_VERSIONS,
        policy_version_by_venue=tuple(
            (venue.value, policy_version)
            for policy_version in CURRENT_FOLLOWUP_POLICY_VERSIONS
            for venue in current_followup_policy_venues(policy_version)
        ),
        plans=plans,
        considered_decisions=considered_decisions,
        considered_pairs=sum(plan.considered_pairs for plan in plans),
        state_counts=tuple(sorted(state_counts.items())),
        state_counts_by_venue=tuple(
            (venue, tuple(sorted(counts.items())))
            for venue, counts in sorted(state_counts_by_venue.items())
        ),
        entries=tuple(entry for plan in plans for entry in plan.entries),
    )


@dataclass(frozen=True, slots=True)
class FollowUpSyncEntry:
    decision_id: str
    venue: SharedVenue
    horizon_kind: FollowUpHorizonKind
    baseline_kind: FollowUpBaselineKind
    # CREATED | REUSED_IDEMPOTENT_REPLAY | BLOCKED_UNAVAILABLE |
    # BLOCKED_PERMANENTLY_UNAVAILABLE (Counterfactual Follow-Up Starvation
    # Fix v1 -- see FollowUpDueState.PERMANENTLY_UNAVAILABLE; a distinct
    # outcome from BLOCKED_UNAVAILABLE so a consumer that only ever
    # inspects ``outcome`` -- never ``reasons`` -- still cannot confuse a
    # proven-permanent classification with a merely transient one).
    outcome: str
    observation_id: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FollowUpSyncResult:
    as_of: datetime
    policy_version: str
    sample_limit: int
    # Counterfactual Follow-Up Starvation Fix v1: a second, independent
    # bound -- see ``SyncFollowUpObservations.execute``'s own docstring.
    candidate_scan_limit: int
    genuine_attempts: int
    # Total (decision, horizon_kind, baseline_kind) triples this call
    # examined, across every outcome -- always >= genuine_attempts +
    # permanently_unavailable_count + every skipped_* count below, and
    # bounded by candidate_scan_limit (Part: "no unbounded full-table/
    # provider loop").
    candidates_examined: int
    created_count: int
    idempotent_replay_count: int
    # Transient blocks only (a genuine attempted read that failed but may
    # still succeed later -- a missing KALSHI read, a not-yet-retained
    # CRYPTO target bucket, or FOLLOWUP_POLICY_V1's own frozen same-day
    # baseline lookup).  Counterfactual Follow-Up Starvation Fix v1 moved
    # every *permanent* CRYPTO baseline block out of this counter and into
    # ``permanently_unavailable_count`` below -- this field's meaning is
    # otherwise unchanged and it still counts toward ``genuine_attempts``.
    blocked_count: int
    # Counterfactual Follow-Up Starvation Fix v1: CRYPTO pairs whose
    # baseline is proven (see ``FollowUpDueState.PERMANENTLY_UNAVAILABLE``)
    # to never be able to produce genuine evidence.  Explicitly separate
    # from ``blocked_count`` (never overloaded into it) and explicitly
    # *not* counted in ``genuine_attempts``/``sample_limit`` consumption or
    # ``provider_request_count`` -- classifying one costs nothing but the
    # same zero-provider-request local read ``blocked_count`` CRYPTO cases
    # already made anyway.
    permanently_unavailable_count: int
    skipped_not_due_count: int
    skipped_unsupported_count: int
    skipped_already_observed_count: int
    provider_request_count: int
    entries: tuple[FollowUpSyncEntry, ...]


class SyncFollowUpObservations:
    def __init__(
        self,
        decisions: DecisionSource,
        subjects: SharedOpportunitySubjectSource,
        crypto_prices: CryptoPriceSource,
        kalshi_source: KalshiMarketSource,
        repository: FollowUpRepository,
        # Securities Research -> Shared Opportunity Recurring Bridge v1.
        # Optional, keyword-only, defaults to ``None`` -- deliberately, so
        # every one of this constructor's ~39 existing call sites (CLI,
        # recurring cycle, both test suites) keeps working completely
        # unchanged rather than needing a new required positional argument
        # threaded through all of them. A SECURITIES candidate reached with
        # ``self._securities_prices is None`` fails closed to
        # ``BLOCKED_UNAVAILABLE`` (see ``execute``) exactly as if the
        # underlying's terminal price genuinely were not yet retained --
        # never a crash, never silently skipped without a reason.
        securities_prices: SecuritiesUnderlyingPriceSource | None = None,
        # Kalshi Settlement & Resolution Evidence Maturation v1. Optional,
        # keyword-only, defaults to a fresh, empty
        # ``InMemoryKalshiResolutionFactRepository`` -- deliberately, so
        # every one of this constructor's existing call sites keeps working
        # unchanged. Unlike ``securities_prices`` above, ``None`` here never
        # means "fail closed": the authoritative-gating rule
        # (``_read_kalshi_with_resolution_gate`` only ever trusting a
        # settlement result once ``domain.kalshi_resolution.
        # build_kalshi_resolution_fact`` classifies the read ``FINALIZED``)
        # is applied unconditionally regardless of which repository is
        # injected -- a caller that omits this argument gets fully correct,
        # safe gating; it only loses this one instance's own avoid-
        # hammering/cross-cycle-durability benefit, which needs a real
        # (Postgres) repository to matter at all, since the unattended
        # recurring-cycle worker is a fresh process every tick. Production
        # wiring (``adapters.operating_cycle_tasks``) always passes the
        # real, engine-backed repository explicitly.
        resolution_facts: KalshiResolutionFactRepository | None = None,
    ) -> None:
        self._decisions = decisions
        self._subjects = subjects
        self._crypto_prices = crypto_prices
        self._kalshi_source = kalshi_source
        self._repository = repository
        self._securities_prices = securities_prices
        self._resolution_facts = resolution_facts or InMemoryKalshiResolutionFactRepository()

    def _build_crypto(
        self,
        *,
        decision: CounterfactualDecision,
        projection: SharedOpportunityProjection,
        horizon_kind: FollowUpHorizonKind,
        baseline_kind: FollowUpBaselineKind,
        horizon_days: int,
        policy_anchor_at: datetime,
        baseline_time: datetime,
        target_time: datetime,
        observed_at: datetime,
        measurement_as_of: datetime,
        policy_version: str,
        at_baseline: CryptoPriceLookup,
    ) -> DecisionFollowUpObservation:
        """``at_baseline`` (Counterfactual Follow-Up Starvation Fix v1) is
        now always resolved by the caller (``execute``, via the shared
        module-level ``_crypto_baseline_reference``) *before* this method is
        ever invoked, and is always already non-``None`` here -- resolving
        it first, before spending any attempt-budget, is exactly what lets
        ``execute`` classify a missing baseline as either ``FollowUpDueState.
        PERMANENTLY_UNAVAILABLE`` (every policy after V1: proven permanent,
        see that state's own docstring) or a genuine, still-transient
        ``BLOCKED_UNAVAILABLE`` (``FOLLOWUP_POLICY_V1``'s own frozen
        same-day lookup, unchanged) *without* ever calling this method at
        all for the permanent case.  This method itself therefore no longer
        needs -- and no longer has -- any code path for a missing baseline;
        it only ever resolves the *target* side, which stays exactly as
        undecided-until-attempted as before.

        Counterfactual Forward Outcome Temporal Integrity v1: under
        ``FOLLOWUP_POLICY_V3`` the very same pre-resolved ``at_baseline``
        value is no longer a measurement endpoint at all -- it is the
        *decision-input reference* (``_crypto_decision_input_reference``),
        retained as disclosure and never used in ``subsequent_return``.  V3
        resolves both of its own endpoints here instead, from exact buckets
        derived solely from ``policy_anchor_at``/``horizon_days``, each
        bounded by the declared ``measurement_as_of``.  One pre-resolved
        value with two version-specific roles is deliberate and is exactly
        what ``_crypto_baseline_reference`` already documents: the value is
        resolved once, by the one shared function both services call, so the
        provably-permanent classification stays identical between them."""

        product_id = projection.product_or_market_id
        if policy_version == FOLLOWUP_POLICY_V3:
            return self._build_crypto_forward_window(
                decision=decision,
                projection=projection,
                horizon_kind=horizon_kind,
                baseline_kind=baseline_kind,
                horizon_days=horizon_days,
                policy_anchor_at=policy_anchor_at,
                baseline_time=baseline_time,
                target_time=target_time,
                observed_at=observed_at,
                measurement_as_of=measurement_as_of,
                decision_input=at_baseline,
            )
        # Target-bucket-from-baseline-candle (Part: pre-deployment daily-
        # candle horizon/bucket-correctness audit; found and fixed alongside
        # the availability-as-of audit above, both before FOLLOWUP_POLICY_V2
        # had ever been synced against the live database -- zero retained V2
        # rows of either baseline kind at the time, so this is a direct
        # correction to V2 itself, not a new policy version, for the same
        # reason the availability-as-of fix above is not one either).
        #
        # A retained daily candle's ``close`` is the closing price of the
        # trading period *starting* at its ``bucket_start`` -- i.e. it
        # economically represents the instant ``bucket_start + 1 day``, not
        # ``bucket_start`` itself (standard OHLC convention; traced through
        # ``adapters.coinbase_universe_public``'s raw candle parsing, which
        # stamps ``bucket_start`` from the provider's own period-*start*
        # timestamp unchanged).  ``latest_close_before(before=boundary)``
        # therefore resolves to a candle economically representing exactly
        # ``boundary`` itself (it selects the latest ``bucket_start`` <
        # ``boundary``, whose close lands one day later, i.e. back on
        # ``boundary``) -- confirmed by ``at_baseline.bucket_start + 1 day
        # == boundary`` whenever no capture gap forced it further back.
        #
        # The old computation here -- ``completed_utc_day(target_time)``,
        # unchanged from FOLLOWUP_POLICY_V1 -- picked an *exact* bucket
        # ``horizon_days`` after that same boundary, whose close therefore
        # represents ``boundary + horizon_days + 1`` -- one calendar day
        # *later* than the ``boundary + horizon_days`` a "SHORT_HORIZON =
        # 1 day" observation is supposed to measure, because it re-derived
        # the target bucket from the boundary independently instead of from
        # where the baseline candle actually landed.  Harmless for V1 (V1's
        # own baseline is also an *exact* bucket at that same boundary, so
        # the same +1 day offset appears on both sides and cancels out of
        # the elapsed interval) -- but genuinely wrong once the baseline
        # side moved to "latest retained close before," which shifts only
        # the baseline, not the target, by one bucket.
        #
        # Deriving the target bucket from ``at_baseline.bucket_start``
        # itself -- not from a boundary re-derived from ``target_time`` --
        # fixes this exactly, and stays correct even in the rare case
        # ``at_baseline`` itself had to walk back more than one day for a
        # genuine capture gap (Part: ``CryptoPriceSource.latest_close_
        # before``'s own gap-tolerance docstring): whatever calendar
        # interval the baseline candle actually spans back from its
        # boundary, the target candle spans forward by exactly the same
        # ``horizon_days`` from there, so the two retained closes this
        # observation actually compares are always exactly ``horizon_days``
        # calendar days apart -- never ``horizon_days`` plus however many
        # days a gap-free assumption would have silently added.  Still an
        # *exact* bucket match, never a nearest-available substitution
        # (Part 5) -- a missing target bucket still fails closed as
        # ``CRYPTO_TARGET_BUCKET_NOT_YET_RETAINED``, exactly as before.
        #
        # V1 is untouched: ``at_baseline`` was resolved via
        # ``_crypto_baseline_reference``'s own early ``FOLLOWUP_POLICY_V1``
        # case regardless (the caller resolves it the same way for every
        # policy version), and this method's own target-bucket computation
        # is keyed on the same ``policy_version``, so every one of the 60
        # real retained V1 rows reproduces the exact target bucket -- and
        # therefore the exact ``reference_price_at_target``/
        # ``canonical_hash`` -- it always did.
        target_bucket = (
            completed_utc_day(target_time)
            if policy_version == FOLLOWUP_POLICY_V1
            else at_baseline.bucket_start + timedelta(days=horizon_days)
        )
        at_target = self._crypto_prices.daily_close(
            product_id=product_id, bucket_start=target_bucket
        )
        if at_target is None:
            raise FollowUpSourceUnavailable("CRYPTO_TARGET_BUCKET_NOT_YET_RETAINED")
        return build_crypto_price_movement_observation(
            decision=decision,
            horizon_kind=horizon_kind,
            baseline_kind=baseline_kind,
            horizon_days=horizon_days,
            baseline_time=baseline_time,
            target_time=target_time,
            subject_instrument_id=projection.instrument_id,
            subject_product_or_market_id=projection.product_or_market_id,
            price_at_baseline=at_baseline.close,
            price_at_baseline_source_observation_id=at_baseline.source_observation_id,
            price_at_baseline_evidence_class=at_baseline.evidence_class,
            price_at_target=at_target.close,
            price_at_target_source_observation_id=at_target.source_observation_id,
            price_at_target_evidence_class=at_target.evidence_class,
            observed_at=observed_at,
            policy_version=policy_version,
        )

    def _build_crypto_forward_window(
        self,
        *,
        decision: CounterfactualDecision,
        projection: SharedOpportunityProjection,
        horizon_kind: FollowUpHorizonKind,
        baseline_kind: FollowUpBaselineKind,
        horizon_days: int,
        policy_anchor_at: datetime,
        baseline_time: datetime,
        target_time: datetime,
        observed_at: datetime,
        measurement_as_of: datetime,
        decision_input: CryptoPriceLookup,
    ) -> DecisionFollowUpObservation:
        """``FOLLOWUP_POLICY_V3``'s measurement, in one place.

        Both endpoints follow the identical five-step contract, in this
        order, with no step ever skipped or reordered:

        1. derive the bucket **solely** from ``policy_anchor_at`` and
           ``horizon_days`` (``forward_measurement_window``) -- never from a
           resolved candle, never from retained data, never from
           ``measurement_as_of``.  This is what makes the window a fixed,
           declared economic fact about the decision rather than a function
           of whatever evidence happened to exist, and it is the direct
           correction to ``FOLLOWUP_POLICY_V2``'s target-bucket-from-
           baseline-candle derivation;
        2. require evidence eligibility, and
        3. require ``batch.completed_at <= measurement_as_of`` -- both
           applied before any ranking, inside ``daily_close_as_of``;
        4. choose deterministically by the total order
           ``(completed_at, observation_id)``;
        5. if absent, fail closed as ``MEASUREMENT_UNAVAILABLE``.  Never
           walk backward, never substitute the nearest available bucket.

        ``baseline_time``/``target_time`` are re-derived here and asserted
        against what the caller computed rather than merely trusted: the
        caller reaches them through ``_horizon_anchor_at`` ->
        ``determine_followup_due_state``, a genuinely different path, so
        agreeing on W0/W1 from two independent derivations is a real check
        that the due/measurement boundaries have not silently diverged.

        The window-start and window-end failures carry *distinct* reason
        codes: in practice a missing window-end (the freshest bar, captured
        only after the window closes) is the ordinary, expected transient
        case, while a missing window-start points at a genuine historical
        capture gap -- an operator reading ``reasons`` alone must be able to
        tell those apart.  Both are honestly transient: V3's availability
        gate advances with ``measurement_as_of``, so a later cycle can
        legitimately succeed where this one could not (contrast the
        decision-input reference's frozen boundary, which is provably
        permanent and is classified before any budget is spent)."""

        window = forward_measurement_window(
            policy_anchor_at=policy_anchor_at, horizon_days=horizon_days
        )
        if window.window_start != baseline_time or window.window_end != target_time:
            raise FollowUpSourceUnavailable(CRYPTO_V3_WINDOW_DERIVATION_DISAGREEMENT)
        product_id = projection.product_or_market_id
        at_window_start = self._crypto_prices.daily_close_as_of(
            product_id=product_id,
            bucket_start=window.window_start_bucket,
            measurement_as_of=measurement_as_of,
        )
        if at_window_start is None:
            raise FollowUpSourceUnavailable(CRYPTO_V3_WINDOW_START_MEASUREMENT_UNAVAILABLE)
        at_window_end = self._crypto_prices.daily_close_as_of(
            product_id=product_id,
            bucket_start=window.window_end_bucket,
            measurement_as_of=measurement_as_of,
        )
        if at_window_end is None:
            raise FollowUpSourceUnavailable(CRYPTO_V3_WINDOW_END_MEASUREMENT_UNAVAILABLE)
        return build_crypto_price_movement_observation(
            decision=decision,
            horizon_kind=horizon_kind,
            baseline_kind=baseline_kind,
            horizon_days=horizon_days,
            baseline_time=window.window_start,
            target_time=window.window_end,
            subject_instrument_id=projection.instrument_id,
            subject_product_or_market_id=product_id,
            price_at_baseline=at_window_start.close,
            price_at_baseline_source_observation_id=at_window_start.source_observation_id,
            price_at_baseline_evidence_class=at_window_start.evidence_class,
            price_at_target=at_window_end.close,
            price_at_target_source_observation_id=at_window_end.source_observation_id,
            price_at_target_evidence_class=at_window_end.evidence_class,
            observed_at=observed_at,
            policy_version=FOLLOWUP_POLICY_V3,
            measurement_as_of=measurement_as_of,
            policy_anchor_at=policy_anchor_at,
            decision_input_reference_price=decision_input.close,
            decision_input_reference_bucket_start=decision_input.bucket_start,
            decision_input_source_observation_id=decision_input.source_observation_id,
            decision_input_evidence_class=decision_input.evidence_class,
        )

    def _read_kalshi_with_resolution_gate(
        self,
        *,
        decision: CounterfactualDecision,
        instrument_id: str,
        product_or_market_id: str,
        observed_at: datetime,
        timeout_seconds: int,
    ) -> KalshiMarketRead:
        """Kalshi Settlement & Resolution Evidence Maturation v1. Wraps
        ``self._kalshi_source.read_market_status`` with an idempotent,
        point-in-time-safe resolution-fact cache/gate:

        * a per-ticker ``KalshiMarketResolutionFact`` already retained as of
          ``observed_at`` and already ``FINALIZED`` needs no live request at
          all -- the market cannot un-resolve, so the read is answered
          entirely from already-retained evidence (avoids hammering the
          provider, Part 6, and makes replay deterministic, Part 7 -- a
          re-run for the identical cycle answers from the identical
          already-retained fact rather than risking a different live read);
        * otherwise, the existing live read runs exactly as before
          (unchanged call, unchanged budget accounting via the caller's own
          bounded source wrapper), and -- for a genuine single-market read
          only (a multi-leg event read has no one ticker to key a
          resolution fact by, and its ``settlement_result`` stays exactly
          the comma-joined, never-``("YES","NO")`` string it always was,
          per ``KalshiFollowUpPublicMarketSource._read_event``) -- its raw
          fields are folded into a new immutable ``KalshiMarketResolutionFact``
          and persisted idempotently before this method returns.

        The ``settlement_result`` this method hands back is never the raw,
        ungated provider ``result`` string once a single ticker is
        identified: it is always ``fact.resolved_result`` -- ``None``
        unless ``fact.resolution_state is KalshiResolutionState.FINALIZED``
        -- so a provisional ``determined``/``disputed``/``amended`` result
        can never reach ``DecisionFollowUpObservation.settlement_result``
        (Governing Constraint: Apex must never treat an unresolved Kalshi
        market as if its outcome were known).

        Best-effort cache key, honestly disclosed: the pre-live-call check
        below looks up by ``instrument_id`` -- the only ticker-shaped value
        available before a live read ever resolves anything. For
        ``BinaryComplementEvaluation`` decisions with no retained event
        ticker, ``instrument_id`` genuinely *is* the market ticker (see
        ``adapters.counterfactual_followup_sources``'s own module
        docstring), so this pre-check reliably hits once any prior read for
        the identical decision/ticker has persisted a ``FINALIZED`` fact --
        proven by this module's own recurring-cycle regression coverage
        (two horizon-kind candidates for one decision, and two separate
        decisions sharing one ticker, each costing exactly one genuine live
        request between them). When ``instrument_id`` is an event ticker
        instead (the other two origins, or a ``BinaryComplementEvaluation``
        whose event ticker *was* retained), this pre-check simply misses
        every time and falls through to the unchanged live read below --
        never incorrect, just not optimized for that shape in this
        milestone; a future milestone could close that gap with an
        explicit ``instrument_id -> market_ticker`` resolution index if the
        avoided-request volume ever justifies the added complexity."""

        cached = self._resolution_facts.latest_fact(market_ticker=instrument_id, as_of=observed_at)
        if cached is not None and cached.resolution_state is KalshiResolutionState.FINALIZED:
            return KalshiMarketRead(
                market_status=cached.provider_status,
                yes_bid=None,
                yes_ask=None,
                settlement_result=cached.resolved_result,
                # Mirrors the live-read branch below and the established
                # convention (``adapters.counterfactual_followup_sources``'
                # own ``settlement_source=... if settlement_result else
                # None``): a FINALIZED fact can still legitimately carry no
                # result (the provider genuinely never sent one -- see
                # ``domain.kalshi_resolution``'s "missing result on a
                # finalized market" case), and source is never stamped
                # for a result that isn't there.
                settlement_source=(
                    KALSHI_RESOLUTION_FACT_SOURCE if cached.resolved_result is not None else None
                ),
                source_endpoint_identity=cached.source_endpoint_identity,
                request_count=0,
                market_ticker=cached.market_ticker,
                event_ticker=cached.event_ticker,
                settlement_value_dollars=cached.settlement_value_dollars,
                settlement_ts=cached.settlement_ts,
                close_time=cached.close_time,
            )

        read = self._kalshi_source.read_market_status(
            subject_source_record_type=decision.subject_source_record_type,
            instrument_id=instrument_id,
            product_or_market_id=product_or_market_id,
            timeout_seconds=timeout_seconds,
        )
        if read.market_ticker is None:
            # Multi-leg event read (or a source double that doesn't
            # populate the new raw fields) -- no single ticker to key a
            # resolution fact by. Return the read exactly as today,
            # unchanged: its ``settlement_result`` was already never a
            # clean ``("YES", "NO")`` value in this shape (see
            # ``MULTI_LEG_PRICE_LIMITATION``), so there is no governing-
            # constraint exposure here to gate.
            return read

        fact = build_kalshi_resolution_fact(
            market_ticker=read.market_ticker,
            event_ticker=read.event_ticker,
            provider_status=read.market_status,
            raw_result=read.settlement_result,
            settlement_value_dollars=read.settlement_value_dollars,
            settlement_ts=read.settlement_ts,
            close_time=read.close_time,
            source_endpoint_identity=read.source_endpoint_identity,
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            captured_at=observed_at,
            run_id=f"followup-kalshi-resolution-{observed_at.isoformat()}",
        )
        self._resolution_facts.record(fact=fact)
        return KalshiMarketRead(
            market_status=read.market_status,
            yes_bid=read.yes_bid,
            yes_ask=read.yes_ask,
            settlement_result=fact.resolved_result,
            settlement_source=(
                KALSHI_RESOLUTION_FACT_SOURCE
                if fact.resolved_result is not None
                else read.settlement_source
            ),
            source_endpoint_identity=read.source_endpoint_identity,
            request_count=read.request_count,
            source_limitations=read.source_limitations,
            market_ticker=read.market_ticker,
            event_ticker=read.event_ticker,
            settlement_value_dollars=read.settlement_value_dollars,
            settlement_ts=read.settlement_ts,
            close_time=read.close_time,
        )

    def _build_kalshi(
        self,
        *,
        decision: CounterfactualDecision,
        projection: SharedOpportunityProjection,
        horizon_kind: FollowUpHorizonKind,
        horizon_days: int,
        target_time: datetime,
        observed_at: datetime,
        timeout_seconds: int,
        policy_version: str,
    ) -> tuple[DecisionFollowUpObservation, int]:
        read = self._read_kalshi_with_resolution_gate(
            decision=decision,
            instrument_id=projection.instrument_id,
            product_or_market_id=projection.product_or_market_id,
            observed_at=observed_at,
            timeout_seconds=timeout_seconds,
        )
        observation = build_kalshi_market_status_observation(
            decision=decision,
            horizon_kind=horizon_kind,
            horizon_days=horizon_days,
            target_time=target_time,
            subject_instrument_id=projection.instrument_id,
            subject_product_or_market_id=projection.product_or_market_id,
            market_status=read.market_status,
            yes_bid=read.yes_bid,
            yes_ask=read.yes_ask,
            settlement_result=read.settlement_result,
            settlement_source=read.settlement_source,
            source_endpoint_identity=read.source_endpoint_identity,
            observed_at=observed_at,
            additional_limitations=read.source_limitations,
            policy_version=policy_version,
        )
        return observation, read.request_count

    def _build_securities(
        self,
        *,
        decision: CounterfactualDecision,
        baseline: _SecuritiesStructureBaseline,
        horizon_kind: FollowUpHorizonKind,
        horizon_days: int,
        target_time: datetime,
        subject_instrument_id: str,
        subject_product_or_market_id: str,
        capital_required: Decimal,
        observed_at: datetime,
        policy_version: str,
    ) -> DecisionFollowUpObservation:
        """``baseline`` is always already resolved by the caller (``execute``,
        via ``_securities_structure_baseline_from_projection``) before this
        method is ever invoked -- mirrors ``_build_crypto``'s own ``at_
        baseline`` contract exactly. Makes zero provider requests: the target
        side is resolved from ``self._securities_prices``, which -- like
        ``self._crypto_prices`` -- only ever reads already-retained evidence
        (Part 7: this bridge never triggers new options capture)."""

        assert self._securities_prices is not None  # execute() already checked
        at_target = self._securities_prices.earliest_reference_price_at_or_after(
            underlying_canonical_security_id=baseline.underlying_canonical_security_id,
            at_or_after=target_time,
        )
        if at_target is None:
            raise FollowUpSourceUnavailable("SECURITIES_TERMINAL_SNAPSHOT_NOT_YET_RETAINED")
        return build_securities_structure_terminal_observation(
            decision=decision,
            horizon_kind=horizon_kind,
            horizon_days=horizon_days,
            target_time=target_time,
            subject_instrument_id=subject_instrument_id,
            subject_product_or_market_id=subject_product_or_market_id,
            structure_type=baseline.structure_type,
            long_strike=baseline.long_strike,
            short_strike=baseline.short_strike,
            multiplier=baseline.multiplier,
            quantity=baseline.quantity,
            initial_cash_flow=baseline.initial_cash_flow,
            capital_required=capital_required,
            underlying_price_at_baseline=baseline.underlying_reference_price_at_decision,
            underlying_price_at_baseline_source_id=baseline.source_chain_snapshot_id,
            underlying_price_at_target=at_target.underlying_reference_price,
            underlying_price_at_target_source_id=at_target.source_snapshot_id,
            observed_at=observed_at,
            policy_version=policy_version,
        )

    def execute(
        self,
        *,
        as_of: datetime,
        sample_limit: int = DEFAULT_SAMPLE_LIMIT,
        candidate_scan_limit: int = DEFAULT_CANDIDATE_SCAN_LIMIT,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        policy_version: str = FOLLOWUP_POLICY_V1,
        venues: tuple[SharedVenue, ...] | None = None,
        measurement_as_of: datetime | None = None,
    ) -> FollowUpSyncResult:
        """``policy_version`` defaults to ``FOLLOWUP_POLICY_V1`` for the same
        reason every other entry point in this module does (see
        ``_validate_policy_version``) -- the recurring operating-cycle task
        and the CLI's own default are the two places that explicitly pass
        ``domain.counterfactual_followup.current_followup_policy_version``
        (per venue) instead.  Because ``followup_observation_id`` (and therefore
        ``existing``/``already_observed`` below) always incorporates
        ``policy_version`` (and, from any policy after V1, ``baseline_kind``
        whenever it is ``ORIGIN_EVIDENCE``), a decision already observed
        under one policy version -- or one baseline kind -- is never
        mistaken for "already observed" under a different one: an
        already-retained V1 row does not satisfy V2's backlog, and a
        genuine V2 sync always attempts its own independent ``ORIGIN_
        EVIDENCE`` and ``DECISION_TIME`` observations even when V1's single
        row is already retained (Part: V1/V2 coexistence).  ``sample_limit``
        bounds genuine *attempts* across every ``(decision, horizon_kind,
        baseline_kind)`` triple, oldest-decision-first -- a CRYPTO decision
        under any policy after V1 can therefore cost up to twice as many
        attempts as it did under V1 (two baseline kinds x two horizon
        kinds), which is an intended consequence of measuring both
        questions honestly, not a regression in this bound's own meaning.

        Counterfactual Follow-Up Starvation Fix v1 -- confirmed starvation
        defect: a permanently-unavailable CRYPTO baseline (see
        ``domain.counterfactual_followup.FollowUpDueState.
        PERMANENTLY_UNAVAILABLE``) used to cost one genuine attempt just
        like any other ``DUE`` pair, forever, on every cycle it was ever
        reconsidered -- and because it is never durably marked observed, it
        sorts first again (oldest-due-first) on every later cycle too.  A
        fixed cohort of 40+ such pairs is enough, by itself, to consume this
        method's entire ``sample_limit`` every single cycle, permanently
        starving every later-sorting, genuinely satisfiable CRYPTO or KALSHI
        pair of any progress at all.  The fix: such a pair is now classified
        ``PERMANENTLY_UNAVAILABLE`` -- via the same zero-provider-request
        local read this method already had to perform anyway (see
        ``_crypto_baseline_reference``) -- *before* ``attempted``/
        ``sample_limit`` is ever charged for it, so it can never again
        consume attempt-budget or provider-request budget that later,
        genuinely satisfiable work needs.  ``FOLLOWUP_POLICY_V1``'s own
        frozen same-day baseline lookup is untouched: it stays exactly the
        transient ``BLOCKED_UNAVAILABLE`` it always was, one genuine attempt
        exactly as before (Part: V1 immutability).

        ``candidate_scan_limit`` is a second, independent, deliberately much
        larger bound on how many ``(decision, horizon_kind, baseline_kind)``
        triples this one call will even *look at* -- across every outcome,
        not only genuine attempts (Part: "no unbounded full-table/provider
        loop").  Classifying a pair ``PERMANENTLY_UNAVAILABLE`` costs zero
        ``sample_limit``/attempt-budget and zero provider requests no matter
        how many times it recurs in one call -- but it still costs one unit
        of ``candidate_scan_limit``, exactly like every other outcome this
        method reports (``OBSERVED``, ``NOT_YET_DUE``, ``UNSUPPORTED``,
        transient ``BLOCKED_UNAVAILABLE``, and a genuine attempt alike), so
        ``candidate_scan_limit`` being "generous" is not, by itself, a
        starvation-freedom guarantee at *any* fixed value: an oldest-sorting
        prefix that is not a fresh genuine attempt -- permanently blocked,
        already observed, or simply not yet due for a long declared horizon
        -- still exceeds any fixed constant eventually (``OBSERVED`` pairs
        alone accumulate at the front of this same order for as long as
        Apex keeps operating).  Counterfactual Follow-Up Long-Run Fairness
        v1 (see the module docstring) is what actually makes this
        unconditional: ``decisions`` is rotated to resume immediately after
        wherever the previous *completed* cycle's scan actually stopped
        (``_rotate_from_cursor``), wrapping back to the oldest decision once
        the newest is passed, so no finite oldest-sorting prefix can block a
        later-sorting satisfiable pair forever -- only for a bounded number
        of cycles, however large that prefix grows.  ``candidate_scan_
        limit`` remains exactly what its name says: a per-invocation wall-
        clock safety net bounding how much work *one call* does, never the
        mechanism that guarantees later work is eventually reached -- that
        guarantee is now the cursor's job.

        Counterfactual Forward Outcome Temporal Integrity v1 adds two
        parameters, both defaulted so every pre-existing call site is
        byte-for-byte unchanged:

        ``venues`` scopes one run to a subset of venues.  The composition
        roots now run this once per version in the current per-venue
        selection -- ``FOLLOWUP_POLICY_V3`` for CRYPTO, ``FOLLOWUP_POLICY_
        V2`` for KALSHI and SECURITIES -- each scoped to that version's own
        venues (``domain.counterfactual_followup.current_followup_policy_
        venues``).  Two runs rather than one mixed run is deliberate: the
        durable scan cursor, the attempt budget, and every count this method
        returns are all genuinely per-policy-version facts already, and
        keeping them that way means this milestone changes nothing about
        ``followup_scan_cursor``, ``FollowUpScanPosition``, or
        ``FollowUpSyncResult``.  It also gives CRYPTO/V3 and
        KALSHI+SECURITIES/V2 independent fairness cursors and independent
        budgets, so neither venue group's backlog can starve the other's.

        ``measurement_as_of`` is the instant every ``FOLLOWUP_POLICY_V3``
        endpoint lookup is bounded by, declared *here*, at dispatch, and
        defaulting to ``as_of``.  It is a separate parameter rather than
        simply reusing ``as_of`` so an explicit replay can genuinely
        reproduce an earlier measurement: replaying with the original
        ``measurement_as_of`` yields the identical availability, prices,
        return and ``canonical_hash`` even after newer historical batches
        have landed, while ``as_of``/``observed_at`` honestly record when
        the replay itself ran.  It is never derived from ``observed_at``
        (which is deliberately excluded from the canonical hash and
        therefore cannot carry measurement semantics).  Ignored entirely by
        every policy version before V3."""

        if sample_limit < 0:
            raise ValueError("FOLLOWUP_SYNC_SAMPLE_LIMIT_INVALID")
        if candidate_scan_limit < 0:
            raise ValueError("FOLLOWUP_SYNC_CANDIDATE_SCAN_LIMIT_INVALID")
        _validate_policy_version(policy_version)
        measurement_instant = as_of if measurement_as_of is None else measurement_as_of
        entries: list[FollowUpSyncEntry] = []
        created = 0
        replayed = 0
        blocked = 0
        permanently_unavailable = 0
        skipped_not_due = 0
        skipped_unsupported = 0
        skipped_already_observed = 0
        provider_requests = 0
        attempted = 0
        candidates_examined = 0
        decisions = tuple(
            sorted(
                self._decisions.all_decisions(),
                key=lambda item: (item.evaluated_at, item.decision_id),
            )
        )
        # Counterfactual Follow-Up Long-Run Fairness v1: resume immediately
        # after wherever the previous *completed* cycle's scan actually
        # stopped, instead of unconditionally restarting at the single
        # oldest decision's first candidate every cycle -- see the module
        # docstring and ``_rotate_from_cursor`` for why ``candidate_scan_
        # limit`` alone cannot guarantee later work is ever reached, no
        # matter its value.
        candidates = _rotate_from_cursor(
            _all_candidates(decisions, policy_version=policy_version, venues=venues),
            self._repository.scan_position(policy_version=policy_version),
        )
        # The last candidate this cycle examined, regardless of its outcome
        # -- ``None`` until the first one is examined.  Candidate-granular
        # (never decision-granular): every candidate this loop reaches
        # becomes a valid resume point the instant it is examined, so even
        # a bound too tight to finish one decision's own candidates in a
        # single cycle still advances -- see ``FollowUpScanPosition``.
        last_examined: _FollowUpCandidate | None = None
        # Looked up at most once per unique decision per call, no matter how
        # many of that decision's candidates this cycle reaches -- restores
        # the original per-decision (not per-candidate) lookup frequency a
        # naive per-candidate lookup would otherwise regress, now that one
        # decision's candidates are no longer necessarily examined
        # consecutively as a single nested-loop group.
        projections: dict[str, SharedOpportunityProjection | None] = {}
        for candidate in candidates:
            if attempted >= sample_limit or candidates_examined >= candidate_scan_limit:
                break
            candidates_examined += 1
            last_examined = candidate
            decision = candidate.decision
            horizon_kind = candidate.horizon_kind
            baseline_kind = candidate.baseline_kind
            if decision.subject_projection_id not in projections:
                projections[decision.subject_projection_id] = self._subjects.projection(
                    decision.subject_projection_id
                )
            projection = projections[decision.subject_projection_id]
            expected_holding_days = None if projection is None else projection.expected_holding_days
            horizon_anchor_at = _horizon_anchor_at(
                decision=decision,
                projection=projection,
                baseline_kind=baseline_kind,
                policy_version=policy_version,
            )
            observation_id = followup_observation_id(
                decision_id=decision.decision_id,
                policy_version=policy_version,
                horizon_kind=horizon_kind,
                baseline_kind=baseline_kind,
            )
            existing = self._repository.observation(observation_id)
            due = determine_followup_due_state(
                horizon_kind=horizon_kind,
                horizon_anchor_at=horizon_anchor_at,
                expected_holding_days=expected_holding_days,
                as_of=as_of,
                already_observed=existing is not None,
            )
            if due.state is FollowUpDueState.OBSERVED:
                skipped_already_observed += 1
                continue
            if due.state is FollowUpDueState.NOT_YET_DUE:
                skipped_not_due += 1
                continue
            if due.state is FollowUpDueState.UNSUPPORTED:
                skipped_unsupported += 1
                continue
            assert due.state is FollowUpDueState.DUE
            assert due.horizon_days is not None and due.target_time is not None
            if projection is None:
                attempted += 1
                blocked += 1
                entries.append(
                    FollowUpSyncEntry(
                        decision_id=decision.decision_id,
                        venue=decision.venue,
                        horizon_kind=horizon_kind,
                        baseline_kind=baseline_kind,
                        outcome="BLOCKED_UNAVAILABLE",
                        observation_id=None,
                        reasons=("SUBJECT_PROJECTION_NOT_RETAINED",),
                    )
                )
                continue
            if decision.venue is SharedVenue.CRYPTO:
                assert horizon_anchor_at is not None  # DUE already ruled out None
                at_baseline = _crypto_baseline_reference(
                    crypto_prices=self._crypto_prices,
                    product_id=projection.product_or_market_id,
                    decision=decision,
                    projection=projection,
                    policy_version=policy_version,
                    baseline_kind=baseline_kind,
                )
                if at_baseline is None:
                    if policy_version != FOLLOWUP_POLICY_V1:
                        # Counterfactual Follow-Up Starvation Fix v1:
                        # proven permanent (see FollowUpDueState.
                        # PERMANENTLY_UNAVAILABLE) -- classified and
                        # skipped *before* attempt/provider budget is
                        # spent, on every cycle this pair is ever
                        # reconsidered, forever.  Never durably
                        # retained (no fabricated row, exactly like
                        # BLOCKED_UNAVAILABLE); honestly re-derived,
                        # at zero incremental cost, every time.
                        permanently_unavailable += 1
                        entries.append(
                            FollowUpSyncEntry(
                                decision_id=decision.decision_id,
                                venue=decision.venue,
                                horizon_kind=horizon_kind,
                                baseline_kind=baseline_kind,
                                outcome="BLOCKED_PERMANENTLY_UNAVAILABLE",
                                observation_id=None,
                                reasons=(_crypto_permanently_unavailable_reason(policy_version),),
                            )
                        )
                        continue
                    # FOLLOWUP_POLICY_V1's own frozen, structurally
                    # transient same-day lookup (Part: V1
                    # immutability) -- unchanged: still spends one
                    # genuine attempt exactly as it always has.
                    attempted += 1
                    blocked += 1
                    entries.append(
                        FollowUpSyncEntry(
                            decision_id=decision.decision_id,
                            venue=decision.venue,
                            horizon_kind=horizon_kind,
                            baseline_kind=baseline_kind,
                            outcome="BLOCKED_UNAVAILABLE",
                            observation_id=None,
                            reasons=("CRYPTO_BASELINE_BUCKET_NOT_RETAINED",),
                        )
                    )
                    continue
                attempted += 1
                try:
                    policy_anchor_at = _policy_anchor_at(
                        decision=decision,
                        projection=projection,
                        baseline_kind=baseline_kind,
                    )
                    # Already non-``None`` here: ``horizon_anchor_at`` is
                    # non-``None`` (DUE ruled it out) and ``_policy_anchor_
                    # at`` returns ``None`` under exactly the same
                    # condition, so this narrows rather than adding a new
                    # failure mode.
                    assert policy_anchor_at is not None
                    observation = self._build_crypto(
                        decision=decision,
                        projection=projection,
                        horizon_kind=horizon_kind,
                        baseline_kind=baseline_kind,
                        horizon_days=due.horizon_days,
                        policy_anchor_at=policy_anchor_at,
                        baseline_time=horizon_anchor_at,
                        target_time=due.target_time,
                        observed_at=as_of,
                        measurement_as_of=measurement_instant,
                        policy_version=policy_version,
                        at_baseline=at_baseline,
                    )
                except FollowUpSourceUnavailable as error:
                    provider_requests += error.request_count
                    blocked += 1
                    entries.append(
                        FollowUpSyncEntry(
                            decision_id=decision.decision_id,
                            venue=decision.venue,
                            horizon_kind=horizon_kind,
                            baseline_kind=baseline_kind,
                            outcome="BLOCKED_UNAVAILABLE",
                            observation_id=None,
                            reasons=(str(error),),
                        )
                    )
                    continue
            elif decision.venue is SharedVenue.KALSHI:
                attempted += 1
                try:
                    observation, request_count = self._build_kalshi(
                        decision=decision,
                        projection=projection,
                        horizon_kind=horizon_kind,
                        horizon_days=due.horizon_days,
                        target_time=due.target_time,
                        observed_at=as_of,
                        timeout_seconds=timeout_seconds,
                        policy_version=policy_version,
                    )
                    provider_requests += request_count
                except FollowUpSourceUnavailable as error:
                    provider_requests += error.request_count
                    blocked += 1
                    entries.append(
                        FollowUpSyncEntry(
                            decision_id=decision.decision_id,
                            venue=decision.venue,
                            horizon_kind=horizon_kind,
                            baseline_kind=baseline_kind,
                            outcome="BLOCKED_UNAVAILABLE",
                            observation_id=None,
                            reasons=(str(error),),
                        )
                    )
                    continue
            elif decision.venue is SharedVenue.SECURITIES:
                # Securities Research -> Shared Opportunity Recurring Bridge
                # v1. This branch (and the defensive final ``else`` below)
                # replaced what was previously an unconditional ``else:``
                # that treated every non-CRYPTO venue as KALSHI -- correct
                # while ``SharedVenue`` had exactly two members, but would
                # have silently misrouted a SECURITIES candidate into
                # ``_build_kalshi`` (a live Kalshi public read keyed on
                # options identifiers) the moment one first became DUE.
                # Found and fixed while adding this venue, before any real
                # SECURITIES follow-up observation could ever be attempted.
                baseline = _securities_structure_baseline_from_projection(projection)
                capital_required = projection.required_hypothetical_capital
                # Each condition gets its own reason code (not one shared
                # catch-all) so a future regression in exactly one of them
                # -- e.g. the composition root forgetting to wire
                # ``securities_prices`` -- points whoever debugs it at the
                # right file instead of the wrong one.
                unavailable_reasons: list[str] = []
                if baseline is None:
                    unavailable_reasons.append("SECURITIES_STRUCTURE_SOURCE_DETAILS_NOT_RESOLVABLE")
                if capital_required is None or capital_required <= 0:
                    unavailable_reasons.append("SECURITIES_REQUIRED_CAPITAL_MISSING_OR_INVALID")
                if self._securities_prices is None:
                    unavailable_reasons.append("SECURITIES_PRICE_SOURCE_NOT_CONFIGURED")
                if unavailable_reasons:
                    attempted += 1
                    blocked += 1
                    entries.append(
                        FollowUpSyncEntry(
                            decision_id=decision.decision_id,
                            venue=decision.venue,
                            horizon_kind=horizon_kind,
                            baseline_kind=baseline_kind,
                            outcome="BLOCKED_UNAVAILABLE",
                            observation_id=None,
                            reasons=tuple(unavailable_reasons),
                        )
                    )
                    continue
                assert baseline is not None  # unavailable_reasons already ruled out None
                assert capital_required is not None  # unavailable_reasons already ruled out None
                attempted += 1
                try:
                    observation = self._build_securities(
                        decision=decision,
                        baseline=baseline,
                        horizon_kind=horizon_kind,
                        horizon_days=due.horizon_days,
                        target_time=due.target_time,
                        subject_instrument_id=projection.instrument_id,
                        subject_product_or_market_id=projection.product_or_market_id,
                        capital_required=capital_required,
                        observed_at=as_of,
                        policy_version=policy_version,
                    )
                except FollowUpSourceUnavailable as error:
                    provider_requests += error.request_count
                    blocked += 1
                    entries.append(
                        FollowUpSyncEntry(
                            decision_id=decision.decision_id,
                            venue=decision.venue,
                            horizon_kind=horizon_kind,
                            baseline_kind=baseline_kind,
                            outcome="BLOCKED_UNAVAILABLE",
                            observation_id=None,
                            reasons=(str(error),),
                        )
                    )
                    continue
            else:
                # Defensive fallback for any future ``SharedVenue`` member
                # this dispatch does not yet recognize -- fails closed to
                # ``BLOCKED_UNAVAILABLE`` for just this one candidate, never
                # raises and never aborts the whole bounded scan for every
                # other candidate this cycle. Deliberately not a bare
                # ``assert`` (which an earlier draft of this venue's own fix
                # used): an uncaught ``AssertionError`` here would be a
                # *worse* failure mode than the bug this branch replaced --
                # this repository's own recent history has already been hit
                # twice by an uncaught exception aborting an entire
                # recurring-cycle task (see the mandate-evaluation VARCHAR
                # and Kalshi baseline-quote threshold-bundle runtime
                # recoveries) -- so the next venue addition that forgets to
                # extend this dispatch degrades gracefully instead.
                attempted += 1
                blocked += 1
                entries.append(
                    FollowUpSyncEntry(
                        decision_id=decision.decision_id,
                        venue=decision.venue,
                        horizon_kind=horizon_kind,
                        baseline_kind=baseline_kind,
                        outcome="BLOCKED_UNAVAILABLE",
                        observation_id=None,
                        reasons=(f"UNRECOGNIZED_VENUE:{decision.venue.value}",),
                    )
                )
                continue
            was_replay = self._repository.save_observation(observation=observation)
            if was_replay:
                replayed += 1
            else:
                created += 1
            entries.append(
                FollowUpSyncEntry(
                    decision_id=decision.decision_id,
                    venue=decision.venue,
                    horizon_kind=horizon_kind,
                    baseline_kind=baseline_kind,
                    outcome="REUSED_IDEMPOTENT_REPLAY" if was_replay else "CREATED",
                    observation_id=observation.observation_id,
                    reasons=(),
                )
            )
        if last_examined is not None:
            self._repository.save_scan_position(
                policy_version=policy_version,
                position=FollowUpScanPosition(
                    decision_id=last_examined.decision.decision_id,
                    horizon_kind=last_examined.horizon_kind.value,
                    baseline_kind=last_examined.baseline_kind.value,
                ),
            )
        return FollowUpSyncResult(
            as_of=as_of,
            policy_version=policy_version,
            sample_limit=sample_limit,
            candidate_scan_limit=candidate_scan_limit,
            genuine_attempts=attempted,
            candidates_examined=candidates_examined,
            created_count=created,
            idempotent_replay_count=replayed,
            blocked_count=blocked,
            permanently_unavailable_count=permanently_unavailable,
            skipped_not_due_count=skipped_not_due,
            skipped_unsupported_count=skipped_unsupported,
            skipped_already_observed_count=skipped_already_observed,
            provider_request_count=provider_requests,
            entries=tuple(entries),
        )


# ==========================================================================
# CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
# Remediation v1 -- additive, idempotent, CLI-triggered-only retroactive
# evidence-quality audit over every already-retained ``DecisionFollowUp
# Observation`` (both venues, every policy version).  Mirrors ``application.
# counterfactual_classification``'s own CLI-triggered-only precedent and its
# reasons for staying out of the recurring operating cycle (see this
# module's own top docstring, Part 12) -- re-running is always safe, but
# this milestone does not add a new moving part to the cycle that currently
# protects the options-forward capture window.
# ==========================================================================


class FollowUpEvidenceQualitySource(Protocol):
    def observations(self) -> tuple[DecisionFollowUpObservation, ...]: ...

    def resolve_observation_evidence_lineage(
        self, observation: DecisionFollowUpObservation
    ) -> tuple[EvidenceClass | None, EvidenceClass | None, bool]:
        """CRYPTO-only.  Returns ``(baseline_evidence_class,
        target_evidence_class, lineage_resolved)`` -- see ``adapters.
        counterfactual_followup_postgres.PostgresCounterfactualFollowUp
        Repository.resolve_observation_evidence_lineage`` for the real
        ``source_lineage`` -> ``crypto_product_observation.evidence_class``
        join.  A caller must never invoke this for a KALSHI observation
        (its ``source_lineage`` names a provider endpoint identity, not a
        crypto batch id)."""

    def resolve_securities_terminal_evidence_timing(
        self, observation: DecisionFollowUpObservation
    ) -> SecuritiesTerminalEvidenceTiming:
        """SECURITIES only, ``FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2`` only
        (Securities Follow-Up Temporal Integrity v1).  Resolve the terminal
        snapshot's retained ``captured_at`` by its exact ``source_lineage``
        id plus the reviewed calendar bracketing ``target_time`` -- see
        ``adapters.counterfactual_followup_postgres.PostgresCounterfactual
        FollowUpRepository.resolve_securities_terminal_evidence_timing``."""


class FollowUpEvidenceQualityRepository(Protocol):
    def save_assessment(self, *, assessment: FollowUpEvidenceQualityAssessment) -> bool:
        """Persist idempotently; return ``True`` on idempotent replay of an
        already-identical retained row, ``False`` on a genuine new write --
        identical contract to ``FollowUpRepository.save_observation``."""


@dataclass(frozen=True, slots=True)
class FollowUpEvidenceQualityAssessmentEntry:
    observation_id: str
    venue: SharedVenue
    verdict: FollowUpEvidenceQualityVerdict
    evidence_defect_type: FollowUpEvidenceDefectType
    outcome: str  # CREATED | REUSED_IDEMPOTENT_REPLAY


@dataclass(frozen=True, slots=True)
class FollowUpEvidenceQualityAssessmentResult:
    assessed_at: datetime
    assessment_policy_version: str
    considered_observations: int
    eligible_count: int
    ineligible_count: int
    created_count: int
    idempotent_replay_count: int
    entries: tuple[FollowUpEvidenceQualityAssessmentEntry, ...]


class AssessFollowUpEvidenceQuality:
    """Additive, idempotent, fully re-runnable retroactive evidence-quality
    audit.  Never mutates a retained ``DecisionFollowUpObservation``; only
    ever appends a ``FollowUpEvidenceQualityAssessment`` row per
    ``(observation, assessment_policy_version)`` (Part: see ``domain.
    counterfactual_followup``'s own "CRYPTO Follow-Up Evidence Eligibility &
    Contaminated Observation Remediation v1" section for the full design).
    Assesses every retained observation, both venues -- a KALSHI
    observation is always genuinely ``ELIGIBLE`` (Part: see ``assess_
    followup_evidence_quality``'s own docstring), so "every retained
    follow-up observation has exactly one assessment under a given policy
    version" holds without a venue exception."""

    def __init__(
        self,
        source: FollowUpEvidenceQualitySource,
        repository: FollowUpEvidenceQualityRepository,
    ) -> None:
        self._source = source
        self._repository = repository

    def execute(
        self,
        *,
        assessed_at: datetime,
        assessment_policy_version: str = CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION,
    ) -> FollowUpEvidenceQualityAssessmentResult:
        if assessment_policy_version not in KNOWN_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSIONS:
            raise CounterfactualFollowUpError("FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION_UNKNOWN")
        # Securities Follow-Up Temporal Integrity v1: a venue-scoped policy
        # version (V2 is SECURITIES-only) assesses only the observations it
        # defines a verdict for; every other retained row is untouched by it.
        observations = sorted(
            (
                item
                for item in self._source.observations()
                if followup_evidence_quality_policy_supports_venue(
                    assessment_policy_version=assessment_policy_version, venue=item.venue
                )
            ),
            key=lambda item: item.observation_id,
        )
        is_v2 = assessment_policy_version == FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
        entries: list[FollowUpEvidenceQualityAssessmentEntry] = []
        eligible = 0
        ineligible = 0
        created = 0
        replayed = 0
        for observation in observations:
            securities_timing: SecuritiesTerminalEvidenceTiming | None = None
            if is_v2:
                securities_timing = self._source.resolve_securities_terminal_evidence_timing(
                    observation
                )
            if (
                observation.venue is SharedVenue.KALSHI
                or observation.venue is SharedVenue.SECURITIES
            ):
                # Both venues are always ELIGIBLE by construction (see
                # ``domain.counterfactual_followup.assess_followup_evidence_
                # quality``'s own docstring) -- neither has a separate
                # lineage-resolution question to ask, so neither should be
                # routed into ``resolve_observation_evidence_lineage``,
                # which is CRYPTO-only by its own contract (Securities
                # Research -> Shared Opportunity Recurring Bridge v1: this
                # was previously an unconditional ``else:``, which silently
                # misrouted every SECURITIES observation into that
                # CRYPTO-only resolver and always misclassified it
                # INELIGIBLE_EVIDENCE).
                baseline_class: EvidenceClass | None = None
                target_class: EvidenceClass | None = None
                resolved = True
            else:
                baseline_class, target_class, resolved = (
                    self._source.resolve_observation_evidence_lineage(observation)
                )
            assessment = assess_followup_evidence_quality(
                observation=observation,
                baseline_evidence_class=baseline_class,
                target_evidence_class=target_class,
                lineage_resolved=resolved,
                assessed_at=assessed_at,
                assessment_policy_version=assessment_policy_version,
                securities_terminal_timing=securities_timing,
            )
            was_replay = self._repository.save_assessment(assessment=assessment)
            if was_replay:
                replayed += 1
            else:
                created += 1
            if assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE:
                eligible += 1
            else:
                ineligible += 1
            entries.append(
                FollowUpEvidenceQualityAssessmentEntry(
                    observation_id=observation.observation_id,
                    venue=observation.venue,
                    verdict=assessment.verdict,
                    evidence_defect_type=assessment.evidence_defect_type,
                    outcome="REUSED_IDEMPOTENT_REPLAY" if was_replay else "CREATED",
                )
            )
        return FollowUpEvidenceQualityAssessmentResult(
            assessed_at=assessed_at,
            assessment_policy_version=assessment_policy_version,
            considered_observations=len(observations),
            eligible_count=eligible,
            ineligible_count=ineligible,
            created_count=created,
            idempotent_replay_count=replayed,
            entries=tuple(entries),
        )
