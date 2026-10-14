"""Counterfactual Decision Follow-Up Observation v1.

This module owns no provider client, credential, network, execution, or
capital-mutation capability, and it introduces no new investment judgment of
its own.  It defines the durable evidence layer that records **objective
future observations** about decisions Apex has already made and already
retained as an immutable ``domain.counterfactual_ledger.CounterfactualDecision``
-- never whether that decision was, in hindsight, "good" or "bad".

The semantic sequence this module exists to preserve, permanently:

    Decision at T  ->  objective follow-up evidence  ->  later attribution

A ``DecisionFollowUpObservation`` answers one narrow question: "for decision
D, under follow-up policy P, at horizon H, what objective fact F did Apex
observe about D's subject?"  It never answers "was D correct", "should Apex
have advanced/rejected D", or any other judgment -- those are a later
attribution milestone's job, deliberately not this one's (see the module's
own acceptance report for why).

Design choices, matching the sibling ``domain.counterfactual_classification``
module's own idiom deliberately (see that module's docstring for the full
rationale; only the deltas are repeated here):

* **Reference, don't duplicate.**  A ``DecisionFollowUpObservation`` never
  copies ``CounterfactualDecision``'s own payload.  It stores the decision's
  ``decision_id`` + ``canonical_hash`` (the same "reference by id and hash"
  idiom ``CounterfactualDecisionClassification`` already uses) plus a few
  denormalized columns (``venue``, ``disposition``) that exist purely so
  cohort queries ("what happened after decisions Apex advanced?") don't
  require a join, mirroring exactly why ``CounterfactualDecision`` itself
  denormalizes ``venue``/subject identity from ``SharedOpportunityProjection``.
* **Evidence, not judgment.**  This module has no vocabulary for
  ``GOOD_REJECTION``/``BAD_REJECTION``/``REGRET``/``SUCCESS``/``FAILURE``/
  ``SHOULD_HAVE_TRADED``/``EDGE``/``VALUE_ADDED`` and never will; it stops at
  objective, source-attributed market facts (a price, a return, a market
  status, a genuinely-evidenced settlement result).
* **Both cohorts, always.**  Nothing here reads ``CounterfactualDecision.
  disposition`` to decide *whether* to follow up -- ``ADVANCED``,
  ``CONSTRAINED``, and ``NOT_ADVANCED`` decisions are all equally eligible,
  gated only by whether the *subject* (venue, instrument, declared horizon)
  supports objective observation.  Excluding ``NOT_ADVANCED`` decisions from
  follow-up (or over-representing them) would reintroduce exactly the
  survivor bias this milestone exists to avoid.
* **Policy is versioned, explicit, and content-hash-fingerprinted**, the same
  ``<NAME>_POLICY_V1`` idiom as ``domain.counterfactual_classification.
  REASON_CLASSIFIER_V1`` / ``domain.crypto_universe.CRYPTO_UNIVERSE_POLICY_V1``
  / ``domain.total_market_context.TOTAL_MARKET_CONTEXT_POLICY_V1``.  A policy
  change is always a new version, never a silent edit to an old one's
  meaning.
* **Horizon target time is a pure function of the decision and the policy**
  (see ``determine_followup_due_state``), independent of when the follow-up
  sync happens to run and independent of when a genuine market/provider read
  happens to succeed.  ``target_time`` (what horizon this observation is
  *about*) and ``observed_at`` (when Apex actually captured it) are always
  two distinct fields on every retained row -- never conflated (Part 5/6).
* **Provenance reuses Apex's own existing evidence-class vocabulary**
  (``domain.research_evidence.EvidenceClass.GENUINE_PUBLIC`` /
  ``HISTORICAL_PUBLIC``) rather than inventing new
  ``FORWARD_OBSERVED``/``RETROSPECTIVE_BACKFILL`` names: that vocabulary
  already draws exactly this line elsewhere (see
  ``domain.crypto_research.CryptoHistoricalCapture`` attaching
  ``RETROSPECTIVE_WARNING`` specifically when ``evidence_class is
  EvidenceClass.HISTORICAL_PUBLIC``).  A CRYPTO observation carries through
  whichever evidence class the retained bar it reads was itself captured
  under; a KALSHI observation is always ``GENUINE_PUBLIC`` because Apex
  retains no historical Kalshi quote archive to backfill from in v1 -- a
  late genuine read is still a genuine (not backfilled) read, see
  ``KALSHI_LATE_OBSERVATION_LIMITATION`` for how lateness is instead
  disclosed.
* **Identity excludes wall-clock capture time; canonical_hash excludes it
  too, but includes the measured payload.**  Mirrors
  ``domain.counterfactual_classification.classification_identity``/
  ``classification_canonical_hash`` exactly: replaying the identical
  decision under the identical policy/horizon always reproduces the
  identical ``observation_id``, and -- because at most one genuine
  observation is ever attempted per ``(decision, horizon)`` (the
  application-layer sync checks for an existing row *before* attempting any
  read; see ``application.counterfactual_followup``) -- also always
  reproduces the identical ``canonical_hash``.  A hash mismatch against an
  already-retained ``observation_id`` is therefore always a genuine
  integrity conflict, never routine drift, exactly like
  ``COUNTERFACTUAL_DECISION_IDENTITY_CONFLICT``.
* **One decision, followed at N horizons, is still one decision.**  Exactly
  the ``CounterfactualDecisionClassification`` idiom: counting
  ``DecisionFollowUpObservation`` rows is never a substitute for counting
  ``CounterfactualDecision`` rows.
* **Independent of classifier version, structurally.**  Nothing in this
  module imports or references ``domain.counterfactual_classification``.  A
  future classifier v3 cannot change ``DecisionFollowUpObservation`` identity
  because there is no path by which it could -- the two modules are siblings
  that both reference ``CounterfactualDecision``, never each other.

No hypothetical fills, order probability, position sizing, realized P&L,
fees not already modeled by the origin decision, slippage, capital
reservation, opportunity cost, or alternative-allocation reasoning is
computed anywhere in this module (Part 14).  An objective subsequent return
is acceptable because its reference-price semantics are declared exactly
once, here, and never silently substituted (Part 5/7).  A settled Kalshi
outcome is acceptable because it is sourced from a genuine provider read,
never inferred from price (Part 8).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from types import MappingProxyType

from apex.domain.counterfactual_ledger import CounterfactualDecision, DecisionDisposition
from apex.domain.options_research_operations import OptionsCaptureWindowPolicy
from apex.domain.options_structures import StructureType, structure_payoff_at
from apex.domain.research_evidence import EvidenceClass
from apex.domain.securities import SecuritiesCalendarSession
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.values import content_digest, deterministic_id

COUNTERFACTUAL_FOLLOWUP_OBSERVATION_V1 = "COUNTERFACTUAL_FOLLOWUP_OBSERVATION_V1"
FOLLOWUP_POLICY_V1 = "FOLLOWUP_POLICY_V1"

# --------------------------------------------------------------------------
# FOLLOWUP_POLICY_V2 -- Counterfactual Crypto Follow-Up Point-in-Time
# Semantics v2.  Corrects one real, traced defect in how FOLLOWUP_POLICY_V1
# resolves a CRYPTO observation's ``reference_price_at_decision``; changes
# nothing else.
#
# The defect (confirmed against real retained decisions, e.g. a genuine
# ZEC-USD decision: V1 stamped 470.06, the decision's own economics actually
# priced against 467.04): a genuine crypto pulse's candle window always
# excludes the current UTC day (``bucket_start < completed_utc_day(pulse.
# as_of)`` -- see ``domain.crypto_market_pulse``/``domain.crypto_universe.
# run_discovery``/``run_relative_strength``/``run_reversal_regime``, all
# three of which read ``observation.candles[-1].close`` as the strategy's
# own "current price" input), so the *subject*
# (``SharedOpportunityProjection``) a decision is about was always priced
# against the prior completed UTC day relative to *that projection's own*
# ``generated_at`` -- never relative to whenever the eligibility decision
# about it happened to be evaluated. ``FOLLOWUP_POLICY_V1``'s
# ``build_crypto_price_movement_observation`` caller
# (``application.counterfactual_followup.SyncFollowUpObservations.
# _build_crypto``) instead resolved the decision-side bucket from
# ``completed_utc_day(decision.evaluated_at)`` directly -- the decision's own
# evaluation day, not the projection's generation day -- which is one day
# *later* than the evidence the projection's economics actually used in the
# common case, and can be arbitrarily later still whenever eligibility is
# (re-)evaluated against a projection that has gone stale (freshness/
# ``EVIDENCE_STALE`` is a rejection *reason*, not a barrier to a
# ``CounterfactualDecision`` being retained -- see the "both cohorts,
# always" design choice above). Read forward, not backward, that reads as
# genuine hindsight leakage: a reference price that was not actually knowable
# at the time the *subject* was priced.
#
# ``FOLLOWUP_POLICY_V2`` fixes this by anchoring the decision-side crypto
# lookup on the subject's own ``SharedOpportunityProjection.generated_at``
# (proven, by tracing every crypto strategy's own opportunity-construction
# code, to equal exactly the pulse ``as_of`` that bounded the candle window
# the subject's economics were computed from -- Option (C)/(D): a
# strategy-retained reference, not a foreign timestamp borrowed from a later,
# unrelated event) and asking for the latest retained daily close strictly
# before that boundary (``CryptoPriceSource.latest_close_before``) rather
# than assuming a fixed "one calendar day back" offset from an unproven
# anchor (a bare Option (B) would not, by itself, survive the stale-
# projection case above). This never re-derives anything from the
# projection's own construction logic -- only reads its already-retained,
# already-immutable ``generated_at`` field -- and it never inspects
# ``supporting_evidence_ids`` (an unordered,
# deduplicated bag by construction; not a reliable positional pointer -- see
# the milestone's own audit for why that path was rejected in favor of this
# one). A "latest strictly before" query (rather than one more exact-bucket
# lookup at a recomputed offset) is used deliberately so V2 is correct even
# in the rare case a retained batch's most recent candle lags the boundary
# by more than one day (a real, if empirically unobserved against today's
# retained data, possibility given the pulse's own ``PULSE_FRESHNESS_HOURS``
# quality-gate tolerance) -- it can never select a bucket at or after the
# boundary, which is one of two properties that actually matter for
# point-in-time correctness (Part: hindsight-leakage regression). The
# second, found by a later pre-deployment availability-as-of audit of this
# exact milestone (also before any genuine V2 sync): a candle's
# ``bucket_start`` is purely an economic label and proves nothing about
# when Apex actually came to possess it -- a backfilled or recaptured batch
# can carry an old ``bucket_start`` while itself having been retained after
# ``before``.  ``CryptoPriceSource.latest_close_before`` therefore also
# requires the candle's own retained batch to have *completed* at or before
# ``before`` -- never inferring availability from ``bucket_start`` alone
# (Part: availability-as-of correctness; see
# ``CryptoPriceSource.latest_close_before``'s own docstring for the full
# trace).
#
# ``reference_price_at_target`` *lookup mechanism* (an exact bucket, fail
# closed, never a nearest-available substitution) is unchanged in V2 -- but
# which bucket a caller asks for is not always ``completed_utc_day(target_
# time)`` any more (Part: target-bucket-from-baseline-candle).  A daily
# candle's ``close`` represents the *end* of the trading period starting at
# its ``bucket_start`` -- economically ``bucket_start`` plus one day, not
# ``bucket_start`` itself (traced through ``adapters.coinbase_universe_
# public``'s raw candle parsing).  V1's baseline and target buckets are both
# *exact* matches at boundaries exactly ``horizon_days`` apart, so that
# one-day offset appears on both sides and cancels: V1's elapsed economic
# interval is genuinely ``horizon_days``, its one real defect
# notwithstanding.  Moving only the *baseline* side to "latest retained
# close before" (one bucket earlier than V1's own exact-match boundary)
# without correspondingly adjusting the target would silently stretch every
# V2 "``SHORT_HORIZON`` = 1 day" observation to a genuine two-day economic
# interval -- a second, independent pre-deployment defect this milestone's
# own daily-candle horizon/bucket-correctness audit found and fixed
# (Part: target-bucket-from-baseline-candle) by deriving the target bucket
# from the resolved *baseline candle's own* ``bucket_start`` (``+
# horizon_days``) rather than re-deriving it from ``target_time``
# independently -- correct even when the baseline had to walk back further
# than one day for a genuine capture gap, since it measures the interval
# the two retained candles actually span, never one a gap-free assumption
# would have implied.  See ``application.counterfactual_followup.
# SyncFollowUpObservations._build_crypto`` for the exact mechanism, and
# ``CryptoPriceLookup.bucket_start`` for how the baseline candle's own
# bucket is carried forward to make this possible.
#
# A pre-deployment staleness-semantics audit of this exact milestone
# (before FOLLOWUP_POLICY_V2 had ever been synced against the live
# database -- see ``FollowUpBaselineKind`` above)
# found that leaving ``target_time`` itself always anchored to
# ``decision.evaluated_at`` -- while the *baseline* had just been moved to
# the subject's own, potentially much earlier, ``projection.generated_at`` --
# reintroduced a different, still-real defect: a nominally "SHORT_HORIZON"
# (1-day) observation could silently span several real calendar days
# whenever the originating projection was stale, blending genuine
# *pre*-decision staleness drift into what was presented as a *post*-decision
# subsequent return -- and, in a real retained example, even *flip sign*
# depending on which baseline was used. Neither a pure "always
# decision-anchored" nor a pure "always origin-anchored" answer is correct
# in isolation: they answer two different, both legitimate, research
# questions (Part: pre-deployment staleness-semantics audit; see the
# milestone's own acceptance report for the full real-data trace).
#
# ``FOLLOWUP_POLICY_V2`` therefore does not silently pick one: it retains
# both, as two structurally distinct rows per (decision, horizon) pair for
# CRYPTO, one per ``FollowUpBaselineKind`` --
#
#   * ``ORIGIN_EVIDENCE`` -- baseline *and* ``target_time`` both anchored to
#     the subject's own ``projection.generated_at``.  "How did the
#     strategy's own hypothesis play out over its own declared horizon,
#     measured purely from when its evidence was captured?"
#   * ``DECISION_TIME`` -- baseline *and* ``target_time`` both anchored to
#     ``decision.evaluated_at`` -- this is what ``FOLLOWUP_POLICY_V1`` always
#     *intended*, corrected to use ``CryptoPriceSource.latest_close_before``
#     (never the same-day, potentially-not-yet-genuinely-known exact bucket
#     V1 leaked) for the baseline side.  "What happened after Apex's own
#     decision?"
#
# Both are needed, not merely nice-to-have: retaining only the origin-to-
# target number would make it structurally impossible to later distinguish
# "the market had already moved before Apex ever evaluated this decision"
# from "the market moved after Apex's decision" -- a distinction future
# counterfactual attribution work (e.g. "did rejecting a stale Opportunity
# actually help?") depends on (Part: future attribution compatibility). No
# attribution judgment is computed here or anywhere in this module -- both
# rows remain objective, source-attributed evidence; a reader who wants the
# staleness drift between them computes ``DECISION_TIME.reference_price_at_
# baseline / ORIGIN_EVIDENCE.reference_price_at_baseline - 1`` themselves,
# from two already-retained objective facts, never from a value this module
# infers or stores as if it were itself objective.
#
# ``reference_price_at_decision`` is also renamed, to
# ``reference_price_at_baseline``, for the same reason: once a baseline can
# legitimately be the *origin* evidence rather than the decision itself, the
# old name no longer honestly describes what an ``ORIGIN_EVIDENCE`` row
# stores (Part: "do not leave misleading terminology merely because tests
# pass"). This is a pure rename plus two new fields (``baseline_kind``,
# ``baseline_time``) -- it does not, by itself, change any already-retained
# row: every historical ``canonical_hash`` and ``observation_id`` in this
# repository's live database (all ``FOLLOWUP_POLICY_V1``, both venues) is
# reproduced byte-for-byte by this exact code, because ``DECISION_TIME`` --
# what every one of those rows structurally always was -- is the one
# ``FollowUpBaselineKind`` that never widens either formula (Part: verified
# against all 60 real retained rows; see ``followup_observation_id``/
# ``followup_observation_canonical_hash``).
#
# Every KALSHI observation stays completely unaffected in substance:
# ``build_kalshi_market_status_observation`` reads a live, bounded, genuine
# public quote -- it has never had a "decision-day bucket" computation of
# any kind for V1 to get wrong, is always ``FollowUpBaselineKind.
# DECISION_TIME`` (Apex retains no historical Kalshi quote archive to
# construct an ``ORIGIN_EVIDENCE`` reading from, and this module does not
# invent new provider history merely to manufacture one), so V2 changes zero
# Kalshi behavior (see the module's own test suite for the byte-for-byte
# proof: identical objective facts, differing only in ``policy_version`` and
# the identities/hashes that are a pure function of it).
#
# ``FOLLOWUP_POLICY_V1`` is never edited, never reinterpreted, and its
# builders keep defaulting to it below -- every already-retained V1 row
# stays exactly what V1 actually computed, forever (Part: V1 immutability).
# A caller must now explicitly opt in to ``FOLLOWUP_POLICY_V2`` to get the
# corrected behavior; nothing here silently upgrades an old call site.
# ``application.counterfactual_followup`` is where that explicit choice is
# actually made (see ``KNOWN_FOLLOWUP_POLICY_VERSIONS``/
# ``CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE`` below and its call sites in the
# recurring operating cycle). Because ``FOLLOWUP_POLICY_V2`` had never been
# synced against the live database before this correction (zero retained V2
# rows of either venue at the time this was found), this is a direct,
# pre-deployment correction to V2 itself, not a new ``FOLLOWUP_POLICY_V3``
# -- repository versioning doctrine reserves a new version number for
# correcting an already-*deployed* policy's meaning, which this is not
# (Part: "do not create FOLLOWUP_POLICY_V3 merely to preserve an undeployed
# mistake").
# --------------------------------------------------------------------------
FOLLOWUP_POLICY_V2 = "FOLLOWUP_POLICY_V2"

# --------------------------------------------------------------------------
# FOLLOWUP_POLICY_V3 -- Counterfactual Forward Outcome Temporal Integrity v1.
# CRYPTO-only.  Corrects two real, traced, *already-deployed* defects in how
# ``FOLLOWUP_POLICY_V2`` measures a CRYPTO forward outcome.  Unlike every
# prior correction in this module's history -- each of which landed while
# ``FOLLOWUP_POLICY_V2`` still had zero (or, for the evidence-eligibility
# fix, one) genuinely retained row and was therefore a direct, in-place
# correction to V2 itself -- this one lands against a large, genuinely
# retained, genuinely *downstream-consumed* V2 corpus (V2 CRYPTO cohorts
# have already reached ``QUALIFIED`` and already seeded retained research
# agenda items, proposals, and feasibility rows).  Repository versioning
# doctrine reserves a new version number for correcting an already-deployed
# policy's meaning; this is exactly that, so it is a new
# ``FOLLOWUP_POLICY_Vn`` and never an edit to V2.
#
# Defect A -- pre-anchor market movement enters the "subsequent" return.
# V2 resolves its baseline as ``latest_close_before(before=completed_utc_
# day(anchor))``.  ``domain.crypto_market_pulse.completed_utc_day`` *floors*
# to the start of the anchor's own UTC day.  A retained daily candle's
# ``close`` economically represents ``bucket_start + 1 day`` (see
# ``CryptoPriceLookup.bucket_start``/``adapters.coinbase_universe_public``),
# so that lookup lands on a close representing exactly the floor itself --
# i.e. ``00:00Z`` of the anchor's own day.  For any *intraday* anchor that
# instant is strictly *before* the anchor: an anchor at ``2026-08-02
# 03:03Z`` measures from ``2026-08-02 00:00Z``, silently carrying 3h03m of
# genuinely pre-anchor market movement inside a number presented as a
# *subsequent* return; an anchor at ``21:40Z`` carries nearly a full day of
# it.  Read forward, not backward, that is the same shape of hindsight
# error V2 itself was created to fix, merely on the other side of the
# anchor.
#
# Defect B -- the forward endpoint is not replayable.  V2 resolves
# ``reference_price_at_target`` via ``CryptoPriceSource.daily_close``, which
# applies *no availability predicate at all* (see that method's own
# docstring, which says so explicitly and, for V2's own purposes, correctly:
# a forward-looking measurement is not a hindsight-leakage concern).  It
# takes whichever eligible retained batch covering the exact bucket has the
# greatest ``completed_at`` *right now*.  Nothing on a retained V2 row says
# *as of when* that "right now" was -- ``observed_at`` is deliberately
# excluded from the canonical hash and therefore cannot carry measurement
# semantics -- so the same ``observation_id``, measured today and honestly
# re-measured after a later historical backfill lands, can yield a different
# price, a different ``subsequent_return``, and a different
# ``canonical_hash``, with no retained fact anywhere that would let a reader
# detect the difference or reproduce the earlier answer.  ``daily_close``'s
# tie-break is additionally ``max(..., key=completed_at)`` -- a *partial*
# order: two batches sharing a ``completed_at`` resolve by scan order.
#
# ``FOLLOWUP_POLICY_V3`` fixes both, and in doing so separates two questions
# V2 conflated into one ``reference_price_at_baseline``:
#
#   * The **decision-input reference** -- "what historical price evidence
#     could Apex *see* at the policy anchor?"  Point-in-time constrained,
#     legitimately stale, resolved by exactly the same already-audited
#     ``latest_close_before(before=completed_utc_day(anchor))`` mechanism V2
#     already uses.  Retained on a V3 row as *disclosure only*, in its own
#     dedicated fields (``decision_input_reference_price`` and friends), and
#     **never used as either endpoint of ``subsequent_return``** -- a V3 row
#     says so in its own ``data_limitations``.  Its boundary is a frozen past
#     instant, so its absence is provably permanent by the identical
#     argument ``FollowUpDueState.PERMANENTLY_UNAVAILABLE`` already makes for
#     V2 -- which is what preserves Counterfactual Follow-Up Starvation Fix
#     v1's guarantee under V3 unchanged.
#   * The **outcome measurement endpoints** -- "what was the market price
#     across the nominal *post*-anchor economic window?"  Two exact nominal
#     buckets derived solely from the anchor and the horizon, each resolved
#     under measurement-time availability.
#
# The economic window is defined semantically, never by an ambiguously named
# floor/ceil helper (Part: Defect A's own root cause was reusing
# ``completed_utc_day``'s floor where a ceiling was meant):
#
#     W0 = first daily-close boundary at or after policy_anchor_at
#     W1 = W0 + horizon_days
#
# See ``first_daily_close_boundary_at_or_after``/``forward_measurement_
# window`` below.  Because ``W0 >= policy_anchor_at`` by construction, no
# movement occurring before the policy anchor can enter the purported
# forward return -- structurally, not by convention.  With this repository's
# daily-candle labeling convention the two exact buckets are
# ``W0 - 1 day`` (its close represents W0) and ``W1 - 1 day`` (its close
# represents W1), so the two retained closes a V3 row compares are always
# exactly ``horizon_days`` apart.
#
# ``measurement_as_of`` (mandatory on every V3 row, hashed, persisted, and
# declared at sync/task dispatch -- *never* derived retrospectively from
# ``observed_at``) bounds every V3 endpoint lookup:
# ``CryptoPriceSource.daily_close_as_of`` requires the exact bucket, an
# eligible evidence class, and ``batch.completed_at <= measurement_as_of``,
# applies that availability predicate *before* ranking, and then chooses by
# the explicit total order ``(completed_at, observation_id)``.  If nothing
# survives it fails closed -- never walking backward, never substituting the
# nearest available bucket.  The intended, deliberate consequence: a later
# sync with a later ``measurement_as_of`` may legitimately succeed once a
# historical batch arrives, while an explicit replay using the *earlier*
# ``measurement_as_of`` remains unavailable, and an explicit replay using
# the *same* ``measurement_as_of`` reproduces the identical availability,
# prices, return, and ``canonical_hash`` forever -- even after newer
# historical batches have landed.
#
# CRYPTO-only, deliberately and structurally (``FOLLOWUP_POLICY_V3_VENUES``
# below, the KALSHI/SECURITIES builders' own explicit rejection of it, and
# migration 0113's ``ck_decision_followup_v3_crypto_only``).  Neither KALSHI
# nor SECURITIES follow-up behavior has changed, so neither is semantically
# relabelled: a KALSHI observation is a live public read with no historical
# quote archive and no daily-candle window to get wrong, and a SECURITIES
# structure terminal payoff is resolved by an entirely separate mechanism
# (``build_securities_structure_terminal_observation``).  Giving either a new
# version number merely to keep one number across all venues would assert a
# semantic change that did not happen.
#
# ``FOLLOWUP_POLICY_V1`` and ``FOLLOWUP_POLICY_V2`` are never edited, never
# reinterpreted, and every already-retained row of either stays byte-for-byte
# what it always was -- including every V2 CRYPTO row and every research
# artifact already descended from one.  V3 creates *parallel corrected*
# evidence through normal versioned lineage (``domain.counterfactual_outcome.
# counterfactual_outcome_cohort_id`` already partitions on ``followup_policy_
# version``, and ``build_counterfactual_outcome_aggregates`` already
# *discovers* its cohort triples from the retained observations themselves),
# never a rewrite of what V2 concluded under its then-versioned semantics.
# --------------------------------------------------------------------------
FOLLOWUP_POLICY_V3 = "FOLLOWUP_POLICY_V3"

# Enumerated explicitly (never inferred from a free-text column) so a new
# follow-up policy version is always a deliberate, reviewed code change --
# mirrors ``domain.counterfactual_classification.KNOWN_CLASSIFIER_VERSIONS``
# exactly, including the reason it exists (Part: "must not ambiguously
# switch behavior based on whatever constant happens to be imported as
# latest").
KNOWN_FOLLOWUP_POLICY_VERSIONS: tuple[str, ...] = (
    FOLLOWUP_POLICY_V1,
    FOLLOWUP_POLICY_V2,
    FOLLOWUP_POLICY_V3,
)

# Which versions a venue that is *not* CRYPTO may ever be observed under.
# Not a policy-version property (``FOLLOWUP_POLICY_V3`` is CRYPTO-only as a
# matter of what it *means*, see its own block above); a plain, explicit,
# reviewed table so adding a venue to V3 is a deliberate code change rather
# than something that happens by omission.
FOLLOWUP_POLICY_V3_VENUES: frozenset[SharedVenue] = frozenset({SharedVenue.CRYPTO})


def followup_policy_version_supports_venue(*, policy_version: str, venue: SharedVenue) -> bool:
    """The single structural authority for "may a row of this venue ever
    exist under this policy version".  ``FOLLOWUP_POLICY_V1``/``V2`` support
    every venue (frozen -- every already-retained row of either, all three
    venues, stays exactly valid); ``FOLLOWUP_POLICY_V3`` supports CRYPTO
    only.  Restated independently at the database boundary by migration
    0113's ``ck_decision_followup_v3_crypto_only`` -- deliberately in both
    places, the same defense-in-depth discipline ``ck_decision_followup_
    kalshi_baseline_kind`` already applies to ``FollowUpBaselineKind``."""

    if policy_version == FOLLOWUP_POLICY_V3:
        return venue in FOLLOWUP_POLICY_V3_VENUES
    return True


# Counterfactual Forward Outcome Temporal Integrity v1 replaces what used to
# be a single global ``CURRENT_FOLLOWUP_POLICY_VERSION`` constant with an
# explicit per-venue selection.  The global was removed outright rather than
# deprecated in place: ``mypy --strict`` then forces every one of its former
# read sites (the recurring cycle task, the CLI, the operator API, the
# read-model benchmark script) to state *which venue(s)* it actually means,
# which is precisely the ambiguous assumption this milestone exists to
# retire.  Leaving a same-named alias behind would have let a site keep
# silently meaning "CRYPTO too" after CRYPTO stopped being V2.
#
# KALSHI and SECURITIES stay on ``FOLLOWUP_POLICY_V2`` because their
# follow-up behavior has not changed -- they are deliberately *not*
# relabelled merely so one number covers every venue (Part: "do not
# semantically relabel a venue whose behavior has not changed").
#
# This selects what is "current," never what is "valid": every earlier
# version stays fully supported (queryable, re-runnable, reproducible, and
# explicitly requestable via the CLI's own ``--policy-version`` override).
# ``build_crypto_price_movement_observation``/``build_kalshi_market_status_
# observation`` below deliberately do *not* read this themselves -- see
# their own docstrings for why.
CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE: Mapping[SharedVenue, str] = MappingProxyType(
    {
        SharedVenue.CRYPTO: FOLLOWUP_POLICY_V3,
        SharedVenue.KALSHI: FOLLOWUP_POLICY_V2,
        SharedVenue.SECURITIES: FOLLOWUP_POLICY_V2,
    }
)

# Every distinct policy version the current selection names, in a stable,
# deterministic order.  A composition root that must cover the whole
# selection iterates this and scopes each run to that version's own venues
# (``current_followup_policy_venues``) -- never sums plans across versions
# (Part: ``PlanFollowUpObservations.execute``'s own "considered_pairs
# reflects one policy version only" warning).
CURRENT_FOLLOWUP_POLICY_VERSIONS: tuple[str, ...] = tuple(
    sorted({version for version in CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE.values()})
)


def current_followup_policy_version(venue: SharedVenue) -> str:
    """The policy version new syncs/plans use for this venue when a caller
    does not explicitly pin one.  Total over ``SharedVenue`` by
    construction: a newly added venue member that is not listed above fails
    loudly here rather than silently inheriting whatever another venue
    happens to use."""

    try:
        return CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE[venue]
    except KeyError as error:
        raise CounterfactualFollowUpError(
            "COUNTERFACTUAL_FOLLOWUP_CURRENT_POLICY_VERSION_UNDECLARED_FOR_VENUE"
        ) from error


def current_followup_policy_venues(policy_version: str) -> tuple[SharedVenue, ...]:
    """The inverse of ``current_followup_policy_version`` -- which venues
    the *current* selection routes to this policy version, in a stable
    order.  Empty for a known-but-not-currently-selected version (e.g.
    ``FOLLOWUP_POLICY_V1``), which is an honest answer, not an error: V1 is
    still fully valid and still explicitly requestable, it is simply not
    what any venue is currently observed under."""

    return tuple(
        venue
        for venue in sorted(CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE, key=lambda item: item.value)
        if CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE[venue] == policy_version
    )


# --------------------------------------------------------------------------
# Counterfactual V2 Research Evidence Quarantine v1
#
# ``CURRENT_FOLLOWUP_POLICY_VERSION_BY_VENUE`` immediately above answers one
# question and says so explicitly in its own comment: *which version new
# observations are written under*.  It deliberately does not answer, and
# must not be overloaded to answer, a second and genuinely different one:
# *whether evidence already retained under an older version may still be
# the source of a **new** research conclusion*.  Those two came apart the
# moment ``FOLLOWUP_POLICY_V3`` proved, against 500 real retained rows, that
# V2's CRYPTO ``subsequent_return`` disagrees in **direction** with the
# corrected V3 measurement in 94 of 150 comparable pairs (62.7%) -- i.e. the
# majority of V2 CRYPTO returns point the wrong way.  V2 stopped being current
# for CRYPTO the same day,
# but every already-retained V2 CRYPTO aggregate stayed fully eligible to
# seed a fresh ``ResearchAgendaSourceFact``, a fresh ``ResearchProposal``,
# and a fresh feasibility row, because nothing downstream had ever had a
# reason to ask which follow-up policy version an aggregate was built from.
#
# This table is that second question's single declarative authority.  It is
# a *forward* restriction only, and deliberately the narrowest one that
# closes the gap:
#
# * every already-retained observation, assessment, aggregate, agenda item,
#   proposal, and feasibility row stays byte-for-byte what it is -- nothing
#   here deletes, closes, rewrites, re-hashes, or reinterprets anything;
# * V2 CRYPTO aggregates keep being computed every cycle and stay fully
#   queryable, re-runnable, and explicitly requestable, exactly as
#   ``current_followup_policy_venues``'s own docstring promises for V1;
# * only the eligibility of a *defective* (venue, policy version) pair to
#   originate **new** research work is withdrawn.
#
# Keyed on ``(venue, policy_version)`` rather than on the policy version
# alone because that is genuinely what the defects are: ``FOLLOWUP_POLICY_
# V2``'s temporal-integrity defect lives entirely in
# ``SyncFollowUpObservations._build_crypto``/``_crypto_baseline_reference``
# and has no bearing whatsoever on the KALSHI or SECURITIES rows that share
# the version label, both of which remain current *and* sound.
FOLLOWUP_POLICY_FORWARD_RETURN_TEMPORAL_INTEGRITY_DEFECT = (
    "FORWARD_RETURN_TEMPORAL_INTEGRITY_DEFECT"
)
FOLLOWUP_POLICY_DECISION_TIME_BASELINE_HINDSIGHT_DEFECT = "DECISION_TIME_BASELINE_HINDSIGHT_DEFECT"


@dataclass(frozen=True, slots=True)
class FollowUpPolicyVersionDefect:
    """One reviewed, named correctness defect in one ``(venue, policy_
    version)`` pair, together with the version that corrects it.

    ``corrected_by_policy_version`` is a mandatory field, not documentation:
    a defect with no corrective successor would quarantine a venue's
    evidence with nothing to replace it, which is a decision to stop
    researching that venue rather than a decision about evidence quality.
    Requiring the successor makes that impossible to declare by accident."""

    venue: SharedVenue
    policy_version: str
    defect_code: str
    corrected_by_policy_version: str
    summary: str


DEFECTIVE_FOLLOWUP_POLICY_VERSIONS: tuple[FollowUpPolicyVersionDefect, ...] = (
    # Counterfactual Forward Outcome Temporal Integrity v1 (migration 0113)
    # names both defects precisely; see its own docs page, section 1.  In
    # short: V2's CRYPTO baseline floors to the start of the anchor's own
    # UTC day, so up to a full calendar day of movement occurring *before*
    # Apex's own policy anchor is carried inside a number presented as a
    # *subsequent* return; and V2's forward endpoint applies no availability
    # predicate at all, so the same observation re-measured later can yield
    # a different return with no retained fact saying as of when either
    # measurement was taken.
    FollowUpPolicyVersionDefect(
        venue=SharedVenue.CRYPTO,
        policy_version=FOLLOWUP_POLICY_V2,
        defect_code=FOLLOWUP_POLICY_FORWARD_RETURN_TEMPORAL_INTEGRITY_DEFECT,
        corrected_by_policy_version=FOLLOWUP_POLICY_V3,
        summary=(
            "CRYPTO FOLLOWUP_POLICY_V2 subsequent_return admits pre-anchor market "
            "movement and has no replayable measurement instant; measured against real "
            "retained evidence it carries the wrong direction in a majority of "
            "comparable FOLLOWUP_POLICY_V3 pairs."
        ),
    ),
    # The same, already-documented, already-corrected reasoning applied one
    # version earlier rather than left as a silent exception: ``domain.
    # counterfactual_outcome``'s own module docstring has always recorded
    # that "FOLLOWUP_POLICY_V1's DECISION_TIME baseline used a hindsight-
    # leaking exact-bucket close; ..._V2's uses an availability-gated
    # latest_close_before read".  That is a correctness defect of exactly
    # the class this table exists for, and leaving it out would have made
    # the rule a one-off carve-out for V2 instead of a general policy.
    # Changes nothing observable today -- no CRYPTO V1 cohort has ever
    # reached QUALIFIED -- which is precisely why it is safe to state now
    # rather than after one does.
    FollowUpPolicyVersionDefect(
        venue=SharedVenue.CRYPTO,
        policy_version=FOLLOWUP_POLICY_V1,
        defect_code=FOLLOWUP_POLICY_DECISION_TIME_BASELINE_HINDSIGHT_DEFECT,
        corrected_by_policy_version=FOLLOWUP_POLICY_V2,
        summary=(
            "CRYPTO FOLLOWUP_POLICY_V1's DECISION_TIME baseline read an exact-bucket "
            "close with no availability gate, leaking evidence that was not yet "
            "retained at the decision instant."
        ),
    ),
)

_DEFECT_BY_VENUE_AND_POLICY_VERSION: Mapping[tuple[SharedVenue, str], FollowUpPolicyVersionDefect]
_DEFECT_BY_VENUE_AND_POLICY_VERSION = MappingProxyType(
    {(item.venue, item.policy_version): item for item in DEFECTIVE_FOLLOWUP_POLICY_VERSIONS}
)


def followup_policy_version_defect(
    *, venue: SharedVenue, policy_version: str
) -> FollowUpPolicyVersionDefect | None:
    """The reviewed defect recorded against this ``(venue, policy version)``
    pair, or ``None`` when none is.  Pure; performs no I/O.

    Returning the record rather than a bare boolean is deliberate: every
    consumer that withholds something on this basis is expected to be able
    to say *which* defect and *what corrects it*, so a quarantined cohort is
    explainable to a human rather than merely absent."""

    return _DEFECT_BY_VENUE_AND_POLICY_VERSION.get((venue, policy_version))


def followup_policy_version_may_source_new_research(
    *, venue: SharedVenue, policy_version: str
) -> bool:
    """Whether retained evidence of this ``(venue, policy version)`` pair may
    originate a **new** research conclusion -- a new ``ResearchAgendaSource
    Fact``, and therefore a new agenda item, proposal, feasibility row, or
    experiment execution derived from one.

    Never a statement about validity, queryability, reproducibility, or
    historical standing: a quarantined pair's already-retained rows and
    already-retained descendants remain exactly as retained, forever.  An
    unknown or unrecognized ``policy_version`` is permitted rather than
    fail-closed, because this gate answers "is there a *reviewed defect
    recorded against* this pair" -- fabricating a defect for a version
    nobody has assessed would be the opposite of the honesty it exists to
    protect, and every version reaching this function has already passed
    ``KNOWN_FOLLOWUP_POLICY_VERSIONS`` validation at the point the evidence
    was written."""

    return followup_policy_version_defect(venue=venue, policy_version=policy_version) is None


COUNTERFACTUAL_FOLLOWUP_WARNING = (
    "DECISION FOLLOW-UP OBSERVATION — OBJECTIVE EVIDENCE ONLY — NOT AN ATTRIBUTION OR "
    "HINDSIGHT-QUALITY JUDGMENT — NO ALLOCATION OR EXECUTION AUTHORITY"
)

# Part 4: exactly two horizon kinds -- "the smallest useful policy", not a
# family of arbitrary session counts.  SHORT_HORIZON is a fixed, uniform,
# venue-independent quick check ("is this still here / did anything move").
# STRATEGY_HORIZON reuses each subject's *own already-declared* holding
# period (``SharedOpportunityProjection.expected_holding_days``) rather than
# inventing a venue-wide number -- for CRYPTO that field already carries the
# research-hypothetical holding period (``domain.crypto_universe.
# HYPOTHETICAL_HOLDING_DAYS`` and friends); for KALSHI it is already the
# settlement-implied lockup (``(settlement_at - evaluated_at).days``,
# computed in ``adapters.shared_opportunity_sources``).  The *mechanism*
# (evaluated_at + N declared days) is identical and versioned once; the
# *meaning* of N is deliberately origin-specific -- Part 4 option (B)/(D).
SHORT_HORIZON_DAYS = 1

# Part 5/6: a KALSHI observation not yet settled is always a *live* ("as of
# right now") read -- Apex retains no historical Kalshi quote archive to
# answer "what was the price exactly at T+H" after the fact.  If the sync
# happens to run within one day of the declared target, that live read is
# treated as a faithful stand-in for "the state at the target"; beyond that,
# the record still retains the genuine read (never suppressed) but is
# explicitly flagged so a later reader cannot mistake "we happened to check
# late" for "this is what the market showed at the target time".  A
# genuinely settled result is exempt: settlement is a permanent fact once
# true, so observing it late does not make it any less a faithful answer to
# "did this market settle, and how".
KALSHI_LATE_OBSERVATION_TOLERANCE = timedelta(hours=24)
KALSHI_LATE_OBSERVATION_LIMITATION = "LIVE_READ_AFTER_TARGET_TIME_NOT_A_POINT_IN_TIME_QUOTE"
CRYPTO_DAILY_CLOSE_LIMITATION = "DAILY_CLOSE_REFERENCE_PRICE_ONLY"

# Counterfactual Forward Outcome Temporal Integrity v1 -- two additional,
# V3-only ``data_limitations`` entries.  Both are genuine disclosures about
# what a V3 row's own numbers do and do not mean, never hedges: the first
# says the measurement was taken under a declared availability bound (so a
# reader knows an identical later measurement could legitimately differ, and
# that this row is reproducible only against its own ``measurement_as_of``);
# the second says, in the row itself rather than only in documentation, that
# the retained decision-input reference price is disclosure and is never an
# endpoint of ``subsequent_return``.
CRYPTO_MEASUREMENT_AS_OF_BOUNDED_LIMITATION = "MEASUREMENT_BOUNDED_BY_DECLARED_MEASUREMENT_AS_OF"
CRYPTO_DECISION_INPUT_REFERENCE_NOT_ENDPOINT_LIMITATION = (
    "DECISION_INPUT_REFERENCE_PRICE_IS_NOT_A_RETURN_ENDPOINT"
)

# Counterfactual Forward Outcome Temporal Integrity v1 reason codes.  Each
# names exactly one condition and is deliberately distinct from every other
# (Part: "a diagnostic that filters on ``reasons`` alone must never be able
# to mistake one classification for another" -- the same rule that already
# keeps ``CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE`` separate from
# ``CRYPTO_BASELINE_BUCKET_NOT_RETAINED``).
#
# ``CRYPTO_DECISION_INPUT_REFERENCE_PERMANENTLY_UNAVAILABLE`` is V3's own
# analogue of ``CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE`` and carries the
# identical permanence proof (a frozen, anchor-derived boundary), but names a
# structurally different missing fact -- V2's *measurement baseline* versus
# V3's *decision-input disclosure* -- so the two must never share a string.
CRYPTO_DECISION_INPUT_REFERENCE_PERMANENTLY_UNAVAILABLE = (
    "CRYPTO_DECISION_INPUT_REFERENCE_PERMANENTLY_UNAVAILABLE"
)
# Deliberately *transient*, never re-classified permanent: V3's endpoint gate
# is ``batch.completed_at <= measurement_as_of``, and ``measurement_as_of``
# advances every cycle, so a historical batch that has not arrived yet can
# legitimately satisfy it later (unlike V2's baseline gate, whose boundary is
# a frozen past instant).  Long-run fairness for these pairs is the scan
# cursor's job (Counterfactual Follow-Up Long-Run Fairness v1), exactly as
# that mechanism was designed to be.
CRYPTO_V3_WINDOW_START_MEASUREMENT_UNAVAILABLE = "CRYPTO_V3_WINDOW_START_MEASUREMENT_UNAVAILABLE"
CRYPTO_V3_WINDOW_END_MEASUREMENT_UNAVAILABLE = "CRYPTO_V3_WINDOW_END_MEASUREMENT_UNAVAILABLE"
# A defensive, never-expected-in-practice reason: the application layer
# derives W0/W1 twice along two genuinely independent paths (once through
# ``_horizon_anchor_at`` -> ``determine_followup_due_state`` to decide
# due-ness, and once through ``forward_measurement_window`` to resolve
# buckets).  Those two must agree.  Fails closed as an ordinary transient
# ``BLOCKED_UNAVAILABLE`` for that one candidate rather than raising, for
# the same reason the venue dispatch's own defensive fallback does: an
# uncaught exception here would abort an entire recurring-cycle task, a
# strictly worse failure mode than one candidate not progressing.
CRYPTO_V3_WINDOW_DERIVATION_DISAGREEMENT = "CRYPTO_V3_WINDOW_DERIVATION_DISAGREEMENT"

# --------------------------------------------------------------------------
# Counterfactual Follow-Up Starvation Fix v1 -- confirmed, read-only-audited
# defect: ``SyncFollowUpObservations.execute`` walked its oldest-due-first
# queue and spent one of its bounded ``sample_limit`` genuine attempts on
# every ``DUE`` pair alike, including a CRYPTO baseline that can *never*
# resolve.  Because that in-memory ``BLOCKED_UNAVAILABLE`` result was never
# durably distinguished from a merely-not-yet-captured one, the identical
# permanently-doomed pairs sorted first again on every later cycle and
# re-consumed the same budget forever -- the specific cohort genuinely
# confirmed live, at the time of this fix, was decisions genuinely evaluated
# on Apex's own first day of retained CRYPTO history (2026-07-30) whose
# ``FOLLOWUP_POLICY_V2``+ baseline boundary equals or predates that floor.
# See ``FollowUpDueState.PERMANENTLY_UNAVAILABLE`` below for the exact proof
# and ``application.counterfactual_followup.SyncFollowUpObservations`` for
# where it is applied *before* any attempt-budget is spent.
#
# That specific 2026-07-30 cohort is closed and cannot grow -- but the proof
# itself (below) is a per-*product* argument ("once no retained batch for
# *this* product satisfies the gate as of *this* boundary, none ever will"),
# never a claim that CRYPTO's *entire* permanently-blocked population is
# capped at whatever it was on any one date.  Apex's own tracked crypto
# universe is not a fixed set decided once at deployment (``domain.
# crypto_universe.materialize_universe`` re-ranks eligibility, by volume,
# every ``CRYPTO_UNIVERSE_REFRESH`` cycle -- a product ineligible today can
# become newly eligible, and therefore newly *captured for the first time*,
# months into Apex's operating life).  A decision evaluated at or before
# *that* product's own first retained batch is, by the identical proof, a
# *new* permanently-unavailable instance -- just anchored to a later,
# product-specific floor instead of 2026-07-30.  The permanently-blocked
# population is therefore open-ended over Apex's lifetime, not a fixed
# constant -- which is exactly why the bounded scan's own starvation-freedom
# (Counterfactual Follow-Up Long-Run Fairness v1; see ``application.
# counterfactual_followup``'s module docstring and ``FollowUpScanPosition``)
# is deliberately built to hold for a cohort of *any* size, growing or not,
# rather than resting on an assumption that it stays small.
#
# ``CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE`` is deliberately a distinct
# string from ``"CRYPTO_BASELINE_BUCKET_NOT_RETAINED"`` (the reason
# ``application.counterfactual_followup.SyncFollowUpObservations._build_
# crypto`` has always raised, and still raises verbatim for
# ``FOLLOWUP_POLICY_V1``'s own frozen, structurally-*transient* same-day
# baseline lookup -- see below) rather than the same string paired with a
# different outcome flag: a diagnostic that filters on ``reasons`` alone
# (without also checking ``outcome``/``state``) must never be able to
# mistake a permanent classification for a transient one, or vice versa.
CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE = "CRYPTO_BASELINE_PERMANENTLY_UNAVAILABLE"
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
# Remediation v1 -- confirmed, read-only-audited defect: neither
# ``CryptoPriceSource.daily_close`` nor ``.latest_close_before`` ever
# excluded synthetic/non-genuine ``domain.research_evidence.EvidenceClass``
# batches from the candidates they rank -- both simply took the
# most-recently-*completed* retained batch covering the bucket a caller
# asked for, regardless of whether that batch was ever a genuine market
# observation.  Confirmed live: the first genuine CRYPTO
# ``FOLLOWUP_POLICY_V2`` observation ever retained
# (``decision_followup_observation_260a08ba994dca94910e1bf2``, DOGE-USD,
# ``ORIGIN_EVIDENCE``) stamped ``reference_price_at_baseline = 189.504`` --
# not a real DOGE price at any point in its history -- because a
# ``crypto-universe-quality-repair-fixture-products-v1``
# ``EvidenceClass.FIXTURE`` batch (``FixtureCoinbaseUniverseSource``'s
# deterministic, character-hash-derived synthetic price generator -- see
# ``adapters.coinbase_universe_public``) had genuinely completed 4m38s
# *after* a retained ``EvidenceClass.HISTORICAL_PUBLIC`` batch covering the
# identical bucket, whose own close (``0.07196``) was the real one.
# Neither lookup method had ever considered evidence class at all -- only
# ``completed_at``.
#
# The fix is a fail-closed **allowlist**, not a FIXTURE-specific denylist:
# a denylist ("anything except FIXTURE") would silently admit any future
# non-genuine ``EvidenceClass`` member (a sandbox/test/simulation/
# synthetic-repair class not yet invented) the instant it existed, with no
# review step -- exactly the shape of mistake this incident already was,
# merely with a different member name.  An allowlist instead requires every
# *newly added* ``EvidenceClass`` member to be deliberately reviewed and
# added here before it can ever participate in genuine follow-up economics,
# by construction (proven against ``EvidenceClass.SANDBOX``/``.PAPER`` in
# this module's own test suite -- both real, already-declared members that
# were never produced by any adapter and were never reviewed for this
# boundary either, standing in for a genuinely future/unknown class).
#
# Classification of every ``EvidenceClass`` member that exists today (Part:
# full vocabulary review, not merely "exclude the one class that bit us"):
#
#   * ``GENUINE_PUBLIC`` -- a genuinely, live, publicly read fact at
#     capture time.  Not yet produced by any ``crypto_product_observation``
#     batch specifically (Coinbase daily-candle capture only ever produces
#     ``HISTORICAL_PUBLIC``, below) but a real, established, actively-used
#     "genuine data" vocabulary member elsewhere in this repository today
#     (``application.research_evidence``, ``adapters.shared_opportunity_
#     sources`` -- a live public read of a still-open/current fact).
#     ELIGIBLE.
#   * ``HISTORICAL_PUBLIC`` -- a genuinely, publicly captured fact,
#     retrospectively/backfilled rather than live-read.  The *only* class
#     Apex's real Coinbase daily-candle capture has ever produced for
#     genuine data (``adapters.coinbase_universe_public.
#     CoinbasePublicUniverseSource.evidence_class``) -- crypto daily bars
#     are backfills by construction (the provider's own daily-candle
#     endpoint always returns already-closed trading days), so excluding
#     this class would make genuine CRYPTO follow-up economics
#     structurally impossible, not merely stricter.  ELIGIBLE.
#   * ``LIVE`` -- INELIGIBLE, deliberately excluded rather than speculatively
#     allowed.  A pre-deployment final-review audit of this exact milestone
#     re-examined the original draft's own reasoning (which had allowed it,
#     reading it as "the single most genuine class conceivable") and found
#     it insufficiently grounded: ``LIVE`` has zero producers anywhere in
#     this entire repository, for *any* evidence domain, not merely for
#     CRYPTO follow-up -- so nothing has ever actually pinned down what it
#     means.  The only textual evidence this repository does carry points
#     the other way: ``domain.research_evidence`` -- the module that
#     declares ``EvidenceClass`` itself -- describes what it deliberately
#     stays out of as "a Strategy, Opportunity, Order, Position,
#     reservation, journal, broker request, PAPER authority, or LIVE
#     authority" (its own module docstring), naming ``LIVE`` in the same
#     breath as ``PAPER`` and as an "authority", i.e. an execution/runtime
#     concept -- exactly the "merely an execution/runtime classification"
#     shape this milestone's own allowlist is designed to keep separate from
#     evidence *genuineness*, not the confirmed evidence-quality class the
#     original draft asserted.  Fail-closed until a real producer exists
#     *and* a reviewed audit confirms ``LIVE`` actually denotes evidence
#     genuineness rather than execution authority -- re-adding it then is a
#     deliberate, one-line, reviewed change, exactly like admitting any other
#     newly-reviewed class (Part: the allowlist's own founding rationale,
#     immediately above).  A regression test pins this exclusion the same
#     way ``FIXTURE``/``SANDBOX``/``PAPER`` are already pinned below.
#   * ``FIXTURE`` -- explicitly synthetic, deterministic, offline test data
#     (``FixtureCoinbaseUniverseSource``, and every other ``FIXTURE``
#     producer in this repository).  The confirmed contamination vector
#     above.  INELIGIBLE.
#   * ``SANDBOX`` -- a provider's own non-production practice/test
#     environment.  Declared in ``domain.research_evidence.EvidenceClass``
#     but never yet produced by any adapter in this repository -- exactly
#     the "not yet reviewed for this boundary" case the allowlist shape
#     exists to fail closed on.  INELIGIBLE.
#   * ``PAPER`` -- Apex's *own* internal simulated PAPER-trading evidence.
#     Circular (an artifact of Apex's own prior simulated decisions, not an
#     independent market fact) even where genuinely retained elsewhere in
#     this repository.  Also never yet produced as a ``crypto_product_
#     observation`` batch.  INELIGIBLE.
#
# Applies to every CRYPTO follow-up lookup path uniformly
# (``CryptoPriceSource.daily_close``/``.latest_close_before``, and the
# defense-in-depth check inside ``build_crypto_price_movement_observation``
# below) -- this is a correctness property of the *lookup primitives*
# themselves, exactly like "never return a bucket at or after ``before``"
# or "a candle's own retained batch must have genuinely completed
# at-or-before ``before``" (``FOLLOWUP_POLICY_V2``'s own availability-as-of
# fix, above) -- never a ``policy_version``-specific concern, and therefore
# never a reason to mint a new ``FOLLOWUP_POLICY_Vn``: every known policy
# version shares the identical two lookup methods, and evidence eligibility
# is a precondition on what either method may ever return, not a change to
# what either method *means*.  This milestone's own read-only census of
# every retained CRYPTO follow-up row confirms all 25 already-retained
# ``FOLLOWUP_POLICY_V1`` rows are clean (no ``FIXTURE`` involvement) --
# this fix changes nothing about how any of them would be reproduced.
#
# That "never a reason to mint a new ``FOLLOWUP_POLICY_Vn``" argument is
# genuinely correct about what ``FOLLOWUP_POLICY_Vn`` itself *means* -- but
# it is not, by itself, sufficient: unlike every prior in-place lookup-layer
# correction this module's own history cites as precedent (the availability-
# as-of fix and the target-bucket-from-baseline-candle fix, both landed
# while ``FOLLOWUP_POLICY_V2`` had *zero* genuinely retained rows), this
# fix lands with one real, already-retained ``FOLLOWUP_POLICY_V2`` CRYPTO
# row that predates it (the confirmed DOGE-USD incident) and every
# ``FOLLOWUP_POLICY_V2`` CRYPTO row from now on postdates it -- both
# permanently, indistinguishably stamped ``policy_version=FOLLOWUP_
# POLICY_V2``.  A final-review pre-deployment audit of this exact milestone
# found that gap and closed it with the smallest correct addition: a
# separate, durable, per-observation ``evidence_eligibility_policy_version``
# fact (below) -- never a new ``FOLLOWUP_POLICY_Vn`` (evidence eligibility
# is still not a change to what the follow-up policy itself means) and never
# only the additive, CLI-triggered ``FollowUpEvidenceQualityAssessment``
# alone (Part: remediation -- that table answers "is this evidence *proven
# clean*", a strictly different question from "did eligibility-aware code
# *generate* this row", and is not durably populated at generation time for
# every row the way this field now is).
# --------------------------------------------------------------------------
FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES: frozenset[EvidenceClass] = frozenset(
    {EvidenceClass.GENUINE_PUBLIC, EvidenceClass.HISTORICAL_PUBLIC}
)

CRYPTO_FOLLOWUP_BASELINE_EVIDENCE_INELIGIBLE = "CRYPTO_FOLLOWUP_BASELINE_EVIDENCE_INELIGIBLE"
CRYPTO_FOLLOWUP_TARGET_EVIDENCE_INELIGIBLE = "CRYPTO_FOLLOWUP_TARGET_EVIDENCE_INELIGIBLE"
# Counterfactual Forward Outcome Temporal Integrity v1 builder-rejection
# reasons.  Each names exactly one violated structural invariant, so a
# failure points at the specific contract that was broken rather than at a
# shared catch-all (the same discipline ``SyncFollowUpObservations``'
# SECURITIES branch already applies to its own unavailability reasons).
CRYPTO_FOLLOWUP_DECISION_INPUT_EVIDENCE_INELIGIBLE = (
    "CRYPTO_FOLLOWUP_DECISION_INPUT_EVIDENCE_INELIGIBLE"
)
CRYPTO_FOLLOWUP_V3_VENUE_UNSUPPORTED = "CRYPTO_FOLLOWUP_V3_VENUE_UNSUPPORTED"
CRYPTO_FOLLOWUP_V3_REQUIRED_FACT_MISSING = "CRYPTO_FOLLOWUP_V3_REQUIRED_FACT_MISSING"
CRYPTO_FOLLOWUP_V3_FACT_ON_NON_V3_POLICY = "CRYPTO_FOLLOWUP_V3_FACT_ON_NON_V3_POLICY"
CRYPTO_FOLLOWUP_V3_WINDOW_PRECEDES_ANCHOR = "CRYPTO_FOLLOWUP_V3_WINDOW_PRECEDES_ANCHOR"
CRYPTO_FOLLOWUP_V3_WINDOW_START_NOT_DERIVED = "CRYPTO_FOLLOWUP_V3_WINDOW_START_NOT_DERIVED"
CRYPTO_FOLLOWUP_V3_WINDOW_SPAN_INVALID = "CRYPTO_FOLLOWUP_V3_WINDOW_SPAN_INVALID"
CRYPTO_FOLLOWUP_V3_MEASUREMENT_AS_OF_PRECEDES_WINDOW = (
    "CRYPTO_FOLLOWUP_V3_MEASUREMENT_AS_OF_PRECEDES_WINDOW"
)
FOLLOWUP_V3_POLICY_VERSION_UNSUPPORTED_FOR_VENUE = (
    "FOLLOWUP_V3_POLICY_VERSION_UNSUPPORTED_FOR_VENUE"
)


def is_followup_evidence_eligible(evidence_class: EvidenceClass) -> bool:
    """Fail-closed allowlist membership test -- never ``evidence_class is
    not EvidenceClass.FIXTURE`` or an equivalent denylist shape (Part: see
    ``FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES``'s own module-level rationale
    above).  Pure, total; the sole authority this repository has for "may
    this evidence participate in genuine counterfactual follow-up
    economics" -- every CRYPTO follow-up lookup path and builder consults
    this function and never restates the allowlist independently."""

    return evidence_class in FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES


# --------------------------------------------------------------------------
# Generation-semantics versioning (final-review addition, same milestone).
#
# Required invariant: two ``DecisionFollowUpObservation`` rows generated
# under materially different evidence-eligibility rules must not be
# indistinguishable under the same durable semantic versioning.  Before this
# addition they were: ``policy_version`` alone is shared by the one already-
# retained pre-fix ``FOLLOWUP_POLICY_V2`` CRYPTO row (the confirmed DOGE-USD
# incident, built when the lookup layer applied no evidence-eligibility
# filter at all) and every post-fix ``FOLLOWUP_POLICY_V2`` CRYPTO row this
# milestone's code will ever build from now on -- with no durable field
# anywhere recording which of the two lookup-layer semantics actually
# governed a given row's generation.
#
# ``evidence_eligibility_policy_version`` closes that gap: a plain, durable,
# per-observation fact -- "which version of ``FOLLOWUP_ELIGIBLE_EVIDENCE_
# CLASSES`` did the lookup layer that produced this row's baseline/target
# actually enforce" -- stamped once, at generation time, by the builder,
# never re-derived or guessed afterward.  Deliberately its own, third,
# independent versioning axis, never conflated with either sibling:
#
#   * ``FOLLOWUP_POLICY_Vn`` answers "what does this observation's overall
#     follow-up methodology mean" (baseline kind, horizon, target-bucket
#     derivation, ...).
#   * ``FOLLOWUP_EVIDENCE_QUALITY_POLICY_Vn`` (Part: remediation, below)
#     answers "under a *retroactive* review policy, was this observation's
#     evidence lineage independently *proven* clean" -- additive, optional,
#     CLI-triggered, and re-derivable from retained state even for a row
#     that predates it entirely.
#   * ``evidence_eligibility_policy_version`` answers neither -- only "did
#     eligibility-aware lookup code produce this specific row", a fact nothing
#     else on the observation captures and that cannot be reconstructed after
#     the fact for a row that predates it (unlike the assessment above, which
#     can independently re-derive its own verdict for *any* row, old or new).
#
# ``None`` on every one of the 92 real rows retained before this milestone
# (CRYPTO and KALSHI, ``FOLLOWUP_POLICY_V1`` and ``V2`` alike) -- reconstructed
# that way for the same "never guessed after the fact" reason ``baseline_
# evidence_class`` already is (see ``DecisionFollowUpObservation``'s own
# docstring).  Populated (``CURRENT_EVIDENCE_ELIGIBILITY_POLICY_VERSION``) on
# every CRYPTO row this module's current code builds from now on; left
# ``None`` for KALSHI (deliberately -- KALSHI evidence was never subject to
# the eligibility bug this axis exists to make visible, exactly like
# ``baseline_evidence_class`` staying ``None`` for KALSHI is deliberate, not
# "unknown").  An operator can therefore find every row this concept does not
# yet cover with a single, honest, zero-tribal-knowledge filter:
# ``WHERE venue = 'CRYPTO' AND evidence_eligibility_policy_version IS NULL``
# -- never by cross-referencing a deployment date.
# --------------------------------------------------------------------------
CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1 = "CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1"
KNOWN_EVIDENCE_ELIGIBILITY_POLICY_VERSIONS: tuple[str, ...] = (
    CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1,
)
CURRENT_EVIDENCE_ELIGIBILITY_POLICY_VERSION = CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1


# --------------------------------------------------------------------------

# ``domain.values.parse_decimal`` caps every authoritative Decimal at 18
# significant digits / 8 fractional digits; raw ``Decimal`` division (e.g. a
# price ratio close to a repeating fraction) can exceed that.  Quantized the
# same way ``domain.crypto_universe.metric``/``domain.shared_opportunity.
# _bounded`` already round every other derived research ratio in Apex --
# not a bespoke precision choice for this module.
RETURN_QUANTUM = Decimal("0.00000001")


class CounterfactualFollowUpError(ValueError):
    """Raised when retained follow-up evidence is invalid or conflicts."""


# --------------------------------------------------------------------------
# Counterfactual Forward Outcome Temporal Integrity v1 -- the explicit
# forward economic-window contract ``FOLLOWUP_POLICY_V3`` measures over.
#
# These exist as *named* helpers, rather than as an inline call to
# ``domain.crypto_market_pulse.completed_utc_day``, precisely because reusing
# that ambiguously-named floor helper where a ceiling was meant is the direct
# root cause of the pre-anchor-movement defect V3 corrects (see
# ``FOLLOWUP_POLICY_V3``'s own block above, Defect A).  Each name states its
# semantic contract, so a future reader cannot satisfy the type checker while
# violating the meaning.
#
# One day is the only boundary cadence involved anywhere here: Apex retains
# CRYPTO price history exclusively as UTC daily candles (``domain.crypto_
# universe.CryptoDailyObservation``), so "daily-close boundary" is always a
# UTC midnight and "the candle whose close represents boundary B" is always
# the one whose ``bucket_start`` is ``B - 1 day``.
# --------------------------------------------------------------------------
DAILY_CLOSE_BOUNDARY_PERIOD = timedelta(days=1)


def first_daily_close_boundary_at_or_after(anchor: datetime) -> datetime:
    """The earliest UTC daily-close boundary at or after ``anchor``.

    A **ceiling**, never a floor.  Equal to ``anchor`` exactly when ``anchor``
    is itself a UTC-midnight boundary; otherwise the next midnight strictly
    after it::

        2026-08-02T03:03Z -> 2026-08-03T00:00Z
        2026-08-02T21:40Z -> 2026-08-03T00:00Z
        2026-08-02T00:00Z -> 2026-08-02T00:00Z   (already a boundary)

    This is the single property that makes a ``FOLLOWUP_POLICY_V3``
    ``subsequent_return`` genuinely *subsequent*: the returned instant is,
    by construction, never earlier than ``anchor``, so no market movement
    that happened before the policy anchor can enter the window that starts
    here.  Contrast ``domain.crypto_market_pulse.completed_utc_day``, which
    floors and therefore lands *before* any intraday anchor.

    Pure and total; ``anchor`` must already be timezone-aware (every caller
    passes a value that has already passed through an authoritative type)."""

    floored = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    if floored == anchor:
        return floored
    return floored + DAILY_CLOSE_BOUNDARY_PERIOD


def daily_close_bucket_for_boundary(boundary: datetime) -> datetime:
    """The ``bucket_start`` of the retained daily candle whose ``close``
    economically represents ``boundary``.

    A daily candle's ``close`` is the closing price of the trading period
    *starting* at its ``bucket_start`` -- i.e. it represents the instant
    ``bucket_start + 1 day`` (standard OHLC convention; traced through
    ``adapters.coinbase_universe_public``'s raw candle parsing, which stamps
    ``bucket_start`` from the provider's own period-*start* timestamp
    unchanged).  So the candle representing ``boundary`` is the one starting
    one day earlier.  Pure; performs no lookup and asserts nothing about
    whether such a candle is actually retained -- that is the availability
    question ``CryptoPriceSource.daily_close_as_of`` answers separately."""

    return boundary - DAILY_CLOSE_BOUNDARY_PERIOD


@dataclass(frozen=True, slots=True)
class ForwardMeasurementWindow:
    """``FOLLOWUP_POLICY_V3``'s nominal post-anchor economic window and the
    two exact retained buckets that measure it.  A private, non-persisted
    derivation carrying no retained data of its own -- every field is a pure
    function of ``(policy_anchor_at, horizon_days)`` alone, never of
    ``measurement_as_of`` and never of whatever happens to be retained."""

    policy_anchor_at: datetime
    horizon_days: int
    # W0 -- first daily-close boundary at or after ``policy_anchor_at``.
    window_start: datetime
    # W1 -- ``window_start + horizon_days``.
    window_end: datetime
    # The candle whose close represents W0.
    window_start_bucket: datetime
    # The candle whose close represents W1.
    window_end_bucket: datetime


def forward_measurement_window(
    *, policy_anchor_at: datetime, horizon_days: int
) -> ForwardMeasurementWindow:
    """``FOLLOWUP_POLICY_V3``'s economic-window contract, in one place::

        W0 = first daily-close boundary at or after policy_anchor_at
        W1 = W0 + horizon_days

    and the two *exact* buckets whose closes represent them.  Derived solely
    from the anchor and the horizon -- deliberately never from a resolved
    baseline candle (``FOLLOWUP_POLICY_V2``'s own target-bucket-from-
    baseline-candle mechanism), because that made the measured window depend
    on which evidence happened to be retained, and therefore unreproducible
    once retention changed.  Under V3 the window is a fixed, declared
    economic fact about the decision; *availability* of evidence to measure
    it is a strictly separate question answered by ``measurement_as_of``.

    Guarantees, both pinned by this module's own property tests:
    ``window_start >= policy_anchor_at`` (no pre-anchor movement), and
    ``window_end - window_start == horizon_days`` exactly."""

    if horizon_days < 0:
        raise CounterfactualFollowUpError("CRYPTO_FORWARD_WINDOW_HORIZON_DAYS_INVALID")
    window_start = first_daily_close_boundary_at_or_after(policy_anchor_at)
    window_end = window_start + timedelta(days=horizon_days)
    return ForwardMeasurementWindow(
        policy_anchor_at=policy_anchor_at,
        horizon_days=horizon_days,
        window_start=window_start,
        window_end=window_end,
        window_start_bucket=daily_close_bucket_for_boundary(window_start),
        window_end_bucket=daily_close_bucket_for_boundary(window_end),
    )


class FollowUpHorizonKind(StrEnum):
    SHORT_HORIZON = "SHORT_HORIZON"
    STRATEGY_HORIZON = "STRATEGY_HORIZON"


class FollowUpBaselineKind(StrEnum):
    """Which instant this observation's *baseline* price and *target*
    horizon are both anchored to -- the axis FOLLOWUP_POLICY_V2's own
    pre-deployment audit found silently conflated inside one
    ``subsequent_return`` (Part: pre-deployment staleness-semantics audit).
    Exactly two kinds, mirroring ``FollowUpHorizonKind``'s own "smallest
    useful policy" discipline -- never a family of arbitrary reference
    points:

    * ``ORIGIN_EVIDENCE`` -- baseline price *and* ``target_time`` are both
      anchored to the *subject's* own ``SharedOpportunityProjection.
      generated_at``.  Answers "how did the strategy's own hypothesis play
      out, measured over its own declared horizon from when its evidence was
      actually captured" -- never mixed with however long Apex later took to
      evaluate a decision about it.
    * ``DECISION_TIME`` -- baseline price *and* ``target_time`` are both
      anchored to ``CounterfactualDecision.evaluated_at`` itself (the
      instant Apex actually advanced/constrained/rejected).  Answers "what
      happened after Apex's own decision" using the latest price genuinely
      available at-or-before that instant -- a true post-decision subsequent
      return, never a same-day-not-yet-known bar and never the origin
      evidence's own (potentially much earlier) reference point.

    CRYPTO gets both kinds under ``FOLLOWUP_POLICY_V2`` (2 horizon kinds x 2
    baseline kinds = 4 retained rows per decision): the projection->decision
    gap is real and, for a decision evaluated against a since-gone-stale
    projection, can span multiple days, so neither question can honestly
    stand in for the other (see the module's own audit report for real
    retained examples where the two even disagree in *sign*).  KALSHI only
    ever produces ``DECISION_TIME``: a live public read has no separate
    "origin evidence" reading to distinguish in v1 -- Apex retains no
    historical Kalshi quote archive to construct one from, and this module
    does not introduce new provider history merely to manufacture a
    distinction Kalshi's own evidence cannot honestly support (Part: "do not
    introduce new provider history merely to manufacture precision").
    ``FOLLOWUP_POLICY_V1`` never had this axis at all -- every V1 row (both
    venues) is, and remains, exactly what V1 always computed; see
    ``followup_observation_id``/``followup_observation_canonical_hash`` for
    how this axis is added without perturbing a single already-retained V1
    identity or hash."""

    ORIGIN_EVIDENCE = "ORIGIN_EVIDENCE"
    DECISION_TIME = "DECISION_TIME"


class FollowUpObservationType(StrEnum):
    """The one supported objective-fact shape per venue in v1 (Part 7/9) --
    not a family of types; liquidity/availability facts are folded into the
    same record as flags/limitations rather than multiplying types."""

    CRYPTO_REFERENCE_PRICE_MOVEMENT = "CRYPTO_REFERENCE_PRICE_MOVEMENT"
    KALSHI_MARKET_STATUS_AND_PRICE = "KALSHI_MARKET_STATUS_AND_PRICE"
    # Securities Research -> Shared Opportunity Recurring Bridge v1. See
    # ``build_securities_structure_terminal_observation``.
    SECURITIES_STRUCTURE_TERMINAL_PAYOFF = "SECURITIES_STRUCTURE_TERMINAL_PAYOFF"


class FollowUpDueState(StrEnum):
    """Part 11/12's minimal due/coverage lifecycle.  ``BLOCKED_UNAVAILABLE``
    is deliberately not returned by the pure ``determine_followup_due_state``
    below -- it is always the result of a genuine, *attempted* evidence
    gather (a missing retained bar, a failed provider read), which is an
    application-layer fact, not a temporal one.  ``UNSUPPORTED`` is a
    terminal, purely structural fact (e.g. no declared holding period) that
    -- unlike ``BLOCKED_UNAVAILABLE`` -- can be determined without attempting
    any read at all.

    ``PERMANENTLY_UNAVAILABLE`` (Counterfactual Follow-Up Starvation Fix v1)
    is a third, distinct application-layer fact -- added to close a genuine,
    read-only-audited starvation defect, never to widen this function's own
    contract.  A CRYPTO decision's ``FOLLOWUP_POLICY_V2``-or-later baseline
    resolves through ``CryptoPriceSource.latest_close_before``: "the latest
    retained daily close strictly *before* a fixed boundary, and only if a
    retained batch had genuinely captured it at-or-before that same boundary"
    (Part: availability-as-of correctness; see ``FOLLOWUP_POLICY_V2``'s own
    comment block above).  For any pair this module ever reports ``DUE``,
    that boundary is *always* already in the past relative to ``as_of``:
    ``target_time = horizon_anchor_at + horizon_days`` with ``horizon_days
    >= 0`` (rejected otherwise), a pair is never ``DUE`` while ``as_of <
    target_time``, and the baseline boundary is never later than
    ``horizon_anchor_at`` itself -- so ``boundary <= horizon_anchor_at <=
    target_time <= as_of`` always holds by the time anything downstream
    could even ask the question.  If, at that point, no already-retained
    batch satisfies ``bucket_start < boundary and completed_at <= boundary``,
    *no future batch ever can either*: Apex never backfills a batch's own
    ``completed_at`` to a wall-clock instant earlier than when it was
    genuinely captured (Part: "no hindsight / backfill / fabricated
    historical availability" -- an existing, unrelated-to-this-fix Apex
    invariant this proof leans on, not a new one), and every future capture
    happens at its own "now", which by then is itself already at or past
    ``as_of >= boundary``.  This is therefore not "still waiting for
    evidence that may genuinely arrive later" (``BLOCKED_UNAVAILABLE``'s own
    meaning, which this state leaves completely intact for every other
    case) but a closed, provably-permanent question the moment it is first
    asked -- structurally different from a missing *target* bucket
    (``CryptoPriceSource.daily_close``'s exact-match lookup, used for every
    target price and for ``FOLLOWUP_POLICY_V1``'s own frozen, never-edited
    same-day baseline lookup), which asks about one specific calendar day
    that a later, not-yet-run capture can still genuinely supply for the
    first time -- that case stays ``BLOCKED_UNAVAILABLE``, retryable,
    exactly as it always has been.  See ``CRYPTO_BASELINE_PERMANENTLY_
    UNAVAILABLE`` and ``application.counterfactual_followup.
    SyncFollowUpObservations``/``PlanFollowUpObservations`` for where this
    distinction is actually applied -- always *before* any bounded
    attempt-budget or provider-request budget is spent, never after.  Like
    ``BLOCKED_UNAVAILABLE``, never returned by the pure
    ``determine_followup_due_state`` below: proving it requires a genuine
    (if zero-provider-request, zero-attempt-budget) retained-evidence read
    this horizon-only function deliberately never performs."""

    NOT_YET_DUE = "NOT_YET_DUE"
    DUE = "DUE"
    OBSERVED = "OBSERVED"
    BLOCKED_UNAVAILABLE = "BLOCKED_UNAVAILABLE"
    PERMANENTLY_UNAVAILABLE = "PERMANENTLY_UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class FollowUpDueResult:
    state: FollowUpDueState
    horizon_kind: FollowUpHorizonKind
    horizon_days: int | None
    target_time: datetime | None
    reasons: tuple[str, ...]


def determine_followup_due_state(
    *,
    horizon_kind: FollowUpHorizonKind,
    horizon_anchor_at: datetime | None,
    expected_holding_days: int | None,
    as_of: datetime,
    already_observed: bool,
) -> FollowUpDueResult:
    """Pure, total, deterministic (Part 18).  Never performs I/O and never
    itself decides ``BLOCKED_UNAVAILABLE`` -- see ``FollowUpDueState``.

    ``horizon_anchor_at`` is the instant ``target_time`` is computed from
    (``target_time = horizon_anchor_at + horizon_days``) -- *not* always
    ``decision.evaluated_at``.  Renamed from this function's original
    ``decision_evaluated_at`` (Counterfactual Crypto Follow-Up Point-in-Time
    Semantics v2's own pre-deployment staleness-semantics audit): once a
    ``FollowUpBaselineKind.ORIGIN_EVIDENCE`` row anchors its horizon to the
    subject's own ``SharedOpportunityProjection.generated_at`` instead, this
    parameter is no longer always the decision's own evaluation instant, so
    it must not be named as if it always were.  The caller
    (``application.counterfactual_followup``) decides which instant to pass
    per ``(horizon_kind, baseline_kind)`` pair -- this function stays a
    single, venue- and baseline-kind-agnostic pure horizon calculation; it
    has no ``baseline_kind`` parameter of its own and does not need one.

    ``horizon_anchor_at`` may be ``None`` -- exactly the ``ORIGIN_EVIDENCE``
    case where no ``SharedOpportunityProjection`` is retained for a
    decision's subject, so there is no ``generated_at`` to anchor to at all
    (``DECISION_TIME``'s own anchor, ``decision.evaluated_at``, is always
    available and a caller should never pass ``None`` for it) -- reported as
    ``UNSUPPORTED``, mirroring exactly how a missing ``expected_holding_days``
    already reports ``STRATEGY_HORIZON`` as structurally unsupported, rather
    than raising: this is a purely structural fact determinable without
    attempting any read (Part: ``FollowUpDueState.UNSUPPORTED`` semantics).

    ``as_of`` and (when not ``None``) ``horizon_anchor_at`` must both be
    timezone-aware; the caller is responsible for that, matching every
    other pure domain function in this codebase that takes a ``datetime``
    without re-validating awareness itself when the value has already
    passed through an authoritative type at construction time.
    """

    if horizon_anchor_at is None:
        return FollowUpDueResult(
            state=FollowUpDueState.UNSUPPORTED,
            horizon_kind=horizon_kind,
            horizon_days=None,
            target_time=None,
            reasons=("HORIZON_ANCHOR_NOT_AVAILABLE",),
        )
    if horizon_kind is FollowUpHorizonKind.STRATEGY_HORIZON and expected_holding_days is None:
        return FollowUpDueResult(
            state=FollowUpDueState.UNSUPPORTED,
            horizon_kind=horizon_kind,
            horizon_days=None,
            target_time=None,
            reasons=("STRATEGY_HORIZON_REQUIRES_SUBJECT_DECLARED_HOLDING_DAYS",),
        )
    horizon_days = (
        SHORT_HORIZON_DAYS
        if horizon_kind is FollowUpHorizonKind.SHORT_HORIZON
        else expected_holding_days
    )
    assert horizon_days is not None  # narrowed above; STRATEGY_HORIZON already returned if None
    if horizon_days < 0:
        return FollowUpDueResult(
            state=FollowUpDueState.UNSUPPORTED,
            horizon_kind=horizon_kind,
            horizon_days=None,
            target_time=None,
            reasons=("SUBJECT_DECLARED_HOLDING_DAYS_NEGATIVE",),
        )
    target_time = horizon_anchor_at + timedelta(days=horizon_days)
    if already_observed:
        return FollowUpDueResult(
            state=FollowUpDueState.OBSERVED,
            horizon_kind=horizon_kind,
            horizon_days=horizon_days,
            target_time=target_time,
            reasons=(),
        )
    if as_of < target_time:
        return FollowUpDueResult(
            state=FollowUpDueState.NOT_YET_DUE,
            horizon_kind=horizon_kind,
            horizon_days=horizon_days,
            target_time=target_time,
            reasons=(),
        )
    return FollowUpDueResult(
        state=FollowUpDueState.DUE,
        horizon_kind=horizon_kind,
        horizon_days=horizon_days,
        target_time=target_time,
        reasons=(),
    )


@dataclass(frozen=True, slots=True)
class DecisionFollowUpObservation:
    """Immutable.  One row per ``(decision_id, policy_version, horizon_kind,
    baseline_kind)`` -- see ``followup_observation_id``.  CRYPTO-only and
    KALSHI-only fields sit side by side and are ``None`` when not applicable
    to ``observation_type``, the same shape ``CounterfactualDecision``
    already uses for its own eligibility-only vs mandate-only fields.

    ``baseline_kind`` (never ``None`` -- always a real, structurally-true
    value, exactly like ``horizon_kind``) records which instant this row's
    own ``reference_price_at_baseline``/``target_time`` are anchored to --
    see ``FollowUpBaselineKind``.  Every ``FOLLOWUP_POLICY_V1`` row and
    every KALSHI row of any policy version is ``DECISION_TIME`` (that is
    what those mechanisms have always structurally been, bugs in *which*
    price they selected notwithstanding); only a CRYPTO row observed under
    ``FOLLOWUP_POLICY_V2`` can be ``ORIGIN_EVIDENCE``.

    ``baseline_time`` (also never ``None``) is the actual instant
    ``reference_price_at_baseline``/``target_time`` were computed from --
    ``decision_evaluated_at`` for ``DECISION_TIME`` rows, the subject's own
    ``SharedOpportunityProjection.generated_at`` for ``ORIGIN_EVIDENCE``
    rows.  ``decision_evaluated_at`` itself stays what it always was: the
    decision's own evaluation instant, denormalized for join-free cohort
    queries (Part: "so cohort queries... don't require a join") --
    independent of which ``baseline_kind`` this particular row is."""

    observation_id: str
    decision_id: str
    decision_canonical_hash: str
    venue: SharedVenue
    disposition: DecisionDisposition
    policy_version: str
    horizon_kind: FollowUpHorizonKind
    baseline_kind: FollowUpBaselineKind
    horizon_days: int
    decision_evaluated_at: datetime
    baseline_time: datetime
    target_time: datetime
    observation_type: FollowUpObservationType
    subject_instrument_id: str
    subject_product_or_market_id: str
    provenance: EvidenceClass
    # CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
    # Remediation v1.  ``provenance`` above is left unchanged in meaning --
    # it still always equals ``target_evidence_class`` below, exactly as
    # every already-retained row (V1 and V2, both venues) already computed
    # it, so no existing consumer of ``.provenance`` needs to change.  These
    # two fields exist because ``provenance`` alone let a fixture-derived
    # *baseline* look exactly like ordinary ``HISTORICAL_PUBLIC`` evidence
    # whenever the *target* side happened to be genuine (the confirmed
    # DOGE-USD incident: ``provenance`` read ``HISTORICAL_PUBLIC`` while the
    # baseline was actually ``FIXTURE``) -- baseline and target evidence
    # quality must both be independently visible, never merged into one
    # field a reader could mistake for describing the whole observation.
    #
    # ``target_evidence_class`` is never ``None`` -- KALSHI rows are always
    # ``EvidenceClass.GENUINE_PUBLIC`` (a live public read), and a CRYPTO
    # row's target bucket is always resolved via the identical eligibility-
    # filtered lookup as its baseline (see ``build_crypto_price_movement_
    # observation``).  ``baseline_evidence_class`` is ``EvidenceClass |
    # None`` because KALSHI has no baseline batch lineage concept at all
    # (``build_kalshi_market_status_observation`` always stamps ``None``);
    # for CRYPTO it is populated for every row built by the current code
    # (never ``None``) but reads back as ``None`` for every row retained
    # before this milestone (backward-compatible reconstruction -- see
    # ``adapters.counterfactual_followup_postgres._observation``), because
    # that fact was never captured at write time and must never be guessed
    # after the fact.  A retroactive, explicit determination for those
    # legacy rows lives in the separate, additive ``FollowUpEvidenceQuality
    # Assessment`` record instead (Part: remediation) -- never backfilled
    # onto the immutable observation itself.
    baseline_evidence_class: EvidenceClass | None
    target_evidence_class: EvidenceClass
    # Generation-semantics versioning (Part: see this module's own dedicated
    # section immediately above ``CRYPTO_EVIDENCE_ELIGIBILITY_POLICY_V1``
    # for the full Required Invariant and rationale).  ``None`` for every
    # row retained before this concept existed and for every KALSHI row of
    # any age (never applicable there); always ``CURRENT_EVIDENCE_
    # ELIGIBILITY_POLICY_VERSION`` for a CRYPTO row built by the current
    # code.  Never re-derived from other fields -- a plain, stamped-once
    # generation-time fact, not a computed property.
    evidence_eligibility_policy_version: str | None
    observed_at: datetime
    source_lineage: tuple[str, ...]
    data_limitations: tuple[str, ...]
    # -- CRYPTO_REFERENCE_PRICE_MOVEMENT only --
    reference_price_at_baseline: Decimal | None
    reference_price_at_target: Decimal | None
    subsequent_return: Decimal | None
    # -- KALSHI_MARKET_STATUS_AND_PRICE only --
    market_status_at_target: str | None
    yes_bid_at_target: Decimal | None
    yes_ask_at_target: Decimal | None
    settlement_result: str | None
    settlement_source: str | None
    # -- FOLLOWUP_POLICY_V3 only (Counterfactual Forward Outcome Temporal
    # Integrity v1) --  All six are ``None`` on every ``FOLLOWUP_POLICY_V1``
    # and ``FOLLOWUP_POLICY_V2`` row, on every row of any age retained before
    # this milestone, and on every KALSHI/SECURITIES row of any policy
    # version; all six are non-``None`` on every V3 row.  That is an
    # all-or-nothing contract, validated in ``build_crypto_price_movement_
    # observation`` and -- for ``measurement_as_of``, the one that is also a
    # real column -- restated at the database by migration 0113's
    # biconditional ``ck_decision_followup_v3_measurement_as_of``.  None of
    # them is ever backfilled onto a legacy row or guessed after the fact,
    # for exactly the reason ``baseline_evidence_class`` above is not.
    #
    # ``measurement_as_of``: the declared instant every V3 endpoint lookup
    # was bounded by (``batch.completed_at <= measurement_as_of``).  Declared
    # at sync/task dispatch and passed down; never derived retrospectively
    # from ``observed_at`` -- which is deliberately excluded from the
    # canonical hash and therefore structurally cannot carry measurement
    # semantics.  This is what makes a V3 row replayable: re-measuring with
    # the identical ``measurement_as_of`` reproduces the identical result
    # even after newer historical batches have landed.
    measurement_as_of: datetime | None
    # ``policy_anchor_at``: the *raw* anchor the economic window was derived
    # from -- ``decision_evaluated_at`` for a ``DECISION_TIME`` row, the
    # subject's own ``SharedOpportunityProjection.generated_at`` for an
    # ``ORIGIN_EVIDENCE`` one.  Retained separately from ``baseline_time``
    # because under V3 those two are genuinely different facts:
    # ``baseline_time`` is W0 (the first daily-close boundary at or *after*
    # the anchor, i.e. the instant ``reference_price_at_baseline`` actually
    # represents), while this is the anchor itself.  Keeping both is what
    # lets a reader verify ``baseline_time >= policy_anchor_at`` -- the
    # no-pre-anchor-movement property -- directly from the retained row.
    policy_anchor_at: datetime | None
    # -- decision-input reference (disclosure only; never a return endpoint) --
    # "What historical price evidence could Apex see at the policy anchor?"
    # Point-in-time constrained and legitimately stale.  Retained so a reader
    # can derive baseline staleness (``policy_anchor_at`` minus the instant
    # ``decision_input_reference_bucket_start``'s close represents) from the
    # anchor and the retained source bucket, rather than from a redundantly
    # persisted duration.  ``subsequent_return`` is computed exclusively from
    # ``reference_price_at_baseline``/``reference_price_at_target``; a V3 row
    # additionally says so in its own ``data_limitations``.
    decision_input_reference_price: Decimal | None
    decision_input_reference_bucket_start: datetime | None
    decision_input_source_observation_id: str | None
    decision_input_evidence_class: EvidenceClass | None
    canonical_hash: str
    authority: str = "NONE"


def followup_observation_id(
    *,
    decision_id: str,
    policy_version: str,
    horizon_kind: FollowUpHorizonKind,
    baseline_kind: FollowUpBaselineKind,
) -> str:
    """Stable per ``(decision, policy version, horizon kind, baseline
    kind)``; independent of ``observed_at``/measured values so a genuine
    idempotent replay always reproduces the identical id (Part 10/13) --
    mirrors ``counterfactual_classification.classification_identity``
    exactly.

    ``baseline_kind`` only widens the identity tuple when it is
    ``FollowUpBaselineKind.ORIGIN_EVIDENCE`` -- every ``DECISION_TIME`` call
    (which is every already-retained id in this repository's history:
    ``FOLLOWUP_POLICY_V1`` never had this axis, and KALSHI never gets any
    other kind) reproduces the exact same 4-element formula this function
    has always used, byte-for-byte.  This is deliberate, not an oversight:
    ``ORIGIN_EVIDENCE`` is the genuinely new measurement this axis exists to
    add, so it is the one that earns a new identity component; ``DECISION_
    TIME`` is "the thing every row already was" and must never silently
    change what id it produces."""

    components: tuple[str, ...] = (
        COUNTERFACTUAL_FOLLOWUP_OBSERVATION_V1,
        policy_version,
        decision_id,
        horizon_kind.value,
    )
    if baseline_kind is FollowUpBaselineKind.ORIGIN_EVIDENCE:
        components = (*components, baseline_kind.value)
    return deterministic_id("decision_followup_observation", components)


def followup_observation_canonical_hash(
    *,
    observation_id: str,
    decision_id: str,
    decision_canonical_hash: str,
    venue: SharedVenue | str,
    disposition: DecisionDisposition | str,
    policy_version: str,
    horizon_kind: FollowUpHorizonKind | str,
    horizon_days: int,
    decision_evaluated_at: datetime,
    target_time: datetime,
    observation_type: FollowUpObservationType | str,
    subject_instrument_id: str,
    subject_product_or_market_id: str,
    provenance: EvidenceClass | str,
    source_lineage: tuple[str, ...],
    data_limitations: tuple[str, ...],
    reference_price_at_baseline: Decimal | None,
    reference_price_at_target: Decimal | None,
    subsequent_return: Decimal | None,
    market_status_at_target: str | None,
    yes_bid_at_target: Decimal | None,
    yes_ask_at_target: Decimal | None,
    settlement_result: str | None,
    settlement_source: str | None,
    baseline_kind: FollowUpBaselineKind | str,
    baseline_time: datetime,
    baseline_evidence_class: EvidenceClass | str | None = None,
    evidence_eligibility_policy_version: str | None = None,
    measurement_as_of: datetime | None = None,
    policy_anchor_at: datetime | None = None,
    decision_input_reference_price: Decimal | None = None,
    decision_input_reference_bucket_start: datetime | None = None,
    decision_input_source_observation_id: str | None = None,
    decision_input_evidence_class: EvidenceClass | str | None = None,
) -> str:
    """Includes the measured payload; excludes ``observed_at`` (Part 10/13) --
    mirrors ``counterfactual_classification.classification_canonical_hash``.
    Exposed (not underscore-prefixed) so persistence/migration code can
    recompute it exactly, matching the sibling modules' own convention.

    ``baseline_kind``/``baseline_time`` are appended to the hash payload
    *only* when ``baseline_kind`` is ``FollowUpBaselineKind.ORIGIN_EVIDENCE``
    -- every other call (every already-retained row in this repository's
    history: all of ``FOLLOWUP_POLICY_V1``, and every KALSHI row of any
    policy version) computes the exact same fixed-length tuple this function
    has always computed, so its hash is provably, structurally unchanged --
    not merely "unchanged because nobody happened to pass a new value" --
    for every row that predates this axis.  Verified against all 60 real
    retained rows in the live database: recomputing each one's hash with
    this exact function reproduces its stored ``canonical_hash`` byte-for-
    byte (see the milestone's own acceptance report).

    ``baseline_evidence_class``/``evidence_eligibility_policy_version``
    (CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
    Remediation v1) are each appended to the payload *independently*, and
    only when not ``None`` -- a final-review correction to this milestone's
    own original draft, which appended ``baseline_evidence_class``
    unconditionally and was proven, against the real retained 92-row corpus
    (60 V1 + 32 V2), to change every single one of their stored hashes on
    recomputation (a payload append changes the payload's length regardless
    of the appended value, so defaulting the parameter to ``None`` did not
    by itself preserve backward compatibility the way it does for a
    genuinely optional *value* -- only *omitting* the element entirely does).
    Conditioning each new field's own inclusion on its own non-``None``ness
    -- exactly the same idiom ``baseline_kind``/``baseline_time`` above
    already uses -- restores the invariant this docstring's own preceding
    paragraph states: every row that predates either axis (every KALSHI row
    of any age, and every CRYPTO row retained before this milestone) omits
    both elements entirely and therefore computes the exact same
    fixed-length tuple, and therefore the exact same hash, it always did.
    Re-verified against the same real 92-row corpus after this correction:
    92/92 round-trip.  Neither field is conditioned on the other or on
    ``baseline_kind`` -- each names an independent fact and each is applied
    in a fixed, deterministic order (``baseline_evidence_class`` always
    precedes ``evidence_eligibility_policy_version`` when both are present)
    so there is no field-ordering ambiguity.  ``target_evidence_class`` is
    deliberately never hashed here: it is always exactly ``provenance``
    (already hashed above), so hashing it a second time would add a
    byte-identical, redundant payload element.

    Counterfactual Forward Outcome Temporal Integrity v1 adds
    ``FOLLOWUP_POLICY_V3``'s six new facts -- ``measurement_as_of``,
    ``policy_anchor_at``, and the four decision-input reference fields -- as
    a single fixed-length block gated on ``measurement_as_of`` alone, rather
    than as six more independent conditional appends.  See the inline
    comment at that append for why six independent conditions would have
    been genuinely unsafe here in a way two are not.  ``measurement_as_of``
    is hashed deliberately and is the whole point of the block: two
    measurements of the same ``observation_id`` taken under different
    declared availability bounds are different facts and must not be able to
    hash identically, while a replay under the *same* bound must reproduce
    the identical hash byte-for-byte."""

    payload: tuple[object, ...] = (
        COUNTERFACTUAL_FOLLOWUP_OBSERVATION_V1,
        observation_id,
        decision_id,
        decision_canonical_hash,
        venue,
        disposition,
        policy_version,
        horizon_kind,
        horizon_days,
        decision_evaluated_at,
        target_time,
        observation_type,
        subject_instrument_id,
        subject_product_or_market_id,
        provenance,
        source_lineage,
        data_limitations,
        reference_price_at_baseline,
        reference_price_at_target,
        subsequent_return,
        market_status_at_target,
        yes_bid_at_target,
        yes_ask_at_target,
        settlement_result,
        settlement_source,
        "NONE",
    )
    if baseline_kind == FollowUpBaselineKind.ORIGIN_EVIDENCE:
        payload = (*payload, baseline_kind, baseline_time)
    if baseline_evidence_class is not None:
        payload = (*payload, baseline_evidence_class)
    if evidence_eligibility_policy_version is not None:
        payload = (*payload, evidence_eligibility_policy_version)
    if measurement_as_of is not None:
        # Counterfactual Forward Outcome Temporal Integrity v1: all six V3
        # facts are appended as ONE fixed-length block under ONE condition,
        # led by a literal marker -- deliberately not six independently
        # conditional appends in the idiom the two elements immediately
        # above use.  That idiom is safe for a pair of mutually independent
        # optional *scalars*, but six of them could place two genuinely
        # different field combinations at the same payload positions (e.g.
        # "price present, bucket absent" versus "price absent, bucket
        # present" both append one element), which would make two materially
        # different observations hash identically.  An all-or-nothing block
        # cannot: ``measurement_as_of`` is non-``None`` on exactly the V3
        # rows, which are exactly the rows where all six are non-``None``
        # (enforced in ``build_crypto_price_movement_observation``), so the
        # block is present in full or absent in full, never partially.
        #
        # Every row that predates V3 -- every ``FOLLOWUP_POLICY_V1`` and
        # ``FOLLOWUP_POLICY_V2`` row of every venue -- passes
        # ``measurement_as_of=None``, omits the block entirely, and therefore
        # computes the exact same tuple, and the exact same hash, it always
        # did.  That is a structural property of the omission, not an
        # artifact of the appended values, exactly as the docstring above
        # requires.
        payload = (
            *payload,
            FOLLOWUP_POLICY_V3,
            measurement_as_of,
            policy_anchor_at,
            decision_input_reference_price,
            decision_input_reference_bucket_start,
            decision_input_source_observation_id,
            decision_input_evidence_class,
        )
    return content_digest(payload)


def _lineage(*values: str | None) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return tuple(seen)


def build_crypto_price_movement_observation(
    *,
    decision: CounterfactualDecision,
    horizon_kind: FollowUpHorizonKind,
    baseline_kind: FollowUpBaselineKind,
    horizon_days: int,
    baseline_time: datetime,
    target_time: datetime,
    subject_instrument_id: str,
    subject_product_or_market_id: str,
    price_at_baseline: Decimal,
    price_at_baseline_source_observation_id: str,
    price_at_baseline_evidence_class: EvidenceClass,
    price_at_target: Decimal,
    price_at_target_source_observation_id: str,
    price_at_target_evidence_class: EvidenceClass,
    observed_at: datetime,
    policy_version: str = FOLLOWUP_POLICY_V1,
    measurement_as_of: datetime | None = None,
    policy_anchor_at: datetime | None = None,
    decision_input_reference_price: Decimal | None = None,
    decision_input_reference_bucket_start: datetime | None = None,
    decision_input_source_observation_id: str | None = None,
    decision_input_evidence_class: EvidenceClass | None = None,
) -> DecisionFollowUpObservation:
    """Pure; performs no I/O.  ``policy_version`` defaults to
    ``FOLLOWUP_POLICY_V1`` -- not whatever the current per-venue selection
    happens to name --
    deliberately: this builder shipped, and was already called from
    ``application.counterfactual_followup`` and exercised by this module's
    own test suite, before ``FOLLOWUP_POLICY_V2`` existed.  Defaulting to a
    mutable "current" alias would silently change every one of those
    existing call sites' behavior the moment a future version becomes
    current -- exactly the "ambiguous, implicit version switch" this
    milestone exists to eliminate elsewhere.  Defaulting to the literal,
    permanently-frozen ``FOLLOWUP_POLICY_V1`` string instead means an
    un-parametrized call reproduces V1 forever, by construction, regardless
    of how many later versions ship; a caller that wants a different version
    must say so.  ``baseline_kind``/``baseline_time`` have no default --
    every caller, V1 included, must say explicitly which instant this row's
    baseline/target horizon are anchored to (Part: "never implicit, always
    explicit" -- this module's own recurring discipline).

    Callers must already have resolved both reference prices from retained
    ``domain.crypto_universe.CryptoDailyObservation`` buckets -- Part 5: no
    silent nearest-available substitution for either side -- if a required
    bucket is not retained, the caller must not call this builder at all;
    that is a ``BLOCKED_UNAVAILABLE`` application-layer fact, not something
    this builder papers over.

    CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
    Remediation v1: both ``price_at_baseline_evidence_class`` and
    ``price_at_target_evidence_class`` must be ``is_followup_evidence_
    eligible`` -- raises ``CounterfactualFollowUpError`` otherwise (checked
    baseline-first).  This is defense in depth, not the primary fix: the
    one real repository (``adapters.counterfactual_followup_postgres.
    _CryptoFollowUpPriceSource``) already excludes ineligible batches
    *before* ranking/tie-breaking, so a well-behaved caller should never
    reach this check in normal operation -- exactly the same "does not rely
    on its only caller behaving" idiom already used a few lines below for
    the V1+``ORIGIN_EVIDENCE`` combination.  Confirmed, read-only-audited
    incident this guards against: the first genuine CRYPTO
    ``FOLLOWUP_POLICY_V2`` observation ever retained stamped a synthetic
    ``EvidenceClass.FIXTURE``-sourced baseline (``189.504``, not a real
    DOGE-USD price) because the *lookup* layer never filtered evidence
    class at all -- see ``FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES``'s own
    module-level rationale above for the full trace and the fail-closed
    allowlist design.

    ``price_at_target`` must always be resolved
    from an *exact* bucket match, fail closed -- unchanged in *mechanism*
    from V1 -- but *which* bucket is no longer always the one whose
    ``bucket_start`` exactly equals ``completed_utc_day(target_time)``:
    ``FOLLOWUP_POLICY_V1`` still resolves it that way (frozen); any later
    policy version resolves it as the resolved baseline candle's own
    ``bucket_start`` plus ``horizon_days`` instead (Part:
    target-bucket-from-baseline-candle -- see the ``FOLLOWUP_POLICY_V2``
    comment block above for why the naive ``completed_utc_day(target_time)``
    bucket would otherwise silently compare closes ``horizon_days + 1``
    calendar days apart, not ``horizon_days``).  ``target_time`` itself is no
    longer always ``decision.evaluated_at + horizon_days``: it
    is ``baseline_time + horizon_days``, and ``baseline_time`` is a function
    of ``baseline_kind`` chosen by the caller
    (``application.counterfactual_followup.SyncFollowUpObservations.
    _build_crypto``), not this builder.  ``price_at_baseline``'s own
    resolution rule is likewise the caller's job: ``FOLLOWUP_POLICY_V1``
    (always ``baseline_kind=DECISION_TIME``) resolves it from the bucket
    whose ``bucket_start`` exactly equals
    ``completed_utc_day(decision.evaluated_at)`` (frozen, and confirmed by
    this milestone's original audit to be genuinely wrong for CRYPTO -- see
    the ``FOLLOWUP_POLICY_V2`` comment block above ``FOLLOWUP_POLICY_V2``'s
    own definition); ``FOLLOWUP_POLICY_V2`` resolves it from the latest
    retained daily close strictly *before* whichever boundary
    ``baseline_kind`` selects -- the subject's own
    ``completed_utc_day(projection.generated_at)`` for ``ORIGIN_EVIDENCE``,
    or the (corrected, no-longer-hindsight-leaking)
    ``completed_utc_day(decision.evaluated_at)`` for ``DECISION_TIME``.
    This builder itself performs no such resolution and does not care which
    policy or baseline kind chose the values it was handed -- it only ever
    stamps whichever ``policy_version``/``baseline_kind``/``baseline_time``
    the caller passed into the observation's identity, hash, and retained
    payload."""

    if price_at_baseline <= 0 or price_at_target <= 0:
        raise CounterfactualFollowUpError("CRYPTO_FOLLOWUP_REFERENCE_PRICE_INVALID")
    if not is_followup_evidence_eligible(price_at_baseline_evidence_class):
        raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_BASELINE_EVIDENCE_INELIGIBLE)
    if not is_followup_evidence_eligible(price_at_target_evidence_class):
        raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_TARGET_EVIDENCE_INELIGIBLE)
    if (
        policy_version == FOLLOWUP_POLICY_V1
        and baseline_kind is FollowUpBaselineKind.ORIGIN_EVIDENCE
    ):
        # FOLLOWUP_POLICY_V1 never had this axis at all -- every V1 row is,
        # structurally, DECISION_TIME (Part: V1 immutability;
        # FollowUpBaselineKind's own docstring). The recurring-cycle/CLI
        # orchestration (application.counterfactual_followup._baseline_
        # kinds_for) never constructs this combination, but this builder
        # does not rely on its only caller behaving -- an invalid
        # combination is rejected here, at the source, not merely avoided
        # by convention.
        raise CounterfactualFollowUpError("CRYPTO_FOLLOWUP_V1_ORIGIN_EVIDENCE_COMBINATION_INVALID")
    # --------------------------------------------------------------
    # Counterfactual Forward Outcome Temporal Integrity v1 -- the six
    # ``FOLLOWUP_POLICY_V3`` facts are an all-or-nothing contract, validated
    # in both directions.  Not merely "V3 requires them": a V1/V2 caller that
    # passes any of them is rejected too, because a legacy row carrying even
    # one of them would break the hash-block invariant the canonical hash
    # depends on (see ``followup_observation_canonical_hash``) and would also
    # violate migration 0113's biconditional database check.  Rejecting at
    # the source, not relying on the one caller behaving -- the same idiom
    # the V1/``ORIGIN_EVIDENCE`` guard immediately above already uses.
    # --------------------------------------------------------------
    v3_facts = (
        measurement_as_of,
        policy_anchor_at,
        decision_input_reference_price,
        decision_input_reference_bucket_start,
        decision_input_source_observation_id,
        decision_input_evidence_class,
    )
    if policy_version == FOLLOWUP_POLICY_V3:
        if not followup_policy_version_supports_venue(
            policy_version=policy_version, venue=decision.venue
        ):
            raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_V3_VENUE_UNSUPPORTED)
        if any(fact is None for fact in v3_facts):
            raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_V3_REQUIRED_FACT_MISSING)
        assert measurement_as_of is not None  # narrowed by the all-or-nothing check above
        assert policy_anchor_at is not None
        assert decision_input_reference_price is not None
        assert decision_input_evidence_class is not None
        if not is_followup_evidence_eligible(decision_input_evidence_class):
            raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_DECISION_INPUT_EVIDENCE_INELIGIBLE)
        if decision_input_reference_price <= 0:
            raise CounterfactualFollowUpError("CRYPTO_FOLLOWUP_REFERENCE_PRICE_INVALID")
        # ``baseline_time``/``target_time`` under V3 are W0/W1 -- the
        # economic window ``forward_measurement_window`` derived.  Restated
        # here rather than trusted: these three are the properties the whole
        # milestone exists to guarantee, so the builder that stamps them into
        # an immutable, hashed row verifies them itself.
        if baseline_time < policy_anchor_at:
            raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_V3_WINDOW_PRECEDES_ANCHOR)
        # ``>= anchor`` alone would still admit an arbitrarily *late* window
        # stamped under the V3 name, which would be a different -- and
        # equally invisible after the fact -- misstatement of what this
        # immutable row measured.  The window must be exactly the one the
        # anchor implies, so this restates ``forward_measurement_window``'s
        # own derivation independently of whichever caller performed it.
        if baseline_time != first_daily_close_boundary_at_or_after(policy_anchor_at):
            raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_V3_WINDOW_START_NOT_DERIVED)
        if target_time - baseline_time != timedelta(days=horizon_days):
            raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_V3_WINDOW_SPAN_INVALID)
        # Measuring "as of" an instant before the window has even closed is
        # not a legitimate reading -- the end candle cannot exist yet.  In
        # normal operation this is unreachable (a pair is only DUE once
        # ``as_of >= W1``); it is checked because an explicit replay is
        # allowed to name its own ``measurement_as_of``.
        if measurement_as_of < target_time:
            raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_V3_MEASUREMENT_AS_OF_PRECEDES_WINDOW)
    elif any(fact is not None for fact in v3_facts):
        raise CounterfactualFollowUpError(CRYPTO_FOLLOWUP_V3_FACT_ON_NON_V3_POLICY)
    subsequent_return = ((price_at_target - price_at_baseline) / price_at_baseline).quantize(
        RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    provenance = price_at_target_evidence_class
    observation_id = followup_observation_id(
        decision_id=decision.decision_id,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
    )
    # Counterfactual Forward Outcome Temporal Integrity v1 deliberately does
    # NOT add a V3 row's decision-input reference batch here, although it is
    # genuine retained lineage.  ``source_lineage`` carries a documented
    # *positional* contract that ``adapters.counterfactual_followup_postgres.
    # resolve_observation_evidence_lineage`` depends on -- index 0 is the
    # baseline's source batch, index 1 the target's -- and ``_lineage``
    # de-duplicates, so appending a third id makes that contract ambiguous
    # exactly when the baseline and target batches coincide (a genuine
    # ``horizon_days == 0`` observation): the two-element tuple would then be
    # ``(baseline, decision_input)``, indistinguishable from an ordinary
    # ``(baseline, target)`` one, and the evidence-quality assessment would
    # silently re-derive the wrong "target" evidence class.  The
    # decision-input batch is instead retained in its own dedicated,
    # unambiguous field (``decision_input_source_observation_id``), where it
    # is directly resolvable from the row without any positional decoding --
    # which is strictly better disclosure, not weaker.
    source_lineage = _lineage(
        price_at_baseline_source_observation_id, price_at_target_source_observation_id
    )
    limitations = (
        (
            CRYPTO_DAILY_CLOSE_LIMITATION,
            CRYPTO_MEASUREMENT_AS_OF_BOUNDED_LIMITATION,
            CRYPTO_DECISION_INPUT_REFERENCE_NOT_ENDPOINT_LIMITATION,
        )
        if policy_version == FOLLOWUP_POLICY_V3
        else (CRYPTO_DAILY_CLOSE_LIMITATION,)
    )
    canonical_hash = followup_observation_canonical_hash(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=decision.venue,
        disposition=decision.disposition,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        horizon_days=horizon_days,
        decision_evaluated_at=decision.evaluated_at,
        target_time=target_time,
        observation_type=FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
        subject_instrument_id=subject_instrument_id,
        subject_product_or_market_id=subject_product_or_market_id,
        provenance=provenance,
        source_lineage=source_lineage,
        data_limitations=limitations,
        reference_price_at_baseline=price_at_baseline,
        reference_price_at_target=price_at_target,
        subsequent_return=subsequent_return,
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
        baseline_kind=baseline_kind,
        baseline_time=baseline_time,
        baseline_evidence_class=price_at_baseline_evidence_class,
        evidence_eligibility_policy_version=CURRENT_EVIDENCE_ELIGIBILITY_POLICY_VERSION,
        measurement_as_of=measurement_as_of,
        policy_anchor_at=policy_anchor_at,
        decision_input_reference_price=decision_input_reference_price,
        decision_input_reference_bucket_start=decision_input_reference_bucket_start,
        decision_input_source_observation_id=decision_input_source_observation_id,
        decision_input_evidence_class=decision_input_evidence_class,
    )
    return DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=decision.venue,
        disposition=decision.disposition,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        horizon_days=horizon_days,
        decision_evaluated_at=decision.evaluated_at,
        baseline_time=baseline_time,
        target_time=target_time,
        observation_type=FollowUpObservationType.CRYPTO_REFERENCE_PRICE_MOVEMENT,
        subject_instrument_id=subject_instrument_id,
        subject_product_or_market_id=subject_product_or_market_id,
        provenance=provenance,
        baseline_evidence_class=price_at_baseline_evidence_class,
        target_evidence_class=price_at_target_evidence_class,
        # Every CRYPTO row this builder constructs has just been through the
        # eligibility check above -- always the current, in-effect version
        # (Part: see this module's own "Generation-semantics versioning"
        # section for why this is never merely inferred from ``baseline_
        # evidence_class`` being non-``None``).
        evidence_eligibility_policy_version=CURRENT_EVIDENCE_ELIGIBILITY_POLICY_VERSION,
        observed_at=observed_at,
        source_lineage=source_lineage,
        data_limitations=limitations,
        reference_price_at_baseline=price_at_baseline,
        reference_price_at_target=price_at_target,
        subsequent_return=subsequent_return,
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
        measurement_as_of=measurement_as_of,
        policy_anchor_at=policy_anchor_at,
        decision_input_reference_price=decision_input_reference_price,
        decision_input_reference_bucket_start=decision_input_reference_bucket_start,
        decision_input_source_observation_id=decision_input_source_observation_id,
        decision_input_evidence_class=decision_input_evidence_class,
        canonical_hash=canonical_hash,
        authority="NONE",
    )


def build_kalshi_market_status_observation(
    *,
    decision: CounterfactualDecision,
    horizon_kind: FollowUpHorizonKind,
    horizon_days: int,
    target_time: datetime,
    subject_instrument_id: str,
    subject_product_or_market_id: str,
    market_status: str,
    yes_bid: Decimal | None,
    yes_ask: Decimal | None,
    settlement_result: str | None,
    settlement_source: str | None,
    source_endpoint_identity: str,
    observed_at: datetime,
    additional_limitations: tuple[str, ...] = (),
    policy_version: str = FOLLOWUP_POLICY_V1,
) -> DecisionFollowUpObservation:
    """Pure; performs no I/O.  ``market_status``/``yes_bid``/``yes_ask``/
    ``settlement_result``/``settlement_source`` must already have been parsed
    from a genuine bounded Kalshi public read by the caller (Part 8/16) --
    this builder never contacts a provider and never infers settlement from
    price (Part 8).  ``additional_limitations`` lets that caller disclose its
    own structural caveats (e.g. "this event has more than one nested market
    so no single yes_bid/yes_ask was attributable") without this builder
    needing to know Kalshi wire-format details itself.

    ``policy_version`` defaults to ``FOLLOWUP_POLICY_V1`` for exactly the
    same reason ``build_crypto_price_movement_observation`` does -- see its
    own docstring.  Unlike the crypto builder, this one's *behavior* is
    identical no matter which known policy version is passed: a Kalshi
    observation has never involved a "decision-day bucket" computation of
    any kind, so ``FOLLOWUP_POLICY_V2``'s correction (a CRYPTO-only,
    decision-side baseline/target-horizon fix) changes nothing here except
    the ``policy_version`` stamped onto the row and the identity/hash values
    that are a pure function of it (Part: Kalshi non-regression -- proven in
    this module's own test suite by constructing the identical read under
    both versions and asserting every other field matches byte-for-byte).

    ``baseline_kind``/``baseline_time`` are deliberately *not* parameters
    here (unlike the crypto builder): a Kalshi observation only ever has one
    structurally possible kind (``FollowUpBaselineKind.DECISION_TIME`` --
    Apex retains no historical Kalshi quote archive to construct an
    ``ORIGIN_EVIDENCE`` reading from in v1, and this module does not
    introduce new provider history merely to manufacture one), so this
    builder fixes it internally rather than exposing a choice that would
    never legitimately vary -- Part: "do not introduce new provider history
    merely to manufacture precision"."""

    if observed_at.tzinfo is None or target_time.tzinfo is None:
        raise CounterfactualFollowUpError("KALSHI_FOLLOWUP_TIMESTAMP_NOT_AWARE")
    # Counterfactual Forward Outcome Temporal Integrity v1: FOLLOWUP_POLICY_
    # V3 is CRYPTO-only as a matter of what it *means* -- it is a daily-
    # candle forward economic-window contract, and a Kalshi observation is a
    # live public read with no retained historical quote archive and no
    # candle window of any kind to measure over.  Rejected here, at the
    # source, rather than merely never constructed by the one orchestration
    # that calls this builder -- the same "does not rely on its only caller
    # behaving" discipline the crypto builder's own V1/ORIGIN_EVIDENCE guard
    # uses, and restated independently at the database by migration 0113's
    # ``ck_decision_followup_v3_crypto_only``.
    if not followup_policy_version_supports_venue(
        policy_version=policy_version, venue=SharedVenue.KALSHI
    ):
        raise CounterfactualFollowUpError(FOLLOWUP_V3_POLICY_VERSION_UNSUPPORTED_FOR_VENUE)
    baseline_kind = FollowUpBaselineKind.DECISION_TIME
    baseline_time = decision.evaluated_at
    limitations: list[str] = list(additional_limitations)
    lateness = observed_at - target_time
    if settlement_result is None and lateness > KALSHI_LATE_OBSERVATION_TOLERANCE:
        limitations.append(KALSHI_LATE_OBSERVATION_LIMITATION)
    observation_id = followup_observation_id(
        decision_id=decision.decision_id,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
    )
    source_lineage = _lineage(source_endpoint_identity)
    data_limitations = tuple(limitations)
    canonical_hash = followup_observation_canonical_hash(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=decision.venue,
        disposition=decision.disposition,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        horizon_days=horizon_days,
        decision_evaluated_at=decision.evaluated_at,
        target_time=target_time,
        observation_type=FollowUpObservationType.KALSHI_MARKET_STATUS_AND_PRICE,
        subject_instrument_id=subject_instrument_id,
        subject_product_or_market_id=subject_product_or_market_id,
        provenance=EvidenceClass.GENUINE_PUBLIC,
        source_lineage=source_lineage,
        data_limitations=data_limitations,
        reference_price_at_baseline=None,
        reference_price_at_target=None,
        subsequent_return=None,
        market_status_at_target=market_status,
        yes_bid_at_target=yes_bid,
        yes_ask_at_target=yes_ask,
        settlement_result=settlement_result,
        settlement_source=settlement_source,
        baseline_kind=baseline_kind,
        baseline_time=baseline_time,
        baseline_evidence_class=None,
        # KALSHI evidence was never subject to the CRYPTO evidence-
        # eligibility bug this axis exists to make visible (Part: see this
        # module's own "Generation-semantics versioning" section) -- ``None``
        # here is "not applicable", exactly like ``baseline_evidence_class``
        # immediately above, never "unknown".
        evidence_eligibility_policy_version=None,
    )
    return DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=decision.venue,
        disposition=decision.disposition,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        horizon_days=horizon_days,
        decision_evaluated_at=decision.evaluated_at,
        baseline_time=baseline_time,
        target_time=target_time,
        observation_type=FollowUpObservationType.KALSHI_MARKET_STATUS_AND_PRICE,
        subject_instrument_id=subject_instrument_id,
        subject_product_or_market_id=subject_product_or_market_id,
        provenance=EvidenceClass.GENUINE_PUBLIC,
        # KALSHI has no baseline-batch lineage concept at all (Part: see
        # ``DecisionFollowUpObservation.baseline_evidence_class``'s own
        # docstring) -- a live public read has no separate "which retained
        # batch" to distinguish from ``target_evidence_class``, which is
        # always exactly ``provenance`` for every KALSHI row.
        baseline_evidence_class=None,
        target_evidence_class=EvidenceClass.GENUINE_PUBLIC,
        evidence_eligibility_policy_version=None,
        observed_at=observed_at,
        source_lineage=source_lineage,
        data_limitations=data_limitations,
        reference_price_at_baseline=None,
        reference_price_at_target=None,
        subsequent_return=None,
        market_status_at_target=market_status,
        yes_bid_at_target=yes_bid,
        yes_ask_at_target=yes_ask,
        settlement_result=settlement_result,
        settlement_source=settlement_source,
        measurement_as_of=None,
        policy_anchor_at=None,
        decision_input_reference_price=None,
        decision_input_reference_bucket_start=None,
        decision_input_source_observation_id=None,
        decision_input_evidence_class=None,
        canonical_hash=canonical_hash,
        authority="NONE",
    )


# ==========================================================================
# Securities Research -> Shared Opportunity Recurring Bridge v1.
#
# ``build_securities_structure_terminal_observation`` answers the same
# narrow objective-fact question as the two builders above, for a SECURITIES
# ``CounterfactualDecision`` whose subject is a
# ``domain.options_structures.OptionStructureCandidate``-backed
# ``SharedOpportunityProjection``: "what would this exact defined-risk
# structure's own analytic payoff have been, given the underlying's own
# later-captured reference price at (or immediately after) ``target_time``?"
#
# Always ``FollowUpBaselineKind.DECISION_TIME`` (this bridge does not
# introduce a SECURITIES analogue of CRYPTO's ``ORIGIN_EVIDENCE`` distinction
# -- a structure's own economics are computed once, at capture time, and
# ``decision.evaluated_at`` is always the same trading session, so there is
# no meaningful projection-vs-decision gap to distinguish here the way
# CRYPTO's can span multiple days). Both ``SHORT_HORIZON`` and
# ``STRATEGY_HORIZON`` are genuinely computable and genuinely distinct
# (the latter's ``horizon_days`` is the structure's own declared DTE; the
# former is always exactly 1) -- neither is suppressed, matching this
# module's own "evidence, not judgment; label honestly, never hide" idiom.
#
# Reuses ``domain.options_structures.structure_payoff_at`` -- the exact same
# deterministic, already-tested analytic payoff function
# ``OptionStructureCandidate.create`` itself uses for scenario analysis --
# rather than inventing a second payoff formula. This is deliberately an
# analytic mark-to-model at the underlying's own later-captured reference
# price, never a broker-executed close and never claimed to be the option
# contract's own official OCC settlement value (see
# ``SECURITIES_TERMINAL_PRICE_LIMITATION`` below) -- exactly the same
# "objective fact, not a judgment, and never hindsight-fabricated" contract
# CRYPTO's ``reference_price_at_target``/``subsequent_return`` already
# promise, applied to a different, but equally objective, underlying fact.
#
# ``subsequent_return`` here is deliberately *not* a raw underlying price
# return (unlike CRYPTO's use of the identical field): it is the structure's
# own analytic net profit/loss at the terminal reference price, expressed as
# a fraction of the structure's own ``capital_required`` -- the genuinely
# comparable "did the capital committed to this specific researched
# opportunity do well" fact, not merely "did the underlying move favorably"
# (which alone says nothing about whether a defined-risk spread's own
# strikes were ever reached). See ``SECURITIES_RETURN_IS_STRUCTURE_PAYOFF_
# LIMITATION`` below for the explicit, always-present disambiguating label.
# ==========================================================================

SECURITIES_TERMINAL_PRICE_LIMITATION = (
    "SECURITIES_TERMINAL_PRICE_IS_APEX_CAPTURED_REFERENCE_PRICE_NOT_OFFICIAL_SETTLEMENT"
)
SECURITIES_RETURN_IS_STRUCTURE_PAYOFF_LIMITATION = (
    "SECURITIES_SUBSEQUENT_RETURN_IS_STRUCTURE_NET_PROFIT_LOSS_OVER_CAPITAL_REQUIRED"
    "_NOT_AN_UNDERLYING_PRICE_RETURN"
)
SECURITIES_STRUCTURE_LIMITATIONS: tuple[str, ...] = (
    SECURITIES_TERMINAL_PRICE_LIMITATION,
    SECURITIES_RETURN_IS_STRUCTURE_PAYOFF_LIMITATION,
)


def build_securities_structure_terminal_observation(
    *,
    decision: CounterfactualDecision,
    horizon_kind: FollowUpHorizonKind,
    horizon_days: int,
    target_time: datetime,
    subject_instrument_id: str,
    subject_product_or_market_id: str,
    structure_type: StructureType,
    long_strike: Decimal,
    short_strike: Decimal,
    multiplier: Decimal,
    quantity: int,
    initial_cash_flow: Decimal,
    capital_required: Decimal,
    underlying_price_at_baseline: Decimal,
    underlying_price_at_baseline_source_id: str,
    underlying_price_at_target: Decimal,
    underlying_price_at_target_source_id: str,
    observed_at: datetime,
    policy_version: str,
) -> DecisionFollowUpObservation:
    """Pure; performs no I/O.  Callers must already have resolved
    ``underlying_price_at_target`` from an already-retained
    ``domain.options_quote_intelligence.OptionsChainSnapshot`` captured at or
    after ``target_time`` for the structure's own underlying -- Part 5's
    "no silent nearest-available substitution" applies identically here: if
    no such snapshot is yet retained, the caller must not call this builder
    at all (a ``BLOCKED_UNAVAILABLE`` application-layer fact, exactly like a
    missing CRYPTO daily-close bucket).

    ``decision.venue`` is not re-checked here (every field this function
    stamps is explicit, never re-derived from the decision) -- callers must
    only ever invoke this for a genuine SECURITIES decision; see
    ``application.counterfactual_followup.SyncFollowUpObservations.
    _build_securities``, the only caller, which never invokes it otherwise."""

    if target_time.tzinfo is None or observed_at.tzinfo is None:
        raise CounterfactualFollowUpError("SECURITIES_FOLLOWUP_TIMESTAMP_NOT_AWARE")
    # Counterfactual Forward Outcome Temporal Integrity v1: see the
    # identical guard in ``build_kalshi_market_status_observation`` above.  A
    # SECURITIES structure terminal payoff is resolved by an entirely
    # separate mechanism (an options-chain snapshot at or after
    # ``target_time``), so FOLLOWUP_POLICY_V3's daily-candle window contract
    # has nothing to say about it and must never be stamped onto one.
    if not followup_policy_version_supports_venue(
        policy_version=policy_version, venue=SharedVenue.SECURITIES
    ):
        raise CounterfactualFollowUpError(FOLLOWUP_V3_POLICY_VERSION_UNSUPPORTED_FOR_VENUE)
    if underlying_price_at_baseline <= 0 or underlying_price_at_target <= 0:
        raise CounterfactualFollowUpError("SECURITIES_FOLLOWUP_REFERENCE_PRICE_INVALID")
    if capital_required <= 0:
        raise CounterfactualFollowUpError("SECURITIES_FOLLOWUP_CAPITAL_REQUIRED_INVALID")
    payoff = structure_payoff_at(
        structure_type=structure_type,
        long_strike=long_strike,
        short_strike=short_strike,
        multiplier=multiplier,
        quantity=quantity,
        initial_cash_flow=initial_cash_flow,
        underlying_price_at_expiration=underlying_price_at_target,
    )
    subsequent_return = (payoff.net_profit_loss / capital_required).quantize(
        RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    baseline_kind = FollowUpBaselineKind.DECISION_TIME
    baseline_time = decision.evaluated_at
    provenance = EvidenceClass.GENUINE_PUBLIC
    observation_id = followup_observation_id(
        decision_id=decision.decision_id,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
    )
    source_lineage = _lineage(
        underlying_price_at_baseline_source_id, underlying_price_at_target_source_id
    )
    canonical_hash = followup_observation_canonical_hash(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.SECURITIES,
        disposition=decision.disposition,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        horizon_days=horizon_days,
        decision_evaluated_at=decision.evaluated_at,
        target_time=target_time,
        observation_type=FollowUpObservationType.SECURITIES_STRUCTURE_TERMINAL_PAYOFF,
        subject_instrument_id=subject_instrument_id,
        subject_product_or_market_id=subject_product_or_market_id,
        provenance=provenance,
        source_lineage=source_lineage,
        data_limitations=SECURITIES_STRUCTURE_LIMITATIONS,
        reference_price_at_baseline=underlying_price_at_baseline,
        reference_price_at_target=underlying_price_at_target,
        subsequent_return=subsequent_return,
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
        baseline_kind=baseline_kind,
        baseline_time=baseline_time,
        baseline_evidence_class=provenance,
        evidence_eligibility_policy_version=None,
    )
    return DecisionFollowUpObservation(
        observation_id=observation_id,
        decision_id=decision.decision_id,
        decision_canonical_hash=decision.canonical_hash,
        venue=SharedVenue.SECURITIES,
        disposition=decision.disposition,
        policy_version=policy_version,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        horizon_days=horizon_days,
        decision_evaluated_at=decision.evaluated_at,
        baseline_time=baseline_time,
        target_time=target_time,
        observation_type=FollowUpObservationType.SECURITIES_STRUCTURE_TERMINAL_PAYOFF,
        subject_instrument_id=subject_instrument_id,
        subject_product_or_market_id=subject_product_or_market_id,
        provenance=provenance,
        baseline_evidence_class=provenance,
        target_evidence_class=provenance,
        # No SECURITIES analogue of CRYPTO's contamination-remediation
        # policy versioning exists (or is needed): this bridge's evidence
        # class is always stamped directly from the genuine, live-credentialed
        # Alpaca capture path (see ``adapters.shared_opportunity_sources.
        # RetainedSharedOpportunitySource.securities_projections``), never
        # inferred through the CRYPTO-specific lineage-lookup this version
        # concept exists to police -- ``None`` here mirrors exactly how every
        # KALSHI row (which has the same "no such policy applies" property)
        # already stamps it.
        evidence_eligibility_policy_version=None,
        observed_at=observed_at,
        source_lineage=source_lineage,
        data_limitations=SECURITIES_STRUCTURE_LIMITATIONS,
        reference_price_at_baseline=underlying_price_at_baseline,
        reference_price_at_target=underlying_price_at_target,
        subsequent_return=subsequent_return,
        market_status_at_target=None,
        yes_bid_at_target=None,
        yes_ask_at_target=None,
        settlement_result=None,
        settlement_source=None,
        measurement_as_of=None,
        policy_anchor_at=None,
        decision_input_reference_price=None,
        decision_input_reference_bucket_start=None,
        decision_input_source_observation_id=None,
        decision_input_evidence_class=None,
        canonical_hash=canonical_hash,
        authority="NONE",
    )


# ==========================================================================
# CRYPTO Follow-Up Evidence Eligibility & Contaminated Observation
# Remediation v1 -- additive remediation for a retained
# ``DecisionFollowUpObservation`` whose evidence lineage is later found to
# violate ``FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES`` (Part: remediation).
#
# The retained observation itself is historical truth about what Apex
# actually produced and is never rewritten: no ``UPDATE``, no ``DELETE``,
# no replacement row pretending to be a corrected original, no silent
# backfill.  ``FollowUpEvidenceQualityAssessment`` instead says, additively,
# "this retained observation exists, but it is ineligible for downstream
# attribution/research conclusions because its evidence lineage violated
# the evidence-eligibility contract" -- mirroring ``domain.
# counterfactual_classification.CounterfactualDecisionClassification``'s
# own idiom deliberately (see that module's own docstring for the full
# rationale; only the deltas are repeated here):
#
# * **Reference, don't duplicate.**  An assessment never copies the
#   observation's own payload -- it stores ``observation_id`` +
#   ``observation_canonical_hash`` (the same "reference by id and hash"
#   idiom ``CounterfactualDecisionClassification`` already uses for
#   ``decision_id``/``decision_canonical_hash``).
# * **Versioned, explicit, content-hash-fingerprinted policy.**  The same
#   ``<NAME>_POLICY_V1`` idiom as ``FOLLOWUP_POLICY_V1``/``FOLLOWUP_POLICY_
#   V2`` above and ``domain.counterfactual_classification.
#   REASON_CLASSIFIER_V1``.  A stricter or looser future eligibility review
#   is always a new ``FOLLOWUP_EVIDENCE_QUALITY_POLICY_Vn``, never a silent
#   edit to this one's meaning.
# * **Idempotent, re-runnable.**  ``evidence_quality_assessment_id`` excludes
#   ``assessed_at``, so re-assessing an unchanged observation under an
#   unchanged policy version always reproduces the identical
#   ``assessment_id``/``canonical_hash`` -- a hash mismatch against an
#   already-retained assessment is a genuine integrity conflict, never
#   routine drift, exactly like ``COUNTERFACTUAL_DECISION_IDENTITY_
#   CONFLICT``/``COUNTERFACTUAL_FOLLOWUP_OBSERVATION_IDENTITY_CONFLICT``.
# * **Every retained observation gets exactly one assessment per policy
#   version, KALSHI included.**  A KALSHI observation is always genuinely
#   ``ELIGIBLE`` (its target is always a live public read, and it has no
#   baseline batch lineage to distinguish -- see ``DecisionFollowUpObservation.
#   baseline_evidence_class``'s own docstring), but this module still
#   computes and stamps a real row for it rather than special-casing venue
#   out of the assessment population entirely, so "every retained follow-up
#   observation has exactly one assessment under a given policy version"
#   holds without exception, and a downstream reader never has to treat
#   "no assessment row" and "assessed ELIGIBLE" as the same thing for one
#   venue but not the other.
# ==========================================================================

FOLLOWUP_EVIDENCE_QUALITY_ASSESSMENT_V1 = "FOLLOWUP_EVIDENCE_QUALITY_ASSESSMENT_V1"
FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1 = "FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1"

# Enumerated explicitly, mirroring ``KNOWN_FOLLOWUP_POLICY_VERSIONS`` exactly
# -- a new assessment policy version is always a deliberate, reviewed code
# change, never inferred from a free-text column.
# Securities Follow-Up Temporal Integrity v1.  Venue-scoped, exactly like
# ``FOLLOWUP_POLICY_V3`` is CRYPTO-only: V2 answers a question that exists
# only for SECURITIES ("was the terminal options-chain snapshot the closing
# chain of the first eligible session at or after the target, or a later
# session's hindsight?").  CRYPTO and KALSHI stay on V1, byte-for-byte --
# no V2 row is ever written for them, and no CRYPTO/KALSHI consumer reads
# one.  See ``resolve_securities_terminal_evidence_timing`` and
# ``docs/SECURITIES_FOLLOWUP_TEMPORAL_INTEGRITY_V1.md``.
FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2 = "FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2"

KNOWN_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSIONS: tuple[str, ...] = (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
)
# The all-venue baseline every retained observation is assessed under (the
# recurring sweep's first lane, the CRYPTO/KALSHI canonical version).  Kept
# at V1 deliberately: a downstream consumer must resolve its canonical
# version *per venue* via ``current_followup_evidence_quality_policy_
# version`` -- never read this constant as "the SECURITIES verdict".
CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION = FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1

_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VENUES: Mapping[str, frozenset[SharedVenue]] = MappingProxyType(
    {
        FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1: frozenset(
            {SharedVenue.CRYPTO, SharedVenue.KALSHI, SharedVenue.SECURITIES}
        ),
        FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2: frozenset({SharedVenue.SECURITIES}),
    }
)
_CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_BY_VENUE: Mapping[SharedVenue, str] = MappingProxyType(
    {
        SharedVenue.CRYPTO: FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        SharedVenue.KALSHI: FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
        SharedVenue.SECURITIES: FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    }
)


def followup_evidence_quality_policy_supports_venue(
    *, assessment_policy_version: str, venue: SharedVenue
) -> bool:
    """Whether ``assessment_policy_version`` defines a verdict for ``venue``
    at all.  ``False`` for an unknown version (fail closed)."""

    venues = _FOLLOWUP_EVIDENCE_QUALITY_POLICY_VENUES.get(assessment_policy_version)
    return venues is not None and venue in venues


def current_followup_evidence_quality_policy_version(venue: SharedVenue) -> str:
    """The canonical evidence-quality policy version a downstream consumer
    must read for ``venue`` -- V2 for SECURITIES (Securities Follow-Up
    Temporal Integrity v1), V1 for CRYPTO/KALSHI (unchanged)."""

    return _CURRENT_FOLLOWUP_EVIDENCE_QUALITY_POLICY_BY_VENUE[venue]


FOLLOWUP_EVIDENCE_QUALITY_WARNING = (
    "FOLLOW-UP EVIDENCE QUALITY ASSESSMENT — RETROACTIVE ELIGIBILITY VERDICT ONLY — "
    "NEVER REWRITES OR REPLACES THE ASSESSED OBSERVATION — NO ALLOCATION OR EXECUTION AUTHORITY"
)


class FollowUpEvidenceQualityVerdict(StrEnum):
    """Exactly two verdicts -- "the smallest useful policy", matching every
    other closed vocabulary in this module.  ``UNASSESSED`` is deliberately
    *not* a member here: it is never a fact this module stamps onto a row,
    only the read-model's own description of "no assessment row exists yet
    for this observation" (Part: an absence, not a verdict -- see
    ``application.operator_console_evidence.build_operator_followups``)."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE_EVIDENCE = "INELIGIBLE_EVIDENCE"


class FollowUpEvidenceDefectType(StrEnum):
    """Closed taxonomy of *why* an assessment reached its verdict -- never
    free text, exactly the discipline every other reason/limitation
    vocabulary in this module already uses (``CRYPTO_BASELINE_PERMANENTLY_
    UNAVAILABLE``, ``KALSHI_LATE_OBSERVATION_LIMITATION``, ...)."""

    NONE = "NONE"
    BASELINE_EVIDENCE_INELIGIBLE = "BASELINE_EVIDENCE_INELIGIBLE"
    TARGET_EVIDENCE_INELIGIBLE = "TARGET_EVIDENCE_INELIGIBLE"
    BOTH_SIDES_EVIDENCE_INELIGIBLE = "BOTH_SIDES_EVIDENCE_INELIGIBLE"
    LINEAGE_NOT_RESOLVABLE = "LINEAGE_NOT_RESOLVABLE"
    # Final-review addition (Part: Section 6, assessment persistence /
    # immutability).  The assessment's own ``observation_canonical_hash``
    # used to be a plain, unverified copy of ``observation.canonical_hash``
    # -- correct for every genuine row (nothing here ever had a reason to
    # differ) but never actually *checked* against an independent
    # recomputation, so a corrupted or tampered retained observation (the
    # immutable-trigger boundary's own defense in depth, not something this
    # module assumes can never happen) would have been silently assessed as
    # if its stored hash were trustworthy.  Now checked, fail-closed, by
    # ``observation_canonical_hash_matches`` below -- a mismatch is always
    # ``INELIGIBLE_EVIDENCE``, regardless of venue or evidence class,
    # because a hash that does not match its own fields means this module
    # can no longer trust *any* fact it read off the row, including
    # ``venue``.
    OBSERVATION_HASH_INVALID = "OBSERVATION_HASH_INVALID"
    # Securities Follow-Up Temporal Integrity v1 (``FOLLOWUP_EVIDENCE_
    # QUALITY_POLICY_V2`` only).  The terminal evidence exists and is
    # genuine, but it is not the closing chain of the first eligible session
    # at or after the target -- a later session's price (hindsight), an
    # off-window capture, or (defensively) one captured before the target.
    TARGET_EVIDENCE_TEMPORALLY_INVALID = "TARGET_EVIDENCE_TEMPORALLY_INVALID"
    # The retained calendar evidence supplied does not bracket the target,
    # so the first eligible session cannot be determined -- fail closed.
    TARGET_SESSION_NOT_RESOLVABLE = "TARGET_SESSION_NOT_RESOLVABLE"


FOLLOWUP_EVIDENCE_ELIGIBLE_REASON = "BASELINE_AND_TARGET_EVIDENCE_CLASS_ALLOWLISTED"
FOLLOWUP_EVIDENCE_KALSHI_ALWAYS_ELIGIBLE_REASON = "KALSHI_OBSERVATION_ALWAYS_GENUINE_PUBLIC_READ"
# Securities Research -> Shared Opportunity Recurring Bridge v1.
FOLLOWUP_EVIDENCE_SECURITIES_ALWAYS_ELIGIBLE_REASON = (
    "SECURITIES_OBSERVATION_ALWAYS_GENUINE_PUBLIC_ALPACA_CAPTURE"
)
FOLLOWUP_EVIDENCE_BASELINE_INELIGIBLE_REASON = "BASELINE_EVIDENCE_CLASS_NOT_ALLOWLISTED"
FOLLOWUP_EVIDENCE_TARGET_INELIGIBLE_REASON = "TARGET_EVIDENCE_CLASS_NOT_ALLOWLISTED"
FOLLOWUP_EVIDENCE_BOTH_SIDES_INELIGIBLE_REASON = (
    "BASELINE_AND_TARGET_EVIDENCE_CLASS_NOT_ALLOWLISTED"
)
FOLLOWUP_EVIDENCE_LINEAGE_UNRESOLVABLE_REASON = "SOURCE_LINEAGE_NOT_RESOLVABLE_FROM_RETAINED_STATE"
FOLLOWUP_EVIDENCE_OBSERVATION_HASH_INVALID_REASON = (
    "OBSERVATION_CANONICAL_HASH_DOES_NOT_MATCH_ITS_OWN_RETAINED_FIELDS"
)


# ==========================================================================
# Securities Follow-Up Temporal Integrity v1 -- ``FOLLOWUP_EVIDENCE_QUALITY_
# POLICY_V2`` (SECURITIES only).
#
# A SECURITIES terminal payoff is "the structure's payoff at the underlying's
# *closing chain* for the first trading session at or after the target" --
# the only terminal fact the options capture model ever produces
# (``domain.options_research_operations.OptionsCaptureWindowPolicy``: one
# closing-chain capture per XNYS session, valid only inside
# ``[official close, close + freshness threshold]``).  The follow-up
# selector (``earliest_snapshot_for_underlying_at_or_after``) has no upper
# bound, so after any missed window it silently substitutes a *later*
# session's close -- during the 2026-09-05..09-21 outage, one ~16 days late.
#
# V2 therefore permits terminal evidence only if its ``captured_at`` lies in
# ``[max(window_open, target_time), window_close]`` of the first session
# whose capture window closes at or after ``target_time``.  The permitted
# lateness is thus derived from the session model, not a fixed duration: it
# spans a weekend or holiday only because no session exists to capture in
# between, and never extends to a second session.  A later snapshot is
# never accepted merely because it is the nearest one retained.
# ==========================================================================

SECURITIES_TERMINAL_EVIDENCE_TIMING_V1 = "SECURITIES_TERMINAL_EVIDENCE_TIMING_V1"


class SecuritiesTerminalTemporalStatus(StrEnum):
    WITHIN_FIRST_ELIGIBLE_CAPTURE_WINDOW = "WITHIN_FIRST_ELIGIBLE_CAPTURE_WINDOW"
    AFTER_FIRST_ELIGIBLE_CAPTURE_WINDOW = "AFTER_FIRST_ELIGIBLE_CAPTURE_WINDOW"
    # The target falls strictly after a session's close but inside its
    # post-close window, so that
    # session's own closing chain (captured before the target, while the
    # market was already shut) is the correct terminal fact -- but the
    # follow-up selector only ever returns a snapshot at/after the target,
    # so the one it retained is a later session's.  Still invalid; kept
    # distinct from outage lateness so it is never miscounted as one.
    TARGET_INSIDE_WINDOW_SAME_SESSION_CHAIN_NOT_SELECTED = (
        "TARGET_INSIDE_WINDOW_SAME_SESSION_CHAIN_NOT_SELECTED"
    )
    OUTSIDE_CAPTURE_WINDOW = "OUTSIDE_CAPTURE_WINDOW"
    CAPTURED_BEFORE_TARGET = "CAPTURED_BEFORE_TARGET"
    TERMINAL_SNAPSHOT_NOT_RESOLVABLE = "TERMINAL_SNAPSHOT_NOT_RESOLVABLE"
    TARGET_SESSION_NOT_RESOLVABLE = "TARGET_SESSION_NOT_RESOLVABLE"


FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_CONTEMPORANEOUS_REASON = (
    "SECURITIES_TERMINAL_SNAPSHOT_WITHIN_FIRST_ELIGIBLE_CAPTURE_WINDOW"
)
FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_LATE_REASON = (
    "SECURITIES_TERMINAL_SNAPSHOT_AFTER_FIRST_ELIGIBLE_CAPTURE_WINDOW"
)
FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_SAME_SESSION_NOT_SELECTED_REASON = (
    "SECURITIES_TARGET_INSIDE_CAPTURE_WINDOW_SAME_SESSION_CHAIN_NOT_SELECTED"
)
FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_OFF_WINDOW_REASON = (
    "SECURITIES_TERMINAL_SNAPSHOT_OUTSIDE_ANY_ELIGIBLE_CAPTURE_WINDOW"
)
FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_BEFORE_TARGET_REASON = (
    "SECURITIES_TERMINAL_SNAPSHOT_CAPTURED_BEFORE_TARGET"
)
FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_UNRESOLVABLE_REASON = (
    "SECURITIES_TERMINAL_SNAPSHOT_NOT_RESOLVABLE_FROM_RETAINED_STATE"
)
FOLLOWUP_EVIDENCE_SECURITIES_TARGET_SESSION_UNRESOLVABLE_REASON = (
    "SECURITIES_TARGET_SESSION_NOT_RESOLVABLE_FROM_SUPPLIED_CALENDAR"
)

_SECURITIES_TEMPORAL_VERDICTS: Mapping[
    SecuritiesTerminalTemporalStatus, tuple[str, FollowUpEvidenceDefectType]
] = MappingProxyType(
    {
        SecuritiesTerminalTemporalStatus.WITHIN_FIRST_ELIGIBLE_CAPTURE_WINDOW: (
            FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_CONTEMPORANEOUS_REASON,
            FollowUpEvidenceDefectType.NONE,
        ),
        SecuritiesTerminalTemporalStatus.AFTER_FIRST_ELIGIBLE_CAPTURE_WINDOW: (
            FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_LATE_REASON,
            FollowUpEvidenceDefectType.TARGET_EVIDENCE_TEMPORALLY_INVALID,
        ),
        SecuritiesTerminalTemporalStatus.TARGET_INSIDE_WINDOW_SAME_SESSION_CHAIN_NOT_SELECTED: (
            FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_SAME_SESSION_NOT_SELECTED_REASON,
            FollowUpEvidenceDefectType.TARGET_EVIDENCE_TEMPORALLY_INVALID,
        ),
        SecuritiesTerminalTemporalStatus.OUTSIDE_CAPTURE_WINDOW: (
            FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_OFF_WINDOW_REASON,
            FollowUpEvidenceDefectType.TARGET_EVIDENCE_TEMPORALLY_INVALID,
        ),
        SecuritiesTerminalTemporalStatus.CAPTURED_BEFORE_TARGET: (
            FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_BEFORE_TARGET_REASON,
            FollowUpEvidenceDefectType.TARGET_EVIDENCE_TEMPORALLY_INVALID,
        ),
        SecuritiesTerminalTemporalStatus.TERMINAL_SNAPSHOT_NOT_RESOLVABLE: (
            FOLLOWUP_EVIDENCE_SECURITIES_TERMINAL_UNRESOLVABLE_REASON,
            FollowUpEvidenceDefectType.LINEAGE_NOT_RESOLVABLE,
        ),
        SecuritiesTerminalTemporalStatus.TARGET_SESSION_NOT_RESOLVABLE: (
            FOLLOWUP_EVIDENCE_SECURITIES_TARGET_SESSION_UNRESOLVABLE_REASON,
            FollowUpEvidenceDefectType.TARGET_SESSION_NOT_RESOLVABLE,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class SecuritiesTerminalEvidenceTiming:
    """Every fact needed to explain, and independently re-check, a V2
    temporal verdict: the target, the exact terminal snapshot and its
    capture instant, the first eligible session and its permitted window,
    and how late the evidence was.  Retained on the assessment itself
    (never on the immutable observation)."""

    status: SecuritiesTerminalTemporalStatus
    target_time: datetime
    terminal_snapshot_id: str | None
    terminal_captured_at: datetime | None
    calendar_identity: str
    # The reviewed calendar build that produced ``eligible_*`` (e.g.
    # ``pandas-market-calendars==5.2.0``), so a verdict records exactly which
    # calendar it was derived from.  ``None`` only when no session bracketed
    # the target.
    calendar_version: str | None
    capture_window_policy_id: str
    eligible_session_date: str | None
    eligible_window_open_at: datetime | None
    eligible_window_close_at: datetime | None
    # ``terminal_captured_at - target_time`` in whole seconds (floor), and
    # how far past the permitted window's close the capture was, rounded
    # *up* (so any overrun, however small, is at least ``1``; ``0`` only
    # when not past it).  ``None`` when the underlying fact is unavailable.
    lateness_seconds: int | None
    seconds_beyond_permitted_window: int | None


def securities_terminal_snapshot_id(observation: DecisionFollowUpObservation) -> str | None:
    """The terminal side of a SECURITIES observation's ``source_lineage``
    (``_lineage(baseline_snapshot_id, target_snapshot_id)``: the last
    element; a one-element lineage means the terminal snapshot *is* the
    decision-time one).  ``None`` for an empty lineage."""

    return observation.source_lineage[-1] if observation.source_lineage else None


def _whole_seconds(delta: timedelta) -> int:
    return delta // timedelta(seconds=1)


def _whole_seconds_ceiling(delta: timedelta) -> int:
    return -((-delta) // timedelta(seconds=1))


def resolve_securities_terminal_evidence_timing(
    *,
    target_time: datetime,
    terminal_snapshot_id: str | None,
    terminal_captured_at: datetime | None,
    sessions: tuple[SecuritiesCalendarSession, ...],
    window_policy: OptionsCaptureWindowPolicy,
) -> SecuritiesTerminalEvidenceTiming:
    """Pure, deterministic.  ``sessions`` must be a contiguous run of the
    reviewed calendar that brackets ``target_time`` (at least one session
    whose close is before it and one whose capture window closes at or after
    it) -- otherwise ``TARGET_SESSION_NOT_RESOLVABLE``, never a guess.

    Uses only the snapshot's own ``captured_at`` (when Apex actually
    fetched the chain) -- never ``observed_at`` (when the follow-up sync
    ran) and never any later re-capture: a snapshot fetched after the
    permitted window is late no matter which session's label a later reader
    might want to attach to it."""

    if target_time.tzinfo is None:
        raise CounterfactualFollowUpError("SECURITIES_FOLLOWUP_TIMESTAMP_NOT_AWARE")
    if terminal_captured_at is not None and terminal_captured_at.tzinfo is None:
        raise CounterfactualFollowUpError("SECURITIES_FOLLOWUP_TIMESTAMP_NOT_AWARE")
    ordered = tuple(
        sorted(
            (
                item
                for item in sessions
                if item.calendar_identity == window_policy.calendar_identity
            ),
            key=lambda item: item.closes_at,
        )
    )
    windows = tuple((item, window_policy.window_for_session(item)) for item in ordered)
    bracketed_before = any(window[1] < target_time for _, window in windows)
    eligible = next(((item, window) for item, window in windows if window[1] >= target_time), None)

    lateness = (
        None if terminal_captured_at is None else _whole_seconds(terminal_captured_at - target_time)
    )
    session_date: str | None = None
    calendar_version: str | None = None
    window_open: datetime | None = None
    window_close: datetime | None = None
    beyond: int | None = None
    if eligible is not None and bracketed_before:
        session_date = eligible[0].session_date
        calendar_version = eligible[0].calendar_version
        window_open, window_close = eligible[1]
        if terminal_captured_at is not None:
            beyond = max(0, _whole_seconds_ceiling(terminal_captured_at - window_close))

    if terminal_snapshot_id is None or terminal_captured_at is None:
        status = SecuritiesTerminalTemporalStatus.TERMINAL_SNAPSHOT_NOT_RESOLVABLE
    elif window_open is None or window_close is None:
        status = SecuritiesTerminalTemporalStatus.TARGET_SESSION_NOT_RESOLVABLE
    elif terminal_captured_at < target_time:
        status = SecuritiesTerminalTemporalStatus.CAPTURED_BEFORE_TARGET
    elif terminal_captured_at > window_close and target_time > window_open:
        status = (
            SecuritiesTerminalTemporalStatus.TARGET_INSIDE_WINDOW_SAME_SESSION_CHAIN_NOT_SELECTED
        )
    elif terminal_captured_at > window_close:
        status = SecuritiesTerminalTemporalStatus.AFTER_FIRST_ELIGIBLE_CAPTURE_WINDOW
    elif terminal_captured_at < window_open:
        status = SecuritiesTerminalTemporalStatus.OUTSIDE_CAPTURE_WINDOW
    else:
        status = SecuritiesTerminalTemporalStatus.WITHIN_FIRST_ELIGIBLE_CAPTURE_WINDOW
    return SecuritiesTerminalEvidenceTiming(
        status=status,
        target_time=target_time,
        terminal_snapshot_id=terminal_snapshot_id,
        terminal_captured_at=terminal_captured_at,
        calendar_identity=window_policy.calendar_identity,
        calendar_version=calendar_version,
        capture_window_policy_id=window_policy.policy_id,
        eligible_session_date=session_date,
        eligible_window_open_at=window_open,
        eligible_window_close_at=window_close,
        lateness_seconds=lateness,
        seconds_beyond_permitted_window=beyond,
    )


def _timing_hash_payload(timing: SecuritiesTerminalEvidenceTiming) -> tuple[object, ...]:
    return (
        SECURITIES_TERMINAL_EVIDENCE_TIMING_V1,
        timing.status,
        timing.target_time,
        timing.terminal_snapshot_id,
        timing.terminal_captured_at,
        timing.calendar_identity,
        timing.calendar_version,
        timing.capture_window_policy_id,
        timing.eligible_session_date,
        timing.eligible_window_open_at,
        timing.eligible_window_close_at,
        timing.lateness_seconds,
        timing.seconds_beyond_permitted_window,
    )


@dataclass(frozen=True, slots=True)
class FollowUpEvidenceQualityAssessment:
    """Immutable, additive verdict about one already-retained
    ``DecisionFollowUpObservation``'s evidence lineage -- never a rewrite of
    that observation (see this section's own module-level docstring).

    ``baseline_evidence_class``/``target_evidence_class`` here are the
    assessment's own *independently resolved* facts (via ``source_lineage``
    joined back against retained ``crypto_product_observation`` batches at
    assessment time) -- deliberately not merely copied from the assessed
    observation's own, possibly-``None`` (legacy-row), dataclass fields of
    the same name.  This is what makes retroactive assessment of a
    pre-remediation row (e.g. the confirmed DOGE-USD incident, retained
    before ``baseline_evidence_class`` existed as a concept at all) possible
    without ever touching the observation itself."""

    assessment_id: str
    observation_id: str
    observation_canonical_hash: str
    assessment_policy_version: str
    baseline_evidence_class: EvidenceClass | None
    target_evidence_class: EvidenceClass | None
    verdict: FollowUpEvidenceQualityVerdict
    reason_code: str
    evidence_defect_type: FollowUpEvidenceDefectType
    assessed_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    # Securities Follow-Up Temporal Integrity v1: present on every
    # ``FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2`` assessment, ``None`` on every
    # V1 assessment (whose canonical hash therefore stays byte-identical).
    securities_terminal_timing: SecuritiesTerminalEvidenceTiming | None = None


def evidence_quality_assessment_id(*, observation_id: str, assessment_policy_version: str) -> str:
    """Stable per ``(observation, assessment policy version)``; independent
    of ``assessed_at`` so a genuine idempotent re-assessment always
    reproduces the identical ``assessment_id`` -- mirrors ``domain.
    counterfactual_classification.classification_identity`` exactly."""

    return deterministic_id(
        "followup_evidence_quality_assessment",
        (FOLLOWUP_EVIDENCE_QUALITY_ASSESSMENT_V1, observation_id, assessment_policy_version),
    )


def evidence_quality_assessment_canonical_hash(
    *,
    assessment_id: str,
    observation_id: str,
    observation_canonical_hash: str,
    assessment_policy_version: str,
    baseline_evidence_class: EvidenceClass | str | None,
    target_evidence_class: EvidenceClass | str | None,
    verdict: FollowUpEvidenceQualityVerdict | str,
    reason_code: str,
    evidence_defect_type: FollowUpEvidenceDefectType | str,
    securities_terminal_timing: SecuritiesTerminalEvidenceTiming | None = None,
) -> str:
    """Deliberately excludes ``assessed_at``: two assessment runs of the
    identical observation under the identical assessment policy must always
    be recognized as the identical fact, no matter how much wall-clock time
    separates them -- mirrors ``domain.counterfactual_classification.
    classification_canonical_hash`` exactly.  Exposed (not
    underscore-prefixed) so persistence/migration code can recompute it
    exactly, matching every sibling module's own convention."""

    payload = (
        FOLLOWUP_EVIDENCE_QUALITY_ASSESSMENT_V1,
        assessment_id,
        observation_id,
        observation_canonical_hash,
        assessment_policy_version,
        baseline_evidence_class,
        target_evidence_class,
        verdict,
        reason_code,
        evidence_defect_type,
        "NONE",
    )
    if securities_terminal_timing is not None:
        # Appended only when present, so every V1 assessment's hash is
        # exactly the formula it was retained under.
        return content_digest((*payload, _timing_hash_payload(securities_terminal_timing)))
    return content_digest(payload)


def observation_canonical_hash_matches(observation: DecisionFollowUpObservation) -> bool:
    """Recomputes ``observation``'s own canonical hash from its own retained
    fields and compares it to ``observation.canonical_hash`` -- the same
    "recompute from retained state, never merely trust a stored column"
    discipline ``PostgresCounterfactualFollowUpRepository.save_observation``'s
    own conflict-detection already applies at *write* time, now available as
    a *read*-time integrity check too (Part: Section 6, "checked on read or
    creation, not merely copied without verification").  Pure; performs no
    I/O -- every input is already a field on ``observation`` itself, exactly
    the same set of fields ``build_crypto_price_movement_observation``/
    ``build_kalshi_market_status_observation`` fed into ``followup_
    observation_canonical_hash`` when they first computed it, so a genuine,
    untampered row always matches by construction.  Only used, today, by
    ``assess_followup_evidence_quality`` below -- but exposed (not
    underscore-prefixed) because it is a generally useful integrity check on
    its own, matching this module's own convention for every other
    recompute-and-verify primitive."""

    recomputed = followup_observation_canonical_hash(
        observation_id=observation.observation_id,
        decision_id=observation.decision_id,
        decision_canonical_hash=observation.decision_canonical_hash,
        venue=observation.venue,
        disposition=observation.disposition,
        policy_version=observation.policy_version,
        horizon_kind=observation.horizon_kind,
        horizon_days=observation.horizon_days,
        decision_evaluated_at=observation.decision_evaluated_at,
        target_time=observation.target_time,
        observation_type=observation.observation_type,
        subject_instrument_id=observation.subject_instrument_id,
        subject_product_or_market_id=observation.subject_product_or_market_id,
        provenance=observation.provenance,
        source_lineage=observation.source_lineage,
        data_limitations=observation.data_limitations,
        reference_price_at_baseline=observation.reference_price_at_baseline,
        reference_price_at_target=observation.reference_price_at_target,
        subsequent_return=observation.subsequent_return,
        market_status_at_target=observation.market_status_at_target,
        yes_bid_at_target=observation.yes_bid_at_target,
        yes_ask_at_target=observation.yes_ask_at_target,
        settlement_result=observation.settlement_result,
        settlement_source=observation.settlement_source,
        baseline_kind=observation.baseline_kind,
        baseline_time=observation.baseline_time,
        baseline_evidence_class=observation.baseline_evidence_class,
        evidence_eligibility_policy_version=observation.evidence_eligibility_policy_version,
        # Counterfactual Forward Outcome Temporal Integrity v1.  Omitting
        # these would make every genuine ``FOLLOWUP_POLICY_V3`` row fail its
        # own integrity check and be permanently, wrongly assessed
        # ``INELIGIBLE_EVIDENCE``/``OBSERVATION_HASH_INVALID`` -- the exact
        # shape of defect this milestone's own recurring-cycle regression
        # test caught before any V3 row was ever synced.  This function must
        # feed ``followup_observation_canonical_hash`` the *complete* field
        # set the builder fed it, never a subset that happens to be enough
        # for the versions that existed when it was written.
        measurement_as_of=observation.measurement_as_of,
        policy_anchor_at=observation.policy_anchor_at,
        decision_input_reference_price=observation.decision_input_reference_price,
        decision_input_reference_bucket_start=observation.decision_input_reference_bucket_start,
        decision_input_source_observation_id=observation.decision_input_source_observation_id,
        decision_input_evidence_class=observation.decision_input_evidence_class,
    )
    return recomputed == observation.canonical_hash


def assess_followup_evidence_quality(
    *,
    observation: DecisionFollowUpObservation,
    baseline_evidence_class: EvidenceClass | None,
    target_evidence_class: EvidenceClass | None,
    lineage_resolved: bool,
    assessed_at: datetime,
    assessment_policy_version: str = FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    securities_terminal_timing: SecuritiesTerminalEvidenceTiming | None = None,
) -> FollowUpEvidenceQualityAssessment:
    """Pure; performs no I/O.  The caller has already resolved both sides'
    evidence class from retained state (Part: "reference, don't duplicate"
    -- this function never reads a repository itself; see
    ``application.followup_evidence_quality`` / ``adapters.
    counterfactual_followup_postgres`` for the actual ``source_lineage`` ->
    ``crypto_product_observation.evidence_class`` resolution).

    ``lineage_resolved=False`` (CRYPTO only -- see below) means the caller
    could not establish one or both sides' source batch from retained state
    at all (e.g. a ``source_lineage`` id no longer resolves to any retained
    ``crypto_product_observation`` row).  This fails closed exactly like an
    ineligible evidence class would -- an unprovable lineage is never
    treated as eligible merely because it could not be checked (Part:
    fail-closed; census Part: "observations whose lineage cannot be proven
    from retained state").

    KALSHI observations never consult ``lineage_resolved`` or either
    evidence-class parameter -- they are always ``ELIGIBLE`` by construction
    (Part: this section's own module-level docstring). SECURITIES
    observations (Securities Research -> Shared Opportunity Recurring
    Bridge v1) are the same shape for the identical reason: ``domain.
    counterfactual_followup.build_securities_structure_terminal_observation``
    always stamps both ``baseline_evidence_class``/``target_evidence_class``
    ``EvidenceClass.GENUINE_PUBLIC`` unconditionally (the real, live-
    credentialed Alpaca options-chain evidence this bridge's only producer
    ever reads), so there is no separate lineage-resolution question to ask
    for it either -- unlike CRYPTO, which genuinely can retain a
    ``FIXTURE``-contaminated batch and therefore must have its lineage
    independently re-derived and checked. Found and fixed while adding this
    venue: this dispatch previously fell through any non-KALSHI venue into
    the CRYPTO-only ``resolve_observation_evidence_lineage`` path, which
    would have silently misclassified every genuine SECURITIES observation
    ``INELIGIBLE_EVIDENCE`` (its ``source_lineage`` holds options snapshot
    ids, never found among retained ``crypto_product_observation`` batches)
    -- not a crash, but a permanent, wrong verdict on genuinely good
    evidence. Every observation, all three venues, is first checked against
    ``observation_canonical_hash_matches`` (Part: Section 6): a hash that
    does not match its own retained fields means this function can no
    longer trust *any* fact it read off the row, including ``venue``, so
    that check precedes and overrides both short-circuits.

    ``FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2`` (Securities Follow-Up Temporal
    Integrity v1) is SECURITIES-only: it replaces V1's unconditional
    SECURITIES ``ELIGIBLE`` with the temporal verdict carried by
    ``securities_terminal_timing`` (required, resolved by the caller via
    ``resolve_securities_terminal_evidence_timing``).  Calling V2 for any
    other venue, or V1 with a timing record, is a caller bug and raises."""

    if assessment_policy_version not in KNOWN_FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSIONS:
        raise CounterfactualFollowUpError("FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION_UNKNOWN")
    is_v2 = assessment_policy_version == FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2
    if is_v2:
        if observation.venue is not SharedVenue.SECURITIES:
            raise CounterfactualFollowUpError(
                "FOLLOWUP_EVIDENCE_QUALITY_POLICY_VERSION_UNSUPPORTED_FOR_VENUE"
            )
        if securities_terminal_timing is None:
            raise CounterfactualFollowUpError(
                "FOLLOWUP_EVIDENCE_QUALITY_V2_REQUIRES_SECURITIES_TERMINAL_TIMING"
            )
        if securities_terminal_timing.target_time != observation.target_time:
            raise CounterfactualFollowUpError("FOLLOWUP_EVIDENCE_QUALITY_V2_TIMING_TARGET_MISMATCH")
    elif securities_terminal_timing is not None:
        raise CounterfactualFollowUpError(
            "FOLLOWUP_EVIDENCE_QUALITY_V1_DOES_NOT_ACCEPT_SECURITIES_TERMINAL_TIMING"
        )

    if not observation_canonical_hash_matches(observation):
        verdict = FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
        reason_code = FOLLOWUP_EVIDENCE_OBSERVATION_HASH_INVALID_REASON
        defect_type = FollowUpEvidenceDefectType.OBSERVATION_HASH_INVALID
    elif observation.venue is SharedVenue.KALSHI:
        verdict = FollowUpEvidenceQualityVerdict.ELIGIBLE
        reason_code = FOLLOWUP_EVIDENCE_KALSHI_ALWAYS_ELIGIBLE_REASON
        defect_type = FollowUpEvidenceDefectType.NONE
    elif is_v2:
        assert securities_terminal_timing is not None  # checked above
        reason_code, defect_type = _SECURITIES_TEMPORAL_VERDICTS[securities_terminal_timing.status]
        verdict = (
            FollowUpEvidenceQualityVerdict.ELIGIBLE
            if defect_type is FollowUpEvidenceDefectType.NONE
            else FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
        )
    elif observation.venue is SharedVenue.SECURITIES:
        verdict = FollowUpEvidenceQualityVerdict.ELIGIBLE
        reason_code = FOLLOWUP_EVIDENCE_SECURITIES_ALWAYS_ELIGIBLE_REASON
        defect_type = FollowUpEvidenceDefectType.NONE
    elif not lineage_resolved:
        verdict = FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
        reason_code = FOLLOWUP_EVIDENCE_LINEAGE_UNRESOLVABLE_REASON
        defect_type = FollowUpEvidenceDefectType.LINEAGE_NOT_RESOLVABLE
    else:
        # ``is not None and ...`` also fails closed defensively if a caller
        # bug ever passes ``lineage_resolved=True`` alongside a ``None``
        # evidence class -- never crashes, never treated as eligible.
        baseline_eligible = baseline_evidence_class is not None and is_followup_evidence_eligible(
            baseline_evidence_class
        )
        target_eligible = target_evidence_class is not None and is_followup_evidence_eligible(
            target_evidence_class
        )
        if baseline_eligible and target_eligible:
            verdict = FollowUpEvidenceQualityVerdict.ELIGIBLE
            reason_code = FOLLOWUP_EVIDENCE_ELIGIBLE_REASON
            defect_type = FollowUpEvidenceDefectType.NONE
        elif not baseline_eligible and not target_eligible:
            verdict = FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
            reason_code = FOLLOWUP_EVIDENCE_BOTH_SIDES_INELIGIBLE_REASON
            defect_type = FollowUpEvidenceDefectType.BOTH_SIDES_EVIDENCE_INELIGIBLE
        elif not baseline_eligible:
            verdict = FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
            reason_code = FOLLOWUP_EVIDENCE_BASELINE_INELIGIBLE_REASON
            defect_type = FollowUpEvidenceDefectType.BASELINE_EVIDENCE_INELIGIBLE
        else:
            verdict = FollowUpEvidenceQualityVerdict.INELIGIBLE_EVIDENCE
            reason_code = FOLLOWUP_EVIDENCE_TARGET_INELIGIBLE_REASON
            defect_type = FollowUpEvidenceDefectType.TARGET_EVIDENCE_INELIGIBLE
    assessment_id = evidence_quality_assessment_id(
        observation_id=observation.observation_id,
        assessment_policy_version=assessment_policy_version,
    )
    canonical_hash = evidence_quality_assessment_canonical_hash(
        assessment_id=assessment_id,
        observation_id=observation.observation_id,
        observation_canonical_hash=observation.canonical_hash,
        assessment_policy_version=assessment_policy_version,
        baseline_evidence_class=baseline_evidence_class,
        target_evidence_class=target_evidence_class,
        verdict=verdict,
        reason_code=reason_code,
        evidence_defect_type=defect_type,
        securities_terminal_timing=securities_terminal_timing,
    )
    return FollowUpEvidenceQualityAssessment(
        assessment_id=assessment_id,
        observation_id=observation.observation_id,
        observation_canonical_hash=observation.canonical_hash,
        assessment_policy_version=assessment_policy_version,
        baseline_evidence_class=baseline_evidence_class,
        target_evidence_class=target_evidence_class,
        verdict=verdict,
        reason_code=reason_code,
        evidence_defect_type=defect_type,
        assessed_at=assessed_at,
        canonical_hash=canonical_hash,
        authority="NONE",
        securities_terminal_timing=securities_terminal_timing,
    )


# ==========================================================================
# Downstream fail-closed eligibility contract (final-review addition, same
# milestone).  No attribution/research-conclusion consumer of retained
# follow-up evidence exists in this repository yet -- but the rule such a
# consumer must obey is defined and centralized *now*, not deferred until
# one is built, exactly as this milestone's own final review required.
#
# Required (preferred) safety invariant: only explicitly ``ELIGIBLE``
# assessed follow-up evidence may ever enter future attribution/conclusion
# logic.  This is the strict transition rule, chosen deliberately over a
# looser one that would grandfather in a retained observation merely because
# it predates the assessment system entirely: ``UNASSESSED`` (no assessment
# row yet -- either nobody has run ``followup-evidence-quality-assess``
# since this observation was retained, or it predates the concept
# completely) is treated identically to a proven ``INELIGIBLE_EVIDENCE``
# verdict -- both are simply "not affirmatively proven clean" from a
# downstream consumer's point of view, and the confirmed DOGE-USD incident
# is exactly what admitting "not yet proven" evidence by default would have
# continued to risk.  A legacy row becomes eligible the same way every other
# row does: a genuine, explicit ``ELIGIBLE`` verdict, never by age or
# absence of contrary evidence.
# ==========================================================================


def is_evidence_eligible_for_downstream_attribution(
    assessment: FollowUpEvidenceQualityAssessment | None,
) -> bool:
    """The one centralized fail-closed gate every future attribution/
    research-conclusion consumer of retained follow-up evidence must call
    before treating a ``DecisionFollowUpObservation`` as usable input --
    never re-implemented ad hoc at each call site (Part: this section's own
    module-level docstring for the Required Invariant this enforces).

    ``assessment is None`` means ``UNASSESSED`` (Part: mirrors ``application.
    operator_console_evidence``'s own read-model idiom: an absence, not a
    verdict) and is fail-closed ineligible, exactly like an explicit
    ``INELIGIBLE_EVIDENCE`` verdict -- there is no legacy-row carve-out and
    none is needed: a pre-remediation row with no assessment yet is simply
    ``UNASSESSED`` like any other unassessed row, and becomes eligible only
    once genuinely assessed ``ELIGIBLE``, exactly like every other row.  Pure;
    performs no I/O -- the caller supplies whatever ``FollowUpEvidenceQuality
    Assessment`` (or ``None``) it already read for this observation under
    whichever ``assessment_policy_version`` it considers current."""

    if assessment is None:
        return False
    return assessment.verdict is FollowUpEvidenceQualityVerdict.ELIGIBLE
