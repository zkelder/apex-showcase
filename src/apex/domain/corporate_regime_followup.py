"""Corporate Calibration-Qualified Regime Follow-Up v1.

Evaluates the single ``SPLIT_BY_REGIME`` follow-up that survived Corporate
Research Calibration Depth v1's requalification
(``application.corporate_calibration_depth.RequalifyCorporateFollowUps``):
``cross_market_follow_up_622f5a0d3af61dcb7b69bd9b``, proposed against parent
hypothesis ``corporate_market_hypothesis_6d268d63f879f96f113e2737``
(``NET_INCOME_GROWTH_YOY`` / ``SUBSEQUENT_RETURN``, 20-session horizon,
``CROSS_SECTIONAL_V1``), which reached calibrated grade ``SUGGESTIVE`` (40
matched controls, 0.95 matched percentile) and qualification
``CALIBRATION_QUALIFIED``.

This is *mechanism/stability investigation*, not independent replication,
not forward confirmation, and not strategy formalization (milestone
preamble). Every retained record carries ``authority = "NONE"`` and
``independent_confirmation_required`` is structurally pinned to ``True`` --
see :data:`POST_HOC_CALIBRATION_QUALIFIED_FOLLOWUP` and
:data:`INDEPENDENT_CONFIRMATION_REASONS`.

Strictly additive on top of five existing modules, every genuinely shared
primitive reused unmodified rather than re-derived:

* ``domain.corporate_market_research`` supplies ``CorporateFeatureId``/
  ``CorporateOutcomeId`` and ``MINIMUM_EFFECTIVE_PANEL_CLUSTERS`` (this
  follow-up's own regime-group distinct-date floor is the *same* constant
  the parent's own panel-dependence diagnostic already uses -- not a
  independently chosen number).
* ``domain.cross_market_research`` supplies ``CrossMarketRegimeDimension``
  (the existing, versioned ten-dimension regime taxonomy section 3 of the
  milestone requires this module select *from*, never invent alongside),
  ``ConditionRule``, and the time-partition stability primitives
  (``StabilityState``/``SubperiodResult``/``StabilityReport``/
  ``derive_stability_state``) reused verbatim for section 12's early/
  middle/recent partition.
* ``domain.research_calibration`` supplies ``EvidenceGrade``/
  ``EvidenceProfile``/``derive_evidence_grade``/``EVIDENCE_GRADE_POLICY_V1``
  unmodified -- this follow-up's evidence grade is derived through the
  identical ceiling logic every other corporate-research subject already
  uses, not a bespoke laxer scale.
* ``domain.research_calibration_v2`` supplies ``MatchedCalibrationResult``
  unmodified for the new, split-specific matched-null population (section
  9: "do not use the parent's unsplit 40-control percentile as sufficient
  calibration for the new split automatically").
* ``domain.research_methodology`` supplies the entire generic statistics
  vocabulary this module composes: ``BenchmarkKind``, ``RobustnessCheckType``/
  ``RobustnessCheckResult``/``RobustnessSummary``/``summarize_robustness``,
  ``SampleQualityState``, ``ResearchConclusionState``/
  ``derive_conclusion_state``, ``CorrectedTestResult``/``TrialTestResult``/
  ``apply_benjamini_hochberg_correction``.
* ``domain.corporate_calibration_depth`` supplies ``CorporateNullMechanism``
  -- this follow-up's matched-null generator reuses exactly one already-
  reviewed mechanism, ``SEEDED_ISSUER_DERANGEMENT`` (shuffle the security ->
  feature-value mapping within each date; the date/regime-membership
  structure is untouched because regime labels are a pure function of the
  date, never of any one issuer's fundamentals), rather than inventing a
  new taxonomy for a one-follow-up budget.

What is genuinely new here (no faithful representation in any of the
above): an immutable, frozen follow-up specification binding the parent's
exact semantics, train/validation/holdout boundaries, calibrated grade, and
matched-null lineage (section 1); a two-member regime-dimension-selection
record whose primary dimension is derived from the *closest structural
precedent already present in the retained v3 agenda* -- never chosen by
scanning all ten dimensions for the best-looking split (section 3); a
regime-group adequacy gate with its own named, declared-before-use floors
(section 6); a per-split null-control record whose statistic is the
*interaction* (regime-A effect minus regime-B effect), not a single-group
effect (section 9); a temporal/sector/issuer/outlier robustness bundle
computed *for the interaction*, not merely for each group in isolation
(sections 12-14); a three-way incremental-contribution decomposition
generalizing ``corporate_market_research.compute_incremental_contribution``
from a single conditioning label to a two-group split (section 15); and a
six-member follow-up conclusion vocabulary structurally distinct from, and
never conflated with, a genuine hypothesis's own ``KnowledgeStatus``
(section 18).

Non-goals (deliberate, matching this milestone's scope restrictions): no
new hypothesis agenda, no execution of any of the other twelve retained
follow-up proposals, no threshold search, no Strategy/Opportunity/PAPER/
LIVE authority, and no claim of independent confirmation from same-dataset
evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.corporate_calibration_depth import CorporateNullMechanism
from apex.domain.corporate_market_research import (
    MINIMUM_EFFECTIVE_PANEL_CLUSTERS,
    CorporateFeatureId,
    CorporateOutcomeId,
)
from apex.domain.cross_market_research import (
    ConditionRule,
    CrossMarketRegimeDimension,
    StabilityReport,
    StabilityState,
)
from apex.domain.research_calibration import EvidenceGrade
from apex.domain.research_calibration_v2 import MatchedCalibrationResult
from apex.domain.research_methodology import (
    BenchmarkKind,
    CorrectedTestResult,
    ResearchConclusionState,
    RobustnessCheckResult,
    RobustnessSummary,
    SampleQualityState,
)
from apex.domain.values import content_digest, deterministic_id

CORPORATE_REGIME_FOLLOWUP_V1 = "CORPORATE_CALIBRATION_QUALIFIED_REGIME_FOLLOWUP_V1"
CORPORATE_REGIME_FOLLOWUP_WARNING = (
    "CORPORATE REGIME FOLLOW-UP EVIDENCE ONLY -- POST-HOC MECHANISM/STABILITY INVESTIGATION OF "
    "ONE ALREADY CALIBRATION-QUALIFIED V3 FOLLOW-UP; NOT INDEPENDENT REPLICATION, NOT FORWARD "
    "CONFIRMATION, NOT STRATEGY FORMALIZATION; NO STRATEGY, OPPORTUNITY, PAPER, OR LIVE AUTHORITY"
)

# Section 2: post-hoc status is mandatory and non-negotiable -- SPLIT_BY_REGIME
# was proposed only after seeing the parent's WEAK_SUPPORT/SUGGESTIVE result,
# so this follow-up can never be represented as independent confirmation.
POST_HOC_CALIBRATION_QUALIFIED_FOLLOWUP = "POST_HOC_CALIBRATION_QUALIFIED_FOLLOWUP"

# Section 19: a successful result still requires future or independently
# sourced confirmation. Every reason below is retained verbatim on every
# genuine result this module produces, success or failure.
INDEPENDENT_CONFIRMATION_REASONS = (
    "SAME_HISTORICAL_DATASET_AS_V3_AND_CALIBRATION_DEPTH",
    "POST_HOC_FOLLOW_UP_PROPOSED_AFTER_SEEING_THE_PARENT_RESULT",
    "PRIOR_HYPOTHESIS_EXPOSURE_INCLUDING_THE_PARENTS_OWN_ALREADY_UNLOCKED_HOLDOUT",
)

# Never lowered from research_calibration_v2's own floor -- the identical
# defensive-freeze precedent corporate_calibration_depth.
# MINIMUM_MATCHED_NULL_COUNT already established for exactly the same reason.
MINIMUM_MATCHED_NULL_COUNT = 12

# Section 6's regime-group adequacy floors, declared once, before any group
# is evaluated, and never adjusted after seeing which group is thinner.
# ``REGIME_GROUP_MINIMUM_DISTINCT_DATES`` reuses
# ``corporate_market_research.MINIMUM_EFFECTIVE_PANEL_CLUSTERS`` directly
# (the same "at least eight distinct market dates" bar the parent hypothesis's
# own panel-dependence diagnostic already applies) rather than choosing an
# independent number; ``REGIME_GROUP_MINIMUM_ISSUER_COUNT`` applies that
# identical numeral along the issuer-count dimension by analogy.
REGIME_GROUP_MINIMUM_DISTINCT_DATES = MINIMUM_EFFECTIVE_PANEL_CLUSTERS
REGIME_GROUP_MINIMUM_ISSUER_COUNT = 8
REGIME_GROUP_MINIMUM_RAW_OBSERVATIONS = 40

# Section 10's fixed calibration budget, declared before any control is
# generated. Both splits share one mechanism (``SEEDED_ISSUER_DERANGEMENT``)
# and one budget so neither is quietly given more chances than the other.
PRIMARY_NULL_CONTROL_BUDGET = 30
SECONDARY_NULL_CONTROL_BUDGET = 30

# Section 11: exactly two predeclared members -- the primary EQUITY_TREND
# split and the tiny secondary EQUITY_VOLATILITY robustness split. Both are
# counted in the multiplicity family even when one is never inferentially
# evaluated because its own regime groups fail section 6 adequacy.
MULTIPLE_TESTING_FAMILY_SIZE = 2
MULTIPLE_TESTING_METHOD = "BENJAMINI_HOCHBERG_FDR"
FALSE_DISCOVERY_RATE = Decimal("0.10")

# Cohen's own conventional "small effect" threshold -- the identical
# hardcoded value ``application.corporate_market_research.
# classify_hypothesis_calibration`` already uses directly (with the same
# justification: neither ``CorporateFeatureId`` nor a regime split is an
# ``research_calibration.OutcomeFamily`` member, so
# ``EffectSizeFloorPolicy.floor_for`` does not apply).
EFFECT_SIZE_FLOOR = Decimal("0.20")

RAW_SIGNIFICANCE_ALPHA = Decimal("0.05")


class CorporateRegimeFollowUpError(ValueError):
    """A corporate regime follow-up domain invariant was violated."""


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CorporateRegimeFollowUpError(code)
    return value


def _text(value: str, code: str, *, maximum: int = 2_000) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise CorporateRegimeFollowUpError(code)
    return normalized


# ==========================================================================
# Section 3: regime dimension selection and freeze.
# ==========================================================================


class RegimeSplitRole(StrEnum):
    """Exactly one primary regime split plus at most a tiny explicitly-
    secondary robustness set (milestone section 3) -- never a third."""

    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"


@dataclass(frozen=True, slots=True)
class RegimeDimensionSelection:
    """One frozen regime-dimension choice with its two-group collapse of
    the existing versioned discrete labels (``CrossMarketRegimeDimension``'s
    own classifier output) and the deterministic precedent that justified
    it -- never a numeric threshold invented for this split."""

    split_role: RegimeSplitRole
    dimension: CrossMarketRegimeDimension
    group_a_name: str
    group_a_labels: tuple[str, ...]
    group_b_name: str
    group_b_labels: tuple[str, ...]
    excluded_labels: tuple[str, ...]
    precedent_hypothesis_id: str
    precedent_feature_id: CorporateFeatureId
    selection_rationale: str
    null_control_budget: int

    @classmethod
    def create(
        cls,
        *,
        split_role: RegimeSplitRole | str,
        dimension: CrossMarketRegimeDimension | str,
        group_a_name: str,
        group_a_labels: Sequence[str],
        group_b_name: str,
        group_b_labels: Sequence[str],
        excluded_labels: Sequence[str],
        precedent_hypothesis_id: str,
        precedent_feature_id: CorporateFeatureId | str,
        selection_rationale: str,
        null_control_budget: int,
    ) -> RegimeDimensionSelection:
        role = RegimeSplitRole(split_role)
        dim = CrossMarketRegimeDimension(dimension)
        group_a = tuple(sorted(set(group_a_labels)))
        group_b = tuple(sorted(set(group_b_labels)))
        if not group_a or not group_b:
            raise CorporateRegimeFollowUpError("REGIME_SELECTION_REQUIRES_BOTH_GROUPS_NONEMPTY")
        if set(group_a) & set(group_b):
            raise CorporateRegimeFollowUpError("REGIME_SELECTION_GROUPS_MUST_BE_DISJOINT")
        if not precedent_hypothesis_id:
            raise CorporateRegimeFollowUpError("REGIME_SELECTION_PRECEDENT_REQUIRED")
        feature = CorporateFeatureId(precedent_feature_id)
        budget = int(null_control_budget)
        if not (MINIMUM_MATCHED_NULL_COUNT <= budget <= 60):
            raise CorporateRegimeFollowUpError("REGIME_SELECTION_NULL_CONTROL_BUDGET_OUT_OF_BOUNDS")
        return cls(
            split_role=role,
            dimension=dim,
            group_a_name=_text(group_a_name, "REGIME_SELECTION_GROUP_A_NAME_INVALID", maximum=80),
            group_a_labels=group_a,
            group_b_name=_text(group_b_name, "REGIME_SELECTION_GROUP_B_NAME_INVALID", maximum=80),
            group_b_labels=group_b,
            excluded_labels=tuple(sorted(set(excluded_labels))),
            precedent_hypothesis_id=precedent_hypothesis_id,
            precedent_feature_id=feature,
            selection_rationale=_text(
                selection_rationale, "REGIME_SELECTION_RATIONALE_INVALID", maximum=2_000
            ),
            null_control_budget=budget,
        )


# ==========================================================================
# Section 1/2/4/5/10: the immutable follow-up specification. Freezes the
# parent's exact semantics; never alters the original hypothesis/conclusion/
# follow-up rows it references by id.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CorporateRegimeFollowUpSpecification:
    specification_id: str
    follow_up_id: str
    parent_hypothesis_id: str
    parent_conclusion_id: str
    parent_reassessment_id: str
    parent_qualification_id: str
    agenda_id: str

    # Section 1: exact frozen parent semantics.
    feature_id: CorporateFeatureId
    feature_transformation: str
    outcome_id: CorporateOutcomeId
    horizon_sessions: int
    benchmark_kind: BenchmarkKind
    condition_rule: ConditionRule
    market_scope: str
    cohort_policy_id: str
    corporate_dataset_id: str
    equity_snapshot_id: str
    replay_run_id: str
    cross_market_dataset_id: str
    original_train_start: str
    original_train_end: str
    original_validation_start: str
    original_validation_end: str
    original_holdout_start: str
    original_holdout_end: str
    original_train_raw_sample: int
    original_train_effective_sample: Decimal
    original_validation_raw_sample: int
    original_validation_effective_sample: Decimal
    original_holdout_raw_sample: int
    original_holdout_effective_sample: Decimal
    original_conclusion_state: str
    original_validation_beats_benchmark: bool
    original_holdout_beats_benchmark: bool
    original_validation_relative_result: Decimal

    # Calibration-depth lineage.
    original_calibrated_grade: EvidenceGrade
    matched_null_stratum: str
    matched_null_count: int
    matched_null_percentile: Decimal

    # Section 2/19.
    follow_up_kind: str
    follow_up_rationale: str
    follow_up_qualification: str
    post_hoc_status: str
    independent_confirmation_required: bool
    independent_confirmation_reasons: tuple[str, ...]

    # Section 3/10/11.
    regime_selections: tuple[RegimeDimensionSelection, ...]
    minimum_matched_null_count: int
    regime_group_minimum_distinct_dates: int
    regime_group_minimum_raw_observations: int
    regime_group_minimum_issuer_count: int
    multiple_testing_family_size: int
    multiple_testing_method: str
    false_discovery_rate: Decimal

    sample_scope_description: str

    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        follow_up_id: str,
        parent_hypothesis_id: str,
        parent_conclusion_id: str,
        parent_reassessment_id: str,
        parent_qualification_id: str,
        agenda_id: str,
        feature_id: CorporateFeatureId | str,
        feature_transformation: str,
        outcome_id: CorporateOutcomeId | str,
        horizon_sessions: int,
        benchmark_kind: BenchmarkKind | str,
        condition_rule: ConditionRule | str,
        market_scope: str,
        cohort_policy_id: str,
        corporate_dataset_id: str,
        equity_snapshot_id: str,
        replay_run_id: str,
        cross_market_dataset_id: str,
        original_train_start: str,
        original_train_end: str,
        original_validation_start: str,
        original_validation_end: str,
        original_holdout_start: str,
        original_holdout_end: str,
        original_train_raw_sample: int,
        original_train_effective_sample: Decimal,
        original_validation_raw_sample: int,
        original_validation_effective_sample: Decimal,
        original_holdout_raw_sample: int,
        original_holdout_effective_sample: Decimal,
        original_conclusion_state: str,
        original_validation_beats_benchmark: bool,
        original_holdout_beats_benchmark: bool,
        original_validation_relative_result: Decimal,
        original_calibrated_grade: EvidenceGrade | str,
        matched_null_stratum: str,
        matched_null_count: int,
        matched_null_percentile: Decimal,
        follow_up_kind: str,
        follow_up_rationale: str,
        follow_up_qualification: str,
        regime_selections: Sequence[RegimeDimensionSelection],
        sample_scope_description: str,
        created_at: datetime,
    ) -> CorporateRegimeFollowUpSpecification:
        if not follow_up_id or not parent_hypothesis_id or not parent_conclusion_id:
            raise CorporateRegimeFollowUpError("SPECIFICATION_PARENT_LINEAGE_REQUIRED")
        if not parent_reassessment_id or not parent_qualification_id or not agenda_id:
            raise CorporateRegimeFollowUpError("SPECIFICATION_CALIBRATION_LINEAGE_REQUIRED")
        feature = CorporateFeatureId(feature_id)
        outcome = CorporateOutcomeId(outcome_id)
        if outcome is not CorporateOutcomeId.SUBSEQUENT_RETURN:
            # Section 5: "the primary follow-up must use the exact parent
            # outcome" -- SUBSEQUENT_RETURN is the only outcome the retained
            # parent/qualified follow-up pairing actually used; a mismatch
            # here means a caller is building the wrong specification, not
            # a legitimate adjacent-outcome examination (explicitly deferred
            # to a separate future budget).
            raise CorporateRegimeFollowUpError("SPECIFICATION_OUTCOME_MUST_MATCH_PARENT_EXACTLY")
        if horizon_sessions != 20:
            raise CorporateRegimeFollowUpError("SPECIFICATION_HORIZON_MUST_MATCH_PARENT_EXACTLY")
        benchmark = BenchmarkKind(benchmark_kind)
        if benchmark is not BenchmarkKind.EQUAL_WEIGHT_COHORT:
            raise CorporateRegimeFollowUpError("SPECIFICATION_BENCHMARK_MUST_MATCH_PARENT_EXACTLY")
        rule = ConditionRule(condition_rule)
        grade = EvidenceGrade(original_calibrated_grade)
        if matched_null_count < 0 or not (Decimal(0) <= matched_null_percentile <= Decimal(1)):
            raise CorporateRegimeFollowUpError("SPECIFICATION_MATCHED_NULL_EVIDENCE_INVALID")
        if follow_up_kind != "SPLIT_BY_REGIME":
            raise CorporateRegimeFollowUpError(
                "SPECIFICATION_FOLLOW_UP_KIND_MUST_BE_SPLIT_BY_REGIME"
            )
        if follow_up_qualification != "CALIBRATION_QUALIFIED":
            raise CorporateRegimeFollowUpError(
                "SPECIFICATION_FOLLOW_UP_MUST_BE_CALIBRATION_QUALIFIED"
            )
        selections = tuple(regime_selections)
        if len(selections) != MULTIPLE_TESTING_FAMILY_SIZE:
            raise CorporateRegimeFollowUpError(
                "SPECIFICATION_REQUIRES_EXACTLY_TWO_REGIME_SELECTIONS"
            )
        roles = {item.split_role for item in selections}
        if roles != {RegimeSplitRole.PRIMARY, RegimeSplitRole.SECONDARY}:
            raise CorporateRegimeFollowUpError(
                "SPECIFICATION_REQUIRES_ONE_PRIMARY_AND_ONE_SECONDARY_SELECTION"
            )
        if len({item.dimension for item in selections}) != len(selections):
            raise CorporateRegimeFollowUpError("SPECIFICATION_REGIME_DIMENSIONS_MUST_BE_DISTINCT")
        if not (original_train_start < original_train_end < original_validation_start):
            raise CorporateRegimeFollowUpError("SPECIFICATION_TRAIN_VALIDATION_ORDERING_INVALID")
        if not (original_validation_end < original_holdout_start):
            raise CorporateRegimeFollowUpError("SPECIFICATION_VALIDATION_HOLDOUT_ORDERING_INVALID")
        created = _aware(created_at, "SPECIFICATION_CREATED_AT_INVALID")
        payload = (
            CORPORATE_REGIME_FOLLOWUP_V1,
            follow_up_id,
            parent_hypothesis_id,
            parent_conclusion_id,
            parent_reassessment_id,
            parent_qualification_id,
            agenda_id,
            feature,
            _text(feature_transformation, "SPECIFICATION_FEATURE_TRANSFORMATION_INVALID"),
            outcome,
            horizon_sessions,
            benchmark,
            rule,
            _text(market_scope, "SPECIFICATION_MARKET_SCOPE_INVALID"),
            cohort_policy_id,
            corporate_dataset_id,
            equity_snapshot_id,
            replay_run_id,
            cross_market_dataset_id,
            original_train_start,
            original_train_end,
            original_validation_start,
            original_validation_end,
            original_holdout_start,
            original_holdout_end,
            original_train_raw_sample,
            original_train_effective_sample,
            original_validation_raw_sample,
            original_validation_effective_sample,
            original_holdout_raw_sample,
            original_holdout_effective_sample,
            _text(original_conclusion_state, "SPECIFICATION_ORIGINAL_CONCLUSION_STATE_INVALID"),
            original_validation_beats_benchmark,
            original_holdout_beats_benchmark,
            original_validation_relative_result,
            grade,
            _text(matched_null_stratum, "SPECIFICATION_MATCHED_NULL_STRATUM_INVALID"),
            matched_null_count,
            matched_null_percentile,
            _text(follow_up_rationale, "SPECIFICATION_FOLLOW_UP_RATIONALE_INVALID", maximum=1_000),
            tuple(
                (
                    item.split_role,
                    item.dimension,
                    item.group_a_labels,
                    item.group_b_labels,
                    item.null_control_budget,
                )
                for item in selections
            ),
            _text(sample_scope_description, "SPECIFICATION_SAMPLE_SCOPE_INVALID", maximum=2_000),
        )
        return cls(
            specification_id=deterministic_id("corporate_regime_followup_specification", payload),
            follow_up_id=follow_up_id,
            parent_hypothesis_id=parent_hypothesis_id,
            parent_conclusion_id=parent_conclusion_id,
            parent_reassessment_id=parent_reassessment_id,
            parent_qualification_id=parent_qualification_id,
            agenda_id=agenda_id,
            feature_id=feature,
            feature_transformation=payload[8],
            outcome_id=outcome,
            horizon_sessions=horizon_sessions,
            benchmark_kind=benchmark,
            condition_rule=rule,
            market_scope=payload[13],
            cohort_policy_id=cohort_policy_id,
            corporate_dataset_id=corporate_dataset_id,
            equity_snapshot_id=equity_snapshot_id,
            replay_run_id=replay_run_id,
            cross_market_dataset_id=cross_market_dataset_id,
            original_train_start=original_train_start,
            original_train_end=original_train_end,
            original_validation_start=original_validation_start,
            original_validation_end=original_validation_end,
            original_holdout_start=original_holdout_start,
            original_holdout_end=original_holdout_end,
            original_train_raw_sample=original_train_raw_sample,
            original_train_effective_sample=original_train_effective_sample,
            original_validation_raw_sample=original_validation_raw_sample,
            original_validation_effective_sample=original_validation_effective_sample,
            original_holdout_raw_sample=original_holdout_raw_sample,
            original_holdout_effective_sample=original_holdout_effective_sample,
            original_conclusion_state=payload[31],
            original_validation_beats_benchmark=original_validation_beats_benchmark,
            original_holdout_beats_benchmark=original_holdout_beats_benchmark,
            original_validation_relative_result=original_validation_relative_result,
            original_calibrated_grade=grade,
            matched_null_stratum=payload[36],
            matched_null_count=matched_null_count,
            matched_null_percentile=matched_null_percentile,
            follow_up_kind=follow_up_kind,
            follow_up_rationale=payload[39],
            follow_up_qualification=follow_up_qualification,
            post_hoc_status=POST_HOC_CALIBRATION_QUALIFIED_FOLLOWUP,
            independent_confirmation_required=True,
            independent_confirmation_reasons=INDEPENDENT_CONFIRMATION_REASONS,
            regime_selections=selections,
            minimum_matched_null_count=MINIMUM_MATCHED_NULL_COUNT,
            regime_group_minimum_distinct_dates=REGIME_GROUP_MINIMUM_DISTINCT_DATES,
            regime_group_minimum_raw_observations=REGIME_GROUP_MINIMUM_RAW_OBSERVATIONS,
            regime_group_minimum_issuer_count=REGIME_GROUP_MINIMUM_ISSUER_COUNT,
            multiple_testing_family_size=MULTIPLE_TESTING_FAMILY_SIZE,
            multiple_testing_method=MULTIPLE_TESTING_METHOD,
            false_discovery_rate=FALSE_DISCOVERY_RATE,
            sample_scope_description=payload[41],
            created_at=created,
            canonical_hash=content_digest(payload),
        )

    def selection_for(self, role: RegimeSplitRole) -> RegimeDimensionSelection:
        for item in self.regime_selections:
            if item.split_role is role:
                return item
        raise CorporateRegimeFollowUpError("SPECIFICATION_REGIME_SELECTION_NOT_FOUND")


# ==========================================================================
# Section 9: matched-null control provenance. One row per replicate; the
# retained statistic is the *interaction* (regime-A effect minus regime-B
# effect), not a single-group effect.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class CorporateRegimeNullControl:
    control_id: str
    specification_id: str
    split_role: RegimeSplitRole
    ordinal: int
    seed: int
    mechanism: CorporateNullMechanism
    group_a_raw_count: int
    group_a_effect: Decimal | None
    group_b_raw_count: int
    group_b_effect: Decimal | None
    interaction_effect: Decimal | None
    preserved_properties: tuple[str, ...]
    destroyed_relationship: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        specification_id: str,
        split_role: RegimeSplitRole | str,
        ordinal: int,
        seed: int,
        mechanism: CorporateNullMechanism | str,
        group_a_raw_count: int,
        group_a_effect: Decimal | None,
        group_b_raw_count: int,
        group_b_effect: Decimal | None,
        preserved_properties: Sequence[str],
        destroyed_relationship: str,
        created_at: datetime,
    ) -> CorporateRegimeNullControl:
        if not specification_id:
            raise CorporateRegimeFollowUpError("NULL_CONTROL_SPECIFICATION_ID_REQUIRED")
        role = RegimeSplitRole(split_role)
        mechanism_value = CorporateNullMechanism(mechanism)
        if seed < 0:
            raise CorporateRegimeFollowUpError("NULL_CONTROL_SEED_INVALID")
        if ordinal < 0:
            raise CorporateRegimeFollowUpError("NULL_CONTROL_ORDINAL_INVALID")
        if group_a_raw_count < 0 or group_b_raw_count < 0:
            raise CorporateRegimeFollowUpError("NULL_CONTROL_GROUP_COUNT_INVALID")
        preserved = tuple(sorted(set(preserved_properties)))
        if not preserved:
            raise CorporateRegimeFollowUpError("NULL_CONTROL_PRESERVED_PROPERTIES_REQUIRED")
        interaction = (
            group_a_effect - group_b_effect
            if group_a_effect is not None and group_b_effect is not None
            else None
        )
        created = _aware(created_at, "NULL_CONTROL_CREATED_AT_INVALID")
        payload = (
            CORPORATE_REGIME_FOLLOWUP_V1,
            specification_id,
            role,
            ordinal,
            seed,
            mechanism_value,
            group_a_raw_count,
            group_a_effect,
            group_b_raw_count,
            group_b_effect,
            interaction,
            preserved,
            _text(destroyed_relationship, "NULL_CONTROL_DESTROYED_RELATIONSHIP_INVALID"),
        )
        return cls(
            control_id=deterministic_id("corporate_regime_followup_null_control", payload),
            specification_id=specification_id,
            split_role=role,
            ordinal=ordinal,
            seed=seed,
            mechanism=mechanism_value,
            group_a_raw_count=group_a_raw_count,
            group_a_effect=group_a_effect,
            group_b_raw_count=group_b_raw_count,
            group_b_effect=group_b_effect,
            interaction_effect=interaction,
            preserved_properties=preserved,
            destroyed_relationship=payload[12],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 6/7: regime-group adequacy and panel-dependence evidence.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class RegimeGroupEvidence:
    group_name: str
    labels: tuple[str, ...]
    raw_observations: int
    distinct_effective_dates: int
    issuer_count: int
    sector_count: int
    missing_feature_rate: Decimal | None
    sufficient: bool
    insufficiency_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegimeGroupEffect:
    group_name: str
    treatment_count: int
    benchmark_count: int
    treatment_mean: Decimal | None
    benchmark_mean: Decimal | None
    effect: Decimal | None
    beats_benchmark: bool | None
    cohens_d: Decimal | None
    effective_sample: Decimal
    standard_error: Decimal | None
    bootstrap_ci_lower: Decimal | None
    bootstrap_ci_upper: Decimal | None


# ==========================================================================
# Section 8/9: the regime interaction itself.
# ==========================================================================

SIGNIFICANCE_FALLACY_GUARD_NOTE = (
    "REGIME_DEPENDENCE_IS_NEVER_INFERRED_FROM_ONE_GROUP_BEING_SIGNIFICANT_AND_THE_OTHER_NOT -- "
    "the conclusion is derived only from the interaction statistic's own matched-null-calibrated "
    "percentile, corrected significance, and robustness, never from comparing each group's raw "
    "significance flag against the other's."
)


@dataclass(frozen=True, slots=True)
class RegimeInteractionResult:
    interaction_effect: Decimal | None
    combined_standard_error: Decimal | None
    interaction_cohens_d: Decimal | None
    effect_meets_floor: bool
    effect_floor_policy_id: str
    raw_p_value: Decimal | None
    raw_significant: bool | None
    matched_calibration: MatchedCalibrationResult
    significance_fallacy_guard_note: str = SIGNIFICANCE_FALLACY_GUARD_NOTE


# ==========================================================================
# Sections 12-14: temporal stability, sector/issuer concentration, and
# outlier robustness -- all evaluated for the interaction, not merely for
# each regime group standing alone.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class RegimeRobustnessAssessment:
    group_a_checks: tuple[RobustnessCheckResult, ...]
    group_b_checks: tuple[RobustnessCheckResult, ...]
    interaction_checks: tuple[RobustnessCheckResult, ...]
    summary: RobustnessSummary
    leave_one_sector_out_excluded: str | None
    leave_one_issuer_out_excluded: str | None


# ==========================================================================
# Section 15: incremental market-context test.
# ==========================================================================


class IncrementalContributionVerdict(StrEnum):
    GENUINELY_INCREMENTAL = "GENUINELY_INCREMENTAL"
    REDUNDANT = "REDUNDANT"
    ORDINARY_NULL_VARIATION = "ORDINARY_NULL_VARIATION"
    INSUFFICIENTLY_CALIBRATED = "INSUFFICIENTLY_CALIBRATED"


@dataclass(frozen=True, slots=True)
class RegimeIncrementalContribution:
    regime_only_effect: Decimal | None
    regime_only_sample: int
    corporate_only_effect: Decimal | None
    corporate_only_sample: int
    combined_interaction_effect: Decimal | None
    combined_sample: int
    verdict: IncrementalContributionVerdict
    rationale: str


def classify_incremental_contribution(
    *,
    matched: MatchedCalibrationResult,
    interaction_effect: Decimal | None,
    regime_only_effect: Decimal | None,
) -> IncrementalContributionVerdict:
    """Section 15's required guard: never call a regime-conditioned
    relationship useful just because the regime alone explains the result."""

    if not matched.sufficient or matched.matched_percentile is None or interaction_effect is None:
        return IncrementalContributionVerdict.INSUFFICIENTLY_CALIBRATED
    genuinely_distinguishable = matched.matched_percentile >= Decimal("0.90")
    if not genuinely_distinguishable:
        return IncrementalContributionVerdict.ORDINARY_NULL_VARIATION
    if regime_only_effect is not None and abs(regime_only_effect) >= abs(interaction_effect):
        # The market-context-only baseline moves at least as much as the
        # feature-conditioned interaction does -- the regime alone already
        # explains what looked like a corporate-feature effect.
        return IncrementalContributionVerdict.REDUNDANT
    return IncrementalContributionVerdict.GENUINELY_INCREMENTAL


# ==========================================================================
# Section 16: parent relationship preservation.
# ==========================================================================


class ParentRelationshipBehavior(StrEnum):
    CONSISTENT = "CONSISTENT"
    WEAKENED = "WEAKENED"
    DISAPPEARED = "DISAPPEARED"
    REVERSED = "REVERSED"


@dataclass(frozen=True, slots=True)
class ParentRelationshipComparison:
    original_validation_relative_result: Decimal
    full_sample_unsplit_relative_result: Decimal | None
    group_a_effect: Decimal | None
    group_b_effect: Decimal | None
    behavior: ParentRelationshipBehavior
    rationale: str


def classify_parent_relationship(
    *,
    original_validation_relative_result: Decimal,
    full_sample_unsplit_relative_result: Decimal | None,
) -> ParentRelationshipBehavior:
    if full_sample_unsplit_relative_result is None:
        return ParentRelationshipBehavior.DISAPPEARED
    original_sign = original_validation_relative_result > 0
    full_sign = full_sample_unsplit_relative_result > 0
    if original_sign != full_sign and full_sample_unsplit_relative_result != 0:
        return ParentRelationshipBehavior.REVERSED
    if full_sample_unsplit_relative_result == 0:
        return ParentRelationshipBehavior.DISAPPEARED
    ratio = abs(full_sample_unsplit_relative_result) / abs(original_validation_relative_result)
    if ratio >= Decimal("0.5"):
        return ParentRelationshipBehavior.CONSISTENT
    return ParentRelationshipBehavior.WEAKENED


# ==========================================================================
# Section 18/20: follow-up conclusion vocabulary and knowledge history.
# ==========================================================================


class RegimeFollowUpConclusion(StrEnum):
    REGIME_EFFECT_SUPPORTED = "REGIME_EFFECT_SUPPORTED"
    PARTIAL_REGIME_EFFECT = "PARTIAL_REGIME_EFFECT"
    REGIME_EFFECT_UNSTABLE = "REGIME_EFFECT_UNSTABLE"
    REGIME_EFFECT_NULL_COMPATIBLE = "REGIME_EFFECT_NULL_COMPATIBLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    INCREMENTAL_VALUE_ABSENT = "INCREMENTAL_VALUE_ABSENT"


@dataclass(frozen=True, slots=True)
class KnowledgeHistoryStage:
    stage_label: str
    reference_id: str
    state: str
    recorded_at: datetime | None


def build_knowledge_history(
    *,
    hypothesis_id: str,
    original_conclusion_id: str,
    original_conclusion_state: str,
    original_conclusion_created_at: datetime,
    reassessment_id: str,
    calibrated_grade: EvidenceGrade,
    reassessment_created_at: datetime,
    follow_up_id: str,
    qualification_id: str,
    qualification: str,
    qualification_created_at: datetime,
    result_id: str,
    result_conclusion: RegimeFollowUpConclusion,
    result_created_at: datetime,
) -> tuple[KnowledgeHistoryStage, ...]:
    """Section 20's append-only chain: v3 hypothesis -> original conclusion
    -> deep calibration assessment -> qualified follow-up -> regime-follow-
    up result. Every prior stage keeps its own original ``recorded_at``;
    nothing here overwrites or backdates a prior evidence stage."""

    return (
        KnowledgeHistoryStage("V3_HYPOTHESIS", hypothesis_id, "PREDECLARED", None),
        KnowledgeHistoryStage(
            "ORIGINAL_CONCLUSION",
            original_conclusion_id,
            original_conclusion_state,
            original_conclusion_created_at,
        ),
        KnowledgeHistoryStage(
            "DEEP_CALIBRATION_ASSESSMENT",
            reassessment_id,
            calibrated_grade.value,
            reassessment_created_at,
        ),
        KnowledgeHistoryStage(
            "QUALIFIED_FOLLOW_UP", follow_up_id, qualification, qualification_created_at
        ),
        KnowledgeHistoryStage(
            "REGIME_FOLLOW_UP_RESULT", result_id, result_conclusion.value, result_created_at
        ),
    )


def classify_regime_follow_up_conclusion(
    *,
    adequate: bool,
    conclusion_state: ResearchConclusionState | None,
    evidence_grade: EvidenceGrade | None,
    stability_state: StabilityState | None,
    incremental_verdict: IncrementalContributionVerdict | None,
) -> RegimeFollowUpConclusion:
    """Section 18's deterministic, explicitly ordered classification.

    Priority mirrors ``research_methodology.derive_conclusion_state``'s own
    "a failure of sample adequacy ... always takes precedence over a
    nominally positive raw result" discipline, extended with the
    incremental-value and temporal-stability guards sections 12 and 15
    separately require.
    """

    if (
        not adequate
        or conclusion_state is None
        or conclusion_state is ResearchConclusionState.INSUFFICIENT_EVIDENCE
    ):
        return RegimeFollowUpConclusion.INSUFFICIENT_EVIDENCE
    if incremental_verdict is IncrementalContributionVerdict.INSUFFICIENTLY_CALIBRATED:
        return RegimeFollowUpConclusion.INSUFFICIENT_EVIDENCE
    if incremental_verdict is IncrementalContributionVerdict.REDUNDANT:
        return RegimeFollowUpConclusion.INCREMENTAL_VALUE_ABSENT
    if conclusion_state in (
        ResearchConclusionState.ROBUSTNESS_FAILED,
        ResearchConclusionState.HOLDOUT_FAILED,
    ):
        return RegimeFollowUpConclusion.REGIME_EFFECT_UNSTABLE
    if conclusion_state in (ResearchConclusionState.REJECTED, ResearchConclusionState.INCONCLUSIVE):
        return RegimeFollowUpConclusion.REGIME_EFFECT_NULL_COMPATIBLE
    if evidence_grade is None or evidence_grade is EvidenceGrade.UNRELIABLE:
        return RegimeFollowUpConclusion.REGIME_EFFECT_NULL_COMPATIBLE
    if evidence_grade is EvidenceGrade.EXPLORATORY:
        return RegimeFollowUpConclusion.REGIME_EFFECT_NULL_COMPATIBLE
    # evidence_grade is SUGGESTIVE/VALIDATED/STRONG from here on.
    if stability_state in (
        StabilityState.UNSTABLE,
        StabilityState.CONCENTRATED_IN_ONE_PERIOD,
        StabilityState.INSUFFICIENT_EVIDENCE,
    ):
        return RegimeFollowUpConclusion.REGIME_EFFECT_UNSTABLE
    if incremental_verdict is IncrementalContributionVerdict.ORDINARY_NULL_VARIATION:
        return RegimeFollowUpConclusion.PARTIAL_REGIME_EFFECT
    if stability_state in (StabilityState.STABLE, StabilityState.REGIME_DEPENDENT):
        return RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED
    return RegimeFollowUpConclusion.PARTIAL_REGIME_EFFECT


# ==========================================================================
# The per-split bundle and the final immutable result.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class RegimeSplitResult:
    role: RegimeSplitRole
    selection: RegimeDimensionSelection
    group_a_evidence: RegimeGroupEvidence
    group_b_evidence: RegimeGroupEvidence
    adequate: bool
    inadequacy_reason: str | None
    group_a_effect: RegimeGroupEffect | None
    group_b_effect: RegimeGroupEffect | None
    interaction: RegimeInteractionResult | None
    interaction_stability: StabilityReport | None
    robustness: RegimeRobustnessAssessment | None
    incremental: RegimeIncrementalContribution | None
    sample_quality: SampleQualityState
    conclusion_state: ResearchConclusionState | None
    evidence_grade: EvidenceGrade | None
    evidence_grade_rationale: str


@dataclass(frozen=True, slots=True)
class CorporateRegimeFollowUpResult:
    result_id: str
    specification_id: str
    primary_split: RegimeSplitResult
    secondary_split: RegimeSplitResult
    multiple_testing_results: tuple[CorrectedTestResult, ...]
    multiple_testing_family_size: int
    multiple_testing_method: str
    parent_relationship: ParentRelationshipComparison
    conclusion: RegimeFollowUpConclusion
    conclusion_rationale: str
    evidence_grade: EvidenceGrade
    evidence_grade_rationale: str
    independent_confirmation_required: bool
    independent_confirmation_reasons: tuple[str, ...]
    knowledge_history: tuple[KnowledgeHistoryStage, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        specification_id: str,
        primary_split: RegimeSplitResult,
        secondary_split: RegimeSplitResult,
        multiple_testing_results: Sequence[CorrectedTestResult],
        parent_relationship: ParentRelationshipComparison,
        conclusion: RegimeFollowUpConclusion | str,
        conclusion_rationale: str,
        evidence_grade: EvidenceGrade | str,
        evidence_grade_rationale: str,
        knowledge_history: Sequence[KnowledgeHistoryStage],
        created_at: datetime,
    ) -> CorporateRegimeFollowUpResult:
        if not specification_id:
            raise CorporateRegimeFollowUpError("RESULT_SPECIFICATION_ID_REQUIRED")
        if primary_split.role is not RegimeSplitRole.PRIMARY:
            raise CorporateRegimeFollowUpError("RESULT_PRIMARY_SPLIT_ROLE_MISMATCH")
        if secondary_split.role is not RegimeSplitRole.SECONDARY:
            raise CorporateRegimeFollowUpError("RESULT_SECONDARY_SPLIT_ROLE_MISMATCH")
        conclusion_value = RegimeFollowUpConclusion(conclusion)
        grade = EvidenceGrade(evidence_grade)
        history = tuple(knowledge_history)
        if len(history) != 5:
            raise CorporateRegimeFollowUpError("RESULT_KNOWLEDGE_HISTORY_MUST_HAVE_FIVE_STAGES")
        expected_stage_order = (
            "V3_HYPOTHESIS",
            "ORIGINAL_CONCLUSION",
            "DEEP_CALIBRATION_ASSESSMENT",
            "QUALIFIED_FOLLOW_UP",
            "REGIME_FOLLOW_UP_RESULT",
        )
        if tuple(item.stage_label for item in history) != expected_stage_order:
            raise CorporateRegimeFollowUpError("RESULT_KNOWLEDGE_HISTORY_ORDER_INVALID")
        created = _aware(created_at, "RESULT_CREATED_AT_INVALID")
        testing_results = tuple(multiple_testing_results)
        payload = (
            CORPORATE_REGIME_FOLLOWUP_V1,
            specification_id,
            primary_split.role,
            secondary_split.role,
            tuple(
                (item.trial_id, item.raw_p_value, item.adjusted_p_value, item.rejected_null)
                for item in testing_results
            ),
            parent_relationship.behavior,
            conclusion_value,
            _text(conclusion_rationale, "RESULT_CONCLUSION_RATIONALE_INVALID", maximum=2_000),
            grade,
            _text(
                evidence_grade_rationale, "RESULT_EVIDENCE_GRADE_RATIONALE_INVALID", maximum=2_000
            ),
            True,
            INDEPENDENT_CONFIRMATION_REASONS,
            tuple((item.stage_label, item.reference_id, item.state) for item in history),
        )
        return cls(
            result_id=deterministic_id("corporate_regime_followup_result", payload),
            specification_id=specification_id,
            primary_split=primary_split,
            secondary_split=secondary_split,
            multiple_testing_results=testing_results,
            multiple_testing_family_size=MULTIPLE_TESTING_FAMILY_SIZE,
            multiple_testing_method=MULTIPLE_TESTING_METHOD,
            parent_relationship=parent_relationship,
            conclusion=conclusion_value,
            conclusion_rationale=payload[7],
            evidence_grade=grade,
            evidence_grade_rationale=payload[9],
            independent_confirmation_required=True,
            independent_confirmation_reasons=INDEPENDENT_CONFIRMATION_REASONS,
            knowledge_history=history,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def corporate_regime_followup_warnings() -> tuple[str, ...]:
    return (
        CORPORATE_REGIME_FOLLOWUP_WARNING,
        POST_HOC_CALIBRATION_QUALIFIED_FOLLOWUP,
        SIGNIFICANCE_FALLACY_GUARD_NOTE,
        "AUTHORITY_NONE -- NO STRATEGY, OPPORTUNITY, PAPER ORDER, OR LIVE AUTHORITY IS CREATED",
    )
