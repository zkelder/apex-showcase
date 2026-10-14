"""Adapter-free orchestration for Research Signal Calibration and
Negative-Control Hardening v1.

Declares a bounded, deterministic null/negative-control battery against the
*same* retained cross-market dataset/replay lineage the genuine 25-hypothesis
Cross-Market Research v1 agenda used, runs every control through the exact
same governed trial/robustness/conclusion/promotion machinery
(``application.research_methodology``, entirely reused and unmodified), and
produces an honest empirical false-discovery calibration plus a derived,
non-destructive reassessment of the prior agenda's already-retained
conclusions and follow-up proposals.

This module never re-registers, re-runs, or widens the prior agenda
(``application.cross_market_research``); it only *reads* that agenda's
already-persisted evidence (through the same repository protocols that
agenda already defines) and *reuses* its ``BuildCrossMarketPanel`` output
(the identical retained dataset, rebuilt from already-fetched history --
never a new provider request) to evaluate its own, separate controls.

Every service here has authority ``NONE``. Nothing here creates a Strategy,
Shared Opportunity, PAPER order, or execution authority, and nothing here
claims trading edge or alpha -- it measures how often the existing research
methodology's own vocabulary would apply itself to constructions known or
strongly suspected to carry no genuine predictive relationship.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apex.application.cross_market_research import (
    AgendaTimeline,
    CrossMarketPanel,
    CrossMarketResearchRepository,
    derive_knowledge_status,
    record_panel_sample_trial,
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
    CrossMarketRegimeDimension,
    CrossMarketRegimeSnapshot,
    CrossMarketResearchError,
    DatedValue,
    FeatureFamily,
    FeatureId,
    FollowUpProposal,
    GeneratedHypothesis,
    KnowledgeStatus,
    OutcomeFamily,
    OutcomeId,
    ResearchKnowledgeEntry,
    deterministic_noise_series,
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
    CalibratedPromotionPolicy,
    CalibrationBreakdown,
    CalibrationPlan,
    CalibrationRateSummary,
    CalibrationReport,
    CalibrationRun,
    CalibrationRunStatus,
    EffectSizeFloorPolicy,
    EvidenceGrade,
    EvidenceGradePolicy,
    EvidenceProfile,
    FollowUpPriorityAssessment,
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
    constant_series,
    derive_evidence_grade,
    effect_meets_floor,
    evidence_grade_at_least,
    null_calibration_percentile,
    sign_randomize,
    synthetic_ar1_surrogate,
)
from apex.domain.research_methodology import (
    APEX_PRODUCTION_PROMOTION_POLICY,
    BenchmarkKind,
    ConfigurationOrigin,
    CorrectedTestResult,
    GovernanceIntent,
    MultipleTestingCorrectionMethod,
    PromotionPolicy,
    ResearchConclusion,
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
    newey_west_standard_error,
    robustness_cost_stress,
    seeded_bootstrap_confidence_interval,
    two_sided_z_test_p_value,
)
from apex.domain.values import deterministic_id

MINIMUM_TRIAL_SAMPLE = 8
STATISTICAL_METHOD_BOOTSTRAP_SEED = 20260807100
HAC_LAG_SESSIONS = 5
Z_CRITICAL_95 = Decimal("1.96")

CALIBRATION_LIMITATIONS = (
    "POINT_IN_TIME_UNIVERSE_INCOMPLETE",
    "SURVIVORSHIP_LIMITED",
    "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE",
    "NULL_CONTROLS_USE_ONLY_THE_PREDICTIVE_RELATIONSHIP_TRIAL_SHAPE_NOT_PERSISTENCE_OR_CONDITIONAL",
    "NULL_CONTROLS_DO_NOT_RUN_WALK_FORWARD_FOLDS_OR_A_FULL_STABILITY_REPORT",
    "ZERO_INTERACTION_CONDITIONAL_MIMICKING_NULL_CONTROLS_THIS_CYCLE",
)


class CalibrationApplicationError(RuntimeError):
    """A calibration application-layer precondition was violated."""


class CalibrationNotFound(CalibrationApplicationError):
    pass


class CalibrationConflict(CalibrationApplicationError):
    pass


def calibration_warnings() -> tuple[str, ...]:
    from apex.domain.research_calibration import research_calibration_warnings

    return research_calibration_warnings()


# ==========================================================================
# Repository port
# ==========================================================================


class CalibrationRepository(Protocol):
    def plan_by_id(self, calibration_id: str) -> CalibrationPlan | None: ...
    def save_plan(self, plan: CalibrationPlan) -> CalibrationPlan: ...
    def list_plans(self) -> tuple[CalibrationPlan, ...]: ...

    def spec_by_id(self, spec_id: str) -> NullControlSpec | None: ...
    def save_spec(self, spec: NullControlSpec) -> NullControlSpec: ...
    def specs_for_calibration(self, calibration_id: str) -> tuple[NullControlSpec, ...]: ...

    def run_by_calibration(self, calibration_id: str) -> CalibrationRun | None: ...
    def run_by_id(self, calibration_run_id: str) -> CalibrationRun | None: ...
    def save_run(self, run: CalibrationRun) -> CalibrationRun: ...

    def outcome_by_spec(self, spec_id: str) -> NullControlOutcome | None: ...
    def save_outcome(self, outcome: NullControlOutcome) -> NullControlOutcome: ...
    def outcomes_for_run(self, calibration_run_id: str) -> tuple[NullControlOutcome, ...]: ...

    def save_breakdown(self, breakdown: CalibrationBreakdown) -> CalibrationBreakdown: ...
    def breakdowns_for_calibration(
        self, calibration_id: str
    ) -> tuple[CalibrationBreakdown, ...]: ...

    def save_method_comparison(
        self, comparison: StatisticalMethodComparison
    ) -> StatisticalMethodComparison: ...
    def method_comparisons_for_calibration(
        self, calibration_id: str
    ) -> tuple[StatisticalMethodComparison, ...]: ...

    def report_by_calibration(self, calibration_id: str) -> CalibrationReport | None: ...
    def save_report(self, report: CalibrationReport) -> CalibrationReport: ...

    def save_recalibration(
        self, item: PriorHypothesisRecalibration
    ) -> PriorHypothesisRecalibration: ...
    def recalibrations_for_calibration(
        self, calibration_id: str
    ) -> tuple[PriorHypothesisRecalibration, ...]: ...

    def save_follow_up_priority(
        self, item: FollowUpPriorityAssessment
    ) -> FollowUpPriorityAssessment: ...
    def follow_up_priorities_for_calibration(
        self, calibration_id: str
    ) -> tuple[FollowUpPriorityAssessment, ...]: ...

    def save_calibrated_promotion(
        self, item: CalibratedPromotionEvaluation
    ) -> CalibratedPromotionEvaluation: ...
    def calibrated_promotions_for_calibration(
        self, calibration_id: str
    ) -> tuple[CalibratedPromotionEvaluation, ...]: ...


# ==========================================================================
# Section 1: the declared V1 calibration plan and null-control battery
# ==========================================================================

V1_CALIBRATION_VERSION = "V1"
V1_CALIBRATION_OBJECTIVE = (
    "Measure how often the current Apex cross-market research methodology "
    "produces an apparently interesting result (WEAK or better, or BH-FDR-corrected "
    "significant) when evaluated against constructions known or strongly suspected "
    "to carry no genuine predictive relationship, using the identical governed "
    "trial/robustness/conclusion/promotion machinery genuine hypotheses use. "
    "Methodology calibration only -- creates no Strategy, Shared Opportunity, PAPER "
    "order, or execution authority, and claims no trading edge or alpha."
)
V1_CALIBRATION_RANDOM_SEED_POLICY = (
    "Every seeded transform in this battery uses a fixed, declared integer seed "
    "chosen before any control was executed (see V1_NULL_CONTROL_SPECS below); no "
    "seed is selected, retried, or swept after observing a result."
)
V1_CALIBRATION_BLOCK_LENGTH_POLICY = (
    "Block-permutation controls use block lengths {5, 20} trading sessions, fixed "
    "a priori to span roughly one week and one month of autocorrelation structure; "
    "never chosen by examining which block length produces a particular outcome."
)
V1_CALIBRATION_HORIZONS = (5, 20)

# -- base feature/outcome probes (declared, bounded, before any result) ----

_BASE_FEATURE_PROBES: tuple[tuple[str, FeatureFamily], ...] = (
    ("EQUITY_BREADTH_FRACTION_ADVANCING", FeatureFamily.EQUITY_BREADTH),
    ("EQUITY_VOLATILITY_20", FeatureFamily.EQUITY_VOLATILITY),
    ("RATES_PROXY_RETURN_20", FeatureFamily.RATES_PROXY_TREND),
    ("COMMODITY_PROXY_RETURN_20", FeatureFamily.COMMODITY_PROXY_TREND),
    ("BTC_RETURN_20", FeatureFamily.BTC_MOMENTUM),
)
_OUTCOME_PROBES: tuple[tuple[str, OutcomeFamily, int], ...] = (
    ("EQUITY_FORWARD_RETURN", OutcomeFamily.EQUITY_BROAD_RETURN, 20),
    ("BTC_FORWARD_RETURN", OutcomeFamily.BTC_RETURN, 5),
)
_TEMPORAL_DISPLACEMENT_LAGS = (60, 120)
_BLOCK_PERMUTE_LENGTHS = (5, 20)
_SIGN_RANDOMIZE_SEED = 20260807101
_DATE_SHUFFLE_SEED = 20260807102
_SYNTHETIC_AR1_SEED = 20260807103

# -- economic-negative-control pairs: real, untransformed, retained
# evidence deliberately paired across markets/timeframes with no plausible
# transmission channel. Declared once, before any result. --------------

_ECONOMIC_NEGATIVE_CONTROL_PAIRS: tuple[
    tuple[str, FeatureFamily, str, OutcomeFamily, int, str], ...
] = (
    (
        "RATES_PROXY_RETURN_20",
        FeatureFamily.RATES_PROXY_TREND,
        "BTC_FORWARD_VOLATILITY",
        OutcomeFamily.BTC_VOLATILITY,
        5,
        "Rates-proxy trend has no plausible one-week transmission channel to BTC volatility.",
    ),
    (
        "COMMODITY_PROXY_RETURN_20",
        FeatureFamily.COMMODITY_PROXY_TREND,
        "EQUITY_FORWARD_DISPERSION",
        OutcomeFamily.EQUITY_BROAD_DISPERSION,
        20,
        "Commodity-proxy trend has no established channel to cross-sectional equity dispersion.",
    ),
    (
        "EQUITY_SECTOR_BREADTH_20",
        FeatureFamily.EQUITY_SECTOR_BREADTH,
        "RATES_PROXY_FORWARD_RETURN",
        OutcomeFamily.RATES_PROXY_RETURN,
        20,
        "Equity sector participation has no established channel to rates-proxy return.",
    ),
    (
        "EQUITY_LARGE_MINUS_SMALL_20",
        FeatureFamily.EQUITY_STYLE_RELATIVE_STRENGTH,
        "BTC_FORWARD_VOLATILITY",
        OutcomeFamily.BTC_VOLATILITY,
        20,
        "Equity size-factor spread has no plausible channel to BTC volatility.",
    ),
    (
        "EQUITY_GROWTH_MINUS_VALUE_20",
        FeatureFamily.EQUITY_STYLE_RELATIVE_STRENGTH,
        "BTC_FORWARD_RETURN",
        OutcomeFamily.BTC_RETURN,
        5,
        "Equity style spread has no plausible one-week channel to BTC return.",
    ),
    (
        "EQUITY_CYCLICAL_MINUS_DEFENSIVE_20",
        FeatureFamily.EQUITY_STYLE_RELATIVE_STRENGTH,
        "RATES_PROXY_FORWARD_RETURN",
        OutcomeFamily.RATES_PROXY_RETURN,
        20,
        "Equity sector-style spread has no established channel to rates-proxy return.",
    ),
    (
        "BTC_DRAWDOWN",
        FeatureFamily.BTC_VOLATILITY,
        "EQUITY_FORWARD_DISPERSION",
        OutcomeFamily.EQUITY_BROAD_DISPERSION,
        20,
        "BTC's own drawdown has no plausible channel to equity cross-sectional dispersion.",
    ),
    (
        "BTC_VOLATILITY_20",
        FeatureFamily.BTC_VOLATILITY,
        "RATES_PROXY_FORWARD_RETURN",
        OutcomeFamily.RATES_PROXY_RETURN,
        20,
        "BTC volatility has no established channel to rates-proxy return.",
    ),
    (
        "COMMODITY_EQUITY_CORRELATION_60",
        FeatureFamily.COMMODITY_EQUITY_RELATIONSHIP,
        "BTC_FORWARD_VOLATILITY",
        OutcomeFamily.BTC_VOLATILITY,
        5,
        "A slow 60-day commodity/equity correlation has no plausible one-week channel to BTC "
        "volatility.",
    ),
    (
        "RATES_EQUITY_CORRELATION_60",
        FeatureFamily.RATES_EQUITY_RELATIONSHIP,
        "BTC_FORWARD_RETURN",
        OutcomeFamily.BTC_RETURN,
        5,
        "A slow 60-day rates/equity correlation has no plausible one-week channel to BTC return.",
    ),
    (
        "EQUITY_AVERAGE_CORRELATION_60",
        FeatureFamily.EQUITY_CORRELATION,
        "RATES_PROXY_FORWARD_RETURN",
        OutcomeFamily.RATES_PROXY_RETURN,
        20,
        "Average equity correlation has no established channel to rates-proxy return.",
    ),
    (
        "EQUITY_RETURN_252",
        FeatureFamily.EQUITY_TREND,
        "BTC_FORWARD_VOLATILITY",
        OutcomeFamily.BTC_VOLATILITY,
        5,
        "A 252-session equity return has no plausible one-week channel to BTC volatility.",
    ),
    (
        "EQUITY_RETURN_90",
        FeatureFamily.EQUITY_TREND,
        "RATES_PROXY_FORWARD_RETURN",
        OutcomeFamily.RATES_PROXY_RETURN,
        5,
        "A 90-session equity return has no plausible one-week channel to rates-proxy return.",
    ),
    (
        "BTC_RETURN_90",
        FeatureFamily.BTC_MOMENTUM,
        "EQUITY_FORWARD_DISPERSION",
        OutcomeFamily.EQUITY_BROAD_DISPERSION,
        5,
        "A 90-session BTC return has no plausible one-week channel to equity cross-sectional "
        "dispersion.",
    ),
    (
        "EQUITY_DRAWDOWN",
        FeatureFamily.EQUITY_TREND,
        "RATES_PROXY_FORWARD_RETURN",
        OutcomeFamily.RATES_PROXY_RETURN,
        20,
        "Equity drawdown depth has no established channel to rates-proxy return specifically.",
    ),
    (
        "COMMODITY_PROXY_RETURN_20",
        FeatureFamily.COMMODITY_PROXY_TREND,
        "RATES_PROXY_FORWARD_RETURN",
        OutcomeFamily.RATES_PROXY_RETURN,
        5,
        "Commodity trend has no plausible one-week channel to rates-proxy return.",
    ),
    (
        "EQUITY_SECTOR_DISPERSION_20",
        FeatureFamily.EQUITY_SECTOR_BREADTH,
        "RATES_PROXY_FORWARD_RETURN",
        OutcomeFamily.RATES_PROXY_RETURN,
        20,
        "Sector-level equity dispersion has no established channel to rates-proxy return.",
    ),
    (
        "BTC_EQUITY_CORRELATION_60",
        FeatureFamily.BTC_EQUITY_RELATIONSHIP,
        "RATES_PROXY_FORWARD_RETURN",
        OutcomeFamily.RATES_PROXY_RETURN,
        20,
        "A slow BTC/equity correlation has no established channel to rates-proxy return "
        "specifically.",
    ),
    (
        "EQUITY_RETURN_60",
        FeatureFamily.EQUITY_TREND,
        "BTC_FORWARD_VOLATILITY",
        OutcomeFamily.BTC_VOLATILITY,
        5,
        "A 60-session equity return has no plausible one-week channel to BTC volatility.",
    ),
    (
        "RATES_PROXY_RETURN_20",
        FeatureFamily.RATES_PROXY_TREND,
        "EQUITY_FORWARD_DISPERSION",
        OutcomeFamily.EQUITY_BROAD_DISPERSION,
        5,
        "Rates-proxy trend has no plausible one-week channel to equity cross-sectional dispersion.",
    ),
)

# -- persistence/random baselines: already-established null shapes -------

_RANDOM_NOISE_PAIRS: tuple[tuple[str, OutcomeFamily, int, int], ...] = (
    ("EQUITY_FORWARD_RETURN", OutcomeFamily.EQUITY_BROAD_RETURN, 20, 20260807201),
    ("BTC_FORWARD_RETURN", OutcomeFamily.BTC_RETURN, 5, 20260807202),
    ("EQUITY_FORWARD_VOLATILITY", OutcomeFamily.EQUITY_BROAD_VOLATILITY, 20, 20260807203),
    ("BTC_FORWARD_VOLATILITY", OutcomeFamily.BTC_VOLATILITY, 5, 20260807204),
    ("RATES_PROXY_FORWARD_RETURN", OutcomeFamily.RATES_PROXY_RETURN, 20, 20260807205),
    ("EQUITY_FORWARD_DISPERSION", OutcomeFamily.EQUITY_BROAD_DISPERSION, 20, 20260807206),
    ("BTC_FORWARD_RETURN", OutcomeFamily.BTC_RETURN, 20, 20260807207),
)
_UNCONDITIONAL_BASELINE_PAIRS: tuple[tuple[str, OutcomeFamily, int], ...] = (
    ("EQUITY_FORWARD_RETURN", OutcomeFamily.EQUITY_BROAD_RETURN, 20),
    ("BTC_FORWARD_RETURN", OutcomeFamily.BTC_RETURN, 5),
    ("EQUITY_FORWARD_VOLATILITY", OutcomeFamily.EQUITY_BROAD_VOLATILITY, 20),
    ("BTC_FORWARD_VOLATILITY", OutcomeFamily.BTC_VOLATILITY, 5),
    ("RATES_PROXY_FORWARD_RETURN", OutcomeFamily.RATES_PROXY_RETURN, 5),
    ("EQUITY_FORWARD_DISPERSION", OutcomeFamily.EQUITY_BROAD_DISPERSION, 5),
    ("BTC_FORWARD_RETURN", OutcomeFamily.BTC_RETURN, 20),
)


def _design_family_for(feature_family: FeatureFamily) -> NullControlDesignFamily:
    name = feature_family.value
    if name.startswith("EQUITY_"):
        return NullControlDesignFamily.EQUITY_INTERNAL
    if name.startswith("BTC_"):
        return NullControlDesignFamily.EQUITY_BTC
    if name.startswith("RATES_"):
        return NullControlDesignFamily.FIXED_INCOME_CONTEXT
    if name.startswith("COMMODITY_"):
        return NullControlDesignFamily.COMMODITY_CONTEXT
    return NullControlDesignFamily.PURE_MATHEMATICAL_NULL


def build_v1_calibration_plan(
    *,
    dataset_snapshot_id: str,
    replay_run_id: str,
    cross_market_dataset_id: str,
    source_agenda_id: str,
    registered_at: datetime,
) -> CalibrationPlan:
    """The one genuine bounded v1 calibration plan's fixed policy -- every
    field except the runtime dataset/replay/agenda lineage is a fixed
    constant, declared once here so the CLI, the API, and the genuine
    acceptance run all register byte-identical plans from the same lineage
    inputs. Reuses the *same* dataset/replay/cross-market-dataset lineage
    the genuine agenda used -- never a refreshed or different dataset."""

    return CalibrationPlan.create(
        version=V1_CALIBRATION_VERSION,
        objective=V1_CALIBRATION_OBJECTIVE,
        dataset_snapshot_id=dataset_snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=cross_market_dataset_id,
        source_agenda_id=source_agenda_id,
        research_methodology_policy_id=(
            "RESEARCH_METHODOLOGY_V1+CONTROLLED_CROSS_MARKET_HYPOTHESIS_RESEARCH_V1"
        ),
        allowed_feature_families=tuple(FeatureFamily),
        allowed_outcome_families=tuple(OutcomeFamily),
        allowed_horizons_sessions=V1_CALIBRATION_HORIZONS,
        null_generation_methods=tuple(NullGenerationMethod),
        negative_control_budget=V1_CALIBRATION_TOTAL_CONTROLS,
        random_seed_policy=V1_CALIBRATION_RANDOM_SEED_POLICY,
        multiple_testing_method=MultipleTestingCorrectionMethod.BENJAMINI_HOCHBERG_FDR,
        block_length_policy=V1_CALIBRATION_BLOCK_LENGTH_POLICY,
        conclusion_policy_id="RESEARCH_SIGNAL_CALIBRATION_V1",
        registered_at=registered_at,
    )


def generate_v1_null_control_specs(
    *, calibration: CalibrationPlan, created_at: datetime
) -> tuple[NullControlSpec, ...]:
    """Deterministic, declared-before-results expansion of the fixed
    module-level probe/pair tables above into the full ~104-control
    battery (section 2/3). Calling this twice with the same calibration
    always produces byte-identical specs -- every field, including
    ``ordinal``, is a pure function of these fixed tables."""

    specs: list[NullControlSpec] = []
    ordinal = 0

    def _add(
        *,
        family: NullControlFamily,
        nature: NullControlNature,
        design_family: NullControlDesignFamily,
        control_kind: str,
        null_method: NullGenerationMethod,
        source_feature_id: str,
        source_feature_family: FeatureFamily,
        outcome_id: str,
        outcome_family: OutcomeFamily,
        horizon_sessions: int,
        method_params: tuple[tuple[str, str], ...],
        rationale: str,
    ) -> None:
        nonlocal ordinal
        ordinal += 1
        specs.append(
            NullControlSpec.create(
                calibration=calibration,
                ordinal=ordinal,
                created_at=created_at,
                family=family,
                nature=nature,
                design_family=design_family,
                control_kind=control_kind,
                null_method=null_method,
                source_feature_id=source_feature_id,
                source_feature_family=source_feature_family,
                outcome_id=outcome_id,
                outcome_family=outcome_family,
                horizon_sessions=horizon_sessions,
                method_params=method_params,
                rationale=rationale,
            )
        )

    # -- TEMPORAL_DISPLACEMENT: far lag-displacement of a real feature ----
    for feature_id, feature_family in _BASE_FEATURE_PROBES:
        for outcome_id, outcome_family, horizon in _OUTCOME_PROBES:
            for lag in _TEMPORAL_DISPLACEMENT_LAGS:
                _add(
                    family=NullControlFamily.TEMPORAL_DISPLACEMENT,
                    nature=NullControlNature.TEMPORAL_NULL,
                    design_family=_design_family_for(feature_family),
                    control_kind="FAR_LAG_DISPLACED_FEATURE",
                    null_method=NullGenerationMethod.TEMPORAL_LAG_DISPLACEMENT,
                    source_feature_id=feature_id,
                    source_feature_family=feature_family,
                    outcome_id=outcome_id,
                    outcome_family=outcome_family,
                    horizon_sessions=horizon,
                    method_params=(("lag_sessions", str(lag)),),
                    rationale=(
                        f"{feature_id} displaced {lag} sessions backward -- far beyond any "
                        f"plausible {horizon}-session informative horizon -- should show no "
                        f"relationship with subsequent {outcome_id}."
                    ),
                )

    # -- DETERMINISTIC_PERMUTATION: block permutation ----------------------
    for feature_id, feature_family in _BASE_FEATURE_PROBES:
        for outcome_id, outcome_family, horizon in _OUTCOME_PROBES:
            for block_length in _BLOCK_PERMUTE_LENGTHS:
                _add(
                    family=NullControlFamily.DETERMINISTIC_PERMUTATION,
                    nature=NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
                    design_family=_design_family_for(feature_family),
                    control_kind="BLOCK_PERMUTED_FEATURE",
                    null_method=NullGenerationMethod.BLOCK_PERMUTATION,
                    source_feature_id=feature_id,
                    source_feature_family=feature_family,
                    outcome_id=outcome_id,
                    outcome_family=outcome_family,
                    horizon_sessions=horizon,
                    method_params=(
                        ("block_length", str(block_length)),
                        ("seed", str(_SIGN_RANDOMIZE_SEED)),
                    ),
                    rationale=(
                        f"{feature_id} with block-length-{block_length} block order permuted "
                        f"(within-block structure preserved, cross-block date alignment "
                        f"destroyed) should show no relationship with subsequent {outcome_id}."
                    ),
                )

    # -- DETERMINISTIC_PERMUTATION: sign randomization ----------------------
    for feature_id, feature_family in _BASE_FEATURE_PROBES:
        for outcome_id, outcome_family, horizon in _OUTCOME_PROBES:
            _add(
                family=NullControlFamily.DETERMINISTIC_PERMUTATION,
                nature=NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
                design_family=_design_family_for(feature_family),
                control_kind="SIGN_RANDOMIZED_FEATURE",
                null_method=NullGenerationMethod.SIGN_RANDOMIZATION,
                source_feature_id=feature_id,
                source_feature_family=feature_family,
                outcome_id=outcome_id,
                outcome_family=outcome_family,
                horizon_sessions=horizon,
                method_params=(("seed", str(_SIGN_RANDOMIZE_SEED)),),
                rationale=(
                    f"{feature_id} with each observation's sign independently randomized "
                    f"(magnitude/volatility preserved, direction destroyed) should show no "
                    f"relationship with subsequent {outcome_id}."
                ),
            )

    # -- DETERMINISTIC_PERMUTATION: IID date shuffle ------------------------
    for feature_id, feature_family in _BASE_FEATURE_PROBES:
        for outcome_id, outcome_family, horizon in _OUTCOME_PROBES:
            _add(
                family=NullControlFamily.DETERMINISTIC_PERMUTATION,
                nature=NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
                design_family=_design_family_for(feature_family),
                control_kind="SHUFFLED_FEATURE",
                null_method=NullGenerationMethod.IID_DATE_SHUFFLE,
                source_feature_id=feature_id,
                source_feature_family=feature_family,
                outcome_id=outcome_id,
                outcome_family=outcome_family,
                horizon_sessions=horizon,
                method_params=(("seed", str(_DATE_SHUFFLE_SEED)),),
                rationale=(
                    f"{feature_id} with its date alignment destroyed by an IID shuffle should "
                    f"show no relationship with subsequent {outcome_id}."
                ),
            )

    # -- SEEDED_SYNTHETIC_NULL: AR(1) surrogate -----------------------------
    for feature_id, feature_family in _BASE_FEATURE_PROBES:
        for outcome_id, outcome_family, horizon in _OUTCOME_PROBES:
            _add(
                family=NullControlFamily.SEEDED_SYNTHETIC_NULL,
                nature=NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
                design_family=_design_family_for(feature_family),
                control_kind="SYNTHETIC_AR1_SURROGATE_FEATURE",
                null_method=NullGenerationMethod.SYNTHETIC_AR1_SURROGATE,
                source_feature_id=feature_id,
                source_feature_family=feature_family,
                outcome_id=outcome_id,
                outcome_family=outcome_family,
                horizon_sessions=horizon,
                method_params=(("seed", str(_SYNTHETIC_AR1_SEED)),),
                rationale=(
                    f"A deterministic AR(1) surrogate fit only to {feature_id}'s own mean, "
                    f"volatility, and lag-1 autocorrelation (a genuinely synthetic path) "
                    f"should show no relationship with subsequent {outcome_id}."
                ),
            )

    # -- ECONOMIC_NEGATIVE_CONTROL: real, untransformed, implausible pairs -
    for (
        feature_id,
        feature_family,
        outcome_id,
        outcome_family,
        horizon,
        rationale,
    ) in _ECONOMIC_NEGATIVE_CONTROL_PAIRS:
        _add(
            family=NullControlFamily.ECONOMIC_NEGATIVE_CONTROL,
            nature=NullControlNature.ECONOMIC_NEGATIVE_CONTROL,
            design_family=_design_family_for(feature_family),
            control_kind="ECONOMICALLY_UNRELATED_PAIR",
            null_method=NullGenerationMethod.NONE_ECONOMIC_PAIRING,
            source_feature_id=feature_id,
            source_feature_family=feature_family,
            outcome_id=outcome_id,
            outcome_family=outcome_family,
            horizon_sessions=horizon,
            method_params=(),
            rationale=rationale,
        )

    # -- PERSISTENCE_RANDOM_BASELINE: seeded noise --------------------------
    for outcome_id, outcome_family, horizon, seed in _RANDOM_NOISE_PAIRS:
        _add(
            family=NullControlFamily.PERSISTENCE_RANDOM_BASELINE,
            nature=NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
            design_family=NullControlDesignFamily.PURE_MATHEMATICAL_NULL,
            control_kind="RANDOM_DATE_BASELINE",
            null_method=NullGenerationMethod.RANDOM_NOISE_BASELINE,
            source_feature_id="RANDOM_NOISE_BASELINE",
            source_feature_family=FeatureFamily.NEGATIVE_CONTROL,
            outcome_id=outcome_id,
            outcome_family=outcome_family,
            horizon_sessions=horizon,
            method_params=(("seed", str(seed)),),
            rationale=(
                f"Pure seeded noise, unrelated to any genuine market evidence, should show "
                f"no relationship with subsequent {outcome_id}."
            ),
        )

    # -- PERSISTENCE_RANDOM_BASELINE: unconditional baseline ----------------
    for outcome_id, outcome_family, horizon in _UNCONDITIONAL_BASELINE_PAIRS:
        _add(
            family=NullControlFamily.PERSISTENCE_RANDOM_BASELINE,
            nature=NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
            design_family=NullControlDesignFamily.PURE_MATHEMATICAL_NULL,
            control_kind="UNCONDITIONAL_BASELINE",
            null_method=NullGenerationMethod.UNCONDITIONAL_BASELINE,
            source_feature_id="ALWAYS_PRESENT_BASELINE",
            source_feature_family=FeatureFamily.NEGATIVE_CONTROL,
            outcome_id=outcome_id,
            outcome_family=outcome_family,
            horizon_sessions=horizon,
            method_params=(),
            rationale=(
                "An always-present condition (the full unconditional sample) tests whether "
                f"the methodology fabricates an edge with no real conditioning information "
                f"against subsequent {outcome_id}."
            ),
        )

    return tuple(specs)


V1_CALIBRATION_TOTAL_CONTROLS = (
    len(_BASE_FEATURE_PROBES) * len(_OUTCOME_PROBES) * len(_TEMPORAL_DISPLACEMENT_LAGS)
    + len(_BASE_FEATURE_PROBES) * len(_OUTCOME_PROBES) * len(_BLOCK_PERMUTE_LENGTHS)
    + len(_BASE_FEATURE_PROBES) * len(_OUTCOME_PROBES)
    + len(_BASE_FEATURE_PROBES) * len(_OUTCOME_PROBES)
    + len(_BASE_FEATURE_PROBES) * len(_OUTCOME_PROBES)
    + len(_ECONOMIC_NEGATIVE_CONTROL_PAIRS)
    + len(_RANDOM_NOISE_PAIRS)
    + len(_UNCONDITIONAL_BASELINE_PAIRS)
)


# ==========================================================================
# Section 2: null-series derivation and the shared TRAIN-median condition
# split (reused by both ``RunNullControl`` and ``FinalizeCalibration``, so
# a reconstructed sample for diagnostics comparison is always byte-
# identical to the one actually evaluated).
# ==========================================================================


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def median_split_dates(
    *, feature_by_date: dict[str, Decimal], train_dates: frozenset[str]
) -> frozenset[str]:
    """The one condition rule every null control uses: "above the TRAIN-
    period median" -- derived only from TRAIN, applied identically to every
    later period, exactly mirroring
    ``application.cross_market_research.resolve_condition_dates``'s
    ``ABOVE_TRAIN_MEDIAN`` branch."""

    train_values = [feature_by_date[d] for d in train_dates if d in feature_by_date]
    threshold = _median(train_values)
    if threshold is None:
        return frozenset()
    return frozenset(d for d, value in feature_by_date.items() if value >= threshold)


def derive_null_series(*, spec: NullControlSpec, panel: CrossMarketPanel) -> tuple[DatedValue, ...]:
    """Pure, deterministic derivation of one control's evaluated series
    from already-retained panel evidence. Never issues a provider request;
    the transform's only inputs are ``panel`` (already built from retained
    history) and ``spec`` (already declared)."""

    dates = panel.session_dates
    if spec.null_method is NullGenerationMethod.RANDOM_NOISE_BASELINE:
        seed = int(dict(spec.method_params)["seed"])
        return deterministic_noise_series(dates, seed=seed)
    if spec.null_method is NullGenerationMethod.UNCONDITIONAL_BASELINE:
        return constant_series(dates)
    raw = panel.feature_by_date(FeatureId(spec.source_feature_id))
    base = tuple(DatedValue(d, raw[d]) for d in dates if d in raw)
    if spec.null_method is NullGenerationMethod.NONE_ECONOMIC_PAIRING:
        return base
    if spec.null_method is NullGenerationMethod.TEMPORAL_LAG_DISPLACEMENT:
        sessions = int(dict(spec.method_params)["lag_sessions"])
        return lag_displace(base, sessions=sessions)
    if spec.null_method is NullGenerationMethod.BLOCK_PERMUTATION:
        params = dict(spec.method_params)
        return block_permute(
            base, seed=int(params["seed"]), block_length=int(params["block_length"])
        )
    if spec.null_method is NullGenerationMethod.SIGN_RANDOMIZATION:
        seed = int(dict(spec.method_params)["seed"])
        return sign_randomize(base, seed=seed)
    if spec.null_method is NullGenerationMethod.IID_DATE_SHUFFLE:
        seed = int(dict(spec.method_params)["seed"])
        return deterministic_shuffle(base, seed=seed)
    if spec.null_method is NullGenerationMethod.SYNTHETIC_AR1_SURROGATE:
        seed = int(dict(spec.method_params)["seed"])
        return synthetic_ar1_surrogate(base, seed=seed)
    raise CalibrationApplicationError("UNKNOWN_NULL_GENERATION_METHOD")


def _direction_consistent(base_mean: Decimal, candidate_mean: Decimal | None) -> bool | None:
    if candidate_mean is None or base_mean == 0:
        return None
    return (candidate_mean > 0) == (base_mean > 0)


# ==========================================================================
# Section 3: shared calibration timeline -- reuses
# ``application.cross_market_research.AgendaTimeline``/``build_agenda_timeline``
# directly (identical governed TRAIN/VALIDATION/HOLDOUT periods the genuine
# agenda used, recomputed from the same panel/dates rather than a second,
# possibly-divergent declaration). Nothing new is defined in this section;
# it exists only to record that choice.
# ==========================================================================
# ==========================================================================
# Section 4: RunNullControl -- one control through the exact governed
# methodology stack real hypotheses use (mirrors
# ``application.cross_market_research.RunSingleHypothesis`` closely,
# parameterized directly by derived series rather than a ``FeatureId``-
# typed hypothesis; walk-forward folds and the full stability report are
# out of scope this cycle -- see ``CALIBRATION_LIMITATIONS``).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class NullControlRunResult:
    governance_policy_id: str
    exploratory_trial_id: str | None
    validation_trial_id: str | None
    holdout_trial_id: str | None
    robustness_run_id: str | None
    conclusion_id: str
    promotion_evaluation_id: str
    knowledge_status: KnowledgeStatus
    blockers: tuple[str, ...]
    trials_recorded: int


class RunNullControl:
    def __init__(
        self,
        *,
        methodology_repository: ResearchMethodologyRepository,
        timeline: AgendaTimeline,
    ) -> None:
        self._methodology_repository = methodology_repository
        self._timeline = timeline

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        spec: NullControlSpec,
        panel: CrossMarketPanel,
        created_at: datetime,
    ) -> NullControlRunResult:
        null_series = derive_null_series(spec=spec, panel=panel)
        feature_by_date = {
            item.session_date: item.value for item in null_series if item.value is not None
        }
        outcome_by_date = panel.outcome_by_date(
            OutcomeId(spec.outcome_id), horizon_sessions=spec.horizon_sessions
        )
        regime_by_date = panel.regime_by_date()
        train_frozenset = frozenset(self._timeline.train_dates)

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
        primary_dates = median_split_dates(
            feature_by_date=feature_by_date, train_dates=train_frozenset
        )
        market_scope = (spec.source_feature_family.value, spec.outcome_family.value)

        def sample_universe(period_dates: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
            return period_dates & primary_dates, period_dates

        blockers: list[str] = []
        trials_recorded = 0

        exploratory_trial = self._trial(
            calibration,
            spec,
            governance_policy.governance_policy_id,
            ResearchPhase.EXPLORATORY,
            sample_universe(train_frozenset),
            outcome_by_date,
            market_scope,
            created_at,
        )
        if exploratory_trial is not None:
            trials_recorded += 1
        validation_trial = self._trial(
            calibration,
            spec,
            governance_policy.governance_policy_id,
            ResearchPhase.VALIDATION,
            sample_universe(frozenset(self._timeline.validation_dates)),
            outcome_by_date,
            market_scope,
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
                    actor="research_calibration_v1",
                    purpose="LOCKED_HOLDOUT_TRIAL_EVALUATION_FOR_RESEARCH_SIGNAL_CALIBRATION_V1",
                    consumed_trial_id=None,
                )
            )
            holdout_trial = self._trial(
                calibration,
                spec,
                governance_policy.governance_policy_id,
                ResearchPhase.LOCKED_HOLDOUT,
                sample_universe(frozenset(self._timeline.holdout_dates)),
                outcome_by_date,
                market_scope,
                created_at,
            )
            if holdout_trial is None:
                blockers.append("HOLDOUT_SAMPLE_INSUFFICIENT")
            else:
                trials_recorded += 1

        base_trial = holdout_trial or validation_trial
        robustness_run = None
        if base_trial is not None and base_trial.diagnostics.base.mean is not None:
            robustness_run = self._robustness(
                sample_universe, outcome_by_date, base_trial, regime_by_date, created_at
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
                point_in_time_limitations=CALIBRATION_LIMITATIONS,
                rationale=spec.rationale,
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
        return NullControlRunResult(
            governance_policy_id=governance_policy.governance_policy_id,
            exploratory_trial_id=exploratory_trial.trial_id if exploratory_trial else None,
            validation_trial_id=validation_trial.trial_id if validation_trial else None,
            holdout_trial_id=holdout_trial.trial_id if holdout_trial else None,
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
        spec: NullControlSpec,
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
                    "research_calibration_observation", (spec.spec_id, phase.value, d)
                ),
            )
            for d in sample_sorted
        )
        grouped_pairs = tuple((phase.value, value) for value in sample_values)
        configuration = ResearchConfiguration.create(
            method_id=ResearchMethodId.RESEARCH_SIGNAL_CALIBRATION_NULL_CONTROL_V1,
            method_version="V1",
            parameter_values=(
                ("spec_id", spec.spec_id),
                ("family", spec.family.value),
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
            raise CalibrationApplicationError("ROBUSTNESS_BASE_TRIAL_HAS_NO_MEAN")
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
                    _direction_consistent(base_mean, mean),
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
                    _direction_consistent(base_mean, mean),
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
                    _direction_consistent(base_mean, mean),
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
                _direction_consistent(base_mean, mean),
                "OK" if mean is not None else "INSUFFICIENT_DATA",
            )
        )
        results.append(robustness_cost_stress())
        return RobustnessCheckRun.create(
            base_trial_id=base_trial.trial_id, results=tuple(results), created_at=created_at
        )


# ==========================================================================
# Section 5: calibration run registration and advancement (mirrors
# ``application.cross_market_research.RegisterAgendaRun``/``AdvanceAgendaRun``)
# ==========================================================================


class RegisterCalibrationRun:
    def __init__(self, *, repository: CalibrationRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        specs: Sequence[NullControlSpec],
        created_at: datetime,
    ) -> CalibrationRun:
        draft = CalibrationRun.draft(
            calibration=calibration,
            spec_ids=tuple(item.spec_id for item in specs),
            created_at=created_at,
        )
        existing = self._repository.run_by_calibration(calibration.calibration_id)
        if existing is not None:
            if existing.canonical_input_hash != draft.canonical_input_hash:
                raise CalibrationConflict("CALIBRATION_RUN_IDENTITY_CONFLICT")
            return existing
        return self._repository.save_run(draft)


class AdvanceCalibrationRun:
    """The one "start"/"resume" engine for a calibration run, exactly
    mirroring ``AdvanceAgendaRun``: process up to ``maximum_specs`` pending
    controls from the run's own cursor; once the cursor reaches the end,
    automatically finalize in the same call, so resuming a completed run
    is a safe no-op."""

    def __init__(
        self,
        *,
        repository: CalibrationRepository,
        methodology_repository: ResearchMethodologyRepository,
        timeline: AgendaTimeline,
    ) -> None:
        self._repository = repository
        self._methodology_repository = methodology_repository
        self._runner = RunNullControl(
            methodology_repository=methodology_repository, timeline=timeline
        )
        self._finalizer = FinalizeCalibration(
            repository=repository, methodology_repository=methodology_repository, timeline=timeline
        )

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        panel: CrossMarketPanel,
        maximum_specs: int,
        created_at: datetime,
    ) -> CalibrationRun:
        if maximum_specs < 1:
            raise CalibrationApplicationError("CALIBRATION_RUN_MAXIMUM_SPECS_INVALID")
        run = self._repository.run_by_calibration(calibration.calibration_id)
        if run is None:
            raise CalibrationNotFound("CALIBRATION_RUN_NOT_REGISTERED")
        if run.status is CalibrationRunStatus.COMPLETE:
            return run
        if run.status is CalibrationRunStatus.FAILED:
            raise CalibrationApplicationError("CALIBRATION_RUN_FAILED_CANNOT_ADVANCE")
        starting_cursor = run.cursor_ordinal
        pending = run.spec_ids[starting_cursor:]
        try:
            for offset, spec_id in enumerate(pending[:maximum_specs]):
                spec = self._repository.spec_by_id(spec_id)
                if spec is None:
                    raise CalibrationNotFound(f"NULL_CONTROL_SPEC_NOT_FOUND:{spec_id}")
                existing_outcome = self._repository.outcome_by_spec(spec_id)
                if existing_outcome is None:
                    result = self._runner.execute(
                        calibration=calibration, spec=spec, panel=panel, created_at=created_at
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
                    trials_added = result.trials_recorded
                else:
                    trials_added = 0
                is_final = (starting_cursor + offset + 1) == len(run.spec_ids)
                run = run.advance(
                    spec_id=spec_id, trials_added=trials_added, is_final=is_final, at=created_at
                )
                run = self._repository.save_run(run)
        except CalibrationApplicationError as error:
            run = self._repository.save_run(run.fail(reason=str(error), at=created_at))
            raise
        if run.status is CalibrationRunStatus.COMPLETE:
            self._finalizer.execute(
                calibration=calibration, run=run, panel=panel, created_at=created_at
            )
        return run


# ==========================================================================
# Section 6: FinalizeCalibration -- BH-FDR/Bonferroni correction, empirical
# false-discovery rates, family/horizon/time-period/volatility-regime
# breakdowns, and statistical-method comparison (sections 4/11/12/16/17/18).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class _ControlRow:
    spec: NullControlSpec
    outcome: NullControlOutcome
    validation_trial: ResearchTrial | None
    holdout_trial: ResearchTrial | None
    robustness_run: RobustnessCheckRun | None
    promotion_eligible: bool


def _validation_p_value(trial: ResearchTrial) -> Decimal | None:
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


def _summarize_rows(
    rows: Sequence[_ControlRow], bh_by_trial: dict[str, CorrectedTestResult]
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


def _time_period_bucket(*, date: str, all_dates: tuple[str, ...]) -> str:
    if not all_dates:
        return "UNKNOWN"
    try:
        index = all_dates.index(date)
    except ValueError:
        return "UNKNOWN"
    third = max(1, len(all_dates) // 3)
    if index < third:
        return "EARLY"
    if index < 2 * third:
        return "MIDDLE"
    return "LATE"


def _adequacy_note(*, dimension: str, count: int) -> str:
    if count >= MINIMUM_ADEQUATE_CONTROL_COUNT:
        return f"Adequate control count for {dimension}-specific calibration."
    token = dimension.upper().replace("-", "_")
    return f"INSUFFICIENT_CONTROL_COUNT_FOR_RELIABLE_{token}_CALIBRATION"


def _volatility_regime_bucket(*, date: str, panel: CrossMarketPanel) -> str:
    regime = panel.regime_by_date().get(date)
    if regime is None:
        return "UNKNOWN"
    try:
        return regime.label_for(CrossMarketRegimeDimension.EQUITY_VOLATILITY)
    except CrossMarketResearchError:
        return "UNKNOWN"


class FinalizeCalibration:
    def __init__(
        self,
        *,
        repository: CalibrationRepository,
        methodology_repository: ResearchMethodologyRepository,
        timeline: AgendaTimeline,
    ) -> None:
        self._repository = repository
        self._methodology_repository = methodology_repository
        self._timeline = timeline

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        run: CalibrationRun,
        panel: CrossMarketPanel,
        created_at: datetime,
    ) -> CalibrationReport:
        existing = self._repository.report_by_calibration(calibration.calibration_id)
        if existing is not None:
            return existing
        specs = {
            item.spec_id: item
            for item in self._repository.specs_for_calibration(calibration.calibration_id)
        }
        outcomes = self._repository.outcomes_for_run(run.calibration_run_id)
        rows: list[_ControlRow] = []
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
            rows.append(
                _ControlRow(
                    spec=spec,
                    outcome=outcome,
                    validation_trial=validation_trial,
                    holdout_trial=holdout_trial,
                    robustness_run=robustness_run,
                    promotion_eligible=bool(promotion is not None and promotion.eligible),
                )
            )

        raw_results = tuple(
            TrialTestResult(
                row.validation_trial.trial_id,
                row.spec.spec_id,
                _validation_p_value(row.validation_trial),
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

        overall_summary = _summarize_rows(rows, bh_by_trial)

        family_breakdowns = []
        for family in NullControlFamily:
            members = [r for r in rows if r.spec.family is family]
            summary = _summarize_rows(members, bh_by_trial)
            family_breakdowns.append(
                self._repository.save_breakdown(
                    CalibrationBreakdown.create(
                        calibration_id=calibration.calibration_id,
                        dimension_kind="FAMILY",
                        dimension_label=family.value,
                        control_count=len(members),
                        summary=summary,
                        note=_adequacy_note(dimension="family", count=len(members)),
                        created_at=created_at,
                    )
                )
            )

        design_breakdowns = []
        for design_family in NullControlDesignFamily:
            members = [r for r in rows if r.spec.design_family is design_family]
            summary = _summarize_rows(members, bh_by_trial)
            design_breakdowns.append(
                self._repository.save_breakdown(
                    CalibrationBreakdown.create(
                        calibration_id=calibration.calibration_id,
                        dimension_kind="DESIGN_FAMILY",
                        dimension_label=design_family.value,
                        control_count=len(members),
                        summary=summary,
                        note=_adequacy_note(dimension="design-family", count=len(members)),
                        created_at=created_at,
                    )
                )
            )

        horizon_breakdowns = []
        for horizon in calibration.allowed_horizons_sessions:
            members = [r for r in rows if r.spec.horizon_sessions == horizon]
            summary = _summarize_rows(members, bh_by_trial)
            horizon_breakdowns.append(
                self._repository.save_breakdown(
                    CalibrationBreakdown.create(
                        calibration_id=calibration.calibration_id,
                        dimension_kind="HORIZON",
                        dimension_label=f"{horizon}_SESSIONS",
                        control_count=len(members),
                        summary=summary,
                        note=(
                            f"Overlap policy: outcome windows at horizon {horizon} overlap by "
                            "construction; effective sample uses the same conservative "
                            "horizon-stride adjustment every genuine-agenda trial uses."
                        ),
                        created_at=created_at,
                    )
                )
            )

        time_buckets: dict[str, list[_ControlRow]] = {
            "EARLY": [],
            "MIDDLE": [],
            "LATE": [],
            "UNKNOWN": [],
        }
        vol_buckets: dict[str, list[_ControlRow]] = {}
        for row in rows:
            if row.validation_trial is None:
                time_buckets["UNKNOWN"].append(row)
                continue
            anchor_date = row.validation_trial.session_date_range[0]
            time_buckets.setdefault(
                _time_period_bucket(date=anchor_date, all_dates=panel.session_dates), []
            ).append(row)
            vol_buckets.setdefault(
                _volatility_regime_bucket(date=anchor_date, panel=panel), []
            ).append(row)

        time_period_breakdowns = []
        for label in ("EARLY", "MIDDLE", "LATE"):
            members = time_buckets.get(label, [])
            summary = _summarize_rows(members, bh_by_trial)
            time_period_breakdowns.append(
                self._repository.save_breakdown(
                    CalibrationBreakdown.create(
                        calibration_id=calibration.calibration_id,
                        dimension_kind="TIME_PERIOD",
                        dimension_label=label,
                        control_count=len(members),
                        summary=summary,
                        note=_adequacy_note(dimension="time-period", count=len(members)),
                        created_at=created_at,
                    )
                )
            )

        volatility_regime_breakdowns = []
        for label, members in sorted(vol_buckets.items()):
            summary = _summarize_rows(members, bh_by_trial)
            volatility_regime_breakdowns.append(
                self._repository.save_breakdown(
                    CalibrationBreakdown.create(
                        calibration_id=calibration.calibration_id,
                        dimension_kind="VOLATILITY_REGIME",
                        dimension_label=label,
                        control_count=len(members),
                        summary=summary,
                        note=_adequacy_note(dimension="volatility-regime", count=len(members)),
                        created_at=created_at,
                    )
                )
            )

        method_comparisons = self._method_comparisons(
            calibration, rows, raw_results, bh_by_trial, bonferroni_by_trial, panel, created_at
        )

        limitations: tuple[str, ...] = CALIBRATION_LIMITATIONS
        if any(
            not item.sample_adequate
            for item in (
                *family_breakdowns,
                *design_breakdowns,
                *time_period_breakdowns,
                *volatility_regime_breakdowns,
            )
        ):
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
            time_period_breakdown_ids=tuple(item.breakdown_id for item in time_period_breakdowns),
            volatility_regime_breakdown_ids=tuple(
                item.breakdown_id for item in volatility_regime_breakdowns
            ),
            method_comparison_ids=tuple(item.comparison_id for item in method_comparisons),
            effect_size_floor_policy_id=EFFECT_SIZE_FLOOR_POLICY_V1.policy_id,
            evidence_grade_policy_id=EVIDENCE_GRADE_POLICY_V1.policy_id,
            recommended_evidence_policy_notes=_recommendation_notes(overall_summary),
            prior_agenda_hypotheses_recalibrated=0,
            justified_follow_up_count=0,
            limitations=limitations,
            created_at=created_at,
        )
        return self._repository.save_report(report)

    def _method_comparisons(
        self,
        calibration: CalibrationPlan,
        rows: Sequence[_ControlRow],
        raw_results: Sequence[TrialTestResult],
        bh_by_trial: dict[str, CorrectedTestResult],
        bonferroni_by_trial: dict[str, CorrectedTestResult],
        panel: CrossMarketPanel,
        created_at: datetime,
    ) -> list[StatisticalMethodComparison]:
        control_count = sum(1 for r in rows if r.validation_trial is not None)
        normal_survivors = sum(
            1
            for item in raw_results
            if item.p_value is not None and item.p_value <= Decimal("0.05")
        )
        bh_survivors = sum(1 for item in bh_by_trial.values() if item.rejected_null)
        bonferroni_survivors = sum(1 for item in bonferroni_by_trial.values() if item.rejected_null)

        hac_survivors = 0
        bootstrap_survivors = 0
        for row in rows:
            if row.validation_trial is None or row.validation_trial.benchmark is None:
                continue
            sample_values = self._reconstruct_validation_sample(row.spec, panel)
            if sample_values is None or len(sample_values) < 8:
                continue
            benchmark_mean = row.validation_trial.benchmark.benchmark_mean
            mean = row.validation_trial.diagnostics.base.mean
            if mean is None:
                continue
            hac_se = newey_west_standard_error(sample_values, lag=HAC_LAG_SESSIONS)
            if (
                hac_se is not None
                and hac_se.value > 0
                and abs(mean - benchmark_mean) > Z_CRITICAL_95 * hac_se.value
            ):
                hac_survivors += 1
            bootstrap_ci = seeded_bootstrap_confidence_interval(
                sample_values, seed=STATISTICAL_METHOD_BOOTSTRAP_SEED
            )
            if bootstrap_ci is not None and not (
                bootstrap_ci.lower <= benchmark_mean <= bootstrap_ci.upper
            ):
                bootstrap_survivors += 1

        raw = [
            (
                "NORMAL_APPROXIMATION_UNCORRECTED_V1",
                normal_survivors,
                "Two-sided z-test, IID standard error, alpha=0.05, no multiple-testing "
                "correction -- the most permissive method compared here.",
            ),
            (
                "HAC_NEWEY_WEST_UNCORRECTED_V1",
                hac_survivors,
                "Heteroskedasticity/autocorrelation-consistent standard error, Bartlett "
                f"kernel, lag={HAC_LAG_SESSIONS}, 95% CI excludes benchmark mean, no correction.",
            ),
            (
                "SEEDED_BOOTSTRAP_UNCORRECTED_V1",
                bootstrap_survivors,
                "Deterministic 500-resample percentile bootstrap, 95% CI excludes "
                "benchmark mean, no correction.",
            ),
            (
                "BENJAMINI_HOCHBERG_FDR_V1",
                bh_survivors,
                "Benjamini-Hochberg step-up procedure, false discovery rate=0.10, "
                "applied across the whole battery.",
            ),
            (
                "BONFERRONI_V1",
                bonferroni_survivors,
                "Bonferroni family-wise correction, alpha=0.05, applied across the whole "
                "battery -- the most conservative method compared here.",
            ),
        ]
        ranked = sorted(raw, key=lambda item: item[1])
        comparisons = []
        for rank, (method_id, survivors, notes) in enumerate(ranked, start=1):
            comparisons.append(
                self._repository.save_method_comparison(
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
        return comparisons

    def _reconstruct_validation_sample(
        self, spec: NullControlSpec, panel: CrossMarketPanel
    ) -> tuple[Decimal, ...] | None:
        """Deterministically reconstructs the exact VALIDATION-phase sample
        ``RunNullControl`` evaluated, purely from already-retained ``spec``/
        ``panel``/``self._timeline`` -- never a new provider request -- so
        HAC/bootstrap can be compared without persisting raw sample values
        on every trial. Reuses ``derive_null_series``/``median_split_dates``
        unmodified, so the reconstructed sample is byte-identical to the
        one the original trial actually evaluated."""

        null_series = derive_null_series(spec=spec, panel=panel)
        feature_by_date = {
            item.session_date: item.value for item in null_series if item.value is not None
        }
        outcome_by_date = panel.outcome_by_date(
            OutcomeId(spec.outcome_id), horizon_sessions=spec.horizon_sessions
        )
        if not feature_by_date or not outcome_by_date:
            return None
        primary_dates = median_split_dates(
            feature_by_date=feature_by_date, train_dates=frozenset(self._timeline.train_dates)
        )
        sample_dates = sorted(
            (frozenset(self._timeline.validation_dates) & primary_dates) & outcome_by_date.keys()
        )
        if not sample_dates:
            return None
        return tuple(outcome_by_date[d] for d in sample_dates)


def _recommendation_notes(summary: CalibrationRateSummary) -> str:
    return (
        f"Empirical corrected false-positive rate across {summary.validation_candidates} "
        f"validation-eligible controls: {summary.corrected_false_positive_rate}. "
        f"False-WEAK rate across all {summary.total_controls} controls: {summary.false_weak_rate}. "
        f"False-SUPPORTED rate: {summary.false_supported_rate}. False-promotion rate: "
        f"{summary.false_promotion_rate}. Current WEAK_SUPPORT/SUGGESTIVE-level conclusions "
        "should not be treated as distinguishable from null behavior without a "
        "null-calibration percentile at or above the declared EvidenceGradePolicy suggestive "
        "floor; see EVIDENCE_GRADE_POLICY_V1."
    )


# ==========================================================================
# Section 7: prior-agenda recalibration (section 10) and follow-up
# priority (section 14) -- strictly read-only over the genuine agenda's
# already-retained evidence; never mutates a ``ResearchConclusion`` or
# ``ResearchKnowledgeEntry``, and never reruns or widens that agenda.
# ==========================================================================

_OUTCOME_FAMILY_BY_OUTCOME_ID: dict[str, OutcomeFamily] = {
    "EQUITY_FORWARD_RETURN": OutcomeFamily.EQUITY_BROAD_RETURN,
    "EQUITY_FORWARD_VOLATILITY": OutcomeFamily.EQUITY_BROAD_VOLATILITY,
    "EQUITY_FORWARD_DISPERSION": OutcomeFamily.EQUITY_BROAD_DISPERSION,
    "RATES_PROXY_FORWARD_RETURN": OutcomeFamily.RATES_PROXY_RETURN,
    "BTC_FORWARD_RETURN": OutcomeFamily.BTC_RETURN,
    "BTC_FORWARD_VOLATILITY": OutcomeFamily.BTC_VOLATILITY,
    # ``PERSISTENCE_INDICATOR`` intentionally has no mapped ``OutcomeFamily``
    # -- it is not one of the six genuine forward-market outcomes (see
    # ``domain.cross_market_research.OutcomeId``'s own docstring), so a
    # hypothesis using it conservatively never meets the effect-size floor.
}


def _outcome_family_for(outcome_id: OutcomeId) -> OutcomeFamily | None:
    return _OUTCOME_FAMILY_BY_OUTCOME_ID.get(outcome_id.value)


def _classify_follow_up_priority(
    *, original_status: KnowledgeStatus, calibrated_grade: EvidenceGrade
) -> FollowUpPriorityClass:
    """Section 14's ranking rule, derived entirely from the calibrated
    evidence grade (never from intuition or from the original, uncalibrated
    status alone): a grade the calibration cannot distinguish from ordinary
    null behavior (``EXPLORATORY``) is unsupported after calibration; a
    genuinely failed/unreliable grade is rejected outright; ``SUGGESTIVE``
    is worth continued investigation only when the original agenda already
    judged it worth continuing (``ACTIVE_RESEARCH``); ``VALIDATED``/
    ``STRONG`` are always justified."""

    if original_status is KnowledgeStatus.REJECTED or calibrated_grade is EvidenceGrade.UNRELIABLE:
        return FollowUpPriorityClass.REJECTED
    if calibrated_grade is EvidenceGrade.EXPLORATORY:
        return FollowUpPriorityClass.UNSUPPORTED_AFTER_CALIBRATION
    if calibrated_grade is EvidenceGrade.SUGGESTIVE:
        return (
            FollowUpPriorityClass.JUSTIFIED
            if original_status is KnowledgeStatus.ACTIVE_RESEARCH
            else FollowUpPriorityClass.LOW_PRIORITY
        )
    return FollowUpPriorityClass.JUSTIFIED


def _walk_forward_consistency(
    *,
    walk_forward_trial_ids: Sequence[str],
    reference_mean: Decimal | None,
    methodology_repository: ResearchMethodologyRepository,
) -> Decimal | None:
    if reference_mean is None or reference_mean == 0 or not walk_forward_trial_ids:
        return None
    consistent = 0
    evaluable = 0
    for trial_id in walk_forward_trial_ids:
        trial = methodology_repository.trial_by_id(trial_id)
        if trial is None or trial.diagnostics.base.mean is None:
            continue
        evaluable += 1
        if (trial.diagnostics.base.mean > 0) == (reference_mean > 0):
            consistent += 1
    if evaluable == 0:
        return None
    return bound_statistical_decimal(Decimal(consistent) / Decimal(evaluable))


def _regime_concentration_from_robustness(robustness_run: RobustnessCheckRun | None) -> str:
    if robustness_run is None:
        return "INSUFFICIENT_EVIDENCE"
    regime_results = [
        item
        for item in robustness_run.summary.results
        if item.check_type is RobustnessCheckType.REGIME_SPLIT
    ]
    evaluable = [item for item in regime_results if item.result_mean is not None]
    if not evaluable:
        return "INSUFFICIENT_EVIDENCE"
    if len(evaluable) == 1:
        return f"CONCENTRATED_IN_{evaluable[0].variant_label}"
    return "DISTRIBUTED_ACROSS_REGIMES"


class RecalibratePriorAgenda:
    """Section 10: applies the calibrated evidence-grade policy as a new,
    derived assessment of the genuine, already-retained 25-hypothesis
    agenda -- reads only through the existing, unmodified
    ``CrossMarketResearchRepository``/``ResearchMethodologyRepository``
    protocols, never reruns a trial, never widens the agenda's search
    budget, and never saves anything back to a historical
    ``ResearchConclusion``/``ResearchKnowledgeEntry``."""

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
        source_agenda_id: str,
        null_cohens_d: Sequence[Decimal],
        effect_size_floor_policy: EffectSizeFloorPolicy = EFFECT_SIZE_FLOOR_POLICY_V1,
        evidence_grade_policy: EvidenceGradePolicy = EVIDENCE_GRADE_POLICY_V1,
        created_at: datetime,
    ) -> tuple[PriorHypothesisRecalibration, ...]:
        hypotheses = self._cross_market_repository.hypotheses_for_agenda(source_agenda_id)
        results: list[PriorHypothesisRecalibration] = []
        for hypothesis in hypotheses:
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
            execution = self._cross_market_repository.execution_by_hypothesis(
                hypothesis.hypothesis_id
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
            wf_consistency = (
                _walk_forward_consistency(
                    walk_forward_trial_ids=execution.walk_forward_trial_ids,
                    reference_mean=(
                        reference_trial.diagnostics.base.mean if reference_trial else None
                    ),
                    methodology_repository=self._methodology_repository,
                )
                if execution is not None
                else None
            )
            null_percentile = (
                null_calibration_percentile(statistic=cohens_d, null_statistics=null_cohens_d)
                if cohens_d is not None
                else None
            )
            outcome_family = _outcome_family_for(hypothesis.outcome_id)
            meets_floor = outcome_family is not None and effect_meets_floor(
                outcome_family=outcome_family, cohens_d=cohens_d, policy=effect_size_floor_policy
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
                walk_forward_consistency=wf_consistency,
                robustness_passed=robustness_passed,
                corrected_significant=corrected_significant,
                effective_sample=effective_sample,
                regime_concentration=_regime_concentration_from_robustness(robustness_run),
                null_percentile=null_percentile,
                practical_significance=practical,
                created_at=created_at,
            )
            grade = derive_evidence_grade(
                conclusion_state=conclusion.conclusion_state,
                profile=profile,
                effect_meets_floor=meets_floor,
                policy=evidence_grade_policy,
            )
            priority = _classify_follow_up_priority(
                original_status=entry.status, calibrated_grade=grade
            )
            verdict = (
                assess_negative_control_quality(
                    null_percentile=null_percentile,
                    robustness_passed=robustness_passed,
                    effective_sample=effective_sample,
                    minimum_effective_sample=self._minimum_effective_sample,
                    policy=evidence_grade_policy,
                )
                if hypothesis.is_negative_control
                else None
            )
            rationale = (
                f"Original conclusion {conclusion.conclusion_state.value} (retained knowledge "
                f"status {entry.status.value}, unchanged); calibrated evidence grade {grade.value} "
                f"(null percentile {null_percentile}); reference cohen's d {cohens_d}; "
                f"benchmark-relative effect {benchmark_relative}; effective sample "
                f"{effective_sample}; practical significance {practical.value}."
            )
            results.append(
                PriorHypothesisRecalibration.create(
                    calibration_id=calibration.calibration_id,
                    hypothesis_id=hypothesis.hypothesis_id,
                    original_conclusion_id=conclusion.conclusion_id,
                    original_conclusion_state=conclusion.conclusion_state.value,
                    original_knowledge_status=entry.status.value,
                    calibrated_evidence_grade=grade,
                    null_percentile=null_percentile,
                    still_promotion_ineligible=not evidence_grade_at_least(
                        grade, CALIBRATED_PROMOTION_POLICY.minimum_evidence_grade
                    ),
                    deserves_follow_up=priority
                    in (FollowUpPriorityClass.JUSTIFIED, FollowUpPriorityClass.LOW_PRIORITY),
                    follow_up_priority=priority,
                    negative_control_verdict=verdict,
                    rationale=rationale,
                    created_at=created_at,
                )
            )
        return tuple(results)

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


class AssessFollowUpPriority:
    """Section 14: classifies every already-retained
    ``domain.cross_market_research.FollowUpProposal`` by its parent
    hypothesis's calibrated recalibration -- never itself authorizes
    running the follow-up, and never generates a new proposal."""

    def __init__(self, *, cross_market_repository: CrossMarketResearchRepository) -> None:
        self._cross_market_repository = cross_market_repository

    def execute(
        self,
        *,
        calibration: CalibrationPlan,
        recalibrations: Sequence[PriorHypothesisRecalibration],
        created_at: datetime,
    ) -> tuple[FollowUpPriorityAssessment, ...]:
        follow_ups: tuple[FollowUpProposal, ...] = self._cross_market_repository.list_follow_ups()
        by_hypothesis = {item.hypothesis_id: item for item in recalibrations}
        results = []
        for follow_up in follow_ups:
            parent = by_hypothesis.get(follow_up.parent_hypothesis_id)
            classification = (
                parent.follow_up_priority
                if parent is not None
                else FollowUpPriorityClass.UNSUPPORTED_AFTER_CALIBRATION
            )
            rationale = (
                f"Parent hypothesis {follow_up.parent_hypothesis_id} calibrated evidence grade "
                f"{parent.calibrated_evidence_grade.value if parent is not None else 'UNKNOWN'}; "
                f"proposed follow-up kind {follow_up.kind.value} targeting "
                f"{follow_up.proposed_template_id.value}."
            )
            results.append(
                FollowUpPriorityAssessment.create(
                    calibration_id=calibration.calibration_id,
                    follow_up_id=follow_up.follow_up_id,
                    parent_hypothesis_id=follow_up.parent_hypothesis_id,
                    classification=classification,
                    rationale=rationale,
                    created_at=created_at,
                )
            )
        return tuple(results)


# ==========================================================================
# Section 8: promotion-policy calibration (section 9) -- strictly additive
# on top of the existing, unmodified ``EvaluatePromotion``.
# ==========================================================================


class EvaluateCalibratedPromotion:
    """Layers the calibrated evidence-grade requirement on top of the
    existing, unmodified production promotion policy. Always calls
    ``EvaluatePromotion`` first (against
    ``APEX_PRODUCTION_PROMOTION_POLICY``, entirely unmodified) and retains
    its result verbatim on the returned record -- never replaces it."""

    def __init__(self, *, methodology_repository: ResearchMethodologyRepository) -> None:
        self._methodology_repository = methodology_repository

    def execute(
        self,
        *,
        conclusion_id: str,
        calibration_id: str,
        evidence_grade: EvidenceGrade,
        policy: CalibratedPromotionPolicy = CALIBRATED_PROMOTION_POLICY,
        created_at: datetime,
    ) -> CalibratedPromotionEvaluation:
        base = EvaluatePromotion(repository=self._methodology_repository).execute(
            EvaluatePromotionCommand(
                conclusion_id=conclusion_id,
                policy=APEX_PRODUCTION_PROMOTION_POLICY,
                lookahead_violation=False,
                created_at=created_at,
            )
        )
        return CalibratedPromotionEvaluation.create(
            promotion_evaluation_id=base.promotion_evaluation_id,
            calibration_id=calibration_id,
            base_eligible=base.eligible,
            base_reasons=base.reasons,
            evidence_grade=evidence_grade,
            policy=policy,
            created_at=created_at,
        )


# ==========================================================================
# In-memory repository (fast application-layer tests; no persistence
# concern lives here)
# ==========================================================================


class InMemoryCalibrationRepository:
    def __init__(self) -> None:
        self._plans: dict[str, CalibrationPlan] = {}
        self._specs: dict[str, NullControlSpec] = {}
        self._runs: dict[str, CalibrationRun] = {}
        self._outcomes: dict[str, NullControlOutcome] = {}
        self._breakdowns: dict[str, CalibrationBreakdown] = {}
        self._method_comparisons: dict[str, StatisticalMethodComparison] = {}
        self._reports: dict[str, CalibrationReport] = {}
        self._recalibrations: dict[str, PriorHypothesisRecalibration] = {}
        self._follow_up_priorities: dict[str, FollowUpPriorityAssessment] = {}
        self._calibrated_promotions: dict[str, CalibratedPromotionEvaluation] = {}

    def plan_by_id(self, calibration_id: str) -> CalibrationPlan | None:
        return self._plans.get(calibration_id)

    def save_plan(self, plan: CalibrationPlan) -> CalibrationPlan:
        self._plans.setdefault(plan.calibration_id, plan)
        return self._plans[plan.calibration_id]

    def list_plans(self) -> tuple[CalibrationPlan, ...]:
        return tuple(sorted(self._plans.values(), key=lambda item: item.registered_at))

    def spec_by_id(self, spec_id: str) -> NullControlSpec | None:
        return self._specs.get(spec_id)

    def save_spec(self, spec: NullControlSpec) -> NullControlSpec:
        self._specs.setdefault(spec.spec_id, spec)
        return self._specs[spec.spec_id]

    def specs_for_calibration(self, calibration_id: str) -> tuple[NullControlSpec, ...]:
        return tuple(
            sorted(
                (item for item in self._specs.values() if item.calibration_id == calibration_id),
                key=lambda item: item.ordinal,
            )
        )

    def run_by_calibration(self, calibration_id: str) -> CalibrationRun | None:
        for run in self._runs.values():
            if run.calibration_id == calibration_id:
                return run
        return None

    def run_by_id(self, calibration_run_id: str) -> CalibrationRun | None:
        return self._runs.get(calibration_run_id)

    def save_run(self, run: CalibrationRun) -> CalibrationRun:
        self._runs[run.calibration_run_id] = run
        return run

    def outcome_by_spec(self, spec_id: str) -> NullControlOutcome | None:
        for outcome in self._outcomes.values():
            if outcome.spec_id == spec_id:
                return outcome
        return None

    def save_outcome(self, outcome: NullControlOutcome) -> NullControlOutcome:
        self._outcomes[outcome.outcome_id] = outcome
        return outcome

    def outcomes_for_run(self, calibration_run_id: str) -> tuple[NullControlOutcome, ...]:
        return tuple(
            item
            for item in self._outcomes.values()
            if item.calibration_run_id == calibration_run_id
        )

    def save_breakdown(self, breakdown: CalibrationBreakdown) -> CalibrationBreakdown:
        self._breakdowns[breakdown.breakdown_id] = breakdown
        return breakdown

    def breakdowns_for_calibration(self, calibration_id: str) -> tuple[CalibrationBreakdown, ...]:
        return tuple(
            item for item in self._breakdowns.values() if item.calibration_id == calibration_id
        )

    def save_method_comparison(
        self, comparison: StatisticalMethodComparison
    ) -> StatisticalMethodComparison:
        self._method_comparisons[comparison.comparison_id] = comparison
        return comparison

    def method_comparisons_for_calibration(
        self, calibration_id: str
    ) -> tuple[StatisticalMethodComparison, ...]:
        return tuple(
            item
            for item in self._method_comparisons.values()
            if item.calibration_id == calibration_id
        )

    def report_by_calibration(self, calibration_id: str) -> CalibrationReport | None:
        return self._reports.get(calibration_id)

    def save_report(self, report: CalibrationReport) -> CalibrationReport:
        self._reports[report.calibration_id] = report
        return report

    def save_recalibration(
        self, item: PriorHypothesisRecalibration
    ) -> PriorHypothesisRecalibration:
        self._recalibrations[item.recalibration_id] = item
        return item

    def recalibrations_for_calibration(
        self, calibration_id: str
    ) -> tuple[PriorHypothesisRecalibration, ...]:
        return tuple(
            item for item in self._recalibrations.values() if item.calibration_id == calibration_id
        )

    def save_follow_up_priority(
        self, item: FollowUpPriorityAssessment
    ) -> FollowUpPriorityAssessment:
        self._follow_up_priorities[item.assessment_id] = item
        return item

    def follow_up_priorities_for_calibration(
        self, calibration_id: str
    ) -> tuple[FollowUpPriorityAssessment, ...]:
        return tuple(
            item
            for item in self._follow_up_priorities.values()
            if item.calibration_id == calibration_id
        )

    def save_calibrated_promotion(
        self, item: CalibratedPromotionEvaluation
    ) -> CalibratedPromotionEvaluation:
        self._calibrated_promotions[item.calibrated_promotion_evaluation_id] = item
        return item

    def calibrated_promotions_for_calibration(
        self, calibration_id: str
    ) -> tuple[CalibratedPromotionEvaluation, ...]:
        return tuple(
            item
            for item in self._calibrated_promotions.values()
            if item.calibration_id == calibration_id
        )
