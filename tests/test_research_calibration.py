from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.cross_market_research import (
    DatedValue,
    FeatureFamily,
    KnowledgeStatus,
    OutcomeFamily,
)
from apex.domain.research_calibration import (
    CALIBRATED_PROMOTION_POLICY,
    EFFECT_SIZE_FLOOR_POLICY_V1,
    MAXIMUM_NEGATIVE_CONTROL_BUDGET,
    MINIMUM_ADEQUATE_CONTROL_COUNT,
    MINIMUM_NEGATIVE_CONTROL_BUDGET,
    CalibratedPromotionEvaluation,
    CalibrationBreakdown,
    CalibrationError,
    CalibrationPlan,
    CalibrationRateSummary,
    CalibrationRun,
    CalibrationRunStatus,
    EvidenceGrade,
    EvidenceProfile,
    FollowUpPriorityClass,
    NegativeControlQualityVerdict,
    NullControlDesignFamily,
    NullControlFamily,
    NullControlNature,
    NullControlSpec,
    NullGenerationMethod,
    PracticalSignificance,
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
from apex.domain.research_methodology import ResearchConclusionState

NOW = datetime(2026, 8, 7, tzinfo=UTC)


def _series(count: int = 30, seed: int = 0) -> tuple[DatedValue, ...]:
    values = [Decimal(str((i * 37 + seed * 13) % 23 - 11)) / Decimal(10) for i in range(count)]
    return tuple(DatedValue(f"2024-01-{i + 1:02d}", v) for i, v in enumerate(values))


def _plan(**overrides: object) -> CalibrationPlan:
    kwargs: dict[str, object] = {
        "version": "V1",
        "objective": "calibrate the methodology",
        "dataset_snapshot_id": "d1",
        "replay_run_id": "r1",
        "cross_market_dataset_id": "cmd1",
        "source_agenda_id": "agenda1",
        "research_methodology_policy_id": "RESEARCH_METHODOLOGY_V1",
        "allowed_feature_families": tuple(FeatureFamily),
        "allowed_outcome_families": tuple(OutcomeFamily),
        "allowed_horizons_sessions": (5, 20),
        "null_generation_methods": tuple(NullGenerationMethod),
        "negative_control_budget": 104,
        "random_seed_policy": "fixed seeds declared a priori",
        "multiple_testing_method": "BENJAMINI_HOCHBERG_FDR",
        "block_length_policy": "block lengths {5, 20} declared a priori",
        "conclusion_policy_id": "RESEARCH_SIGNAL_CALIBRATION_V1",
        "registered_at": NOW,
    }
    kwargs.update(overrides)
    return CalibrationPlan.create(**kwargs)  # type: ignore[arg-type]


def _spec(calibration: CalibrationPlan, **overrides: object) -> NullControlSpec:
    kwargs: dict[str, object] = {
        "calibration": calibration,
        "family": NullControlFamily.DETERMINISTIC_PERMUTATION,
        "nature": NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
        "design_family": NullControlDesignFamily.PURE_MATHEMATICAL_NULL,
        "control_kind": "BLOCK_PERMUTED_FEATURE",
        "null_method": NullGenerationMethod.BLOCK_PERMUTATION,
        "source_feature_id": "EQUITY_BREADTH_FRACTION_ADVANCING",
        "source_feature_family": FeatureFamily.EQUITY_BREADTH,
        "outcome_id": "EQUITY_FORWARD_RETURN",
        "outcome_family": OutcomeFamily.EQUITY_BROAD_RETURN,
        "horizon_sessions": 20,
        "method_params": (("block_length", "5"), ("seed", "1")),
        "rationale": "block-permuted breadth vs forward return",
        "ordinal": 1,
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return NullControlSpec.create(**kwargs)  # type: ignore[arg-type]


# ==========================================================================
# Null-generation primitives: determinism, point-in-time safety, structure
# ==========================================================================


def test_block_permute_is_seed_reproducible_and_preserves_dates_and_multiset() -> None:
    series = _series()
    first = block_permute(series, seed=7, block_length=5)
    second = block_permute(series, seed=7, block_length=5)
    assert first == second
    assert [item.session_date for item in first] == [item.session_date for item in series]
    assert sorted(item.value for item in first) == sorted(item.value for item in series)


def test_block_permute_preserves_within_block_adjacency() -> None:
    # A block of length 1 is equivalent to an IID shuffle of the *positions*;
    # a longer block must keep consecutive original values adjacent to each
    # other (though not necessarily at their original position) -- this is
    # the property that distinguishes it from a naive IID shuffle.
    series = _series(count=20)
    permuted = block_permute(series, seed=3, block_length=5)
    original_values = [item.value for item in series]
    permuted_values = [item.value for item in permuted]
    original_blocks = [tuple(original_values[i : i + 5]) for i in range(0, 20, 5)]
    permuted_blocks = [tuple(permuted_values[i : i + 5]) for i in range(0, 20, 5)]
    assert sorted(permuted_blocks) == sorted(original_blocks)


def test_block_permute_rejects_invalid_block_length() -> None:
    with pytest.raises(CalibrationError):
        block_permute(_series(), seed=1, block_length=0)


def test_block_permute_different_seeds_produce_different_orders_on_a_large_series() -> None:
    series = _series(count=60)
    first = block_permute(series, seed=1, block_length=5)
    second = block_permute(series, seed=2, block_length=5)
    assert first != second


def test_sign_randomize_is_seed_reproducible_and_preserves_magnitudes() -> None:
    series = _series()
    first = sign_randomize(series, seed=11)
    second = sign_randomize(series, seed=11)
    assert first == second
    assert sorted(abs(item.value) for item in first) == sorted(abs(item.value) for item in series)
    assert [item.session_date for item in first] == [item.session_date for item in series]


def test_synthetic_ar1_surrogate_is_seed_reproducible_and_never_echoes_the_real_path() -> None:
    series = _series(count=50)
    first = synthetic_ar1_surrogate(series, seed=5)
    second = synthetic_ar1_surrogate(series, seed=5)
    assert first == second
    assert [item.session_date for item in first] == [item.session_date for item in series]
    # The surrogate is a genuinely different path -- not a permutation, not
    # the identity transform.
    assert tuple(item.value for item in first) != tuple(item.value for item in series)


def test_synthetic_ar1_surrogate_approximately_preserves_declared_mean_and_volatility() -> None:
    series = _series(count=200)
    surrogate = synthetic_ar1_surrogate(series, seed=42)
    real_values = [item.value for item in series]
    surrogate_values = [item.value for item in surrogate]
    real_mean = sum(real_values) / Decimal(len(real_values))
    surrogate_mean = sum(surrogate_values) / Decimal(len(surrogate_values))
    # Loose bound: same order of magnitude, not an exact match (it is a
    # stochastic surrogate, not a deterministic rescaling).
    assert abs(real_mean - surrogate_mean) < Decimal("1.0")


def test_synthetic_ar1_surrogate_handles_degenerate_short_series_without_raising() -> None:
    series = _series(count=2)
    surrogate = synthetic_ar1_surrogate(series, seed=1)
    assert len(surrogate) == 2
    assert all(item.value is not None for item in surrogate)


def test_constant_series_is_a_trivially_vacuous_condition() -> None:
    dates = [f"2024-02-{i + 1:02d}" for i in range(10)]
    series = constant_series(dates)
    assert {item.value for item in series} == {Decimal(1)}
    assert [item.session_date for item in series] == dates


def test_null_transforms_never_reference_a_date_outside_the_source_series() -> None:
    """Point-in-time safety: every transform's output dates are exactly the
    source series' own dates -- no transform can smuggle in evidence from a
    date that was never part of the already-retained, already-point-in-time
    -safe input series."""

    series = _series(count=40)
    source_dates = {item.session_date for item in series}
    for transformed in (
        block_permute(series, seed=1, block_length=5),
        sign_randomize(series, seed=1),
        synthetic_ar1_surrogate(series, seed=1),
    ):
        assert {item.session_date for item in transformed} == source_dates


# ==========================================================================
# Calibration plan / null control spec
# ==========================================================================


def test_calibration_plan_requires_a_block_based_null_method() -> None:
    with pytest.raises(CalibrationError):
        _plan(null_generation_methods=(NullGenerationMethod.IID_DATE_SHUFFLE,))


@pytest.mark.parametrize(
    "budget", [MINIMUM_NEGATIVE_CONTROL_BUDGET - 1, MAXIMUM_NEGATIVE_CONTROL_BUDGET + 1]
)
def test_calibration_plan_rejects_budget_outside_declared_bounds(budget: int) -> None:
    with pytest.raises(CalibrationError):
        _plan(negative_control_budget=budget)


def test_calibration_plan_identity_is_a_pure_function_of_its_declared_fields() -> None:
    assert _plan().calibration_id == _plan().calibration_id
    assert _plan().calibration_id != _plan(source_agenda_id="agenda2").calibration_id


def test_null_control_spec_rejects_a_method_not_approved_by_its_plan() -> None:
    calibration = _plan(null_generation_methods=(NullGenerationMethod.BLOCK_PERMUTATION,))
    with pytest.raises(CalibrationError):
        _spec(calibration, null_method=NullGenerationMethod.SIGN_RANDOMIZATION)


def test_null_control_spec_rejects_unrecognized_kind() -> None:
    calibration = _plan()
    with pytest.raises(CalibrationError):
        _spec(calibration, control_kind="NOT_A_REAL_KIND")


def test_null_control_spec_rejects_horizon_not_approved_by_plan() -> None:
    calibration = _plan(allowed_horizons_sessions=(5, 20))
    with pytest.raises(CalibrationError):
        _spec(calibration, horizon_sessions=60)


def test_null_control_spec_identity_is_a_pure_function_of_its_declared_fields() -> None:
    calibration = _plan()
    first = _spec(calibration, ordinal=1)
    second = _spec(calibration, ordinal=1)
    assert first.spec_id == second.spec_id
    third = _spec(calibration, ordinal=2)
    assert first.spec_id != third.spec_id


# ==========================================================================
# Calibration run (bounded mutable cursor)
# ==========================================================================


def test_calibration_run_advance_is_forward_only_and_fails_closed_from_terminal_state() -> None:
    calibration = _plan()
    run = CalibrationRun.draft(calibration=calibration, spec_ids=("s1", "s2"), created_at=NOW)
    assert run.status is CalibrationRunStatus.NOT_STARTED
    run = run.advance(spec_id="s1", trials_added=3, is_final=False, at=NOW)
    assert run.status is CalibrationRunStatus.IN_PROGRESS
    run = run.advance(spec_id="s2", trials_added=3, is_final=True, at=NOW)
    assert run.status is CalibrationRunStatus.COMPLETE
    with pytest.raises(CalibrationError):
        run.advance(spec_id="s3", trials_added=1, is_final=True, at=NOW)


def test_calibration_run_rejects_spec_count_exceeding_declared_budget() -> None:
    calibration = _plan(negative_control_budget=50)
    with pytest.raises(CalibrationError):
        CalibrationRun.draft(
            calibration=calibration, spec_ids=tuple(f"s{i}" for i in range(51)), created_at=NOW
        )


# ==========================================================================
# Empirical false-discovery calibration (section 4)
# ==========================================================================


def test_calibration_rate_summary_computes_every_declared_rate() -> None:
    summary = CalibrationRateSummary.compute(
        total_controls=100,
        validation_candidates=100,
        raw_positive_findings=40,
        bh_fdr_survivors=2,
        holdout_survivors=45,
        robustness_survivors=90,
        weak_count=15,
        supported_count=1,
        promotion_eligible_count=0,
    )
    assert summary.raw_false_positive_rate == Decimal("0.40000000")
    assert summary.corrected_false_positive_rate == Decimal("0.02000000")
    assert summary.false_weak_rate == Decimal("0.15000000")
    assert summary.false_supported_rate == Decimal("0.01000000")
    assert summary.false_promotion_rate == Decimal("0")


def test_calibration_rate_summary_never_divides_by_zero() -> None:
    summary = CalibrationRateSummary.compute(
        total_controls=0,
        validation_candidates=0,
        raw_positive_findings=0,
        bh_fdr_survivors=0,
        holdout_survivors=0,
        robustness_survivors=0,
        weak_count=0,
        supported_count=0,
        promotion_eligible_count=0,
    )
    assert summary.raw_false_positive_rate == Decimal(0)
    assert summary.false_weak_rate == Decimal(0)


def test_calibration_rate_summary_rejects_counts_exceeding_the_total() -> None:
    with pytest.raises(CalibrationError):
        CalibrationRateSummary.compute(
            total_controls=10,
            validation_candidates=10,
            raw_positive_findings=11,
            bh_fdr_survivors=0,
            holdout_survivors=0,
            robustness_survivors=0,
            weak_count=0,
            supported_count=0,
            promotion_eligible_count=0,
        )


def test_calibration_breakdown_flags_inadequate_sample_without_hiding_it() -> None:
    summary = CalibrationRateSummary.compute(
        total_controls=3,
        validation_candidates=3,
        raw_positive_findings=1,
        bh_fdr_survivors=0,
        holdout_survivors=1,
        robustness_survivors=2,
        weak_count=1,
        supported_count=0,
        promotion_eligible_count=0,
    )
    breakdown = CalibrationBreakdown.create(
        calibration_id="c1",
        dimension_kind="FAMILY",
        dimension_label="SEEDED_SYNTHETIC_NULL",
        control_count=3,
        summary=summary,
        note="thin family",
        created_at=NOW,
    )
    assert breakdown.control_count < MINIMUM_ADEQUATE_CONTROL_COUNT
    assert breakdown.sample_adequate is False
    # The rate is still retained, never hidden.
    assert breakdown.summary.raw_false_positive_rate == Decimal("0.33333333")


def test_calibration_breakdown_adequate_when_at_or_above_the_declared_minimum() -> None:
    summary = CalibrationRateSummary.compute(
        total_controls=MINIMUM_ADEQUATE_CONTROL_COUNT,
        validation_candidates=MINIMUM_ADEQUATE_CONTROL_COUNT,
        raw_positive_findings=0,
        bh_fdr_survivors=0,
        holdout_survivors=0,
        robustness_survivors=0,
        weak_count=0,
        supported_count=0,
        promotion_eligible_count=0,
    )
    breakdown = CalibrationBreakdown.create(
        calibration_id="c1",
        dimension_kind="HORIZON",
        dimension_label="20_SESSIONS",
        control_count=MINIMUM_ADEQUATE_CONTROL_COUNT,
        summary=summary,
        note="adequate",
        created_at=NOW,
    )
    assert breakdown.sample_adequate is True


# ==========================================================================
# Effect-size floor and practical significance (section 6)
# ==========================================================================


def test_effect_meets_floor_uses_the_outcome_family_specific_threshold() -> None:
    floor = EFFECT_SIZE_FLOOR_POLICY_V1.floor_for(OutcomeFamily.EQUITY_BROAD_RETURN)
    assert effect_meets_floor(
        outcome_family=OutcomeFamily.EQUITY_BROAD_RETURN,
        cohens_d=floor + Decimal("0.01"),
        policy=EFFECT_SIZE_FLOOR_POLICY_V1,
    )
    assert not effect_meets_floor(
        outcome_family=OutcomeFamily.EQUITY_BROAD_RETURN,
        cohens_d=floor - Decimal("0.01"),
        policy=EFFECT_SIZE_FLOOR_POLICY_V1,
    )


def test_effect_meets_floor_is_false_without_a_computable_effect_size() -> None:
    assert not effect_meets_floor(
        outcome_family=OutcomeFamily.BTC_RETURN, cohens_d=None, policy=EFFECT_SIZE_FLOOR_POLICY_V1
    )


@pytest.mark.parametrize(
    ("significant", "meets_floor", "expected"),
    [
        (True, True, PracticalSignificance.BOTH),
        (True, False, PracticalSignificance.STATISTICAL_ONLY),
        (False, True, PracticalSignificance.PRACTICAL_ONLY),
        (False, False, PracticalSignificance.NEITHER),
    ],
)
def test_classify_practical_significance_covers_all_four_quadrants(
    significant: bool, meets_floor: bool, expected: PracticalSignificance
) -> None:
    assert (
        classify_practical_significance(
            statistically_significant=significant, meets_effect_floor=meets_floor
        )
        == expected
    )


# ==========================================================================
# Null-calibration percentile
# ==========================================================================


def test_null_calibration_percentile_is_none_without_a_null_distribution() -> None:
    assert null_calibration_percentile(statistic=Decimal("0.5"), null_statistics=()) is None


def test_null_calibration_percentile_ranks_by_absolute_magnitude() -> None:
    nulls = tuple(
        Decimal(str(x)) for x in (0.01, 0.02, 0.03, 0.04, 0.05, -0.06, 0.07, 0.08, 0.09, 0.10)
    )
    percentile = null_calibration_percentile(statistic=Decimal("0.05"), null_statistics=nulls)
    assert percentile == Decimal("0.50000000")
    top = null_calibration_percentile(statistic=Decimal("100"), null_statistics=nulls)
    assert top == Decimal("1.00000000")
    bottom = null_calibration_percentile(statistic=Decimal("0"), null_statistics=nulls)
    assert bottom == Decimal("0")


# ==========================================================================
# Evidence grading (section 8) -- the calibration-vocabulary heart of the
# milestone: a WEAK_SUPPORT conclusion can never structurally reach
# VALIDATED/STRONG, and a null-like percentile caps at EXPLORATORY.
# ==========================================================================


def _profile(**overrides: object) -> EvidenceProfile:
    kwargs: dict[str, object] = {
        "subject_id": "h1",
        "subject_kind": "HYPOTHESIS",
        "benchmark_relative_effect": Decimal("0.01"),
        "cohens_d": Decimal("0.1"),
        "validation_beats_benchmark": True,
        "holdout_result": "BEATS_BENCHMARK",
        "walk_forward_consistency": None,
        "robustness_passed": True,
        "corrected_significant": True,
        "effective_sample": Decimal("40"),
        "regime_concentration": "DISTRIBUTED_ACROSS_REGIMES",
        "null_percentile": Decimal("0.5"),
        "practical_significance": PracticalSignificance.BOTH,
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return EvidenceProfile.create(**kwargs)  # type: ignore[arg-type]


def test_evidence_grade_is_unreliable_for_a_failed_conclusion_regardless_of_percentile() -> None:
    profile = _profile(null_percentile=Decimal("0.999"))
    for state in (
        ResearchConclusionState.REJECTED,
        ResearchConclusionState.HOLDOUT_FAILED,
        ResearchConclusionState.ROBUSTNESS_FAILED,
    ):
        grade = derive_evidence_grade(
            conclusion_state=state, profile=profile, effect_meets_floor=True
        )
        assert grade is EvidenceGrade.UNRELIABLE


def test_evidence_grade_caps_at_exploratory_without_a_null_percentile() -> None:
    profile = _profile(null_percentile=None)
    grade = derive_evidence_grade(
        conclusion_state=ResearchConclusionState.SUPPORTED, profile=profile, effect_meets_floor=True
    )
    assert grade is EvidenceGrade.EXPLORATORY


def test_evidence_grade_caps_at_exploratory_for_null_like_percentile() -> None:
    profile = _profile(null_percentile=Decimal("0.5"))
    grade = derive_evidence_grade(
        conclusion_state=ResearchConclusionState.SUPPORTED, profile=profile, effect_meets_floor=True
    )
    assert grade is EvidenceGrade.EXPLORATORY


def test_evidence_grade_is_suggestive_in_the_null_tail_without_full_supported_evidence() -> None:
    profile = _profile(null_percentile=Decimal("0.95"))
    grade = derive_evidence_grade(
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        profile=profile,
        effect_meets_floor=True,
    )
    assert grade is EvidenceGrade.SUGGESTIVE


def test_weak_support_can_never_structurally_reach_validated_or_strong() -> None:
    """The central calibration-policy guarantee (section 8/9): regardless of
    how favorable every other fact is, a conclusion that only reached
    WEAK_SUPPORT is capped below VALIDATED."""

    profile = _profile(
        null_percentile=Decimal("0.999"),
        corrected_significant=True,
        robustness_passed=True,
        holdout_result="BEATS_BENCHMARK",
    )
    grade = derive_evidence_grade(
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        profile=profile,
        effect_meets_floor=True,
    )
    assert grade is EvidenceGrade.SUGGESTIVE
    assert not evidence_grade_at_least(grade, EvidenceGrade.VALIDATED)


def test_evidence_grade_reaches_validated_only_with_every_supporting_fact_at_once() -> None:
    profile = _profile(
        null_percentile=Decimal("0.96"),
        corrected_significant=True,
        robustness_passed=True,
        holdout_result="BEATS_BENCHMARK",
    )
    grade = derive_evidence_grade(
        conclusion_state=ResearchConclusionState.SUPPORTED, profile=profile, effect_meets_floor=True
    )
    assert grade is EvidenceGrade.VALIDATED


def test_evidence_grade_reaches_strong_only_at_the_extreme_null_tail() -> None:
    profile = _profile(
        null_percentile=Decimal("0.995"),
        corrected_significant=True,
        robustness_passed=True,
        holdout_result="BEATS_BENCHMARK",
    )
    grade = derive_evidence_grade(
        conclusion_state=ResearchConclusionState.SUPPORTED, profile=profile, effect_meets_floor=True
    )
    assert grade is EvidenceGrade.STRONG


def test_evidence_grade_cannot_reach_validated_or_strong_without_the_effect_floor() -> None:
    """VALIDATED/STRONG require holdout, robustness, correction, *and* effect
    evidence simultaneously (see ``derive_evidence_grade``'s docstring) --
    an otherwise-perfect profile still falls all the way to SUGGESTIVE (the
    null-tail-only grade) when the effect size never clears its declared
    outcome-family floor."""

    profile = _profile(
        null_percentile=Decimal("0.995"), corrected_significant=True, robustness_passed=True
    )
    grade = derive_evidence_grade(
        conclusion_state=ResearchConclusionState.SUPPORTED,
        profile=profile,
        effect_meets_floor=False,
    )
    assert grade is EvidenceGrade.SUGGESTIVE
    assert not evidence_grade_at_least(grade, EvidenceGrade.VALIDATED)


def test_evidence_grade_order_is_monotonic() -> None:
    order = [
        EvidenceGrade.UNRELIABLE,
        EvidenceGrade.EXPLORATORY,
        EvidenceGrade.SUGGESTIVE,
        EvidenceGrade.VALIDATED,
        EvidenceGrade.STRONG,
    ]
    for index, grade in enumerate(order):
        for other in order[: index + 1]:
            assert evidence_grade_at_least(grade, other)
        for other in order[index + 1 :]:
            assert not evidence_grade_at_least(grade, other)


# ==========================================================================
# Negative-control quality verdict (section 15)
# ==========================================================================


def test_negative_control_quality_is_unresolved_without_a_percentile() -> None:
    verdict = assess_negative_control_quality(
        null_percentile=None,
        robustness_passed=True,
        effective_sample=Decimal("30"),
        minimum_effective_sample=Decimal("10"),
    )
    assert verdict is NegativeControlQualityVerdict.UNRESOLVED


def test_negative_control_quality_is_methodology_false_positive_for_typical_null_magnitude() -> (
    None
):
    verdict = assess_negative_control_quality(
        null_percentile=Decimal("0.6"),
        robustness_passed=True,
        effective_sample=Decimal("30"),
        minimum_effective_sample=Decimal("10"),
    )
    assert verdict is NegativeControlQualityVerdict.METHODOLOGY_FALSE_POSITIVE


def test_negative_control_quality_is_unstable_artifact_for_thin_sample_or_failed_robustness() -> (
    None
):
    thin_sample = assess_negative_control_quality(
        null_percentile=Decimal("0.95"),
        robustness_passed=True,
        effective_sample=Decimal("2"),
        minimum_effective_sample=Decimal("10"),
    )
    assert thin_sample is NegativeControlQualityVerdict.UNSTABLE_ARTIFACT
    failed_robustness = assess_negative_control_quality(
        null_percentile=Decimal("0.95"),
        robustness_passed=False,
        effective_sample=Decimal("30"),
        minimum_effective_sample=Decimal("10"),
    )
    assert failed_robustness is NegativeControlQualityVerdict.UNSTABLE_ARTIFACT


def test_negative_control_quality_is_plausible_real_association_only_with_full_evidence() -> None:
    verdict = assess_negative_control_quality(
        null_percentile=Decimal("0.97"),
        robustness_passed=True,
        effective_sample=Decimal("30"),
        minimum_effective_sample=Decimal("10"),
    )
    assert verdict is NegativeControlQualityVerdict.PLAUSIBLE_REAL_ASSOCIATION


# ==========================================================================
# Promotion-policy calibration (section 9) -- strictly additive
# ==========================================================================


def test_calibrated_promotion_requires_both_base_eligibility_and_the_minimum_grade() -> None:
    for base_eligible, grade, expected in (
        (True, EvidenceGrade.STRONG, True),
        (True, EvidenceGrade.SUGGESTIVE, False),
        (False, EvidenceGrade.STRONG, False),
        (False, EvidenceGrade.EXPLORATORY, False),
    ):
        evaluation = CalibratedPromotionEvaluation.create(
            promotion_evaluation_id="pe1",
            calibration_id="c1",
            base_eligible=base_eligible,
            base_reasons=() if base_eligible else ("SOME_REASON",),
            evidence_grade=grade,
            policy=CALIBRATED_PROMOTION_POLICY,
            created_at=NOW,
        )
        assert evaluation.eligible is expected


def test_calibrated_promotion_rejects_weak_support_ceiling_grade_even_when_base_eligible() -> None:
    """A conclusion that only ever reached WEAK_SUPPORT structurally caps at
    SUGGESTIVE (see the evidence-grade tests above); this confirms that a
    SUGGESTIVE-graded, otherwise-eligible conclusion is still rejected by
    the calibrated production policy -- 'WEAK_SUPPORT alone' can never
    satisfy production promotion regardless of any other single fact."""

    evaluation = CalibratedPromotionEvaluation.create(
        promotion_evaluation_id="pe1",
        calibration_id="c1",
        base_eligible=True,
        base_reasons=(),
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        policy=CALIBRATED_PROMOTION_POLICY,
        created_at=NOW,
    )
    assert evaluation.eligible is False
    assert "EVIDENCE_GRADE_BELOW_CALIBRATED_PRODUCTION_MINIMUM" in evaluation.reasons


def test_calibrated_promotion_accepts_genuinely_stronger_evidence() -> None:
    evaluation = CalibratedPromotionEvaluation.create(
        promotion_evaluation_id="pe1",
        calibration_id="c1",
        base_eligible=True,
        base_reasons=(),
        evidence_grade=EvidenceGrade.VALIDATED,
        policy=CALIBRATED_PROMOTION_POLICY,
        created_at=NOW,
    )
    assert evaluation.eligible is True
    assert evaluation.reasons == ()


def test_calibrated_promotion_never_mutates_the_base_promotion_evaluation() -> None:
    evaluation = CalibratedPromotionEvaluation.create(
        promotion_evaluation_id="pe1",
        calibration_id="c1",
        base_eligible=True,
        base_reasons=(),
        evidence_grade=EvidenceGrade.STRONG,
        policy=CALIBRATED_PROMOTION_POLICY,
        created_at=NOW,
    )
    assert evaluation.promotion_evaluation_id == "pe1"
    assert evaluation.base_eligible is True
    assert evaluation.authority == "NONE"


def test_follow_up_priority_class_covers_every_declared_classification() -> None:
    assert {item.value for item in FollowUpPriorityClass} == {
        "JUSTIFIED",
        "LOW_PRIORITY",
        "UNSUPPORTED_AFTER_CALIBRATION",
        "REJECTED",
    }


def test_knowledge_status_reused_unmodified_for_null_control_outcomes() -> None:
    """Calibration null controls are graded through the identical
    ``KnowledgeStatus`` vocabulary genuine hypotheses use -- no separate,
    laxer vocabulary for "this is just a null control"."""

    assert set(KnowledgeStatus) == {
        KnowledgeStatus.ACTIVE_RESEARCH,
        KnowledgeStatus.SUPPORTED_FOR_FURTHER_STUDY,
        KnowledgeStatus.WEAK,
        KnowledgeStatus.REJECTED,
        KnowledgeStatus.RETIRED,
    }
