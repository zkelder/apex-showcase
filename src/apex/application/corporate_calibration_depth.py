"""Adapter-free orchestration for Corporate Research Calibration Depth v1.

Composes, entirely unmodified: ``application.corporate_market_research``'s
``CorporatePanel``/``BuildCorporatePanel``/``HypothesisSample``/``build_
corporate_diagnostics``/``compute_robustness``/``compute_stability``/
``trial_p_value``/``corporate_matched_stratum_key``/``classify_hypothesis_
calibration``/``build_trial_families``/``reconstruct_hypothesis_outcome``/
``split_train_validation_holdout`` for every already-genuine corporate
panel/trial/classification primitive; ``domain.corporate_calibration_
depth`` for every genuinely new calibration-depth type; ``domain.research_
calibration_v2``'s ``ResearchDesign``/``ConditioningShape``/``Matched
CalibrationResult``/``StabilitySlice``/``FollowUpQualification``/``classify_
follow_up_v2`` unmodified; ``domain.research_calibration``'s ``EvidenceGrade``/
``derive_evidence_grade``/``EvidenceProfile``/``CalibrationRateSummary``/
``NullGenerationMethod``/``synthetic_ar1_surrogate`` unmodified;
``domain.research_methodology`` for the entire governed trial lifecycle.

Why this module builds its own small local numeric/sample-building helpers
(``_feature_value``, ``_outcome_value``, ``_train_median_threshold``, the
ten ``CorporateNullMechanism`` sample builders, ...) instead of importing
``application.corporate_market_research``'s own private (underscore-
prefixed) equivalents: this codebase's own convention, observed
consistently across every "vN+1 builds on vN" module in this research
stack, is that cross-module reuse only ever imports *public* names: a
private helper stays private to its own module, and a dependent module
that needs similar-but-differently-parameterized mechanics writes its own
small, independently reviewable version instead. Every helper here is
short (a handful of lines) and the mechanisms they compose (temporal
displacement, issuer derangement, filing-event date matching, outcome
permutation) are the ones milestone sections 5-8 name explicitly.

No service here has Strategy, Opportunity, PAPER, LIVE, or provider-write
authority; every retained record stays ``authority = "NONE"``. Nothing here
executes any of the 13 retained v3 follow-up proposals it requalifies, and
nothing here reruns or widens the v3 agenda's own 20-genuine/17-control
search.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Protocol

from apex.application.corporate_market_research import (
    EMPTY_SAMPLE,
    CorporateMarketResearchRepository,
    CorporatePanel,
    HypothesisRunOutcome,
    HypothesisSample,
    ResearchMethodologyCollaborator,
    build_corporate_diagnostics,
    build_trial_families,
    classify_hypothesis_calibration,
    compute_incremental_contribution,
    compute_robustness,
    corporate_matched_stratum_key,
    reconstruct_hypothesis_outcome,
    trial_p_value,
)
from apex.application.cross_market_research import derive_knowledge_status
from apex.domain.corporate_calibration_depth import (
    CONDITIONAL_INTERACTION_MECHANISMS,
    CROSS_SECTIONAL_MECHANISMS,
    FILING_EVENT_MECHANISMS,
    MINIMUM_MATCHED_NULL_COUNT,
    CorporateCalibrationPlan,
    CorporateCalibrationReport,
    CorporateCalibrationRun,
    CorporateNullControlOutcome,
    CorporateNullControlSpec,
    CorporateNullMechanism,
    CorporateReassessment,
    CorporateReassessmentSubjectKind,
    CorporateStratumTarget,
    EffectiveSampleBucket,
    IncrementalCalibrationVerdict,
    WeakSupportCause,
    bucket_effective_sample,
    classify_weak_support_cause,
    corporate_calibration_depth_warnings,
    temporal_region_for_date,
)
from apex.domain.corporate_market_research import (
    CorporateFeatureId,
    CorporateOutcomeId,
    CorporatePanelRow,
    GeneratedCorporateHypothesis,
    IncrementalContributionResult,
)
from apex.domain.cross_market_research import (
    DatedValue,
    FollowUpProposal,
)
from apex.domain.historical_replay import (
    ResearchMethodId,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
)
from apex.domain.research_calibration import (
    EVIDENCE_GRADE_POLICY_V1,
    MINIMUM_ADEQUATE_CONTROL_COUNT,
    CalibrationRateSummary,
    CalibrationRunStatus,
    EvidenceGrade,
    EvidenceProfile,
    classify_practical_significance,
    derive_evidence_grade,
    null_calibration_percentile,
    synthetic_ar1_surrogate,
)
from apex.domain.research_calibration_v2 import (
    CALIBRATION_STRATUM_INSUFFICIENT,
    ConditioningShape,
    FollowUpQualification,
    FollowUpQualificationV2,
    MatchedCalibrationResult,
    ResearchDesign,
    StabilitySlice,
    classify_follow_up_v2,
)
from apex.domain.research_methodology import (
    BenchmarkKind,
    ConfigurationOrigin,
    GovernanceIntent,
    HoldoutExposureEvent,
    PromotionEvaluation,
    PromotionPolicy,
    ResearchConclusion,
    ResearchConclusionState,
    ResearchConfiguration,
    ResearchGovernancePolicy,
    ResearchPhase,
    ResearchTrial,
    RobustnessCheckRun,
    SampleQualityState,
    TrialTestResult,
    apply_benjamini_hochberg_correction,
    bound_statistical_decimal,
    compare_to_benchmark,
    derive_conclusion_state,
    guard_governed_holdout_access,
    resolve_minimum_sample_requirement,
)
from apex.domain.values import deterministic_id

CORPORATE_CALIBRATION_DEPTH_METHOD_ID = ResearchMethodId.CORPORATE_CALIBRATION_DEPTH_NULL_CONTROL_V1
CORPORATE_CALIBRATION_DEPTH_PROMOTION_POLICY = PromotionPolicy(
    policy_id="CORPORATE_CALIBRATION_DEPTH_V1_PROMOTION_POLICY", require_holdout_consumed=True
)
CONTROLS_PER_STRATUM = 24
CROSS_SECTIONAL_DRAWS_PER_MECHANISM = 6
FILING_EVENT_DRAWS_PER_MECHANISM = 8
CONDITIONAL_DRAWS_PER_DIMENSION_PER_MECHANISM = 4
FILING_EVENT_DATE_OFFSETS: tuple[int, ...] = (-40, -30, -20, -10, 10, 20, 30, 40)
BLOCK_PERMUTATION_BLOCK_LENGTH = 3

SEED_BASES: dict[CorporateNullMechanism, int] = {
    CorporateNullMechanism.SEEDED_TEMPORAL_DISPLACEMENT: 20260809401,
    CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT: 20260809402,
    CorporateNullMechanism.SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION: 20260809403,
    CorporateNullMechanism.SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION: 20260809404,
    CorporateNullMechanism.SEEDED_RANDOM_NON_EVENT_DATE_MATCHED: 20260809405,
    CorporateNullMechanism.SEEDED_DISPLACED_FILING_EVENT_DATE: 20260809406,
    CorporateNullMechanism.SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT: 20260809407,
    CorporateNullMechanism.SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION: 20260809408,
    CorporateNullMechanism.SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION: 20260809409,
    CorporateNullMechanism.SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION: 20260809410,
}
TEMPLATE_ID_BY_DESIGN: dict[ResearchDesign, str] = {
    ResearchDesign.CROSS_SECTIONAL: "CROSS_SECTIONAL_V1",
    ResearchDesign.CHANGE_BASED: "CORPORATE_CHANGE_V1",
    ResearchDesign.FILING_EVENT: "FILING_EVENT_V1",
    ResearchDesign.CONDITIONAL: "CONDITIONAL_RELATIONSHIP_V1",
    ResearchDesign.INTERACTION: "INTERACTION_V1",
}
PRESERVED_PROPERTIES: dict[CorporateNullMechanism, tuple[str, ...]] = {
    CorporateNullMechanism.SEEDED_TEMPORAL_DISPLACEMENT: (
        "MARKET_DATE_STRUCTURE",
        "ISSUER_UNIVERSE",
        "SECTOR_COMPOSITION",
        "OUTCOME_HORIZON",
        "FEATURE_MISSINGNESS_PATTERN",
    ),
    CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT: (
        "MARKET_DATE_STRUCTURE",
        "NUMBER_OF_ISSUERS",
        "OUTCOME_HORIZON",
        "FEATURE_DISTRIBUTION",
    ),
    CorporateNullMechanism.SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION: (
        "MARKET_DATE_STRUCTURE",
        "ISSUER_UNIVERSE",
        "FEATURE_MISSINGNESS_PATTERN",
        "OUTCOME_HORIZON",
    ),
    CorporateNullMechanism.SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION: (
        "PER_ISSUER_MEAN",
        "PER_ISSUER_VOLATILITY",
        "PER_ISSUER_AUTOCORRELATION",
        "FEATURE_MISSINGNESS_PATTERN",
        "MARKET_DATE_STRUCTURE",
    ),
    CorporateNullMechanism.SEEDED_RANDOM_NON_EVENT_DATE_MATCHED: (
        "EVENT_FREQUENCY",
        "ISSUER_UNIVERSE",
        "MARKET_CALENDAR_STRUCTURE",
        "OUTCOME_HORIZON",
    ),
    CorporateNullMechanism.SEEDED_DISPLACED_FILING_EVENT_DATE: (
        "EVENT_FREQUENCY",
        "ISSUER",
        "AFTER_CLOSE_NEXT_SESSION_TIMING_SEMANTICS",
        "OUTCOME_HORIZON",
    ),
    CorporateNullMechanism.SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT: (
        "EVENT_FREQUENCY",
        "MARKET_CALENDAR_STRUCTURE",
        "APPROXIMATE_FILING_PERIOD_DISTRIBUTION",
        "OUTCOME_HORIZON",
    ),
    CorporateNullMechanism.SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION: (
        "CONDITIONING_GROUP_FREQUENCIES",
        "REGIME_PREVALENCE",
        "MARKET_DATE_DEPENDENCE",
        "OUTCOME_HORIZON",
        "CORPORATE_FEATURE_MISSINGNESS",
    ),
    CorporateNullMechanism.SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION: (
        "CONDITIONING_GROUP_FREQUENCIES",
        "OUTCOME_MAGNITUDE_DISTRIBUTION",
        "MARKET_DATE_DEPENDENCE",
        "OUTCOME_HORIZON",
    ),
    CorporateNullMechanism.SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION: (
        "CONDITIONING_GROUP_FREQUENCIES",
        "INTERACTION_CELL_COUNTS",
        "MARKET_DATE_DEPENDENCE",
        "OUTCOME_HORIZON",
    ),
}
DESTROYED_RELATIONSHIP: dict[CorporateNullMechanism, str] = {
    CorporateNullMechanism.SEEDED_TEMPORAL_DISPLACEMENT: (
        "FEATURE_VALUE_TIMING_ALIGNMENT_WITH_OUTCOME"
    ),
    CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT: "ISSUER_SPECIFIC_FEATURE_OUTCOME_PAIRING",
    CorporateNullMechanism.SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION: (
        "ECONOMIC_CONTENT_OF_FEATURE_REPLACED_BY_UNRELATED_NOISE"
    ),
    CorporateNullMechanism.SEEDED_SURROGATE_FUNDAMENTAL_DISTRIBUTION: (
        "TRUE_HISTORICAL_FEATURE_PATH_AND_REGIME_TIMING"
    ),
    CorporateNullMechanism.SEEDED_RANDOM_NON_EVENT_DATE_MATCHED: (
        "GENUINE_FILING_AVAILABILITY_TIMING"
    ),
    CorporateNullMechanism.SEEDED_DISPLACED_FILING_EVENT_DATE: "EXACT_FILING_AVAILABILITY_DATE",
    CorporateNullMechanism.SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT: (
        "ISSUER_SPECIFIC_FILING_EVENT_PAIRING"
    ),
    CorporateNullMechanism.SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION: (
        "FEATURE_OUTCOME_TIMING_ALIGNMENT"
    ),
    CorporateNullMechanism.SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION: (
        "DIRECTIONAL_FEATURE_OUTCOME_RELATIONSHIP"
    ),
    CorporateNullMechanism.SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION: (
        "ISSUER_SPECIFIC_FEATURE_OUTCOME_PAIRING_WITHIN_CONDITION"
    ),
}


class CorporateCalibrationDepthApplicationError(RuntimeError):
    pass


class CorporateCalibrationDepthRepository(Protocol):
    def plan_by_id(self, calibration_id: str) -> CorporateCalibrationPlan | None: ...
    def save_plan(self, plan: CorporateCalibrationPlan) -> CorporateCalibrationPlan: ...
    def list_plans(self) -> tuple[CorporateCalibrationPlan, ...]: ...

    def run_by_id(self, calibration_run_id: str) -> CorporateCalibrationRun | None: ...
    def run_by_calibration(self, calibration_id: str) -> CorporateCalibrationRun | None: ...
    def save_run(self, run: CorporateCalibrationRun) -> CorporateCalibrationRun: ...

    def spec_by_id(self, spec_id: str) -> CorporateNullControlSpec | None: ...
    def save_spec(self, spec: CorporateNullControlSpec) -> CorporateNullControlSpec: ...
    def specs_for_calibration(
        self, calibration_id: str
    ) -> tuple[CorporateNullControlSpec, ...]: ...
    def specs_for_stratum(self, matched_stratum: str) -> tuple[CorporateNullControlSpec, ...]: ...

    def outcome_by_spec(self, spec_id: str) -> CorporateNullControlOutcome | None: ...
    def save_outcome(self, outcome: CorporateNullControlOutcome) -> CorporateNullControlOutcome: ...
    def outcomes_for_run(
        self, calibration_run_id: str
    ) -> tuple[CorporateNullControlOutcome, ...]: ...

    def save_reassessment(self, item: CorporateReassessment) -> CorporateReassessment: ...
    def reassessments_for_calibration(
        self, calibration_id: str
    ) -> tuple[CorporateReassessment, ...]: ...

    def save_follow_up_qualification(
        self, item: FollowUpQualification
    ) -> FollowUpQualification: ...
    def follow_up_qualifications_for_calibration(
        self, calibration_id: str
    ) -> tuple[FollowUpQualification, ...]: ...

    def report_by_calibration(self, calibration_id: str) -> CorporateCalibrationReport | None: ...
    def save_report(self, report: CorporateCalibrationReport) -> CorporateCalibrationReport: ...


# ==========================================================================
# Small local numeric/panel-reading helpers -- see module docstring for why
# these are re-declared rather than imported from ``application.corporate_
# market_research``'s own private equivalents.
# ==========================================================================


def _feature_value(
    panel: CorporatePanel, *, security: str, date: str, feature_id: CorporateFeatureId
) -> Decimal | None:
    observation = panel.feature_observation(security, date)
    return None if observation is None else observation.feature(feature_id)


def _outcome_value(
    panel: CorporatePanel,
    *,
    security: str,
    date: str,
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
) -> Decimal | None:
    observation = panel.outcome(
        stable_security_id=security, session_date=date, horizon_sessions=horizon_sessions
    )
    return None if observation is None else observation.outcome(outcome_id)


def _dates_in_range(dates: Sequence[str], *, start: str, end: str) -> tuple[str, ...]:
    return tuple(item for item in dates if start <= item <= end)


def _median_decimal(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    with localcontext() as context:
        context.prec = 28
        return (ordered[middle - 1] + ordered[middle]) / 2


def _mean_of(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 28
        return sum(values, Decimal(0)) / Decimal(len(values))


def _train_median_threshold(
    panel: CorporatePanel, *, dates: Sequence[str], feature_id: CorporateFeatureId
) -> Decimal | None:
    values = [
        value
        for security in panel.usable_security_ids
        for date in dates
        if (value := _feature_value(panel, security=security, date=date, feature_id=feature_id))
        is not None
    ]
    return _median_decimal(values) if len(values) >= 4 else None


def _beats_benchmark(trial: ResearchTrial | None) -> bool | None:
    if trial is None or trial.benchmark is None:
        return None
    return trial.benchmark.beats_benchmark


def _security_index(panel: CorporatePanel, security: str) -> int:
    try:
        return panel.usable_security_ids.index(security)
    except ValueError:
        return 0


def _combine_samples(a: HypothesisSample | None, b: HypothesisSample | None) -> HypothesisSample:
    left = a or EMPTY_SAMPLE
    right = b or EMPTY_SAMPLE
    kind = right.benchmark_kind if right.treatment else left.benchmark_kind
    return HypothesisSample(
        treatment=left.treatment + right.treatment,
        benchmark=left.benchmark + right.benchmark,
        benchmark_kind=kind,
    )


# ==========================================================================
# Section 5/6: cross-sectional level and change-based null mechanisms.
# ==========================================================================


def _seeded_displaced_date(panel: CorporatePanel, date: str, *, steps: int) -> str:
    index = panel.monthly_dates.index(date) if date in panel.monthly_dates else None
    if index is None or index - steps < 0:
        return date
    return panel.monthly_dates[index - steps]


def _seeded_derangement(security_ids: Sequence[str], *, seed: int) -> dict[str, str]:
    """A deterministic, fixed-point-free reassignment of ``security_ids``
    onto itself (milestone section 5: "fixed-point-free issuer
    reassignment"). Repeatedly seeded-shuffles until no id maps to itself;
    a guaranteed-correct rotate-by-one fallback covers the vanishingly
    unlikely case that no fixed-point-free shuffle is found within the
    bounded retry budget."""

    ordered = sorted(set(security_ids))
    count = len(ordered)
    if count < 2:
        return {sid: sid for sid in ordered}
    generator = random.Random(seed)
    permuted = list(ordered)
    for _attempt in range(50):
        generator.shuffle(permuted)
        if all(original != mapped for original, mapped in zip(ordered, permuted, strict=True)):
            return dict(zip(ordered, permuted, strict=True))
    return {sid: ordered[(index + 1) % count] for index, sid in enumerate(ordered)}


def _cross_sectional_rows_local(
    panel: CorporatePanel,
    *,
    dates: Sequence[str],
    feature_id: CorporateFeatureId,
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
    threshold: Decimal,
    feature_security_override: dict[tuple[str, str], str] | None = None,
    feature_date_override: dict[tuple[str, str], str] | None = None,
) -> HypothesisSample:
    treatment: list[CorporatePanelRow] = []
    benchmark: list[CorporatePanelRow] = []
    for security in panel.usable_security_ids:
        for date in dates:
            feature_security = (
                feature_security_override.get((security, date), security)
                if feature_security_override
                else security
            )
            feature_date = (
                feature_date_override.get((security, date), date) if feature_date_override else date
            )
            feature_value = _feature_value(
                panel, security=feature_security, date=feature_date, feature_id=feature_id
            )
            outcome_value = _outcome_value(
                panel,
                security=security,
                date=date,
                outcome_id=outcome_id,
                horizon_sessions=horizon_sessions,
            )
            if feature_value is None or outcome_value is None:
                continue
            row = CorporatePanelRow(security, date, outcome_value)
            benchmark.append(row)
            if feature_value >= threshold:
                treatment.append(row)
    return HypothesisSample(tuple(treatment), tuple(benchmark), BenchmarkKind.EQUAL_WEIGHT_COHORT)


def _synthetic_noise_series(
    panel: CorporatePanel, *, security: str, feature_id: CorporateFeatureId, seed: int
) -> dict[str, Decimal]:
    """Section 5's "concept substitution": economically meaningless seeded
    noise, retained only wherever the real ``feature_id`` genuinely has a
    value (preserving that feature's own missingness footprint)."""

    generator = random.Random(seed * 1000 + _security_index(panel, security))
    result: dict[str, Decimal] = {}
    for date in panel.monthly_dates:
        if _feature_value(panel, security=security, date=date, feature_id=feature_id) is not None:
            result[date] = bound_statistical_decimal(Decimal(str(generator.gauss(0.0, 1.0))))
    return result


def _synthetic_surrogate_series(
    panel: CorporatePanel, *, security: str, feature_id: CorporateFeatureId, seed: int
) -> dict[str, Decimal]:
    """Section 5's "deterministic surrogate fundamental values preserving
    missingness/distribution characteristics": reuses ``research_
    calibration.synthetic_ar1_surrogate`` unmodified, fit only to this one
    security's own retained mean/volatility/lag-1 autocorrelation, over
    exactly the dates that security's real feature genuinely covers."""

    real = tuple(
        DatedValue(date, value)
        for date in panel.monthly_dates
        if (value := _feature_value(panel, security=security, date=date, feature_id=feature_id))
        is not None
    )
    if not real:
        return {}
    surrogate = synthetic_ar1_surrogate(real, seed=seed * 1000 + _security_index(panel, security))
    return {item.session_date: item.value for item in surrogate}


def _synthetic_train_median_threshold(
    dates: Sequence[str], synthetic_by_security: Mapping[str, Mapping[str, Decimal]]
) -> Decimal | None:
    values = [
        series[date]
        for series in synthetic_by_security.values()
        for date in dates
        if date in series
    ]
    return _median_decimal(values) if len(values) >= 4 else None


def _synthetic_cross_sectional_rows(
    panel: CorporatePanel,
    *,
    dates: Sequence[str],
    synthetic_by_security: Mapping[str, Mapping[str, Decimal]],
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
    threshold: Decimal,
) -> HypothesisSample:
    treatment: list[CorporatePanelRow] = []
    benchmark: list[CorporatePanelRow] = []
    for security in panel.usable_security_ids:
        series = synthetic_by_security.get(security, {})
        for date in dates:
            feature_value = series.get(date)
            if feature_value is None:
                continue
            outcome_value = _outcome_value(
                panel,
                security=security,
                date=date,
                outcome_id=outcome_id,
                horizon_sessions=horizon_sessions,
            )
            if outcome_value is None:
                continue
            row = CorporatePanelRow(security, date, outcome_value)
            benchmark.append(row)
            if feature_value >= threshold:
                treatment.append(row)
    return HypothesisSample(tuple(treatment), tuple(benchmark), BenchmarkKind.EQUAL_WEIGHT_COHORT)


def _build_cross_sectional_null_sample(
    spec: CorporateNullControlSpec,
    *,
    panel: CorporatePanel,
    dates: Sequence[str],
    train_dates: Sequence[str],
) -> HypothesisSample:
    if spec.mechanism in (
        CorporateNullMechanism.SEEDED_TEMPORAL_DISPLACEMENT,
        CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT,
    ):
        threshold = _train_median_threshold(panel, dates=train_dates, feature_id=spec.feature_id)
        if threshold is None:
            return EMPTY_SAMPLE
        if spec.mechanism is CorporateNullMechanism.SEEDED_TEMPORAL_DISPLACEMENT:
            generator = random.Random(spec.seed)
            steps = generator.randint(2, 6)
            overrides = {
                (security, date): _seeded_displaced_date(panel, date, steps=steps)
                for security in panel.usable_security_ids
                for date in dates
            }
            return _cross_sectional_rows_local(
                panel,
                dates=dates,
                feature_id=spec.feature_id,
                outcome_id=spec.outcome_id,
                horizon_sessions=spec.horizon_sessions,
                threshold=threshold,
                feature_date_override=overrides,
            )
        rotation = _seeded_derangement(panel.usable_security_ids, seed=spec.seed)
        overrides = {
            (security, date): rotation[security]
            for security in panel.usable_security_ids
            for date in dates
        }
        return _cross_sectional_rows_local(
            panel,
            dates=dates,
            feature_id=spec.feature_id,
            outcome_id=spec.outcome_id,
            horizon_sessions=spec.horizon_sessions,
            threshold=threshold,
            feature_security_override=overrides,
        )

    builder = (
        _synthetic_noise_series
        if spec.mechanism is CorporateNullMechanism.SEEDED_RANDOM_NOISE_CONCEPT_SUBSTITUTION
        else _synthetic_surrogate_series
    )
    synthetic = {
        security: builder(panel, security=security, feature_id=spec.feature_id, seed=spec.seed)
        for security in panel.usable_security_ids
    }
    threshold = _synthetic_train_median_threshold(train_dates, synthetic)
    if threshold is None:
        return EMPTY_SAMPLE
    return _synthetic_cross_sectional_rows(
        panel,
        dates=dates,
        synthetic_by_security=synthetic,
        outcome_id=spec.outcome_id,
        horizon_sessions=spec.horizon_sessions,
        threshold=threshold,
    )


# ==========================================================================
# Section 7: filing-event null mechanisms.
# ==========================================================================


def _filing_event_candidates(
    panel: CorporatePanel, *, feature_id: CorporateFeatureId, date_range: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    is_earnings = feature_id is CorporateFeatureId.RECENT_8K_INDICATOR
    is_amendment = feature_id is CorporateFeatureId.AMENDMENT_INDICATOR
    events: list[tuple[str, str]] = []
    for security, observations in panel.filing_events_by_security.items():
        for observation in observations:
            if observation.first_outcome_session_date not in date_range:
                continue
            if is_earnings and not observation.is_earnings_proxy:
                continue
            if is_amendment and not observation.is_amendment:
                continue
            if not is_earnings and not is_amendment and observation.is_amendment:
                continue
            events.append((security, observation.first_outcome_session_date))
    return tuple(events)


def _random_matched_dates_local(
    *, universe: Sequence[tuple[str, str]], count: int, seed: int
) -> tuple[tuple[str, str], ...]:
    if not universe or count <= 0:
        return ()
    generator = random.Random(seed)
    pool = list(universe)
    generator.shuffle(pool)
    return tuple(pool[: min(count, len(pool))])


def _rows_from_pairs(
    panel: CorporatePanel,
    pairs: Sequence[tuple[str, str]],
    *,
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
) -> tuple[CorporatePanelRow, ...]:
    rows: list[CorporatePanelRow] = []
    for security, date in pairs:
        outcome_value = _outcome_value(
            panel,
            security=security,
            date=date,
            outcome_id=outcome_id,
            horizon_sessions=horizon_sessions,
        )
        if outcome_value is not None:
            rows.append(CorporatePanelRow(security, date, outcome_value))
    return tuple(rows)


def _build_filing_event_null_sample(
    spec: CorporateNullControlSpec, *, panel: CorporatePanel, dates: Sequence[str]
) -> HypothesisSample:
    date_range = frozenset(dates)
    universe = tuple((security, date) for security in panel.usable_security_ids for date in dates)
    candidates = _filing_event_candidates(panel, feature_id=spec.feature_id, date_range=date_range)

    if spec.mechanism is CorporateNullMechanism.SEEDED_RANDOM_NON_EVENT_DATE_MATCHED:
        events = _random_matched_dates_local(
            universe=universe, count=max(len(candidates), 20), seed=spec.seed
        )
    elif spec.mechanism is CorporateNullMechanism.SEEDED_DISPLACED_FILING_EVENT_DATE:
        generator = random.Random(spec.seed)
        displaced: list[tuple[str, str]] = []
        for security, date in candidates:
            index = panel.calendar.index_of(date)
            if index is None:
                continue
            target = index + generator.choice(FILING_EVENT_DATE_OFFSETS)
            if 0 <= target < len(panel.calendar.dates):
                new_date = panel.calendar.dates[target]
                if new_date in date_range:
                    displaced.append((security, new_date))
        events = tuple(displaced)
    else:  # SEEDED_ISSUER_MATCHED_EVENT_REASSIGNMENT
        rotation = _seeded_derangement(panel.usable_security_ids, seed=spec.seed)
        events = tuple((rotation.get(security, security), date) for security, date in candidates)

    treatment = _rows_from_pairs(
        panel, events, outcome_id=spec.outcome_id, horizon_sessions=spec.horizon_sessions
    )
    matched = _random_matched_dates_local(
        universe=universe, count=len(treatment) or 20, seed=spec.seed + 1
    )
    benchmark = _rows_from_pairs(
        panel, matched, outcome_id=spec.outcome_id, horizon_sessions=spec.horizon_sessions
    )
    return HypothesisSample(treatment, benchmark, BenchmarkKind.RANDOM_DATE_BASELINE)


# ==========================================================================
# Section 8: conditional/interaction null mechanisms -- every one nulls the
# OUTCOME only, preserving the real feature values and the real
# conditioning-group (date) membership exactly.
# ==========================================================================


def _shuffled_outcome_within_date(
    panel: CorporatePanel,
    pairs: Sequence[tuple[str, str]],
    *,
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
    seed: int,
) -> dict[tuple[str, str], Decimal]:
    by_date: dict[str, list[str]] = {}
    for security, date in pairs:
        by_date.setdefault(date, []).append(security)
    result: dict[tuple[str, str], Decimal] = {}
    for date_ordinal, date in enumerate(sorted(by_date)):
        securities = by_date[date]
        real_values = [
            _outcome_value(
                panel,
                security=s,
                date=date,
                outcome_id=outcome_id,
                horizon_sessions=horizon_sessions,
            )
            for s in securities
        ]
        shuffled = list(real_values)
        random.Random(seed + date_ordinal).shuffle(shuffled)
        for security, value in zip(securities, shuffled, strict=True):
            if value is not None:
                result[(security, date)] = value
    return result


def _sign_randomized_outcome(
    panel: CorporatePanel,
    pairs: Sequence[tuple[str, str]],
    *,
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
    seed: int,
) -> dict[tuple[str, str], Decimal]:
    generator = random.Random(seed)
    result: dict[tuple[str, str], Decimal] = {}
    for security, date in sorted(pairs):
        value = _outcome_value(
            panel,
            security=security,
            date=date,
            outcome_id=outcome_id,
            horizon_sessions=horizon_sessions,
        )
        if value is not None:
            result[(security, date)] = -value if generator.random() < 0.5 else value
    return result


def _block_permuted_outcome(
    panel: CorporatePanel,
    pairs: Sequence[tuple[str, str]],
    *,
    dates: Sequence[str],
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
    seed: int,
) -> dict[tuple[str, str], Decimal]:
    ordered_dates = sorted(set(dates))
    blocks = [
        ordered_dates[i : i + BLOCK_PERMUTATION_BLOCK_LENGTH]
        for i in range(0, len(ordered_dates), BLOCK_PERMUTATION_BLOCK_LENGTH)
    ]
    permuted_blocks = list(blocks)
    random.Random(seed).shuffle(permuted_blocks)
    permuted_dates = [d for block in permuted_blocks for d in block]
    date_map = dict(zip(ordered_dates, permuted_dates, strict=True))
    result: dict[tuple[str, str], Decimal] = {}
    for security, date in pairs:
        substitute = date_map.get(date, date)
        value = _outcome_value(
            panel,
            security=security,
            date=substitute,
            outcome_id=outcome_id,
            horizon_sessions=horizon_sessions,
        )
        if value is not None:
            result[(security, date)] = value
    return result


def _build_conditional_interaction_null_sample(
    spec: CorporateNullControlSpec,
    *,
    panel: CorporatePanel,
    dates: Sequence[str],
    threshold: Decimal,
) -> HypothesisSample:
    feature_by_pair: dict[tuple[str, str], Decimal] = {}
    for security in panel.usable_security_ids:
        for date in dates:
            value = _feature_value(panel, security=security, date=date, feature_id=spec.feature_id)
            if value is not None:
                feature_by_pair[(security, date)] = value
    pairs = list(feature_by_pair)
    membership = {pair: value >= threshold for pair, value in feature_by_pair.items()}
    if spec.mechanism is CorporateNullMechanism.SEEDED_OUTCOME_SHUFFLE_WITHIN_DATE_CONDITION:
        outcome_by_pair = _shuffled_outcome_within_date(
            panel,
            pairs,
            outcome_id=spec.outcome_id,
            horizon_sessions=spec.horizon_sessions,
            seed=spec.seed,
        )
    elif (
        spec.mechanism is CorporateNullMechanism.SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION
    ):
        outcome_by_pair = _sign_randomized_outcome(
            panel,
            pairs,
            outcome_id=spec.outcome_id,
            horizon_sessions=spec.horizon_sessions,
            seed=spec.seed,
        )
    else:  # SEEDED_OUTCOME_BLOCK_PERMUTATION_WITHIN_CONDITION
        outcome_by_pair = _block_permuted_outcome(
            panel,
            pairs,
            dates=dates,
            outcome_id=spec.outcome_id,
            horizon_sessions=spec.horizon_sessions,
            seed=spec.seed,
        )
    treatment: list[CorporatePanelRow] = []
    benchmark: list[CorporatePanelRow] = []
    for (security, date), is_treatment in membership.items():
        value = outcome_by_pair.get((security, date))
        if value is None:
            continue
        row = CorporatePanelRow(security, date, value)
        benchmark.append(row)
        if is_treatment:
            treatment.append(row)
    return HypothesisSample(tuple(treatment), tuple(benchmark), BenchmarkKind.EQUAL_WEIGHT_COHORT)


# ==========================================================================
# The one dispatcher every null control routes through -- the calibration-
# depth analogue of ``application.corporate_market_research.build_
# hypothesis_sample``.
# ==========================================================================


def build_corporate_null_sample(
    spec: CorporateNullControlSpec,
    *,
    panel: CorporatePanel,
    period_policy: ResearchPeriodPolicy,
    role: ResearchPeriodRole,
) -> HypothesisSample | None:
    period = next((item for item in period_policy.periods if item.role is role), None)
    if period is None:
        return None
    dates = _dates_in_range(panel.monthly_dates, start=period.start_date, end=period.end_date)
    train_period = next(
        item for item in period_policy.periods if item.role is ResearchPeriodRole.TRAIN
    )
    train_dates = _dates_in_range(
        panel.monthly_dates, start=train_period.start_date, end=train_period.end_date
    )

    if spec.research_design is ResearchDesign.FILING_EVENT:
        filing_dates = _dates_in_range(
            panel.calendar.dates, start=period.start_date, end=period.end_date
        )
        return _build_filing_event_null_sample(spec, panel=panel, dates=filing_dates)

    if spec.research_design in (ResearchDesign.CONDITIONAL, ResearchDesign.INTERACTION):
        if spec.secondary_context_dimension is None or spec.secondary_condition_label is None:
            raise CorporateCalibrationDepthApplicationError(
                "CONDITIONAL_OR_INTERACTION_SPEC_MISSING_SECONDARY_CONTEXT"
            )
        restricted_dates = tuple(
            d
            for d in dates
            if panel.context_label(spec.secondary_context_dimension, d)
            == spec.secondary_condition_label
        )
        restricted_train_dates = tuple(
            d
            for d in train_dates
            if panel.context_label(spec.secondary_context_dimension, d)
            == spec.secondary_condition_label
        )
        threshold = _train_median_threshold(
            panel, dates=restricted_train_dates, feature_id=spec.feature_id
        )
        if threshold is None:
            return EMPTY_SAMPLE
        return _build_conditional_interaction_null_sample(
            spec, panel=panel, dates=restricted_dates, threshold=threshold
        )

    return _build_cross_sectional_null_sample(
        spec, panel=panel, dates=dates, train_dates=train_dates
    )


# ==========================================================================
# Trial construction -- mirrors ``application.corporate_market_research.
# build_corporate_trial`` field for field, substituting ``CorporateNull
# ControlSpec``'s own fields for ``GeneratedCorporateHypothesis``'s, and
# ``CORPORATE_CALIBRATION_DEPTH_METHOD_ID`` so a calibration-depth trial's
# evidence is never mistaken for a genuine or original v3 hypothesis trial.
# ==========================================================================


def build_corporate_null_trial(
    spec: CorporateNullControlSpec,
    sample: HypothesisSample,
    *,
    dataset_snapshot_id: str,
    replay_run_id: str,
    phase: ResearchPhase,
    governance_policy: ResearchGovernancePolicy | None,
    created_at: datetime,
    bootstrap_seed: int | None = None,
) -> ResearchTrial | None:
    if not sample.treatment:
        return None
    diagnostics = build_corporate_diagnostics(sample, bootstrap_seed=bootstrap_seed)
    benchmark = None
    if sample.benchmark and diagnostics.base.mean is not None:
        benchmark_values = tuple(row.value for row in sample.benchmark)
        benchmark = compare_to_benchmark(
            sample_mean=diagnostics.base.mean,
            benchmark_kind=sample.benchmark_kind,
            benchmark_values=benchmark_values,
        )
    configuration = ResearchConfiguration.create(
        method_id=CORPORATE_CALIBRATION_DEPTH_METHOD_ID,
        method_version="V1",
        parameter_values=(
            ("spec_id", spec.spec_id),
            ("matched_stratum", spec.matched_stratum),
            ("research_design", spec.research_design.value),
            ("mechanism", spec.mechanism.value),
            ("feature_id", spec.feature_id.value),
            ("outcome_id", spec.outcome_id.value),
            ("horizon_sessions", str(spec.horizon_sessions)),
            ("seed", str(spec.seed)),
        ),
        origin=ConfigurationOrigin.FIXED_A_PRIORI,
        candidates_examined_count=1,
    )
    dated_ids = tuple(
        sorted(
            {
                (
                    row.session_date,
                    deterministic_id(
                        "corporate_calibration_null_panel_row",
                        (spec.spec_id, row.stable_security_id, row.session_date, phase),
                    ),
                )
                for row in sample.treatment
            }
        )
    )
    return ResearchTrial.create(
        hypothesis_version_id=spec.spec_id,
        dataset_snapshot_id=dataset_snapshot_id,
        replay_run_id=replay_run_id,
        phase=phase,
        market_scope=("US_EQUITY",),
        configuration=configuration,
        dated_evaluation_ids=dated_ids,
        diagnostics=diagnostics,
        benchmark=benchmark,
        created_at=created_at,
        governance_policy=governance_policy,
    )


# ==========================================================================
# Section 2/10: strata inventory and the declared calibration plan.
# ==========================================================================


def _original_matched_null_pool_tagged(
    stratum: str,
    *,
    control_hypotheses: Sequence[GeneratedCorporateHypothesis],
    trials_by_hypothesis: Mapping[str, ResearchTrial | None],
) -> tuple[tuple[str, Decimal], ...]:
    """Returns each original v3 control's own statistic tagged by its
    ``hypothesis_id`` so a later leave-one-out grading pass can exclude a
    subject's own contribution from its own matched-null comparison
    population (never grading a control, or an original control being
    reassessed, against a pool that still contains itself)."""

    values: list[tuple[str, Decimal]] = []
    for control in control_hypotheses:
        if corporate_matched_stratum_key(control) != stratum:
            continue
        trial = trials_by_hypothesis.get(control.hypothesis_id)
        if trial is not None and trial.diagnostics.effect_size is not None:
            statistic = trial.diagnostics.effect_size.cohens_d
            if statistic is not None:
                values.append((control.hypothesis_id, statistic))
    return tuple(values)


def _original_matched_null_pool(
    stratum: str,
    *,
    control_hypotheses: Sequence[GeneratedCorporateHypothesis],
    trials_by_hypothesis: Mapping[str, ResearchTrial | None],
) -> tuple[Decimal, ...]:
    return tuple(
        value
        for _hypothesis_id, value in _original_matched_null_pool_tagged(
            stratum,
            control_hypotheses=control_hypotheses,
            trials_by_hypothesis=trials_by_hypothesis,
        )
    )


def _leave_one_out(pool: Sequence[tuple[str, Decimal]], *, exclude_id: str) -> tuple[Decimal, ...]:
    """The matched-null population for ``exclude_id``'s own grade: every
    other subject's statistic in the same stratum, with ``exclude_id``'s
    own contribution (if present) removed exactly once -- never grading a
    subject's own result against a population that still includes it."""

    removed = False
    result: list[Decimal] = []
    for subject_id, value in pool:
        if not removed and subject_id == exclude_id:
            removed = True
            continue
        result.append(value)
    return tuple(result)


def build_corporate_strata_inventory(
    *,
    genuine_hypotheses: Sequence[GeneratedCorporateHypothesis],
    control_hypotheses: Sequence[GeneratedCorporateHypothesis],
    trials_by_hypothesis: Mapping[str, ResearchTrial | None],
    controls_per_stratum: int = CONTROLS_PER_STRATUM,
) -> tuple[CorporateStratumTarget, ...]:
    """Section 2's inventory: groups the 20 already-retained genuine v3
    hypotheses by the exact matched-stratum key ``application.corporate_
    market_research.corporate_matched_stratum_key`` already uses, and
    reports each stratum's currently retained (original, 17-control)
    matched-null count -- the input section 10's budget declaration is
    built from, never computed after observing any new result."""

    by_stratum: dict[str, list[GeneratedCorporateHypothesis]] = {}
    for hypothesis in genuine_hypotheses:
        stratum = corporate_matched_stratum_key(hypothesis)
        by_stratum.setdefault(stratum, []).append(hypothesis)

    targets: list[CorporateStratumTarget] = []
    for stratum, members in sorted(by_stratum.items()):
        design_str, outcome_str, horizon_str, shape_str = stratum.split("|")
        design = ResearchDesign(design_str)
        shape = ConditioningShape(shape_str)
        horizon = int(horizon_str[1:])
        backing_features = tuple(
            sorted({item.feature_id for item in members}, key=lambda f: f.value)
        )
        secondary_conditions: tuple[tuple[str, str, str], ...] = ()
        if design in (ResearchDesign.CONDITIONAL, ResearchDesign.INTERACTION):
            secondary_conditions = tuple(
                sorted(
                    {
                        (
                            item.feature_id.value,
                            item.secondary_context_dimension,
                            item.secondary_condition_label,
                        )
                        for item in members
                        if item.secondary_context_dimension is not None
                        and item.secondary_condition_label is not None
                    }
                )
            )
        original_pool = _original_matched_null_pool(
            stratum,
            control_hypotheses=control_hypotheses,
            trials_by_hypothesis=trials_by_hypothesis,
        )
        targets.append(
            CorporateStratumTarget.create(
                matched_stratum=stratum,
                research_design=design,
                conditioning_shape=shape,
                outcome_id=outcome_str,
                horizon_sessions=horizon,
                genuine_hypothesis_ids=tuple(item.hypothesis_id for item in members),
                backing_feature_ids=backing_features,
                original_matched_null_count=len(original_pool),
                target_new_control_count=controls_per_stratum,
                secondary_conditions=secondary_conditions,
            )
        )
    return tuple(targets)


CALIBRATION_DEPTH_OBJECTIVE = (
    "For each corporate research design Cross-Market Research v3 used, measure what equally "
    "strong evidence looks like under structurally matched null experiments -- methodology "
    "calibration depth over the already-retained v3 agenda, never a new corporate hypothesis "
    "search. Strictly additive: creates no Strategy, Shared Opportunity, PAPER order, or "
    "execution authority, and rewrites no v3 hypothesis, trial, conclusion, or knowledge entry."
)
CALIBRATION_DEPTH_SEED_POLICY = (
    "Every seeded transform uses one of ten fixed, declared per-mechanism integer seed bases "
    "(20260809401-20260809410), one per CorporateNullMechanism, chosen before any calibration-"
    "depth control was executed; each draw within a mechanism/stratum adds its own zero-based "
    "draw index to that base. No seed is selected, retried, or swept after observing a result."
)


def build_corporate_calibration_plan(
    *,
    source_agenda_id: str,
    corporate_dataset_id: str,
    equity_snapshot_id: str,
    replay_run_id: str,
    cross_market_dataset_id: str,
    stratum_targets: tuple[CorporateStratumTarget, ...],
    registered_at: datetime,
    controls_per_stratum: int = CONTROLS_PER_STRATUM,
) -> CorporateCalibrationPlan:
    return CorporateCalibrationPlan.create(
        version="CORPORATE_CALIBRATION_DEPTH_V1",
        objective=CALIBRATION_DEPTH_OBJECTIVE,
        source_agenda_id=source_agenda_id,
        corporate_dataset_id=corporate_dataset_id,
        equity_snapshot_id=equity_snapshot_id,
        replay_run_id=replay_run_id,
        cross_market_dataset_id=cross_market_dataset_id,
        stratum_targets=stratum_targets,
        controls_per_stratum=controls_per_stratum,
        random_seed_policy=CALIBRATION_DEPTH_SEED_POLICY,
        registered_at=registered_at,
    )


# ==========================================================================
# Section 11: deterministic, declared-before-results spec generation.
# ==========================================================================


def generate_corporate_null_control_specs(
    plan: CorporateCalibrationPlan, *, created_at: datetime
) -> tuple[CorporateNullControlSpec, ...]:
    specs: list[CorporateNullControlSpec] = []
    ordinal = 0
    for target in plan.stratum_targets:
        template_id = TEMPLATE_ID_BY_DESIGN[target.research_design]
        if target.research_design in (ResearchDesign.CROSS_SECTIONAL, ResearchDesign.CHANGE_BASED):
            for mechanism in CROSS_SECTIONAL_MECHANISMS:
                base_seed = SEED_BASES[mechanism]
                for draw in range(CROSS_SECTIONAL_DRAWS_PER_MECHANISM):
                    feature = target.backing_feature_ids[draw % len(target.backing_feature_ids)]
                    ordinal += 1
                    specs.append(
                        CorporateNullControlSpec.create(
                            calibration_id=plan.calibration_id,
                            matched_stratum=target.matched_stratum,
                            research_design=target.research_design,
                            conditioning_shape=target.conditioning_shape,
                            template_id=template_id,
                            mechanism=mechanism,
                            feature_id=feature,
                            outcome_id=target.outcome_id,
                            horizon_sessions=target.horizon_sessions,
                            secondary_context_dimension=None,
                            secondary_condition_label=None,
                            seed=base_seed + draw,
                            preserved_properties=PRESERVED_PROPERTIES[mechanism],
                            destroyed_relationship=DESTROYED_RELATIONSHIP[mechanism],
                            rationale=(
                                f"Calibration-depth control for stratum {target.matched_stratum}: "
                                f"{mechanism.value} applied to {feature.value}."
                            ),
                            ordinal=ordinal,
                            created_at=created_at,
                        )
                    )
        elif target.research_design is ResearchDesign.FILING_EVENT:
            for mechanism in FILING_EVENT_MECHANISMS:
                base_seed = SEED_BASES[mechanism]
                for draw in range(FILING_EVENT_DRAWS_PER_MECHANISM):
                    feature = target.backing_feature_ids[draw % len(target.backing_feature_ids)]
                    ordinal += 1
                    specs.append(
                        CorporateNullControlSpec.create(
                            calibration_id=plan.calibration_id,
                            matched_stratum=target.matched_stratum,
                            research_design=target.research_design,
                            conditioning_shape=target.conditioning_shape,
                            template_id=template_id,
                            mechanism=mechanism,
                            feature_id=feature,
                            outcome_id=target.outcome_id,
                            horizon_sessions=target.horizon_sessions,
                            secondary_context_dimension=None,
                            secondary_condition_label=None,
                            seed=base_seed + draw,
                            preserved_properties=PRESERVED_PROPERTIES[mechanism],
                            destroyed_relationship=DESTROYED_RELATIONSHIP[mechanism],
                            rationale=(
                                f"Calibration-depth control for stratum {target.matched_stratum}: "
                                f"{mechanism.value} applied to {feature.value}."
                            ),
                            ordinal=ordinal,
                            created_at=created_at,
                        )
                    )
        else:  # CONDITIONAL / INTERACTION
            for mechanism in CONDITIONAL_INTERACTION_MECHANISMS:
                base_seed = SEED_BASES[mechanism]
                draw = 0
                for feature_value, dimension, label in target.secondary_conditions:
                    for _local_draw in range(CONDITIONAL_DRAWS_PER_DIMENSION_PER_MECHANISM):
                        ordinal += 1
                        specs.append(
                            CorporateNullControlSpec.create(
                                calibration_id=plan.calibration_id,
                                matched_stratum=target.matched_stratum,
                                research_design=target.research_design,
                                conditioning_shape=target.conditioning_shape,
                                template_id=template_id,
                                mechanism=mechanism,
                                feature_id=feature_value,
                                outcome_id=target.outcome_id,
                                horizon_sessions=target.horizon_sessions,
                                secondary_context_dimension=dimension,
                                secondary_condition_label=label,
                                seed=base_seed + draw,
                                preserved_properties=PRESERVED_PROPERTIES[mechanism],
                                destroyed_relationship=DESTROYED_RELATIONSHIP[mechanism],
                                rationale=(
                                    f"Calibration-depth control for stratum "
                                    f"{target.matched_stratum}: {mechanism.value} preserving real "
                                    f"{dimension}={label} conditioning on {feature_value}."
                                ),
                                ordinal=ordinal,
                                created_at=created_at,
                            )
                        )
                        draw += 1
    return tuple(specs)


# ==========================================================================
# Section 10 (run registration/advancement).
# ==========================================================================


@dataclass(frozen=True, slots=True)
class RegisterCorporateCalibrationRunResult:
    run: CorporateCalibrationRun
    specs: tuple[CorporateNullControlSpec, ...]


class RegisterCorporateCalibrationRun:
    def __init__(self, *, repository: CorporateCalibrationDepthRepository) -> None:
        self._repository = repository

    def execute(
        self, *, plan: CorporateCalibrationPlan, created_at: datetime
    ) -> RegisterCorporateCalibrationRunResult:
        existing_run = self._repository.run_by_calibration(plan.calibration_id)
        specs = generate_corporate_null_control_specs(plan, created_at=created_at)
        saved_specs = tuple(self._repository.save_spec(spec) for spec in specs)
        if existing_run is not None:
            return RegisterCorporateCalibrationRunResult(existing_run, saved_specs)
        run = CorporateCalibrationRun.draft(
            calibration=plan,
            spec_ids=tuple(spec.spec_id for spec in saved_specs),
            created_at=created_at,
        )
        return RegisterCorporateCalibrationRunResult(self._repository.save_run(run), saved_specs)


class RunSingleCorporateNullControl:
    def __init__(
        self,
        *,
        methodology: ResearchMethodologyCollaborator,
        panel: CorporatePanel,
        period_policy: ResearchPeriodPolicy,
    ) -> None:
        self._methodology = methodology
        self._panel = panel
        self._period_policy = period_policy

    def execute(
        self,
        spec: CorporateNullControlSpec,
        *,
        plan: CorporateCalibrationPlan,
        calibration_run_id: str,
        created_at: datetime,
    ) -> CorporateNullControlOutcome:
        policy = ResearchGovernancePolicy.create(
            hypothesis_version_id=spec.spec_id,
            dataset_snapshot_id=plan.corporate_dataset_id,
            replay_run_id=plan.replay_run_id,
            intent=GovernanceIntent.PROMOTION_TRACK,
            period_policy=self._period_policy,
            revision=1,
            supersedes_governance_policy_id=None,
            declared_at=created_at,
        )
        existing_policy = self._methodology.governance_policy_by_id(policy.governance_policy_id)
        policy = existing_policy or self._methodology.save_governance_policy(policy)

        train_sample = build_corporate_null_sample(
            spec,
            panel=self._panel,
            period_policy=self._period_policy,
            role=ResearchPeriodRole.TRAIN,
        )
        validation_sample = build_corporate_null_sample(
            spec,
            panel=self._panel,
            period_policy=self._period_policy,
            role=ResearchPeriodRole.VALIDATION,
        )
        guard_governed_holdout_access(policy, purpose="EVALUATION_ONLY")
        holdout_sample = build_corporate_null_sample(
            spec,
            panel=self._panel,
            period_policy=self._period_policy,
            role=ResearchPeriodRole.HOLDOUT,
        )
        exploratory_sample = _combine_samples(train_sample, validation_sample)

        blockers: list[str] = []
        exploratory_trial = self._save_trial(
            build_corporate_null_trial(
                spec,
                exploratory_sample,
                dataset_snapshot_id=plan.corporate_dataset_id,
                replay_run_id=plan.replay_run_id,
                phase=ResearchPhase.EXPLORATORY,
                governance_policy=None,
                created_at=created_at,
            )
        )
        validation_trial = self._save_trial(
            build_corporate_null_trial(
                spec,
                validation_sample or EMPTY_SAMPLE,
                dataset_snapshot_id=plan.corporate_dataset_id,
                replay_run_id=plan.replay_run_id,
                phase=ResearchPhase.VALIDATION,
                governance_policy=policy,
                created_at=created_at,
                bootstrap_seed=spec.seed,
            )
        )
        if validation_trial is None:
            blockers.append("VALIDATION_SAMPLE_INSUFFICIENT")
        holdout_trial = self._save_trial(
            build_corporate_null_trial(
                spec,
                holdout_sample or EMPTY_SAMPLE,
                dataset_snapshot_id=plan.corporate_dataset_id,
                replay_run_id=plan.replay_run_id,
                phase=ResearchPhase.LOCKED_HOLDOUT,
                governance_policy=policy,
                created_at=created_at,
            )
        )
        if holdout_trial is None:
            blockers.append("HOLDOUT_SAMPLE_INSUFFICIENT")
        else:
            exposure = HoldoutExposureEvent.create(
                governance_policy=policy,
                exposed_at=created_at,
                actor="system:corporate_calibration_depth_run",
                purpose="EVALUATION_ONLY",
                consumed_trial_id=holdout_trial.trial_id,
            )
            self._methodology.save_holdout_exposure(exposure)

        robustness_run: RobustnessCheckRun | None = None
        if validation_trial is not None and validation_trial.diagnostics.base.mean is not None:
            validation_or_empty = validation_sample or EMPTY_SAMPLE
            dev_sample = _combine_samples(train_sample, validation_or_empty)
            results = compute_robustness(
                dev_sample, panel=self._panel, base_mean=validation_trial.diagnostics.base.mean
            )
            candidate_run = RobustnessCheckRun.create(
                base_trial_id=validation_trial.trial_id, results=results, created_at=created_at
            )
            existing_run = self._methodology.robustness_run_by_id(candidate_run.robustness_run_id)
            robustness_run = existing_run or self._methodology.save_robustness_run(candidate_run)
        else:
            blockers.append("ROBUSTNESS_NOT_RUN_NO_REFERENCE_TRIAL")

        requirement = resolve_minimum_sample_requirement(phase=ResearchPhase.VALIDATION)
        effective = (
            validation_trial.diagnostics.overlap.effective_sample_count
            if validation_trial is not None
            else Decimal(0)
        )
        sample_quality = (
            SampleQualityState.SUFFICIENT
            if validation_trial is not None
            and validation_trial.diagnostics.overlap.inference_available
            and effective >= requirement.minimum_effective_sample
            else SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE
        )
        conclusion_state = derive_conclusion_state(
            sample_quality=sample_quality,
            robustness_summary=robustness_run.summary if robustness_run else None,
            validation_beats_benchmark=_beats_benchmark(validation_trial),
            holdout_beats_benchmark=_beats_benchmark(holdout_trial),
            multiple_testing_corrected_significant=None,
        )
        conclusion = ResearchConclusion.create(
            hypothesis_version_id=spec.spec_id,
            dataset_snapshot_id=plan.corporate_dataset_id,
            replay_run_id=plan.replay_run_id,
            phase=ResearchPhase.LOCKED_HOLDOUT
            if holdout_trial is not None
            else ResearchPhase.VALIDATION,
            training_trial_id=exploratory_trial.trial_id if exploratory_trial else None,
            validation_trial_id=validation_trial.trial_id if validation_trial else None,
            holdout_trial_id=holdout_trial.trial_id if holdout_trial else None,
            walk_forward_run_id=None,
            walk_forward_fold_count=0,
            robustness_run_id=robustness_run.robustness_run_id if robustness_run else None,
            trial_family_id=None,
            sample_quality_state=sample_quality,
            point_in_time_limitations=corporate_calibration_depth_warnings(),
            benchmark_kind=(
                validation_trial.benchmark.benchmark_kind
                if validation_trial is not None and validation_trial.benchmark
                else None
            ),
            beats_benchmark=_beats_benchmark(validation_trial),
            conclusion_state=conclusion_state,
            rationale=spec.rationale,
            blockers=tuple(blockers),
            created_at=created_at,
        )
        existing_conclusion = self._methodology.conclusion_by_id(conclusion.conclusion_id)
        saved_conclusion = existing_conclusion or self._methodology.save_conclusion(conclusion)

        promotion = PromotionEvaluation.create(
            conclusion=saved_conclusion,
            policy=CORPORATE_CALIBRATION_DEPTH_PROMOTION_POLICY,
            holdout_exposed=holdout_trial is not None,
            robustness_completed=robustness_run is not None,
            lookahead_violation=False,
            created_at=created_at,
        )
        self._methodology.save_promotion_evaluation(promotion)

        robustness_passed = robustness_run.summary.passed if robustness_run is not None else False
        knowledge_status = derive_knowledge_status(
            saved_conclusion.conclusion_state,
            promotion_eligible=promotion.eligible,
            robustness_passed=robustness_passed,
        )
        cohens_d = (
            validation_trial.diagnostics.effect_size.cohens_d
            if validation_trial is not None and validation_trial.diagnostics.effect_size is not None
            else None
        )
        return CorporateNullControlOutcome.create(
            spec_id=spec.spec_id,
            calibration_run_id=calibration_run_id,
            governance_policy_id=policy.governance_policy_id,
            exploratory_trial_id=exploratory_trial.trial_id if exploratory_trial else None,
            validation_trial_id=validation_trial.trial_id if validation_trial else None,
            holdout_trial_id=holdout_trial.trial_id if holdout_trial else None,
            robustness_run_id=robustness_run.robustness_run_id if robustness_run else None,
            conclusion_id=saved_conclusion.conclusion_id,
            promotion_evaluation_id=promotion.promotion_evaluation_id,
            knowledge_status=knowledge_status.value,
            cohens_d=cohens_d,
            blockers=tuple(blockers),
            created_at=created_at,
        )

    def _save_trial(self, trial: ResearchTrial | None) -> ResearchTrial | None:
        if trial is None:
            return None
        existing = self._methodology.trial_by_id(trial.trial_id)
        return existing if existing is not None else self._methodology.save_trial(trial)


@dataclass(frozen=True, slots=True)
class AdvanceCorporateCalibrationRunResult:
    run: CorporateCalibrationRun
    outcome: CorporateNullControlOutcome | None


class AdvanceCorporateCalibrationRun:
    def __init__(
        self,
        *,
        repository: CorporateCalibrationDepthRepository,
        methodology: ResearchMethodologyCollaborator,
        panel: CorporatePanel,
        period_policy: ResearchPeriodPolicy,
    ) -> None:
        self._repository = repository
        self._runner = RunSingleCorporateNullControl(
            methodology=methodology, panel=panel, period_policy=period_policy
        )

    def execute(
        self, *, plan: CorporateCalibrationPlan, created_at: datetime
    ) -> AdvanceCorporateCalibrationRunResult:
        run = self._repository.run_by_calibration(plan.calibration_id)
        if run is None:
            raise CorporateCalibrationDepthApplicationError("CALIBRATION_RUN_NOT_REGISTERED")
        if run.status is CalibrationRunStatus.COMPLETE:
            return AdvanceCorporateCalibrationRunResult(run, None)
        remaining = [sid for sid in run.spec_ids if sid not in run.completed_spec_ids]
        if not remaining:
            # Every declared spec is already completed; the last advance() call that
            # processed the final spec always passed is_final=True, so status is already
            # COMPLETE by construction -- this branch is an unreachable-in-practice guard,
            # never re-advancing (and never duplicating) an already-completed cursor.
            return AdvanceCorporateCalibrationRunResult(run, None)
        next_spec_id = remaining[0]
        spec = self._repository.spec_by_id(next_spec_id)
        if spec is None:
            raise CorporateCalibrationDepthApplicationError(
                "CALIBRATION_NULL_CONTROL_SPEC_NOT_FOUND"
            )
        outcome = self._runner.execute(
            spec, plan=plan, calibration_run_id=run.calibration_run_id, created_at=created_at
        )
        existing_outcome = self._repository.outcome_by_spec(spec.spec_id)
        saved_outcome = existing_outcome or self._repository.save_outcome(outcome)
        trials_added = sum(
            1
            for trial_id in (
                saved_outcome.exploratory_trial_id,
                saved_outcome.validation_trial_id,
                saved_outcome.holdout_trial_id,
            )
            if trial_id is not None
        )
        is_final = len(remaining) == 1
        updated_run = run.advance(
            spec_id=next_spec_id, trials_added=trials_added, is_final=is_final, at=created_at
        )
        return AdvanceCorporateCalibrationRunResult(
            self._repository.save_run(updated_run), saved_outcome
        )


# ==========================================================================
# Section 2/10 (plan registration): reads only already-retained v3 evidence
# (agenda, hypotheses, executions, trials) -- never rebuilds or reruns any
# of it.
# ==========================================================================


class RegisterCorporateCalibrationPlan:
    def __init__(
        self,
        *,
        repository: CorporateCalibrationDepthRepository,
        corporate_market_repository: CorporateMarketResearchRepository,
        methodology: ResearchMethodologyCollaborator,
    ) -> None:
        self._repository = repository
        self._corporate_market = corporate_market_repository
        self._methodology = methodology

    def execute(self, *, agenda_id: str, registered_at: datetime) -> CorporateCalibrationPlan:
        for existing in self._repository.list_plans():
            if existing.source_agenda_id == agenda_id:
                return existing
        agenda = self._corporate_market.agenda_by_id(agenda_id)
        if agenda is None:
            raise CorporateCalibrationDepthApplicationError(
                "SOURCE_CORPORATE_MARKET_AGENDA_NOT_FOUND"
            )
        hypotheses = self._corporate_market.hypotheses_for_agenda(agenda_id)
        genuine = [item for item in hypotheses if not item.is_negative_control]
        controls = [item for item in hypotheses if item.is_negative_control]
        executions = self._corporate_market.executions_for_agenda(agenda_id)
        trials_by_hypothesis: dict[str, ResearchTrial | None] = {}
        for execution in executions:
            trial = (
                self._methodology.trial_by_id(execution.validation_trial_id)
                if execution.validation_trial_id
                else None
            )
            trials_by_hypothesis[execution.hypothesis_id] = trial
        strata = build_corporate_strata_inventory(
            genuine_hypotheses=genuine,
            control_hypotheses=controls,
            trials_by_hypothesis=trials_by_hypothesis,
        )
        plan = build_corporate_calibration_plan(
            source_agenda_id=agenda.agenda_id,
            corporate_dataset_id=agenda.corporate_dataset_id,
            equity_snapshot_id=agenda.equity_snapshot_id,
            replay_run_id=agenda.replay_run_id,
            cross_market_dataset_id=agenda.cross_market_dataset_id,
            stratum_targets=strata,
            registered_at=registered_at,
        )
        return self._repository.save_plan(plan)


# ==========================================================================
# Sections 15/16/17: non-destructive reassessment of the 20 genuine v3
# hypotheses, the 17 original v3 negative controls, and (for the four
# conditional/interaction genuine hypotheses only) an incremental-
# contribution calibration slice.
# ==========================================================================


def _conclusion_state_from_evidence(
    *,
    validation_trial: ResearchTrial | None,
    holdout_trial: ResearchTrial | None,
    robustness_run: RobustnessCheckRun | None,
) -> ResearchConclusionState:
    requirement = resolve_minimum_sample_requirement(phase=ResearchPhase.VALIDATION)
    effective = (
        validation_trial.diagnostics.overlap.effective_sample_count
        if validation_trial is not None
        else Decimal(0)
    )
    sample_quality = (
        SampleQualityState.SUFFICIENT
        if validation_trial is not None
        and validation_trial.diagnostics.overlap.inference_available
        and effective >= requirement.minimum_effective_sample
        else SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE
    )
    return derive_conclusion_state(
        sample_quality=sample_quality,
        robustness_summary=robustness_run.summary if robustness_run else None,
        validation_beats_benchmark=_beats_benchmark(validation_trial),
        holdout_beats_benchmark=_beats_benchmark(holdout_trial),
        multiple_testing_corrected_significant=None,
    )


def _grade_for_population(
    subject_id: str,
    *,
    subject_kind: str,
    statistic: Decimal | None,
    matched_null_statistics: Sequence[Decimal],
    pooled_null_statistics: Sequence[Decimal],
    matched_stratum: str,
    conclusion_state: ResearchConclusionState,
    validation_trial: ResearchTrial | None,
    holdout_trial: ResearchTrial | None,
    robustness_passed: bool | None,
    regime_concentration: str,
    corrected_significant: bool | None,
    created_at: datetime,
) -> tuple[MatchedCalibrationResult, EvidenceGrade]:
    matched = MatchedCalibrationResult.evaluate(
        statistic=statistic,
        matched_null_statistics=tuple(matched_null_statistics),
        pooled_null_statistics=tuple(pooled_null_statistics),
        matched_stratum=matched_stratum,
    )
    effect_meets_floor = statistic is not None and abs(statistic) >= Decimal("0.20")
    raw_p = trial_p_value(validation_trial) if validation_trial is not None else None
    raw_significant = raw_p is not None and raw_p <= Decimal("0.05")
    profile = EvidenceProfile.create(
        subject_id=subject_id,
        subject_kind=subject_kind,
        benchmark_relative_effect=(
            validation_trial.benchmark.relative_result
            if validation_trial is not None and validation_trial.benchmark
            else None
        ),
        cohens_d=statistic,
        validation_beats_benchmark=_beats_benchmark(validation_trial),
        holdout_result=(
            "BEATS_BENCHMARK" if _beats_benchmark(holdout_trial) else "DOES_NOT_BEAT_OR_UNAVAILABLE"
        ),
        walk_forward_consistency=None,
        robustness_passed=robustness_passed,
        corrected_significant=corrected_significant,
        effective_sample=(
            validation_trial.diagnostics.overlap.effective_sample_count
            if validation_trial is not None
            else Decimal(0)
        ),
        regime_concentration=regime_concentration,
        null_percentile=matched.matched_percentile,
        practical_significance=classify_practical_significance(
            statistically_significant=raw_significant, meets_effect_floor=effect_meets_floor
        ),
        created_at=created_at,
    )
    grade = derive_evidence_grade(
        conclusion_state=conclusion_state,
        profile=profile,
        effect_meets_floor=effect_meets_floor,
        policy=EVIDENCE_GRADE_POLICY_V1,
    )
    return matched, grade


def _incremental_null_population(
    stratum: str,
    *,
    repository: CorporateCalibrationDepthRepository,
    methodology: ResearchMethodologyCollaborator,
) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for spec in repository.specs_for_stratum(stratum):
        outcome = repository.outcome_by_spec(spec.spec_id)
        if outcome is None or outcome.validation_trial_id is None:
            continue
        trial = methodology.trial_by_id(outcome.validation_trial_id)
        if trial is not None and trial.benchmark is not None:
            values.append(trial.benchmark.relative_result)
    return tuple(values)


def _diagnose_weak_support(
    outcomes: Sequence[HypothesisRunOutcome], *, weak_rate_by_stratum: Mapping[str, Decimal]
) -> str:
    """Section 13's explicit WEAK_SUPPORT investigation: classifies every
    v3 genuine hypothesis or negative control that reconstructs to
    ``WEAK_SUPPORT`` by ``domain.corporate_calibration_depth.classify_
    weak_support_cause`` (a fixed, deterministic rule -- never tuned
    because the resulting distribution looks unattractive)."""

    requirement = resolve_minimum_sample_requirement(phase=ResearchPhase.VALIDATION)
    causes: dict[WeakSupportCause, int] = {}
    weak_subjects = 0
    for outcome in outcomes:
        state = _conclusion_state_from_evidence(
            validation_trial=outcome.validation_trial,
            holdout_trial=outcome.holdout_trial,
            robustness_run=outcome.robustness_run,
        )
        if state is not ResearchConclusionState.WEAK_SUPPORT:
            continue
        weak_subjects += 1
        stratum = corporate_matched_stratum_key(outcome.hypothesis)
        effective = (
            outcome.validation_trial.diagnostics.overlap.effective_sample_count
            if outcome.validation_trial is not None
            else Decimal(0)
        )
        inference_available = bool(
            outcome.validation_trial is not None
            and outcome.validation_trial.diagnostics.overlap.inference_available
        )
        cause = classify_weak_support_cause(
            effective_sample=effective,
            minimum_effective_sample=requirement.minimum_effective_sample,
            inference_available=inference_available,
            validation_beats_benchmark=_beats_benchmark(outcome.validation_trial),
            holdout_beats_benchmark=_beats_benchmark(outcome.holdout_trial),
            robustness_passed=(
                outcome.robustness_run.summary.passed if outcome.robustness_run else None
            ),
            control_weak_rate_for_stratum=weak_rate_by_stratum.get(stratum),
        )
        causes[cause] = causes.get(cause, 0) + 1
    if weak_subjects == 0:
        return "NO_WEAK_SUPPORT_CONCLUSIONS_RECONSTRUCTED_AMONG_V3_GENUINE_OR_CONTROL_HYPOTHESES"
    parts = ", ".join(
        f"{cause.value}:{count}"
        for cause, count in sorted(causes.items(), key=lambda item: item[0].value)
    )
    return (
        f"{weak_subjects} v3 hypotheses/controls reconstruct to WEAK_SUPPORT (5 genuine + 4 "
        f"control per the original v3 quality report); calibration-depth diagnosis by cause: "
        f"{parts}."
    )


class ReassessCorporateEvidence:
    def __init__(
        self,
        *,
        repository: CorporateCalibrationDepthRepository,
        corporate_market_repository: CorporateMarketResearchRepository,
        methodology: ResearchMethodologyCollaborator,
        panel: CorporatePanel,
        period_policy: ResearchPeriodPolicy,
    ) -> None:
        self._repository = repository
        self._corporate_market = corporate_market_repository
        self._methodology = methodology
        self._panel = panel
        self._period_policy = period_policy
        self.last_weak_support_note: str = ""

    def execute(
        self,
        *,
        plan: CorporateCalibrationPlan,
        matched_pools: Mapping[str, Sequence[tuple[str, Decimal]]],
        pooled: Sequence[Decimal],
        created_at: datetime,
        weak_rate_by_stratum: Mapping[str, Decimal] | None = None,
    ) -> tuple[CorporateReassessment, ...]:
        existing = {
            item.subject_id: item
            for item in self._repository.reassessments_for_calibration(plan.calibration_id)
        }
        agenda_id = plan.source_agenda_id
        hypotheses = self._corporate_market.hypotheses_for_agenda(agenda_id)
        executions = self._corporate_market.executions_for_agenda(agenda_id)
        executions_by_hypothesis = {item.hypothesis_id: item for item in executions}
        outcomes: list[HypothesisRunOutcome] = []
        for hypothesis in hypotheses:
            execution = executions_by_hypothesis.get(hypothesis.hypothesis_id)
            if execution is None:
                continue
            outcome = reconstruct_hypothesis_outcome(execution, hypothesis, self._methodology)
            if outcome is not None:
                outcomes.append(outcome)
        trial_families = build_trial_families(outcomes, panel=self._panel, created_at=created_at)
        original_classifications = {
            item.subject_id: item
            for item in classify_hypothesis_calibration(
                outcomes, trial_families, agenda_id=agenda_id, created_at=created_at
            )
        }
        self.last_weak_support_note = _diagnose_weak_support(
            outcomes, weak_rate_by_stratum=weak_rate_by_stratum or {}
        )

        results: list[CorporateReassessment] = []
        for outcome in outcomes:
            hypothesis = outcome.hypothesis
            if hypothesis.hypothesis_id in existing:
                results.append(existing[hypothesis.hypothesis_id])
                continue
            execution = executions_by_hypothesis[hypothesis.hypothesis_id]
            if execution.conclusion_id is None:
                continue
            stratum = corporate_matched_stratum_key(hypothesis)
            statistic = (
                outcome.validation_trial.diagnostics.effect_size.cohens_d
                if outcome.validation_trial is not None
                and outcome.validation_trial.diagnostics.effect_size is not None
                else None
            )
            conclusion_state = _conclusion_state_from_evidence(
                validation_trial=outcome.validation_trial,
                holdout_trial=outcome.holdout_trial,
                robustness_run=outcome.robustness_run,
            )
            robustness_passed = (
                outcome.robustness_run.summary.passed if outcome.robustness_run else None
            )
            regime = outcome.stability.state.value if outcome.stability else "INSUFFICIENT_EVIDENCE"

            if hypothesis.is_negative_control:
                subject_kind = CorporateReassessmentSubjectKind.NEGATIVE_CONTROL
                original_grade_text = "NOT_PREVIOUSLY_GRADED_AS_CALIBRATION_SUBJECT"
                original_matched_count = 0
                corrected_significant: bool | None = None
            else:
                subject_kind = CorporateReassessmentSubjectKind.HYPOTHESIS
                original = original_classifications.get(hypothesis.hypothesis_id)
                original_grade_text = (
                    original.evidence_grade_v2.value
                    if original is not None
                    else EvidenceGrade.EXPLORATORY.value
                )
                original_matched_count = (
                    original.matched.matched_null_count if original is not None else 0
                )
                corrected_significant = (
                    original.corrected_significant if original is not None else None
                )

            stratum_pool = matched_pools.get(stratum, ())
            # A genuine hypothesis's own statistic was never added to matched_pools (only
            # control statistics are), so no exclusion applies to it; an original v3 control
            # *was* merged into its own stratum's pool and must be left out of its own grade.
            matched_null_statistics = (
                _leave_one_out(stratum_pool, exclude_id=hypothesis.hypothesis_id)
                if hypothesis.is_negative_control
                else tuple(value for _subject_id, value in stratum_pool)
            )
            new_matched, new_grade = _grade_for_population(
                hypothesis.hypothesis_id,
                subject_kind=("NULL_CONTROL" if hypothesis.is_negative_control else "HYPOTHESIS"),
                statistic=statistic,
                matched_null_statistics=matched_null_statistics,
                pooled_null_statistics=pooled,
                matched_stratum=stratum,
                conclusion_state=conclusion_state,
                validation_trial=outcome.validation_trial,
                holdout_trial=outcome.holdout_trial,
                robustness_passed=robustness_passed,
                regime_concentration=regime,
                corrected_significant=corrected_significant,
                created_at=created_at,
            )

            incremental_count: int | None = None
            incremental_percentile: Decimal | None = None
            incremental_verdict: IncrementalCalibrationVerdict | None = None
            if (
                not hypothesis.is_negative_control
                and hypothesis.secondary_context_dimension is not None
            ):
                incremental: IncrementalContributionResult | None = (
                    compute_incremental_contribution(
                        hypothesis, panel=self._panel, period_policy=self._period_policy
                    )
                )
                null_deltas = _incremental_null_population(
                    stratum, repository=self._repository, methodology=self._methodology
                )
                incremental_count = len(null_deltas)
                if (
                    incremental is None
                    or incremental.combined_effect is None
                    or incremental_count < MINIMUM_MATCHED_NULL_COUNT
                ):
                    incremental_verdict = IncrementalCalibrationVerdict.INSUFFICIENT_CALIBRATION
                else:
                    incremental_percentile = null_calibration_percentile(
                        statistic=abs(incremental.combined_effect),
                        null_statistics=tuple(abs(value) for value in null_deltas),
                    )
                    if incremental_percentile is None:
                        incremental_verdict = IncrementalCalibrationVerdict.INSUFFICIENT_CALIBRATION
                    elif (
                        incremental_percentile >= Decimal("0.90")
                        and incremental.incremental_delta_present
                    ):
                        incremental_verdict = IncrementalCalibrationVerdict.GENUINELY_INCREMENTAL
                    elif incremental_percentile < Decimal("0.90"):
                        incremental_verdict = IncrementalCalibrationVerdict.ORDINARY_NULL_VARIATION
                    else:
                        incremental_verdict = (
                            IncrementalCalibrationVerdict.REDUNDANT_CORPORATE_INFORMATION
                        )

            sufficiency_text = (
                "sufficient" if new_matched.sufficient else CALIBRATION_STRATUM_INSUFFICIENT
            )
            rationale = (
                f"{subject_kind.value} {hypothesis.hypothesis_id}: original stratum {stratum} "
                f"carried {original_matched_count} matched v3 controls (grade "
                f"{original_grade_text}); the calibration-depth battery raises the matched "
                f"population to {new_matched.matched_null_count} controls ({sufficiency_text}), "
                f"matched percentile {new_matched.matched_percentile}, new grade {new_grade.value}."
            )
            item = CorporateReassessment.create(
                calibration_id=plan.calibration_id,
                subject_id=hypothesis.hypothesis_id,
                subject_kind=subject_kind,
                matched_stratum=stratum,
                original_conclusion_id=execution.conclusion_id,
                original_conclusion_state=conclusion_state.value,
                original_evidence_grade=original_grade_text,
                original_matched_null_count=original_matched_count,
                new_matched_null_count=new_matched.matched_null_count,
                new_matched_percentile=new_matched.matched_percentile,
                new_pooled_null_count=new_matched.pooled_null_count,
                new_evidence_grade=new_grade,
                cohens_d=statistic,
                rationale=rationale,
                created_at=created_at,
                incremental_null_delta_count=incremental_count,
                incremental_null_delta_percentile=incremental_percentile,
                incremental_verdict=incremental_verdict,
            )
            results.append(self._repository.save_reassessment(item))
        return tuple(results)


# ==========================================================================
# Section 23: requalification of the 13 retained v3 follow-up proposals.
# Never executes any of them.
# ==========================================================================


class RequalifyCorporateFollowUps:
    def __init__(
        self,
        *,
        repository: CorporateCalibrationDepthRepository,
        corporate_market_repository: CorporateMarketResearchRepository,
    ) -> None:
        self._repository = repository
        self._corporate_market = corporate_market_repository

    def execute(
        self,
        *,
        plan: CorporateCalibrationPlan,
        reassessments: Sequence[CorporateReassessment],
        created_at: datetime,
    ) -> tuple[FollowUpQualification, ...]:
        existing = {
            item.follow_up_id: item
            for item in self._repository.follow_up_qualifications_for_calibration(
                plan.calibration_id
            )
        }
        reassessment_by_subject = {item.subject_id: item for item in reassessments}
        follow_ups: tuple[FollowUpProposal, ...] = self._corporate_market.list_follow_ups()
        results: list[FollowUpQualification] = []
        for follow_up in follow_ups:
            if follow_up.follow_up_id in existing:
                results.append(existing[follow_up.follow_up_id])
                continue
            reassessment = reassessment_by_subject.get(follow_up.parent_hypothesis_id)
            if reassessment is None:
                matched = MatchedCalibrationResult(
                    matched_stratum="UNKNOWN_PARENT_HYPOTHESIS",
                    matched_null_count=0,
                    sufficient=False,
                    matched_percentile=None,
                    pooled_null_count=0,
                    pooled_percentile=None,
                )
                qualification = FollowUpQualificationV2.CALIBRATION_INSUFFICIENT
                grade = EvidenceGrade.EXPLORATORY
                design = ResearchDesign.CROSS_SECTIONAL
                horizon = 1
                rationale = (
                    f"Parent hypothesis {follow_up.parent_hypothesis_id} was not reassessed under "
                    "corporate calibration depth; cannot requalify."
                )
            else:
                matched = MatchedCalibrationResult(
                    matched_stratum=reassessment.matched_stratum,
                    matched_null_count=reassessment.new_matched_null_count,
                    sufficient=reassessment.new_matched_null_count >= MINIMUM_MATCHED_NULL_COUNT,
                    matched_percentile=reassessment.new_matched_percentile,
                    pooled_null_count=reassessment.new_pooled_null_count,
                    pooled_percentile=None,
                )
                grade = reassessment.new_evidence_grade
                stratum_parts = reassessment.matched_stratum.split("|")
                design = ResearchDesign(stratum_parts[0])
                horizon = int(stratum_parts[2][1:])
                qualification = classify_follow_up_v2(matched=matched, evidence_grade=grade)
                sufficiency_text = (
                    "sufficient" if matched.sufficient else CALIBRATION_STRATUM_INSUFFICIENT
                )
                rationale = (
                    f"Parent hypothesis {follow_up.parent_hypothesis_id} calibration-depth "
                    f"matched stratum {matched.matched_stratum} ({matched.matched_null_count} "
                    f"matched controls, {sufficiency_text}); matched percentile "
                    f"{matched.matched_percentile}; evidence grade {grade.value}; proposed "
                    f"follow-up kind {follow_up.kind.value}."
                )
            item = FollowUpQualification.create(
                calibration_id=plan.calibration_id,
                follow_up_id=follow_up.follow_up_id,
                parent_hypothesis_id=follow_up.parent_hypothesis_id,
                research_design=design,
                horizon_sessions=horizon,
                matched=matched,
                evidence_grade_v2=grade,
                qualification=qualification,
                rationale=rationale,
                created_at=created_at,
            )
            results.append(self._repository.save_follow_up_qualification(item))
        return tuple(results)


# ==========================================================================
# Section 12/18/19/20/21/22: rate computation, sector/filing-event/temporal
# diagnostics, policy recommendation, and the retained quality report.
# ==========================================================================


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal(0)
    return bound_statistical_decimal(Decimal(numerator) / Decimal(denominator))


@dataclass(frozen=True, slots=True)
class _ControlRow:
    spec: CorporateNullControlSpec
    outcome: CorporateNullControlOutcome
    validation_trial: ResearchTrial | None
    holdout_trial: ResearchTrial | None
    robustness_run: RobustnessCheckRun | None
    conclusion: ResearchConclusion | None
    promotion_eligible: bool


def _raw_significant(row: _ControlRow) -> bool:
    trial = row.validation_trial
    if trial is None:
        return False
    p_value = trial_p_value(trial)
    return p_value is not None and p_value <= Decimal("0.05")


def _bh_survivor(row: _ControlRow, bh_by_trial: Mapping[str, object]) -> bool:
    trial = row.validation_trial
    if trial is None:
        return False
    item = bh_by_trial.get(trial.trial_id)
    return item is not None and bool(getattr(item, "rejected_null", False))


def _representative_trial_date(
    trial: ResearchTrial | None, *, ordered_dates: Sequence[str]
) -> str | None:
    """``ResearchTrial`` retains only the deduplicated ``evaluation_ids``
    set plus a summary ``session_date_range`` (min, max), never the full
    per-evaluation date list -- so an exact median is not retrievable
    after construction. This approximates it as the panel-calendar date
    roughly midway between the trial's own retained start/end dates."""

    if trial is None:
        return None
    start, end = trial.session_date_range
    if start not in ordered_dates or end not in ordered_dates:
        return end if end in ordered_dates else (start if start in ordered_dates else None)
    start_index = ordered_dates.index(start)
    end_index = ordered_dates.index(end)
    return ordered_dates[(start_index + end_index) // 2]


def _volatility_bucket(label: str | None) -> str | None:
    if label is None:
        return None
    return (
        "ELEVATED_OR_HIGH_VOLATILITY"
        if label in ("ELEVATED_VOLATILITY", "HIGH_VOLATILITY")
        else "NORMAL_OR_LOW_VOLATILITY"
    )


class FinalizeCorporateCalibration:
    def __init__(
        self,
        *,
        repository: CorporateCalibrationDepthRepository,
        corporate_market_repository: CorporateMarketResearchRepository,
        methodology: ResearchMethodologyCollaborator,
        panel: CorporatePanel,
        period_policy: ResearchPeriodPolicy,
    ) -> None:
        self._repository = repository
        self._corporate_market = corporate_market_repository
        self._methodology = methodology
        self._panel = panel
        self._period_policy = period_policy

    def execute(
        self,
        *,
        plan: CorporateCalibrationPlan,
        run: CorporateCalibrationRun,
        created_at: datetime,
    ) -> CorporateCalibrationReport:
        existing_report = self._repository.report_by_calibration(plan.calibration_id)
        if existing_report is not None:
            return existing_report

        specs = {
            item.spec_id: item
            for item in self._repository.specs_for_calibration(plan.calibration_id)
        }
        raw_outcomes = self._repository.outcomes_for_run(run.calibration_run_id)
        rows: list[_ControlRow] = []
        for outcome in raw_outcomes:
            spec = specs.get(outcome.spec_id)
            if spec is None:
                continue
            validation_trial = (
                self._methodology.trial_by_id(outcome.validation_trial_id)
                if outcome.validation_trial_id
                else None
            )
            holdout_trial = (
                self._methodology.trial_by_id(outcome.holdout_trial_id)
                if outcome.holdout_trial_id
                else None
            )
            robustness_run = (
                self._methodology.robustness_run_by_id(outcome.robustness_run_id)
                if outcome.robustness_run_id
                else None
            )
            conclusion = (
                self._methodology.conclusion_by_id(outcome.conclusion_id)
                if outcome.conclusion_id
                else None
            )
            promotion = (
                self._methodology.promotion_evaluation_by_id(outcome.promotion_evaluation_id)
                if outcome.promotion_evaluation_id
                and hasattr(self._methodology, "promotion_evaluation_by_id")
                else None
            )
            rows.append(
                _ControlRow(
                    spec,
                    outcome,
                    validation_trial,
                    holdout_trial,
                    robustness_run,
                    conclusion,
                    bool(promotion is not None and getattr(promotion, "eligible", False)),
                )
            )

        matched_pools: dict[str, list[tuple[str, Decimal]]] = {}
        pooled: list[Decimal] = []
        for row in rows:
            if row.outcome.cohens_d is not None:
                matched_pools.setdefault(row.spec.matched_stratum, []).append(
                    (row.spec.spec_id, row.outcome.cohens_d)
                )
                pooled.append(row.outcome.cohens_d)
        # Merge in the already-retained original v3 control statistics (section 3/15: the
        # deepened matched population is the original evidence *plus* the new calibration-depth
        # battery, never the new battery alone -- stratum 1's 16 original controls must still
        # count toward its final matched-null total, not be silently dropped). Every entry stays
        # tagged by its own subject id so grading a subject against "everyone else" (section 16's
        # leave-one-out below) never includes that subject's own contribution.
        original_controls = [
            item
            for item in self._corporate_market.hypotheses_for_agenda(plan.source_agenda_id)
            if item.is_negative_control
        ]
        original_trials_by_hypothesis: dict[str, ResearchTrial | None] = {}
        for control in original_controls:
            execution = self._corporate_market.execution_by_hypothesis(control.hypothesis_id)
            trial = (
                self._methodology.trial_by_id(execution.validation_trial_id)
                if execution is not None and execution.validation_trial_id
                else None
            )
            original_trials_by_hypothesis[control.hypothesis_id] = trial
        for stratum_key in {row.spec.matched_stratum for row in rows}:
            original_pool = _original_matched_null_pool_tagged(
                stratum_key,
                control_hypotheses=original_controls,
                trials_by_hypothesis=original_trials_by_hypothesis,
            )
            if original_pool:
                matched_pools.setdefault(stratum_key, []).extend(original_pool)
                pooled.extend(value for _hypothesis_id, value in original_pool)

        raw_results = tuple(
            TrialTestResult(
                row.validation_trial.trial_id, row.spec.spec_id, trial_p_value(row.validation_trial)
            )
            for row in rows
            if row.validation_trial is not None
        )
        bh_corrected = apply_benjamini_hochberg_correction(
            raw_results, false_discovery_rate=Decimal("0.10")
        )
        bh_by_trial = {item.trial_id: item for item in bh_corrected}

        graded: dict[str, EvidenceGrade] = {}
        for row in rows:
            conclusion_state = (
                row.conclusion.conclusion_state
                if row.conclusion is not None
                else ResearchConclusionState.INSUFFICIENT_EVIDENCE
            )
            corrected = (
                bh_by_trial[row.validation_trial.trial_id].rejected_null
                if row.validation_trial is not None and row.validation_trial.trial_id in bh_by_trial
                else None
            )
            _matched, grade = _grade_for_population(
                row.spec.spec_id,
                subject_kind="NULL_CONTROL",
                statistic=row.outcome.cohens_d,
                matched_null_statistics=_leave_one_out(
                    matched_pools.get(row.spec.matched_stratum, ()), exclude_id=row.spec.spec_id
                ),
                pooled_null_statistics=tuple(pooled),
                matched_stratum=row.spec.matched_stratum,
                conclusion_state=conclusion_state,
                validation_trial=row.validation_trial,
                holdout_trial=row.holdout_trial,
                robustness_passed=(
                    row.robustness_run.summary.passed if row.robustness_run else None
                ),
                regime_concentration="NOT_EVALUATED_FOR_CALIBRATION_DEPTH_CONTROLS",
                corrected_significant=corrected,
                created_at=created_at,
            )
            graded[row.spec.spec_id] = grade

        total_controls = len(rows)
        testable = [row for row in rows if row.validation_trial is not None]
        validation_candidates = len(testable)
        raw_positive = sum(1 for row in rows if _raw_significant(row))
        bh_survivors = sum(1 for row in rows if _bh_survivor(row, bh_by_trial))
        holdout_survivors = sum(1 for row in rows if _beats_benchmark(row.holdout_trial) is True)
        robustness_survivors = sum(
            1
            for row in rows
            if row.robustness_run is not None and row.robustness_run.summary.passed
        )
        weak_count = sum(
            1
            for row in rows
            if row.conclusion is not None
            and row.conclusion.conclusion_state is ResearchConclusionState.WEAK_SUPPORT
        )
        supported_count = sum(
            1
            for row in rows
            if row.conclusion is not None
            and row.conclusion.conclusion_state is ResearchConclusionState.SUPPORTED
        )
        promotion_eligible_count = sum(1 for row in rows if row.promotion_eligible)
        suggestive_count = sum(1 for grade in graded.values() if grade is EvidenceGrade.SUGGESTIVE)
        validated_or_strong_count = sum(
            1
            for grade in graded.values()
            if grade in (EvidenceGrade.VALIDATED, EvidenceGrade.STRONG)
        )

        overall_summary = CalibrationRateSummary.compute(
            total_controls=total_controls,
            validation_candidates=validation_candidates,
            raw_positive_findings=raw_positive,
            bh_fdr_survivors=bh_survivors,
            holdout_survivors=holdout_survivors,
            robustness_survivors=robustness_survivors,
            weak_count=weak_count,
            supported_count=supported_count,
            promotion_eligible_count=promotion_eligible_count,
        )
        false_suggestive_rate = _rate(suggestive_count, total_controls)
        false_validated_rate = _rate(validated_or_strong_count, total_controls)

        design_breakdown_labels = tuple(sorted({row.spec.research_design.value for row in rows}))
        stability_slices = self._stability_slices(
            rows, bh_by_trial=bh_by_trial, calibration_id=plan.calibration_id, created_at=created_at
        )
        for slice_item in stability_slices:
            self._repository_save_stability_slice_if_supported(slice_item)
        sector_note = self._sector_concentration_note(rows)
        filing_note = self._filing_event_sparsity_note(rows, bh_by_trial=bh_by_trial)
        weak_rate_by_stratum = self._weak_rate_by_stratum(rows)

        reassessor = ReassessCorporateEvidence(
            repository=self._repository,
            corporate_market_repository=self._corporate_market,
            methodology=self._methodology,
            panel=self._panel,
            period_policy=self._period_policy,
        )
        reassessments = reassessor.execute(
            plan=plan,
            matched_pools=matched_pools,
            pooled=tuple(pooled),
            created_at=created_at,
            weak_rate_by_stratum=weak_rate_by_stratum,
        )
        weak_support_note = reassessor.last_weak_support_note
        genuine_reassessments = [
            r
            for r in reassessments
            if r.subject_kind is CorporateReassessmentSubjectKind.HYPOTHESIS
        ]
        control_reassessments = [
            r
            for r in reassessments
            if r.subject_kind is CorporateReassessmentSubjectKind.NEGATIVE_CONTROL
        ]
        control_validated_or_strong = sum(
            1
            for r in control_reassessments
            if r.new_evidence_grade in (EvidenceGrade.VALIDATED, EvidenceGrade.STRONG)
        )
        genuine_changed = sum(1 for r in genuine_reassessments if r.assessment_changed)
        incremental_count = sum(
            1 for r in genuine_reassessments if r.incremental_verdict is not None
        )

        RequalifyCorporateFollowUps(
            repository=self._repository,
            corporate_market_repository=self._corporate_market,
        ).execute(plan=plan, reassessments=reassessments, created_at=created_at)

        policy_v2_justified = false_validated_rate > Decimal(
            "0.05"
        ) or overall_summary.false_promotion_rate > Decimal("0.02")
        policy_v2_id = "CORPORATE_EVIDENCE_GRADE_POLICY_V2" if policy_v2_justified else None
        policy_rationale = (
            f"False-VALIDATED-or-STRONG rate {false_validated_rate} and false-promotion rate "
            f"{overall_summary.false_promotion_rate} among {total_controls} calibration-depth null "
            "controls "
            + (
                "exceed the disclosed 5%/2% review thresholds -- a stricter default evidence "
                "requirement is empirically justified for at least one corporate research "
                f"design; see {policy_v2_id}. EVIDENCE_GRADE_POLICY_V1 is preserved unchanged; "
                "no historical conclusion is retroactively altered."
                if policy_v2_justified
                else "remain within the disclosed 5%/2% review thresholds -- no empirical "
                "justification yet exists for a stricter default corporate evidence policy; "
                "EVIDENCE_GRADE_POLICY_V1 remains in force unchanged."
            )
        )

        report = CorporateCalibrationReport.create(
            calibration_id=plan.calibration_id,
            calibration_run_id=run.calibration_run_id,
            strata_evaluated=len(plan.stratum_targets),
            controls_generated=total_controls,
            controls_per_stratum=plan.controls_per_stratum,
            raw_false_positive_rate=overall_summary.raw_false_positive_rate,
            corrected_false_positive_rate=overall_summary.corrected_false_positive_rate,
            false_weak_rate=overall_summary.false_weak_rate,
            false_suggestive_rate=false_suggestive_rate,
            false_validated_rate=false_validated_rate,
            false_promotion_rate=overall_summary.false_promotion_rate,
            design_breakdown_labels=design_breakdown_labels,
            stability_slice_labels=tuple(
                sorted(f"{s.dimension_kind}:{s.dimension_label}" for s in stability_slices)
            ),
            sector_concentration_note=sector_note,
            filing_event_sparsity_note=filing_note,
            weak_support_diagnostic_note=weak_support_note,
            genuine_reassessment_count=len(genuine_reassessments),
            genuine_assessment_changed_count=genuine_changed,
            control_reassessment_count=len(control_reassessments),
            control_now_validated_or_strong_count=control_validated_or_strong,
            incremental_calibration_count=incremental_count,
            policy_v2_justified=policy_v2_justified,
            policy_v2_id=policy_v2_id,
            policy_recommendation_rationale=policy_rationale,
            limitations=(
                *corporate_calibration_depth_warnings(),
                "EVERY_CONTROLS_OWN_EVIDENCE_GRADE_USES_LEAVE_ONE_OUT_ITS_OWN_STATISTIC_IS_"
                "EXCLUDED_FROM_THE_MATCHED_NULL_POPULATION_IT_IS_COMPARED_AGAINST",
                "STABILITY_SLICES_ARE_A_LIGHTWEIGHT_RAW_CORRECTED_SIGNIFICANCE_RECOMPUTATION_NOT_"
                "A_FULL_GOVERNED_PIPELINE_RERUN_PER_SLICE",
            ),
            created_at=created_at,
        )
        return self._repository.save_report(report)

    def _weak_rate_by_stratum(self, rows: Sequence[_ControlRow]) -> dict[str, Decimal]:
        by_stratum: dict[str, list[_ControlRow]] = {}
        for row in rows:
            by_stratum.setdefault(row.spec.matched_stratum, []).append(row)
        return {
            stratum: _rate(
                sum(
                    1
                    for member in members
                    if member.conclusion is not None
                    and member.conclusion.conclusion_state is ResearchConclusionState.WEAK_SUPPORT
                ),
                len(members),
            )
            for stratum, members in by_stratum.items()
        }

    def _repository_save_stability_slice_if_supported(self, slice_item: StabilitySlice) -> None:
        saver = getattr(self._repository, "save_stability_slice", None)
        if callable(saver):
            saver(slice_item)

    def _stability_slices(
        self,
        rows: Sequence[_ControlRow],
        *,
        bh_by_trial: Mapping[str, object],
        calibration_id: str,
        created_at: datetime,
    ) -> tuple[StabilitySlice, ...]:
        ordered_dates = self._panel.monthly_dates
        by_region: dict[str, list[_ControlRow]] = {}
        by_volatility: dict[str, list[_ControlRow]] = {}
        for row in rows:
            median = _representative_trial_date(row.validation_trial, ordered_dates=ordered_dates)
            if median is None:
                continue
            region = temporal_region_for_date(median, ordered_dates=ordered_dates)
            if region is not None:
                by_region.setdefault(region.value, []).append(row)
            bucket = _volatility_bucket(self._panel.context_label("EQUITY_VOLATILITY", median))
            if bucket is not None:
                by_volatility.setdefault(bucket, []).append(row)

        slices: list[StabilitySlice] = []
        for dimension_kind, groups in (
            ("TIME_PERIOD", by_region),
            ("VOLATILITY_REGIME", by_volatility),
        ):
            for label, members in sorted(groups.items()):
                raw_positive = sum(
                    1
                    for member in members
                    if member.validation_trial is not None
                    and (p := trial_p_value(member.validation_trial)) is not None
                    and p <= Decimal("0.05")
                )
                corrected_positive = sum(
                    1
                    for member in members
                    if member.validation_trial is not None
                    and (item := bh_by_trial.get(member.validation_trial.trial_id)) is not None
                    and getattr(item, "rejected_null", False)
                )
                slices.append(
                    StabilitySlice.create(
                        calibration_id=calibration_id,
                        dimension_kind=dimension_kind,
                        dimension_label=label,
                        control_count=len(members),
                        raw_positive_count=raw_positive,
                        corrected_positive_count=corrected_positive,
                        minimum_adequate_count=MINIMUM_ADEQUATE_CONTROL_COUNT,
                        note=(
                            f"{dimension_kind} slice {label}: {len(members)} "
                            "calibration-depth controls."
                        ),
                        created_at=created_at,
                    )
                )
        return tuple(slices)

    def _sector_concentration_note(self, rows: Sequence[_ControlRow]) -> str:
        cross_sectional_rows = [
            row
            for row in rows
            if row.spec.research_design
            in (ResearchDesign.CROSS_SECTIONAL, ResearchDesign.CHANGE_BASED)
            and row.robustness_run is not None
        ]
        if not cross_sectional_rows:
            return (
                "NO_CROSS_SECTIONAL_OR_CHANGE_BASED_CALIBRATION_DEPTH_CONTROLS_WITH_ROBUSTNESS_"
                "EVIDENCE"
            )
        broke_on_sector = 0
        broke_on_issuer = 0
        for row in cross_sectional_rows:
            assert row.robustness_run is not None
            for result in row.robustness_run.summary.results:
                if result.check_type.value != "ASSET_COHORT_EXCLUSION":
                    continue
                if result.direction_consistent_with_base is False:
                    if "sector" in result.variant_label:
                        broke_on_sector += 1
                    elif "security" in result.variant_label:
                        broke_on_issuer += 1
        total = len(cross_sectional_rows)
        return (
            f"Leave-one-sector-out reversed the direction of {broke_on_sector}/{total} "
            f"calibration-depth cross-sectional/change-based controls; leave-one-issuer-out "
            f"reversed {broke_on_issuer}/{total}. A high rate here would indicate a control's own "
            "apparent effect is itself concentrated in one sector or one mega-cap issuer, the "
            "same diagnostic future genuine findings must clear."
        )

    def _filing_event_sparsity_note(
        self, rows: Sequence[_ControlRow], *, bh_by_trial: Mapping[str, object]
    ) -> str:
        filing_rows = [
            row for row in rows if row.spec.research_design is ResearchDesign.FILING_EVENT
        ]
        if not filing_rows:
            return "NO_FILING_EVENT_CALIBRATION_DEPTH_CONTROLS_RETAINED"
        buckets: dict[EffectiveSampleBucket, list[_ControlRow]] = {}
        for row in filing_rows:
            effective = (
                row.validation_trial.diagnostics.overlap.effective_sample_count
                if row.validation_trial is not None
                else Decimal(0)
            )
            buckets.setdefault(bucket_effective_sample(effective), []).append(row)
        parts: list[str] = []
        for bucket in EffectiveSampleBucket:
            members = buckets.get(bucket, [])
            if not members:
                continue
            corrected_positive = sum(
                1
                for member in members
                if member.validation_trial is not None
                and (item := bh_by_trial.get(member.validation_trial.trial_id)) is not None
                and getattr(item, "rejected_null", False)
            )
            parts.append(
                f"{bucket.value}:{corrected_positive}/{len(members)}_corrected_significant"
            )
        return (
            f"{len(filing_rows)} filing-event calibration-depth controls bucketed by "
            "validation-phase effective sample: "
            + "; ".join(parts)
            + ". Sparse-bucket significance should be treated as "
            "less reliable than adequate-bucket significance at current history depth."
        )


def calibration_depth_run_complete(run: CorporateCalibrationRun) -> bool:
    return run.status is CalibrationRunStatus.COMPLETE


# ==========================================================================
# In-memory repository (tests, and any bounded offline CLI usage).
# ==========================================================================


class InMemoryCorporateCalibrationDepthRepository:
    def __init__(self) -> None:
        self._plans: dict[str, CorporateCalibrationPlan] = {}
        self._runs: dict[str, CorporateCalibrationRun] = {}
        self._specs: dict[str, CorporateNullControlSpec] = {}
        self._outcomes: dict[str, CorporateNullControlOutcome] = {}
        self._reassessments: dict[str, CorporateReassessment] = {}
        self._follow_up_qualifications: dict[str, FollowUpQualification] = {}
        self._reports: dict[str, CorporateCalibrationReport] = {}

    def plan_by_id(self, calibration_id: str) -> CorporateCalibrationPlan | None:
        return self._plans.get(calibration_id)

    def save_plan(self, plan: CorporateCalibrationPlan) -> CorporateCalibrationPlan:
        return self._plans.setdefault(plan.calibration_id, plan)

    def list_plans(self) -> tuple[CorporateCalibrationPlan, ...]:
        return tuple(sorted(self._plans.values(), key=lambda item: item.registered_at))

    def run_by_id(self, calibration_run_id: str) -> CorporateCalibrationRun | None:
        return self._runs.get(calibration_run_id)

    def run_by_calibration(self, calibration_id: str) -> CorporateCalibrationRun | None:
        for run in self._runs.values():
            if run.calibration_id == calibration_id:
                return run
        return None

    def save_run(self, run: CorporateCalibrationRun) -> CorporateCalibrationRun:
        self._runs[run.calibration_run_id] = run
        return run

    def spec_by_id(self, spec_id: str) -> CorporateNullControlSpec | None:
        return self._specs.get(spec_id)

    def save_spec(self, spec: CorporateNullControlSpec) -> CorporateNullControlSpec:
        return self._specs.setdefault(spec.spec_id, spec)

    def specs_for_calibration(self, calibration_id: str) -> tuple[CorporateNullControlSpec, ...]:
        return tuple(
            sorted(
                (item for item in self._specs.values() if item.calibration_id == calibration_id),
                key=lambda item: item.ordinal,
            )
        )

    def specs_for_stratum(self, matched_stratum: str) -> tuple[CorporateNullControlSpec, ...]:
        return tuple(
            item for item in self._specs.values() if item.matched_stratum == matched_stratum
        )

    def outcome_by_spec(self, spec_id: str) -> CorporateNullControlOutcome | None:
        return self._outcomes.get(spec_id)

    def save_outcome(self, outcome: CorporateNullControlOutcome) -> CorporateNullControlOutcome:
        return self._outcomes.setdefault(outcome.spec_id, outcome)

    def outcomes_for_run(self, calibration_run_id: str) -> tuple[CorporateNullControlOutcome, ...]:
        return tuple(
            item
            for item in self._outcomes.values()
            if item.calibration_run_id == calibration_run_id
        )

    def save_reassessment(self, item: CorporateReassessment) -> CorporateReassessment:
        return self._reassessments.setdefault(item.reassessment_id, item)

    def reassessments_for_calibration(
        self, calibration_id: str
    ) -> tuple[CorporateReassessment, ...]:
        return tuple(
            item for item in self._reassessments.values() if item.calibration_id == calibration_id
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

    def report_by_calibration(self, calibration_id: str) -> CorporateCalibrationReport | None:
        for report in self._reports.values():
            if report.calibration_id == calibration_id:
                return report
        return None

    def save_report(self, report: CorporateCalibrationReport) -> CorporateCalibrationReport:
        return self._reports.setdefault(report.report_id, report)
