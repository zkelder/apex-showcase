"""Adapter-free orchestration for Corporate Calibration-Qualified Regime
Follow-Up v1.

Composes, entirely unmodified: ``application.corporate_market_research``'s
``CorporatePanel``/``BuildCorporatePanel``/``HypothesisSample``/``build_
corporate_diagnostics``/``compute_robustness``/``CorporateMarketResearch
Repository``/``ResearchMethodologyCollaborator``/``CorporatePanelBuilder
Collaborator`` for every already-genuine panel/diagnostics/robustness
primitive; ``domain.corporate_regime_followup`` for every genuinely new
follow-up type; ``domain.research_methodology`` for
``compare_to_benchmark``/``compute_effect_size``/``derive_conclusion_state``/
``apply_benjamini_hochberg_correction``/``summarize_robustness``;
``domain.research_calibration`` for ``EvidenceProfile``/``derive_evidence_
grade``; ``domain.research_calibration_v2`` for ``MatchedCalibrationResult``;
``domain.cross_market_research`` for ``derive_stability_state``.

Why this module writes its own small local sample-building helpers
(``_feature_value``, ``_outcome_value``, ``_train_median_threshold``,
``_regime_rows``, ...) instead of importing
``application.corporate_market_research``'s own private (underscore-
prefixed) equivalents: this codebase's own established convention --
``application.corporate_calibration_depth`` states it explicitly and
``application.corporate_market_research`` demonstrates it structurally --
is that cross-module reuse only ever imports *public* names; a dependent
module that needs similar-but-differently-parameterized mechanics writes
its own small, independently reviewable version instead.

No service here has Strategy, Opportunity, PAPER, LIVE, or provider-write
authority; every retained record stays ``authority = "NONE"``. This module
never rewrites the v3 agenda, its calibration-depth reassessment, or the 13
retained follow-up proposals it reads by id; it never generates or executes
any follow-up other than the one, retained
``cross_market_follow_up_622f5a0d3af61dcb7b69bd9b``.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Protocol

from apex.application.corporate_market_research import (
    CorporateMarketResearchRepository,
    CorporatePanel,
    CorporatePanelBuilderCollaborator,
    HypothesisSample,
    ResearchMethodologyCollaborator,
    build_corporate_diagnostics,
    build_hypothesis_sample,
    compute_robustness,
    split_train_validation_holdout,
)
from apex.domain.corporate_calibration_depth import (
    CorporateCalibrationPlan,
    CorporateNullMechanism,
    CorporateReassessment,
)
from apex.domain.corporate_market_research import (
    CorporateFeatureId,
    CorporateHypothesisFamily,
    CorporateOutcomeId,
    CorporatePanelRow,
    GeneratedCorporateHypothesis,
    compute_panel_dependence_diagnostics,
)
from apex.domain.corporate_regime_followup import (
    EFFECT_SIZE_FLOOR,
    FALSE_DISCOVERY_RATE,
    MINIMUM_MATCHED_NULL_COUNT,
    PRIMARY_NULL_CONTROL_BUDGET,
    RAW_SIGNIFICANCE_ALPHA,
    REGIME_GROUP_MINIMUM_DISTINCT_DATES,
    REGIME_GROUP_MINIMUM_ISSUER_COUNT,
    REGIME_GROUP_MINIMUM_RAW_OBSERVATIONS,
    SECONDARY_NULL_CONTROL_BUDGET,
    CorporateRegimeFollowUpResult,
    CorporateRegimeFollowUpSpecification,
    CorporateRegimeNullControl,
    IncrementalContributionVerdict,
    ParentRelationshipComparison,
    RegimeDimensionSelection,
    RegimeGroupEffect,
    RegimeGroupEvidence,
    RegimeIncrementalContribution,
    RegimeInteractionResult,
    RegimeRobustnessAssessment,
    RegimeSplitResult,
    RegimeSplitRole,
    build_knowledge_history,
    classify_incremental_contribution,
    classify_parent_relationship,
    classify_regime_follow_up_conclusion,
)
from apex.domain.cross_market_research import (
    CrossMarketRegimeDimension,
    StabilityReport,
    StabilityState,
    SubperiodResult,
    derive_stability_state,
)
from apex.domain.historical_replay import ResearchPeriodRole
from apex.domain.research_calibration import (
    EvidenceGrade,
    EvidenceProfile,
    classify_practical_significance,
)
from apex.domain.research_calibration_v2 import FollowUpQualification, MatchedCalibrationResult
from apex.domain.research_methodology import (
    BenchmarkComparison,
    BenchmarkKind,
    CorrectedTestResult,
    ResearchConclusionState,
    RobustnessCheckResult,
    RobustnessCheckType,
    SampleQualityState,
    TrialTestResult,
    apply_benjamini_hochberg_correction,
    bound_statistical_decimal,
    compare_to_benchmark,
    compute_effect_size,
    derive_conclusion_state,
    summarize_robustness,
)

# Section 1: the parent hypothesis's own feature-transformation methodology,
# transcribed byte-for-byte from ``domain.corporate_fundamentals._growth``'s
# own ``methodology`` local (that function computes exactly this for every
# ``DerivedFeatureType.YOY_NET_INCOME_GROWTH`` observation it ever builds;
# there is no separate persisted-per-fact methodology row to query
# independent of picking one specific security/date pair, so this constant
# is the resolved, versioned semantics rather than a per-observation fact).
NET_INCOME_GROWTH_YOY_TRANSFORMATION = (
    "NET_INCOME_GROWTH_YOY = (current_period_net_income - prior_year_same_fiscal_period_"
    "net_income) / abs(prior_year_same_fiscal_period_net_income); requires the identical "
    "fiscal period one year earlier; both facts point-in-time visible as of the panel date "
    "(future filings/restatements excluded); undefined (missing) when the prior-year fact is "
    "absent or exactly zero."
)

PARENT_FOLLOW_UP_ID = "cross_market_follow_up_622f5a0d3af61dcb7b69bd9b"
PARENT_HYPOTHESIS_ID = "corporate_market_hypothesis_6d268d63f879f96f113e2737"

FULL_PANEL_SAMPLE_SCOPE_DESCRIPTION = (
    "Uses the full retained historical panel this exact hypothesis's own three "
    "already-completed trials read (TRAIN union VALIDATION union HOLDOUT -- every monthly date "
    "the parent agenda run ever evaluated for this hypothesis), rather than the 11-date "
    "VALIDATION window alone: this is a post-hoc mechanism/stability investigation, not a fresh "
    "confirmatory holdout test (milestone preamble), and the VALIDATION-only window is too thin "
    "for a genuine two-regime-group comparison (only one UPTREND month in eleven). This performs "
    "no *new* holdout exposure: the parent hypothesis's own HOLDOUT phase was already read once, "
    "for this exact hypothesis_id, by its own original run. The ABOVE_TRAIN_MEDIAN feature "
    "threshold is fixed from TRAIN dates only, exactly as the parent computed it -- never "
    "re-derived over a different window."
)


class CorporateRegimeFollowUpApplicationError(RuntimeError):
    pass


# ==========================================================================
# Collaborator protocols
# ==========================================================================


class CorporateCalibrationDepthCollaborator(Protocol):
    """The narrow, read-only slice of ``PostgresCorporateCalibrationDepth
    Repository`` this module depends on -- deliberately the plural
    ``reassessments_for_calibration`` (filtered client-side below), not the
    singular ``reassessment_for_subject`` convenience method the Postgres
    adapter also happens to expose, so the same, already-established
    ``InMemoryCorporateCalibrationDepthRepository`` test double (which only
    implements the plural form) satisfies this Protocol unmodified."""

    def list_plans(self) -> tuple[CorporateCalibrationPlan, ...]: ...
    def reassessments_for_calibration(
        self, calibration_id: str
    ) -> tuple[CorporateReassessment, ...]: ...
    def follow_up_qualifications_for_calibration(
        self, calibration_id: str
    ) -> tuple[FollowUpQualification, ...]: ...


class CorporateRegimeFollowUpRepository(Protocol):
    def specification_by_id(
        self, specification_id: str
    ) -> CorporateRegimeFollowUpSpecification | None: ...
    def specification_by_follow_up(
        self, follow_up_id: str
    ) -> CorporateRegimeFollowUpSpecification | None: ...
    def save_specification(
        self, specification: CorporateRegimeFollowUpSpecification
    ) -> CorporateRegimeFollowUpSpecification: ...

    def save_null_control(
        self, control: CorporateRegimeNullControl
    ) -> CorporateRegimeNullControl: ...
    def controls_for_specification(
        self, specification_id: str, *, role: RegimeSplitRole | None = None
    ) -> tuple[CorporateRegimeNullControl, ...]: ...

    def result_by_specification(
        self, specification_id: str
    ) -> CorporateRegimeFollowUpResult | None: ...
    def save_result(
        self, result: CorporateRegimeFollowUpResult
    ) -> CorporateRegimeFollowUpResult: ...


# ==========================================================================
# Section 1: freeze the exact parent specification.
# ==========================================================================


class CreateCorporateRegimeFollowUpSpecification:
    """Resolves the retained parent hypothesis/conclusion/reassessment/
    qualification and freezes them into one immutable specification.
    Reads only; never mutates the original v3 hypothesis, conclusion, or
    follow-up-proposal rows. Idempotent: a specification already retained
    for this ``follow_up_id`` is returned unchanged rather than rebuilt."""

    def __init__(
        self,
        *,
        repository: CorporateRegimeFollowUpRepository,
        corporate_market_repository: CorporateMarketResearchRepository,
        methodology: ResearchMethodologyCollaborator,
        calibration_depth: CorporateCalibrationDepthCollaborator,
        panel_builder: CorporatePanelBuilderCollaborator,
    ) -> None:
        self._repository = repository
        self._corporate_market = corporate_market_repository
        self._methodology = methodology
        self._calibration_depth = calibration_depth
        self._panel_builder = panel_builder

    def execute(
        self, *, follow_up_id: str, created_at: datetime
    ) -> CorporateRegimeFollowUpSpecification:
        existing = self._repository.specification_by_follow_up(follow_up_id)
        if existing is not None:
            return existing

        follow_up = next(
            (
                item
                for item in self._corporate_market.list_follow_ups()
                if item.follow_up_id == follow_up_id
            ),
            None,
        )
        if follow_up is None:
            raise CorporateRegimeFollowUpApplicationError("FOLLOW_UP_NOT_RETAINED")
        if follow_up.kind.value != "SPLIT_BY_REGIME":
            raise CorporateRegimeFollowUpApplicationError("FOLLOW_UP_KIND_MUST_BE_SPLIT_BY_REGIME")

        hypothesis = self._corporate_market.hypothesis_by_id(follow_up.parent_hypothesis_id)
        if hypothesis is None:
            raise CorporateRegimeFollowUpApplicationError("PARENT_HYPOTHESIS_NOT_RETAINED")
        agenda = self._corporate_market.agenda_by_id(hypothesis.agenda_id)
        if agenda is None:
            raise CorporateRegimeFollowUpApplicationError("PARENT_AGENDA_NOT_RETAINED")

        conclusion = self._methodology.conclusion_by_id(follow_up.parent_conclusion_id)
        if conclusion is None:
            raise CorporateRegimeFollowUpApplicationError("PARENT_CONCLUSION_NOT_RETAINED")
        validation_trial = (
            self._methodology.trial_by_id(conclusion.validation_trial_id)
            if conclusion.validation_trial_id
            else None
        )
        holdout_trial = (
            self._methodology.trial_by_id(conclusion.holdout_trial_id)
            if conclusion.holdout_trial_id
            else None
        )
        if validation_trial is None or holdout_trial is None:
            raise CorporateRegimeFollowUpApplicationError(
                "PARENT_VALIDATION_AND_HOLDOUT_TRIALS_MUST_BE_RETAINED"
            )
        if validation_trial.benchmark is None:
            raise CorporateRegimeFollowUpApplicationError("PARENT_VALIDATION_BENCHMARK_MISSING")

        # Section 1's "original train boundary/effective sample" is
        # recomputed -- not guessed -- from the identical deterministic
        # panel/period-split/sample-building functions (all PUBLIC, reused
        # unmodified) the original agenda run itself used. This is
        # necessary, not merely convenient: the trial ``conclusion.
        # training_trial_id`` actually points to is the EXPLORATORY phase
        # (TRAIN union VALIDATION combined -- see
        # ``application.corporate_market_research.
        # RunSingleCorporateHypothesis``), so its own ``session_date_range``
        # spans both periods; there is no separately persisted TRAIN-only
        # trial to read a boundary from. VALIDATION/HOLDOUT boundaries and
        # samples below stay sourced from the retained trials themselves
        # (the genuine evidence the WEAK_SUPPORT conclusion was actually
        # computed from), never recomputed.
        panel = self._panel_builder.execute(
            corporate_dataset_id=agenda.corporate_dataset_id,
            equity_snapshot_id=agenda.equity_snapshot_id,
            replay_run_id=agenda.replay_run_id,
            cross_market_dataset_id=agenda.cross_market_dataset_id,
        )
        period_policy = split_train_validation_holdout(
            panel.monthly_dates, replay_run_id=agenda.replay_run_id
        )
        if period_policy is None:
            raise CorporateRegimeFollowUpApplicationError("PERIOD_POLICY_UNAVAILABLE")
        train_sample = build_hypothesis_sample(
            hypothesis, panel=panel, period_policy=period_policy, role=ResearchPeriodRole.TRAIN
        )
        if train_sample is None or not train_sample.treatment:
            raise CorporateRegimeFollowUpApplicationError("RECOMPUTED_TRAIN_SAMPLE_EMPTY")
        train_dates_observed = sorted({row.session_date for row in train_sample.treatment})
        train_overlap = compute_panel_dependence_diagnostics(train_sample.treatment)

        # Defense-in-depth: the VALIDATION sample recomputed from this exact
        # frozen dataset/panel lineage must reproduce the retained trial's
        # own *benchmark* population exactly -- every (security, date) row
        # with a genuine outcome observation in the VALIDATION window,
        # independent of the corporate feature or its threshold. A mismatch
        # here means the underlying outcome/security universe genuinely
        # drifted since the original run; fail closed rather than freeze a
        # specification against evidence that no longer matches what was
        # originally retained. The narrower *treatment* count (rows at or
        # above the TRAIN-derived median threshold) is deliberately not
        # asserted exactly equal: it is threshold-sensitive, and the
        # TRAIN-only median itself is never separately persisted from the
        # original run (only the retained EXPLORATORY phase, TRAIN union
        # VALIDATION, exists) -- an infinitesimal difference in the
        # recomputed threshold can move a small number of near-median
        # observations across it without any change to the underlying
        # evidence. Both counts are retained on the specification either
        # way (see ``sample_scope_description``).
        recomputed_validation_sample = build_hypothesis_sample(
            hypothesis, panel=panel, period_policy=period_policy, role=ResearchPeriodRole.VALIDATION
        )
        recomputed_validation_overlap = compute_panel_dependence_diagnostics(
            recomputed_validation_sample.treatment if recomputed_validation_sample else ()
        )
        recomputed_validation_benchmark_count = (
            len(recomputed_validation_sample.benchmark) if recomputed_validation_sample else 0
        )
        if (
            recomputed_validation_benchmark_count
            != validation_trial.benchmark.benchmark_sample_count
        ):
            raise CorporateRegimeFollowUpApplicationError(
                "RECOMPUTED_VALIDATION_OUTCOME_UNIVERSE_DOES_NOT_MATCH_RETAINED_TRIAL_EVIDENCE"
            )
        recomputed_treatment_count = recomputed_validation_overlap.raw_sample_count
        retained_treatment_count = validation_trial.diagnostics.overlap.raw_sample_count
        treatment_count_delta = recomputed_treatment_count - retained_treatment_count
        threshold_sensitive_note = (
            ""
            if recomputed_treatment_count == retained_treatment_count
            else (
                " NOTE: the recomputed VALIDATION treatment (>= TRAIN-median-threshold) row count "
                f"({recomputed_treatment_count}) differs from the originally retained trial's own "
                f"treatment row count ({retained_treatment_count}) by {treatment_count_delta} "
                "row(s); the benchmark (feature-independent outcome) population above is "
                "identical, so this is threshold sensitivity in the never-separately-persisted "
                "TRAIN-only median, not evidence drift -- disclosed, not hidden."
            )
        )

        calibration_plans = self._calibration_depth.list_plans()
        if not calibration_plans:
            raise CorporateRegimeFollowUpApplicationError("NO_RETAINED_CALIBRATION_DEPTH_PLAN")
        calibration_plan = calibration_plans[-1]
        reassessment = next(
            (
                item
                for item in self._calibration_depth.reassessments_for_calibration(
                    calibration_plan.calibration_id
                )
                if item.subject_id == hypothesis.hypothesis_id
            ),
            None,
        )
        if reassessment is None:
            raise CorporateRegimeFollowUpApplicationError("PARENT_REASSESSMENT_NOT_RETAINED")
        qualifications = self._calibration_depth.follow_up_qualifications_for_calibration(
            calibration_plan.calibration_id
        )
        qualification = next(
            (item for item in qualifications if item.follow_up_id == follow_up_id),
            None,
        )
        if qualification is None:
            raise CorporateRegimeFollowUpApplicationError("FOLLOW_UP_QUALIFICATION_NOT_RETAINED")
        if qualification.qualification.value != "CALIBRATION_QUALIFIED":
            raise CorporateRegimeFollowUpApplicationError(
                "ONLY_A_CALIBRATION_QUALIFIED_FOLLOW_UP_MAY_BE_SPECIFIED"
            )
        if reassessment.new_matched_percentile is None:
            # Structurally unreachable for a genuinely CALIBRATION_QUALIFIED
            # follow-up (MatchedCalibrationResult.evaluate only reports
            # ``sufficient`` -- the qualification's own precondition --
            # with a non-None percentile), but guarded explicitly rather
            # than assumed.
            raise CorporateRegimeFollowUpApplicationError(
                "REASSESSMENT_MATCHED_PERCENTILE_MISSING_FOR_A_QUALIFIED_FOLLOW_UP"
            )

        precedent_hypotheses = [
            item
            for item in self._corporate_market.hypotheses_for_agenda(agenda.agenda_id)
            if item.family is CorporateHypothesisFamily.CONDITIONAL_MARKET_REGIME
        ]
        primary_precedent = _one_precedent(
            precedent_hypotheses, dimension="EQUITY_TREND", code="PRIMARY_REGIME_PRECEDENT"
        )
        secondary_precedent = _one_precedent(
            precedent_hypotheses, dimension="EQUITY_VOLATILITY", code="SECONDARY_REGIME_PRECEDENT"
        )

        regime_selections = (
            RegimeDimensionSelection.create(
                split_role=RegimeSplitRole.PRIMARY,
                dimension=CrossMarketRegimeDimension.EQUITY_TREND,
                group_a_name="REGIME_ADVANCING",
                group_a_labels=("STRONG_UPTREND", "UPTREND"),
                group_b_name="REGIME_NOT_ADVANCING",
                group_b_labels=("RANGE_BOUND", "DOWNTREND", "STRONG_DOWNTREND"),
                excluded_labels=("INSUFFICIENT_EVIDENCE",),
                precedent_hypothesis_id=primary_precedent.hypothesis_id,
                precedent_feature_id=primary_precedent.feature_id,
                selection_rationale=(
                    "No concrete regime dimension was proposed with this SPLIT_BY_REGIME follow-up "
                    "(milestone section 3). EQUITY_TREND is derived from the single closest "
                    f"structural precedent already retained in the v3 agenda: hypothesis "
                    f"{primary_precedent.hypothesis_id} pairs {primary_precedent.feature_id.value} "
                    "-- the structurally closest sibling feature to NET_INCOME_GROWTH_YOY (same "
                    "CHANGE_BASED_ELIGIBLE_FEATURE_IDS YoY-growth category, same SUBSEQUENT_RETURN "
                    "outcome, same 20-session horizon, same CROSS_SECTIONAL geometry) -- with "
                    "EQUITY_TREND under CONDITIONAL_RELATIONSHIP_V1. This is the only market-"
                    "context dimension the agenda's own deterministic generator already "
                    "associated with a growth-style feature; every other of the ten "
                    "CrossMarketRegimeDimension members was never examined for this split. "
                    "Group labels collapse "
                    "CrossMarketRegimeDimension's own already-versioned discrete "
                    "TrendStrengthLabel output (CROSS_MARKET_REGIME_RULE_V2, unchanged thresholds) "
                    "by sign; INSUFFICIENT_EVIDENCE dates are excluded from both groups, never "
                    "guessed."
                ),
                null_control_budget=PRIMARY_NULL_CONTROL_BUDGET,
            ),
            RegimeDimensionSelection.create(
                split_role=RegimeSplitRole.SECONDARY,
                dimension=CrossMarketRegimeDimension.EQUITY_VOLATILITY,
                group_a_name="REGIME_ELEVATED_OR_HIGHER_VOLATILITY",
                group_a_labels=("ELEVATED_VOLATILITY", "HIGH_VOLATILITY"),
                group_b_name="REGIME_NOT_ELEVATED_VOLATILITY",
                group_b_labels=("LOW_VOLATILITY", "NORMAL_VOLATILITY"),
                excluded_labels=("INSUFFICIENT_EVIDENCE",),
                precedent_hypothesis_id=secondary_precedent.hypothesis_id,
                precedent_feature_id=secondary_precedent.feature_id,
                selection_rationale=(
                    "Tiny, explicitly-secondary robustness split only (milestone section 3): the "
                    f"other retained CONDITIONAL_MARKET_REGIME hypothesis, "
                    f"{secondary_precedent.hypothesis_id}, pairs "
                    f"{secondary_precedent.feature_id.value} -- also a YoY-change feature -- with "
                    "EQUITY_VOLATILITY. Never used as a substitute primary split, and never "
                    "selected by comparing candidate outcomes against each other."
                ),
                null_control_budget=SECONDARY_NULL_CONTROL_BUDGET,
            ),
        )

        trial_family = (
            self._methodology.trial_family_by_id(conclusion.trial_family_id)
            if conclusion.trial_family_id
            else None
        )
        issuer_count_note = (
            f"{trial_family.asset_count}-issuer" if trial_family is not None else "retained"
        )
        market_scope = (
            f"{', '.join(validation_trial.market_scope)}; {issuer_count_note} genuine "
            f"multi-asset corporate cohort (agenda.cohort_policy_id={agenda.cohort_policy_id})"
        )

        specification = CorporateRegimeFollowUpSpecification.create(
            follow_up_id=follow_up.follow_up_id,
            parent_hypothesis_id=hypothesis.hypothesis_id,
            parent_conclusion_id=conclusion.conclusion_id,
            parent_reassessment_id=reassessment.reassessment_id,
            parent_qualification_id=qualification.assessment_id,
            agenda_id=agenda.agenda_id,
            feature_id=hypothesis.feature_id,
            feature_transformation=NET_INCOME_GROWTH_YOY_TRANSFORMATION,
            outcome_id=hypothesis.outcome_id,
            horizon_sessions=hypothesis.horizon_sessions,
            benchmark_kind=hypothesis.benchmark_kind,
            condition_rule=hypothesis.condition_rule,
            market_scope=market_scope,
            cohort_policy_id=agenda.cohort_policy_id,
            corporate_dataset_id=agenda.corporate_dataset_id,
            equity_snapshot_id=agenda.equity_snapshot_id,
            replay_run_id=agenda.replay_run_id,
            cross_market_dataset_id=agenda.cross_market_dataset_id,
            original_train_start=train_dates_observed[0],
            original_train_end=train_dates_observed[-1],
            original_validation_start=validation_trial.session_date_range[0],
            original_validation_end=validation_trial.session_date_range[1],
            original_holdout_start=holdout_trial.session_date_range[0],
            original_holdout_end=holdout_trial.session_date_range[1],
            original_train_raw_sample=train_overlap.raw_sample_count,
            original_train_effective_sample=train_overlap.effective_sample_count,
            original_validation_raw_sample=validation_trial.diagnostics.overlap.raw_sample_count,
            original_validation_effective_sample=(
                validation_trial.diagnostics.overlap.effective_sample_count
            ),
            original_holdout_raw_sample=holdout_trial.diagnostics.overlap.raw_sample_count,
            original_holdout_effective_sample=holdout_trial.diagnostics.overlap.effective_sample_count,
            original_conclusion_state=conclusion.conclusion_state.value,
            original_validation_beats_benchmark=validation_trial.benchmark.beats_benchmark,
            original_holdout_beats_benchmark=(
                holdout_trial.benchmark.beats_benchmark
                if holdout_trial.benchmark is not None
                else False
            ),
            original_validation_relative_result=validation_trial.benchmark.relative_result,
            original_calibrated_grade=reassessment.new_evidence_grade,
            matched_null_stratum=reassessment.matched_stratum,
            matched_null_count=reassessment.new_matched_null_count,
            matched_null_percentile=reassessment.new_matched_percentile,
            follow_up_kind=follow_up.kind.value,
            follow_up_rationale=qualification.rationale,
            follow_up_qualification=qualification.qualification.value,
            regime_selections=regime_selections,
            sample_scope_description=FULL_PANEL_SAMPLE_SCOPE_DESCRIPTION + threshold_sensitive_note,
            created_at=created_at,
        )
        return self._repository.save_specification(specification)


def _one_precedent(
    candidates: Sequence[GeneratedCorporateHypothesis], *, dimension: str, code: str
) -> GeneratedCorporateHypothesis:
    matches = [item for item in candidates if item.secondary_context_dimension == dimension]
    if len(matches) != 1:
        raise CorporateRegimeFollowUpApplicationError(code)
    return matches[0]


# ==========================================================================
# Sample-building primitives (own small copies; see module docstring).
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


@dataclass(frozen=True, slots=True)
class _PanelValueCache:
    """``panel.feature_observation``/``panel.outcome`` are each an O(dates)
    linear scan, and ``panel.outcome`` additionally re-derives a sector
    basket return by re-scanning and re-computing every sector member's own
    window return on *every* call. This one follow-up run calls the
    (security, date) lookup for the same fixed ``feature_id``/``outcome_id``/
    ``horizon_sessions`` combination tens of thousands of times (~60 matched-
    null replicates x two regime groups, plus adequacy/incremental/parent-
    relationship passes) -- reused unmodified panel primitives, but called
    *through* a plain-dict memoization computed once up front, rather than
    re-walking the panel on every single one of those lookups."""

    feature: dict[tuple[str, str], Decimal | None]
    outcome: dict[tuple[str, str], Decimal | None]


def _build_value_cache(
    panel: CorporatePanel,
    *,
    security_ids: Sequence[str],
    dates: Sequence[str],
    feature_id: CorporateFeatureId,
    outcome_id: CorporateOutcomeId,
    horizon_sessions: int,
) -> _PanelValueCache:
    feature: dict[tuple[str, str], Decimal | None] = {}
    outcome: dict[tuple[str, str], Decimal | None] = {}
    for security in security_ids:
        for date in dates:
            feature[(security, date)] = _feature_value(
                panel, security=security, date=date, feature_id=feature_id
            )
            outcome[(security, date)] = _outcome_value(
                panel,
                security=security,
                date=date,
                outcome_id=outcome_id,
                horizon_sessions=horizon_sessions,
            )
    return _PanelValueCache(feature=feature, outcome=outcome)


def _train_median_threshold(
    cache: _PanelValueCache, *, security_ids: Sequence[str], dates: Sequence[str]
) -> Decimal | None:
    values = [
        value
        for security in security_ids
        for date in dates
        if (value := cache.feature.get((security, date))) is not None
    ]
    return _median_decimal(values) if len(values) >= 4 else None


def _regime_rows(
    *,
    security_ids: Sequence[str],
    dates: Sequence[str],
    cache: _PanelValueCache,
    threshold: Decimal,
    feature_security_override: dict[tuple[str, str], str] | None = None,
) -> HypothesisSample:
    """The regime-follow-up analogue of ``application.corporate_market_
    research._cross_sectional_rows``, restricted to a caller-supplied date
    subset (a regime group's own membership) instead of one governed
    period, and reading through the memoized ``_PanelValueCache`` rather
    than the panel directly. ``feature_security_override`` lets the
    matched-null generator (section 9) redirect *which* security's cached
    feature value is read while the outcome always stays with its genuine
    ``(security, date)`` pair -- the deliberate misalignment that destroys
    the feature/outcome link while leaving every date/regime/market-
    structure fact untouched."""

    benchmark: list[CorporatePanelRow] = []
    treatment: list[CorporatePanelRow] = []
    for security in security_ids:
        for date in dates:
            feature_security = (
                feature_security_override.get((security, date), security)
                if feature_security_override
                else security
            )
            feature_value = cache.feature.get((feature_security, date))
            outcome_value = cache.outcome.get((security, date))
            if feature_value is None or outcome_value is None:
                continue
            row = CorporatePanelRow(
                stable_security_id=security, session_date=date, value=outcome_value
            )
            benchmark.append(row)
            if feature_value >= threshold:
                treatment.append(row)
    return HypothesisSample(
        treatment=tuple(treatment),
        benchmark=tuple(benchmark),
        benchmark_kind=BenchmarkKind.EQUAL_WEIGHT_COHORT,
    )


def _split_dates(
    panel: CorporatePanel,
    *,
    dates: Sequence[str],
    dimension: CrossMarketRegimeDimension,
    group_a_labels: Sequence[str],
    group_b_labels: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    group_a: list[str] = []
    group_b: list[str] = []
    excluded: list[str] = []
    a_set, b_set = set(group_a_labels), set(group_b_labels)
    for date in dates:
        label = panel.context_label(dimension.value, date)
        if label in a_set:
            group_a.append(date)
        elif label in b_set:
            group_b.append(date)
        else:
            excluded.append(date)
    return tuple(group_a), tuple(group_b), tuple(excluded)


# ==========================================================================
# Section 6/7: regime-group adequacy and panel-dependence evidence.
# ==========================================================================


def _group_evidence(
    panel: CorporatePanel,
    *,
    security_ids_universe: Sequence[str],
    cache: _PanelValueCache,
    group_name: str,
    labels: Sequence[str],
    dates: Sequence[str],
) -> tuple[RegimeGroupEvidence, HypothesisSample]:
    threshold_placeholder = Decimal(0)  # unused for evidence-only accounting below.
    sample = _regime_rows(
        security_ids=security_ids_universe,
        dates=dates,
        cache=cache,
        threshold=threshold_placeholder,
    )
    benchmark_rows = sample.benchmark
    security_ids = {row.stable_security_id for row in benchmark_rows}
    sectors = {panel.sector_of(security) for security in security_ids} - {None}
    total_possible = len(security_ids_universe) * len(dates)
    missing = sum(
        1
        for security in security_ids_universe
        for date in dates
        if cache.feature.get((security, date)) is None
    )
    missing_rate = (
        bound_statistical_decimal(Decimal(missing) / Decimal(total_possible))
        if total_possible > 0
        else None
    )
    distinct_dates = len({row.session_date for row in benchmark_rows})
    reasons: list[str] = []
    if len(benchmark_rows) < REGIME_GROUP_MINIMUM_RAW_OBSERVATIONS:
        reasons.append(
            f"RAW_OBSERVATIONS_{len(benchmark_rows)}_BELOW_MINIMUM_"
            f"{REGIME_GROUP_MINIMUM_RAW_OBSERVATIONS}"
        )
    if distinct_dates < REGIME_GROUP_MINIMUM_DISTINCT_DATES:
        reasons.append(
            f"DISTINCT_DATES_{distinct_dates}_BELOW_MINIMUM_{REGIME_GROUP_MINIMUM_DISTINCT_DATES}"
        )
    if len(security_ids) < REGIME_GROUP_MINIMUM_ISSUER_COUNT:
        reasons.append(
            f"ISSUER_COUNT_{len(security_ids)}_BELOW_MINIMUM_{REGIME_GROUP_MINIMUM_ISSUER_COUNT}"
        )
    evidence = RegimeGroupEvidence(
        group_name=group_name,
        labels=tuple(labels),
        raw_observations=len(benchmark_rows),
        distinct_effective_dates=distinct_dates,
        issuer_count=len(security_ids),
        sector_count=len(sectors),
        missing_feature_rate=missing_rate,
        sufficient=not reasons,
        insufficiency_reasons=tuple(reasons),
    )
    return evidence, sample


# ==========================================================================
# Section 8: the regime effect and interaction.
# ==========================================================================


def _group_effect(
    sample: HypothesisSample, *, group_name: str, bootstrap_seed: int
) -> RegimeGroupEffect:
    diagnostics = build_corporate_diagnostics(sample, bootstrap_seed=bootstrap_seed)
    comparison: BenchmarkComparison | None = None
    if sample.benchmark and diagnostics.base.mean is not None:
        comparison = compare_to_benchmark(
            sample_mean=diagnostics.base.mean,
            benchmark_kind=sample.benchmark_kind,
            benchmark_values=tuple(row.value for row in sample.benchmark),
        )
    return RegimeGroupEffect(
        group_name=group_name,
        treatment_count=len(sample.treatment),
        benchmark_count=len(sample.benchmark),
        treatment_mean=diagnostics.base.mean,
        benchmark_mean=comparison.benchmark_mean if comparison else None,
        effect=comparison.relative_result if comparison else None,
        beats_benchmark=comparison.beats_benchmark if comparison else None,
        cohens_d=diagnostics.effect_size.cohens_d if diagnostics.effect_size else None,
        effective_sample=diagnostics.overlap.effective_sample_count,
        standard_error=diagnostics.standard_error.value if diagnostics.standard_error else None,
        bootstrap_ci_lower=(
            diagnostics.bootstrap_confidence_interval.lower
            if diagnostics.bootstrap_confidence_interval
            else None
        ),
        bootstrap_ci_upper=(
            diagnostics.bootstrap_confidence_interval.upper
            if diagnostics.bootstrap_confidence_interval
            else None
        ),
    )


def _interaction_result(
    *,
    group_a_effect: RegimeGroupEffect,
    group_b_effect: RegimeGroupEffect,
    sample_a: HypothesisSample,
    sample_b: HypothesisSample,
    null_interaction_statistics: Sequence[Decimal],
    matched_stratum: str,
) -> RegimeInteractionResult:
    effect_a, effect_b = group_a_effect.effect, group_b_effect.effect
    interaction_effect = (
        bound_statistical_decimal(effect_a - effect_b)
        if effect_a is not None and effect_b is not None
        else None
    )
    se_a, se_b = group_a_effect.standard_error, group_b_effect.standard_error
    combined_se = (
        bound_statistical_decimal((se_a**2 + se_b**2).sqrt())
        if se_a is not None and se_b is not None
        else None
    )
    interaction_effect_size = compute_effect_size(
        sample_values=tuple(row.value for row in sample_a.treatment),
        benchmark_values=tuple(row.value for row in sample_b.treatment),
    )
    effect_meets_floor = (
        interaction_effect_size.cohens_d is not None
        and abs(interaction_effect_size.cohens_d) >= EFFECT_SIZE_FLOOR
    )
    matched = MatchedCalibrationResult.evaluate(
        statistic=interaction_effect,
        matched_null_statistics=tuple(null_interaction_statistics),
        pooled_null_statistics=tuple(null_interaction_statistics),
        matched_stratum=matched_stratum,
        minimum_matched_count=MINIMUM_MATCHED_NULL_COUNT,
    )
    raw_p_value = (
        bound_statistical_decimal(Decimal(1) - matched.matched_percentile)
        if matched.matched_percentile is not None
        else None
    )
    raw_significant = raw_p_value is not None and raw_p_value <= RAW_SIGNIFICANCE_ALPHA
    return RegimeInteractionResult(
        interaction_effect=interaction_effect,
        combined_standard_error=combined_se,
        interaction_cohens_d=interaction_effect_size.cohens_d,
        effect_meets_floor=bool(effect_meets_floor),
        effect_floor_policy_id="CORPORATE_REGIME_FOLLOWUP_EFFECT_SIZE_FLOOR_V1_0.20",
        raw_p_value=raw_p_value,
        raw_significant=raw_significant,
        matched_calibration=matched,
    )


# ==========================================================================
# Section 9: matched-null calibration for the split.
# ==========================================================================


def _shuffled_feature_overrides(
    dates: Sequence[str], security_ids: Sequence[str], *, seed: int
) -> dict[tuple[str, str], str]:
    """``SEEDED_ISSUER_DERANGEMENT`` (reusing ``corporate_calibration_
    depth.CorporateNullMechanism``'s own vocabulary for the identical
    mechanism): shuffles the security -> feature-value mapping *within*
    each date with a deterministic per-date seed, exactly mirroring
    ``application.corporate_market_research._randomized_issuer_rows``.
    Every date/security/outcome fact -- and therefore every regime-group
    membership, since regime labels are a pure function of the date --
    stays exactly as retained; only which security's *feature* value backs
    each row is deranged."""

    overrides: dict[tuple[str, str], str] = {}
    ordered_securities = tuple(security_ids)
    for date_ordinal, date in enumerate(dates):
        generator = random.Random(seed + date_ordinal)
        shuffled = list(ordered_securities)
        generator.shuffle(shuffled)
        for security, source in zip(ordered_securities, shuffled, strict=True):
            overrides[(security, date)] = source
    return overrides


NULL_PRESERVED_PROPERTIES = (
    "REAL_MARKET_DATES",
    "REAL_REGIME_GROUP_MEMBERSHIP_AND_PREVALENCE",
    "REAL_OUTCOME_VALUES_FOR_THEIR_GENUINE_SECURITY",
    "REAL_ISSUER_UNIVERSE_PER_DATE",
    "REAL_PANEL_DEPENDENCE_STRUCTURE",
    "APPROXIMATE_MISSINGNESS_PATTERN",
)
NULL_DESTROYED_RELATIONSHIP = (
    "TRUE_SECURITY_LEVEL_FEATURE_TO_OUTCOME_PAIRING_WITHIN_EACH_DATE -- the security -> feature "
    "mapping is shuffled within each date (SEEDED_ISSUER_DERANGEMENT); each row's outcome always "
    "stays with its own genuine security."
)


def _generate_null_controls(
    *,
    security_ids: Sequence[str],
    cache: _PanelValueCache,
    specification_id: str,
    role: RegimeSplitRole,
    dates_all: Sequence[str],
    group_a_dates: Sequence[str],
    group_b_dates: Sequence[str],
    threshold: Decimal,
    budget: int,
    base_seed: int,
    created_at: datetime,
) -> tuple[CorporateRegimeNullControl, ...]:
    controls: list[CorporateRegimeNullControl] = []
    for ordinal in range(budget):
        seed = base_seed + ordinal * 1000
        overrides = _shuffled_feature_overrides(dates_all, security_ids, seed=seed)
        sample_a = _regime_rows(
            security_ids=security_ids,
            dates=group_a_dates,
            cache=cache,
            threshold=threshold,
            feature_security_override=overrides,
        )
        sample_b = _regime_rows(
            security_ids=security_ids,
            dates=group_b_dates,
            cache=cache,
            threshold=threshold,
            feature_security_override=overrides,
        )
        effect_a = _sample_effect(sample_a)
        effect_b = _sample_effect(sample_b)
        control = CorporateRegimeNullControl.create(
            specification_id=specification_id,
            split_role=role,
            ordinal=ordinal,
            seed=seed,
            mechanism=CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT,
            group_a_raw_count=len(sample_a.benchmark),
            group_a_effect=effect_a,
            group_b_raw_count=len(sample_b.benchmark),
            group_b_effect=effect_b,
            preserved_properties=NULL_PRESERVED_PROPERTIES,
            destroyed_relationship=NULL_DESTROYED_RELATIONSHIP,
            created_at=created_at,
        )
        controls.append(control)
    return tuple(controls)


def _sample_effect(sample: HypothesisSample) -> Decimal | None:
    treatment_mean = _mean_of(tuple(row.value for row in sample.treatment))
    benchmark_mean = _mean_of(tuple(row.value for row in sample.benchmark))
    if treatment_mean is None or benchmark_mean is None:
        return None
    return bound_statistical_decimal(treatment_mean - benchmark_mean)


# ==========================================================================
# Section 12: temporal stability of the interaction.
# ==========================================================================


def _interaction_stability(
    *, dates_all: Sequence[str], sample_a: HypothesisSample, sample_b: HypothesisSample
) -> StabilityReport:
    ordered = sorted(set(dates_all))
    if len(ordered) < 6:
        return StabilityReport(
            subperiods=(),
            recent_holdout=None,
            state=StabilityState.INSUFFICIENT_EVIDENCE,
            evidence_note="FEWER_THAN_SIX_DISTINCT_DATES_IN_THE_SHARED_CALENDAR",
        )
    third = max(1, len(ordered) // 3)
    bounds = (
        ordered[0],
        ordered[third - 1],
        ordered[third],
        ordered[2 * third - 1],
        ordered[2 * third],
        ordered[-1],
    )
    subperiods: list[SubperiodResult] = []
    for index, label in enumerate(("EARLY", "MIDDLE", "LATE")):
        start, end = bounds[index * 2], bounds[index * 2 + 1]
        effect_a, count_a = _effect_within_dates(sample_a, start=start, end=end)
        effect_b, count_b = _effect_within_dates(sample_b, start=start, end=end)
        interaction = (
            bound_statistical_decimal(effect_a - effect_b)
            if effect_a is not None and effect_b is not None
            else None
        )
        subperiods.append(
            SubperiodResult(
                label=label,
                start_date=start,
                end_date=end,
                sample_count=count_a + count_b,
                mean=interaction,
                is_high_stress_evidence=False,
            )
        )
    state = derive_stability_state(subperiods)
    return StabilityReport(
        subperiods=tuple(subperiods),
        recent_holdout=None,
        state=state,
        evidence_note=(
            "EARLY_MIDDLE_LATE_TERCILE_SPLIT_OF_THE_SHARED_FULL_PANEL_CALENDAR_AXIS -- 'LATE' is "
            "this milestone's 'RECENT'; each slice's mean is the interaction effect (regime-A "
            "minus regime-B) within that calendar window, not a raw outcome mean."
        ),
    )


def _effect_within_dates(
    sample: HypothesisSample, *, start: str, end: str
) -> tuple[Decimal | None, int]:
    treatment = [row.value for row in sample.treatment if start <= row.session_date <= end]
    benchmark = [row.value for row in sample.benchmark if start <= row.session_date <= end]
    treatment_mean = _mean_of(treatment)
    benchmark_mean = _mean_of(benchmark)
    if treatment_mean is None or benchmark_mean is None:
        return None, len(treatment)
    return bound_statistical_decimal(treatment_mean - benchmark_mean), len(treatment)


# ==========================================================================
# Sections 13/14: sector/issuer concentration and outlier robustness of the
# interaction.
# ==========================================================================

_DERIVED_CHECK_TYPES: tuple[RobustnessCheckType, ...] = (
    RobustnessCheckType.LEAVE_ONE_PERIOD_OUT,
    RobustnessCheckType.WINSORIZED_SENSITIVITY,
    RobustnessCheckType.MEDIAN_VS_MEAN_COMPARISON,
)


def _direction_consistent(base_mean: Decimal | None, candidate: Decimal | None) -> bool | None:
    if base_mean is None or candidate is None or base_mean == 0:
        return None
    return (candidate > 0) == (base_mean > 0)


def _joint_leave_largest_out(
    panel: CorporatePanel, *, sample_a: HypothesisSample, sample_b: HypothesisSample, by: str
) -> tuple[Decimal | None, str]:
    def grouped(sample: HypothesisSample) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in sample.treatment:
            key = (
                row.stable_security_id
                if by == "security"
                else (panel.sector_of(row.stable_security_id) or "UNKNOWN")
            )
            counts[key] = counts.get(key, 0) + 1
        return counts

    combined: dict[str, int] = {}
    for source in (grouped(sample_a), grouped(sample_b)):
        for key, count in source.items():
            combined[key] = combined.get(key, 0) + count
    if not combined:
        return None, "NO_GROUPS"
    largest = max(combined, key=lambda key: combined[key])

    def excluded_effect(sample: HypothesisSample) -> Decimal | None:
        def key_of(row: CorporatePanelRow) -> str:
            return (
                row.stable_security_id
                if by == "security"
                else (panel.sector_of(row.stable_security_id) or "UNKNOWN")
            )

        treatment = tuple(row for row in sample.treatment if key_of(row) != largest)
        benchmark = tuple(row for row in sample.benchmark if key_of(row) != largest)
        treatment_mean = _mean_of(tuple(row.value for row in treatment))
        benchmark_mean = _mean_of(tuple(row.value for row in benchmark))
        if treatment_mean is None or benchmark_mean is None:
            return None
        return bound_statistical_decimal(treatment_mean - benchmark_mean)

    effect_a = excluded_effect(sample_a)
    effect_b = excluded_effect(sample_b)
    if effect_a is None or effect_b is None:
        return None, largest
    return bound_statistical_decimal(effect_a - effect_b), largest


def _interaction_robustness(
    panel: CorporatePanel,
    *,
    sample_a: HypothesisSample,
    sample_b: HypothesisSample,
    base_interaction: Decimal | None,
) -> RegimeRobustnessAssessment:
    mean_a = _mean_of(tuple(row.value for row in sample_a.treatment))
    mean_b = _mean_of(tuple(row.value for row in sample_b.treatment))
    checks_a = (
        compute_robustness(sample_a, panel=panel, base_mean=mean_a) if mean_a is not None else ()
    )
    checks_b = (
        compute_robustness(sample_b, panel=panel, base_mean=mean_b) if mean_b is not None else ()
    )
    by_type_a = {item.check_type: item for item in checks_a}
    by_type_b = {item.check_type: item for item in checks_b}

    interaction_checks: list[RobustnessCheckResult] = []
    sector_effect, sector_excluded = _joint_leave_largest_out(
        panel, sample_a=sample_a, sample_b=sample_b, by="sector"
    )
    interaction_checks.append(
        RobustnessCheckResult(
            check_type=RobustnessCheckType.ASSET_COHORT_EXCLUSION,
            variant_label=f"interaction_leave_one_sector_out_excluding_{sector_excluded}",
            sample_count=len(sample_a.treatment) + len(sample_b.treatment),
            result_mean=sector_effect,
            direction_consistent_with_base=_direction_consistent(base_interaction, sector_effect),
            note="OK" if sector_effect is not None else "INSUFFICIENT_DATA_AFTER_EXCLUSION",
        )
    )
    issuer_effect, issuer_excluded = _joint_leave_largest_out(
        panel, sample_a=sample_a, sample_b=sample_b, by="security"
    )
    interaction_checks.append(
        RobustnessCheckResult(
            check_type=RobustnessCheckType.ASSET_COHORT_EXCLUSION,
            variant_label=f"interaction_leave_one_issuer_out_excluding_{issuer_excluded}",
            sample_count=len(sample_a.treatment) + len(sample_b.treatment),
            result_mean=issuer_effect,
            direction_consistent_with_base=_direction_consistent(base_interaction, issuer_effect),
            note="OK" if issuer_effect is not None else "INSUFFICIENT_DATA_AFTER_EXCLUSION",
        )
    )
    for check_type in _DERIVED_CHECK_TYPES:
        a_check = by_type_a.get(check_type)
        b_check = by_type_b.get(check_type)
        if (
            a_check
            and b_check
            and a_check.result_mean is not None
            and b_check.result_mean is not None
        ):
            derived = bound_statistical_decimal(a_check.result_mean - b_check.result_mean)
            interaction_checks.append(
                RobustnessCheckResult(
                    check_type=check_type,
                    variant_label=f"interaction_derived_{check_type.value.lower()}",
                    sample_count=a_check.sample_count + b_check.sample_count,
                    result_mean=derived,
                    direction_consistent_with_base=_direction_consistent(base_interaction, derived),
                    note="OK",
                )
            )
        else:
            interaction_checks.append(
                RobustnessCheckResult(
                    check_type=check_type,
                    variant_label=f"interaction_derived_{check_type.value.lower()}",
                    sample_count=0,
                    result_mean=None,
                    direction_consistent_with_base=None,
                    note="INSUFFICIENT_DATA_IN_ONE_OR_BOTH_GROUPS",
                )
            )
    summary = summarize_robustness(interaction_checks)
    return RegimeRobustnessAssessment(
        group_a_checks=checks_a,
        group_b_checks=checks_b,
        interaction_checks=tuple(interaction_checks),
        summary=summary,
        leave_one_sector_out_excluded=sector_excluded,
        leave_one_issuer_out_excluded=issuer_excluded,
    )


# ==========================================================================
# Section 15: incremental market-context test.
# ==========================================================================


def _incremental_contribution(
    *,
    security_ids: Sequence[str],
    cache: _PanelValueCache,
    dates_all: Sequence[str],
    group_a_dates: Sequence[str],
    group_b_dates: Sequence[str],
    threshold: Decimal,
    matched: MatchedCalibrationResult,
    interaction_effect: Decimal | None,
) -> RegimeIncrementalContribution:
    regime_a_outcomes = [
        value
        for security in security_ids
        for date in group_a_dates
        if (value := cache.outcome.get((security, date))) is not None
    ]
    regime_b_outcomes = [
        value
        for security in security_ids
        for date in group_b_dates
        if (value := cache.outcome.get((security, date))) is not None
    ]
    regime_mean_a = _mean_of(regime_a_outcomes)
    regime_mean_b = _mean_of(regime_b_outcomes)
    regime_only_effect = (
        bound_statistical_decimal(regime_mean_a - regime_mean_b)
        if regime_mean_a is not None and regime_mean_b is not None
        else None
    )

    full_sample = _regime_rows(
        security_ids=security_ids, dates=dates_all, cache=cache, threshold=threshold
    )
    high_values = tuple(row.value for row in full_sample.treatment)
    low_values = tuple(
        row.value for row in full_sample.benchmark if row not in frozenset(full_sample.treatment)
    )
    high_mean, low_mean = _mean_of(high_values), _mean_of(low_values)
    corporate_only_effect = (
        bound_statistical_decimal(high_mean - low_mean)
        if high_values and low_values and high_mean is not None and low_mean is not None
        else None
    )

    verdict = classify_incremental_contribution(
        matched=matched,
        interaction_effect=interaction_effect,
        regime_only_effect=regime_only_effect,
    )
    rationale = {
        IncrementalContributionVerdict.GENUINELY_INCREMENTAL: (
            "The interaction's matched percentile clears the 0.90 suggestive floor and the "
            "market-context-only baseline alone does not explain an effect of comparable "
            "magnitude -- the corporate feature adds information beyond the regime split itself."
        ),
        IncrementalContributionVerdict.REDUNDANT: (
            "The market-context-only (regime, feature-blind) effect is at least as large as the "
            "feature-conditioned interaction -- regime alone already explains what looked like a "
            "corporate-feature effect; not called useful per milestone section 15."
        ),
        IncrementalContributionVerdict.ORDINARY_NULL_VARIATION: (
            "The interaction's matched percentile does not clear the 0.90 suggestive floor -- "
            "ordinary null-control-like behavior, not a genuine incremental signal."
        ),
        IncrementalContributionVerdict.INSUFFICIENTLY_CALIBRATED: (
            "The matched-null population for this split is insufficient or the interaction "
            "statistic is unavailable; incremental contribution cannot be assessed."
        ),
    }[verdict]
    return RegimeIncrementalContribution(
        regime_only_effect=regime_only_effect,
        regime_only_sample=len(regime_a_outcomes) + len(regime_b_outcomes),
        corporate_only_effect=corporate_only_effect,
        corporate_only_sample=len(high_values) + len(low_values),
        combined_interaction_effect=interaction_effect,
        combined_sample=len(full_sample.treatment),
        verdict=verdict,
        rationale=rationale,
    )


# ==========================================================================
# Section 16: parent relationship preservation.
# ==========================================================================


def _parent_relationship(
    *,
    security_ids: Sequence[str],
    cache: _PanelValueCache,
    dates_all: Sequence[str],
    threshold: Decimal,
    original_validation_relative_result: Decimal,
    group_a_effect: Decimal | None,
    group_b_effect: Decimal | None,
) -> ParentRelationshipComparison:
    full_sample = _regime_rows(
        security_ids=security_ids, dates=dates_all, cache=cache, threshold=threshold
    )
    full_effect = _sample_effect(full_sample)
    behavior = classify_parent_relationship(
        original_validation_relative_result=original_validation_relative_result,
        full_sample_unsplit_relative_result=full_effect,
    )
    rationale = (
        f"Original VALIDATION-phase relative result {original_validation_relative_result} "
        f"(beats_benchmark). Full-panel unsplit relative result (all retained TRAIN/VALIDATION/"
        f"HOLDOUT dates, feature-threshold unchanged): {full_effect}. Regime-A effect: "
        f"{group_a_effect}. Regime-B effect: {group_b_effect}. Classified {behavior.value} by "
        "sign and magnitude ratio against the original figure; the original VALIDATION-phase "
        "WEAK_SUPPORT conclusion itself is never rewritten."
    )
    return ParentRelationshipComparison(
        original_validation_relative_result=original_validation_relative_result,
        full_sample_unsplit_relative_result=full_effect,
        group_a_effect=group_a_effect,
        group_b_effect=group_b_effect,
        behavior=behavior,
        rationale=rationale,
    )


# ==========================================================================
# Evidence grade for one split.
# ==========================================================================


def _split_evidence_grade(
    *,
    specification_id: str,
    role: RegimeSplitRole,
    sample_quality: SampleQualityState,
    interaction: RegimeInteractionResult | None,
    robustness: RegimeRobustnessAssessment | None,
    stability: StabilityReport | None,
    corrected_significant: bool | None,
    created_at: datetime,
) -> tuple[EvidenceGrade, str, ResearchConclusionState]:
    if (
        sample_quality is not SampleQualityState.SUFFICIENT
        or interaction is None
        or robustness is None
    ):
        return (
            EvidenceGrade.EXPLORATORY,
            "Regime-group adequacy was not met; grade cannot exceed EXPLORATORY.",
            ResearchConclusionState.INSUFFICIENT_EVIDENCE,
        )
    conclusion_state = derive_conclusion_state(
        sample_quality=sample_quality,
        robustness_summary=robustness.summary,
        validation_beats_benchmark=interaction.raw_significant,
        # This follow-up design has no separate locked-holdout phase of its
        # own (section: full-retained-panel scope); ``None`` here is
        # honest, not a missing value, and structurally caps
        # ``derive_evidence_grade`` below at SUGGESTIVE -- SUPPORTED (and
        # therefore VALIDATED/STRONG) requires a genuine holdout result,
        # which a same-dataset post-hoc follow-up does not have (section
        # 19: independent confirmation is always separately required).
        holdout_beats_benchmark=None,
        multiple_testing_corrected_significant=corrected_significant,
    )
    profile = EvidenceProfile.create(
        subject_id=f"{specification_id}_{role.value.lower()}_interaction",
        subject_kind="HYPOTHESIS",
        benchmark_relative_effect=interaction.interaction_effect,
        cohens_d=interaction.interaction_cohens_d,
        validation_beats_benchmark=interaction.raw_significant,
        holdout_result="NO_SEPARATE_HOLDOUT_PHASE_IN_THIS_FOLLOWUP_DESIGN",
        walk_forward_consistency=None,
        robustness_passed=robustness.summary.passed,
        corrected_significant=corrected_significant,
        effective_sample=Decimal(
            min(len(robustness.group_a_checks), len(robustness.group_b_checks))
        )
        if robustness.group_a_checks and robustness.group_b_checks
        else Decimal(0),
        regime_concentration=stability.state.value
        if stability is not None
        else "INSUFFICIENT_EVIDENCE",
        null_percentile=interaction.matched_calibration.matched_percentile,
        practical_significance=classify_practical_significance(
            statistically_significant=interaction.raw_significant,
            meets_effect_floor=interaction.effect_meets_floor,
        ),
        created_at=created_at,
    )
    from apex.domain.research_calibration import derive_evidence_grade

    grade = derive_evidence_grade(
        conclusion_state=conclusion_state,
        profile=profile,
        effect_meets_floor=interaction.effect_meets_floor,
    )
    rationale = (
        f"conclusion_state={conclusion_state.value}, matched_percentile="
        f"{interaction.matched_calibration.matched_percentile}, robustness_passed="
        f"{robustness.summary.passed}, corrected_significant={corrected_significant}, "
        f"effect_meets_floor={interaction.effect_meets_floor} -> {grade.value} via "
        "research_calibration.derive_evidence_grade (EVIDENCE_GRADE_POLICY_V1, unchanged)."
    )
    return grade, rationale, conclusion_state


# ==========================================================================
# Top-level orchestrator.
# ==========================================================================


class RunCorporateRegimeFollowUp:
    """Executes the one, bounded, predeclared regime follow-up. Both
    ``start`` and ``resume`` (see the CLI) delegate to this identical
    idempotent entry point: this follow-up's ~60-control null battery and
    single genuine two-split evaluation complete in one deterministic pass
    over an already-in-memory panel -- unlike the 240-control calibration-
    depth run, there is no natural multi-step checkpoint boundary to
    resume across. A specification already carrying a retained result is
    returned unchanged rather than recomputed, exactly mirroring "exact
    replay is free" elsewhere in this research stack."""

    def __init__(
        self,
        *,
        repository: CorporateRegimeFollowUpRepository,
        panel_builder: CorporatePanelBuilderCollaborator,
    ) -> None:
        self._repository = repository
        self._panel_builder = panel_builder
        self._all_controls: list[CorporateRegimeNullControl] = []

    def execute(
        self, *, specification: CorporateRegimeFollowUpSpecification, created_at: datetime
    ) -> CorporateRegimeFollowUpResult:
        existing = self._repository.result_by_specification(specification.specification_id)
        if existing is not None:
            return existing
        self._all_controls = []

        panel = self._panel_builder.execute(
            corporate_dataset_id=specification.corporate_dataset_id,
            equity_snapshot_id=specification.equity_snapshot_id,
            replay_run_id=specification.replay_run_id,
            cross_market_dataset_id=specification.cross_market_dataset_id,
        )
        security_ids = panel.usable_security_ids
        dates_all = tuple(
            date
            for date in panel.monthly_dates
            if specification.original_train_start <= date <= specification.original_holdout_end
        )
        # One memoized pass over every (security, date) pair this entire
        # run will ever need -- see ``_PanelValueCache``'s own docstring for
        # why this matters: without it, the ~60 matched-null replicates
        # alone would re-walk the full panel roughly two orders of
        # magnitude more than the genuine v3 agenda run itself ever did for
        # this one hypothesis.
        cache = _build_value_cache(
            panel,
            security_ids=security_ids,
            dates=dates_all,
            feature_id=specification.feature_id,
            outcome_id=specification.outcome_id,
            horizon_sessions=specification.horizon_sessions,
        )
        train_dates = tuple(
            date
            for date in dates_all
            if specification.original_train_start <= date <= specification.original_train_end
        )
        threshold = _train_median_threshold(cache, security_ids=security_ids, dates=train_dates)
        if threshold is None:
            raise CorporateRegimeFollowUpApplicationError("TRAIN_MEDIAN_THRESHOLD_UNAVAILABLE")

        primary = self._run_split(
            panel,
            security_ids=security_ids,
            cache=cache,
            specification=specification,
            role=RegimeSplitRole.PRIMARY,
            dates_all=dates_all,
            threshold=threshold,
            base_seed=20260809,
            created_at=created_at,
        )
        secondary = self._run_split(
            panel,
            security_ids=security_ids,
            cache=cache,
            specification=specification,
            role=RegimeSplitRole.SECONDARY,
            dates_all=dates_all,
            threshold=threshold,
            base_seed=20270809,
            created_at=created_at,
        )

        # Section 11: BH-FDR across exactly the two predeclared family
        # members, whether or not each one produced a testable statistic.
        test_results = [
            TrialTestResult(
                trial_id=f"{specification.specification_id}_{RegimeSplitRole.PRIMARY.value.lower()}",
                label=primary.selection.dimension.value,
                p_value=primary.interaction.raw_p_value if primary.interaction else None,
            ),
            TrialTestResult(
                trial_id=f"{specification.specification_id}_{RegimeSplitRole.SECONDARY.value.lower()}",
                label=secondary.selection.dimension.value,
                p_value=secondary.interaction.raw_p_value if secondary.interaction else None,
            ),
        ]
        corrected = apply_benjamini_hochberg_correction(
            test_results, false_discovery_rate=FALSE_DISCOVERY_RATE
        )
        corrected_by_role = {
            RegimeSplitRole.PRIMARY: corrected[0],
            RegimeSplitRole.SECONDARY: corrected[1],
        }
        primary = self._finalize_split_grade(
            specification=specification,
            split=primary,
            corrected=corrected_by_role[RegimeSplitRole.PRIMARY],
            created_at=created_at,
        )
        secondary = self._finalize_split_grade(
            specification=specification,
            split=secondary,
            corrected=corrected_by_role[RegimeSplitRole.SECONDARY],
            created_at=created_at,
        )

        parent_relationship = _parent_relationship(
            security_ids=security_ids,
            cache=cache,
            dates_all=dates_all,
            threshold=threshold,
            original_validation_relative_result=specification.original_validation_relative_result,
            group_a_effect=primary.group_a_effect.effect if primary.group_a_effect else None,
            group_b_effect=primary.group_b_effect.effect if primary.group_b_effect else None,
        )

        conclusion = classify_regime_follow_up_conclusion(
            adequate=primary.adequate,
            conclusion_state=primary.conclusion_state,
            evidence_grade=primary.evidence_grade,
            stability_state=primary.interaction_stability.state
            if primary.interaction_stability
            else None,
            incremental_verdict=primary.incremental.verdict if primary.incremental else None,
        )
        primary_conclusion_state_text = (
            primary.conclusion_state.value if primary.conclusion_state else None
        )
        primary_evidence_grade_text = (
            primary.evidence_grade.value if primary.evidence_grade else None
        )
        primary_stability_text = (
            primary.interaction_stability.state.value if primary.interaction_stability else None
        )
        primary_incremental_text = (
            primary.incremental.verdict.value if primary.incremental else None
        )
        conclusion_rationale = (
            f"PRIMARY split (EQUITY_TREND): adequate={primary.adequate}, "
            f"conclusion_state={primary_conclusion_state_text}, "
            f"evidence_grade={primary_evidence_grade_text}, "
            f"stability={primary_stability_text}, "
            f"incremental={primary_incremental_text}. "
            f"SECONDARY split (EQUITY_VOLATILITY, robustness-only): adequate={secondary.adequate}. "
            f"-> {conclusion.value}."
        )

        # The final knowledge-history stage references ``specification_id``,
        # not a result id: the two are already a fixed 1:1 pairing (one
        # result per specification, looked up via
        # ``result_by_specification``), so no circular self-reference is
        # needed to name "this result" from within its own payload.
        history = build_knowledge_history(
            hypothesis_id=specification.parent_hypothesis_id,
            original_conclusion_id=specification.parent_conclusion_id,
            original_conclusion_state=specification.original_conclusion_state,
            original_conclusion_created_at=specification.created_at,
            reassessment_id=specification.parent_reassessment_id,
            calibrated_grade=specification.original_calibrated_grade,
            reassessment_created_at=specification.created_at,
            follow_up_id=specification.follow_up_id,
            qualification_id=specification.parent_qualification_id,
            qualification=specification.follow_up_qualification,
            qualification_created_at=specification.created_at,
            result_id=specification.specification_id,
            result_conclusion=conclusion,
            result_created_at=created_at,
        )

        result = CorporateRegimeFollowUpResult.create(
            specification_id=specification.specification_id,
            primary_split=primary,
            secondary_split=secondary,
            multiple_testing_results=corrected,
            parent_relationship=parent_relationship,
            conclusion=conclusion,
            conclusion_rationale=conclusion_rationale,
            evidence_grade=primary.evidence_grade or EvidenceGrade.EXPLORATORY,
            evidence_grade_rationale=primary.evidence_grade_rationale,
            knowledge_history=history,
            created_at=created_at,
        )

        for control in self._all_controls:
            self._repository.save_null_control(control)
        return self._repository.save_result(result)

    def _run_split(
        self,
        panel: CorporatePanel,
        *,
        security_ids: Sequence[str],
        cache: _PanelValueCache,
        specification: CorporateRegimeFollowUpSpecification,
        role: RegimeSplitRole,
        dates_all: Sequence[str],
        threshold: Decimal,
        base_seed: int,
        created_at: datetime,
    ) -> RegimeSplitResult:
        selection = specification.selection_for(role)
        group_a_dates, group_b_dates, _excluded = _split_dates(
            panel,
            dates=dates_all,
            dimension=selection.dimension,
            group_a_labels=selection.group_a_labels,
            group_b_labels=selection.group_b_labels,
        )
        # The sample each call also returns is built with a placeholder
        # threshold (adequacy is feature-threshold-independent); the real
        # threshold-scoped samples are built separately below, once
        # adequacy is confirmed.
        evidence_a, _evidence_only_sample_a = _group_evidence(
            panel,
            security_ids_universe=security_ids,
            cache=cache,
            group_name=selection.group_a_name,
            labels=selection.group_a_labels,
            dates=group_a_dates,
        )
        evidence_b, _evidence_only_sample_b = _group_evidence(
            panel,
            security_ids_universe=security_ids,
            cache=cache,
            group_name=selection.group_b_name,
            labels=selection.group_b_labels,
            dates=group_b_dates,
        )
        adequate = evidence_a.sufficient and evidence_b.sufficient
        if not adequate:
            reasons = ", ".join(
                (*evidence_a.insufficiency_reasons, *evidence_b.insufficiency_reasons)
            )
            return RegimeSplitResult(
                role=role,
                selection=selection,
                group_a_evidence=evidence_a,
                group_b_evidence=evidence_b,
                adequate=False,
                inadequacy_reason=reasons or "REGIME_GROUP_ADEQUACY_NOT_MET",
                group_a_effect=None,
                group_b_effect=None,
                interaction=None,
                interaction_stability=None,
                robustness=None,
                incremental=None,
                sample_quality=SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE,
                conclusion_state=ResearchConclusionState.INSUFFICIENT_EVIDENCE,
                evidence_grade=None,
                evidence_grade_rationale="Regime-group adequacy (section 6) was not met.",
            )

        # Now build real (unshuffled) samples using the actual regime-scoped
        # threshold cross-sectional rows for effect/robustness/stability.
        real_sample_a = _regime_rows(
            security_ids=security_ids, dates=group_a_dates, cache=cache, threshold=threshold
        )
        real_sample_b = _regime_rows(
            security_ids=security_ids, dates=group_b_dates, cache=cache, threshold=threshold
        )
        group_a_effect = _group_effect(
            real_sample_a, group_name=selection.group_a_name, bootstrap_seed=base_seed + 1
        )
        group_b_effect = _group_effect(
            real_sample_b, group_name=selection.group_b_name, bootstrap_seed=base_seed + 2
        )

        null_controls = _generate_null_controls(
            security_ids=security_ids,
            cache=cache,
            specification_id=specification.specification_id,
            role=role,
            dates_all=dates_all,
            group_a_dates=group_a_dates,
            group_b_dates=group_b_dates,
            threshold=threshold,
            budget=selection.null_control_budget,
            base_seed=base_seed,
            created_at=created_at,
        )
        self._all_controls.extend(null_controls)
        null_interaction_statistics = tuple(
            item.interaction_effect for item in null_controls if item.interaction_effect is not None
        )
        matched_stratum = (
            f"REGIME_SPLIT|CROSS_SECTIONAL|{specification.outcome_id.value}|"
            f"H{specification.horizon_sessions}|{selection.dimension.value}"
        )
        interaction = _interaction_result(
            group_a_effect=group_a_effect,
            group_b_effect=group_b_effect,
            sample_a=real_sample_a,
            sample_b=real_sample_b,
            null_interaction_statistics=null_interaction_statistics,
            matched_stratum=matched_stratum,
        )
        stability = _interaction_stability(
            dates_all=dates_all, sample_a=real_sample_a, sample_b=real_sample_b
        )
        robustness = _interaction_robustness(
            panel,
            sample_a=real_sample_a,
            sample_b=real_sample_b,
            base_interaction=interaction.interaction_effect,
        )
        incremental = _incremental_contribution(
            security_ids=security_ids,
            cache=cache,
            dates_all=dates_all,
            group_a_dates=group_a_dates,
            group_b_dates=group_b_dates,
            threshold=threshold,
            matched=interaction.matched_calibration,
            interaction_effect=interaction.interaction_effect,
        )
        sample_quality = (
            SampleQualityState.SUFFICIENT
            if adequate
            and min(group_a_effect.effective_sample, group_b_effect.effective_sample) >= Decimal(4)
            else SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE
        )
        return RegimeSplitResult(
            role=role,
            selection=selection,
            group_a_evidence=evidence_a,
            group_b_evidence=evidence_b,
            adequate=True,
            inadequacy_reason=None,
            group_a_effect=group_a_effect,
            group_b_effect=group_b_effect,
            interaction=interaction,
            interaction_stability=stability,
            robustness=robustness,
            incremental=incremental,
            sample_quality=sample_quality,
            conclusion_state=None,  # filled in by _finalize_split_grade, once BH-FDR is known.
            evidence_grade=None,
            evidence_grade_rationale="",
        )

    def _finalize_split_grade(
        self,
        *,
        specification: CorporateRegimeFollowUpSpecification,
        split: RegimeSplitResult,
        corrected: CorrectedTestResult,
        created_at: datetime,
    ) -> RegimeSplitResult:
        if not split.adequate:
            return split
        grade, rationale, conclusion_state = _split_evidence_grade(
            specification_id=specification.specification_id,
            role=split.role,
            sample_quality=split.sample_quality,
            interaction=split.interaction,
            robustness=split.robustness,
            stability=split.interaction_stability,
            corrected_significant=corrected.rejected_null,
            created_at=created_at,
        )
        return replace(
            split,
            conclusion_state=conclusion_state,
            evidence_grade=grade,
            evidence_grade_rationale=rationale,
        )


# ==========================================================================
# Offline in-memory repository -- mirrors ``application.corporate_
# calibration_depth.InMemoryCorporateCalibrationDepthRepository``'s own
# convention of a same-Protocol, no-database double for tests.
# ==========================================================================


class InMemoryCorporateRegimeFollowUpRepository:
    def __init__(self) -> None:
        self._specifications: dict[str, CorporateRegimeFollowUpSpecification] = {}
        self._controls: dict[str, CorporateRegimeNullControl] = {}
        self._results: dict[str, CorporateRegimeFollowUpResult] = {}

    def specification_by_id(
        self, specification_id: str
    ) -> CorporateRegimeFollowUpSpecification | None:
        return self._specifications.get(specification_id)

    def specification_by_follow_up(
        self, follow_up_id: str
    ) -> CorporateRegimeFollowUpSpecification | None:
        for item in self._specifications.values():
            if item.follow_up_id == follow_up_id:
                return item
        return None

    def save_specification(
        self, specification: CorporateRegimeFollowUpSpecification
    ) -> CorporateRegimeFollowUpSpecification:
        return self._specifications.setdefault(specification.specification_id, specification)

    def save_null_control(self, control: CorporateRegimeNullControl) -> CorporateRegimeNullControl:
        return self._controls.setdefault(control.control_id, control)

    def controls_for_specification(
        self, specification_id: str, *, role: RegimeSplitRole | None = None
    ) -> tuple[CorporateRegimeNullControl, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._controls.values()
                    if item.specification_id == specification_id
                    and (role is None or item.split_role is role)
                ),
                key=lambda item: (item.split_role.value, item.ordinal),
            )
        )

    def result_by_specification(
        self, specification_id: str
    ) -> CorporateRegimeFollowUpResult | None:
        for item in self._results.values():
            if item.specification_id == specification_id:
                return item
        return None

    def save_result(self, result: CorporateRegimeFollowUpResult) -> CorporateRegimeFollowUpResult:
        return self._results.setdefault(result.result_id, result)
