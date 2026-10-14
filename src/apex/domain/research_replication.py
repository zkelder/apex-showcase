"""Calibration-Qualified Interaction Replication v1.

Answers one narrow question about one already-calibrated result: does the
single ``CALIBRATION_QUALIFIED`` follow-up from Research Calibration v2 --
the ``BTC_RETURN_20`` x ``EQUITY_VOLATILITY_20`` interaction whose matched
percentile was 1.00 against 15 matched interaction-design null controls --
deserve continued research? This module never widens that question into a
new agenda: it freezes the exact parent hypothesis, conclusion, and v1/v2
calibration lineage into one immutable :class:`ReplicationSpecification`,
then retains genuinely new (but strictly descriptive, never re-searched)
evidence about that one frozen specification's temporal stability,
volatility-state stability, cross-market timing integrity, effect size,
distributional/outlier robustness, and regime-confound plausibility.

Every dataclass here is ``authority = "NONE"``. Nothing here creates a
Strategy, Shared Opportunity, PAPER order, or execution authority, and
nothing here claims causality or trading edge -- see
``REPLICATION_WARNING``/``CAUSAL_LANGUAGE_POLICY`` (the latter reused
unmodified from ``domain.cross_market_research``).

Built entirely on top of existing, unmodified vocabulary:

* :class:`~apex.domain.cross_market_research.FeatureId`/``OutcomeId``/
  ``MarketId``/``ConditionRule`` for the frozen interaction definition.
* :class:`~apex.domain.research_calibration_v2.ResearchDesign`/
  ``ConditioningShape``/``MatchedCalibrationResult`` for the reused,
  unaugmented 15-control matched-null population.
* :class:`~apex.domain.research_calibration.EvidenceGrade` for the
  replication's own (never automatically upgraded) evidence grade.

Nothing here mutates a historical ``GeneratedHypothesis``, ``ResearchConclusion``,
``ResearchKnowledgeEntry``, ``FollowUpProposal``, v1 ``FollowUpPriorityAssessment``,
or v2 ``FollowUpQualification`` record; every one of those is only ever
*referenced* here by id (lineage), never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.cross_market_research import ConditionRule, FeatureId, MarketId, OutcomeId
from apex.domain.research_calibration import EvidenceGrade
from apex.domain.research_calibration_v2 import ConditioningShape, ResearchDesign
from apex.domain.values import content_digest, deterministic_id

RESEARCH_REPLICATION_V1 = "CALIBRATION_QUALIFIED_INTERACTION_REPLICATION_V1"
REPLICATION_WARNING = (
    "CALIBRATION-QUALIFIED INTERACTION REPLICATION EVIDENCE ONLY -- IN-SAMPLE RESEARCH "
    "REPLICATION OF ONE FROZEN HYPOTHESIS, NOT INDEPENDENT CONFIRMATION; NO STRATEGY, PAPER, "
    "OR LIVE AUTHORITY; GRANTS NO EXECUTION AUTHORITY"
)
REPLICATION_LINEAGE_NOTE = (
    "This replication is strictly additive on top of Cross-Market Research v1, Research "
    "Signal Calibration v1, and Research Calibration v2: no GeneratedHypothesis, "
    "ResearchConclusion, ResearchKnowledgeEntry, FollowUpProposal, v1 "
    "FollowUpPriorityAssessment, or v2 FollowUpQualification row is ever rewritten. Every "
    "replication record retains lineage to that evidence by id."
)
IN_SAMPLE_REPLICATION_NOTE = (
    "The entire retained [2020-07-27, 2026-06-30] cross-market panel was already consumed by "
    "this hypothesis's own exploratory/validation/locked-holdout trial phases and by both "
    "calibration-v1 and calibration-v2's negative-control batteries before this replication "
    "ran. No genuinely unused historical segment remains. This replication is therefore "
    "IN_SAMPLE_REPLICATION / RESEARCH_REPLICATION evidence -- never independent confirmation "
    "-- and a positive result requires genuinely fresh forward observation before it can "
    "justify anything beyond continued research."
)
CAUSAL_LANGUAGE_POLICY = (
    "Reuses domain.cross_market_research.CAUSAL_LANGUAGE_POLICY unmodified: this replication "
    "may describe a relationship as historically associated with, or stable/unstable across a "
    "retained partition; it may never conclude a relationship 'causes' an outcome."
)
FINITE_SAMPLE_WARNING = (
    "The matched-null percentile this replication retains (1.00) ranks the genuine statistic "
    "top of exactly 15 retained matched interaction-design null controls -- a finite-sample "
    "rank, not a claim about a continuous 100th-percentile population effect. No control was "
    "added to this stratum after the genuine result was observed."
)

# Section 3: fixed configuration values, declared once and reused verbatim
# by every replication this module ever creates -- never re-derived per run.
MINIMUM_EFFECTIVE_SAMPLE = Decimal("5")
STATISTICAL_METHOD_ID = (
    "COHENS_D_POOLED_STDEV_V1_VS_TEMPLATE_MATCHED_NULL_POPULATION_RESEARCH_CALIBRATION_V2"
)
TEMPORAL_PARTITION_POLICY_ID = "TIME_PERIOD_THIRDS_V1"
VOLATILITY_STATE_POLICY_ID = "EQUITY_VOLATILITY_REGIME_LABEL_V1"
PRIMARY_ALIGNMENT_CONVENTION_ID = "BTC_UTC_DAY_END_AVAILABILITY_V1"
LAGGED_ALIGNMENT_CONVENTION_ID = "LAGGED_EQUITY_ONE_SESSION_V1"

# Section 4: the one tiny, predeclared sensitivity set -- never a search over
# many lookbacks, thresholds, or horizons. Four robustness checks reused
# verbatim from the parent agenda's own declared policy
# (SUBPERIOD_SPLIT/REGIME_SPLIT/LEAVE_ONE_PERIOD_OUT/MISSING_DATA_SENSITIVITY/
# COST_STRESS -- see ``ResearchAgenda.robustness_requirements``) plus three
# genuinely new but equally bounded checks this milestone's sections 12/15
# require: a 5%-tail winsorization, a robust (median-based) comparison, and
# one bounded subperiod exclusion (the tiny locked-holdout window).
REPLICATION_ROBUSTNESS_REQUIREMENTS = (
    "WINSORIZED_OUTCOME_5PCT_TAIL",
    "LEAVE_ONE_TEMPORAL_THIRD_OUT",
    "ROBUST_MEDIAN_COMPARISON",
    "BOUNDED_HOLDOUT_WINDOW_EXCLUSION",
)


class ReplicationError(ValueError):
    """A replication-domain invariant was violated."""


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReplicationError(f"{label}_MUST_BE_TIMEZONE_AWARE")
    return value


def _text(value: str, code: str, *, maximum: int = 2_000) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ReplicationError(code)
    return normalized


# ==========================================================================
# Section 5: replication-period exposure policy
# ==========================================================================


class DatasetExposureLabel(StrEnum):
    """Section 5's honest prior-exposure classification. Every retained
    date in the panel falls into at least one of these; none is
    ``UNUSED_NOT_YET_OBSERVED`` for this hypothesis today (see
    :data:`IN_SAMPLE_REPLICATION_NOTE`) -- that member exists so the
    taxonomy stays genuinely applicable to a future, less-exposed
    follow-up rather than being a dead label."""

    DISCOVERY_EXPLORATORY_USED = "DISCOVERY_EXPLORATORY_USED"
    VALIDATION_USED = "VALIDATION_USED"
    LOCKED_HOLDOUT_CONSUMED = "LOCKED_HOLDOUT_CONSUMED"
    CALIBRATION_V1_AND_V2_USED = "CALIBRATION_V1_AND_V2_USED"
    UNUSED_NOT_YET_OBSERVED = "UNUSED_NOT_YET_OBSERVED"


@dataclass(frozen=True, slots=True)
class DatasetExposureSegment:
    """One retained date range's exposure state (section 5), declared as
    part of the frozen specification -- before any replication result is
    computed, never inferred afterward to justify one."""

    label: DatasetExposureLabel
    start_date: str
    end_date: str
    note: str


# ==========================================================================
# Section 14/15: cross-market timing alignment convention
# ==========================================================================


class TimingConvention(StrEnum):
    """Section 14/15's two predeclared, timing-safe alignments -- a
    robustness axis, never an optimization dimension (section 15)."""

    PRIMARY_SAME_DAY_V1 = "PRIMARY_SAME_DAY_V1"
    LAGGED_EQUITY_ONE_SESSION_V1 = "LAGGED_EQUITY_ONE_SESSION_V1"


# ==========================================================================
# Section 6/7: partition taxonomy (temporal and volatility-state share one
# shape; see ``ReplicationPartitionResult``)
# ==========================================================================


class PartitionKind(StrEnum):
    FULL_SAMPLE = "FULL_SAMPLE"
    TIME_PERIOD = "TIME_PERIOD"
    VOLATILITY_STATE = "VOLATILITY_STATE"


# ==========================================================================
# Section 16: transaction-cost relevance classification (never a strategy)
# ==========================================================================


class TransactionCostRelevance(StrEnum):
    BELOW_COST_SCALE = "BELOW_COST_SCALE"
    COMPARABLE_TO_COST_SCALE = "COMPARABLE_TO_COST_SCALE"
    ABOVE_COST_SCALE = "ABOVE_COST_SCALE"
    COST_EVIDENCE_UNAVAILABLE = "COST_EVIDENCE_UNAVAILABLE"


def classify_transaction_cost_relevance(
    *, effect: Decimal | None, round_trip_cost_rate: Decimal
) -> TransactionCostRelevance:
    """Section 16: classify magnitude only -- never construct a strategy or
    simulate an order. ``round_trip_cost_rate`` is the caller's already-
    retained conservative crypto cost assumption (two sides of fee +
    slippage), passed explicitly rather than hard-coded here."""

    if effect is None or round_trip_cost_rate <= 0:
        return TransactionCostRelevance.COST_EVIDENCE_UNAVAILABLE
    ratio = abs(effect) / round_trip_cost_rate
    if ratio < Decimal("0.5"):
        return TransactionCostRelevance.BELOW_COST_SCALE
    if ratio <= Decimal("2"):
        return TransactionCostRelevance.COMPARABLE_TO_COST_SCALE
    return TransactionCostRelevance.ABOVE_COST_SCALE


# ==========================================================================
# Section 17/20: replication and follow-up-qualification vocabulary
# ==========================================================================


class ReplicationConclusionState(StrEnum):
    REPLICATED = "REPLICATED"
    PARTIALLY_REPLICATED = "PARTIALLY_REPLICATED"
    UNSTABLE = "UNSTABLE"
    NULL_COMPATIBLE = "NULL_COMPATIBLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    TIMING_INVALID = "TIMING_INVALID"
    CONFOUNDING_NOT_RESOLVED = "CONFOUNDING_NOT_RESOLVED"


class FollowUpReplicationQualification(StrEnum):
    REPLICATION_SUPPORTED = "REPLICATION_SUPPORTED"
    FORWARD_CONFIRMATION_REQUIRED = "FORWARD_CONFIRMATION_REQUIRED"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REJECTED = "REJECTED"
    INSUFFICIENT = "INSUFFICIENT"


# ==========================================================================
# Sections 1-3: the one immutable replication specification
# ==========================================================================


@dataclass(frozen=True, slots=True)
class ReplicationSpecification:
    """The frozen parent-hypothesis lineage plus fixed primary
    configuration (sections 1-3). Every field is either copied verbatim
    from an already-retained record (parent hypothesis, conclusion, v1/v2
    calibration lineage) or comes from an existing versioned policy
    (temporal/volatility partition policy, robustness set, alignment
    convention) -- none is selected after seeing a replication result, and
    ``ReplicationSpecification.create`` is the *only* way to declare one:
    it is called exactly once per follow-up id (deterministic id), so a
    second call with the identical inputs is a no-op replay rather than a
    new declaration."""

    replication_id: str
    follow_up_id: str
    parent_hypothesis_id: str
    parent_conclusion_id: str
    parent_agenda_id: str
    v1_calibration_id: str
    v1_priority_assessment_id: str
    v2_calibration_id: str
    v2_classification_id: str
    v2_matched_stratum: str
    v2_matched_null_count: int
    v2_matched_percentile: Decimal
    research_design: ResearchDesign
    conditioning_shape: ConditioningShape
    primary_feature_id: FeatureId
    primary_condition_rule: ConditionRule
    secondary_feature_id: FeatureId
    secondary_condition_rule: ConditionRule
    market_scope: tuple[MarketId, ...]
    outcome_id: OutcomeId
    horizon_sessions: int
    benchmark_kind: str
    dataset_snapshot_id: str
    replay_run_id: str
    cross_market_dataset_id: str
    train_period_start: str
    train_period_end: str
    minimum_effective_sample: Decimal
    statistical_method_id: str
    robustness_requirements: tuple[str, ...]
    temporal_partition_policy_id: str
    volatility_state_policy_id: str
    alignment_convention_id: str
    exposure_segments: tuple[DatasetExposureSegment, ...]
    is_in_sample_replication: bool
    rationale: str
    original_agenda_objective: str
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
        parent_agenda_id: str,
        v1_calibration_id: str,
        v1_priority_assessment_id: str,
        v2_calibration_id: str,
        v2_classification_id: str,
        v2_matched_stratum: str,
        v2_matched_null_count: int,
        v2_matched_percentile: Decimal,
        research_design: ResearchDesign | str,
        conditioning_shape: ConditioningShape | str,
        primary_feature_id: FeatureId | str,
        primary_condition_rule: ConditionRule | str,
        secondary_feature_id: FeatureId | str,
        secondary_condition_rule: ConditionRule | str,
        market_scope: tuple[MarketId | str, ...],
        outcome_id: OutcomeId | str,
        horizon_sessions: int,
        benchmark_kind: str,
        dataset_snapshot_id: str,
        replay_run_id: str,
        cross_market_dataset_id: str,
        train_period_start: str,
        train_period_end: str,
        exposure_segments: tuple[DatasetExposureSegment, ...],
        is_in_sample_replication: bool,
        rationale: str,
        original_agenda_objective: str,
        created_at: datetime,
        minimum_effective_sample: Decimal = MINIMUM_EFFECTIVE_SAMPLE,
        statistical_method_id: str = STATISTICAL_METHOD_ID,
        robustness_requirements: tuple[str, ...] = REPLICATION_ROBUSTNESS_REQUIREMENTS,
        temporal_partition_policy_id: str = TEMPORAL_PARTITION_POLICY_ID,
        volatility_state_policy_id: str = VOLATILITY_STATE_POLICY_ID,
        alignment_convention_id: str = PRIMARY_ALIGNMENT_CONVENTION_ID,
    ) -> ReplicationSpecification:
        if not follow_up_id or not parent_hypothesis_id or not parent_conclusion_id:
            raise ReplicationError("REPLICATION_PARENT_LINEAGE_REQUIRED")
        if not v1_calibration_id or not v1_priority_assessment_id:
            raise ReplicationError("REPLICATION_V1_LINEAGE_REQUIRED")
        if not v2_calibration_id or not v2_classification_id or not v2_matched_stratum:
            raise ReplicationError("REPLICATION_V2_LINEAGE_REQUIRED")
        design_value = ResearchDesign(research_design)
        if design_value is not ResearchDesign.INTERACTION:
            # Section 24 scope: this milestone runs exactly the one
            # CALIBRATION_QUALIFIED interaction follow-up -- never a
            # conditional or any other design.
            raise ReplicationError("REPLICATION_SCOPE_IS_INTERACTION_DESIGN_ONLY")
        shape_value = ConditioningShape(conditioning_shape)
        if shape_value is not ConditioningShape.NUMERIC_INTERACTION:
            raise ReplicationError("REPLICATION_SCOPE_IS_NUMERIC_INTERACTION_SHAPE_ONLY")
        primary_feature = FeatureId(primary_feature_id)
        primary_rule = ConditionRule(primary_condition_rule)
        secondary_feature = FeatureId(secondary_feature_id)
        secondary_rule = ConditionRule(secondary_condition_rule)
        scope = tuple(MarketId(item) for item in market_scope)
        if not scope:
            raise ReplicationError("REPLICATION_MARKET_SCOPE_REQUIRED")
        outcome = OutcomeId(outcome_id)
        if horizon_sessions < 1:
            raise ReplicationError("REPLICATION_HORIZON_INVALID")
        if not train_period_start or not train_period_end or train_period_start > train_period_end:
            raise ReplicationError("REPLICATION_TRAIN_PERIOD_INVALID")
        if v2_matched_null_count < 0:
            raise ReplicationError("REPLICATION_V2_MATCHED_NULL_COUNT_INVALID")
        if not (0 <= v2_matched_percentile <= 1):
            raise ReplicationError("REPLICATION_V2_MATCHED_PERCENTILE_OUT_OF_RANGE")
        if minimum_effective_sample <= 0:
            raise ReplicationError("REPLICATION_MINIMUM_EFFECTIVE_SAMPLE_INVALID")
        # Section 4: a tiny, bounded, predeclared sensitivity set only --
        # never an open-ended search space.
        if not (1 <= len(robustness_requirements) <= 8):
            raise ReplicationError("REPLICATION_ROBUSTNESS_SET_MUST_BE_SMALL_AND_PREDECLARED")
        if len(set(robustness_requirements)) != len(robustness_requirements):
            raise ReplicationError("REPLICATION_ROBUSTNESS_SET_MUST_NOT_REPEAT")
        if not exposure_segments:
            raise ReplicationError("REPLICATION_EXPOSURE_SEGMENTS_REQUIRED")
        created = _aware(created_at, "REPLICATION_CREATED_AT")
        requirements = tuple(robustness_requirements)
        segments_payload = tuple(
            (item.label, item.start_date, item.end_date, item.note) for item in exposure_segments
        )
        payload = (
            RESEARCH_REPLICATION_V1,
            follow_up_id,
            parent_hypothesis_id,
            parent_conclusion_id,
            parent_agenda_id,
            v1_calibration_id,
            v1_priority_assessment_id,
            v2_calibration_id,
            v2_classification_id,
            v2_matched_stratum,
            v2_matched_null_count,
            v2_matched_percentile,
            design_value,
            shape_value,
            primary_feature,
            primary_rule,
            secondary_feature,
            secondary_rule,
            scope,
            outcome,
            horizon_sessions,
            _text(benchmark_kind, "REPLICATION_BENCHMARK_KIND_INVALID", maximum=80),
            dataset_snapshot_id,
            replay_run_id,
            cross_market_dataset_id,
            train_period_start,
            train_period_end,
            minimum_effective_sample,
            statistical_method_id,
            requirements,
            temporal_partition_policy_id,
            volatility_state_policy_id,
            alignment_convention_id,
            segments_payload,
            is_in_sample_replication,
            _text(rationale, "REPLICATION_RATIONALE_INVALID"),
            _text(original_agenda_objective, "REPLICATION_AGENDA_OBJECTIVE_INVALID", maximum=2_000),
        )
        return cls(
            replication_id=deterministic_id("research_replication_specification", payload),
            follow_up_id=follow_up_id,
            parent_hypothesis_id=parent_hypothesis_id,
            parent_conclusion_id=parent_conclusion_id,
            parent_agenda_id=parent_agenda_id,
            v1_calibration_id=v1_calibration_id,
            v1_priority_assessment_id=v1_priority_assessment_id,
            v2_calibration_id=v2_calibration_id,
            v2_classification_id=v2_classification_id,
            v2_matched_stratum=v2_matched_stratum,
            v2_matched_null_count=v2_matched_null_count,
            v2_matched_percentile=v2_matched_percentile,
            research_design=design_value,
            conditioning_shape=shape_value,
            primary_feature_id=primary_feature,
            primary_condition_rule=primary_rule,
            secondary_feature_id=secondary_feature,
            secondary_condition_rule=secondary_rule,
            market_scope=scope,
            outcome_id=outcome,
            horizon_sessions=horizon_sessions,
            benchmark_kind=payload[21],
            dataset_snapshot_id=dataset_snapshot_id,
            replay_run_id=replay_run_id,
            cross_market_dataset_id=cross_market_dataset_id,
            train_period_start=train_period_start,
            train_period_end=train_period_end,
            minimum_effective_sample=minimum_effective_sample,
            statistical_method_id=statistical_method_id,
            robustness_requirements=requirements,
            temporal_partition_policy_id=temporal_partition_policy_id,
            volatility_state_policy_id=volatility_state_policy_id,
            alignment_convention_id=alignment_convention_id,
            exposure_segments=tuple(exposure_segments),
            is_in_sample_replication=is_in_sample_replication,
            rationale=payload[35],
            original_agenda_objective=payload[36],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 14/15: retained timing-alignment check
# ==========================================================================


@dataclass(frozen=True, slots=True)
class TimingAlignmentResult:
    """One retained cross-market timing check (sections 14/15): whether
    equity-volatility evidence for date T was genuinely available before
    the BTC outcome window it conditions began, under one declared
    alignment convention. ``ordering_valid is False`` for the primary
    convention is a critical acceptance failure (section 14); a lagged
    alternative existing to additionally check is a robustness axis, never
    a way to rescue a failed primary check."""

    alignment_id: str
    replication_id: str
    convention: TimingConvention
    ordering_valid: bool
    checked_date_count: int
    violation_count: int
    sample_count: int
    effective_sample_count: Decimal
    benchmark_sample_count: int
    interaction_effect: Decimal | None
    cohens_d: Decimal | None
    note: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        replication_id: str,
        convention: TimingConvention | str,
        ordering_valid: bool,
        checked_date_count: int,
        violation_count: int,
        sample_count: int,
        effective_sample_count: Decimal,
        benchmark_sample_count: int,
        interaction_effect: Decimal | None,
        cohens_d: Decimal | None,
        note: str,
        created_at: datetime,
    ) -> TimingAlignmentResult:
        if not replication_id:
            raise ReplicationError("TIMING_ALIGNMENT_REPLICATION_ID_REQUIRED")
        convention_value = TimingConvention(convention)
        if checked_date_count < 0 or violation_count < 0 or violation_count > checked_date_count:
            raise ReplicationError("TIMING_ALIGNMENT_COUNTS_INVALID")
        if sample_count < 0 or benchmark_sample_count < 0 or effective_sample_count < 0:
            raise ReplicationError("TIMING_ALIGNMENT_SAMPLE_COUNTS_INVALID")
        created = _aware(created_at, "TIMING_ALIGNMENT_CREATED_AT")
        payload = (
            RESEARCH_REPLICATION_V1,
            replication_id,
            convention_value,
            ordering_valid,
            checked_date_count,
            violation_count,
            sample_count,
            effective_sample_count,
            benchmark_sample_count,
            interaction_effect,
            cohens_d,
            _text(note, "TIMING_ALIGNMENT_NOTE_INVALID"),
        )
        return cls(
            alignment_id=deterministic_id("research_replication_timing", payload),
            replication_id=replication_id,
            convention=convention_value,
            ordering_valid=ordering_valid,
            checked_date_count=checked_date_count,
            violation_count=violation_count,
            sample_count=sample_count,
            effective_sample_count=effective_sample_count,
            benchmark_sample_count=benchmark_sample_count,
            interaction_effect=interaction_effect,
            cohens_d=cohens_d,
            note=payload[11],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Sections 6/7: temporal and volatility-state partition results (one shape)
# ==========================================================================


@dataclass(frozen=True, slots=True)
class ReplicationPartitionResult:
    """One retained slice of the frozen interaction, restricted to a
    predeclared partition (sections 6/7): the full sample, one of the
    three TIME_PERIOD thirds, or one VOLATILITY_STATE regime label. Only
    ``FULL_SAMPLE`` ever carries a genuine ``matched_percentile`` -- every
    finer partition explicitly reports why it does not (section 7: "do not
    claim evidence from a regime whose calibration population is
    insufficient" -- no per-partition matched-null battery was built)."""

    partition_id: str
    replication_id: str
    partition_kind: PartitionKind
    label: str
    start_date: str | None
    end_date: str | None
    sample_count: int
    effective_sample_count: Decimal
    benchmark_sample_count: int
    sample_mean: Decimal | None
    benchmark_mean: Decimal | None
    interaction_effect: Decimal | None
    cohens_d: Decimal | None
    sign: int
    beats_benchmark: bool | None
    matched_null_count: int
    matched_percentile: Decimal | None
    calibration_note: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        replication_id: str,
        partition_kind: PartitionKind | str,
        label: str,
        start_date: str | None,
        end_date: str | None,
        sample_count: int,
        effective_sample_count: Decimal,
        benchmark_sample_count: int,
        sample_mean: Decimal | None,
        benchmark_mean: Decimal | None,
        interaction_effect: Decimal | None,
        cohens_d: Decimal | None,
        beats_benchmark: bool | None,
        matched_null_count: int,
        matched_percentile: Decimal | None,
        calibration_note: str,
        created_at: datetime,
    ) -> ReplicationPartitionResult:
        if not replication_id or not label:
            raise ReplicationError("PARTITION_RESULT_IDENTITY_REQUIRED")
        kind_value = PartitionKind(partition_kind)
        if sample_count < 0 or benchmark_sample_count < 0 or effective_sample_count < 0:
            raise ReplicationError("PARTITION_RESULT_SAMPLE_COUNTS_INVALID")
        if matched_null_count < 0:
            raise ReplicationError("PARTITION_RESULT_MATCHED_NULL_COUNT_INVALID")
        sign = 0
        if interaction_effect is not None and interaction_effect != 0:
            sign = 1 if interaction_effect > 0 else -1
        created = _aware(created_at, "PARTITION_RESULT_CREATED_AT")
        payload = (
            RESEARCH_REPLICATION_V1,
            replication_id,
            kind_value,
            label,
            start_date,
            end_date,
            sample_count,
            effective_sample_count,
            benchmark_sample_count,
            sample_mean,
            benchmark_mean,
            interaction_effect,
            cohens_d,
            sign,
            beats_benchmark,
            matched_null_count,
            matched_percentile,
            _text(calibration_note, "PARTITION_RESULT_NOTE_INVALID"),
        )
        return cls(
            partition_id=deterministic_id("research_replication_partition", payload),
            replication_id=replication_id,
            partition_kind=kind_value,
            label=label,
            start_date=start_date,
            end_date=end_date,
            sample_count=sample_count,
            effective_sample_count=effective_sample_count,
            benchmark_sample_count=benchmark_sample_count,
            sample_mean=sample_mean,
            benchmark_mean=benchmark_mean,
            interaction_effect=interaction_effect,
            cohens_d=cohens_d,
            sign=sign,
            beats_benchmark=beats_benchmark,
            matched_null_count=matched_null_count,
            matched_percentile=matched_percentile,
            calibration_note=payload[17],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Sections 10/11/12: effect size, distribution, and outlier robustness
# ==========================================================================


@dataclass(frozen=True, slots=True)
class ReplicationEffectProfile:
    """One retained answer to "statistically unusual, or actually
    material?" (section 10), the distributional shape behind it (section
    11), and its predeclared outlier-robustness checks (section 12) -- all
    computed once, over the frozen full-sample specification, and kept
    together because every one of these numbers describes the same
    underlying sample."""

    profile_id: str
    replication_id: str
    unconditional_mean: Decimal | None
    unconditional_sample_count: int
    low_vol_mean: Decimal | None
    low_vol_sample_count: int
    low_vol_median: Decimal | None
    low_vol_hit_rate: Decimal | None
    low_vol_stdev: Decimal | None
    high_vol_mean: Decimal | None
    high_vol_sample_count: int
    high_vol_median: Decimal | None
    high_vol_hit_rate: Decimal | None
    high_vol_stdev: Decimal | None
    high_vol_p10: Decimal | None
    high_vol_p90: Decimal | None
    interaction_effect: Decimal | None
    cohens_d: Decimal | None
    benchmark_relative_effect: Decimal | None
    practical_effect_floor: Decimal
    meets_practical_floor: bool
    winsorized_interaction_effect: Decimal | None
    robust_median_interaction_effect: Decimal | None
    leave_one_third_out_effects: tuple[tuple[str, Decimal | None], ...]
    holdout_window_excluded_effect: Decimal | None
    most_influential_dates: tuple[str, ...]
    outlier_robust: bool
    outlier_dependence_note: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        replication_id: str,
        unconditional_mean: Decimal | None,
        unconditional_sample_count: int,
        low_vol_mean: Decimal | None,
        low_vol_sample_count: int,
        low_vol_median: Decimal | None,
        low_vol_hit_rate: Decimal | None,
        low_vol_stdev: Decimal | None,
        high_vol_mean: Decimal | None,
        high_vol_sample_count: int,
        high_vol_median: Decimal | None,
        high_vol_hit_rate: Decimal | None,
        high_vol_stdev: Decimal | None,
        high_vol_p10: Decimal | None,
        high_vol_p90: Decimal | None,
        interaction_effect: Decimal | None,
        cohens_d: Decimal | None,
        benchmark_relative_effect: Decimal | None,
        practical_effect_floor: Decimal,
        winsorized_interaction_effect: Decimal | None,
        robust_median_interaction_effect: Decimal | None,
        leave_one_third_out_effects: tuple[tuple[str, Decimal | None], ...],
        holdout_window_excluded_effect: Decimal | None,
        most_influential_dates: tuple[str, ...],
        outlier_robust: bool,
        outlier_dependence_note: str,
        created_at: datetime,
    ) -> ReplicationEffectProfile:
        if not replication_id:
            raise ReplicationError("EFFECT_PROFILE_REPLICATION_ID_REQUIRED")
        if unconditional_sample_count < 0 or low_vol_sample_count < 0 or high_vol_sample_count < 0:
            raise ReplicationError("EFFECT_PROFILE_SAMPLE_COUNTS_INVALID")
        if practical_effect_floor <= 0:
            raise ReplicationError("EFFECT_PROFILE_PRACTICAL_FLOOR_INVALID")
        meets_floor = cohens_d is not None and abs(cohens_d) >= practical_effect_floor
        created = _aware(created_at, "EFFECT_PROFILE_CREATED_AT")
        payload = (
            RESEARCH_REPLICATION_V1,
            replication_id,
            unconditional_mean,
            unconditional_sample_count,
            low_vol_mean,
            low_vol_sample_count,
            low_vol_median,
            low_vol_hit_rate,
            low_vol_stdev,
            high_vol_mean,
            high_vol_sample_count,
            high_vol_median,
            high_vol_hit_rate,
            high_vol_stdev,
            high_vol_p10,
            high_vol_p90,
            interaction_effect,
            cohens_d,
            benchmark_relative_effect,
            practical_effect_floor,
            meets_floor,
            winsorized_interaction_effect,
            robust_median_interaction_effect,
            tuple(leave_one_third_out_effects),
            holdout_window_excluded_effect,
            tuple(most_influential_dates),
            outlier_robust,
            _text(outlier_dependence_note, "EFFECT_PROFILE_NOTE_INVALID"),
        )
        return cls(
            profile_id=deterministic_id("research_replication_effect_profile", payload),
            replication_id=replication_id,
            unconditional_mean=unconditional_mean,
            unconditional_sample_count=unconditional_sample_count,
            low_vol_mean=low_vol_mean,
            low_vol_sample_count=low_vol_sample_count,
            low_vol_median=low_vol_median,
            low_vol_hit_rate=low_vol_hit_rate,
            low_vol_stdev=low_vol_stdev,
            high_vol_mean=high_vol_mean,
            high_vol_sample_count=high_vol_sample_count,
            high_vol_median=high_vol_median,
            high_vol_hit_rate=high_vol_hit_rate,
            high_vol_stdev=high_vol_stdev,
            high_vol_p10=high_vol_p10,
            high_vol_p90=high_vol_p90,
            interaction_effect=interaction_effect,
            cohens_d=cohens_d,
            benchmark_relative_effect=benchmark_relative_effect,
            practical_effect_floor=practical_effect_floor,
            meets_practical_floor=meets_floor,
            winsorized_interaction_effect=winsorized_interaction_effect,
            robust_median_interaction_effect=robust_median_interaction_effect,
            leave_one_third_out_effects=tuple(leave_one_third_out_effects),
            holdout_window_excluded_effect=holdout_window_excluded_effect,
            most_influential_dates=tuple(most_influential_dates),
            outlier_robust=outlier_robust,
            outlier_dependence_note=payload[27],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 13: regime-confound plausibility (descriptive only -- never a new
# combinatorial search, never a causal adjustment)
# ==========================================================================


@dataclass(frozen=True, slots=True)
class ConfoundDimensionResult:
    """One overlapping regime dimension's descriptive concentration check
    (section 13): does the interaction's own high-momentum/high-vol sample
    concentrate inside one label of an *already-retained* regime
    dimension, and does the interaction effect persist within that
    dominant label? ``confound_plausible`` only ever flags a description,
    never a causal adjustment."""

    dimension: str
    dominant_trial_label: str
    trial_concentration: Decimal
    dominant_benchmark_label: str
    benchmark_concentration: Decimal
    same_dominant_label: bool
    effect_within_dominant_label: Decimal | None
    dominant_label_sample_count: int
    confound_plausible: bool
    note: str


@dataclass(frozen=True, slots=True)
class ReplicationConfoundAssessment:
    """The retained answer to section 13's question across every
    overlapping regime dimension the milestone lists (equity risk tone,
    equity trend, equity breadth, BTC volatility, BTC trend, bond/risk
    condition)."""

    assessment_id: str
    replication_id: str
    dimensions: tuple[ConfoundDimensionResult, ...]
    overall_confound_resolved: bool
    note: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        replication_id: str,
        dimensions: tuple[ConfoundDimensionResult, ...],
        note: str,
        created_at: datetime,
    ) -> ReplicationConfoundAssessment:
        if not replication_id:
            raise ReplicationError("CONFOUND_ASSESSMENT_REPLICATION_ID_REQUIRED")
        if not dimensions:
            raise ReplicationError("CONFOUND_ASSESSMENT_DIMENSIONS_REQUIRED")
        overall_resolved = not any(item.confound_plausible for item in dimensions)
        created = _aware(created_at, "CONFOUND_ASSESSMENT_CREATED_AT")
        dims_payload = tuple(
            (
                item.dimension,
                item.dominant_trial_label,
                item.trial_concentration,
                item.dominant_benchmark_label,
                item.benchmark_concentration,
                item.same_dominant_label,
                item.effect_within_dominant_label,
                item.dominant_label_sample_count,
                item.confound_plausible,
                item.note,
            )
            for item in dimensions
        )
        payload = (
            RESEARCH_REPLICATION_V1,
            replication_id,
            dims_payload,
            overall_resolved,
            _text(note, "CONFOUND_ASSESSMENT_NOTE_INVALID"),
        )
        return cls(
            assessment_id=deterministic_id("research_replication_confound", payload),
            replication_id=replication_id,
            dimensions=tuple(dimensions),
            overall_confound_resolved=overall_resolved,
            note=payload[4],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Sections 17-20: the replication conclusion
# ==========================================================================


def classify_follow_up_replication(
    *,
    conclusion_state: ReplicationConclusionState,
    forward_confirmation_required: bool,
) -> FollowUpReplicationQualification:
    """Section 20's deterministic follow-up qualification -- derived
    entirely from the retained conclusion state, never selected to make
    the result look more supported than the evidence shows."""

    if conclusion_state in (
        ReplicationConclusionState.NULL_COMPATIBLE,
        ReplicationConclusionState.TIMING_INVALID,
    ):
        return FollowUpReplicationQualification.REJECTED
    if conclusion_state in (
        ReplicationConclusionState.INSUFFICIENT_EVIDENCE,
        ReplicationConclusionState.CONFOUNDING_NOT_RESOLVED,
    ):
        return FollowUpReplicationQualification.INSUFFICIENT
    if conclusion_state is ReplicationConclusionState.UNSTABLE:
        return FollowUpReplicationQualification.RESEARCH_ONLY
    if conclusion_state is ReplicationConclusionState.PARTIALLY_REPLICATED:
        return FollowUpReplicationQualification.FORWARD_CONFIRMATION_REQUIRED
    # REPLICATED
    if forward_confirmation_required:
        return FollowUpReplicationQualification.FORWARD_CONFIRMATION_REQUIRED
    return FollowUpReplicationQualification.REPLICATION_SUPPORTED


@dataclass(frozen=True, slots=True)
class ReplicationConclusion:
    """Section 17's retained replication conclusion, distinct from (and
    never a rewrite of) the original ``ResearchConclusion``. Section 18:
    ``evidence_grade`` may preserve, downgrade, or -- only when every one
    of ``evidence_grade_upgraded``'s justifying conditions holds -- exceed
    the historical calibrated grade; historical grades themselves remain
    untouched everywhere else in the system."""

    conclusion_id: str
    replication_id: str
    follow_up_id: str
    parent_hypothesis_id: str
    conclusion_state: ReplicationConclusionState
    evidence_grade: EvidenceGrade
    evidence_grade_upgraded: bool
    period_stability_note: str
    volatility_stability_note: str
    timing_note: str
    matched_null_note: str
    effect_size_note: str
    outlier_robustness_note: str
    confound_note: str
    transaction_cost_relevance: TransactionCostRelevance
    forward_confirmation_required: bool
    forward_confirmation_reason: str
    minimum_forward_observation_policy: str
    qualification: FollowUpReplicationQualification
    limitations: tuple[str, ...]
    rationale: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        replication_id: str,
        follow_up_id: str,
        parent_hypothesis_id: str,
        conclusion_state: ReplicationConclusionState | str,
        evidence_grade: EvidenceGrade | str,
        evidence_grade_upgraded: bool,
        period_stability_note: str,
        volatility_stability_note: str,
        timing_note: str,
        matched_null_note: str,
        effect_size_note: str,
        outlier_robustness_note: str,
        confound_note: str,
        transaction_cost_relevance: TransactionCostRelevance | str,
        forward_confirmation_required: bool,
        forward_confirmation_reason: str,
        minimum_forward_observation_policy: str,
        limitations: tuple[str, ...],
        rationale: str,
        created_at: datetime,
    ) -> ReplicationConclusion:
        if not replication_id or not follow_up_id or not parent_hypothesis_id:
            raise ReplicationError("REPLICATION_CONCLUSION_LINEAGE_REQUIRED")
        state_value = ReplicationConclusionState(conclusion_state)
        grade_value = EvidenceGrade(evidence_grade)
        cost_value = TransactionCostRelevance(transaction_cost_relevance)
        qualification = classify_follow_up_replication(
            conclusion_state=state_value,
            forward_confirmation_required=forward_confirmation_required,
        )
        created = _aware(created_at, "REPLICATION_CONCLUSION_CREATED_AT")
        payload = (
            RESEARCH_REPLICATION_V1,
            replication_id,
            follow_up_id,
            parent_hypothesis_id,
            state_value,
            grade_value,
            evidence_grade_upgraded,
            _text(period_stability_note, "REPLICATION_CONCLUSION_NOTE_INVALID"),
            _text(volatility_stability_note, "REPLICATION_CONCLUSION_NOTE_INVALID"),
            _text(timing_note, "REPLICATION_CONCLUSION_NOTE_INVALID"),
            _text(matched_null_note, "REPLICATION_CONCLUSION_NOTE_INVALID"),
            _text(effect_size_note, "REPLICATION_CONCLUSION_NOTE_INVALID"),
            _text(outlier_robustness_note, "REPLICATION_CONCLUSION_NOTE_INVALID"),
            _text(confound_note, "REPLICATION_CONCLUSION_NOTE_INVALID"),
            cost_value,
            forward_confirmation_required,
            _text(forward_confirmation_reason, "REPLICATION_CONCLUSION_NOTE_INVALID"),
            _text(minimum_forward_observation_policy, "REPLICATION_CONCLUSION_NOTE_INVALID"),
            qualification,
            tuple(limitations),
            _text(rationale, "REPLICATION_CONCLUSION_RATIONALE_INVALID", maximum=4_000),
        )
        return cls(
            conclusion_id=deterministic_id("research_replication_conclusion", payload),
            replication_id=replication_id,
            follow_up_id=follow_up_id,
            parent_hypothesis_id=parent_hypothesis_id,
            conclusion_state=state_value,
            evidence_grade=grade_value,
            evidence_grade_upgraded=evidence_grade_upgraded,
            period_stability_note=payload[7],
            volatility_stability_note=payload[8],
            timing_note=payload[9],
            matched_null_note=payload[10],
            effect_size_note=payload[11],
            outlier_robustness_note=payload[12],
            confound_note=payload[13],
            transaction_cost_relevance=cost_value,
            forward_confirmation_required=forward_confirmation_required,
            forward_confirmation_reason=payload[16],
            minimum_forward_observation_policy=payload[17],
            qualification=qualification,
            limitations=tuple(limitations),
            rationale=payload[20],
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def research_replication_warnings() -> tuple[str, ...]:
    from apex.domain.cross_market_research import CAUSAL_LANGUAGE_POLICY as _CAUSAL_POLICY
    from apex.domain.research_calibration import CALIBRATION_ALPHA_DISCLAIMER

    return (
        REPLICATION_WARNING,
        CALIBRATION_ALPHA_DISCLAIMER,
        REPLICATION_LINEAGE_NOTE,
        IN_SAMPLE_REPLICATION_NOTE,
        _CAUSAL_POLICY,
        FINITE_SAMPLE_WARNING,
    )
