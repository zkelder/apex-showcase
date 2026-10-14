"""Counterfactual Outcome Feedback v1.

This module owns no provider client, credential, network, execution, or
capital-mutation capability, and it introduces no new investment judgment.
It is the aggregate-evidence layer above the already-retained Counterfactual
Decision Ledger (``domain.counterfactual_ledger``), its analytical
classification (``domain.counterfactual_classification``), and its
follow-up observation / evidence-quality subsystem
(``domain.counterfactual_followup``):

    CounterfactualDecision
    -> DecisionFollowUpObservation
    -> FollowupEvidenceQualityAssessment
    -> (this module) versioned aggregate outcome feedback
    -> Research Operations fact
    -> ResearchAgendaItem
    -> human-reviewed ResearchProposal

What this module produces, and what it deliberately does not:

* It produces only **aggregate** evidence over a bounded, explicitly
  partitioned cohort of decisions -- never a per-decision verdict. A sound
  decision may have a bad stochastic outcome and an unsound decision may
  get lucky; nothing here ever labels one retained ``CounterfactualDecision``
  "correct", "wrong", "good", or "bad" from its realized outcome.
* It does not tune a strategy, adjust a parameter, mutate a mandate,
  mutate an allocation, change execution/PAPER/LIVE authority, feed
  ``prioritize()`` or any other ranking input, or train anything. Every
  record this module produces carries ``authority = "NONE"``.
* It computes nothing from scratch: eligible economic evidence is exactly
  the evidence that already passes the existing, centralized
  ``is_evidence_eligible_for_downstream_attribution`` gate
  (``domain.counterfactual_followup``) -- that gate's logic is never
  reproduced or re-implemented here, only called.

Policy identifier: ``COUNTERFACTUAL_OUTCOME_POLICY_V1``. Frozen, exactly
like ``REASON_CLASSIFIER_V1``/``TOTAL_MARKET_CONTEXT_POLICY_V1`` before it:
a later semantic correction becomes an additive ``COUNTERFACTUAL_OUTCOME_
POLICY_V2`` sitting beside this one, never a mutation of an already-retained
aggregate's meaning. Every ``CounterfactualOutcomeAggregate`` this module
builds is pure and deterministic from already-immutable/already-versioned
retained inputs: two runs over byte-identical decisions, classifications,
follow-up observations, and evidence-quality assessments produce
byte-identical ``canonical_hash`` values (``generated_at`` -- a run's own
wall-clock stamp -- is deliberately excluded from the hash, mirroring
``CounterfactualDecisionClassification``'s own exclusion of
``classified_at``, so a genuine crash-resume replay under the same
``run_id`` but a different wall-clock instant never manufactures a spurious
identity conflict).

Partitioning and correlation discipline (Phase 2/3): an aggregate is
identified by a **cohort** -- ``(outcome_policy_version, venue,
strategy_or_method_id, reason_family, horizon_kind, baseline_kind,
followup_policy_version, classifier_version, evidence_quality_policy_
version)`` -- never coarser. Three concrete, evidence-based incompatibility
risks are handled by this partitioning, not merely asserted:

1. **Baseline-kind correlation.** CRYPTO can retain both an
   ``ORIGIN_EVIDENCE`` and a ``DECISION_TIME`` observation for the same
   decision/horizon -- two real, structurally different measurements of the
   same underlying decision, not two independent samples. Partitioning by
   ``baseline_kind`` means these are never summed into one cell.
2. **Follow-up policy-version correlation.** Confirmed against the real
   retained corpus (not assumed): ``FOLLOWUP_POLICY_V1`` and ``..._V2``
   rows coexist for the identical ``(venue, horizon_kind, baseline_kind)``
   today. ``FOLLOWUP_POLICY_V1``'s ``DECISION_TIME`` baseline used a
   hindsight-leaking exact-bucket close; ``..._V2``'s uses an
   availability-gated ``latest_close_before`` read (Counterfactual Crypto
   Follow-Up Point-in-Time Semantics v2). These are not the same economic
   quantity even though they share a ``baseline_kind`` label, so
   ``followup_policy_version`` is a mandatory partition dimension alongside
   the five the audit named explicitly (venue, strategy_or_method_id,
   reason_family, horizon_kind, baseline_kind) -- discovered by direct
   inspection of the live retained corpus during this milestone, not
   inherited from the audit's own list.
3. **Reason-family fan-out is not double counting.** A decision's raw
   reason codes can classify into more than one ``ReasonFamily``
   (``classify_all`` returns a tuple). Such a decision legitimately
   contributes to more than one *cohort* (once per family) -- this is
   correct, not a duplicate: it is not counted twice *within* any single
   cohort, only once each in the distinct cohorts its own reasons actually
   named.

What this module does **not** attempt to solve: asset-level/temporal
autocorrelation between *different* decisions about the same or a related
underlying instrument (e.g., the same asset rejected on consecutive days)
is a real, disclosed limitation -- ``explanation`` says so on every
aggregate -- not a statistic this module corrects for. Doing so would mean
importing PSR/DSR/effective-trial/CSCV-PBO machinery from
``domain.research_integrity``/``domain.research_calibration`` into
crypto/Kalshi, which is explicitly out of scope for this milestone.

Kalshi (Phase 8): investigated and confirmed absent, not assumed absent.
``DecisionFollowUpObservation`` retains a Kalshi follow-up's *target*-time
market status/settlement (``market_status_at_target``, ``yes_bid_at_target``,
``yes_ask_at_target``, ``settlement_result``) but -- because Apex retains no
historical Kalshi quote archive -- no *baseline*-time entry price or
probability at all (``reference_price_at_baseline`` is always ``None`` for
KALSHI, by construction; this is the same reason ``ORIGIN_EVIDENCE`` does
not exist for KALSHI baselines). A settlement result alone, without an entry
price, yields at best a binary "would this direction have resolved
correctly" fact -- not an economic return (a market trading at 0.95 YES
that resolves YES is a trivial win; one trading at 0.05 YES that resolves
YES is a large one; treating both as the same "+1" is not "economically
defensible"). No deterministic settlement -> return reduction follows from
already-retained evidence, so this module ships an explicit, honest
``VENUE_ECONOMIC_REDUCTION_UNAVAILABLE`` qualification state for KALSHI
cohorts rather than fabricating one; KALSHI evidence-quality/maturity
counts are still retained honestly (they do not require a price), only the
return-derived statistics stay ``None``. The prerequisite for a future
milestone is explicit: retaining a baseline-time Kalshi quote (an
analogue of CRYPTO's ``reference_price_at_baseline``/``ORIGIN_EVIDENCE``).

Kalshi Decision-Linked Baseline Quote Evidence v1 closes that prerequisite
for exactly one Kalshi method, additively. ``COUNTERFACTUAL_OUTCOME_POLICY_V1``
is frozen -- every already-retained V1 aggregate, and V1's own venue-only
gate (``_ECONOMICALLY_QUANTIFIED_VENUES``), stays byte-for-byte unchanged
forever; this module never rewrites Counterfactual Outcome Feedback merely
to force immediate Kalshi qualification. ``COUNTERFACTUAL_OUTCOME_POLICY_V2``
sits beside it: for a KALSHI decision sourced from a
``BinaryComplementEvaluation`` with a retained ``KalshiBaselineQuoteEvidence``
row, ``reduce_kalshi_binary_complement_outcome`` (``domain.
kalshi_baseline_quote``) can now compute a genuine, deterministic economic
return from the exact retained decision-time acquisition cost and the
exact retained target-time settlement result -- the missing entry price
this docstring's own Kalshi paragraph named as the prerequisite. Every
other Kalshi method (``ThresholdBundleEvaluation``, unsupported source
record types) stays ``VENUE_ECONOMIC_REDUCTION_UNAVAILABLE`` even under
V2 -- see ``domain.kalshi_baseline_quote``'s own "method isolation"
discussion for exactly why threshold-ladder's dual-market settlement is
not yet a defensible reduction. ``outcome_policy_version`` was already a
mandatory cohort-partition dimension before this milestone (Phase 2/3's own
partitioning discipline), so a V2 aggregate for a cohort V1 already computed
is always a new, additional, sibling row -- never a mutation or replacement
of the V1 row for that same cohort.

Counterfactual Outcome Evidence Specificity v3 adds
``COUNTERFACTUAL_OUTCOME_POLICY_V3`` beside both, CRYPTO-only: canonical
single-family attribution, a same-method/same-origin gate-complement spread
per exact economic window, and qualification counted in independent
(fixed-phase-grid, complete-coverage) windows against the unchanged floor of
``STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES``.  It exists because V1's
sample unit -- one observation per asset per decision day per satisfied
family, over overlapping windows -- is not an independent sample of anything
(recorded as ``DEFECTIVE_OUTCOME_POLICY_VERSIONS``).  See the "Specificity
v3" section below for the full contract; V1 and V2 are byte-for-byte
unchanged.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from types import MappingProxyType

from apex.domain.counterfactual_classification import (
    CURRENT_CLASSIFIER_VERSION,
    KNOWN_CLASSIFIER_VERSIONS,
    CounterfactualDecisionClassification,
    ReasonFamily,
)
from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_POLICY_V3,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpEvidenceQualityAssessment,
    FollowUpHorizonKind,
    first_daily_close_boundary_at_or_after,
    followup_policy_version_defect,
    is_evidence_eligible_for_downstream_attribution,
)
from apex.domain.counterfactual_ledger import CounterfactualDecision
from apex.domain.kalshi_baseline_quote import (
    BINARY_COMPLEMENT_SOURCE_RECORD_TYPE,
    KalshiBaselineQuoteEvidence,
    reduce_kalshi_binary_complement_outcome,
)
from apex.domain.shared_opportunity import STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES, SharedVenue
from apex.domain.values import canonical_timestamp, content_digest, deterministic_id

COUNTERFACTUAL_OUTCOME_POLICY_V1 = "COUNTERFACTUAL_OUTCOME_POLICY_V1"
# Kalshi Decision-Linked Baseline Quote Evidence v1: additive, exactly like
# ``REASON_CLASSIFIER_V1``/``..._V2`` and ``FOLLOWUP_POLICY_V1``/``..._V2``
# before it -- see this module's own module-level docstring paragraph below
# for why V1's already-retained aggregates and already-established KALSHI
# behavior stay completely untouched, and ``_decision_is_economically_
# reducible`` for the one place V1 vs V2 actually differ.
COUNTERFACTUAL_OUTCOME_POLICY_V2 = "COUNTERFACTUAL_OUTCOME_POLICY_V2"
# Counterfactual Outcome Evidence Specificity v3: CRYPTO-only, additive
# beside the frozen V1 and V2 exactly as V2 sits beside V1 -- see the
# "Specificity v3" section below for the contract, and
# ``_cohort_venue_is_in_scope_for_policy_version`` for the scope gate.
COUNTERFACTUAL_OUTCOME_POLICY_V3 = "COUNTERFACTUAL_OUTCOME_POLICY_V3"
KNOWN_COUNTERFACTUAL_OUTCOME_POLICY_VERSIONS = (
    COUNTERFACTUAL_OUTCOME_POLICY_V1,
    COUNTERFACTUAL_OUTCOME_POLICY_V2,
    COUNTERFACTUAL_OUTCOME_POLICY_V3,
)
COUNTERFACTUAL_OUTCOME_WARNING = (
    "COUNTERFACTUAL OUTCOME FEEDBACK — AGGREGATE EVIDENCE ONLY — "
    "NO PER-DECISION VERDICT — NOT HINDSIGHT JUDGMENT — "
    "NO ALLOCATION, MANDATE, RANKING, OR EXECUTION AUTHORITY"
)

# Reuses, rather than reinvents, the one existing minimum-sample floor
# ``STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES`` (= 30) already established
# by crypto walk-forward calibration (``domain.crypto_universe.
# RELATIVE_STRENGTH_MINIMUM_SAMPLES``, the same value under a
# vertical-specific name) and Evidence-Weighted Ranking v1
# (``domain.shared_opportunity``). Imported from ``shared_opportunity``
# specifically because it is the venue-neutral of the two existing
# definitions and this module -- like ``counterfactual_ledger``/
# ``counterfactual_followup`` before it -- already depends on
# ``shared_opportunity`` for ``SharedVenue``; no new coupling is added.
COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES = STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES

_RETURN_QUANTUM = Decimal("0.00000001")


class CounterfactualOutcomeError(ValueError):
    """Raised when retained counterfactual-outcome inputs are invalid,
    reference an unknown policy version, or a decision's subject projection
    cannot be resolved (a genuine referential-integrity violation -- every
    ``CounterfactualDecision.subject_projection_id`` must resolve, since the
    decision itself could only have been built by resolving that same
    projection; see ``BatchedLedgerEvidenceSource``)."""


# Counterfactual Outcome Refresh Recovery v1: named (not an inline literal at
# both the one raise site, ``adapters.counterfactual_outcome_postgres.
# PostgresCounterfactualOutcomeRepository.save_aggregate``, and the one
# narrow catch site, ``application.counterfactual_outcome.
# RefreshCounterfactualOutcomeAggregates.execute``) so the two can never
# silently drift apart. Means exactly what it always has: a ``save_aggregate``
# call found an already-retained row under this ``aggregate_id`` whose
# ``canonical_hash`` differs from the one being written now. The application
# layer decides whether that is a genuine conflict (still raised) or a
# same-day evidence-maturation replay (isolated) -- this module only names
# the condition, it does not judge it.
COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT = "COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT"


class CounterfactualOutcomeQualificationState(StrEnum):
    """What, if anything, a cohort's aggregate evidence supports concluding.

    ``QUALIFIED`` and ``INSUFFICIENT_SAMPLE`` are both scoped to venues with
    a defined economic reduction (today: CRYPTO only) -- the difference is
    purely sample size against ``COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES``.
    ``VENUE_ECONOMIC_REDUCTION_UNAVAILABLE`` is a structurally different
    fact: no amount of additional evidence changes it until a future
    milestone adds a genuine baseline-anchored return reduction for that
    venue (today: KALSHI) -- it must never be conflated with
    ``INSUFFICIENT_SAMPLE``, which improves with more data."""

    QUALIFIED = "QUALIFIED"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    VENUE_ECONOMIC_REDUCTION_UNAVAILABLE = "VENUE_ECONOMIC_REDUCTION_UNAVAILABLE"


# Venues with a retained, baseline-anchored, deterministic settlement/price
# -> economic-return reduction under ``COUNTERFACTUAL_OUTCOME_POLICY_V1``.
# See the module docstring's Kalshi section for the investigated,
# evidence-based reasoning behind this set being exactly one venue under V1.
# Frozen: V1's own behavior must never change, including for KALSHI, which
# is exactly why the newer, narrower KALSHI capability below is gated on
# ``COUNTERFACTUAL_OUTCOME_POLICY_V2`` rather than added here.
_ECONOMICALLY_QUANTIFIED_VENUES = frozenset({SharedVenue.CRYPTO})

# Kalshi Decision-Linked Baseline Quote Evidence v1, additive under V2 only:
# the one Kalshi ``subject_source_record_type`` with both (a) a retained
# ``KalshiBaselineQuoteEvidence`` capture path and (b) an existing follow-up
# settlement read that resolves one unambiguous single-market result. See
# ``domain.kalshi_baseline_quote``'s module docstring "Method isolation"
# section for exactly why ``ThresholdBundleEvaluation`` is excluded.
_KALSHI_V2_REDUCIBLE_SOURCE_RECORD_TYPES = frozenset({BINARY_COMPLEMENT_SOURCE_RECORD_TYPE})


def _decision_is_economically_reducible(
    *, venue: SharedVenue, subject_source_record_type: str, outcome_policy_version: str
) -> bool:
    """Whether a *single* decision's realized outcome can, in principle,
    contribute a return under ``outcome_policy_version`` -- never whether it
    actually did this run (a decision can be structurally reducible and
    still contribute nothing this cycle, e.g. not yet matured or its
    baseline quote not yet captured; that is ``INSUFFICIENT_SAMPLE``
    territory, not ``VENUE_ECONOMIC_REDUCTION_UNAVAILABLE`` territory --
    see ``_build_cohort_aggregate``).

    CRYPTO's answer is identical under every known policy version -- this
    milestone does not touch CRYPTO's own reduction at all. KALSHI's answer
    is ``False`` under V1 unconditionally (byte-for-byte the prior
    ``venue in _ECONOMICALLY_QUANTIFIED_VENUES`` behavior) and, under V2,
    ``True`` only for the one reducible source-record type."""

    if venue in _ECONOMICALLY_QUANTIFIED_VENUES:
        return True
    if venue is SharedVenue.KALSHI and outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V2:
        return subject_source_record_type in _KALSHI_V2_REDUCIBLE_SOURCE_RECORD_TYPES
    return False


# V1 is unconditionally in scope for every venue -- frozen, byte-for-byte
# prior behavior, including still producing an honest
# VENUE_ECONOMIC_REDUCTION_UNAVAILABLE aggregate for KALSHI exactly as
# before this milestone. V2 is scoped to KALSHI only: it is additive
# *capability*, not an additive *computation pass over everything V1
# already covers*. Without this gate, a V2 refresh would also rebuild and
# persist a byte-identical restatement of every CRYPTO aggregate V1 already
# computes each cycle (same statistics, same eligible/mature counts --
# only ``outcome_policy_version``/``cohort_id``/``aggregate_id`` would
# differ) -- pure storage duplication carrying zero new information, since
# CRYPTO's own reduction is untouched between V1 and V2 (`_decision_is_
# economically_reducible` returns the identical answer for CRYPTO under
# both). Confirmed empirically, not merely asserted: a genuine recurring-
# cycle regression test (persistence) caught this exact duplication before
# this gate existed.
def _cohort_venue_is_in_scope_for_policy_version(
    *, venue: SharedVenue, outcome_policy_version: str
) -> bool:
    """Whether ``outcome_policy_version`` builds/persists a cohort
    aggregate for this venue at all -- checked once per cohort, before
    ``_decision_is_economically_reducible`` (which answers a narrower,
    per-decision question only meaningful for a venue already in scope)."""

    if outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V2:
        return venue is SharedVenue.KALSHI
    if outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V3:
        # Specificity v3 is scoped to CRYPTO by the identical reasoning that
        # scopes V2 to KALSHI: additive capability for the one venue whose
        # forward windows carry a declared W0/W1 and a contemporaneous
        # multi-asset peer set -- never a second pass restating V1's KALSHI
        # rows.
        return venue is SharedVenue.CRYPTO
    return True


# ---------------------------------------------------------------------------
# Counterfactual Outcome Evidence Specificity v3
# ---------------------------------------------------------------------------
#
# ``COUNTERFACTUAL_OUTCOME_POLICY_V3`` answers a question V1 structurally
# cannot: does a cohort's realized outcome say anything *specific* about the
# gate that produced it, or is it broad market movement, the same economic
# move counted once per asset, and the same decision counted once per
# satisfied reason family?  Three mechanisms, each declared, versioned, and
# fingerprinted into ``COUNTERFACTUAL_OUTCOME_POLICY_V3_HASH``.  The design
# was adversarially reviewed before implementation; the review's blockers
# (a whole-universe cohort's leave-one-out excess is zero by construction;
# a precedence-defined complement measures the wrong gate; a ``None``
# absolute mean makes every V3 fact inert downstream) shaped what follows.
#
# 1. **Canonical gate attribution.**  A decision's classification can name
#    several ``ReasonFamily`` values (``classify_all`` returns a tuple), and
#    some are mechanical consequences of others (a calibration sample below
#    its floor leaves ``expected``/``uncertainty_lower`` ``None`` in
#    ``domain.crypto_universe``, which then fires the ECONOMICS-family codes
#    unconditionally).  Under V1 the identical outcome therefore counts
#    toward every family it names.  V3 binds each decision to exactly one
#    *cohort* via a declared precedence that runs from "not about the
#    asset's own economics at all" (operational, evidence, liquidity,
#    mandate) through sample-size confidence, relative selection, absolute
#    economics, and the residual bucket last.  The precedence is declared
#    rather than inferred because ``CounterfactualDecision.raw_reason_codes``
#    is retained sorted, so a method's runtime emission order is not
#    recoverable.  Precedence decides *membership only* -- never the
#    counterfactual side (see 2), so it introduces no outcome-correlated
#    bias.  A decision with an empty classification (a gate-passer such as
#    ``ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY``) joins no cohort but does
#    join complements.
#
# 2. **Gate-complement outcome.**  For cohort (method M, family G) and one
#    exact economic window (horizon, baseline kind, W0, W1), the comparison
#    is against the products that the *same method*, under the *same
#    decision origin*, evaluated over the *same window* and whose raw
#    classification does not carry G at all -- the gate's genuine
#    complement.  A product carrying G but attributed upstream by
#    precedence is in neither side.  The window's sample value is
#    ``mean(cohort products' returns) - mean(complement products' returns)``,
#    equal-scaled across windows.  A window whose complement holds fewer
#    than ``SPECIFICITY_MINIMUM_PEER_PRODUCTS`` products is *degenerate*:
#    a gate that rejects the whole universe on a day has nothing to be
#    compared against on that day, and is honestly counted rather than
#    reported as a zero-by-construction "sample".  Nothing later than W1 is
#    ever read: every return comes from sibling FOLLOWUP_POLICY_V3 rows over
#    the identical window, so V3's point-in-time guarantees carry over.
#
# 3. **Economic-window independence.**  One window is one sample.  Only
#    windows on a fixed-phase grid count -- ``W0`` a whole multiple of
#    ``horizon_days`` from ``INDEPENDENT_WINDOW_GRID_EPOCH`` -- so accepted
#    windows are pairwise non-overlapping, the accepted set is *monotone*
#    under evidence growth (a late-arriving older window never re-phases the
#    rest, unlike greedy interval scheduling), and every cohort sits on the
#    identical calendar phase.  A window also counts only once its peer
#    coverage is complete: every product with a same-method, same-origin
#    decision anchored to that W0 has a retained eligible return, so a
#    cohort can never first qualify on a partially-drained follow-up
#    backlog.  Qualification is decided on the accepted-window count against
#    the unchanged ``STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES`` floor -- the
#    same number, now counted in independent economic windows rather than in
#    per-asset, per-family, overlapping observations.
#
# Raw and complement economics are never hidden: every V3 aggregate retains
# the raw observation count, the raw window count, every exclusion count by
# cause, and descriptive (explicitly ungated, explicitly labelled) means of
# the raw returns, the complement returns, and the window spreads over every
# informative window, beside the gated accepted-window statistics.

CANONICAL_REASON_FAMILY_ATTRIBUTION_V1 = "CANONICAL_REASON_FAMILY_ATTRIBUTION_V1"
CANONICAL_REASON_FAMILY_PRECEDENCE_V1: tuple[ReasonFamily, ...] = (
    ReasonFamily.PROVIDER_UNAVAILABLE,
    ReasonFamily.OPERATIONAL_FAILURE,
    ReasonFamily.EVIDENCE_INCOMPLETE,
    ReasonFamily.STALE,
    ReasonFamily.LIQUIDITY,
    ReasonFamily.CAPACITY,
    ReasonFamily.MANDATE,
    ReasonFamily.RISK,
    ReasonFamily.RESEARCH_CONFIDENCE,
    ReasonFamily.COMPETITIVE_SELECTION,
    ReasonFamily.ECONOMICS,
    ReasonFamily.OTHER_EXPLICIT,
)
# Import-time invariant: the precedence is a permutation of ``ReasonFamily``,
# so a family added later can never silently fall out of attribution.
if tuple(sorted(CANONICAL_REASON_FAMILY_PRECEDENCE_V1)) != tuple(sorted(ReasonFamily)) or len(
    set(CANONICAL_REASON_FAMILY_PRECEDENCE_V1)
) != len(CANONICAL_REASON_FAMILY_PRECEDENCE_V1):
    raise RuntimeError(
        "CANONICAL_REASON_FAMILY_PRECEDENCE_V1 must be a permutation of ReasonFamily"
    )
PEER_REFERENCE_POLICY_V1 = "SAME_METHOD_SAME_ORIGIN_GATE_COMPLEMENT_V1"
# The fewest complement products a window must retain before its spread is
# treated as informative at all.  Below this the "complement" would be one or
# two idiosyncratic assets rather than the rest of the method's universe.
SPECIFICITY_MINIMUM_PEER_PRODUCTS = 3
INDEPENDENT_WINDOW_POLICY_V1 = "FIXED_PHASE_GRID_TILING_V1"
INDEPENDENT_WINDOW_GRID_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Deliberately the identical number V1 already reuses, not a new one: the
# floor is unchanged, only the unit it counts (independent economic windows,
# never raw observations) is.
COUNTERFACTUAL_OUTCOME_MINIMUM_INDEPENDENT_WINDOWS = STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES
# Which V3 cohorts may originate a *new* research fact.  ``DECISION_TIME``
# V3 windows are lagged by the daily-close ceiling (a cycle at 00:00:20Z
# anchors its 1-day window to [D+1, D+2), while ``ORIGIN_EVIDENCE`` anchors
# the same evidence to [D, D+1)), and both baselines of the same gate
# describe the same economic question; sourcing research from one avoids
# every gate producing two facts per horizon.  ``DECISION_TIME`` V3
# aggregates are still computed, retained, and fully queryable.
V3_RESEARCH_SOURCING_BASELINE_KINDS: frozenset[FollowUpBaselineKind] = frozenset(
    {FollowUpBaselineKind.ORIGIN_EVIDENCE}
)
V3_DECISION_TIME_WINDOW_LAGGED_BY_DAILY_CEILING = "V3_DECISION_TIME_WINDOW_LAGGED_BY_DAILY_CEILING"
COUNTERFACTUAL_OUTCOME_POLICY_V3_HASH = content_digest(
    (
        COUNTERFACTUAL_OUTCOME_POLICY_V3,
        CANONICAL_REASON_FAMILY_ATTRIBUTION_V1,
        tuple(family.value for family in CANONICAL_REASON_FAMILY_PRECEDENCE_V1),
        PEER_REFERENCE_POLICY_V1,
        SPECIFICITY_MINIMUM_PEER_PRODUCTS,
        INDEPENDENT_WINDOW_POLICY_V1,
        INDEPENDENT_WINDOW_GRID_EPOCH,
        COUNTERFACTUAL_OUTCOME_MINIMUM_INDEPENDENT_WINDOWS,
        tuple(sorted(kind.value for kind in V3_RESEARCH_SOURCING_BASELINE_KINDS)),
    )
)
# Leads the one conditional block ``counterfactual_outcome_aggregate_
# canonical_hash`` appends for a V3 aggregate -- the same single-marker,
# all-or-nothing idiom ``followup_observation_canonical_hash`` uses for its
# FOLLOWUP_POLICY_V3 block, so every already-retained V1/V2 aggregate
# computes the exact same fixed-length tuple, and the exact same hash, it
# always did.
COUNTERFACTUAL_OUTCOME_SPECIFICITY_HASH_BLOCK = "COUNTERFACTUAL_OUTCOME_SPECIFICITY_V3_BLOCK"

OUTCOME_POLICY_RAW_SAMPLE_NON_INDEPENDENCE_AND_MARKET_BETA_DEFECT = (
    "RAW_SAMPLE_NON_INDEPENDENCE_AND_MARKET_BETA_DEFECT"
)


@dataclass(frozen=True, slots=True)
class OutcomePolicyVersionDefect:
    """One reviewed, named qualification-contract defect in one ``(venue,
    outcome_policy_version)`` pair, together with the version that corrects
    it -- the outcome-policy sibling of ``domain.counterfactual_followup.
    FollowUpPolicyVersionDefect``, with the identical mandatory-successor
    discipline for the identical reason: a defect with no corrective
    successor would be a decision to stop researching a venue, not a
    decision about evidence quality.  The measurements V1 retains are
    correct; what is defective is treating them as independent,
    gate-specific samples."""

    venue: SharedVenue
    outcome_policy_version: str
    defect_code: str
    corrected_by_outcome_policy_version: str
    summary: str


DEFECTIVE_OUTCOME_POLICY_VERSIONS: tuple[OutcomePolicyVersionDefect, ...] = (
    # Measured against real retained evidence before V3 existed: every
    # CRYPTO STRATEGY_HORIZON cohort qualified under V1 reported a mean raw
    # return of roughly +27% to +32% with a positive rate of exactly 1.0
    # across every method and every reason family -- a market rally, not
    # gate-specific information -- and a read-only peer-relative restatement
    # of the RELATIVE_STRENGTH family moved +13.53% raw to -0.40%.  V1's
    # sample unit (one observation per asset per decision day per satisfied
    # family) is not an independent sample of anything.
    OutcomePolicyVersionDefect(
        venue=SharedVenue.CRYPTO,
        outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V1,
        defect_code=OUTCOME_POLICY_RAW_SAMPLE_NON_INDEPENDENCE_AND_MARKET_BETA_DEFECT,
        corrected_by_outcome_policy_version=COUNTERFACTUAL_OUTCOME_POLICY_V3,
        summary=(
            "CRYPTO COUNTERFACTUAL_OUTCOME_POLICY_V1 qualifies on raw per-asset, per-family, "
            "overlapping-window observation counts and reports absolute returns, so broad "
            "market movement and one economic move counted many times read as "
            "gate-specific evidence."
        ),
    ),
)

_OUTCOME_DEFECT_BY_VENUE_AND_POLICY_VERSION: Mapping[
    tuple[SharedVenue, str], OutcomePolicyVersionDefect
]
_OUTCOME_DEFECT_BY_VENUE_AND_POLICY_VERSION = MappingProxyType(
    {(item.venue, item.outcome_policy_version): item for item in DEFECTIVE_OUTCOME_POLICY_VERSIONS}
)


def outcome_policy_version_defect(
    *, venue: SharedVenue, outcome_policy_version: str
) -> OutcomePolicyVersionDefect | None:
    """The reviewed defect recorded against this ``(venue, outcome policy
    version)`` pair, or ``None``.  Pure; performs no I/O."""

    return _OUTCOME_DEFECT_BY_VENUE_AND_POLICY_VERSION.get((venue, outcome_policy_version))


def outcome_policy_version_may_source_new_research(
    *, venue: SharedVenue, outcome_policy_version: str
) -> bool:
    """Whether a retained aggregate of this ``(venue, outcome policy version)``
    pair may originate a **new** research conclusion.  Same semantics as
    ``followup_policy_version_may_source_new_research``: a forward sourcing
    restriction only, never a statement about the validity, queryability,
    or historical standing of already-retained rows or their descendants;
    an unrecognized version is permitted rather than fabricated into a
    defect."""

    return (
        outcome_policy_version_defect(venue=venue, outcome_policy_version=outcome_policy_version)
        is None
    )


@dataclass(frozen=True, slots=True)
class CounterfactualOutcomeSourcingExclusion:
    """Why one retained aggregate may not originate new research: exactly
    one of the three declared gates, named so a withheld cohort is
    explainable rather than merely absent."""

    reason_code: str
    corrected_by: str
    summary: str


def counterfactual_outcome_sourcing_exclusion(
    *,
    venue: SharedVenue,
    outcome_policy_version: str,
    followup_policy_version: str,
    baseline_kind: FollowUpBaselineKind,
) -> CounterfactualOutcomeSourcingExclusion | None:
    """The single choke point every research-sourcing surface consults for
    a counterfactual-outcome aggregate.  Three gates, checked in a fixed
    order: the follow-up policy quarantine (``domain.counterfactual_followup.
    DEFECTIVE_FOLLOWUP_POLICY_VERSIONS``), the outcome policy quarantine
    (``DEFECTIVE_OUTCOME_POLICY_VERSIONS``), and V3's own baseline-kind
    sourcing selection (``V3_RESEARCH_SOURCING_BASELINE_KINDS``).  ``None``
    means the aggregate may source new research."""

    followup_defect = followup_policy_version_defect(
        venue=venue, policy_version=followup_policy_version
    )
    if followup_defect is not None:
        return CounterfactualOutcomeSourcingExclusion(
            reason_code=followup_defect.defect_code,
            corrected_by=followup_defect.corrected_by_policy_version,
            summary=followup_defect.summary,
        )
    outcome_defect = outcome_policy_version_defect(
        venue=venue, outcome_policy_version=outcome_policy_version
    )
    if outcome_defect is not None:
        return CounterfactualOutcomeSourcingExclusion(
            reason_code=outcome_defect.defect_code,
            corrected_by=outcome_defect.corrected_by_outcome_policy_version,
            summary=outcome_defect.summary,
        )
    if (
        outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V3
        and baseline_kind not in V3_RESEARCH_SOURCING_BASELINE_KINDS
    ):
        return CounterfactualOutcomeSourcingExclusion(
            reason_code=V3_DECISION_TIME_WINDOW_LAGGED_BY_DAILY_CEILING,
            corrected_by=FollowUpBaselineKind.ORIGIN_EVIDENCE.value,
            summary=(
                "COUNTERFACTUAL_OUTCOME_POLICY_V3 sources research from ORIGIN_EVIDENCE "
                "cohorts only: a DECISION_TIME window is lagged one daily-close boundary "
                "by the V3 ceiling and describes the same economic question, so it is "
                "retained and queryable but originates no new research fact."
            ),
        )
    return None


def canonical_reason_family(
    reason_families: Sequence[ReasonFamily],
) -> ReasonFamily | None:
    """The single family a decision is attributed to under
    ``CANONICAL_REASON_FAMILY_ATTRIBUTION_V1``: the first entry of the
    declared precedence present in ``reason_families``, or ``None`` for an
    empty classification (an unattributed gate-passer).  Order-independent
    in its input, total over every ``ReasonFamily``, and pure."""

    present = frozenset(reason_families)
    for family in CANONICAL_REASON_FAMILY_PRECEDENCE_V1:
        if family in present:
            return family
    return None


def independent_window_key(
    *, window_start: datetime, window_end: datetime, cohort_count: int, complement_count: int
) -> str:
    """The retained, human-readable identity of one accepted economic
    window -- ``<W0>/<W1>:cohort=<n>:complement=<m>`` in canonical UTC
    timestamps -- so an aggregate's accepted windows can be audited and
    replayed exactly from the row itself."""

    return (
        f"{canonical_timestamp(window_start)}/{canonical_timestamp(window_end)}"
        f":cohort={cohort_count}:complement={complement_count}"
    )


def window_is_on_independent_grid(*, window_start: datetime, horizon_days: int) -> bool:
    """``INDEPENDENT_WINDOW_POLICY_V1``: a window of ``horizon_days`` days is
    on the fixed-phase grid iff its start is a whole multiple of
    ``horizon_days`` from ``INDEPENDENT_WINDOW_GRID_EPOCH``.  Every daily
    (``horizon_days == 1``) window is on the grid; consecutive-day 30-day
    windows share at most one grid slot.  Two grid windows of the same
    horizon can never overlap, so the accepted set is independent by
    construction and monotone under evidence growth."""

    if horizon_days <= 0:
        return False
    elapsed = window_start - INDEPENDENT_WINDOW_GRID_EPOCH
    if elapsed.seconds or elapsed.microseconds:
        return False
    return elapsed.days % horizon_days == 0


@dataclass(frozen=True, slots=True)
class SubjectProjectionFacts:
    """The three facts a V3 build needs from a decision's own
    ``SharedOpportunityProjection``: its method, its product, and the
    projection's ``generated_at`` (the ``ORIGIN_EVIDENCE`` policy anchor).
    Read once by the application layer from the identical projection lookup
    V1 already performs; never re-derived here."""

    strategy_or_method_id: str
    product_or_market_id: str
    generated_at: datetime


_WindowKey = tuple[str, str, str, datetime, datetime]


def _window_key(observation: DecisionFollowUpObservation) -> _WindowKey:
    return (
        observation.horizon_kind.value,
        observation.baseline_kind.value,
        observation.policy_version,
        observation.baseline_time,
        observation.target_time,
    )


@dataclass(frozen=True, slots=True)
class ContemporaneousPeerWindow:
    """The contemporaneous observed universe for one exact economic window:
    one evidence-eligible return per product.  ``consistent`` is ``False``
    when two retained observations of the identical product over the
    identical window disagree on the return -- possible only between
    *different decisions'* observations of the same product-window, since
    an OBSERVED pair is never re-measured -- and makes every spread in this
    window unavailable rather than silently averaged."""

    returns_by_product: Mapping[str, Decimal]
    consistent: bool
    inconsistent_products: tuple[str, ...]


def build_contemporaneous_peer_windows(
    *,
    observations: Sequence[DecisionFollowUpObservation],
    assessment_by_observation: Mapping[str, FollowUpEvidenceQualityAssessment],
) -> Mapping[_WindowKey, ContemporaneousPeerWindow]:
    """Every CRYPTO FOLLOWUP_POLICY_V3 observation that is evidence-eligible
    under the identical centralized gate the cohort statistics use and
    carries a ``subsequent_return``, grouped by exact window and reduced to
    one return per product.  Every decision, method, cohort, and
    disposition contributes: this is the retained market over that window,
    from which each cohort's own side and complement side are then drawn."""

    grouped: dict[_WindowKey, dict[str, set[Decimal]]] = defaultdict(lambda: defaultdict(set))
    for observation in observations:
        if observation.venue is not SharedVenue.CRYPTO:
            continue
        if observation.policy_version != FOLLOWUP_POLICY_V3:
            continue
        if observation.subsequent_return is None:
            continue
        if not is_evidence_eligible_for_downstream_attribution(
            assessment_by_observation.get(observation.observation_id)
        ):
            continue
        grouped[_window_key(observation)][observation.subject_product_or_market_id].add(
            observation.subsequent_return
        )
    windows: dict[_WindowKey, ContemporaneousPeerWindow] = {}
    for key, returns_by_product in grouped.items():
        inconsistent = tuple(
            sorted(product for product, values in returns_by_product.items() if len(values) != 1)
        )
        consistent = not inconsistent
        windows[key] = ContemporaneousPeerWindow(
            returns_by_product=MappingProxyType(
                {
                    product: next(iter(values))
                    for product, values in sorted(returns_by_product.items())
                }
                if consistent
                else {}
            ),
            consistent=consistent,
            inconsistent_products=inconsistent,
        )
    return MappingProxyType(windows)


@dataclass(frozen=True, slots=True)
class _Membership:
    """One decision's standing at one (method, origin, baseline kind, W0)."""

    decision_id: str
    product_id: str
    raw_families: frozenset[ReasonFamily]
    canonical_family: ReasonFamily | None


_MembershipKey = tuple[str, str, str, datetime]  # method, origin, baseline kind, W0


def _membership_index(
    *,
    decisions: Sequence[CounterfactualDecision],
    classification_by_decision: Mapping[str, CounterfactualDecisionClassification],
    subject_facts_by_projection: Mapping[str, SubjectProjectionFacts],
) -> tuple[Mapping[_MembershipKey, tuple[_Membership, ...]], Mapping[str, int]]:
    """Every classified CRYPTO decision's expected ``W0`` under each
    baseline kind, derived structurally with the identical
    ``first_daily_close_boundary_at_or_after`` FOLLOWUP_POLICY_V3 itself
    anchors with -- ``SharedOpportunityProjection.generated_at`` for
    ``ORIGIN_EVIDENCE``, ``CounterfactualDecision.evaluated_at`` for
    ``DECISION_TIME``.  This is the complete denominator a window's peer
    coverage is measured against (the decision ledger is complete; the
    follow-up backlog is not).  Also returns, per method, how many
    decisions are unattributed (empty classification)."""

    index: dict[_MembershipKey, list[_Membership]] = defaultdict(list)
    unattributed_by_method: dict[str, int] = defaultdict(int)
    for decision in sorted(decisions, key=lambda item: item.decision_id):
        if decision.venue is not SharedVenue.CRYPTO:
            continue
        classification = classification_by_decision.get(decision.decision_id)
        if classification is None:
            continue
        facts = subject_facts_by_projection.get(decision.subject_projection_id)
        if facts is None:
            raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_PROJECTION_MISSING")
        raw_families = frozenset(classification.reason_families)
        canonical = canonical_reason_family(classification.reason_families)
        if canonical is None:
            unattributed_by_method[facts.strategy_or_method_id] += 1
        membership = _Membership(
            decision_id=decision.decision_id,
            product_id=facts.product_or_market_id,
            raw_families=raw_families,
            canonical_family=canonical,
        )
        for baseline_kind, anchor in (
            (FollowUpBaselineKind.ORIGIN_EVIDENCE, facts.generated_at),
            (FollowUpBaselineKind.DECISION_TIME, decision.evaluated_at),
        ):
            window_start = first_daily_close_boundary_at_or_after(anchor)
            index[
                (
                    facts.strategy_or_method_id,
                    decision.origin.value,
                    baseline_kind.value,
                    window_start,
                )
            ].append(membership)
    return (
        MappingProxyType({key: tuple(values) for key, values in index.items()}),
        MappingProxyType(dict(unattributed_by_method)),
    )


@dataclass(frozen=True, slots=True)
class CounterfactualOutcomeSpecificity:
    """The V3-only block of a ``CounterfactualOutcomeAggregate``: raw,
    complement, and spread economics side by side, with every window
    exclusion counted by cause, so a reader can see exactly how much
    apparent evidence collapsed and why.  ``None`` on every V1/V2
    aggregate; never ``None`` on a V3 aggregate (a biconditional with
    ``outcome_policy_version``, exactly like migration 0113's
    ``ck_decision_followup_v3_measurement_as_of``).

    ``mean_relative_window_return``/``median_relative_window_return``/
    ``relative_positive_window_rate``/``relative_window_return_standard_
    deviation`` are the gated, qualified statistics over accepted windows
    only.  Every ``descriptive_*`` field is explicitly ungated and
    explicitly descriptive."""

    attribution_policy_version: str
    peer_reference_policy_version: str
    independent_window_policy_version: str
    policy_hash: str
    multi_family_decision_count: int
    method_unattributed_decision_count: int
    raw_return_observation_count: int
    anchor_mismatch_observation_count: int
    raw_window_count: int
    inconsistent_window_count: int
    mixed_origin_window_count: int
    membership_conflict_product_count: int
    degenerate_window_count: int
    products_with_decision_but_no_return_count: int
    awaiting_peer_completion_window_count: int
    off_grid_window_count: int
    window_horizon_days: int | None
    horizon_days_heterogeneous: bool
    independent_window_count: int
    independent_window_keys: tuple[str, ...]
    first_window_start: datetime | None
    last_window_end: datetime | None
    # ``descriptive_mean_raw_return`` is *observation*-weighted (a day with
    # six same-product decisions counts six times, exactly the V1 unit) and
    # is retained only so the raw economics are never hidden; the three
    # window-level descriptive means below are equal-weighted per
    # informative window and are the ones comparable to each other.
    descriptive_mean_raw_return: Decimal | None
    descriptive_mean_cohort_window_return: Decimal | None
    descriptive_mean_complement_return: Decimal | None
    descriptive_mean_window_spread: Decimal | None
    descriptive_informative_window_count: int
    relative_positive_window_count: int
    relative_negative_window_count: int
    relative_zero_window_count: int
    mean_relative_window_return: Decimal | None
    median_relative_window_return: Decimal | None
    relative_positive_window_rate: Decimal | None
    relative_window_return_standard_deviation: Decimal | None


def _sample_standard_deviation(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = sum(values, start=Decimal(0)) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), start=Decimal(0)) / Decimal(
        len(values) - 1
    )
    return variance.sqrt().quantize(_RETURN_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class _WindowEvaluation:
    window_start: datetime
    window_end: datetime
    value: Decimal | None
    cohort_mean: Decimal | None
    complement_mean: Decimal | None
    cohort_count: int
    complement_count: int
    cohort_decision_ids: tuple[str, ...]
    accepted: bool


@dataclass(frozen=True, slots=True)
class _SpecificityBuild:
    specificity: CounterfactualOutcomeSpecificity
    qualified: bool
    contributing_decision_ids: tuple[str, ...]


def _build_specificity(
    *,
    strategy_or_method_id: str,
    reason_family: ReasonFamily,
    baseline_kind: FollowUpBaselineKind,
    cohort_decisions: Sequence[CounterfactualDecision],
    cohort_observations: Sequence[DecisionFollowUpObservation],
    classification_by_decision: Mapping[str, CounterfactualDecisionClassification],
    peer_windows: Mapping[_WindowKey, ContemporaneousPeerWindow],
    membership_index: Mapping[_MembershipKey, tuple[_Membership, ...]],
    method_unattributed_decision_count: int,
) -> _SpecificityBuild:
    """Pure reduction of one cohort's evidence-eligible, return-carrying V3
    observations (one per contributing decision, all sharing one horizon
    kind, baseline kind, and follow-up policy version) into the specificity
    block.  Order-independent: every grouping is keyed by retained identity
    and every list is sorted or reduced by a commutative operation."""

    decision_by_id = {decision.decision_id: decision for decision in cohort_decisions}
    multi_family = sum(
        1
        for decision in cohort_decisions
        if (classification := classification_by_decision.get(decision.decision_id)) is not None
        and len(classification.reason_families) > 1
    )
    raw_returns: list[Decimal] = []
    anchor_mismatch = 0
    windows: dict[tuple[datetime, datetime], list[DecisionFollowUpObservation]] = defaultdict(list)
    for observation in sorted(cohort_observations, key=lambda item: item.observation_id):
        if observation.subsequent_return is None:
            continue
        raw_returns.append(observation.subsequent_return)
        decision = decision_by_id[observation.decision_id]
        expected_key: _MembershipKey = (
            strategy_or_method_id,
            decision.origin.value,
            baseline_kind.value,
            observation.baseline_time,
        )
        if not any(
            member.decision_id == decision.decision_id
            for member in membership_index.get(expected_key, ())
        ):
            # The observation's own retained W0 disagrees with the W0 its
            # decision structurally implies -- fail closed for this
            # observation rather than trust either silently.
            anchor_mismatch += 1
            continue
        windows[(observation.baseline_time, observation.target_time)].append(observation)

    inconsistent = 0
    mixed_origin = 0
    conflicts = 0
    degenerate = 0
    missing_returns = 0
    awaiting = 0
    off_grid = 0
    evaluations: list[_WindowEvaluation] = []
    horizon_days_seen: set[int] = set()
    for (window_start, window_end), window_observations in sorted(windows.items()):
        horizon_days = (window_end - window_start).days
        horizon_days_seen.add(horizon_days)
        peer_window = peer_windows.get(
            (
                window_observations[0].horizon_kind.value,
                baseline_kind.value,
                FOLLOWUP_POLICY_V3,
                window_start,
                window_end,
            )
        )
        if peer_window is None or not peer_window.consistent:
            inconsistent += 1
            continue
        origins = sorted(
            {decision_by_id[item.decision_id].origin.value for item in window_observations}
        )
        if len(origins) != 1:
            mixed_origin += 1
            continue
        members = membership_index.get(
            (strategy_or_method_id, origins[0], baseline_kind.value, window_start), ()
        )
        expected_products = {member.product_id for member in members}
        returns = peer_window.returns_by_product
        covered = {product for product in expected_products if product in returns}
        missing_returns += len(expected_products) - len(covered)
        cohort_side = {
            member.product_id for member in members if member.canonical_family is reason_family
        }
        complement_side = {
            member.product_id for member in members if reason_family not in member.raw_families
        }
        conflicting = cohort_side & complement_side
        conflicts += len(conflicting)
        cohort_side -= conflicting
        complement_side -= conflicting
        cohort_returns = [returns[product] for product in sorted(cohort_side) if product in returns]
        complement_returns = [
            returns[product] for product in sorted(complement_side) if product in returns
        ]
        cohort_decision_ids = tuple(
            sorted(
                member.decision_id
                for member in members
                if member.product_id in cohort_side and member.product_id in returns
            )
        )
        if not cohort_returns or len(complement_returns) < SPECIFICITY_MINIMUM_PEER_PRODUCTS:
            degenerate += 1
            continue
        cohort_mean = _mean(cohort_returns)
        complement_mean = _mean(complement_returns)
        value = (cohort_mean - complement_mean).quantize(_RETURN_QUANTUM, rounding=ROUND_HALF_EVEN)
        complete = covered == expected_products
        on_grid = window_is_on_independent_grid(
            window_start=window_start, horizon_days=horizon_days
        )
        if not complete:
            awaiting += 1
        elif not on_grid:
            off_grid += 1
        evaluations.append(
            _WindowEvaluation(
                window_start=window_start,
                window_end=window_end,
                value=value,
                cohort_mean=cohort_mean,
                complement_mean=complement_mean,
                cohort_count=len(cohort_returns),
                complement_count=len(complement_returns),
                cohort_decision_ids=cohort_decision_ids,
                accepted=complete and on_grid,
            )
        )

    heterogeneous = len(horizon_days_seen) > 1
    window_horizon_days = next(iter(horizon_days_seen)) if len(horizon_days_seen) == 1 else None
    accepted = () if heterogeneous else tuple(item for item in evaluations if item.accepted)
    accepted_values = [item.value for item in accepted if item.value is not None]
    informative_values = [item.value for item in evaluations if item.value is not None]
    cohort_means = [item.cohort_mean for item in evaluations if item.cohort_mean is not None]
    complement_means = [
        item.complement_mean for item in evaluations if item.complement_mean is not None
    ]
    qualified = len(accepted) >= COUNTERFACTUAL_OUTCOME_MINIMUM_INDEPENDENT_WINDOWS
    positive = sum(1 for value in accepted_values if value > 0)
    negative = sum(1 for value in accepted_values if value < 0)
    zero = sum(1 for value in accepted_values if value == 0)
    contributing = tuple(
        sorted({decision_id for item in accepted for decision_id in item.cohort_decision_ids})
    )
    specificity = CounterfactualOutcomeSpecificity(
        attribution_policy_version=CANONICAL_REASON_FAMILY_ATTRIBUTION_V1,
        peer_reference_policy_version=PEER_REFERENCE_POLICY_V1,
        independent_window_policy_version=INDEPENDENT_WINDOW_POLICY_V1,
        policy_hash=COUNTERFACTUAL_OUTCOME_POLICY_V3_HASH,
        multi_family_decision_count=multi_family,
        method_unattributed_decision_count=method_unattributed_decision_count,
        raw_return_observation_count=len(raw_returns),
        anchor_mismatch_observation_count=anchor_mismatch,
        raw_window_count=len(windows),
        inconsistent_window_count=inconsistent,
        mixed_origin_window_count=mixed_origin,
        membership_conflict_product_count=conflicts,
        degenerate_window_count=degenerate,
        products_with_decision_but_no_return_count=missing_returns,
        awaiting_peer_completion_window_count=awaiting,
        off_grid_window_count=off_grid,
        window_horizon_days=window_horizon_days,
        horizon_days_heterogeneous=heterogeneous,
        independent_window_count=len(accepted),
        independent_window_keys=tuple(
            independent_window_key(
                window_start=item.window_start,
                window_end=item.window_end,
                cohort_count=item.cohort_count,
                complement_count=item.complement_count,
            )
            for item in accepted
        ),
        first_window_start=accepted[0].window_start if accepted else None,
        last_window_end=accepted[-1].window_end if accepted else None,
        descriptive_mean_raw_return=_mean(raw_returns) if raw_returns else None,
        descriptive_mean_cohort_window_return=_mean(cohort_means) if cohort_means else None,
        descriptive_mean_complement_return=(_mean(complement_means) if complement_means else None),
        descriptive_mean_window_spread=(_mean(informative_values) if informative_values else None),
        descriptive_informative_window_count=len(evaluations),
        relative_positive_window_count=positive,
        relative_negative_window_count=negative,
        relative_zero_window_count=zero,
        mean_relative_window_return=_mean(accepted_values) if qualified else None,
        median_relative_window_return=_median(accepted_values) if qualified else None,
        relative_positive_window_rate=(
            (Decimal(positive) / Decimal(len(accepted_values))).quantize(
                _RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
            )
            if qualified
            else None
        ),
        relative_window_return_standard_deviation=(
            _sample_standard_deviation(accepted_values) if qualified else None
        ),
    )
    return _SpecificityBuild(
        specificity=specificity, qualified=qualified, contributing_decision_ids=contributing
    )


@dataclass(frozen=True, slots=True)
class CounterfactualOutcomeAggregate:
    """One immutable, append-only aggregate rollup over one fully-specified
    cohort of retained counterfactual evidence, as of one recurring-cycle
    run. Never a per-decision verdict; see the module docstring."""

    aggregate_id: str
    cohort_id: str
    outcome_policy_version: str
    run_id: str
    venue: SharedVenue
    strategy_or_method_id: str
    reason_family: ReasonFamily
    horizon_kind: FollowUpHorizonKind
    baseline_kind: FollowUpBaselineKind
    followup_policy_version: str
    classifier_version: str
    evidence_quality_policy_version: str
    generated_at: datetime
    total_observations_considered: int
    mature_observation_count: int
    eligible_observation_count: int
    evidence_ineligible_observation_count: int
    unassessed_observation_count: int
    unavailable_observation_count: int
    positive_outcome_count: int
    negative_outcome_count: int
    zero_outcome_count: int
    mean_subsequent_return: Decimal | None
    median_subsequent_return: Decimal | None
    positive_rate: Decimal | None
    qualification_state: CounterfactualOutcomeQualificationState
    contributing_decision_ids: tuple[str, ...]
    explanation: str
    canonical_hash: str
    # Counterfactual Outcome Evidence Specificity v3: populated only on a
    # ``COUNTERFACTUAL_OUTCOME_POLICY_V3`` aggregate; ``None`` on every V1/V2
    # aggregate of any age (never backfilled), and excluded from the hash
    # payload entirely when ``None`` -- see ``counterfactual_outcome_
    # aggregate_canonical_hash``.
    specificity: CounterfactualOutcomeSpecificity | None = None
    authority: str = "NONE"


def counterfactual_outcome_cohort_id(
    *,
    outcome_policy_version: str,
    venue: SharedVenue | str,
    strategy_or_method_id: str,
    reason_family: ReasonFamily | str,
    horizon_kind: FollowUpHorizonKind | str,
    baseline_kind: FollowUpBaselineKind | str,
    followup_policy_version: str,
    classifier_version: str,
    evidence_quality_policy_version: str,
) -> str:
    """Stable across every recurring-cycle run: a pure function of only the
    cohort's own structural partition key, never of ``run_id`` or any
    computed statistic. This is what a Research Operations fact anchors to
    so "the same cohort continuing to qualify" never re-notifies a human
    every cycle -- see ``counterfactual_outcome_aggregate_id`` for the
    per-run identity this is deliberately *not*."""

    return deterministic_id(
        "counterfactual_outcome_cohort",
        (
            outcome_policy_version,
            venue,
            strategy_or_method_id,
            reason_family,
            horizon_kind,
            baseline_kind,
            followup_policy_version,
            classifier_version,
            evidence_quality_policy_version,
        ),
    )


def counterfactual_outcome_aggregate_id(*, cohort_id: str, run_id: str) -> str:
    """One row per ``(cohort_id, run_id)`` -- mirrors
    ``domain.performance_attribution``'s own established idiom for a
    periodically-recomputed rollup over accumulating evidence
    (``StrategyHealthAssessment.health_id = f"{run_id}-{method_id}-
    {method_version}-health"``): a caller-supplied, cycle-scoped ``run_id``
    is the versioning axis across time, so replaying the identical cycle
    (``run_id`` unchanged) reproduces the identical id (idempotent), while
    each new cycle's ``run_id`` produces a fresh, append-only row -- an
    evolving time series of point-in-time rollups, never a mutation of an
    earlier one in place."""

    return deterministic_id("counterfactual_outcome_aggregate", (cohort_id, run_id))


def counterfactual_outcome_aggregate_canonical_hash(
    *,
    aggregate_id: str,
    cohort_id: str,
    outcome_policy_version: str,
    run_id: str,
    venue: SharedVenue | str,
    strategy_or_method_id: str,
    reason_family: ReasonFamily | str,
    horizon_kind: FollowUpHorizonKind | str,
    baseline_kind: FollowUpBaselineKind | str,
    followup_policy_version: str,
    classifier_version: str,
    evidence_quality_policy_version: str,
    total_observations_considered: int,
    mature_observation_count: int,
    eligible_observation_count: int,
    evidence_ineligible_observation_count: int,
    unassessed_observation_count: int,
    unavailable_observation_count: int,
    positive_outcome_count: int,
    negative_outcome_count: int,
    zero_outcome_count: int,
    mean_subsequent_return: Decimal | None,
    median_subsequent_return: Decimal | None,
    positive_rate: Decimal | None,
    qualification_state: CounterfactualOutcomeQualificationState | str,
    contributing_decision_ids: tuple[str, ...],
    explanation: str,
    specificity: CounterfactualOutcomeSpecificity | None = None,
) -> str:
    """The ``CounterfactualOutcomeAggregate.canonical_hash`` formula,
    exposed (not underscore-prefixed) exactly like ``counterfactual_ledger.
    decision_canonical_hash`` and ``counterfactual_classification.
    classification_canonical_hash`` before it, so a future migration or
    diagnostic can recompute it byte-for-byte from a row's own already-
    persisted fields. ``generated_at`` (a run's own wall-clock stamp) is
    deliberately excluded -- mirroring ``CounterfactualDecisionClassification
    ``'s own exclusion of ``classified_at`` -- so a genuine crash-resume
    replay under the same ``run_id`` at a different wall-clock instant
    never manufactures a spurious identity conflict.

    ``specificity`` (Counterfactual Outcome Evidence Specificity v3) is
    appended as ONE conditional block led by
    ``COUNTERFACTUAL_OUTCOME_SPECIFICITY_HASH_BLOCK``, only when not
    ``None`` -- so every V1/V2 aggregate, retained or freshly built, computes
    the exact same fixed-length tuple this function always computed."""

    payload: tuple[object, ...] = (
        outcome_policy_version,
        aggregate_id,
        cohort_id,
        run_id,
        venue,
        strategy_or_method_id,
        reason_family,
        horizon_kind,
        baseline_kind,
        followup_policy_version,
        classifier_version,
        evidence_quality_policy_version,
        total_observations_considered,
        mature_observation_count,
        eligible_observation_count,
        evidence_ineligible_observation_count,
        unassessed_observation_count,
        unavailable_observation_count,
        positive_outcome_count,
        negative_outcome_count,
        zero_outcome_count,
        mean_subsequent_return,
        median_subsequent_return,
        positive_rate,
        qualification_state,
        contributing_decision_ids,
        explanation,
        "NONE",
    )
    if specificity is not None:
        payload = (*payload, COUNTERFACTUAL_OUTCOME_SPECIFICITY_HASH_BLOCK, specificity)
    return content_digest(payload)


def _mean(values: Sequence[Decimal]) -> Decimal:
    total = sum(values, start=Decimal(0))
    return (total / Decimal(len(values))).quantize(_RETURN_QUANTUM, rounding=ROUND_HALF_EVEN)


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / Decimal(2)).quantize(
        _RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
    )


def _explanation(
    *,
    venue: SharedVenue,
    strategy_or_method_id: str,
    reason_family: ReasonFamily,
    horizon_kind: FollowUpHorizonKind,
    baseline_kind: FollowUpBaselineKind,
    followup_policy_version: str,
    total_observations_considered: int,
    mature_observation_count: int,
    eligible_observation_count: int,
    qualification_state: CounterfactualOutcomeQualificationState,
    mean_subsequent_return: Decimal | None,
    positive_rate: Decimal | None,
    specificity: CounterfactualOutcomeSpecificity | None = None,
) -> str:
    header = (
        f"{venue.value}/{strategy_or_method_id}/{reason_family.value} "
        f"({horizon_kind.value}, {baseline_kind.value}, {followup_policy_version}): "
        f"{mature_observation_count}/{total_observations_considered} decisions matured, "
        f"{eligible_observation_count} evidence-eligible."
    )
    if (
        qualification_state
        is CounterfactualOutcomeQualificationState.VENUE_ECONOMIC_REDUCTION_UNAVAILABLE
    ):
        return (
            f"{header} {venue.value} retains no baseline-time entry price/probability for "
            "this evidence, so no deterministic settlement-to-return reduction exists yet "
            "(prerequisite: a baseline-anchored quote analogous to CRYPTO's "
            "reference_price_at_baseline/ORIGIN_EVIDENCE) -- economic statistics are "
            "intentionally left unavailable rather than fabricated."
        )
    if specificity is not None:
        return _specificity_explanation(
            header=header, qualification_state=qualification_state, specificity=specificity
        )
    if qualification_state is CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE:
        return (
            f"{header} {eligible_observation_count} eligible sample(s) is below the "
            f"established minimum of {COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES} independently "
            "partitioned observations required before mean/median/win-rate are treated as "
            "qualified evidence."
        )
    return (
        f"{header} mean subsequent return {mean_subsequent_return}, "
        f"positive-outcome rate {positive_rate} over {eligible_observation_count} qualified "
        f"samples (raw count; not adjusted for potential temporal/asset autocorrelation "
        "between decisions on related underlying instruments -- see this milestone's own "
        "disclosed limitation)."
    )


def _specificity_explanation(
    *,
    header: str,
    qualification_state: CounterfactualOutcomeQualificationState,
    specificity: CounterfactualOutcomeSpecificity,
) -> str:
    """The V3 explanation names the statistic for what it is -- a
    peer-relative, window-level gate-complement spread -- and every
    exclusion by cause, because this string is the research fact's own
    summary and the one place a human reads before deciding anything."""

    exclusions = (
        f"{specificity.raw_window_count} raw windows: "
        f"{specificity.independent_window_count} accepted independent, "
        f"{specificity.degenerate_window_count} degenerate (complement below "
        f"{SPECIFICITY_MINIMUM_PEER_PRODUCTS} products), "
        f"{specificity.awaiting_peer_completion_window_count} awaiting complete peer coverage, "
        f"{specificity.off_grid_window_count} off the fixed-phase grid, "
        f"{specificity.inconsistent_window_count} inconsistent, "
        f"{specificity.mixed_origin_window_count} mixed-origin"
        + (
            "; HORIZON_DAYS_HETEROGENEOUS (no window can be accepted)"
            if specificity.horizon_days_heterogeneous
            else ""
        )
        + "."
    )
    descriptive = (
        f" Descriptive (ungated) equal-weighted means over "
        f"{specificity.descriptive_informative_window_count} informative windows: cohort "
        f"window-mean {specificity.descriptive_mean_cohort_window_return}, complement window-mean "
        f"{specificity.descriptive_mean_complement_return}, window spread "
        f"{specificity.descriptive_mean_window_spread}; raw observation-weighted mean "
        f"{specificity.descriptive_mean_raw_return} over "
        f"{specificity.raw_return_observation_count} observations (same-day duplicate decisions "
        "counted per observation, not comparable to the window-level figures)."
    )
    first = specificity.first_window_start
    last = specificity.last_window_end
    span = (
        f"{canonical_timestamp(first)} to {canonical_timestamp(last)}"
        if first is not None and last is not None
        else "no accepted window"
    )
    if qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED:
        return (
            f"{header} PEER-RELATIVE, WINDOW-LEVEL: mean gate-complement spread "
            f"{specificity.mean_relative_window_return} (median "
            f"{specificity.median_relative_window_return}, sample SD "
            f"{specificity.relative_window_return_standard_deviation}), positive-window rate "
            f"{specificity.relative_positive_window_rate} over "
            f"{specificity.independent_window_count} independent economic windows "
            f"({specificity.window_horizon_days}-day, {span}) "
            f"of {specificity.raw_return_observation_count} raw observations. {exclusions}"
            f"{descriptive} A positive spread means the cohort outperformed the same-method, "
            "same-origin products its gate did not reject; this is a research-priority sign "
            "with a scale, never an expected return."
        )
    return (
        f"{header} PEER-RELATIVE, WINDOW-LEVEL: {specificity.independent_window_count} "
        f"independent economic window(s) is below the established minimum of "
        f"{COUNTERFACTUAL_OUTCOME_MINIMUM_INDEPENDENT_WINDOWS} required before the "
        f"gate-complement spread is treated as qualified evidence "
        f"({specificity.raw_return_observation_count} raw observations). {exclusions}{descriptive}"
    )


def build_counterfactual_outcome_aggregates(
    *,
    decisions: tuple[CounterfactualDecision, ...],
    classifications: tuple[CounterfactualDecisionClassification, ...],
    observations: tuple[DecisionFollowUpObservation, ...],
    assessments: tuple[FollowUpEvidenceQualityAssessment, ...],
    strategy_or_method_id_by_projection: Mapping[str, str],
    run_id: str,
    generated_at: datetime,
    classifier_version: str = CURRENT_CLASSIFIER_VERSION,
    evidence_quality_policy_version: str = FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    outcome_policy_version: str = COUNTERFACTUAL_OUTCOME_POLICY_V1,
    kalshi_baseline_quotes: tuple[KalshiBaselineQuoteEvidence, ...] = (),
    subject_facts_by_projection: Mapping[str, SubjectProjectionFacts] | None = None,
) -> tuple[CounterfactualOutcomeAggregate, ...]:
    """Pure, deterministic aggregation. Performs no I/O; every argument is
    already-retained/already-immutable evidence the caller (application
    layer) is responsible for reading. Returns one aggregate per
    non-empty cohort that genuinely occurs in the retained evidence -- never
    a materialized cross product of every hypothetically possible
    combination (Phase 2's cohorts are discovered from real data, not
    enumerated from the type system).

    A decision contributes to a cohort only if it (a) resolves a
    ``strategy_or_method_id`` via its ``subject_projection_id`` (always
    true for a genuinely retained decision; a failure here is a referential-
    integrity violation, raised rather than silently skipped) and (b) has
    been classified under exactly ``classifier_version`` (a decision the
    ledger has synced but classification sync has not yet reached simply
    does not contribute to any reason-family cohort this run -- an honest,
    self-healing gap, not an error, since classification sync is itself
    idempotent and catches up on a later cycle).

    ``kalshi_baseline_quotes`` is ignored entirely under
    ``COUNTERFACTUAL_OUTCOME_POLICY_V1`` (its default, empty, changes
    nothing about V1's own byte-for-byte behavior). Under
    ``COUNTERFACTUAL_OUTCOME_POLICY_V2`` it is reduced, per matured KALSHI
    ``BinaryComplementEvaluation`` observation, via
    ``domain.kalshi_baseline_quote.reduce_kalshi_binary_complement_outcome``
    into ``kalshi_returns_by_observation_id`` -- keyed by
    ``DecisionFollowUpObservation.observation_id`` rather than by
    ``decision_id`` alone, because one decision can retain more than one
    follow-up observation (distinct horizons), and a Kalshi market's
    settlement result can genuinely differ between an earlier, not-yet-
    settled checkpoint and a later, settled one for the identical decision."""

    if outcome_policy_version not in KNOWN_COUNTERFACTUAL_OUTCOME_POLICY_VERSIONS:
        raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_POLICY_VERSION_UNKNOWN")
    if classifier_version not in KNOWN_CLASSIFIER_VERSIONS:
        raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_CLASSIFIER_VERSION_UNKNOWN")
    if evidence_quality_policy_version != FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1:
        raise CounterfactualOutcomeError(
            "COUNTERFACTUAL_OUTCOME_EVIDENCE_QUALITY_POLICY_VERSION_UNKNOWN"
        )
    if not run_id:
        raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_RUN_ID_INVALID")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_GENERATED_AT_INVALID")
    is_v3 = outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V3
    if is_v3:
        # Specificity v3 consumes only the observations that carry a declared
        # forward window (``baseline_time`` = W0, ``target_time`` = W1): CRYPTO
        # FOLLOWUP_POLICY_V3 rows.  Quarantined CRYPTO V1/V2 rows have no such
        # semantics and are structurally, not merely by policy, excluded.
        observations = tuple(
            observation
            for observation in observations
            if observation.venue is SharedVenue.CRYPTO
            and observation.policy_version == FOLLOWUP_POLICY_V3
        )
        if subject_facts_by_projection is None:
            raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_SUBJECT_FACTS_REQUIRED")

    classification_by_decision: dict[str, CounterfactualDecisionClassification] = {
        item.decision_id: item
        for item in classifications
        if item.classifier_version == classifier_version
    }
    assessment_by_observation: dict[str, FollowUpEvidenceQualityAssessment] = {
        item.observation_id: item
        for item in assessments
        if item.assessment_policy_version == evidence_quality_policy_version
    }
    decision_by_id: dict[str, CounterfactualDecision] = {
        decision.decision_id: decision for decision in decisions
    }
    peer_windows: Mapping[_WindowKey, ContemporaneousPeerWindow] = MappingProxyType({})
    membership_index: Mapping[_MembershipKey, tuple[_Membership, ...]] = MappingProxyType({})
    unattributed_by_method: Mapping[str, int] = MappingProxyType({})
    if is_v3:
        peer_windows = build_contemporaneous_peer_windows(
            observations=observations, assessment_by_observation=assessment_by_observation
        )
        membership_index, unattributed_by_method = _membership_index(
            decisions=decisions,
            classification_by_decision=classification_by_decision,
            subject_facts_by_projection=subject_facts_by_projection or {},
        )
    kalshi_returns_by_observation_id: dict[str, Decimal] = {}
    if outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V2:
        baseline_quote_by_decision_id = {
            quote.decision_id: quote for quote in kalshi_baseline_quotes
        }
        for observation in observations:
            if observation.venue is not SharedVenue.KALSHI:
                continue
            source_decision = decision_by_id.get(observation.decision_id)
            if source_decision is None or not _decision_is_economically_reducible(
                venue=source_decision.venue,
                subject_source_record_type=source_decision.subject_source_record_type,
                outcome_policy_version=outcome_policy_version,
            ):
                continue
            baseline_quote = baseline_quote_by_decision_id.get(observation.decision_id)
            if baseline_quote is None:
                continue
            reduced = reduce_kalshi_binary_complement_outcome(
                baseline=baseline_quote, observation=observation
            )
            if reduced is not None:
                kalshi_returns_by_observation_id[observation.observation_id] = reduced

    observation_by_key: dict[tuple[str, str, str, str], DecisionFollowUpObservation] = {}
    triples_by_venue: dict[
        SharedVenue, set[tuple[FollowUpHorizonKind, FollowUpBaselineKind, str]]
    ] = defaultdict(set)
    for observation in observations:
        # ``uq_decision_followup_identity`` (migration 0097) guarantees this
        # key is already unique at the database boundary; no dedupe logic
        # is needed here beyond the plain assignment.
        observation_by_key[
            (
                observation.decision_id,
                observation.horizon_kind.value,
                observation.baseline_kind.value,
                observation.policy_version,
            )
        ] = observation
        triples_by_venue[observation.venue].add(
            (observation.horizon_kind, observation.baseline_kind, observation.policy_version)
        )

    super_cohorts: dict[tuple[SharedVenue, str, ReasonFamily], list[CounterfactualDecision]] = (
        defaultdict(list)
    )
    for decision in decisions:
        strategy_or_method_id = strategy_or_method_id_by_projection.get(
            decision.subject_projection_id
        )
        if strategy_or_method_id is None:
            raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_PROJECTION_MISSING")
        if not _cohort_venue_is_in_scope_for_policy_version(
            venue=decision.venue, outcome_policy_version=outcome_policy_version
        ):
            continue
        classification = classification_by_decision.get(decision.decision_id)
        if classification is None:
            continue
        if is_v3:
            # Canonical gate attribution: exactly one cohort per decision, or
            # none for an unattributed gate-passer (which still joins
            # complements -- see ``_membership_index``).
            canonical = canonical_reason_family(classification.reason_families)
            families: tuple[ReasonFamily, ...] = () if canonical is None else (canonical,)
        else:
            families = classification.reason_families
        for reason_family in families:
            super_cohorts[(decision.venue, strategy_or_method_id, reason_family)].append(decision)

    aggregates: list[CounterfactualOutcomeAggregate] = []
    for (venue, strategy_or_method_id, reason_family), cohort_decisions in super_cohorts.items():
        triples = sorted(
            triples_by_venue.get(venue, ()),
            key=lambda triple: (triple[0].value, triple[1].value, triple[2]),
        )
        for horizon_kind, baseline_kind, followup_policy_version in triples:
            aggregates.append(
                _build_cohort_aggregate(
                    venue=venue,
                    strategy_or_method_id=strategy_or_method_id,
                    reason_family=reason_family,
                    horizon_kind=horizon_kind,
                    baseline_kind=baseline_kind,
                    followup_policy_version=followup_policy_version,
                    cohort_decisions=tuple(cohort_decisions),
                    observation_by_key=observation_by_key,
                    assessment_by_observation=assessment_by_observation,
                    run_id=run_id,
                    generated_at=generated_at,
                    classifier_version=classifier_version,
                    evidence_quality_policy_version=evidence_quality_policy_version,
                    outcome_policy_version=outcome_policy_version,
                    kalshi_returns_by_observation_id=kalshi_returns_by_observation_id,
                    classification_by_decision=classification_by_decision,
                    peer_windows=peer_windows,
                    membership_index=membership_index,
                    method_unattributed_decision_count=unattributed_by_method.get(
                        strategy_or_method_id, 0
                    ),
                )
            )
    return tuple(sorted(aggregates, key=lambda item: item.aggregate_id))


def _build_cohort_aggregate(
    *,
    venue: SharedVenue,
    strategy_or_method_id: str,
    reason_family: ReasonFamily,
    horizon_kind: FollowUpHorizonKind,
    baseline_kind: FollowUpBaselineKind,
    followup_policy_version: str,
    cohort_decisions: tuple[CounterfactualDecision, ...],
    observation_by_key: Mapping[tuple[str, str, str, str], DecisionFollowUpObservation],
    assessment_by_observation: Mapping[str, FollowUpEvidenceQualityAssessment],
    run_id: str,
    generated_at: datetime,
    classifier_version: str,
    evidence_quality_policy_version: str,
    outcome_policy_version: str,
    kalshi_returns_by_observation_id: Mapping[str, Decimal],
    classification_by_decision: Mapping[str, CounterfactualDecisionClassification] | None = None,
    peer_windows: Mapping[_WindowKey, ContemporaneousPeerWindow] | None = None,
    membership_index: Mapping[_MembershipKey, tuple[_Membership, ...]] | None = None,
    method_unattributed_decision_count: int = 0,
) -> CounterfactualOutcomeAggregate:
    is_v3 = outcome_policy_version == COUNTERFACTUAL_OUTCOME_POLICY_V3
    total = len(cohort_decisions)
    mature = 0
    eligible = 0
    evidence_ineligible = 0
    unassessed = 0
    contributing_decision_ids: list[str] = []
    returns: list[Decimal] = []
    cohort_observations: list[DecisionFollowUpObservation] = []
    # Every decision in this cohort shares the identical ``strategy_or_
    # method_id`` (the cohort's own partition key), which -- for every
    # currently-integrated origin -- deterministically implies the identical
    # ``subject_source_record_type`` too. A cohort is therefore always
    # homogeneously either structurally reducible or not; ``any(...)`` over
    # its decisions is a safe, simple way to answer that once rather than
    # per-decision, without depending on that homogeneity being exact.
    cohort_structurally_reducible = any(
        _decision_is_economically_reducible(
            venue=venue,
            subject_source_record_type=decision.subject_source_record_type,
            outcome_policy_version=outcome_policy_version,
        )
        for decision in cohort_decisions
    )
    for decision in sorted(cohort_decisions, key=lambda item: item.decision_id):
        observation = observation_by_key.get(
            (decision.decision_id, horizon_kind.value, baseline_kind.value, followup_policy_version)
        )
        if observation is None:
            continue
        mature += 1
        assessment = assessment_by_observation.get(observation.observation_id)
        if assessment is None:
            # Fail-closed, matching ``is_evidence_eligible_for_downstream_
            # attribution(None) is False`` exactly -- tracked as its own
            # honest category (evidence exists but has not yet been
            # retroactively assessed) rather than folded silently into
            # "ineligible", which would imply a defect was found rather
            # than "not examined yet".
            unassessed += 1
            continue
        if not is_evidence_eligible_for_downstream_attribution(assessment):
            evidence_ineligible += 1
            continue
        eligible += 1
        contributing_decision_ids.append(decision.decision_id)
        if not _decision_is_economically_reducible(
            venue=venue,
            subject_source_record_type=decision.subject_source_record_type,
            outcome_policy_version=outcome_policy_version,
        ):
            continue
        if venue is SharedVenue.CRYPTO:
            # Untouched by this milestone: identical read, identical value,
            # under every known policy version.
            if observation.subsequent_return is not None:
                returns.append(observation.subsequent_return)
                cohort_observations.append(observation)
        else:
            reduced = kalshi_returns_by_observation_id.get(observation.observation_id)
            if reduced is not None:
                returns.append(reduced)
    unavailable = total - mature

    # Raw sample composition (how many of the eligible, economically-
    # quantified observations were positive/negative/zero) is honest
    # provenance -- retained regardless of qualification state, exactly
    # like ``eligible_observation_count`` itself. The *derived* statistics
    # below (mean, median, win rate) are a different kind of claim -- an
    # implied "this is the expected/reliable outcome" -- and are gated on
    # ``QUALIFIED`` only, mirroring the established Apex convention this
    # milestone reuses the sample floor from: ``domain.crypto_universe``'s
    # own walk-forward calibration leaves ``expected``/``expected_net``
    # (etc.) as ``None`` below ``RELATIVE_STRENGTH_MINIMUM_SAMPLES`` rather
    # than reporting a number computed from too few samples with only a
    # advisory flag attached.
    positive_count = sum(1 for value in returns if value > 0)
    negative_count = sum(1 for value in returns if value < 0)
    zero_count = sum(1 for value in returns if value == 0)

    specificity: CounterfactualOutcomeSpecificity | None = None
    contributing = tuple(sorted(contributing_decision_ids))
    if is_v3:
        build = _build_specificity(
            strategy_or_method_id=strategy_or_method_id,
            reason_family=reason_family,
            baseline_kind=baseline_kind,
            cohort_decisions=cohort_decisions,
            cohort_observations=cohort_observations,
            classification_by_decision=classification_by_decision or {},
            peer_windows=peer_windows or {},
            membership_index=membership_index or {},
            method_unattributed_decision_count=method_unattributed_decision_count,
        )
        specificity = build.specificity
        # Under V3 the contributing decisions are exactly those on the
        # cohort side of an accepted independent window -- the decisions
        # the qualified statistic is actually computed from -- never every
        # eligible decision, which would overstate ``robust_scope_count``
        # downstream.
        contributing = build.contributing_decision_ids

    if not cohort_structurally_reducible:
        qualification_state = (
            CounterfactualOutcomeQualificationState.VENUE_ECONOMIC_REDUCTION_UNAVAILABLE
        )
    elif is_v3:
        qualification_state = (
            CounterfactualOutcomeQualificationState.QUALIFIED
            if build.qualified
            else CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE
        )
    elif len(returns) >= COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES:
        qualification_state = CounterfactualOutcomeQualificationState.QUALIFIED
    else:
        qualification_state = CounterfactualOutcomeQualificationState.INSUFFICIENT_SAMPLE

    if qualification_state is CounterfactualOutcomeQualificationState.QUALIFIED and not is_v3:
        mean_return = _mean(returns)
        median_return = _median(returns)
        # Quantized exactly like ``mean``/``median`` immediately above (not
        # left as a raw division): an unquantized ratio can carry more
        # significant digits than ``domain.values.canonical_decimal`` (the
        # ``canonical_hash`` formula's own dependency) permits -- e.g.
        # 10/30 needs 28 digits under Python's default Decimal context, one
        # more than 18-digit ``MAX_PRECISION`` allows -- which would make
        # hashing (and therefore persisting) a perfectly legitimate
        # qualified aggregate raise, not merely look imprecise. Mirrors
        # ``domain.securities_structure_outcome``'s identical fix for this
        # identical shape.
        positive_rate = (Decimal(positive_count) / Decimal(len(returns))).quantize(
            _RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
        )
    else:
        mean_return = None
        median_return = None
        positive_rate = None

    cohort_id = counterfactual_outcome_cohort_id(
        outcome_policy_version=outcome_policy_version,
        venue=venue.value,
        strategy_or_method_id=strategy_or_method_id,
        reason_family=reason_family.value,
        horizon_kind=horizon_kind.value,
        baseline_kind=baseline_kind.value,
        followup_policy_version=followup_policy_version,
        classifier_version=classifier_version,
        evidence_quality_policy_version=evidence_quality_policy_version,
    )
    aggregate_id = counterfactual_outcome_aggregate_id(cohort_id=cohort_id, run_id=run_id)
    explanation = _explanation(
        venue=venue,
        strategy_or_method_id=strategy_or_method_id,
        reason_family=reason_family,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        followup_policy_version=followup_policy_version,
        total_observations_considered=total,
        mature_observation_count=mature,
        eligible_observation_count=eligible,
        qualification_state=qualification_state,
        mean_subsequent_return=mean_return,
        positive_rate=positive_rate,
        specificity=specificity,
    )
    canonical_hash = counterfactual_outcome_aggregate_canonical_hash(
        aggregate_id=aggregate_id,
        cohort_id=cohort_id,
        outcome_policy_version=outcome_policy_version,
        run_id=run_id,
        venue=venue.value,
        strategy_or_method_id=strategy_or_method_id,
        reason_family=reason_family.value,
        horizon_kind=horizon_kind.value,
        baseline_kind=baseline_kind.value,
        followup_policy_version=followup_policy_version,
        classifier_version=classifier_version,
        evidence_quality_policy_version=evidence_quality_policy_version,
        total_observations_considered=total,
        mature_observation_count=mature,
        eligible_observation_count=eligible,
        evidence_ineligible_observation_count=evidence_ineligible,
        unassessed_observation_count=unassessed,
        unavailable_observation_count=unavailable,
        positive_outcome_count=positive_count,
        negative_outcome_count=negative_count,
        zero_outcome_count=zero_count,
        mean_subsequent_return=mean_return,
        median_subsequent_return=median_return,
        positive_rate=positive_rate,
        qualification_state=qualification_state.value,
        contributing_decision_ids=contributing,
        explanation=explanation,
        specificity=specificity,
    )
    return CounterfactualOutcomeAggregate(
        aggregate_id=aggregate_id,
        cohort_id=cohort_id,
        outcome_policy_version=outcome_policy_version,
        run_id=run_id,
        venue=venue,
        strategy_or_method_id=strategy_or_method_id,
        reason_family=reason_family,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        followup_policy_version=followup_policy_version,
        classifier_version=classifier_version,
        evidence_quality_policy_version=evidence_quality_policy_version,
        generated_at=generated_at,
        total_observations_considered=total,
        mature_observation_count=mature,
        eligible_observation_count=eligible,
        evidence_ineligible_observation_count=evidence_ineligible,
        unassessed_observation_count=unassessed,
        unavailable_observation_count=unavailable,
        positive_outcome_count=positive_count,
        negative_outcome_count=negative_count,
        zero_outcome_count=zero_count,
        mean_subsequent_return=mean_return,
        median_subsequent_return=median_return,
        positive_rate=positive_rate,
        qualification_state=qualification_state,
        contributing_decision_ids=contributing,
        explanation=explanation,
        canonical_hash=canonical_hash,
        specificity=specificity,
        authority="NONE",
    )
