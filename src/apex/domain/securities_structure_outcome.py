"""Securities Structure Forward Performance v1.

This module owns no provider client, credential, network, execution, or
capital-mutation capability, and it introduces no new investment judgment.
Like ``domain.counterfactual_outcome`` (Counterfactual Outcome Feedback v1),
it is an aggregate-evidence layer above the already-retained Counterfactual
Decision Ledger (``domain.counterfactual_ledger``) and its follow-up
observation / evidence-quality subsystem (``domain.counterfactual_followup``):

    CounterfactualDecision (SECURITIES, ADVANCED)
    -> DecisionFollowUpObservation (SECURITIES_STRUCTURE_TERMINAL_PAYOFF)
    -> FollowupEvidenceQualityAssessment
    -> (this module) versioned aggregate forward-performance evidence
    -> Research Operations fact (adapters.research_operations_sources)

Why this is a *separate* module from ``domain.counterfactual_outcome``,
not an additive policy version bolted onto it: that module's own docstring
is precise about what it answers -- "would a path Apex did *not* take have
done well" -- and its cohort partition key (``reason_family``, drawn from
the 12-family rejection taxonomy) is structurally about *rejected/blocked*
decisions. ``evaluate_eligibility`` stamps every genuinely ``ELIGIBLE``
decision's only raw reason code as ``ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY``
-- a fact confirmed by direct inspection of the live retained SECURITIES
corpus during this milestone (270/270 retained SECURITIES eligibility
decisions are ``ADVANCED``, zero are ``REJECTED``) -- and both
``REASON_CLASSIFIER_V1`` and ``..._V2`` deliberately exclude that marker
from classification (``_V1_EXCLUDED_MARKERS``/``_V2_EXCLUDED_MARKERS``):
an ``ADVANCED`` decision always classifies to zero reason families and so
can never enter any ``domain.counterfactual_outcome`` cohort, for *any*
venue -- this is cross-venue, pre-existing, intentional behavior, not a
SECURITIES-specific gap. Reusing that module's reason-family cohort for
SECURITIES would therefore be structurally inert: it would never fire
against the real, currently-retained SECURITIES corpus, which is
100% ``ADVANCED``. This module asks the complementary, and for SECURITIES
today the *only applicable*, question -- "how are the structures Apex
*did* surface to research actually performing, grouped by structural
family" -- using the identical, already-genuine, already-computed
per-decision evidence (``DecisionFollowUpObservation.subsequent_return``,
built by ``domain.counterfactual_followup.
build_securities_structure_terminal_observation`` from real strike/spot
terminal-payoff math) that a SECURITIES follow-up already retains.

What this module produces, and what it deliberately does not (mirrors
``domain.counterfactual_outcome``'s own list, restated here so this module
is independently auditable without cross-reading that one):

* It produces only **aggregate** evidence over a bounded, explicitly
  partitioned cohort of decisions -- never a per-decision verdict. A sound
  research candidate may have a bad stochastic outcome and a mediocre one
  may get lucky; nothing here ever labels one retained
  ``CounterfactualDecision`` "correct", "wrong", "good", or "bad" from its
  realized outcome.
* It does not tune a strategy, adjust a parameter, mutate a mandate,
  mutate an allocation, change execution/PAPER/LIVE authority, feed
  ``prioritize()`` or any other ranking input, or train anything. Every
  record this module produces carries ``authority = "NONE"``.
* It computes nothing from scratch: eligible economic evidence is exactly
  the evidence that already passes the existing, centralized
  ``is_evidence_eligible_for_downstream_attribution`` gate
  (``domain.counterfactual_followup``) -- that gate's logic is never
  reproduced or re-implemented here, only called. ``subsequent_return`` is
  read verbatim from the retained observation; no return is ever
  recomputed, adjusted, or re-derived here.
* Unlike ``domain.counterfactual_outcome``, no venue-economic-reduction
  gate is needed: every retained SECURITIES follow-up observation already
  carries a genuine, directly-usable, capital-anchored
  ``subsequent_return`` (structure net profit/loss over capital required)
  for every one of the four supported vertical-spread structure types --
  there is no SECURITIES analogue of Kalshi's "no baseline entry price"
  problem. Only two qualification states exist here (``INSUFFICIENT_
  SAMPLE``/``QUALIFIED``); a third, permanently-unavailable state would
  misrepresent a genuinely reducible venue as structurally blocked.

Partitioning discipline (mirrors ``domain.counterfactual_outcome`` Phase
2/3 exactly, substituting ``structure_type`` for that module's
``reason_family``): an aggregate is identified by a cohort --
``(strategy_or_method_id, structure_type, horizon_kind, baseline_kind,
followup_policy_version)`` -- never coarser. A ``SHORT_HORIZON`` (1-day
mark-to-model) return and a ``STRATEGY_HORIZON`` (at-the-structure's-own-
DTE mark-to-model) return are not the same economic quantity even for the
identical decision, exactly as CRYPTO's own baseline-kind correlation risk
is disclosed in ``domain.counterfactual_outcome`` -- partitioning by
``horizon_kind``/``baseline_kind``/``followup_policy_version`` prevents
them from ever being summed into one cell.

Structure-type is deliberately the *only* structural partition dimension
in this first version -- not also DTE, moneyness, or underlying symbol.
The retained corpus (270 structures, 18 underlyings, 6 DTE values, 4
structure types skewed 82% ``CALL_CREDIT_VERTICAL``) supports a genuine,
non-thin structure-type comparison today; a finer partition would mostly
manufacture near-empty cells this early, which is why it is left for a
future, additively-versioned policy once real evidence shows it is
warranted -- never invented ahead of the data that would justify it.

Policy identifier: ``SECURITIES_STRUCTURE_OUTCOME_POLICY_V1``. Frozen,
exactly like every other versioned policy in this codebase
(``REASON_CLASSIFIER_V1``, ``COUNTERFACTUAL_OUTCOME_POLICY_V1``, ...): a
later semantic correction becomes an additive ``..._V2`` sitting beside
this one, never a mutation of an already-retained aggregate's meaning.
Every ``SecuritiesStructureOutcomeAggregate`` this module builds is pure
and deterministic from already-immutable/already-versioned retained
inputs: two runs over byte-identical decisions/observations/assessments
produce byte-identical ``canonical_hash`` values (``generated_at`` -- a
run's own wall-clock stamp -- is deliberately excluded from the hash,
mirroring every other aggregate-canonical-hash formula in this codebase,
so a genuine crash-resume replay under the same ``run_id`` at a different
wall-clock instant never manufactures a spurious identity conflict).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from apex.domain.counterfactual_followup import (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
    DecisionFollowUpObservation,
    FollowUpBaselineKind,
    FollowUpEvidenceQualityAssessment,
    FollowUpHorizonKind,
    is_evidence_eligible_for_downstream_attribution,
)
from apex.domain.counterfactual_ledger import (
    CounterfactualDecision,
    DecisionDisposition,
    DecisionOrigin,
)
from apex.domain.shared_opportunity import STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES, SharedVenue
from apex.domain.values import content_digest, deterministic_id

SECURITIES_STRUCTURE_OUTCOME_POLICY_V1 = "SECURITIES_STRUCTURE_OUTCOME_POLICY_V1"
KNOWN_SECURITIES_STRUCTURE_OUTCOME_POLICY_VERSIONS = (SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,)

# Securities Follow-Up Temporal Integrity v1.  The evidence-quality verdicts
# an aggregate may be built from.  V1 (provenance only) remains readable so
# retained V1 cohorts can be recomputed/audited exactly; V2 (provenance plus
# terminal-evidence contemporaneity) is the canonical SECURITIES verdict and
# the only one the recurring cycle and Research Operations use from now on.
# ``evidence_quality_policy_version`` is part of the cohort id, so a V2
# aggregate is always a distinct cohort -- no V1 row is ever superseded in
# place.
KNOWN_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSIONS = (
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2,
)
CURRENT_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSION = FOLLOWUP_EVIDENCE_QUALITY_POLICY_V2

SECURITIES_STRUCTURE_OUTCOME_WARNING = (
    "SECURITIES STRUCTURE FORWARD PERFORMANCE — AGGREGATE EVIDENCE ONLY — "
    "NO PER-DECISION VERDICT — NOT HINDSIGHT JUDGMENT — "
    "NO ALLOCATION, MANDATE, RANKING, OR EXECUTION AUTHORITY"
)

# Reuses, rather than reinvents, the one existing minimum-sample floor
# already established by crypto walk-forward calibration and Evidence-
# Weighted Ranking v1 (``domain.shared_opportunity``) and already reused by
# ``domain.counterfactual_outcome.COUNTERFACTUAL_OUTCOME_MINIMUM_SAMPLES``
# under the identical name. Imported directly from ``shared_opportunity``
# (the venue-neutral original) rather than from ``counterfactual_outcome``,
# so this module has no dependency on that sibling module at all.
SECURITIES_STRUCTURE_OUTCOME_MINIMUM_SAMPLES = STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES

_RETURN_QUANTUM = Decimal("0.00000001")


class SecuritiesStructureOutcomeError(ValueError):
    """Raised when retained securities-structure-outcome inputs are invalid,
    reference an unknown policy version, or a decision's subject projection
    cannot be resolved (a genuine referential-integrity violation -- every
    ``CounterfactualDecision.subject_projection_id`` must resolve, since the
    decision itself could only have been built by resolving that same
    projection)."""


# Counterfactual Outcome Refresh Recovery v1: named exactly like
# ``domain.counterfactual_outcome.COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT``,
# for the identical reason -- this sibling module shares the same
# same-``run_id``-retry-with-maturing-evidence exposure (production-confirmed
# 2026-09-02: 2 of that day's 8 affected cohorts were SECURITIES ones), fixed
# the identical way in ``application.securities_structure_outcome``.
SECURITIES_STRUCTURE_OUTCOME_IDENTITY_CONFLICT = "SECURITIES_STRUCTURE_OUTCOME_IDENTITY_CONFLICT"


class SecuritiesStructureOutcomeQualificationState(StrEnum):
    """What, if anything, a cohort's aggregate evidence supports concluding.

    Unlike ``domain.counterfactual_outcome.
    CounterfactualOutcomeQualificationState``, there is no venue-economic-
    reduction-unavailable state here: every retained SECURITIES structure
    follow-up observation already carries a genuine, directly usable
    ``subsequent_return`` (see this module's own docstring), so the only
    axis of variation is sample size."""

    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    QUALIFIED = "QUALIFIED"


@dataclass(frozen=True, slots=True)
class SecuritiesStructureOutcomeAggregate:
    """One immutable, append-only aggregate rollup over one fully-specified
    cohort of retained SECURITIES structure forward-performance evidence, as
    of one recurring-cycle run. Never a per-decision verdict; see the module
    docstring."""

    aggregate_id: str
    cohort_id: str
    outcome_policy_version: str
    run_id: str
    strategy_or_method_id: str
    structure_type: str
    horizon_kind: FollowUpHorizonKind
    baseline_kind: FollowUpBaselineKind
    followup_policy_version: str
    evidence_quality_policy_version: str
    generated_at: datetime
    total_decisions_considered: int
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
    qualification_state: SecuritiesStructureOutcomeQualificationState
    contributing_decision_ids: tuple[str, ...]
    explanation: str
    canonical_hash: str
    authority: str = "NONE"


def securities_structure_outcome_cohort_id(
    *,
    outcome_policy_version: str,
    strategy_or_method_id: str,
    structure_type: str,
    horizon_kind: FollowUpHorizonKind | str,
    baseline_kind: FollowUpBaselineKind | str,
    followup_policy_version: str,
    evidence_quality_policy_version: str,
) -> str:
    """Stable across every recurring-cycle run: a pure function of only the
    cohort's own structural partition key, never of ``run_id`` or any
    computed statistic -- mirrors ``domain.counterfactual_outcome.
    counterfactual_outcome_cohort_id`` exactly, substituting ``structure_
    type`` for that function's ``venue``/``reason_family``."""

    return deterministic_id(
        "securities_structure_outcome_cohort",
        (
            outcome_policy_version,
            strategy_or_method_id,
            structure_type,
            horizon_kind,
            baseline_kind,
            followup_policy_version,
            evidence_quality_policy_version,
        ),
    )


def securities_structure_outcome_aggregate_id(*, cohort_id: str, run_id: str) -> str:
    """One row per ``(cohort_id, run_id)`` -- mirrors ``domain.
    counterfactual_outcome.counterfactual_outcome_aggregate_id``'s own
    idiom: a caller-supplied, cycle-scoped ``run_id`` is the versioning axis
    across time, so replaying the identical cycle reproduces the identical
    id (idempotent), while each new cycle's ``run_id`` produces a fresh,
    append-only row -- never a mutation of an earlier one in place."""

    return deterministic_id("securities_structure_outcome_aggregate", (cohort_id, run_id))


def securities_structure_outcome_aggregate_canonical_hash(
    *,
    aggregate_id: str,
    cohort_id: str,
    outcome_policy_version: str,
    run_id: str,
    strategy_or_method_id: str,
    structure_type: str,
    horizon_kind: FollowUpHorizonKind | str,
    baseline_kind: FollowUpBaselineKind | str,
    followup_policy_version: str,
    evidence_quality_policy_version: str,
    total_decisions_considered: int,
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
    qualification_state: SecuritiesStructureOutcomeQualificationState | str,
    contributing_decision_ids: tuple[str, ...],
    explanation: str,
) -> str:
    """The ``SecuritiesStructureOutcomeAggregate.canonical_hash`` formula,
    exposed (not underscore-prefixed) exactly like ``domain.
    counterfactual_outcome.counterfactual_outcome_aggregate_canonical_hash``,
    so a future migration or diagnostic can recompute it byte-for-byte from
    a row's own already-persisted fields. ``generated_at`` is deliberately
    excluded -- see the module docstring."""

    payload = (
        outcome_policy_version,
        aggregate_id,
        cohort_id,
        run_id,
        strategy_or_method_id,
        structure_type,
        horizon_kind,
        baseline_kind,
        followup_policy_version,
        evidence_quality_policy_version,
        total_decisions_considered,
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
    strategy_or_method_id: str,
    structure_type: str,
    horizon_kind: FollowUpHorizonKind,
    baseline_kind: FollowUpBaselineKind,
    followup_policy_version: str,
    total_decisions_considered: int,
    mature_observation_count: int,
    eligible_observation_count: int,
    qualification_state: SecuritiesStructureOutcomeQualificationState,
    mean_subsequent_return: Decimal | None,
    positive_rate: Decimal | None,
) -> str:
    header = (
        f"SECURITIES/{strategy_or_method_id}/{structure_type} "
        f"({horizon_kind.value}, {baseline_kind.value}, {followup_policy_version}): "
        f"{mature_observation_count}/{total_decisions_considered} decisions matured, "
        f"{eligible_observation_count} evidence-eligible."
    )
    if qualification_state is SecuritiesStructureOutcomeQualificationState.INSUFFICIENT_SAMPLE:
        return (
            f"{header} {eligible_observation_count} eligible sample(s) is below the "
            f"established minimum of {SECURITIES_STRUCTURE_OUTCOME_MINIMUM_SAMPLES} "
            "independently partitioned observations required before mean/median/positive-"
            "rate are treated as qualified evidence."
        )
    return (
        f"{header} mean subsequent return {mean_subsequent_return}, "
        f"positive-outcome rate {positive_rate} over {eligible_observation_count} qualified "
        "samples (raw count; not adjusted for potential temporal/asset autocorrelation "
        "between structures on the same or a related underlying instrument -- a disclosed "
        "limitation, not a statistic this module corrects for)."
    )


def build_securities_structure_outcome_aggregates(
    *,
    decisions: tuple[CounterfactualDecision, ...],
    observations: tuple[DecisionFollowUpObservation, ...],
    assessments: tuple[FollowUpEvidenceQualityAssessment, ...],
    strategy_or_method_id_by_projection: Mapping[str, str],
    structure_type_by_projection: Mapping[str, str],
    run_id: str,
    generated_at: datetime,
    evidence_quality_policy_version: str = FOLLOWUP_EVIDENCE_QUALITY_POLICY_V1,
    outcome_policy_version: str = SECURITIES_STRUCTURE_OUTCOME_POLICY_V1,
) -> tuple[SecuritiesStructureOutcomeAggregate, ...]:
    """Pure, deterministic aggregation. Performs no I/O; every argument is
    already-retained/already-immutable evidence the caller (application
    layer) is responsible for reading. Returns one aggregate per non-empty
    cohort that genuinely occurs in the retained evidence -- never a
    materialized cross product of every hypothetically possible combination.

    Only decisions that are SECURITIES-venue, ``ADVANCED``, and originate
    from ``SHARED_OPPORTUNITY_ELIGIBILITY`` contribute -- see the module
    docstring for exactly why (this is the entire currently-retained
    SECURITIES corpus; a future ``REJECTED`` SECURITIES eligibility
    decision would instead be genuine evidence for ``domain.
    counterfactual_outcome``'s own reason-family cohorts, not this one --
    the two mechanisms are complementary, not overlapping, by construction
    of the disposition/origin filter below)."""

    if outcome_policy_version not in KNOWN_SECURITIES_STRUCTURE_OUTCOME_POLICY_VERSIONS:
        raise SecuritiesStructureOutcomeError("SECURITIES_STRUCTURE_OUTCOME_POLICY_VERSION_UNKNOWN")
    if (
        evidence_quality_policy_version
        not in KNOWN_SECURITIES_STRUCTURE_EVIDENCE_QUALITY_POLICY_VERSIONS
    ):
        raise SecuritiesStructureOutcomeError(
            "SECURITIES_STRUCTURE_OUTCOME_EVIDENCE_QUALITY_POLICY_VERSION_UNKNOWN"
        )
    if not run_id:
        raise SecuritiesStructureOutcomeError("SECURITIES_STRUCTURE_OUTCOME_RUN_ID_INVALID")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise SecuritiesStructureOutcomeError("SECURITIES_STRUCTURE_OUTCOME_GENERATED_AT_INVALID")

    assessment_by_observation: dict[str, FollowUpEvidenceQualityAssessment] = {
        item.observation_id: item
        for item in assessments
        if item.assessment_policy_version == evidence_quality_policy_version
    }

    pursued_decisions = tuple(
        decision
        for decision in decisions
        if decision.venue is SharedVenue.SECURITIES
        and decision.disposition is DecisionDisposition.ADVANCED
        and decision.origin is DecisionOrigin.SHARED_OPPORTUNITY_ELIGIBILITY
    )

    observation_by_key: dict[tuple[str, str, str, str], DecisionFollowUpObservation] = {}
    triples: set[tuple[FollowUpHorizonKind, FollowUpBaselineKind, str]] = set()
    for observation in observations:
        if observation.venue is not SharedVenue.SECURITIES:
            continue
        # ``uq_decision_followup_identity`` (migration 0097) guarantees this
        # key is already unique at the database boundary; no dedupe logic
        # is needed here beyond the plain assignment (mirrors ``domain.
        # counterfactual_outcome.build_counterfactual_outcome_aggregates``).
        observation_by_key[
            (
                observation.decision_id,
                observation.horizon_kind.value,
                observation.baseline_kind.value,
                observation.policy_version,
            )
        ] = observation
        triples.add(
            (observation.horizon_kind, observation.baseline_kind, observation.policy_version)
        )

    super_cohorts: dict[tuple[str, str], list[CounterfactualDecision]] = defaultdict(list)
    for decision in pursued_decisions:
        projection_id = decision.subject_projection_id
        strategy_or_method_id = strategy_or_method_id_by_projection.get(projection_id)
        if strategy_or_method_id is None:
            raise SecuritiesStructureOutcomeError("SECURITIES_STRUCTURE_OUTCOME_PROJECTION_MISSING")
        structure_type = structure_type_by_projection.get(projection_id)
        if structure_type is None:
            raise SecuritiesStructureOutcomeError(
                "SECURITIES_STRUCTURE_OUTCOME_STRUCTURE_TYPE_MISSING"
            )
        super_cohorts[(strategy_or_method_id, structure_type)].append(decision)

    aggregates: list[SecuritiesStructureOutcomeAggregate] = []
    sorted_triples = sorted(
        triples, key=lambda triple: (triple[0].value, triple[1].value, triple[2])
    )
    for (strategy_or_method_id, structure_type), cohort_decisions in super_cohorts.items():
        for horizon_kind, baseline_kind, followup_policy_version in sorted_triples:
            aggregates.append(
                _build_structure_cohort_aggregate(
                    strategy_or_method_id=strategy_or_method_id,
                    structure_type=structure_type,
                    horizon_kind=horizon_kind,
                    baseline_kind=baseline_kind,
                    followup_policy_version=followup_policy_version,
                    cohort_decisions=tuple(cohort_decisions),
                    observation_by_key=observation_by_key,
                    assessment_by_observation=assessment_by_observation,
                    run_id=run_id,
                    generated_at=generated_at,
                    evidence_quality_policy_version=evidence_quality_policy_version,
                    outcome_policy_version=outcome_policy_version,
                )
            )
    return tuple(sorted(aggregates, key=lambda item: item.aggregate_id))


def _build_structure_cohort_aggregate(
    *,
    strategy_or_method_id: str,
    structure_type: str,
    horizon_kind: FollowUpHorizonKind,
    baseline_kind: FollowUpBaselineKind,
    followup_policy_version: str,
    cohort_decisions: tuple[CounterfactualDecision, ...],
    observation_by_key: Mapping[tuple[str, str, str, str], DecisionFollowUpObservation],
    assessment_by_observation: Mapping[str, FollowUpEvidenceQualityAssessment],
    run_id: str,
    generated_at: datetime,
    evidence_quality_policy_version: str,
    outcome_policy_version: str,
) -> SecuritiesStructureOutcomeAggregate:
    total = len(cohort_decisions)
    mature = 0
    eligible = 0
    evidence_ineligible = 0
    unassessed = 0
    contributing_decision_ids: list[str] = []
    returns: list[Decimal] = []
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
            # attribution(None) is False`` exactly -- its own honest
            # category (evidence exists but has not yet been retroactively
            # assessed), never folded silently into "ineligible".
            unassessed += 1
            continue
        if not is_evidence_eligible_for_downstream_attribution(assessment):
            evidence_ineligible += 1
            continue
        eligible += 1
        contributing_decision_ids.append(decision.decision_id)
        # No reduction step is needed here (unlike Kalshi under
        # ``domain.counterfactual_outcome``): a SECURITIES observation's
        # own ``subsequent_return`` already *is* the genuine, final,
        # capital-anchored return -- see the module docstring.
        if observation.subsequent_return is not None:
            returns.append(observation.subsequent_return)
    unavailable = total - mature

    positive_count = sum(1 for value in returns if value > 0)
    negative_count = sum(1 for value in returns if value < 0)
    zero_count = sum(1 for value in returns if value == 0)

    if len(returns) >= SECURITIES_STRUCTURE_OUTCOME_MINIMUM_SAMPLES:
        qualification_state = SecuritiesStructureOutcomeQualificationState.QUALIFIED
    else:
        qualification_state = SecuritiesStructureOutcomeQualificationState.INSUFFICIENT_SAMPLE

    if qualification_state is SecuritiesStructureOutcomeQualificationState.QUALIFIED:
        mean_return = _mean(returns)
        median_return = _median(returns)
        # Quantized exactly like ``mean``/``median`` immediately above (not
        # left as a raw division): an unquantized ratio can carry more
        # significant digits than ``domain.values.canonical_decimal`` (the
        # ``canonical_hash`` formula's own dependency) permits -- e.g.
        # 10/30 needs 28 digits under Python's default Decimal context, one
        # more than 18-digit ``MAX_PRECISION`` allows -- which would make
        # hashing (and therefore persisting) a perfectly legitimate
        # qualified aggregate raise, not merely look imprecise.
        positive_rate = (Decimal(positive_count) / Decimal(len(returns))).quantize(
            _RETURN_QUANTUM, rounding=ROUND_HALF_EVEN
        )
    else:
        mean_return = None
        median_return = None
        positive_rate = None

    cohort_id = securities_structure_outcome_cohort_id(
        outcome_policy_version=outcome_policy_version,
        strategy_or_method_id=strategy_or_method_id,
        structure_type=structure_type,
        horizon_kind=horizon_kind.value,
        baseline_kind=baseline_kind.value,
        followup_policy_version=followup_policy_version,
        evidence_quality_policy_version=evidence_quality_policy_version,
    )
    aggregate_id = securities_structure_outcome_aggregate_id(cohort_id=cohort_id, run_id=run_id)
    contributing = tuple(sorted(contributing_decision_ids))
    explanation = _explanation(
        strategy_or_method_id=strategy_or_method_id,
        structure_type=structure_type,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        followup_policy_version=followup_policy_version,
        total_decisions_considered=total,
        mature_observation_count=mature,
        eligible_observation_count=eligible,
        qualification_state=qualification_state,
        mean_subsequent_return=mean_return,
        positive_rate=positive_rate,
    )
    canonical_hash = securities_structure_outcome_aggregate_canonical_hash(
        aggregate_id=aggregate_id,
        cohort_id=cohort_id,
        outcome_policy_version=outcome_policy_version,
        run_id=run_id,
        strategy_or_method_id=strategy_or_method_id,
        structure_type=structure_type,
        horizon_kind=horizon_kind.value,
        baseline_kind=baseline_kind.value,
        followup_policy_version=followup_policy_version,
        evidence_quality_policy_version=evidence_quality_policy_version,
        total_decisions_considered=total,
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
    )
    return SecuritiesStructureOutcomeAggregate(
        aggregate_id=aggregate_id,
        cohort_id=cohort_id,
        outcome_policy_version=outcome_policy_version,
        run_id=run_id,
        strategy_or_method_id=strategy_or_method_id,
        structure_type=structure_type,
        horizon_kind=horizon_kind,
        baseline_kind=baseline_kind,
        followup_policy_version=followup_policy_version,
        evidence_quality_policy_version=evidence_quality_policy_version,
        generated_at=generated_at,
        total_decisions_considered=total,
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
        authority="NONE",
    )
