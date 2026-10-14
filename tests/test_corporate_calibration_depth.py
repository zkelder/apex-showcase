"""Domain proofs for Corporate Research Calibration Depth v1: strata-target
construction and design/mechanism compatibility, the calibration plan's
declared-before-results budget invariants, the resumable run cursor, null-
control provenance validation, non-destructive reassessment records, the
WEAK_SUPPORT diagnostic classifier, and the bounded report shape. No
database, no synthetic panel -- pure dataclass/validation proofs only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.corporate_calibration_depth import (
    CONDITIONAL_INTERACTION_MECHANISMS,
    CROSS_SECTIONAL_MECHANISMS,
    FILING_EVENT_MECHANISMS,
    MECHANISMS_BY_DESIGN,
    MINIMUM_MATCHED_NULL_COUNT,
    CorporateCalibrationDepthError,
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
    TemporalRegion,
    WeakSupportCause,
    bucket_effective_sample,
    classify_weak_support_cause,
    corporate_calibration_depth_warnings,
    temporal_region_for_date,
)
from apex.domain.research_calibration import CalibrationRunStatus, EvidenceGrade
from apex.domain.research_calibration_v2 import ConditioningShape, ResearchDesign

NOW = datetime(2026, 8, 9, tzinfo=UTC)

CROSS_SECTIONAL_STRATUM = "CROSS_SECTIONAL|SUBSEQUENT_RETURN|H20|NUMERIC_SPLIT"
CONDITIONAL_STRATUM = "CONDITIONAL|SUBSEQUENT_RETURN|H20|LABEL_SPLIT"


def _cross_sectional_target(
    *, original_matched_null_count: int = 0, target_new_control_count: int = 24
) -> CorporateStratumTarget:
    return CorporateStratumTarget.create(
        matched_stratum=CROSS_SECTIONAL_STRATUM,
        research_design=ResearchDesign.CROSS_SECTIONAL,
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
        outcome_id="SUBSEQUENT_RETURN",
        horizon_sessions=20,
        genuine_hypothesis_ids=("hyp-1", "hyp-2"),
        backing_feature_ids=("REVENUE_GROWTH_YOY", "OPERATING_MARGIN"),
        original_matched_null_count=original_matched_null_count,
        target_new_control_count=target_new_control_count,
    )


def _conditional_target() -> CorporateStratumTarget:
    return CorporateStratumTarget.create(
        matched_stratum=CONDITIONAL_STRATUM,
        research_design=ResearchDesign.CONDITIONAL,
        conditioning_shape=ConditioningShape.LABEL_SPLIT,
        outcome_id="SUBSEQUENT_RETURN",
        horizon_sessions=20,
        genuine_hypothesis_ids=("hyp-3",),
        backing_feature_ids=("REVENUE_GROWTH_YOY",),
        original_matched_null_count=0,
        target_new_control_count=24,
        secondary_conditions=(("REVENUE_GROWTH_YOY", "EQUITY_TREND", "UPTREND"),),
    )


# ==========================================================================
# Section 4: null-design taxonomy
# ==========================================================================


def test_every_research_design_used_by_v3_has_a_mechanism_set() -> None:
    for design in (
        ResearchDesign.CROSS_SECTIONAL,
        ResearchDesign.CHANGE_BASED,
        ResearchDesign.FILING_EVENT,
        ResearchDesign.CONDITIONAL,
        ResearchDesign.INTERACTION,
    ):
        assert design in MECHANISMS_BY_DESIGN
        assert len(MECHANISMS_BY_DESIGN[design]) >= 3


def test_cross_sectional_and_change_based_share_the_same_four_mechanisms() -> None:
    assert MECHANISMS_BY_DESIGN[ResearchDesign.CROSS_SECTIONAL] == CROSS_SECTIONAL_MECHANISMS
    assert MECHANISMS_BY_DESIGN[ResearchDesign.CHANGE_BASED] == CROSS_SECTIONAL_MECHANISMS
    assert len(CROSS_SECTIONAL_MECHANISMS) == 4


def test_filing_event_and_conditional_interaction_mechanism_counts() -> None:
    assert len(FILING_EVENT_MECHANISMS) == 3
    assert len(CONDITIONAL_INTERACTION_MECHANISMS) == 3
    assert MECHANISMS_BY_DESIGN[ResearchDesign.CONDITIONAL] == CONDITIONAL_INTERACTION_MECHANISMS
    assert MECHANISMS_BY_DESIGN[ResearchDesign.INTERACTION] == CONDITIONAL_INTERACTION_MECHANISMS


def test_mechanisms_never_overlap_across_the_three_design_groups() -> None:
    cross_sectional = set(CROSS_SECTIONAL_MECHANISMS)
    filing = set(FILING_EVENT_MECHANISMS)
    conditional = set(CONDITIONAL_INTERACTION_MECHANISMS)
    assert not (cross_sectional & filing)
    assert not (cross_sectional & conditional)
    assert not (filing & conditional)
    assert cross_sectional | filing | conditional == set(CorporateNullMechanism)


# ==========================================================================
# Section 2/10: strata target and calibration plan
# ==========================================================================


def test_stratum_target_rejects_a_design_outside_the_five_corporate_designs() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        CorporateStratumTarget.create(
            matched_stratum="PREDICTIVE|X|H20|UNCONDITIONAL",
            research_design=ResearchDesign.PREDICTIVE,
            conditioning_shape=ConditioningShape.UNCONDITIONAL,
            outcome_id="SUBSEQUENT_RETURN",
            horizon_sessions=20,
            genuine_hypothesis_ids=("hyp-1",),
            backing_feature_ids=("REVENUE_GROWTH_YOY",),
            original_matched_null_count=0,
            target_new_control_count=24,
        )


def test_stratum_target_requires_secondary_conditions_for_conditional_design() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        CorporateStratumTarget.create(
            matched_stratum=CONDITIONAL_STRATUM,
            research_design=ResearchDesign.CONDITIONAL,
            conditioning_shape=ConditioningShape.LABEL_SPLIT,
            outcome_id="SUBSEQUENT_RETURN",
            horizon_sessions=20,
            genuine_hypothesis_ids=("hyp-3",),
            backing_feature_ids=("REVENUE_GROWTH_YOY",),
            original_matched_null_count=0,
            target_new_control_count=24,
        )


def test_stratum_target_rejects_secondary_conditions_for_a_non_conditional_design() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        CorporateStratumTarget.create(
            matched_stratum=CROSS_SECTIONAL_STRATUM,
            research_design=ResearchDesign.CROSS_SECTIONAL,
            conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
            outcome_id="SUBSEQUENT_RETURN",
            horizon_sessions=20,
            genuine_hypothesis_ids=("hyp-1",),
            backing_feature_ids=("REVENUE_GROWTH_YOY",),
            original_matched_null_count=0,
            target_new_control_count=24,
            secondary_conditions=(("REVENUE_GROWTH_YOY", "EQUITY_TREND", "UPTREND"),),
        )


def test_stratum_target_original_sufficiency_uses_the_undropped_minimum() -> None:
    below = _cross_sectional_target(original_matched_null_count=MINIMUM_MATCHED_NULL_COUNT - 1)
    at = _cross_sectional_target(original_matched_null_count=MINIMUM_MATCHED_NULL_COUNT)
    assert below.original_sufficient is False
    assert at.original_sufficient is True


def test_stratum_target_rejects_new_control_count_outside_20_to_40() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        _cross_sectional_target(target_new_control_count=19)
    with pytest.raises(CorporateCalibrationDepthError):
        _cross_sectional_target(target_new_control_count=41)


def _plan(*, targets: tuple[CorporateStratumTarget, ...] | None = None) -> CorporateCalibrationPlan:
    stratum_targets = (
        (_cross_sectional_target(), _conditional_target()) if targets is None else targets
    )
    return CorporateCalibrationPlan.create(
        version="CORPORATE_CALIBRATION_DEPTH_V1",
        objective="Test objective for the calibration-depth plan.",
        source_agenda_id="agenda-1",
        corporate_dataset_id="corporate-dataset-1",
        equity_snapshot_id="equity-snapshot-1",
        replay_run_id="replay-run-1",
        cross_market_dataset_id="cross-market-dataset-1",
        stratum_targets=stratum_targets,
        controls_per_stratum=24,
        random_seed_policy="Fixed per-mechanism seed bases.",
        registered_at=NOW,
    )


def test_plan_is_deterministic_given_identical_inputs() -> None:
    first = _plan()
    second = _plan()
    assert first.calibration_id == second.calibration_id
    assert first.canonical_hash == second.canonical_hash


def test_plan_total_budget_equals_controls_per_stratum_times_stratum_count() -> None:
    plan = _plan()
    assert plan.total_new_control_budget == plan.controls_per_stratum * len(plan.stratum_targets)


def test_plan_rejects_lowering_the_minimum_matched_null_count() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        CorporateCalibrationPlan.create(
            version="V1",
            objective="Test.",
            source_agenda_id="agenda-1",
            corporate_dataset_id="corporate-dataset-1",
            equity_snapshot_id="equity-snapshot-1",
            replay_run_id="replay-run-1",
            cross_market_dataset_id="cross-market-dataset-1",
            stratum_targets=(_cross_sectional_target(),),
            controls_per_stratum=24,
            random_seed_policy="policy",
            registered_at=NOW,
            minimum_matched_null_count=MINIMUM_MATCHED_NULL_COUNT - 1,
        )


def test_plan_rejects_duplicate_strata() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        _plan(targets=(_cross_sectional_target(), _cross_sectional_target()))


def test_plan_rejects_a_stratum_whose_budget_disagrees_with_controls_per_stratum() -> None:
    mismatched = _cross_sectional_target(target_new_control_count=20)
    with pytest.raises(CorporateCalibrationDepthError):
        _plan(targets=(mismatched,))


def test_plan_requires_at_least_one_stratum() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        _plan(targets=())


# ==========================================================================
# Section 10 (run cursor)
# ==========================================================================


def test_run_draft_advance_and_completion() -> None:
    plan = _plan()
    run = CorporateCalibrationRun.draft(
        calibration=plan, spec_ids=("spec-1", "spec-2"), created_at=NOW
    )
    assert run.status is CalibrationRunStatus.NOT_STARTED
    step_one = run.advance(spec_id="spec-1", trials_added=3, is_final=False, at=NOW)
    assert step_one.status is CalibrationRunStatus.IN_PROGRESS
    assert step_one.completed_spec_ids == ("spec-1",)
    assert step_one.trials_executed == 3
    step_two = step_one.advance(spec_id="spec-2", trials_added=2, is_final=True, at=NOW)
    assert step_two.status is CalibrationRunStatus.COMPLETE
    assert step_two.completed_spec_ids == ("spec-1", "spec-2")
    assert step_two.trials_executed == 5


def test_run_cannot_advance_past_a_terminal_state() -> None:
    plan = _plan()
    run = CorporateCalibrationRun.draft(calibration=plan, spec_ids=("spec-1",), created_at=NOW)
    completed = run.advance(spec_id="spec-1", trials_added=1, is_final=True, at=NOW)
    with pytest.raises(CorporateCalibrationDepthError):
        completed.advance(spec_id="spec-1", trials_added=1, is_final=True, at=NOW)


def test_run_rejects_spec_count_exceeding_the_declared_budget() -> None:
    plan = _plan()
    too_many = tuple(f"spec-{i}" for i in range(plan.total_new_control_budget + 1))
    with pytest.raises(CorporateCalibrationDepthError):
        CorporateCalibrationRun.draft(calibration=plan, spec_ids=too_many, created_at=NOW)


# ==========================================================================
# Section 11: null-control provenance
# ==========================================================================


def _spec(
    *,
    mechanism: CorporateNullMechanism = CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT,
    research_design: ResearchDesign = ResearchDesign.CROSS_SECTIONAL,
    conditioning_shape: ConditioningShape = ConditioningShape.NUMERIC_SPLIT,
    secondary_context_dimension: str | None = None,
    secondary_condition_label: str | None = None,
) -> CorporateNullControlSpec:
    return CorporateNullControlSpec.create(
        calibration_id="calibration-1",
        matched_stratum=CROSS_SECTIONAL_STRATUM,
        research_design=research_design,
        conditioning_shape=conditioning_shape,
        template_id="CROSS_SECTIONAL_V1",
        mechanism=mechanism,
        feature_id="REVENUE_GROWTH_YOY",
        outcome_id="SUBSEQUENT_RETURN",
        horizon_sessions=20,
        secondary_context_dimension=secondary_context_dimension,
        secondary_condition_label=secondary_condition_label,
        seed=20260809402,
        preserved_properties=("MARKET_DATE_STRUCTURE", "NUMBER_OF_ISSUERS"),
        destroyed_relationship="ISSUER_SPECIFIC_FEATURE_OUTCOME_PAIRING",
        rationale="Deterministic issuer derangement for calibration depth.",
        ordinal=1,
        created_at=NOW,
    )


def test_null_control_spec_is_deterministic_given_identical_inputs() -> None:
    first = _spec()
    second = _spec()
    assert first.spec_id == second.spec_id
    assert first.canonical_hash == second.canonical_hash


def test_null_control_spec_rejects_a_mechanism_not_valid_for_its_design() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        _spec(
            mechanism=CorporateNullMechanism.SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION,
            research_design=ResearchDesign.CROSS_SECTIONAL,
        )


def test_null_control_spec_requires_secondary_context_for_conditional_design() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        _spec(
            mechanism=CorporateNullMechanism.SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION,
            research_design=ResearchDesign.CONDITIONAL,
            conditioning_shape=ConditioningShape.LABEL_SPLIT,
        )


def test_null_control_spec_accepts_a_valid_conditional_spec_with_secondary_context() -> None:
    spec = _spec(
        mechanism=CorporateNullMechanism.SEEDED_OUTCOME_SIGN_RANDOMIZATION_WITHIN_CONDITION,
        research_design=ResearchDesign.CONDITIONAL,
        conditioning_shape=ConditioningShape.LABEL_SPLIT,
        secondary_context_dimension="EQUITY_TREND",
        secondary_condition_label="UPTREND",
    )
    assert spec.null_target == "OUTCOME"


def test_null_control_spec_rejects_a_negative_seed() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        CorporateNullControlSpec.create(
            calibration_id="calibration-1",
            matched_stratum=CROSS_SECTIONAL_STRATUM,
            research_design=ResearchDesign.CROSS_SECTIONAL,
            conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
            template_id="CROSS_SECTIONAL_V1",
            mechanism=CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT,
            feature_id="REVENUE_GROWTH_YOY",
            outcome_id="SUBSEQUENT_RETURN",
            horizon_sessions=20,
            secondary_context_dimension=None,
            secondary_condition_label=None,
            seed=-1,
            preserved_properties=("MARKET_DATE_STRUCTURE",),
            destroyed_relationship="X",
            rationale="Test.",
            ordinal=1,
            created_at=NOW,
        )


def test_null_control_outcome_requires_lineage() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        CorporateNullControlOutcome.create(
            spec_id="",
            calibration_run_id="run-1",
            governance_policy_id="policy-1",
            exploratory_trial_id=None,
            validation_trial_id=None,
            holdout_trial_id=None,
            robustness_run_id=None,
            conclusion_id=None,
            promotion_evaluation_id=None,
            knowledge_status="ACTIVE_RESEARCH",
            cohens_d=None,
            blockers=(),
            created_at=NOW,
        )


# ==========================================================================
# Sections 15/16/17: reassessment
# ==========================================================================


def _reassessment(
    *,
    subject_kind: CorporateReassessmentSubjectKind = CorporateReassessmentSubjectKind.HYPOTHESIS,
    original_evidence_grade: str = "EXPLORATORY",
    original_matched_null_count: int = 0,
    new_matched_null_count: int = 24,
    new_evidence_grade: EvidenceGrade = EvidenceGrade.EXPLORATORY,
    incremental_null_delta_count: int | None = None,
    incremental_null_delta_percentile: Decimal | None = None,
    incremental_verdict: IncrementalCalibrationVerdict | None = None,
) -> CorporateReassessment:
    return CorporateReassessment.create(
        calibration_id="calibration-1",
        subject_id="hyp-1",
        subject_kind=subject_kind,
        matched_stratum=CROSS_SECTIONAL_STRATUM,
        original_conclusion_id="conclusion-1",
        original_conclusion_state="WEAK_SUPPORT",
        original_evidence_grade=original_evidence_grade,
        original_matched_null_count=original_matched_null_count,
        new_matched_null_count=new_matched_null_count,
        new_matched_percentile=Decimal("0.92"),
        new_pooled_null_count=200,
        new_evidence_grade=new_evidence_grade,
        cohens_d=Decimal("0.31"),
        rationale="Deterministic reassessment for testing.",
        created_at=NOW,
        incremental_null_delta_count=incremental_null_delta_count,
        incremental_null_delta_percentile=incremental_null_delta_percentile,
        incremental_verdict=incremental_verdict,
    )


def test_reassessment_never_mutates_the_original_conclusion_state() -> None:
    """Section 15's non-destructive requirement: the reassessment retains
    the original conclusion state and grade verbatim -- it is never
    overwritten by the newly computed grade, even when they differ."""

    item = _reassessment(
        original_evidence_grade="UNRELIABLE", new_evidence_grade=EvidenceGrade.SUGGESTIVE
    )
    assert item.original_conclusion_state == "WEAK_SUPPORT"
    assert item.original_evidence_grade == "UNRELIABLE"
    assert item.new_evidence_grade is EvidenceGrade.SUGGESTIVE


def test_reassessment_marks_changed_when_grade_or_matched_count_differs() -> None:
    changed = _reassessment(
        original_evidence_grade="UNRELIABLE", new_evidence_grade=EvidenceGrade.SUGGESTIVE
    )
    assert changed.assessment_changed is True
    unchanged = _reassessment(
        original_evidence_grade="EXPLORATORY",
        original_matched_null_count=24,
        new_matched_null_count=24,
        new_evidence_grade=EvidenceGrade.EXPLORATORY,
    )
    assert unchanged.assessment_changed is False


def test_reassessment_follow_up_eligibility_requires_sufficiency_and_a_strong_enough_grade() -> (
    None
):
    insufficient = _reassessment(
        new_matched_null_count=1, new_evidence_grade=EvidenceGrade.SUGGESTIVE
    )
    assert insufficient.follow_up_eligible is False
    unreliable = _reassessment(
        new_matched_null_count=24, new_evidence_grade=EvidenceGrade.UNRELIABLE
    )
    assert unreliable.follow_up_eligible is False
    eligible = _reassessment(new_matched_null_count=24, new_evidence_grade=EvidenceGrade.SUGGESTIVE)
    assert eligible.follow_up_eligible is True


def test_reassessment_incremental_fields_must_be_declared_together() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        _reassessment(incremental_null_delta_count=24, incremental_verdict=None)
    with pytest.raises(CorporateCalibrationDepthError):
        _reassessment(
            incremental_null_delta_count=None,
            incremental_verdict=IncrementalCalibrationVerdict.ORDINARY_NULL_VARIATION,
        )


def test_reassessment_accepts_a_negative_control_subject_kind() -> None:
    item = _reassessment(
        subject_kind=CorporateReassessmentSubjectKind.NEGATIVE_CONTROL,
        original_evidence_grade="NOT_PREVIOUSLY_GRADED_AS_CALIBRATION_SUBJECT",
    )
    assert item.subject_kind is CorporateReassessmentSubjectKind.NEGATIVE_CONTROL


# ==========================================================================
# Section 13: WEAK_SUPPORT diagnostic
# ==========================================================================


def test_weak_support_cause_is_panel_dependence_when_inference_unavailable() -> None:
    cause = classify_weak_support_cause(
        effective_sample=Decimal(3),
        minimum_effective_sample=Decimal(5),
        inference_available=False,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=True,
        robustness_passed=True,
        control_weak_rate_for_stratum=Decimal("0.10"),
    )
    assert cause is WeakSupportCause.PANEL_DEPENDENCE


def test_weak_support_cause_is_sparse_sample_below_the_minimum_effective_sample() -> None:
    cause = classify_weak_support_cause(
        effective_sample=Decimal(3),
        minimum_effective_sample=Decimal(5),
        inference_available=True,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=True,
        robustness_passed=True,
        control_weak_rate_for_stratum=Decimal("0.10"),
    )
    assert cause is WeakSupportCause.SPARSE_SAMPLE


def test_weak_support_cause_is_robustness_ordering_on_failed_robustness() -> None:
    cause = classify_weak_support_cause(
        effective_sample=Decimal(20),
        minimum_effective_sample=Decimal(5),
        inference_available=True,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=True,
        robustness_passed=False,
        control_weak_rate_for_stratum=Decimal("0.10"),
    )
    assert cause is WeakSupportCause.ROBUSTNESS_OR_HOLDOUT_ORDERING


def test_weak_support_cause_is_robustness_ordering_on_validation_holdout_disagreement() -> None:
    cause = classify_weak_support_cause(
        effective_sample=Decimal(20),
        minimum_effective_sample=Decimal(5),
        inference_available=True,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=False,
        robustness_passed=True,
        control_weak_rate_for_stratum=Decimal("0.10"),
    )
    assert cause is WeakSupportCause.ROBUSTNESS_OR_HOLDOUT_ORDERING


def test_weak_support_cause_is_ordinary_when_the_matched_controls_are_similarly_weak() -> None:
    cause = classify_weak_support_cause(
        effective_sample=Decimal(20),
        minimum_effective_sample=Decimal(5),
        inference_available=True,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=True,
        robustness_passed=True,
        control_weak_rate_for_stratum=Decimal("0.20"),
    )
    assert cause is WeakSupportCause.ORDINARY_NULL_BEHAVIOR


def test_weak_support_cause_is_template_permissiveness_when_controls_are_rarely_weak() -> None:
    cause = classify_weak_support_cause(
        effective_sample=Decimal(20),
        minimum_effective_sample=Decimal(5),
        inference_available=True,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=True,
        robustness_passed=True,
        control_weak_rate_for_stratum=Decimal("0.05"),
    )
    assert cause is WeakSupportCause.TEMPLATE_PERMISSIVENESS


def test_weak_support_cause_is_unresolved_with_no_control_rate_available() -> None:
    cause = classify_weak_support_cause(
        effective_sample=Decimal(20),
        minimum_effective_sample=Decimal(5),
        inference_available=True,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=True,
        robustness_passed=True,
        control_weak_rate_for_stratum=None,
    )
    assert cause is WeakSupportCause.UNRESOLVED


def test_weak_support_cause_never_tuned_covers_every_enum_member_reachably() -> None:
    """Section 13's "never tune WEAK_SUPPORT because the rate looks
    unattractive" -- a cheap proxy proof that every declared cause is
    actually reachable through the fixed rule, not dead vocabulary."""

    reachable = {
        classify_weak_support_cause(
            effective_sample=Decimal(3),
            minimum_effective_sample=Decimal(5),
            inference_available=False,
            validation_beats_benchmark=None,
            holdout_beats_benchmark=None,
            robustness_passed=None,
            control_weak_rate_for_stratum=None,
        ),
        classify_weak_support_cause(
            effective_sample=Decimal(3),
            minimum_effective_sample=Decimal(5),
            inference_available=True,
            validation_beats_benchmark=None,
            holdout_beats_benchmark=None,
            robustness_passed=None,
            control_weak_rate_for_stratum=None,
        ),
        classify_weak_support_cause(
            effective_sample=Decimal(20),
            minimum_effective_sample=Decimal(5),
            inference_available=True,
            validation_beats_benchmark=None,
            holdout_beats_benchmark=None,
            robustness_passed=False,
            control_weak_rate_for_stratum=None,
        ),
        classify_weak_support_cause(
            effective_sample=Decimal(20),
            minimum_effective_sample=Decimal(5),
            inference_available=True,
            validation_beats_benchmark=True,
            holdout_beats_benchmark=False,
            robustness_passed=True,
            control_weak_rate_for_stratum=None,
        ),
        classify_weak_support_cause(
            effective_sample=Decimal(20),
            minimum_effective_sample=Decimal(5),
            inference_available=True,
            validation_beats_benchmark=True,
            holdout_beats_benchmark=True,
            robustness_passed=True,
            control_weak_rate_for_stratum=None,
        ),
        classify_weak_support_cause(
            effective_sample=Decimal(20),
            minimum_effective_sample=Decimal(5),
            inference_available=True,
            validation_beats_benchmark=True,
            holdout_beats_benchmark=True,
            robustness_passed=True,
            control_weak_rate_for_stratum=Decimal("0.20"),
        ),
        classify_weak_support_cause(
            effective_sample=Decimal(20),
            minimum_effective_sample=Decimal(5),
            inference_available=True,
            validation_beats_benchmark=True,
            holdout_beats_benchmark=True,
            robustness_passed=True,
            control_weak_rate_for_stratum=Decimal("0.05"),
        ),
    }
    assert reachable == set(WeakSupportCause)


# ==========================================================================
# Sections 19/20: bounded bucketing helpers
# ==========================================================================


def test_bucket_effective_sample_thresholds() -> None:
    assert bucket_effective_sample(Decimal(3)) is EffectiveSampleBucket.SPARSE
    assert bucket_effective_sample(Decimal(8)) is EffectiveSampleBucket.MODERATE
    assert bucket_effective_sample(Decimal(19)) is EffectiveSampleBucket.MODERATE
    assert bucket_effective_sample(Decimal(20)) is EffectiveSampleBucket.ADEQUATE


def test_temporal_region_for_date_splits_into_early_middle_recent_terciles() -> None:
    dates = tuple(f"2024-01-{day:02d}" for day in range(1, 31))
    assert temporal_region_for_date(dates[0], ordered_dates=dates) is TemporalRegion.EARLY
    assert temporal_region_for_date(dates[15], ordered_dates=dates) is TemporalRegion.MIDDLE
    assert temporal_region_for_date(dates[-1], ordered_dates=dates) is TemporalRegion.RECENT


def test_temporal_region_for_date_returns_none_for_an_unknown_date() -> None:
    dates = ("2024-01-01", "2024-01-02", "2024-01-03")
    assert temporal_region_for_date("2099-01-01", ordered_dates=dates) is None


# ==========================================================================
# Section 22: the retained report
# ==========================================================================


def _report(
    *, policy_v2_justified: bool = False, policy_v2_id: str | None = None
) -> CorporateCalibrationReport:
    return CorporateCalibrationReport.create(
        calibration_id="calibration-1",
        calibration_run_id="run-1",
        strata_evaluated=10,
        controls_generated=240,
        controls_per_stratum=24,
        raw_false_positive_rate=Decimal("0.01"),
        corrected_false_positive_rate=Decimal("0.005"),
        false_weak_rate=Decimal("0.25"),
        false_suggestive_rate=Decimal("0.2"),
        false_validated_rate=Decimal("0"),
        false_promotion_rate=Decimal("0"),
        design_breakdown_labels=("CROSS_SECTIONAL", "FILING_EVENT"),
        stability_slice_labels=("TIME_PERIOD:EARLY",),
        sector_concentration_note="note",
        filing_event_sparsity_note="note",
        weak_support_diagnostic_note="note",
        genuine_reassessment_count=20,
        genuine_assessment_changed_count=18,
        control_reassessment_count=17,
        control_now_validated_or_strong_count=0,
        incremental_calibration_count=4,
        policy_v2_justified=policy_v2_justified,
        policy_v2_id=policy_v2_id,
        policy_recommendation_rationale="rationale",
        limitations=corporate_calibration_depth_warnings(),
        created_at=NOW,
    )


def test_report_requires_a_policy_v2_id_when_policy_v2_is_justified() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        _report(policy_v2_justified=True, policy_v2_id=None)


def test_report_rejects_a_policy_v2_id_when_policy_v2_is_not_justified() -> None:
    with pytest.raises(CorporateCalibrationDepthError):
        _report(policy_v2_justified=False, policy_v2_id="CORPORATE_EVIDENCE_GRADE_POLICY_V2")


def test_report_accepts_the_justified_and_unjustified_policy_shapes() -> None:
    unjustified = _report()
    assert unjustified.policy_v2_id is None
    justified = _report(policy_v2_justified=True, policy_v2_id="CORPORATE_EVIDENCE_GRADE_POLICY_V2")
    assert justified.policy_v2_id == "CORPORATE_EVIDENCE_GRADE_POLICY_V2"


def test_corporate_calibration_depth_warnings_disclose_v3_preservation_and_no_execution() -> None:
    warnings = corporate_calibration_depth_warnings()
    assert any("NEVER" in item or "never" in item for item in warnings)
    assert any("v3" in item for item in warnings)
