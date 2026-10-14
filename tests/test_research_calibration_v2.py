"""Domain-level proofs for Research Calibration v2: Template-Matched Nulls
and Stability. Pure, deterministic, no panel/database required."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.cross_market_research import HypothesisTemplateId, MarketId, OutcomeFamily
from apex.domain.research_calibration import (
    EvidenceGrade,
    NullControlDesignFamily,
    NullControlFamily,
    NullControlNature,
    NullGenerationMethod,
)
from apex.domain.research_calibration_v2 import (
    CALIBRATION_STRATUM_INSUFFICIENT,
    MAXIMUM_V2_CONTROL_BUDGET,
    MINIMUM_MATCHED_NULL_COUNT,
    MINIMUM_V2_CONTROL_BUDGET,
    CalibrationV2Error,
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
    research_calibration_v2_warnings,
    research_design_for_hypothesis,
)

NOW = datetime(2026, 8, 8, tzinfo=UTC)


def _spec(**overrides: object) -> NullControlSpecV2:
    kwargs: dict[str, object] = {
        "calibration_id": "calib_v2_1",
        "research_design": ResearchDesign.CONDITIONAL,
        "conditioning_shape": ConditioningShape.NUMERIC_SPLIT,
        "family": NullControlFamily.DETERMINISTIC_PERMUTATION,
        "nature": NullControlNature.MATHEMATICAL_SYNTHETIC_NULL,
        "design_family": NullControlDesignFamily.INTERACTION_CONDITIONAL,
        "control_kind": "CONDITIONAL_OUTCOME_NULLED",
        "null_method": NullGenerationMethod.BLOCK_PERMUTATION,
        "null_target": "OUTCOME",
        "source_feature_id": "BTC_RETURN_20",
        "source_feature_family": "BTC_MOMENTUM",
        "secondary_feature_id": "BTC_EQUITY_CORRELATION_60",
        "secondary_condition_rule": "ABOVE_TRAIN_MEDIAN",
        "secondary_condition_label": None,
        "outcome_id": "BTC_FORWARD_RETURN",
        "outcome_family": "BTC_RETURN",
        "horizon_sessions": 20,
        "market_scope": (MarketId.BTC, MarketId.US_EQUITY),
        "method_params": (("block_length", "5"), ("seed", "1")),
        "rationale": "test",
        "ordinal": 1,
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return NullControlSpecV2.create(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Section 2: research-design taxonomy / matched-stratum classification
# --------------------------------------------------------------------------


def test_predictive_template_same_market_classifies_predictive() -> None:
    design = research_design_for_hypothesis(
        template_id=HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        market_scope=(MarketId.US_EQUITY,),
    )
    assert design is ResearchDesign.PREDICTIVE


def test_predictive_template_cross_market_scope_classifies_cross_market() -> None:
    """The negative control (equity sector dispersion -> BTC volatility) is
    mechanically a PREDICTIVE_RELATIONSHIP_V1 template but spans two
    markets -- section 19 explicitly asks it be evaluated against
    cross-market nulls, so market scope decides here, not template alone."""

    design = research_design_for_hypothesis(
        template_id=HypothesisTemplateId.PREDICTIVE_RELATIONSHIP_V1,
        market_scope=(MarketId.US_EQUITY, MarketId.BTC),
    )
    assert design is ResearchDesign.CROSS_MARKET


def test_conditional_template_classifies_conditional_even_cross_market() -> None:
    design = research_design_for_hypothesis(
        template_id=HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1,
        market_scope=(MarketId.BTC, MarketId.US_EQUITY),
    )
    assert design is ResearchDesign.CONDITIONAL


def test_interaction_template_classifies_interaction() -> None:
    design = research_design_for_hypothesis(
        template_id=HypothesisTemplateId.INTERACTION_V1, market_scope=(MarketId.US_EQUITY,)
    )
    assert design is ResearchDesign.INTERACTION


def test_cross_market_template_classifies_cross_market() -> None:
    design = research_design_for_hypothesis(
        template_id=HypothesisTemplateId.CROSS_MARKET_RELATIONSHIP_V1,
        market_scope=(MarketId.RATES_PROXY, MarketId.BTC),
    )
    assert design is ResearchDesign.CROSS_MARKET


def test_persistence_template_classifies_persistence() -> None:
    design = research_design_for_hypothesis(
        template_id=HypothesisTemplateId.PERSISTENCE_V1, market_scope=(MarketId.BTC,)
    )
    assert design is ResearchDesign.PERSISTENCE


def test_regime_comparison_is_a_distinct_taxonomy_member_not_derivable_from_a_template() -> None:
    # No HypothesisTemplateId maps to REGIME_COMPARISON -- only null controls
    # exercise it (section 2: the taxonomy is complete even though no
    # genuine hypothesis currently uses this design).
    assert ResearchDesign.REGIME_COMPARISON not in {
        research_design_for_hypothesis(template_id=t, market_scope=(MarketId.US_EQUITY,))
        for t in HypothesisTemplateId
    }
    assert ResearchDesign.REGIME_COMPARISON.value == "REGIME_COMPARISON"


def test_matched_stratum_key_is_stable_across_market_scope_order() -> None:
    a = matched_stratum_key(
        research_design=ResearchDesign.CROSS_MARKET,
        outcome_family=OutcomeFamily.BTC_VOLATILITY,
        horizon_sessions=5,
        market_scope=(MarketId.US_EQUITY, MarketId.BTC),
        conditioning_shape=ConditioningShape.UNCONDITIONAL,
    )
    b = matched_stratum_key(
        research_design=ResearchDesign.CROSS_MARKET,
        outcome_family=OutcomeFamily.BTC_VOLATILITY,
        horizon_sessions=5,
        market_scope=(MarketId.BTC, MarketId.US_EQUITY),
        conditioning_shape=ConditioningShape.UNCONDITIONAL,
    )
    assert a == b


def test_matched_stratum_key_differs_on_horizon() -> None:
    base = dict(
        research_design=ResearchDesign.CONDITIONAL,
        outcome_family=OutcomeFamily.BTC_RETURN,
        market_scope=(MarketId.BTC,),
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
    )
    assert matched_stratum_key(horizon_sessions=5, **base) != matched_stratum_key(
        horizon_sessions=20, **base
    )


# --------------------------------------------------------------------------
# NullControlSpecV2 validation (section 3/4)
# --------------------------------------------------------------------------


def test_null_control_spec_v2_declares_its_research_design() -> None:
    spec = _spec()
    assert spec.research_design is ResearchDesign.CONDITIONAL
    assert spec.matched_stratum == matched_stratum_key(
        research_design=ResearchDesign.CONDITIONAL,
        outcome_family=OutcomeFamily.BTC_RETURN,
        horizon_sessions=20,
        market_scope=(MarketId.BTC, MarketId.US_EQUITY),
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
    )


def test_null_control_spec_v2_rejects_unknown_control_kind() -> None:
    with pytest.raises(CalibrationV2Error):
        _spec(control_kind="NOT_A_REAL_KIND")


def test_conditional_shape_requires_secondary_feature() -> None:
    with pytest.raises(CalibrationV2Error):
        _spec(secondary_feature_id=None, secondary_condition_rule=None)


def test_regime_comparison_shape_does_not_require_secondary_feature_but_requires_label_a() -> None:
    spec = _spec(
        research_design=ResearchDesign.REGIME_COMPARISON,
        conditioning_shape=ConditioningShape.REGIME_LABEL_COMPARISON,
        secondary_feature_id=None,
        secondary_condition_rule=None,
        source_feature_id="EQUITY_RISK_TONE_REGIME_LABEL",
        source_feature_family="NEGATIVE_CONTROL",
        method_params=(("label_a", "RISK_ON"), ("label_b", "RISK_OFF")),
    )
    assert spec.research_design is ResearchDesign.REGIME_COMPARISON
    with pytest.raises(CalibrationV2Error):
        _spec(
            research_design=ResearchDesign.REGIME_COMPARISON,
            conditioning_shape=ConditioningShape.REGIME_LABEL_COMPARISON,
            secondary_feature_id=None,
            secondary_condition_rule=None,
            source_feature_id="EQUITY_RISK_TONE_REGIME_LABEL",
            source_feature_family="NEGATIVE_CONTROL",
            method_params=(),
        )


def test_null_control_spec_v2_is_deterministic() -> None:
    first = _spec()
    second = _spec()
    assert first.spec_id == second.spec_id
    assert first.canonical_hash == second.canonical_hash


def test_null_control_spec_v2_ordinal_must_be_non_negative() -> None:
    with pytest.raises(CalibrationV2Error):
        _spec(ordinal=-1)


# --------------------------------------------------------------------------
# Matched percentile / insufficient-calibration fail-closed handling
# (sections 6/7)
# --------------------------------------------------------------------------


def test_matched_result_sufficient_when_at_or_above_minimum_count() -> None:
    nulls = tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT))
    result = MatchedCalibrationResult.evaluate(
        statistic=Decimal("50"),
        matched_null_statistics=nulls,
        pooled_null_statistics=nulls,
        matched_stratum="X",
    )
    assert result.sufficient is True
    assert result.matched_percentile is not None


def test_matched_result_insufficient_below_minimum_count_never_assigns_a_percentile() -> None:
    nulls = tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT - 1))
    result = MatchedCalibrationResult.evaluate(
        statistic=Decimal("50"),
        matched_null_statistics=nulls,
        pooled_null_statistics=nulls,
        matched_stratum="X",
    )
    assert result.sufficient is False
    assert result.matched_percentile is None


def test_pooled_percentile_still_reported_even_when_matched_is_insufficient() -> None:
    """Section 7: pooled calibration is reported separately for context
    even when the matched stratum fails closed."""

    matched = tuple(Decimal(i) for i in range(3))
    pooled = tuple(Decimal(i) for i in range(50))
    result = MatchedCalibrationResult.evaluate(
        statistic=Decimal("0.5"),
        matched_null_statistics=matched,
        pooled_null_statistics=pooled,
        matched_stratum="X",
    )
    assert result.sufficient is False
    assert result.matched_percentile is None
    assert result.pooled_percentile is not None
    assert result.pooled_null_count == 50


def test_matched_and_pooled_percentile_can_genuinely_differ() -> None:
    """A statistic that is extreme relative to a narrow matched population
    but ordinary relative to a broad pooled population must show a
    genuinely different matched-vs-pooled percentile -- proving the
    matched calculation is not silently falling back to the pooled one."""

    matched_nulls = tuple(Decimal("0.01") for _ in range(MINIMUM_MATCHED_NULL_COUNT))
    pooled_nulls = tuple(Decimal(i) for i in range(100))  # dominated by large values
    result = MatchedCalibrationResult.evaluate(
        statistic=Decimal("0.5"),
        matched_null_statistics=matched_nulls,
        pooled_null_statistics=pooled_nulls,
        matched_stratum="X",
    )
    assert result.matched_percentile == Decimal("1")
    assert result.pooled_percentile is not None
    assert result.pooled_percentile < Decimal("1")
    assert result.matched_percentile != result.pooled_percentile


def test_design_classification_caps_evidence_grade_when_insufficient() -> None:
    insufficient = MatchedCalibrationResult.evaluate(
        statistic=Decimal("5"),
        matched_null_statistics=(Decimal("1"), Decimal("2")),
        pooled_null_statistics=(Decimal("1"), Decimal("2")),
        matched_stratum="X",
    )
    classification = DesignClassification.create(
        calibration_id="calib_v2_1",
        subject_id="hyp_1",
        subject_kind="HYPOTHESIS",
        research_design=ResearchDesign.CONDITIONAL,
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
        matched=insufficient,
        raw_significant=True,
        corrected_significant=True,
        cohens_d=Decimal("5"),
        evidence_grade_v2=EvidenceGrade.EXPLORATORY,
        created_at=NOW,
    )
    assert classification.insufficiency_code == CALIBRATION_STRATUM_INSUFFICIENT


def test_design_classification_no_insufficiency_code_when_sufficient() -> None:
    sufficient = MatchedCalibrationResult.evaluate(
        statistic=Decimal("5"),
        matched_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        pooled_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        matched_stratum="X",
    )
    classification = DesignClassification.create(
        calibration_id="calib_v2_1",
        subject_id="hyp_1",
        subject_kind="HYPOTHESIS",
        research_design=ResearchDesign.CONDITIONAL,
        conditioning_shape=ConditioningShape.NUMERIC_SPLIT,
        matched=sufficient,
        raw_significant=True,
        corrected_significant=True,
        cohens_d=Decimal("5"),
        evidence_grade_v2=EvidenceGrade.SUGGESTIVE,
        created_at=NOW,
    )
    assert classification.insufficiency_code is None


def test_design_classification_rejects_invalid_subject_kind() -> None:
    matched = MatchedCalibrationResult.evaluate(
        statistic=None, matched_null_statistics=(), pooled_null_statistics=(), matched_stratum="X"
    )
    with pytest.raises(CalibrationV2Error):
        DesignClassification.create(
            calibration_id="c",
            subject_id="s",
            subject_kind="NOT_A_KIND",
            research_design=ResearchDesign.PREDICTIVE,
            conditioning_shape=ConditioningShape.UNCONDITIONAL,
            matched=matched,
            raw_significant=None,
            corrected_significant=None,
            cohens_d=None,
            evidence_grade_v2=EvidenceGrade.EXPLORATORY,
            created_at=NOW,
        )


# --------------------------------------------------------------------------
# Section 17: follow-up requalification vocabulary
# --------------------------------------------------------------------------


def test_classify_follow_up_v2_insufficient_when_matched_stratum_too_small() -> None:
    matched = MatchedCalibrationResult.evaluate(
        statistic=Decimal("5"),
        matched_null_statistics=(Decimal("1"),),
        pooled_null_statistics=(Decimal("1"),),
        matched_stratum="X",
    )
    assert (
        classify_follow_up_v2(matched=matched, evidence_grade=EvidenceGrade.VALIDATED)
        is FollowUpQualificationV2.CALIBRATION_INSUFFICIENT
    )


def test_classify_follow_up_v2_no_longer_justified_when_sufficient_but_exploratory() -> None:
    matched = MatchedCalibrationResult.evaluate(
        statistic=Decimal("0"),
        matched_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        pooled_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        matched_stratum="X",
    )
    assert (
        classify_follow_up_v2(matched=matched, evidence_grade=EvidenceGrade.EXPLORATORY)
        is FollowUpQualificationV2.NO_LONGER_JUSTIFIED
    )


def test_classify_follow_up_v2_rejected_when_unreliable() -> None:
    matched = MatchedCalibrationResult.evaluate(
        statistic=Decimal("5"),
        matched_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        pooled_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        matched_stratum="X",
    )
    assert (
        classify_follow_up_v2(matched=matched, evidence_grade=EvidenceGrade.UNRELIABLE)
        is FollowUpQualificationV2.REJECTED
    )


def test_classify_follow_up_v2_qualified_when_sufficient_and_suggestive_or_better() -> None:
    matched = MatchedCalibrationResult.evaluate(
        statistic=Decimal("5"),
        matched_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        pooled_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        matched_stratum="X",
    )
    for grade in (EvidenceGrade.SUGGESTIVE, EvidenceGrade.VALIDATED, EvidenceGrade.STRONG):
        assert (
            classify_follow_up_v2(matched=matched, evidence_grade=grade)
            is FollowUpQualificationV2.CALIBRATION_QUALIFIED
        )


def test_follow_up_qualification_never_touches_the_historical_proposal_row() -> None:
    """A FollowUpQualification record only ever *references* a follow-up
    proposal id and a parent hypothesis id -- it carries no field capable
    of mutating either, by construction (structural proof, not a runtime
    check: the dataclass simply has no such field)."""

    fields = {f for f in FollowUpQualification.__dataclass_fields__}
    assert "follow_up_id" in fields
    assert not any("proposal" in f and f != "follow_up_id" for f in fields)


def test_follow_up_qualification_is_deterministic_and_bound_to_calibration() -> None:
    matched = MatchedCalibrationResult.evaluate(
        statistic=Decimal("5"),
        matched_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        pooled_null_statistics=tuple(Decimal(i) for i in range(MINIMUM_MATCHED_NULL_COUNT)),
        matched_stratum="X",
    )
    kwargs = dict(
        calibration_id="calib_v2_1",
        follow_up_id="fu_1",
        parent_hypothesis_id="hyp_1",
        research_design=ResearchDesign.CONDITIONAL,
        horizon_sessions=20,
        matched=matched,
        evidence_grade_v2=EvidenceGrade.SUGGESTIVE,
        qualification=FollowUpQualificationV2.CALIBRATION_QUALIFIED,
        rationale="test rationale",
        created_at=NOW,
    )
    first = FollowUpQualification.create(**kwargs)  # type: ignore[arg-type]
    second = FollowUpQualification.create(**kwargs)  # type: ignore[arg-type]
    assert first.assessment_id == second.assessment_id


# --------------------------------------------------------------------------
# Stability slices (sections 10/11) and walk-forward null summaries
# (section 12)
# --------------------------------------------------------------------------


def test_stability_slice_retains_insufficient_samples_rather_than_hiding_them() -> None:
    slice_ = StabilitySlice.create(
        calibration_id="calib_v2_1",
        dimension_kind="VOLATILITY_REGIME",
        dimension_label="ELEVATED_VOLATILITY",
        control_count=3,
        raw_positive_count=1,
        corrected_positive_count=0,
        minimum_adequate_count=10,
        note="too few controls in this regime",
        created_at=NOW,
    )
    assert slice_.control_count == 3
    assert slice_.sample_adequate is False


def test_stability_slice_rejects_invalid_dimension_kind() -> None:
    with pytest.raises(CalibrationV2Error):
        StabilitySlice.create(
            calibration_id="c",
            dimension_kind="NOT_A_DIMENSION",
            dimension_label="X",
            control_count=1,
            raw_positive_count=0,
            corrected_positive_count=0,
            minimum_adequate_count=10,
            note="n",
            created_at=NOW,
        )


def test_stability_slice_rates_are_computed_correctly() -> None:
    slice_ = StabilitySlice.create(
        calibration_id="c",
        dimension_kind="TIME_PERIOD",
        dimension_label="EARLY",
        control_count=20,
        raw_positive_count=5,
        corrected_positive_count=1,
        minimum_adequate_count=10,
        note="n",
        created_at=NOW,
    )
    assert slice_.raw_false_positive_rate == Decimal("0.25")
    assert slice_.corrected_false_positive_rate == Decimal("0.05")
    assert slice_.sample_adequate is True


def test_walk_forward_null_summary_computes_fold_survival_rate() -> None:
    summary = WalkForwardNullSummary.create(
        calibration_id="c",
        spec_id="spec_1",
        fold_count=4,
        folds_beating_benchmark=1,
        validation_survived=False,
        holdout_survived=False,
        robustness_survived=False,
        final_evidence_grade=EvidenceGrade.EXPLORATORY,
        created_at=NOW,
    )
    assert summary.fold_survival_rate == Decimal("0.25")


def test_walk_forward_null_summary_rejects_folds_beating_exceeding_fold_count() -> None:
    with pytest.raises(CalibrationV2Error):
        WalkForwardNullSummary.create(
            calibration_id="c",
            spec_id="spec_1",
            fold_count=2,
            folds_beating_benchmark=3,
            validation_survived=False,
            holdout_survived=False,
            robustness_survived=False,
            final_evidence_grade=EvidenceGrade.EXPLORATORY,
            created_at=NOW,
        )


# --------------------------------------------------------------------------
# Budget bounds and warnings
# --------------------------------------------------------------------------


def test_v2_budget_bounds_are_120_to_180() -> None:
    assert MINIMUM_V2_CONTROL_BUDGET == 120
    assert MAXIMUM_V2_CONTROL_BUDGET == 180


def test_v2_warnings_include_lineage_note() -> None:
    warnings = research_calibration_v2_warnings()
    assert any("v1" in w.lower() for w in warnings)
    assert any("NO STRATEGY" in w for w in warnings)


def test_v2_warnings_include_finite_sample_uncertainty_disclosure() -> None:
    """Section 20: a zero-observed-error result must still carry
    finite-sample uncertainty -- never presented as proof of an exactly
    zero true false-discovery rate."""

    warnings = research_calibration_v2_warnings()
    assert any("finite-sample" in w.lower() for w in warnings)
    assert any("never proof" in w.lower() or "not proof" in w.lower() for w in warnings)
