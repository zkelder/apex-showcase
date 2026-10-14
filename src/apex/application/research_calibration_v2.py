"""Adapter-free orchestration for Research Calibration v2: Template-Matched
Nulls and Stability.

Builds a bounded, template-matched null-control battery -- distinct null
generators per :class:`~apex.domain.research_calibration_v2.ResearchDesign`
that preserve each design's own conditioning/interaction/dependence
structure rather than reusing one generic "shuffle the feature" null for
every shape -- runs every control through the *same* governed
trial/robustness/conclusion/promotion machinery genuine hypotheses and v1's
calibration already use, and produces a template-matched percentile,
time/regime stability slices, a bounded walk-forward-null subset, and a
non-destructive requalification of the three currently ``JUSTIFIED``
follow-up proposals plus the prior agenda's genuine ``WEAK`` findings.

This module never widens or reruns the genuine 25-hypothesis agenda
(``application.cross_market_research``) or v1's calibration battery
(``application.research_calibration``); it only reads their already-
retained evidence and reuses ``BuildCrossMarketPanel``'s identical output.
Every service here has authority ``NONE``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Protocol

from apex.application.cross_market_research import (
    AgendaTimeline,
    CrossMarketPanel,
    CrossMarketResearchRepository,
    derive_knowledge_status,
    record_panel_sample_trial,
    resolve_condition_dates,
)
from apex.application.research_calibration import (
    MINIMUM_TRIAL_SAMPLE,
    CalibrationConflict,
    CalibrationNotFound,
    CalibrationRepository,
    median_split_dates,
)
from apex.application.research_methodology import (
    BuildConclusionCommand,
    BuildResearchConclusion,
    DeclareGovernancePolicyCommand,
    DeclareResearchGovernancePolicy,
    EvaluatePromotion,
    EvaluatePromotionCommand,
    MethodologyConflict,
    RecordGroupedResearchTrialCommand,
    RecordHoldoutExposure,
    RecordHoldoutExposureCommand,
    ResearchMethodologyApplicationError,
    ResearchMethodologyRepository,
)
from apex.domain.cross_market_research import (
    REGIME_CONDITION_FEATURE_IDS,
    ConditionRule,
    CrossMarketRegimeDimension,
    CrossMarketRegimeSnapshot,
    DatedValue,
    FeatureFamily,
    FeatureId,
    GeneratedHypothesis,
    KnowledgeStatus,
    MarketId,
    OutcomeFamily,
    OutcomeId,
    ResearchKnowledgeEntry,
    deterministic_shuffle,
    lag_displace,
)
from apex.domain.historical_replay import ResearchMethodId
from apex.domain.research_calibration import (
    CALIBRATED_PROMOTION_POLICY,
    EFFECT_SIZE_FLOOR_POLICY_V1,
    EVIDENCE_GRADE_POLICY_V1,
    MINIMUM_ADEQUATE_CONTROL_COUNT,
    CalibratedPromotionEvaluation,
    CalibrationBreakdown,
    CalibrationPlan,
    CalibrationRateSummary,
    CalibrationReport,
    CalibrationRun,
    CalibrationRunStatus,
    EvidenceGrade,
    EvidenceProfile,
    FollowUpPriorityClass,
    NullControlDesignFamily,
    NullControlFamily,
    NullControlNature,
    NullControlOutcome,
    NullControlSpec,
    NullGenerationMethod,
    PriorHypothesisRecalibration,
    StatisticalMethodComparison,
    assess_negative_control_quality,
    block_permute,
    classify_practical_significance,
    derive_evidence_grade,
    effect_meets_floor,
    evidence_grade_at_least,
    sign_randomize,
    synthetic_ar1_surrogate,
)
from apex.domain.research_calibration_v2 import (
    CALIBRATION_STRATUM_INSUFFICIENT,
    MINIMUM_MATCHED_NULL_COUNT,
    MINIMUM_V2_CONTROL_BUDGET,
    ConditioningShape,
    DesignClassification,
    FollowUpQualification,
    FollowUpQualificationV2,
    MatchedCalibrationResult,
    NullControlSpecV2,
    ResearchDesign,
    StabilitySlice,
    WalkForwardNullSummary,
    classify_follow_up_v2,
    matched_stratum_key,
    research_design_for_hypothesis,
)
from apex.domain.research_methodology import (
    BenchmarkKind,
    ConfigurationOrigin,
    CorrectedTestResult,
    GovernanceIntent,
    MultipleTestingCorrectionMethod,
    PromotionPolicy,
    ResearchConclusion,
    ResearchConclusionState,
    ResearchConfiguration,
    ResearchPhase,
    ResearchTrial,
    RobustnessCheckResult,
    RobustnessCheckRun,
    RobustnessCheckType,
    TrialTestResult,
    apply_benjamini_hochberg_correction,
    apply_bonferroni_correction,
    bound_statistical_decimal,
    robustness_cost_stress,
    two_sided_z_test_p_value,
)
from apex.domain.values import deterministic_id

STATE_SEED = 20260808301
OUTCOME_SEED_BLOCK = 20260808302
OUTCOME_SEED_SIGN = 20260808303
OUTCOME_SEED_SHUFFLE = 20260808304
OUTCOME_SEED_AR1 = 20260808305
FEATURE_SEED_BLOCK = 20260808306
FEATURE_SEED_SIGN = 20260808307

V2_CALIBRATION_VERSION = "V2"
V2_CALIBRATION_HORIZONS = (5, 20)
V2_CALIBRATION_OBJECTIVE = (
    "Measure whether a 99th-percentile result remains unusual when compared with null "
    "experiments that use the same hypothesis template, horizon, conditioning structure, "
    "dependence structure, and research methodology -- rather than against v1's single "
    "pooled 104-control population dominated by ordinary predictive-shaped controls. "
    "Strictly additive on top of Research Signal Calibration v1: creates no Strategy, "
    "Shared Opportunity, PAPER order, or execution authority, and claims no trading edge."
)
V2_CALIBRATION_SEED_POLICY = (
    "Every seeded transform in this battery uses one of seven fixed, declared integer seeds "
    "(20260808301-20260808307), one per transform kind, chosen before any v2 control was "
    "executed; no seed is selected, retried, or swept after observing a result."
)
V2_CALIBRATION_BLOCK_LENGTH_POLICY = (
    "Outcome/feature block-permutation controls use block lengths {5, 20} sessions (matching "
    "v1); persistence state-series block-permutation controls additionally use {10, 40} "
    "sessions to span shorter and longer run-length structure, all fixed a priori."
)


class CalibrationV2ApplicationError(RuntimeError):
    """A v2 calibration application-layer precondition was violated."""


def calibration_v2_warnings() -> tuple[str, ...]:
    from apex.domain.research_calibration_v2 import research_calibration_v2_warnings

    return research_calibration_v2_warnings()


def build_v2_calibration_plan(
    *,
    dataset_snapshot_id: str,
    replay_run_id: str,
    cross_market_dataset_id: str,
    source_agenda_id: str,
    source_calibration_id: str,
    registered_at: datetime,
) -> CalibrationPlan:
    """The one genuine bounded v2 calibration plan's fixed policy. Reuses
    v1's ``CalibrationPlan`` dataclass unmodified (a v2 plan is simply
    another row, distinguished by ``version="V2"``) and the *same*
    dataset/replay/cross-market-dataset/source-agenda lineage v1 used --
    never a refreshed dataset. ``source_calibration_id`` (v1's plan id) is
    recorded in the objective text, giving v2's plan a permanent, hashed,
    human-readable pointer back to v1 (exact v1/v2 lineage) without adding
    a new field to a dataclass that must remain byte-identical for v1."""

    return CalibrationPlan.create(
        version=V2_CALIBRATION_VERSION,
        objective=f"{V2_CALIBRATION_OBJECTIVE} Extends v1 calibration {source_calibration_id}.",
        dataset_snapshot_id=dataset_snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=cross_market_dataset_id,
        source_agenda_id=source_agenda_id,
        research_methodology_policy_id=(
            "RESEARCH_METHODOLOGY_V1+CONTROLLED_CROSS_MARKET_HYPOTHESIS_RESEARCH_V1+"
            "RESEARCH_SIGNAL_CALIBRATION_V1+RESEARCH_SIGNAL_CALIBRATION_V2"
        ),
        allowed_feature_families=tuple(FeatureFamily),
        allowed_outcome_families=tuple(OutcomeFamily),
        allowed_horizons_sessions=V2_CALIBRATION_HORIZONS,
        null_generation_methods=tuple(NullGenerationMethod),
        negative_control_budget=V2_CALIBRATION_TOTAL_CONTROLS,
        random_seed_policy=V2_CALIBRATION_SEED_POLICY,
        multiple_testing_method=MultipleTestingCorrectionMethod.BENJAMINI_HOCHBERG_FDR,
        block_length_policy=V2_CALIBRATION_BLOCK_LENGTH_POLICY,
        conclusion_policy_id="RESEARCH_SIGNAL_CALIBRATION_V2",
        registered_at=registered_at,
    )


def _v2_design_family_for(feature_family: FeatureFamily) -> NullControlDesignFamily:
    name = feature_family.value
    if name.startswith("BTC_"):
        return NullControlDesignFamily.EQUITY_BTC
    if name.startswith("RATES_"):
        return NullControlDesignFamily.FIXED_INCOME_CONTEXT
    if name.startswith("COMMODITY_"):
        return NullControlDesignFamily.COMMODITY_CONTEXT
    return NullControlDesignFamily.EQUITY_INTERNAL


# ==========================================================================
# Section 3/4: declared, template-matched null-control battery
# ==========================================================================


@dataclass(frozen=True, slots=True)
class _GroupedShape:
    """One declared conditional/interaction/regime-comparison shape: a
    real, untransformed conditioning structure the null construction must
    preserve exactly, paired with an outcome that gets nulled."""

    design: ResearchDesign
    shape: ConditioningShape
    primary_feature_id: str
    primary_feature_family: FeatureFamily
    secondary_feature_id: str
    secondary_condition_rule: str
    secondary_condition_label: str | None
    outcome_id: str
    outcome_family: OutcomeFamily
    horizon_sessions: int
    market_scope: tuple[MarketId, ...]
    rationale: str


# -- CONDITIONAL: two strata sized to make the two JUSTIFIED CONDITIONAL
# follow-ups' own matched stratum meaningful (>= MINIMUM_MATCHED_NULL_COUNT
# after expansion across null methods below), plus one smaller diversity
# shape for overall design-level coverage. ---------------------------------
_CONDITIONAL_SHAPES: tuple[_GroupedShape, ...] = (
    _GroupedShape(
        ResearchDesign.CONDITIONAL,
        ConditioningShape.NUMERIC_SPLIT,
        "BTC_RETURN_20",
        FeatureFamily.BTC_MOMENTUM,
        "BTC_EQUITY_CORRELATION_60",
        "ABOVE_TRAIN_MEDIAN",
        None,
        "BTC_FORWARD_RETURN",
        OutcomeFamily.BTC_RETURN,
        20,
        (MarketId.BTC, MarketId.US_EQUITY),
        "Matches the BTC-momentum/BTC-equity-correlation JUSTIFIED follow-up's exact stratum.",
    ),
    _GroupedShape(
        ResearchDesign.CONDITIONAL,
        ConditioningShape.NUMERIC_SPLIT,
        "BTC_VOLATILITY_20",
        FeatureFamily.BTC_VOLATILITY,
        "BTC_EQUITY_CORRELATION_60",
        "ABOVE_TRAIN_MEDIAN",
        None,
        "BTC_FORWARD_RETURN",
        OutcomeFamily.BTC_RETURN,
        20,
        (MarketId.BTC, MarketId.US_EQUITY),
        "Same matched stratum as the BTC-momentum JUSTIFIED follow-up; different primary feature.",
    ),
    _GroupedShape(
        ResearchDesign.CONDITIONAL,
        ConditioningShape.NUMERIC_SPLIT,
        "BTC_RETURN_90",
        FeatureFamily.BTC_MOMENTUM,
        "EQUITY_AVERAGE_CORRELATION_60",
        "ABOVE_TRAIN_MEDIAN",
        None,
        "BTC_FORWARD_RETURN",
        OutcomeFamily.BTC_RETURN,
        20,
        (MarketId.BTC, MarketId.US_EQUITY),
        "Same matched stratum as the BTC-momentum JUSTIFIED follow-up; different secondary "
        "feature.",
    ),
    _GroupedShape(
        ResearchDesign.CONDITIONAL,
        ConditioningShape.LABEL_SPLIT,
        "RATES_PROXY_RETURN_20",
        FeatureFamily.RATES_PROXY_TREND,
        "BOND_RISK_RELATIONSHIP_REGIME_LABEL",
        "LABEL_EQUALS",
        "RATE_TAILWIND",
        "EQUITY_FORWARD_RETURN",
        OutcomeFamily.EQUITY_BROAD_RETURN,
        20,
        (MarketId.RATES_PROXY, MarketId.US_EQUITY),
        "Matches the rates-proxy/bond-risk-regime JUSTIFIED follow-up's exact stratum.",
    ),
    _GroupedShape(
        ResearchDesign.CONDITIONAL,
        ConditioningShape.LABEL_SPLIT,
        "RATES_PROXY_RETURN_20",
        FeatureFamily.RATES_PROXY_TREND,
        "BOND_RISK_RELATIONSHIP_REGIME_LABEL",
        "LABEL_EQUALS",
        "RATE_HEADWIND",
        "EQUITY_FORWARD_RETURN",
        OutcomeFamily.EQUITY_BROAD_RETURN,
        20,
        (MarketId.RATES_PROXY, MarketId.US_EQUITY),
        "Same matched stratum as the rates-proxy JUSTIFIED follow-up; different regime label.",
    ),
    _GroupedShape(
        ResearchDesign.CONDITIONAL,
        ConditioningShape.LABEL_SPLIT,
        "EQUITY_RETURN_60",
        FeatureFamily.EQUITY_TREND,
        "BOND_RISK_RELATIONSHIP_REGIME_LABEL",
        "LABEL_EQUALS",
        "RATE_TAILWIND",
        "EQUITY_FORWARD_RETURN",
        OutcomeFamily.EQUITY_BROAD_RETURN,
        20,
        (MarketId.RATES_PROXY, MarketId.US_EQUITY),
        "Same matched stratum as the rates-proxy JUSTIFIED follow-up; different primary feature.",
    ),
    _GroupedShape(
        ResearchDesign.CONDITIONAL,
        ConditioningShape.NUMERIC_SPLIT,
        "EQUITY_VOLATILITY_20",
        FeatureFamily.EQUITY_VOLATILITY,
        "EQUITY_AVERAGE_CORRELATION_60",
        "ABOVE_TRAIN_MEDIAN",
        None,
        "EQUITY_FORWARD_VOLATILITY",
        OutcomeFamily.EQUITY_BROAD_VOLATILITY,
        5,
        (MarketId.US_EQUITY,),
        "Diversity shape: a same-market conditional design not tied to a JUSTIFIED follow-up.",
    ),
)

_INTERACTION_SHAPES: tuple[_GroupedShape, ...] = (
    _GroupedShape(
        ResearchDesign.INTERACTION,
        ConditioningShape.NUMERIC_INTERACTION,
        "BTC_RETURN_20",
        FeatureFamily.BTC_MOMENTUM,
        "EQUITY_VOLATILITY_20",
        "ABOVE_TRAIN_MEDIAN",
        None,
        "BTC_FORWARD_RETURN",
        OutcomeFamily.BTC_RETURN,
        20,
        (MarketId.BTC, MarketId.US_EQUITY),
        "Matches the BTC-momentum x equity-volatility JUSTIFIED follow-up's exact stratum.",
    ),
    _GroupedShape(
        ResearchDesign.INTERACTION,
        ConditioningShape.NUMERIC_INTERACTION,
        "BTC_RETURN_90",
        FeatureFamily.BTC_MOMENTUM,
        "EQUITY_VOLATILITY_20",
        "ABOVE_TRAIN_MEDIAN",
        None,
        "BTC_FORWARD_RETURN",
        OutcomeFamily.BTC_RETURN,
        20,
        (MarketId.BTC, MarketId.US_EQUITY),
        "Same matched stratum as the INTERACTION JUSTIFIED follow-up; different primary feature.",
    ),
    _GroupedShape(
        ResearchDesign.INTERACTION,
        ConditioningShape.NUMERIC_INTERACTION,
        "BTC_RETURN_20",
        FeatureFamily.BTC_MOMENTUM,
        "EQUITY_AVERAGE_CORRELATION_60",
        "ABOVE_TRAIN_MEDIAN",
        None,
        "BTC_FORWARD_RETURN",
        OutcomeFamily.BTC_RETURN,
        20,
        (MarketId.BTC, MarketId.US_EQUITY),
        "Same matched stratum as the INTERACTION JUSTIFIED follow-up; different secondary feature.",
    ),
    _GroupedShape(
        ResearchDesign.INTERACTION,
        ConditioningShape.NUMERIC_INTERACTION,
        "EQUITY_BREADTH_FRACTION_ADVANCING",
        FeatureFamily.EQUITY_BREADTH,
        "EQUITY_VOLATILITY_20",
        "ABOVE_TRAIN_MEDIAN",
        None,
        "EQUITY_FORWARD_RETURN",
        OutcomeFamily.EQUITY_BROAD_RETURN,
        20,
        (MarketId.US_EQUITY,),
        "Diversity shape: same-market interaction design not tied to a JUSTIFIED follow-up.",
    ),
    _GroupedShape(
        ResearchDesign.INTERACTION,
        ConditioningShape.NUMERIC_INTERACTION,
        "RATES_PROXY_RETURN_20",
        FeatureFamily.RATES_PROXY_TREND,
        "COMMODITY_PROXY_RETURN_20",
        "ABOVE_TRAIN_MEDIAN",
        None,
        "EQUITY_FORWARD_DISPERSION",
        OutcomeFamily.EQUITY_BROAD_DISPERSION,
        5,
        (MarketId.RATES_PROXY, MarketId.COMMODITY_PROXY, MarketId.US_EQUITY),
        "Diversity shape: two-context-proxy interaction design.",
    ),
)

# -- CROSS_MARKET: matches the sector-dispersion -> BTC-volatility
# negative control's exact stratum, plus diversity shapes in both
# directions. Feature (not outcome) is nulled, via declared block-
# displacement/lag-displacement/sign-randomization transforms -- breaking
# cross-market timing while preserving each market's own calendar and
# autocorrelation exactly (section 3). -------------------------------------


@dataclass(frozen=True, slots=True)
class _CrossMarketShape:
    feature_id: str
    feature_family: FeatureFamily
    outcome_id: str
    outcome_family: OutcomeFamily
    horizon_sessions: int
    market_scope: tuple[MarketId, ...]
    rationale: str


_CROSS_MARKET_SHAPES: tuple[_CrossMarketShape, ...] = (
    _CrossMarketShape(
        "EQUITY_SECTOR_DISPERSION_20",
        FeatureFamily.EQUITY_SECTOR_BREADTH,
        "BTC_FORWARD_VOLATILITY",
        OutcomeFamily.BTC_VOLATILITY,
        5,
        (MarketId.US_EQUITY, MarketId.BTC),
        "Matches the sector-equity-dispersion -> BTC-volatility negative control's exact stratum.",
    ),
    _CrossMarketShape(
        "EQUITY_BREADTH_FRACTION_ADVANCING",
        FeatureFamily.EQUITY_BREADTH,
        "BTC_FORWARD_VOLATILITY",
        OutcomeFamily.BTC_VOLATILITY,
        5,
        (MarketId.US_EQUITY, MarketId.BTC),
        "Same matched stratum as the negative control; different equity feature.",
    ),
    _CrossMarketShape(
        "EQUITY_AVERAGE_CORRELATION_60",
        FeatureFamily.EQUITY_CORRELATION,
        "BTC_FORWARD_VOLATILITY",
        OutcomeFamily.BTC_VOLATILITY,
        5,
        (MarketId.US_EQUITY, MarketId.BTC),
        "Same matched stratum as the negative control; different equity feature.",
    ),
    _CrossMarketShape(
        "RATES_PROXY_RETURN_20",
        FeatureFamily.RATES_PROXY_TREND,
        "BTC_FORWARD_RETURN",
        OutcomeFamily.BTC_RETURN,
        20,
        (MarketId.RATES_PROXY, MarketId.BTC),
        "Diversity shape: rates-proxy -> BTC cross-market null.",
    ),
    _CrossMarketShape(
        "COMMODITY_PROXY_RETURN_20",
        FeatureFamily.COMMODITY_PROXY_TREND,
        "BTC_FORWARD_VOLATILITY",
        OutcomeFamily.BTC_VOLATILITY,
        20,
        (MarketId.COMMODITY_PROXY, MarketId.BTC),
        "Diversity shape: commodity-proxy -> BTC cross-market null.",
    ),
    _CrossMarketShape(
        "BTC_RETURN_20",
        FeatureFamily.BTC_MOMENTUM,
        "EQUITY_FORWARD_DISPERSION",
        OutcomeFamily.EQUITY_BROAD_DISPERSION,
        5,
        (MarketId.BTC, MarketId.US_EQUITY),
        "Diversity shape: BTC -> equity cross-market null (opposite direction).",
    ),
)

# -- PERSISTENCE: true state-transition nulls. Each shape's boolean
# condition-membership series (never the underlying feature) is block-
# permuted, preserving overall state frequency and within-block run-length
# structure while destroying genuine transition timing (section 3/9). -----


@dataclass(frozen=True, slots=True)
class _PersistenceShape:
    condition_feature_id: str
    condition_feature_family: FeatureFamily
    condition_rule: str
    condition_label: str | None
    horizon_sessions: int
    outcome_family: OutcomeFamily
    market_scope: tuple[MarketId, ...]
    rationale: str


_PERSISTENCE_SHAPES: tuple[_PersistenceShape, ...] = (
    _PersistenceShape(
        "EQUITY_RISK_TONE_REGIME_LABEL",
        FeatureFamily.EQUITY_BREADTH,
        "LABEL_EQUALS",
        "RISK_ON",
        20,
        OutcomeFamily.EQUITY_BROAD_RETURN,
        (MarketId.US_EQUITY,),
        "Does the RISK_ON regime persist 20 sessions later?",
    ),
    _PersistenceShape(
        "EQUITY_BREADTH_FRACTION_ADVANCING",
        FeatureFamily.EQUITY_BREADTH,
        "ABOVE_TRAIN_MEDIAN",
        None,
        20,
        OutcomeFamily.EQUITY_BROAD_RETURN,
        (MarketId.US_EQUITY,),
        "Does above-median equity breadth persist 20 sessions later?",
    ),
    _PersistenceShape(
        "EQUITY_VOLATILITY_20",
        FeatureFamily.EQUITY_VOLATILITY,
        "ABOVE_TRAIN_MEDIAN",
        None,
        20,
        OutcomeFamily.EQUITY_BROAD_RETURN,
        (MarketId.US_EQUITY,),
        "Does above-median equity volatility persist 20 sessions later?",
    ),
    _PersistenceShape(
        "BTC_RETURN_20",
        FeatureFamily.BTC_MOMENTUM,
        "ABOVE_TRAIN_MEDIAN",
        None,
        5,
        OutcomeFamily.BTC_RETURN,
        (MarketId.BTC,),
        "Does above-median BTC momentum persist 5 sessions later?",
    ),
    _PersistenceShape(
        "BTC_VOLATILITY_20",
        FeatureFamily.BTC_VOLATILITY,
        "ABOVE_TRAIN_MEDIAN",
        None,
        5,
        OutcomeFamily.BTC_RETURN,
        (MarketId.BTC,),
        "Does above-median BTC volatility persist 5 sessions later?",
    ),
    _PersistenceShape(
        "BTC_RETURN_90",
        FeatureFamily.BTC_MOMENTUM,
        "ABOVE_TRAIN_MEDIAN",
        None,
        5,
        OutcomeFamily.BTC_RETURN,
        (MarketId.BTC,),
        "Does above-median 90-session BTC momentum persist 5 sessions later?",
    ),
)
_PERSISTENCE_BLOCK_LENGTHS = (5, 10, 20, 40)

# -- REGIME_COMPARISON: a null two-regime comparison (section 2's sixth
# design; no genuine hypothesis template uses this shape). Real regime-
# label membership is preserved exactly; the outcome is nulled. -----------


@dataclass(frozen=True, slots=True)
class _RegimeComparisonShape:
    regime_feature_id: str
    label_a: str
    label_b: str
    outcome_id: str
    outcome_family: OutcomeFamily
    horizon_sessions: int
    market_scope: tuple[MarketId, ...]
    rationale: str


_REGIME_COMPARISON_SHAPES: tuple[_RegimeComparisonShape, ...] = (
    _RegimeComparisonShape(
        "EQUITY_RISK_TONE_REGIME_LABEL",
        "RISK_ON",
        "RISK_OFF",
        "EQUITY_FORWARD_RETURN",
        OutcomeFamily.EQUITY_BROAD_RETURN,
        20,
        (MarketId.US_EQUITY,),
        "Does subsequent equity return genuinely differ between RISK_ON and RISK_OFF regimes?",
    ),
    _RegimeComparisonShape(
        "EQUITY_RISK_TONE_REGIME_LABEL",
        "RISK_ON",
        "NEUTRAL",
        "EQUITY_FORWARD_RETURN",
        OutcomeFamily.EQUITY_BROAD_RETURN,
        20,
        (MarketId.US_EQUITY,),
        "Does subsequent equity return genuinely differ between RISK_ON and NEUTRAL regimes?",
    ),
    _RegimeComparisonShape(
        "EQUITY_RISK_TONE_REGIME_LABEL",
        "RISK_OFF",
        "NEUTRAL",
        "EQUITY_FORWARD_RETURN",
        OutcomeFamily.EQUITY_BROAD_RETURN,
        20,
        (MarketId.US_EQUITY,),
        "Does subsequent equity return genuinely differ between RISK_OFF and NEUTRAL regimes?",
    ),
    _RegimeComparisonShape(
        "BOND_RISK_RELATIONSHIP_REGIME_LABEL",
        "RATE_TAILWIND",
        "RATE_HEADWIND",
        "EQUITY_FORWARD_VOLATILITY",
        OutcomeFamily.EQUITY_BROAD_VOLATILITY,
        20,
        (MarketId.RATES_PROXY, MarketId.US_EQUITY),
        "Does subsequent equity volatility genuinely differ between bond-risk regimes?",
    ),
    _RegimeComparisonShape(
        "BOND_RISK_RELATIONSHIP_REGIME_LABEL",
        "RATE_TAILWIND",
        "DECOUPLED",
        "EQUITY_FORWARD_VOLATILITY",
        OutcomeFamily.EQUITY_BROAD_VOLATILITY,
        20,
        (MarketId.RATES_PROXY, MarketId.US_EQUITY),
        "Does subsequent equity volatility genuinely differ between bond-risk regimes?",
    ),
    _RegimeComparisonShape(
        "BOND_RISK_RELATIONSHIP_REGIME_LABEL",
        "RATE_HEADWIND",
        "DECOUPLED",
        "EQUITY_FORWARD_VOLATILITY",
        OutcomeFamily.EQUITY_BROAD_VOLATILITY,
        20,
        (MarketId.RATES_PROXY, MarketId.US_EQUITY),
        "Does subsequent equity volatility genuinely differ between bond-risk regimes?",
    ),
)

_OUTCOME_NULL_METHODS: tuple[tuple[NullGenerationMethod, tuple[tuple[str, str], ...]], ...] = (
    (
        NullGenerationMethod.BLOCK_PERMUTATION,
        (("block_length", "5"), ("seed", str(OUTCOME_SEED_BLOCK))),
    ),
    (
        NullGenerationMethod.BLOCK_PERMUTATION,
        (("block_length", "20"), ("seed", str(OUTCOME_SEED_BLOCK))),
    ),
    (NullGenerationMethod.SIGN_RANDOMIZATION, (("seed", str(OUTCOME_SEED_SIGN)),)),
    (NullGenerationMethod.IID_DATE_SHUFFLE, (("seed", str(OUTCOME_SEED_SHUFFLE)),)),
    (NullGenerationMethod.SYNTHETIC_AR1_SURROGATE, (("seed", str(OUTCOME_SEED_AR1)),)),
)
_FEATURE_NULL_METHODS: tuple[tuple[NullGenerationMethod, tuple[tuple[str, str], ...]], ...] = (
    (
        NullGenerationMethod.BLOCK_PERMUTATION,
        (("block_length", "5"), ("seed", str(FEATURE_SEED_BLOCK))),
    ),
    (
        NullGenerationMethod.BLOCK_PERMUTATION,
        (("block_length", "20"), ("seed", str(FEATURE_SEED_BLOCK))),
    ),
    (NullGenerationMethod.TEMPORAL_LAG_DISPLACEMENT, (("lag_sessions", "60"),)),
    (NullGenerationMethod.TEMPORAL_LAG_DISPLACEMENT, (("lag_sessions", "120"),)),
    (NullGenerationMethod.SIGN_RANDOMIZATION, (("seed", str(FEATURE_SEED_SIGN)),)),
)


def _method_family_nature(
    method: NullGenerationMethod, *, persistence: bool = False
) -> tuple[NullControlFamily, NullControlNature]:
    if persistence:
        return (
            NullControlFamily.PERSISTENCE_RANDOM_BASELINE,
            NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
        )
    if method is NullGenerationMethod.TEMPORAL_LAG_DISPLACEMENT:
        return NullControlFamily.TEMPORAL_DISPLACEMENT, NullControlNature.TEMPORAL_NULL
    if method is NullGenerationMethod.SYNTHETIC_AR1_SURROGATE:
        return (
            NullControlFamily.SEEDED_SYNTHETIC_NULL,
            NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
        )
    return (
        NullControlFamily.DETERMINISTIC_PERMUTATION,
        NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
    )


def generate_v2_null_control_specs(
    *, calibration: CalibrationPlan, created_at: datetime
) -> tuple[NullControlSpecV2, ...]:
    """Deterministic, declared-before-results expansion of the fixed
    module-level shape tables above into the full template-matched v2
    battery (sections 3/4). Calling this twice with the same calibration
    always produces byte-identical specs."""

    specs: list[NullControlSpecV2] = []
    ordinal = 0

    def _add(
        *,
        research_design: ResearchDesign,
        conditioning_shape: ConditioningShape,
        family: NullControlFamily,
        nature: NullControlNature,
        design_family: NullControlDesignFamily,
        control_kind: str,
        null_method: NullGenerationMethod,
        null_target: str,
        source_feature_id: str,
        source_feature_family: FeatureFamily,
        secondary_feature_id: str | None,
        secondary_condition_rule: str | None,
        secondary_condition_label: str | None,
        outcome_id: str,
        outcome_family: OutcomeFamily,
        horizon_sessions: int,
        market_scope: tuple[MarketId, ...],
        method_params: tuple[tuple[str, str], ...],
        rationale: str,
    ) -> None:
        nonlocal ordinal
        ordinal += 1
        specs.append(
            NullControlSpecV2.create(
                calibration_id=calibration.calibration_id,
                ordinal=ordinal,
                created_at=created_at,
                research_design=research_design,
                conditioning_shape=conditioning_shape,
                family=family,
                nature=nature,
                design_family=design_family,
                control_kind=control_kind,
                null_method=null_method,
                null_target=null_target,
                source_feature_id=source_feature_id,
                source_feature_family=source_feature_family,
                secondary_feature_id=secondary_feature_id,
                secondary_condition_rule=secondary_condition_rule,
                secondary_condition_label=secondary_condition_label,
                outcome_id=outcome_id,
                outcome_family=outcome_family,
                horizon_sessions=horizon_sessions,
                market_scope=market_scope,
                method_params=method_params,
                rationale=rationale,
            )
        )

    for group, control_kind, design_family in (
        (
            _CONDITIONAL_SHAPES,
            "CONDITIONAL_OUTCOME_NULLED",
            NullControlDesignFamily.INTERACTION_CONDITIONAL,
        ),
        (
            _INTERACTION_SHAPES,
            "INTERACTION_OUTCOME_NULLED",
            NullControlDesignFamily.INTERACTION_CONDITIONAL,
        ),
    ):
        for gshape in group:
            for method, params in _OUTCOME_NULL_METHODS:
                family, nature = _method_family_nature(method)
                _add(
                    research_design=gshape.design,
                    conditioning_shape=gshape.shape,
                    family=family,
                    nature=nature,
                    design_family=design_family,
                    control_kind=control_kind,
                    null_method=method,
                    null_target="OUTCOME",
                    source_feature_id=gshape.primary_feature_id,
                    source_feature_family=gshape.primary_feature_family,
                    secondary_feature_id=gshape.secondary_feature_id,
                    secondary_condition_rule=gshape.secondary_condition_rule,
                    secondary_condition_label=gshape.secondary_condition_label,
                    outcome_id=gshape.outcome_id,
                    outcome_family=gshape.outcome_family,
                    horizon_sessions=gshape.horizon_sessions,
                    market_scope=gshape.market_scope,
                    method_params=params,
                    rationale=gshape.rationale,
                )

    for cshape in _CROSS_MARKET_SHAPES:
        for method, params in _FEATURE_NULL_METHODS:
            family, nature = _method_family_nature(method)
            _add(
                research_design=ResearchDesign.CROSS_MARKET,
                conditioning_shape=ConditioningShape.UNCONDITIONAL,
                family=family,
                nature=nature,
                design_family=_v2_design_family_for(cshape.feature_family),
                control_kind="CROSS_MARKET_BLOCK_DISPLACED"
                if method is NullGenerationMethod.BLOCK_PERMUTATION
                else (
                    "CROSS_MARKET_LAG_DISPLACED"
                    if method is NullGenerationMethod.TEMPORAL_LAG_DISPLACEMENT
                    else "CROSS_MARKET_SIGN_RANDOMIZED"
                ),
                null_method=method,
                null_target="FEATURE",
                source_feature_id=cshape.feature_id,
                source_feature_family=cshape.feature_family,
                secondary_feature_id=None,
                secondary_condition_rule=None,
                secondary_condition_label=None,
                outcome_id=cshape.outcome_id,
                outcome_family=cshape.outcome_family,
                horizon_sessions=cshape.horizon_sessions,
                market_scope=cshape.market_scope,
                method_params=params,
                rationale=cshape.rationale,
            )

    for pshape in _PERSISTENCE_SHAPES:
        for block_length in _PERSISTENCE_BLOCK_LENGTHS:
            family, nature = _method_family_nature(
                NullGenerationMethod.BLOCK_PERMUTATION, persistence=True
            )
            persistence_params: tuple[tuple[str, str], ...] = (
                ("block_length", str(block_length)),
                ("seed", str(STATE_SEED)),
                ("condition_rule", pshape.condition_rule),
            )
            if pshape.condition_label is not None:
                persistence_params = (
                    *persistence_params,
                    ("condition_label", pshape.condition_label),
                )
            _add(
                research_design=ResearchDesign.PERSISTENCE,
                conditioning_shape=ConditioningShape.STATE_TRANSITION,
                family=family,
                nature=nature,
                design_family=_v2_design_family_for(pshape.condition_feature_family),
                control_kind="PERSISTENCE_STATE_BLOCK_PERMUTED",
                null_method=NullGenerationMethod.BLOCK_PERMUTATION,
                null_target="STATE_SERIES",
                source_feature_id=pshape.condition_feature_id,
                source_feature_family=pshape.condition_feature_family,
                secondary_feature_id=None,
                secondary_condition_rule=None,
                secondary_condition_label=None,
                outcome_id=f"PERSISTENCE_STATE_AT_HORIZON_{pshape.horizon_sessions}",
                outcome_family=pshape.outcome_family,
                horizon_sessions=pshape.horizon_sessions,
                market_scope=pshape.market_scope,
                method_params=persistence_params,
                rationale=pshape.rationale,
            )

    for rshape in _REGIME_COMPARISON_SHAPES:
        for method, method_params in _OUTCOME_NULL_METHODS:
            family, nature = _method_family_nature(method)
            regime_params = (
                *method_params,
                ("label_a", rshape.label_a),
                ("label_b", rshape.label_b),
            )
            _add(
                research_design=ResearchDesign.REGIME_COMPARISON,
                conditioning_shape=ConditioningShape.REGIME_LABEL_COMPARISON,
                family=family,
                nature=nature,
                design_family=NullControlDesignFamily.INTERACTION_CONDITIONAL,
                control_kind="REGIME_COMPARISON_OUTCOME_NULLED",
                null_method=method,
                null_target="OUTCOME",
                source_feature_id=rshape.regime_feature_id,
                source_feature_family=FeatureFamily.NEGATIVE_CONTROL,
                secondary_feature_id=None,
                secondary_condition_rule=None,
                secondary_condition_label=None,
                outcome_id=rshape.outcome_id,
                outcome_family=rshape.outcome_family,
                horizon_sessions=rshape.horizon_sessions,
                market_scope=rshape.market_scope,
                method_params=regime_params,
                rationale=rshape.rationale,
            )

    return tuple(specs)


V2_CALIBRATION_TOTAL_CONTROLS = (
    len(_CONDITIONAL_SHAPES) * len(_OUTCOME_NULL_METHODS)
    + len(_INTERACTION_SHAPES) * len(_OUTCOME_NULL_METHODS)
    + len(_CROSS_MARKET_SHAPES) * len(_FEATURE_NULL_METHODS)
    + len(_PERSISTENCE_SHAPES) * len(_PERSISTENCE_BLOCK_LENGTHS)
    + len(_REGIME_COMPARISON_SHAPES) * len(_OUTCOME_NULL_METHODS)
)
if not (MINIMUM_V2_CONTROL_BUDGET <= V2_CALIBRATION_TOTAL_CONTROLS <= 180):
    raise CalibrationV2ApplicationError("V2_CALIBRATION_BUDGET_OUT_OF_DECLARED_BOUNDS")


CALIBRATION_V2_LIMITATIONS = (
    "POINT_IN_TIME_UNIVERSE_INCOMPLETE",
    "SURVIVORSHIP_LIMITED",
    "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE",
    "WALK_FORWARD_NULL_EVALUATION_LIMITED_TO_A_BOUNDED_PREDECLARED_SUBSET",
    "TIME_PERIOD_AND_VOLATILITY_REGIME_STABILITY_SLICES_ARE_A_LIGHTWEIGHT_RECOMPUTATION_"
    "NOT_A_FULL_GOVERNED_PIPELINE_RERUN_PER_SLICE",
    "REGIME_COMPARISON_DESIGN_IS_EXERCISED_ONLY_BY_NULL_CONTROLS_NO_GENUINE_HYPOTHESIS_USES_IT",
)


# ==========================================================================
# Section: template-matched null-series derivation (sections 3/8/9) -- one
# builder per research design, each preserving that design's own real
# conditioning/interaction/dependence structure while nulling only the
# component the milestone specifies (the outcome for
# conditional/interaction/regime-comparison, the feature for cross-market,
# the state series for persistence).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class _NullEvaluation:
    sample_universe: Callable[[frozenset[str]], tuple[frozenset[str], frozenset[str]]]
    outcome_by_date: dict[str, Decimal]
    market_scope: tuple[str, ...]


def _nulled_series(
    series: tuple[DatedValue, ...], *, method: NullGenerationMethod, params: dict[str, str]
) -> tuple[DatedValue, ...]:
    if method is NullGenerationMethod.BLOCK_PERMUTATION:
        return block_permute(
            series, seed=int(params["seed"]), block_length=int(params["block_length"])
        )
    if method is NullGenerationMethod.SIGN_RANDOMIZATION:
        return sign_randomize(series, seed=int(params["seed"]))
    if method is NullGenerationMethod.IID_DATE_SHUFFLE:
        return deterministic_shuffle(series, seed=int(params["seed"]))
    if method is NullGenerationMethod.SYNTHETIC_AR1_SURROGATE:
        return synthetic_ar1_surrogate(series, seed=int(params["seed"]))
    if method is NullGenerationMethod.TEMPORAL_LAG_DISPLACEMENT:
        return lag_displace(series, sessions=int(params["lag_sessions"]))
    raise CalibrationV2ApplicationError("UNSUPPORTED_NULL_METHOD_FOR_V2")


def _condition_dates(
    *,
    feature_id_str: str,
    rule: str,
    label: str | None,
    panel: CrossMarketPanel,
    train_dates: frozenset[str],
) -> frozenset[str]:
    feature_id = FeatureId(feature_id_str)
    if rule == "ABOVE_TRAIN_MEDIAN":
        return median_split_dates(
            feature_by_date=panel.feature_by_date(feature_id), train_dates=train_dates
        )
    if rule == "LABEL_EQUALS":
        return resolve_condition_dates(
            feature_id=feature_id,
            condition_rule=ConditionRule.LABEL_EQUALS,
            condition_label=label,
            feature_by_date={},
            regime_by_date=panel.regime_by_date(),
            train_dates=train_dates,
        )
    raise CalibrationV2ApplicationError("UNSUPPORTED_CONDITION_RULE_FOR_V2")


def _persistence_state_by_date(
    *, condition_dates: frozenset[str], dates: tuple[str, ...], horizon_sessions: int
) -> dict[str, Decimal]:
    """Mirrors ``application.cross_market_research._persistence_outcome_by_
    date`` exactly, generalized to take an already-resolved condition-date
    set (real *or* nulled) so the same function serves both a genuine
    persistence hypothesis's own real state and this milestone's
    block-permuted null state."""

    result: dict[str, Decimal] = {}
    for index in range(len(dates) - horizon_sessions):
        source_date = dates[index]
        if source_date not in condition_dates:
            continue
        target_date = dates[index + horizon_sessions]
        result[source_date] = Decimal(1) if target_date in condition_dates else Decimal(0)
    return result


def build_null_evaluation(
    *, spec: NullControlSpecV2, panel: CrossMarketPanel, train_dates: frozenset[str]
) -> _NullEvaluation:
    params = dict(spec.method_params)
    scope = tuple(item.value for item in spec.market_scope)

    if spec.research_design in (ResearchDesign.CONDITIONAL, ResearchDesign.INTERACTION):
        if spec.secondary_feature_id is None or spec.secondary_condition_rule is None:
            raise CalibrationV2ApplicationError("CONDITIONAL_OR_INTERACTION_SPEC_MISSING_SECONDARY")
        primary_dates = median_split_dates(
            feature_by_date=panel.feature_by_date(FeatureId(spec.source_feature_id)),
            train_dates=train_dates,
        )
        secondary_dates = _condition_dates(
            feature_id_str=spec.secondary_feature_id,
            rule=spec.secondary_condition_rule,
            label=spec.secondary_condition_label,
            panel=panel,
            train_dates=train_dates,
        )
        raw_outcome = panel.outcome_by_date(
            OutcomeId(spec.outcome_id), horizon_sessions=spec.horizon_sessions
        )
        outcome_series = tuple(DatedValue(d, v) for d, v in raw_outcome.items())
        nulled = _nulled_series(outcome_series, method=spec.null_method, params=params)
        outcome_by_date = {
            item.session_date: item.value for item in nulled if item.value is not None
        }

        def sample_universe(period_dates: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
            fixed = period_dates & primary_dates
            return fixed & secondary_dates, fixed - secondary_dates

        return _NullEvaluation(sample_universe, outcome_by_date, scope)

    if spec.research_design is ResearchDesign.REGIME_COMPARISON:
        label_a = params["label_a"]
        label_b = params["label_b"]
        regime_by_date = panel.regime_by_date()
        dimension = REGIME_CONDITION_FEATURE_IDS.get(FeatureId(spec.source_feature_id))
        if dimension is None:
            raise CalibrationV2ApplicationError("REGIME_COMPARISON_FEATURE_HAS_NO_REGIME_DIMENSION")
        group_a = frozenset(
            d for d, r in regime_by_date.items() if r.label_for(dimension) == label_a
        )
        group_b = frozenset(
            d for d, r in regime_by_date.items() if r.label_for(dimension) == label_b
        )
        raw_outcome = panel.outcome_by_date(
            OutcomeId(spec.outcome_id), horizon_sessions=spec.horizon_sessions
        )
        outcome_series = tuple(DatedValue(d, v) for d, v in raw_outcome.items())
        nulled = _nulled_series(outcome_series, method=spec.null_method, params=params)
        outcome_by_date = {
            item.session_date: item.value for item in nulled if item.value is not None
        }

        def sample_universe(period_dates: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
            return period_dates & group_a, period_dates & group_b

        return _NullEvaluation(sample_universe, outcome_by_date, scope)

    if spec.research_design is ResearchDesign.CROSS_MARKET:
        raw_feature = panel.feature_by_date(FeatureId(spec.source_feature_id))
        base = tuple(DatedValue(d, v) for d, v in raw_feature.items())
        nulled_feature = _nulled_series(base, method=spec.null_method, params=params)
        nulled_feature_by_date = {
            item.session_date: item.value for item in nulled_feature if item.value is not None
        }
        primary_dates = median_split_dates(
            feature_by_date=nulled_feature_by_date, train_dates=train_dates
        )
        outcome_by_date = panel.outcome_by_date(
            OutcomeId(spec.outcome_id), horizon_sessions=spec.horizon_sessions
        )

        def sample_universe(period_dates: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
            return period_dates & primary_dates, period_dates

        return _NullEvaluation(sample_universe, outcome_by_date, scope)

    if spec.research_design is ResearchDesign.PERSISTENCE:
        real_condition_dates = _condition_dates(
            feature_id_str=spec.source_feature_id,
            rule=params["condition_rule"],
            label=params.get("condition_label"),
            panel=panel,
            train_dates=train_dates,
        )
        dates = panel.session_dates
        indicator_series = tuple(
            DatedValue(d, Decimal(1) if d in real_condition_dates else Decimal(0)) for d in dates
        )
        permuted = block_permute(
            indicator_series, seed=int(params["seed"]), block_length=int(params["block_length"])
        )
        permuted_condition_dates = frozenset(
            item.session_date for item in permuted if item.value == 1
        )
        outcome_by_date = _persistence_state_by_date(
            condition_dates=permuted_condition_dates,
            dates=dates,
            horizon_sessions=spec.horizon_sessions,
        )

        def sample_universe(period_dates: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
            return period_dates & permuted_condition_dates, period_dates

        return _NullEvaluation(sample_universe, outcome_by_date, scope)

    raise CalibrationV2ApplicationError("UNSUPPORTED_RESEARCH_DESIGN_FOR_V2_NULL_EVALUATION")


# ==========================================================================
# Section: RunNullControlV2 -- one template-matched control through the
# exact governed methodology stack genuine hypotheses and v1's calibration
# already use, optionally including walk-forward folds (section 12).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class NullControlRunResultV2:
    governance_policy_id: str
    exploratory_trial_id: str | None
    validation_trial_id: str | None
    holdout_trial_id: str | None
    walk_forward_trial_ids: tuple[str, ...]
    robustness_run_id: str | None
    conclusion_id: str
    promotion_evaluation_id: str
    knowledge_status: KnowledgeStatus
    blockers: tuple[str, ...]
    trials_recorded: int


def _direction_consistent_v2(base_mean: Decimal, candidate_mean: Decimal | None) -> bool | None:
    if candidate_mean is None or base_mean == 0:
        return None
    return (candidate_mean > 0) == (base_mean > 0)


class RunNullControlV2:
    def __init__(
        self, *, methodology_repository: ResearchMethodologyRepository, timeline: AgendaTimeline
    ) -> None:
        self._methodology_repository = methodology_repository
        self._timeline = timeline

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        spec: NullControlSpecV2,
        panel: CrossMarketPanel,
        run_walk_forward: bool,
        created_at: datetime,
    ) -> NullControlRunResultV2:
        evaluation = build_null_evaluation(
            spec=spec, panel=panel, train_dates=frozenset(self._timeline.train_dates)
        )
        governance_policy = DeclareResearchGovernancePolicy(
            repository=self._methodology_repository
        ).execute(
            DeclareGovernancePolicyCommand(
                hypothesis_version_id=spec.spec_id,
                dataset_snapshot_id=calibration.dataset_snapshot_id,
                replay_run_id=calibration.replay_run_id,
                intent=GovernanceIntent.PROMOTION_TRACK,
                period_policy=self._timeline.period_policy,
                revision=1,
                supersedes_governance_policy_id=None,
                declared_at=created_at,
            )
        )
        blockers: list[str] = []
        trials_recorded = 0
        train_frozenset = frozenset(self._timeline.train_dates)

        exploratory_trial = self._trial(
            calibration,
            spec,
            governance_policy.governance_policy_id,
            ResearchPhase.EXPLORATORY,
            evaluation.sample_universe(train_frozenset),
            evaluation.outcome_by_date,
            evaluation.market_scope,
            created_at,
        )
        if exploratory_trial is not None:
            trials_recorded += 1
        validation_trial = self._trial(
            calibration,
            spec,
            governance_policy.governance_policy_id,
            ResearchPhase.VALIDATION,
            evaluation.sample_universe(frozenset(self._timeline.validation_dates)),
            evaluation.outcome_by_date,
            evaluation.market_scope,
            created_at,
        )
        if validation_trial is None:
            blockers.append("VALIDATION_SAMPLE_INSUFFICIENT")
        else:
            trials_recorded += 1

        holdout_trial = None
        if validation_trial is not None:
            RecordHoldoutExposure(repository=self._methodology_repository).execute(
                RecordHoldoutExposureCommand(
                    governance_policy_id=governance_policy.governance_policy_id,
                    exposed_at=created_at,
                    actor="research_calibration_v2",
                    purpose="LOCKED_HOLDOUT_TRIAL_EVALUATION_FOR_RESEARCH_SIGNAL_CALIBRATION_V2",
                    consumed_trial_id=None,
                )
            )
            holdout_trial = self._trial(
                calibration,
                spec,
                governance_policy.governance_policy_id,
                ResearchPhase.LOCKED_HOLDOUT,
                evaluation.sample_universe(frozenset(self._timeline.holdout_dates)),
                evaluation.outcome_by_date,
                evaluation.market_scope,
                created_at,
            )
            if holdout_trial is None:
                blockers.append("HOLDOUT_SAMPLE_INSUFFICIENT")
            else:
                trials_recorded += 1

        walk_forward_ids: list[str] = []
        if run_walk_forward:
            for window in self._timeline.fold_windows:
                test_dates = frozenset(
                    d for d in panel.session_dates if window.test_start <= d <= window.test_end
                )
                fold_trial = self._trial(
                    calibration,
                    spec,
                    governance_policy.governance_policy_id,
                    ResearchPhase.WALK_FORWARD,
                    evaluation.sample_universe(test_dates),
                    evaluation.outcome_by_date,
                    evaluation.market_scope,
                    created_at,
                )
                if fold_trial is not None:
                    walk_forward_ids.append(fold_trial.trial_id)
                    trials_recorded += 1

        base_trial = holdout_trial or validation_trial
        robustness_run = None
        if base_trial is not None and base_trial.diagnostics.base.mean is not None:
            robustness_run = self._robustness(
                evaluation.sample_universe,
                evaluation.outcome_by_date,
                base_trial,
                panel.regime_by_date(),
                created_at,
            )
            self._methodology_repository.save_robustness_run(robustness_run)
        else:
            blockers.append("ROBUSTNESS_NOT_RUN_NO_REFERENCE_TRIAL")

        conclusion = BuildResearchConclusion(repository=self._methodology_repository).execute(
            BuildConclusionCommand(
                hypothesis_version_id=spec.spec_id,
                dataset_snapshot_id=calibration.dataset_snapshot_id,
                replay_run_id=calibration.replay_run_id,
                training_trial_id=exploratory_trial.trial_id if exploratory_trial else None,
                validation_trial_id=validation_trial.trial_id if validation_trial else None,
                holdout_trial_id=holdout_trial.trial_id if holdout_trial else None,
                walk_forward_run_id=None,
                robustness_run_id=robustness_run.robustness_run_id if robustness_run else None,
                trial_family_id=None,
                point_in_time_limitations=CALIBRATION_V2_LIMITATIONS,
                rationale=f"{spec.research_design.value} template-matched null: {spec.rationale}",
                created_at=created_at,
            )
        )
        promotion = EvaluatePromotion(repository=self._methodology_repository).execute(
            EvaluatePromotionCommand(
                conclusion_id=conclusion.conclusion_id,
                policy=PromotionPolicy(),
                lookahead_violation=False,
                created_at=created_at,
            )
        )
        robustness_passed = robustness_run.summary.passed if robustness_run is not None else False
        status = derive_knowledge_status(
            conclusion.conclusion_state,
            promotion_eligible=promotion.eligible,
            robustness_passed=robustness_passed,
        )
        return NullControlRunResultV2(
            governance_policy_id=governance_policy.governance_policy_id,
            exploratory_trial_id=exploratory_trial.trial_id if exploratory_trial else None,
            validation_trial_id=validation_trial.trial_id if validation_trial else None,
            holdout_trial_id=holdout_trial.trial_id if holdout_trial else None,
            walk_forward_trial_ids=tuple(walk_forward_ids),
            robustness_run_id=robustness_run.robustness_run_id if robustness_run else None,
            conclusion_id=conclusion.conclusion_id,
            promotion_evaluation_id=promotion.promotion_evaluation_id,
            knowledge_status=status,
            blockers=tuple(blockers),
            trials_recorded=trials_recorded,
        )

    def _trial(
        self,
        calibration: CalibrationPlan,
        spec: NullControlSpecV2,
        governance_policy_id: str,
        phase: ResearchPhase,
        sample_universe_pair: tuple[frozenset[str], frozenset[str]],
        outcome_by_date: dict[str, Decimal],
        market_scope: tuple[str, ...],
        created_at: datetime,
    ) -> ResearchTrial | None:
        sample_dates, universe_dates = sample_universe_pair
        sample_sorted = sorted(d for d in sample_dates if d in outcome_by_date)
        universe_sorted = sorted(d for d in universe_dates if d in outcome_by_date)
        if len(sample_sorted) < MINIMUM_TRIAL_SAMPLE or len(universe_sorted) < MINIMUM_TRIAL_SAMPLE:
            return None
        sample_values = tuple(outcome_by_date[d] for d in sample_sorted)
        universe_values = tuple(outcome_by_date[d] for d in universe_sorted)
        sample_ids = tuple(
            (
                d,
                deterministic_id(
                    "research_calibration_v2_observation", (spec.spec_id, phase.value, d)
                ),
            )
            for d in sample_sorted
        )
        grouped_pairs = tuple((phase.value, value) for value in sample_values)
        configuration = ResearchConfiguration.create(
            method_id=ResearchMethodId.TEMPLATE_MATCHED_CALIBRATION_NULL_CONTROL_V2,
            method_version="V2",
            parameter_values=(
                ("spec_id", spec.spec_id),
                ("research_design", spec.research_design.value),
                ("conditioning_shape", spec.conditioning_shape.value),
                ("matched_stratum", spec.matched_stratum),
                ("null_method", spec.null_method.value),
                ("source_feature_id", spec.source_feature_id),
                ("outcome_id", spec.outcome_id),
                ("horizon_sessions", str(spec.horizon_sessions)),
            ),
            origin=ConfigurationOrigin.FIXED_A_PRIORI,
            candidates_examined_count=1,
        )
        command = RecordGroupedResearchTrialCommand(
            hypothesis_version_id=spec.spec_id,
            dataset_snapshot_id=calibration.dataset_snapshot_id,
            replay_run_id=calibration.replay_run_id,
            phase=phase,
            market_scope=market_scope,
            configuration=configuration,
            sample_evaluation_ids=sample_ids,
            sample_values=sample_values,
            universe_values=universe_values,
            grouped_pairs=grouped_pairs,
            benchmark_kind=BenchmarkKind.UNCONDITIONAL_SUBSEQUENT_RETURN,
            created_at=created_at,
            governance_policy_id=governance_policy_id,
            outcome_horizon_sessions=spec.horizon_sessions,
        )
        try:
            return record_panel_sample_trial(
                repository=self._methodology_repository, command=command
            )
        except MethodologyConflict:
            raise
        except ResearchMethodologyApplicationError:
            return None

    def _robustness(
        self,
        sample_universe: Callable[[frozenset[str]], tuple[frozenset[str], frozenset[str]]],
        outcome_by_date: dict[str, Decimal],
        base_trial: ResearchTrial,
        regime_by_date: dict[str, CrossMarketRegimeSnapshot],
        created_at: datetime,
    ) -> RobustnessCheckRun:
        base_mean = base_trial.diagnostics.base.mean
        if base_mean is None:
            raise CalibrationV2ApplicationError("ROBUSTNESS_BASE_TRIAL_HAS_NO_MEAN")
        results: list[RobustnessCheckResult] = []

        def _mean_of(dates: frozenset[str]) -> tuple[int, Decimal | None]:
            values = [outcome_by_date[d] for d in dates if d in outcome_by_date]
            if len(values) < 5:
                return len(values), None
            return len(values), bound_statistical_decimal(
                sum(values, Decimal(0)) / Decimal(len(values))
            )

        for label, start, end, _stress in self._timeline.quarters:
            period_dates = frozenset(d for d in self._timeline.all_dates if start <= d <= end)
            sample, _universe = sample_universe(period_dates)
            count, mean = _mean_of(sample)
            results.append(
                RobustnessCheckResult(
                    RobustnessCheckType.SUBPERIOD_SPLIT,
                    label,
                    count,
                    mean,
                    _direction_consistent_v2(base_mean, mean),
                    "OK" if mean is not None else "INSUFFICIENT_DATA",
                )
            )

        combined = frozenset(self._timeline.validation_dates) | frozenset(
            self._timeline.holdout_dates
        )
        sample_all, _universe = sample_universe(combined)
        for label in ("RISK_ON", "RISK_OFF", "NEUTRAL"):
            subset = frozenset(
                d
                for d in sample_all
                if d in regime_by_date
                and regime_by_date[d].label_for(CrossMarketRegimeDimension.EQUITY_RISK_TONE)
                == label
            )
            count, mean = _mean_of(subset)
            results.append(
                RobustnessCheckResult(
                    RobustnessCheckType.REGIME_SPLIT,
                    label,
                    count,
                    mean,
                    _direction_consistent_v2(base_mean, mean),
                    "OK" if mean is not None else "INSUFFICIENT_DATA",
                )
            )

        all_frozenset = frozenset(self._timeline.all_dates)
        for label, start, end, _stress in self._timeline.quarters:
            excluded = frozenset(d for d in self._timeline.all_dates if start <= d <= end)
            sample, _universe = sample_universe(all_frozenset - excluded)
            count, mean = _mean_of(sample)
            results.append(
                RobustnessCheckResult(
                    RobustnessCheckType.LEAVE_ONE_PERIOD_OUT,
                    f"exclude_{label}",
                    count,
                    mean,
                    _direction_consistent_v2(base_mean, mean),
                    "OK" if mean is not None else "INSUFFICIENT_DATA",
                )
            )

        strided = frozenset(d for index, d in enumerate(sorted(sample_all)) if index % 2 == 0)
        count, mean = _mean_of(strided)
        results.append(
            RobustnessCheckResult(
                RobustnessCheckType.MISSING_DATA_SENSITIVITY,
                "stride_2_subsample",
                count,
                mean,
                _direction_consistent_v2(base_mean, mean),
                "OK" if mean is not None else "INSUFFICIENT_DATA",
            )
        )
        results.append(robustness_cost_stress())
        return RobustnessCheckRun.create(
            base_trial_id=base_trial.trial_id, results=tuple(results), created_at=created_at
        )


# ==========================================================================
# Section: v2-only repository port. Plan/run/report/breakdown/method-
# comparison/prior-recalibration/calibrated-promotion reuse v1's existing
# ``CalibrationRepository`` (imported by callers directly, never
# reimplemented here).
# ==========================================================================


class CalibrationV2Repository(Protocol):
    def spec_by_id(self, spec_id: str) -> NullControlSpecV2 | None: ...
    def save_spec(self, spec: NullControlSpecV2) -> NullControlSpecV2: ...
    def specs_for_calibration(self, calibration_id: str) -> tuple[NullControlSpecV2, ...]: ...
    def specs_for_stratum(self, matched_stratum: str) -> tuple[NullControlSpecV2, ...]: ...

    def outcome_by_spec(self, spec_id: str) -> NullControlOutcome | None: ...
    def save_outcome(self, outcome: NullControlOutcome) -> NullControlOutcome: ...
    def outcomes_for_run(self, calibration_run_id: str) -> tuple[NullControlOutcome, ...]: ...

    def save_classification(self, item: DesignClassification) -> DesignClassification: ...
    def classifications_for_calibration(
        self, calibration_id: str
    ) -> tuple[DesignClassification, ...]: ...
    def classification_for_subject(
        self, *, calibration_id: str, subject_id: str
    ) -> DesignClassification | None: ...

    def save_stability_slice(self, item: StabilitySlice) -> StabilitySlice: ...
    def stability_slices_for_calibration(
        self, calibration_id: str
    ) -> tuple[StabilitySlice, ...]: ...

    def save_walk_forward_summary(self, item: WalkForwardNullSummary) -> WalkForwardNullSummary: ...
    def walk_forward_summaries_for_calibration(
        self, calibration_id: str
    ) -> tuple[WalkForwardNullSummary, ...]: ...

    def save_follow_up_qualification(
        self, item: FollowUpQualification
    ) -> FollowUpQualification: ...
    def follow_up_qualifications_for_calibration(
        self, calibration_id: str
    ) -> tuple[FollowUpQualification, ...]: ...


# ==========================================================================
# Section 12: bounded, predeclared walk-forward-null subset-selection
# policy -- computed only from the specs' own declared fields (ordinal,
# matched_stratum, research_design), never from an observed outcome.
# ==========================================================================


def select_walk_forward_null_subset(
    *,
    specs: Sequence[NullControlSpecV2],
    priority_strata: frozenset[str],
    per_other_design: int = 2,
) -> tuple[str, ...]:
    """Every control whose matched stratum exactly equals one of the
    priority strata (the highest-value walk-forward question this
    milestone asks: do the null controls that most resemble the three
    JUSTIFIED follow-ups' own design look artificially stable across
    folds?) is included; every other research design additionally
    contributes its first ``per_other_design`` controls by ordinal, for
    bounded representative coverage (section 12: "do not necessarily run
    every null through the most expensive path")."""

    selected: list[str] = []
    seen_designs: dict[ResearchDesign, int] = {}
    for spec in sorted(specs, key=lambda item: item.ordinal):
        if spec.matched_stratum in priority_strata:
            selected.append(spec.spec_id)
            continue
        count = seen_designs.get(spec.research_design, 0)
        if count < per_other_design:
            selected.append(spec.spec_id)
            seen_designs[spec.research_design] = count + 1
    return tuple(selected)


# ==========================================================================
# Section 1/5: run registration/advancement over the v2 null battery.
# Reuses v1's ``CalibrationRun``/``research_calibration_run`` (a distinct
# ``calibration_id`` means no collision with v1's own cursor row).
# ==========================================================================


class RegisterCalibrationRunV2:
    def __init__(self, *, calibration_repository: CalibrationRepository) -> None:
        self._calibration_repository = calibration_repository

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        specs: Sequence[NullControlSpecV2],
        created_at: datetime,
    ) -> CalibrationRun:
        draft = CalibrationRun.draft(
            calibration=calibration,
            spec_ids=tuple(item.spec_id for item in specs),
            created_at=created_at,
        )
        existing = self._calibration_repository.run_by_calibration(calibration.calibration_id)
        if existing is not None:
            if existing.canonical_input_hash != draft.canonical_input_hash:
                raise CalibrationConflict("CALIBRATION_V2_RUN_IDENTITY_CONFLICT")
            return existing
        return self._calibration_repository.save_run(draft)


class AdvanceCalibrationRunV2:
    """Mirrors v1's ``AdvanceCalibrationRun``: process up to
    ``maximum_specs`` pending controls from the run's own cursor per call;
    once the cursor reaches the end, finalization happens in a separate,
    explicit step (``FinalizeCalibrationV2`` below) rather than
    automatically, since v2 finalization is materially more expensive
    (design classification, stability slices, and the walk-forward subset)
    than v1's."""

    def __init__(
        self,
        *,
        calibration_repository: CalibrationRepository,
        calibration_v2_repository: CalibrationV2Repository,
        methodology_repository: ResearchMethodologyRepository,
        timeline: AgendaTimeline,
        walk_forward_spec_ids: frozenset[str],
    ) -> None:
        self._calibration_repository = calibration_repository
        self._repository = calibration_v2_repository
        self._methodology_repository = methodology_repository
        self._runner = RunNullControlV2(
            methodology_repository=methodology_repository, timeline=timeline
        )
        self._walk_forward_spec_ids = walk_forward_spec_ids

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        panel: CrossMarketPanel,
        maximum_specs: int,
        created_at: datetime,
    ) -> CalibrationRun:
        if maximum_specs < 1:
            raise CalibrationV2ApplicationError("CALIBRATION_V2_RUN_MAXIMUM_SPECS_INVALID")
        run = self._calibration_repository.run_by_calibration(calibration.calibration_id)
        if run is None:
            raise CalibrationNotFound("CALIBRATION_V2_RUN_NOT_REGISTERED")
        if run.status is CalibrationRunStatus.COMPLETE:
            return run
        if run.status is CalibrationRunStatus.FAILED:
            raise CalibrationV2ApplicationError("CALIBRATION_V2_RUN_FAILED_CANNOT_ADVANCE")
        starting_cursor = run.cursor_ordinal
        pending = run.spec_ids[starting_cursor:]
        try:
            for offset, spec_id in enumerate(pending[:maximum_specs]):
                spec = self._repository.spec_by_id(spec_id)
                if spec is None:
                    raise CalibrationNotFound(f"NULL_CONTROL_V2_SPEC_NOT_FOUND:{spec_id}")
                existing_outcome = self._repository.outcome_by_spec(spec_id)
                if existing_outcome is None:
                    result = self._runner.execute(
                        calibration=calibration,
                        spec=spec,
                        panel=panel,
                        run_walk_forward=spec_id in self._walk_forward_spec_ids,
                        created_at=created_at,
                    )
                    outcome = NullControlOutcome.create(
                        spec_id=spec.spec_id,
                        calibration_run_id=run.calibration_run_id,
                        governance_policy_id=result.governance_policy_id,
                        exploratory_trial_id=result.exploratory_trial_id,
                        validation_trial_id=result.validation_trial_id,
                        holdout_trial_id=result.holdout_trial_id,
                        robustness_run_id=result.robustness_run_id,
                        conclusion_id=result.conclusion_id,
                        promotion_evaluation_id=result.promotion_evaluation_id,
                        knowledge_status=result.knowledge_status,
                        blockers=result.blockers,
                        created_at=created_at,
                    )
                    self._repository.save_outcome(outcome)
                    if result.walk_forward_trial_ids:
                        self._save_walk_forward_summary(calibration, spec, result, created_at)
                    trials_added = result.trials_recorded
                else:
                    trials_added = 0
                is_final = (starting_cursor + offset + 1) == len(run.spec_ids)
                run = run.advance(
                    spec_id=spec_id, trials_added=trials_added, is_final=is_final, at=created_at
                )
                run = self._calibration_repository.save_run(run)
        except (CalibrationV2ApplicationError, CalibrationNotFound) as error:
            run = self._calibration_repository.save_run(run.fail(reason=str(error), at=created_at))
            raise
        return run

    def _save_walk_forward_summary(
        self,
        calibration: CalibrationPlan,
        spec: NullControlSpecV2,
        result: NullControlRunResultV2,
        created_at: datetime,
    ) -> None:
        folds_beating_benchmark = 0
        for trial_id in result.walk_forward_trial_ids:
            trial = self._methodology_repository.trial_by_id(trial_id)
            if (
                trial is not None
                and trial.benchmark is not None
                and trial.benchmark.beats_benchmark
            ):
                folds_beating_benchmark += 1
        validation_survived = bool(
            result.validation_trial_id
            and (trial := self._methodology_repository.trial_by_id(result.validation_trial_id))
            and trial.benchmark is not None
            and trial.benchmark.beats_benchmark
        )
        holdout_survived = bool(
            result.holdout_trial_id
            and (trial := self._methodology_repository.trial_by_id(result.holdout_trial_id))
            and trial.benchmark is not None
            and trial.benchmark.beats_benchmark
        )
        robustness_survived = bool(
            result.robustness_run_id
            and (run := self._methodology_repository.robustness_run_by_id(result.robustness_run_id))
            and run.summary.passed
        )
        # No matched-stratum null percentile exists yet at this per-control
        # processing stage (the full battery is still being advanced), so
        # v1's own fail-closed rule in ``derive_evidence_grade`` -- "without
        # a null-calibration percentile the grade can never exceed
        # EXPLORATORY" -- is reused directly rather than reimplemented, via
        # a minimal profile whose only load-bearing field is
        # ``null_percentile=None``.
        conclusion = self._methodology_repository.conclusion_by_id(result.conclusion_id)
        conclusion_state = (
            conclusion.conclusion_state
            if conclusion is not None
            else ResearchConclusionState.INSUFFICIENT_EVIDENCE
        )
        placeholder_profile = EvidenceProfile.create(
            subject_id=spec.spec_id,
            subject_kind="NULL_CONTROL",
            benchmark_relative_effect=None,
            cohens_d=None,
            validation_beats_benchmark=validation_survived,
            holdout_result="BEATS_BENCHMARK" if holdout_survived else "NOT_EVALUATED",
            walk_forward_consistency=None,
            robustness_passed=robustness_survived,
            corrected_significant=None,
            effective_sample=Decimal(0),
            regime_concentration="NOT_YET_MATCHED_CALIBRATED",
            null_percentile=None,
            practical_significance=classify_practical_significance(
                statistically_significant=None, meets_effect_floor=False
            ),
            created_at=created_at,
        )
        grade = derive_evidence_grade(
            conclusion_state=conclusion_state,
            profile=placeholder_profile,
            effect_meets_floor=False,
        )
        summary = WalkForwardNullSummary.create(
            calibration_id=calibration.calibration_id,
            spec_id=spec.spec_id,
            fold_count=len(result.walk_forward_trial_ids),
            folds_beating_benchmark=folds_beating_benchmark,
            validation_survived=validation_survived,
            holdout_survived=holdout_survived,
            robustness_survived=robustness_survived,
            final_evidence_grade=grade,
            created_at=created_at,
        )
        self._repository.save_walk_forward_summary(summary)


# ==========================================================================
# Section 6/7/8: matched-stratum percentile and design classification for
# the v2 null population itself, plus sections 4/11/16/17/18 (BH-FDR/
# Bonferroni correction, family/design/horizon breakdowns, method
# comparison).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class _ControlRowV2:
    spec: NullControlSpecV2
    outcome: NullControlOutcome
    validation_trial: ResearchTrial | None
    holdout_trial: ResearchTrial | None
    robustness_run: RobustnessCheckRun | None
    promotion_eligible: bool
    cohens_d: Decimal | None


def _validation_p_value_v2(trial: ResearchTrial) -> Decimal | None:
    if (
        trial.benchmark is None
        or trial.diagnostics.base.mean is None
        or trial.diagnostics.base.stdev is None
    ):
        return None
    return two_sided_z_test_p_value(
        sample_mean=trial.diagnostics.base.mean,
        benchmark_mean=trial.benchmark.benchmark_mean,
        sample_stdev=trial.diagnostics.base.stdev,
        effective_sample_count=trial.diagnostics.overlap.effective_sample_count,
    )


def _summarize_rows_v2(
    rows: Sequence[_ControlRowV2], bh_by_trial: dict[str, CorrectedTestResult]
) -> CalibrationRateSummary:
    total = len(rows)
    validation_candidates = sum(1 for r in rows if r.validation_trial is not None)
    raw_positive = sum(
        1
        for r in rows
        if r.validation_trial is not None
        and r.validation_trial.benchmark is not None
        and r.validation_trial.benchmark.beats_benchmark
    )
    bh_survivors = sum(
        1
        for r in rows
        if r.validation_trial is not None
        and (correction := bh_by_trial.get(r.validation_trial.trial_id)) is not None
        and correction.rejected_null
    )
    holdout_survivors = sum(
        1
        for r in rows
        if r.holdout_trial is not None
        and r.holdout_trial.benchmark is not None
        and r.holdout_trial.benchmark.beats_benchmark
    )
    robustness_survivors = sum(
        1 for r in rows if r.robustness_run is not None and r.robustness_run.summary.passed
    )
    weak = sum(1 for r in rows if r.outcome.knowledge_status is KnowledgeStatus.WEAK)
    supported = sum(
        1 for r in rows if r.outcome.knowledge_status is KnowledgeStatus.SUPPORTED_FOR_FURTHER_STUDY
    )
    promotion_eligible = sum(1 for r in rows if r.promotion_eligible)
    return CalibrationRateSummary.compute(
        total_controls=total,
        validation_candidates=validation_candidates,
        raw_positive_findings=raw_positive,
        bh_fdr_survivors=bh_survivors,
        holdout_survivors=holdout_survivors,
        robustness_survivors=robustness_survivors,
        weak_count=weak,
        supported_count=supported,
        promotion_eligible_count=promotion_eligible,
    )


def _adequacy_note_v2(*, dimension: str, count: int) -> str:
    if count >= MINIMUM_ADEQUATE_CONTROL_COUNT:
        return f"Adequate control count for {dimension}-specific calibration."
    token = dimension.upper().replace("-", "_")
    return f"INSUFFICIENT_CONTROL_COUNT_FOR_RELIABLE_{token}_CALIBRATION"


@dataclass(frozen=True, slots=True)
class CalibrationV2FinalizationResult:
    report: CalibrationReport
    classifications: tuple[DesignClassification, ...]
    stability_slices: tuple[StabilitySlice, ...]
    method_comparisons: tuple[StatisticalMethodComparison, ...]


class FinalizeCalibrationV2:
    def __init__(
        self,
        *,
        calibration_repository: CalibrationRepository,
        calibration_v2_repository: CalibrationV2Repository,
        methodology_repository: ResearchMethodologyRepository,
        timeline: AgendaTimeline,
    ) -> None:
        self._calibration_repository = calibration_repository
        self._repository = calibration_v2_repository
        self._methodology_repository = methodology_repository
        self._timeline = timeline

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        run: CalibrationRun,
        panel: CrossMarketPanel,
        created_at: datetime,
    ) -> CalibrationV2FinalizationResult:
        existing_report = self._calibration_repository.report_by_calibration(
            calibration.calibration_id
        )
        specs = {
            item.spec_id: item
            for item in self._repository.specs_for_calibration(calibration.calibration_id)
        }
        outcomes = self._repository.outcomes_for_run(run.calibration_run_id)
        rows: list[_ControlRowV2] = []
        for outcome in outcomes:
            spec = specs.get(outcome.spec_id)
            if spec is None:
                continue
            validation_trial = (
                self._methodology_repository.trial_by_id(outcome.validation_trial_id)
                if outcome.validation_trial_id
                else None
            )
            holdout_trial = (
                self._methodology_repository.trial_by_id(outcome.holdout_trial_id)
                if outcome.holdout_trial_id
                else None
            )
            robustness_run = (
                self._methodology_repository.robustness_run_by_id(outcome.robustness_run_id)
                if outcome.robustness_run_id
                else None
            )
            promotion = (
                self._methodology_repository.promotion_evaluation_by_id(
                    outcome.promotion_evaluation_id
                )
                if outcome.promotion_evaluation_id
                else None
            )
            reference_trial = holdout_trial or validation_trial
            cohens_d = (
                reference_trial.diagnostics.effect_size.cohens_d
                if reference_trial is not None
                and reference_trial.diagnostics.effect_size is not None
                else None
            )
            rows.append(
                _ControlRowV2(
                    spec=spec,
                    outcome=outcome,
                    validation_trial=validation_trial,
                    holdout_trial=holdout_trial,
                    robustness_run=robustness_run,
                    promotion_eligible=bool(promotion is not None and promotion.eligible),
                    cohens_d=cohens_d,
                )
            )

        raw_results = tuple(
            TrialTestResult(
                row.validation_trial.trial_id,
                row.spec.spec_id,
                _validation_p_value_v2(row.validation_trial),
            )
            for row in rows
            if row.validation_trial is not None
        )
        bh_corrected = apply_benjamini_hochberg_correction(
            raw_results, false_discovery_rate=Decimal("0.10")
        )
        bonferroni_corrected = apply_bonferroni_correction(
            raw_results, family_wise_alpha=Decimal("0.05")
        )
        bh_by_trial = {item.trial_id: item for item in bh_corrected}
        bonferroni_by_trial = {item.trial_id: item for item in bonferroni_corrected}

        classifications = self._design_classifications(rows, bh_by_trial, created_at)
        stability_slices = self._stability_slices(rows, panel, created_at)
        method_comparisons = self._method_comparisons(
            calibration, rows, raw_results, bh_by_trial, bonferroni_by_trial, created_at
        )

        if existing_report is not None:
            return CalibrationV2FinalizationResult(
                report=existing_report,
                classifications=classifications,
                stability_slices=stability_slices,
                method_comparisons=method_comparisons,
            )

        overall_summary = _summarize_rows_v2(rows, bh_by_trial)
        family_breakdowns = [
            self._calibration_repository.save_breakdown(
                CalibrationBreakdown.create(
                    calibration_id=calibration.calibration_id,
                    dimension_kind="FAMILY",
                    dimension_label=family.value,
                    control_count=len(members := [r for r in rows if r.spec.family is family]),
                    summary=_summarize_rows_v2(members, bh_by_trial),
                    note=_adequacy_note_v2(dimension="family", count=len(members)),
                    created_at=created_at,
                )
            )
            for family in NullControlFamily
        ]
        design_breakdowns = [
            self._calibration_repository.save_breakdown(
                CalibrationBreakdown.create(
                    calibration_id=calibration.calibration_id,
                    dimension_kind="RESEARCH_DESIGN",
                    dimension_label=design.value,
                    control_count=len(
                        members := [r for r in rows if r.spec.research_design is design]
                    ),
                    summary=_summarize_rows_v2(members, bh_by_trial),
                    note=_adequacy_note_v2(dimension="research-design", count=len(members)),
                    created_at=created_at,
                )
            )
            for design in ResearchDesign
        ]
        horizon_breakdowns = [
            self._calibration_repository.save_breakdown(
                CalibrationBreakdown.create(
                    calibration_id=calibration.calibration_id,
                    dimension_kind="HORIZON",
                    dimension_label=f"{horizon}_SESSIONS",
                    control_count=len(
                        members := [r for r in rows if r.spec.horizon_sessions == horizon]
                    ),
                    summary=_summarize_rows_v2(members, bh_by_trial),
                    note=(
                        f"Overlap policy: outcome windows at horizon {horizon} overlap by "
                        "construction; effective sample uses the same conservative "
                        "horizon-stride adjustment every genuine-agenda trial uses."
                    ),
                    created_at=created_at,
                )
            )
            for horizon in calibration.allowed_horizons_sessions
        ]

        limitations: tuple[str, ...] = CALIBRATION_V2_LIMITATIONS
        any_breakdown_insufficient = any(
            not item.sample_adequate
            for item in (*family_breakdowns, *design_breakdowns, *horizon_breakdowns)
        )
        any_slice_insufficient = any(not item.sample_adequate for item in stability_slices)
        if any_breakdown_insufficient or any_slice_insufficient:
            limitations = (
                *limitations,
                "ONE_OR_MORE_REPORTING_SLICES_BELOW_MINIMUM_ADEQUATE_CONTROL_COUNT",
            )

        report = CalibrationReport.create(
            calibration_id=calibration.calibration_id,
            calibration_run_id=run.calibration_run_id,
            overall_summary=overall_summary,
            family_breakdown_ids=tuple(
                item.breakdown_id for item in family_breakdowns + design_breakdowns
            ),
            horizon_breakdown_ids=tuple(item.breakdown_id for item in horizon_breakdowns),
            time_period_breakdown_ids=tuple(
                item.slice_id for item in stability_slices if item.dimension_kind == "TIME_PERIOD"
            ),
            volatility_regime_breakdown_ids=tuple(
                item.slice_id
                for item in stability_slices
                if item.dimension_kind == "VOLATILITY_REGIME"
            ),
            method_comparison_ids=tuple(item.comparison_id for item in method_comparisons),
            effect_size_floor_policy_id=EFFECT_SIZE_FLOOR_POLICY_V1.policy_id,
            evidence_grade_policy_id=EVIDENCE_GRADE_POLICY_V1.policy_id,
            recommended_evidence_policy_notes=_recommendation_notes_v2(
                overall_summary, classifications
            ),
            prior_agenda_hypotheses_recalibrated=0,
            justified_follow_up_count=0,
            limitations=limitations,
            created_at=created_at,
        )
        saved_report = self._calibration_repository.save_report(report)
        return CalibrationV2FinalizationResult(
            report=saved_report,
            classifications=classifications,
            stability_slices=stability_slices,
            method_comparisons=method_comparisons,
        )

    def _design_classifications(
        self,
        rows: Sequence[_ControlRowV2],
        bh_by_trial: dict[str, CorrectedTestResult],
        created_at: datetime,
    ) -> tuple[DesignClassification, ...]:
        by_stratum: dict[str, list[Decimal]] = {}
        for row in rows:
            if row.cohens_d is not None:
                by_stratum.setdefault(row.spec.matched_stratum, []).append(row.cohens_d)
        pooled = [row.cohens_d for row in rows if row.cohens_d is not None]
        results: list[DesignClassification] = []
        for row in rows:
            matched_pool = by_stratum.get(row.spec.matched_stratum, [])
            matched = MatchedCalibrationResult.evaluate(
                statistic=row.cohens_d,
                matched_null_statistics=matched_pool,
                pooled_null_statistics=pooled,
                matched_stratum=row.spec.matched_stratum,
            )
            raw_significant = (
                row.validation_trial.benchmark.beats_benchmark
                if row.validation_trial is not None and row.validation_trial.benchmark is not None
                else None
            )
            corrected_significant = (
                bh_by_trial[row.validation_trial.trial_id].rejected_null
                if row.validation_trial is not None and row.validation_trial.trial_id in bh_by_trial
                else None
            )
            conclusion = (
                self._methodology_repository.conclusion_by_id(row.outcome.conclusion_id)
                if row.outcome.conclusion_id is not None
                else None
            )
            conclusion_state = (
                conclusion.conclusion_state
                if conclusion is not None
                else ResearchConclusionState.INSUFFICIENT_EVIDENCE
            )
            outcome_family = row.spec.outcome_family
            meets_floor = effect_meets_floor(
                outcome_family=outcome_family,
                cohens_d=row.cohens_d,
                policy=EFFECT_SIZE_FLOOR_POLICY_V1,
            )
            profile = EvidenceProfile.create(
                subject_id=row.spec.spec_id,
                subject_kind="NULL_CONTROL",
                benchmark_relative_effect=(
                    row.validation_trial.benchmark.relative_result
                    if row.validation_trial is not None
                    and row.validation_trial.benchmark is not None
                    else None
                ),
                cohens_d=row.cohens_d,
                validation_beats_benchmark=raw_significant,
                holdout_result=(
                    "BEATS_BENCHMARK"
                    if row.holdout_trial is not None
                    and row.holdout_trial.benchmark is not None
                    and row.holdout_trial.benchmark.beats_benchmark
                    else "NOT_EVALUATED"
                ),
                walk_forward_consistency=None,
                robustness_passed=(
                    row.robustness_run.summary.passed if row.robustness_run is not None else None
                ),
                corrected_significant=corrected_significant,
                effective_sample=(
                    row.validation_trial.diagnostics.overlap.effective_sample_count
                    if row.validation_trial is not None
                    else Decimal(0)
                ),
                regime_concentration="NOT_APPLICABLE_NULL_CONTROL",
                null_percentile=matched.matched_percentile,
                practical_significance=classify_practical_significance(
                    statistically_significant=corrected_significant, meets_effect_floor=meets_floor
                ),
                created_at=created_at,
            )
            grade = derive_evidence_grade(
                conclusion_state=conclusion_state, profile=profile, effect_meets_floor=meets_floor
            )
            classification = DesignClassification.create(
                calibration_id=row.spec.calibration_id,
                subject_id=row.spec.spec_id,
                subject_kind="NULL_CONTROL",
                research_design=row.spec.research_design,
                conditioning_shape=row.spec.conditioning_shape,
                matched=matched,
                raw_significant=raw_significant,
                corrected_significant=corrected_significant,
                cohens_d=row.cohens_d,
                evidence_grade_v2=grade,
                created_at=created_at,
            )
            self._repository.save_classification(classification)
            results.append(classification)
        return tuple(results)

    def _stability_slices(
        self, rows: Sequence[_ControlRowV2], panel: CrossMarketPanel, created_at: datetime
    ) -> tuple[StabilitySlice, ...]:
        dates = panel.session_dates
        train_dates = frozenset(self._timeline.train_dates)
        third = max(1, len(dates) // 3)
        time_buckets = {
            "EARLY": frozenset(dates[:third]),
            "MIDDLE": frozenset(dates[third : 2 * third]),
            "LATE": frozenset(dates[2 * third :]),
        }
        regime_by_date = panel.regime_by_date()
        vol_buckets: dict[str, frozenset[str]] = {}
        for date in dates:
            regime = regime_by_date.get(date)
            if regime is None:
                continue
            try:
                label = regime.label_for(CrossMarketRegimeDimension.EQUITY_VOLATILITY)
            except Exception:
                label = "UNKNOWN"
            vol_buckets[label] = vol_buckets.get(label, frozenset()) | frozenset({date})

        slices: list[StabilitySlice] = []
        for dimension_kind, buckets in (
            ("TIME_PERIOD", time_buckets),
            ("VOLATILITY_REGIME", vol_buckets),
        ):
            for label, partition_dates in sorted(buckets.items()):
                raw_positive = 0
                p_values: list[tuple[str, Decimal | None]] = []
                evaluated = 0
                for row in rows:
                    evaluation = build_null_evaluation(
                        spec=row.spec, panel=panel, train_dates=train_dates
                    )
                    sample_dates, universe_dates = evaluation.sample_universe(partition_dates)
                    sample_values = tuple(
                        evaluation.outcome_by_date[d]
                        for d in sample_dates
                        if d in evaluation.outcome_by_date
                    )
                    universe_values = tuple(
                        evaluation.outcome_by_date[d]
                        for d in universe_dates
                        if d in evaluation.outcome_by_date
                    )
                    p_value = _quick_p_value(sample_values, universe_values)
                    if p_value is not None:
                        evaluated += 1
                        p_values.append((row.spec.spec_id, p_value))
                        if p_value <= Decimal("0.05"):
                            raw_positive += 1
                corrected_positive = _bh_positive_count(p_values)
                slices.append(
                    self._repository.save_stability_slice(
                        StabilitySlice.create(
                            calibration_id=rows[0].spec.calibration_id if rows else "",
                            dimension_kind=dimension_kind,
                            dimension_label=label,
                            control_count=evaluated,
                            raw_positive_count=raw_positive,
                            corrected_positive_count=corrected_positive,
                            minimum_adequate_count=MINIMUM_ADEQUATE_CONTROL_COUNT,
                            note=_adequacy_note_v2(
                                dimension=f"{dimension_kind.lower()}-{label.lower()}",
                                count=evaluated,
                            ),
                            created_at=created_at,
                        )
                    )
                )
        return tuple(slices)

    def _method_comparisons(
        self,
        calibration: CalibrationPlan,
        rows: Sequence[_ControlRowV2],
        raw_results: Sequence[TrialTestResult],
        bh_by_trial: dict[str, CorrectedTestResult],
        bonferroni_by_trial: dict[str, CorrectedTestResult],
        created_at: datetime,
    ) -> tuple[StatisticalMethodComparison, ...]:
        control_count = sum(1 for r in rows if r.validation_trial is not None)
        normal_survivors = sum(
            1
            for item in raw_results
            if item.p_value is not None and item.p_value <= Decimal("0.05")
        )
        bh_survivors = sum(1 for item in bh_by_trial.values() if item.rejected_null)
        bonferroni_survivors = sum(1 for item in bonferroni_by_trial.values() if item.rejected_null)
        raw = [
            (
                "NORMAL_APPROXIMATION_UNCORRECTED_V2",
                normal_survivors,
                "Two-sided z-test, IID standard error, alpha=0.05, no multiple-testing correction, "
                "computed inside the template-matched v2 battery.",
            ),
            (
                "BENJAMINI_HOCHBERG_FDR_V2",
                bh_survivors,
                "Benjamini-Hochberg step-up procedure, false discovery rate=0.10, applied across "
                "the whole v2 battery.",
            ),
            (
                "BONFERRONI_V2",
                bonferroni_survivors,
                "Bonferroni family-wise correction, alpha=0.05, applied across the whole v2 "
                "battery -- the most conservative method compared here.",
            ),
        ]
        ranked = sorted(raw, key=lambda item: item[1])
        comparisons = []
        for rank, (method_id, survivors, notes) in enumerate(ranked, start=1):
            comparisons.append(
                self._calibration_repository.save_method_comparison(
                    StatisticalMethodComparison.create(
                        calibration_id=calibration.calibration_id,
                        method_id=method_id,
                        survivors_on_nulls=survivors,
                        control_count=control_count,
                        conservatism_rank=rank,
                        notes=notes,
                        created_at=created_at,
                    )
                )
            )
        return tuple(comparisons)


def _quick_p_value(sample: Sequence[Decimal], benchmark: Sequence[Decimal]) -> Decimal | None:
    """A cheap, honest, direct two-sample significance check reused only
    for time-period/volatility-regime stability slices (sections 10/11) --
    deliberately not the full governed trial pipeline (see
    :class:`StabilitySlice`'s docstring for why)."""

    if len(sample) < 5 or len(benchmark) < 5:
        return None
    with localcontext() as context:
        context.prec = 28
        sample_mean = sum(sample, Decimal(0)) / Decimal(len(sample))
        benchmark_mean = sum(benchmark, Decimal(0)) / Decimal(len(benchmark))
        variance = sum(((v - sample_mean) ** 2 for v in sample), Decimal(0)) / Decimal(
            max(1, len(sample) - 1)
        )
        stdev = variance.sqrt() if variance > 0 else Decimal(0)
    return two_sided_z_test_p_value(
        sample_mean=sample_mean,
        benchmark_mean=benchmark_mean,
        sample_stdev=stdev,
        effective_sample_count=Decimal(len(sample)),
    )


def _bh_positive_count(p_values: Sequence[tuple[str, Decimal | None]]) -> int:
    if not p_values:
        return 0
    results = tuple(
        TrialTestResult(subject_id, subject_id, p_value) for subject_id, p_value in p_values
    )
    corrected = apply_benjamini_hochberg_correction(results, false_discovery_rate=Decimal("0.10"))
    return sum(1 for item in corrected if item.rejected_null)


def _recommendation_notes_v2(
    summary: CalibrationRateSummary, classifications: Sequence[DesignClassification]
) -> str:
    insufficient = sum(1 for item in classifications if not item.matched.sufficient)
    return (
        f"Empirical corrected (BH-FDR) false-positive rate across {summary.validation_candidates} "
        f"matched-population validation candidates: {summary.corrected_false_positive_rate} "
        f"({summary.bh_fdr_survivors}/{summary.validation_candidates}). False-WEAK rate across all "
        f"{summary.total_controls} v2 controls: {summary.false_weak_rate}. "
        f"{insufficient}/{len(classifications)} v2 controls fall in a matched stratum below "
        f"{MINIMUM_MATCHED_NULL_COUNT} controls and are therefore reported as "
        f"{CALIBRATION_STRATUM_INSUFFICIENT} rather than assigned a matched percentile. "
        "EVIDENCE_GRADE_POLICY_V1's thresholds (0.90/0.95/0.99) are retained unchanged for v2 -- "
        "what changes is the null population fed into null_percentile (the matched stratum, "
        "fail-closed to EXPLORATORY when insufficient) rather than v1's single pooled population. "
        "This is a finite-sample empirical estimate, not proof of a true zero false-discovery "
        "rate; see the calibration-confidence warning on every report surface."
    )


def conditioning_shape_for_hypothesis(
    hypothesis: GeneratedHypothesis, design: ResearchDesign
) -> ConditioningShape:
    """The matching half of :func:`research_design_for_hypothesis` --
    derives the section 5 matched-stratum conditioning-shape dimension
    from a genuine hypothesis's own declared secondary-condition fields,
    reused both by hypothesis reassessment and by CLI/API orchestration
    that needs to compute a hypothesis's matched stratum key directly
    (e.g. to select the walk-forward-null priority strata)."""

    if design is ResearchDesign.PERSISTENCE:
        return ConditioningShape.STATE_TRANSITION
    if hypothesis.secondary_feature_id is None:
        return ConditioningShape.UNCONDITIONAL
    if design is ResearchDesign.INTERACTION:
        return ConditioningShape.NUMERIC_INTERACTION
    if (
        hypothesis.secondary_condition_rule is not None
        and hypothesis.secondary_condition_rule.value == "LABEL_EQUALS"
    ):
        return ConditioningShape.LABEL_SPLIT
    return ConditioningShape.NUMERIC_SPLIT


def collect_v1_matched_pool(
    *,
    v1_specs: Sequence[NullControlSpec],
    v1_outcomes: Sequence[NullControlOutcome],
    methodology_repository: ResearchMethodologyRepository,
    outcome_family: OutcomeFamily,
    horizon_sessions: int,
) -> tuple[Decimal, ...]:
    """Section 3: "Predictive/regime nulls: continue supporting the
    existing v1 null designs." Every one of v1's 104 controls is
    mechanically PREDICTIVE-shaped by construction (a single train-median
    condition split, never a secondary condition) -- there is no need to
    regenerate what v1 already calibrated well. Filters v1's retained
    controls to the same outcome family and horizon as the target
    hypothesis (the closest matching v1's own records support, since a v1
    ``NullControlSpec`` carries no v2 matched-stratum key) and returns
    their already-computed, already-retained reference-trial cohens_d
    values. Read-only: no v1 evidence is touched, retained, or rewritten."""

    by_spec = {spec.spec_id: spec for spec in v1_specs}
    values: list[Decimal] = []
    for outcome in v1_outcomes:
        spec = by_spec.get(outcome.spec_id)
        if (
            spec is None
            or spec.outcome_family is not outcome_family
            or spec.horizon_sessions != horizon_sessions
        ):
            continue
        trial_id = outcome.holdout_trial_id or outcome.validation_trial_id
        if trial_id is None:
            continue
        trial = methodology_repository.trial_by_id(trial_id)
        if (
            trial is not None
            and trial.diagnostics.effect_size is not None
            and trial.diagnostics.effect_size.cohens_d is not None
        ):
            values.append(trial.diagnostics.effect_size.cohens_d)
    return tuple(values)


def build_matched_pools(
    classifications: Sequence[DesignClassification],
) -> tuple[dict[str, tuple[Decimal, ...]], tuple[Decimal, ...]]:
    """Rebuilds the matched-stratum and pooled cohens_d populations from
    already-retained v2 :class:`DesignClassification` records -- reused by
    both the internal null-vs-null classification pass and the genuine-
    hypothesis reassessment pass below, so both always compare against the
    identical, already-computed v2 null population."""

    by_stratum: dict[str, list[Decimal]] = {}
    pooled: list[Decimal] = []
    for item in classifications:
        if item.cohens_d is None or item.subject_kind != "NULL_CONTROL":
            continue
        by_stratum.setdefault(item.matched.matched_stratum, []).append(item.cohens_d)
        pooled.append(item.cohens_d)
    return {key: tuple(value) for key, value in by_stratum.items()}, tuple(pooled)


# ==========================================================================
# Section 18/19: non-destructive reassessment of the genuine agenda's
# already-retained findings (breadth->return, dispersion->volatility, and
# the sector-dispersion->BTC-volatility negative control) plus the parent
# hypotheses of the three currently JUSTIFIED follow-ups, against v2's
# matched null population. Mirrors v1's ``RecalibratePriorAgenda`` closely
# but restricted to a caller-supplied set of hypothesis ids and scored
# against the matched (not pooled-only) population.
# ==========================================================================


def _outcome_family_for_v2(outcome_id: OutcomeId) -> OutcomeFamily | None:
    from apex.application.research_calibration import _OUTCOME_FAMILY_BY_OUTCOME_ID

    return _OUTCOME_FAMILY_BY_OUTCOME_ID.get(outcome_id.value)


class ReassessGenuineFindingsV2:
    def __init__(
        self,
        *,
        cross_market_repository: CrossMarketResearchRepository,
        methodology_repository: ResearchMethodologyRepository,
        minimum_effective_sample: Decimal = Decimal("5"),
    ) -> None:
        self._cross_market_repository = cross_market_repository
        self._methodology_repository = methodology_repository
        self._minimum_effective_sample = minimum_effective_sample

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        hypotheses: Sequence[GeneratedHypothesis],
        matched_pools: dict[str, tuple[Decimal, ...]],
        pooled: tuple[Decimal, ...],
        created_at: datetime,
    ) -> tuple[tuple[DesignClassification, PriorHypothesisRecalibration], ...]:
        results: list[tuple[DesignClassification, PriorHypothesisRecalibration]] = []
        for hypothesis in hypotheses:
            outcome_family_check = _outcome_family_for_v2(hypothesis.outcome_id)
            if outcome_family_check is None:
                # PERSISTENCE_V1's sentinel PERSISTENCE_INDICATOR outcome has
                # no mapped OutcomeFamily (mirrors v1's own posture: never a
                # crash, just an honestly unresolvable matched stratum for
                # this hypothesis). None of the three JUSTIFIED follow-ups'
                # parents or the genuine WEAK findings use this template.
                continue
            entry = self._latest_entry(hypothesis)
            if entry is None:
                continue
            conclusion = self._methodology_repository.conclusion_by_id(entry.conclusion_id)
            if conclusion is None:
                continue
            validation_trial = self._trial(conclusion.validation_trial_id)
            holdout_trial = self._trial(conclusion.holdout_trial_id)
            robustness_run = (
                self._methodology_repository.robustness_run_by_id(conclusion.robustness_run_id)
                if conclusion.robustness_run_id
                else None
            )
            reference_trial = holdout_trial or validation_trial
            cohens_d = (
                reference_trial.diagnostics.effect_size.cohens_d
                if reference_trial is not None
                and reference_trial.diagnostics.effect_size is not None
                else None
            )
            benchmark_relative = (
                reference_trial.benchmark.relative_result
                if reference_trial is not None and reference_trial.benchmark is not None
                else None
            )
            effective_sample = (
                reference_trial.diagnostics.overlap.effective_sample_count
                if reference_trial is not None
                else Decimal(0)
            )
            corrected_significant = self._corrected_significant(conclusion)

            design = research_design_for_hypothesis(
                template_id=hypothesis.template_id, market_scope=hypothesis.market_scope
            )
            shape = self._conditioning_shape(hypothesis, design)
            stratum = matched_stratum_key(
                research_design=design,
                outcome_family=self._outcome_family(hypothesis),
                horizon_sessions=hypothesis.horizon_sessions,
                market_scope=hypothesis.market_scope,
                conditioning_shape=shape,
            )
            matched = MatchedCalibrationResult.evaluate(
                statistic=cohens_d,
                matched_null_statistics=matched_pools.get(stratum, ()),
                pooled_null_statistics=pooled,
                matched_stratum=stratum,
            )
            outcome_family = _outcome_family_for_v2(hypothesis.outcome_id)
            meets_floor = outcome_family is not None and effect_meets_floor(
                outcome_family=outcome_family, cohens_d=cohens_d, policy=EFFECT_SIZE_FLOOR_POLICY_V1
            )
            practical = classify_practical_significance(
                statistically_significant=corrected_significant, meets_effect_floor=meets_floor
            )
            holdout_result = (
                "NOT_EVALUATED"
                if holdout_trial is None or holdout_trial.benchmark is None
                else (
                    "BEATS_BENCHMARK"
                    if holdout_trial.benchmark.beats_benchmark
                    else "DOES_NOT_BEAT_BENCHMARK"
                )
            )
            robustness_passed = (
                robustness_run.summary.passed if robustness_run is not None else None
            )
            profile = EvidenceProfile.create(
                subject_id=hypothesis.hypothesis_id,
                subject_kind="HYPOTHESIS",
                benchmark_relative_effect=benchmark_relative,
                cohens_d=cohens_d,
                validation_beats_benchmark=(
                    validation_trial.benchmark.beats_benchmark
                    if validation_trial is not None and validation_trial.benchmark is not None
                    else None
                ),
                holdout_result=holdout_result,
                walk_forward_consistency=None,
                robustness_passed=robustness_passed,
                corrected_significant=corrected_significant,
                effective_sample=effective_sample,
                regime_concentration="SEE_V1_RECALIBRATION_FOR_REGIME_CONCENTRATION",
                null_percentile=matched.matched_percentile,
                practical_significance=practical,
                created_at=created_at,
            )
            grade = derive_evidence_grade(
                conclusion_state=conclusion.conclusion_state,
                profile=profile,
                effect_meets_floor=meets_floor,
                policy=EVIDENCE_GRADE_POLICY_V1,
            )
            priority = (
                FollowUpQualificationV2.CALIBRATION_INSUFFICIENT
                if not matched.sufficient
                else classify_follow_up_v2(matched=matched, evidence_grade=grade)
            )
            verdict = (
                assess_negative_control_quality(
                    null_percentile=matched.matched_percentile,
                    robustness_passed=robustness_passed,
                    effective_sample=effective_sample,
                    minimum_effective_sample=self._minimum_effective_sample,
                    policy=EVIDENCE_GRADE_POLICY_V1,
                )
                if hypothesis.is_negative_control
                else None
            )
            classification = DesignClassification.create(
                calibration_id=calibration.calibration_id,
                subject_id=hypothesis.hypothesis_id,
                subject_kind="HYPOTHESIS",
                research_design=design,
                conditioning_shape=shape,
                matched=matched,
                raw_significant=(
                    validation_trial.benchmark.beats_benchmark
                    if validation_trial is not None and validation_trial.benchmark is not None
                    else None
                ),
                corrected_significant=corrected_significant,
                cohens_d=cohens_d,
                evidence_grade_v2=grade,
                created_at=created_at,
            )
            sufficiency = "sufficient" if matched.sufficient else CALIBRATION_STRATUM_INSUFFICIENT
            rationale = (
                f"v2 matched stratum {stratum} "
                f"({matched.matched_null_count} matched null controls, {sufficiency}); "
                f"matched percentile {matched.matched_percentile}; pooled percentile "
                f"{matched.pooled_percentile} across {matched.pooled_null_count} v2 controls; "
                f"original conclusion {conclusion.conclusion_state.value} "
                f"(retained knowledge status {entry.status.value}, unchanged); "
                f"v2 calibrated evidence grade {grade.value}."
            )
            recalibration = PriorHypothesisRecalibration.create(
                calibration_id=calibration.calibration_id,
                hypothesis_id=hypothesis.hypothesis_id,
                original_conclusion_id=conclusion.conclusion_id,
                original_conclusion_state=conclusion.conclusion_state.value,
                original_knowledge_status=entry.status.value,
                calibrated_evidence_grade=grade,
                null_percentile=matched.matched_percentile,
                still_promotion_ineligible=not evidence_grade_at_least(
                    grade, CALIBRATED_PROMOTION_POLICY.minimum_evidence_grade
                ),
                deserves_follow_up=priority in (FollowUpQualificationV2.CALIBRATION_QUALIFIED,),
                follow_up_priority=self._as_v1_priority(priority),
                negative_control_verdict=verdict,
                rationale=rationale,
                created_at=created_at,
            )
            results.append((classification, recalibration))
        return tuple(results)

    @staticmethod
    def _as_v1_priority(priority: FollowUpQualificationV2) -> FollowUpPriorityClass:
        unsupported = FollowUpPriorityClass.UNSUPPORTED_AFTER_CALIBRATION
        return {
            FollowUpQualificationV2.CALIBRATION_QUALIFIED: FollowUpPriorityClass.JUSTIFIED,
            FollowUpQualificationV2.CALIBRATION_INSUFFICIENT: FollowUpPriorityClass.LOW_PRIORITY,
            FollowUpQualificationV2.NO_LONGER_JUSTIFIED: unsupported,
            FollowUpQualificationV2.REJECTED: FollowUpPriorityClass.REJECTED,
        }[priority]

    @staticmethod
    def _outcome_family(hypothesis: GeneratedHypothesis) -> OutcomeFamily:
        family = _outcome_family_for_v2(hypothesis.outcome_id)
        if family is None:
            raise CalibrationV2ApplicationError("UNKNOWN_OUTCOME_FAMILY_FOR_HYPOTHESIS_OUTCOME")
        return family

    @staticmethod
    def _conditioning_shape(
        hypothesis: GeneratedHypothesis, design: ResearchDesign
    ) -> ConditioningShape:
        return conditioning_shape_for_hypothesis(hypothesis, design)

    def _latest_entry(self, hypothesis: GeneratedHypothesis) -> ResearchKnowledgeEntry | None:
        entries = self._cross_market_repository.knowledge_entries_for_hypothesis(
            hypothesis.hypothesis_id
        )
        return max(entries, key=lambda item: item.revision, default=None)

    def _trial(self, trial_id: str | None) -> ResearchTrial | None:
        return self._methodology_repository.trial_by_id(trial_id) if trial_id else None

    def _corrected_significant(self, conclusion: ResearchConclusion) -> bool | None:
        trial_family_id = conclusion.trial_family_id
        if trial_family_id is None:
            return None
        family = self._methodology_repository.trial_family_by_id(trial_family_id)
        if family is None:
            return None
        return any(
            item.rejected_null
            and item.trial_id in (conclusion.validation_trial_id, conclusion.holdout_trial_id)
            for item in family.corrected_results
        )


# ==========================================================================
# Section 17: follow-up requalification (new vocabulary; never modifies
# the historical ``FollowUpProposal`` row or v1's own
# ``FollowUpPriorityAssessment`` row).
# ==========================================================================


class RequalifyFollowUpsV2:
    def __init__(self, *, cross_market_repository: CrossMarketResearchRepository) -> None:
        self._cross_market_repository = cross_market_repository

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        follow_up_ids: Sequence[str],
        hypotheses_by_id: dict[str, GeneratedHypothesis],
        hypothesis_classifications: dict[str, DesignClassification],
        created_at: datetime,
    ) -> tuple[FollowUpQualification, ...]:
        follow_ups = {
            item.follow_up_id: item for item in self._cross_market_repository.list_follow_ups()
        }
        results: list[FollowUpQualification] = []
        for follow_up_id in follow_up_ids:
            follow_up = follow_ups.get(follow_up_id)
            if follow_up is None:
                continue
            hypothesis = hypotheses_by_id.get(follow_up.parent_hypothesis_id)
            classification = hypothesis_classifications.get(follow_up.parent_hypothesis_id)
            if hypothesis is None or classification is None:
                matched = MatchedCalibrationResult.evaluate(
                    statistic=None,
                    matched_null_statistics=(),
                    pooled_null_statistics=(),
                    matched_stratum="UNKNOWN_PARENT_HYPOTHESIS",
                )
                qualification = FollowUpQualificationV2.CALIBRATION_INSUFFICIENT
                grade = EvidenceGrade.EXPLORATORY
                design = ResearchDesign.PREDICTIVE
                horizon = 1
                rationale = (
                    f"Parent hypothesis {follow_up.parent_hypothesis_id} was not reassessed under "
                    "v2 calibration; cannot requalify."
                )
            else:
                matched = classification.matched
                grade = classification.evidence_grade_v2
                design = classification.research_design
                horizon = hypothesis.horizon_sessions
                qualification = classify_follow_up_v2(matched=matched, evidence_grade=grade)
                sufficiency = (
                    "sufficient" if matched.sufficient else CALIBRATION_STRATUM_INSUFFICIENT
                )
                rationale = (
                    f"Parent hypothesis {follow_up.parent_hypothesis_id} v2 matched stratum "
                    f"{matched.matched_stratum} "
                    f"({matched.matched_null_count} matched null controls, {sufficiency}); "
                    f"matched percentile {matched.matched_percentile}; "
                    f"v2 calibrated evidence grade {grade.value}; "
                    f"proposed follow-up kind {follow_up.kind.value}."
                )
            results.append(
                FollowUpQualification.create(
                    calibration_id=calibration.calibration_id,
                    follow_up_id=follow_up_id,
                    parent_hypothesis_id=follow_up.parent_hypothesis_id,
                    research_design=design,
                    horizon_sessions=horizon,
                    matched=matched,
                    evidence_grade_v2=grade,
                    qualification=qualification,
                    rationale=rationale,
                    created_at=created_at,
                )
            )
        return tuple(results)


# ==========================================================================
# Section 16: calibrated promotion policy v2 -- strictly additive on top
# of v1's unmodified ``CalibratedPromotionEvaluation``/
# ``CALIBRATED_PROMOTION_POLICY``. Never loosens v1.
# ==========================================================================


@dataclass(frozen=True, slots=True)
class PromotionGateV2Result:
    promotion_evaluation_id: str
    calibration_id: str
    base_eligible: bool
    calibrated_eligible: bool
    matched_sufficient: bool
    eligible: bool
    reasons: tuple[str, ...]


def evaluate_v2_promotion_gate(
    *,
    promotion_evaluation_id: str,
    calibration_id: str,
    base_eligible: bool,
    base_reasons: tuple[str, ...],
    evidence_grade: EvidenceGrade,
    matched: MatchedCalibrationResult,
    created_at: datetime,
) -> PromotionGateV2Result:
    calibrated = CalibratedPromotionEvaluation.create(
        promotion_evaluation_id=promotion_evaluation_id,
        calibration_id=calibration_id,
        base_eligible=base_eligible,
        base_reasons=base_reasons,
        evidence_grade=evidence_grade,
        policy=CALIBRATED_PROMOTION_POLICY,
        created_at=created_at,
    )
    reasons = list(calibrated.reasons)
    if not matched.sufficient:
        reasons.append(CALIBRATION_STRATUM_INSUFFICIENT)
    eligible = calibrated.eligible and matched.sufficient
    return PromotionGateV2Result(
        promotion_evaluation_id=promotion_evaluation_id,
        calibration_id=calibration_id,
        base_eligible=base_eligible,
        calibrated_eligible=calibrated.eligible,
        matched_sufficient=matched.sufficient,
        eligible=eligible,
        reasons=tuple(sorted(set(reasons))),
    )


# ==========================================================================
# In-memory v2-only repository (fast application-layer tests)
# ==========================================================================


class InMemoryCalibrationV2Repository:
    def __init__(self) -> None:
        self._specs: dict[str, NullControlSpecV2] = {}
        self._outcomes: dict[str, NullControlOutcome] = {}
        self._classifications: dict[str, DesignClassification] = {}
        self._stability_slices: dict[str, StabilitySlice] = {}
        self._walk_forward_summaries: dict[str, WalkForwardNullSummary] = {}
        self._follow_up_qualifications: dict[str, FollowUpQualification] = {}

    def spec_by_id(self, spec_id: str) -> NullControlSpecV2 | None:
        return self._specs.get(spec_id)

    def save_spec(self, spec: NullControlSpecV2) -> NullControlSpecV2:
        return self._specs.setdefault(spec.spec_id, spec)

    def specs_for_calibration(self, calibration_id: str) -> tuple[NullControlSpecV2, ...]:
        return tuple(
            sorted(
                (item for item in self._specs.values() if item.calibration_id == calibration_id),
                key=lambda item: item.ordinal,
            )
        )

    def specs_for_stratum(self, matched_stratum: str) -> tuple[NullControlSpecV2, ...]:
        return tuple(
            item for item in self._specs.values() if item.matched_stratum == matched_stratum
        )

    def outcome_by_spec(self, spec_id: str) -> NullControlOutcome | None:
        return self._outcomes.get(spec_id)

    def save_outcome(self, outcome: NullControlOutcome) -> NullControlOutcome:
        return self._outcomes.setdefault(outcome.spec_id, outcome)

    def outcomes_for_run(self, calibration_run_id: str) -> tuple[NullControlOutcome, ...]:
        return tuple(
            item
            for item in self._outcomes.values()
            if item.calibration_run_id == calibration_run_id
        )

    def save_classification(self, item: DesignClassification) -> DesignClassification:
        return self._classifications.setdefault(item.classification_id, item)

    def classifications_for_calibration(
        self, calibration_id: str
    ) -> tuple[DesignClassification, ...]:
        return tuple(
            item for item in self._classifications.values() if item.calibration_id == calibration_id
        )

    def classification_for_subject(
        self, *, calibration_id: str, subject_id: str
    ) -> DesignClassification | None:
        for item in self._classifications.values():
            if item.calibration_id == calibration_id and item.subject_id == subject_id:
                return item
        return None

    def save_stability_slice(self, item: StabilitySlice) -> StabilitySlice:
        return self._stability_slices.setdefault(item.slice_id, item)

    def stability_slices_for_calibration(self, calibration_id: str) -> tuple[StabilitySlice, ...]:
        return tuple(
            item
            for item in self._stability_slices.values()
            if item.calibration_id == calibration_id
        )

    def save_walk_forward_summary(self, item: WalkForwardNullSummary) -> WalkForwardNullSummary:
        return self._walk_forward_summaries.setdefault(item.spec_id, item)

    def walk_forward_summaries_for_calibration(
        self, calibration_id: str
    ) -> tuple[WalkForwardNullSummary, ...]:
        return tuple(
            item
            for item in self._walk_forward_summaries.values()
            if item.calibration_id == calibration_id
        )

    def save_follow_up_qualification(self, item: FollowUpQualification) -> FollowUpQualification:
        return self._follow_up_qualifications.setdefault(item.assessment_id, item)

    def follow_up_qualifications_for_calibration(
        self, calibration_id: str
    ) -> tuple[FollowUpQualification, ...]:
        return tuple(
            item
            for item in self._follow_up_qualifications.values()
            if item.calibration_id == calibration_id
        )
