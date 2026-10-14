"""Domain proofs for Corporate Calibration-Qualified Regime Follow-Up v1:
frozen-specification construction and parent-lineage/outcome/horizon/
benchmark enforcement, regime-dimension-selection validation, null-control
provenance validation, the incremental-contribution and parent-relationship
classifiers, the six-member follow-up conclusion vocabulary's explicit
priority ordering (including the "no significance-difference fallacy"
guard), the five-stage knowledge-history chain, and the structurally-pinned
independent-confirmation requirement. No database, no synthetic panel --
pure dataclass/validation proofs only.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.corporate_calibration_depth import CorporateNullMechanism
from apex.domain.corporate_regime_followup import (
    FALSE_DISCOVERY_RATE,
    INDEPENDENT_CONFIRMATION_REASONS,
    MINIMUM_MATCHED_NULL_COUNT,
    MULTIPLE_TESTING_FAMILY_SIZE,
    POST_HOC_CALIBRATION_QUALIFIED_FOLLOWUP,
    REGIME_GROUP_MINIMUM_DISTINCT_DATES,
    SIGNIFICANCE_FALLACY_GUARD_NOTE,
    CorporateRegimeFollowUpError,
    CorporateRegimeFollowUpResult,
    CorporateRegimeFollowUpSpecification,
    CorporateRegimeNullControl,
    IncrementalContributionVerdict,
    KnowledgeHistoryStage,
    ParentRelationshipBehavior,
    ParentRelationshipComparison,
    RegimeDimensionSelection,
    RegimeFollowUpConclusion,
    RegimeGroupEffect,
    RegimeGroupEvidence,
    RegimeIncrementalContribution,
    RegimeInteractionResult,
    RegimeSplitResult,
    RegimeSplitRole,
    build_knowledge_history,
    classify_incremental_contribution,
    classify_parent_relationship,
    classify_regime_follow_up_conclusion,
    corporate_regime_followup_warnings,
)
from apex.domain.cross_market_research import CrossMarketRegimeDimension, StabilityState
from apex.domain.research_calibration import EvidenceGrade
from apex.domain.research_calibration_v2 import MatchedCalibrationResult
from apex.domain.research_methodology import (
    BenchmarkKind,
    ResearchConclusionState,
    SampleQualityState,
)

NOW = datetime(2026, 8, 10, tzinfo=UTC)

PARENT_HYPOTHESIS_ID = "corporate_market_hypothesis_6d268d63f879f96f113e2737"
PARENT_CONCLUSION_ID = "research_conclusion_aec0fbefbc599bbb2507b383"
FOLLOW_UP_ID = "cross_market_follow_up_622f5a0d3af61dcb7b69bd9b"


def _selection(
    *,
    role: RegimeSplitRole = RegimeSplitRole.PRIMARY,
    dimension: CrossMarketRegimeDimension = CrossMarketRegimeDimension.EQUITY_TREND,
    budget: int = 30,
) -> RegimeDimensionSelection:
    return RegimeDimensionSelection.create(
        split_role=role,
        dimension=dimension,
        group_a_name="REGIME_ADVANCING",
        group_a_labels=("STRONG_UPTREND", "UPTREND"),
        group_b_name="REGIME_NOT_ADVANCING",
        group_b_labels=("RANGE_BOUND", "DOWNTREND", "STRONG_DOWNTREND"),
        excluded_labels=("INSUFFICIENT_EVIDENCE",),
        precedent_hypothesis_id="corporate_market_hypothesis_4d6eb652b767030c07b680a4",
        precedent_feature_id="REVENUE_GROWTH_YOY",
        selection_rationale=(
            "Deterministic sibling-feature precedent, not a scan of all dimensions."
        ),
        null_control_budget=budget,
    )


def _spec_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        follow_up_id=FOLLOW_UP_ID,
        parent_hypothesis_id=PARENT_HYPOTHESIS_ID,
        parent_conclusion_id=PARENT_CONCLUSION_ID,
        parent_reassessment_id="corporate_calibration_reassessment_9d58260f9e3d2edfbe21490f",
        parent_qualification_id="research_calibration_v2_follow_up_qualification_75ab788b364836c1b09a726e",
        agenda_id="corporate_market_research_agenda_cf064c47a65890d181a91c3f",
        feature_id="NET_INCOME_GROWTH_YOY",
        feature_transformation="(current - prior) / abs(prior)",
        outcome_id="SUBSEQUENT_RETURN",
        horizon_sessions=20,
        benchmark_kind=BenchmarkKind.EQUAL_WEIGHT_COHORT,
        condition_rule="ABOVE_TRAIN_MEDIAN",
        market_scope="US_EQUITY; 68-issuer genuine multi-asset corporate cohort",
        cohort_policy_id="research_cohort_policy_ec793204956c1df37cf16bd8",
        corporate_dataset_id="corporate_history_dataset_snapshot_a41d2675442ccf51cb483d26",
        equity_snapshot_id="historical_dataset_snapshot_6e9cdd455c1531289c7dba0a",
        replay_run_id="historical_replay_run_92d36a87085c5b027bba2129",
        cross_market_dataset_id="cross_market_dataset_658651c42f11671b454e83aa",
        original_train_start="2020-07-27",
        original_train_end="2024-08-01",
        original_validation_start="2024-10-01",
        original_validation_end="2025-08-01",
        original_holdout_start="2025-09-02",
        original_holdout_end="2026-07-01",
        original_train_raw_sample=2058,
        original_train_effective_sample=Decimal(62),
        original_validation_raw_sample=349,
        original_validation_effective_sample=Decimal(11),
        original_holdout_raw_sample=341,
        original_holdout_effective_sample=Decimal(11),
        original_conclusion_state="WEAK_SUPPORT",
        original_validation_beats_benchmark=True,
        original_holdout_beats_benchmark=True,
        original_validation_relative_result=Decimal("0.00657659"),
        original_calibrated_grade=EvidenceGrade.SUGGESTIVE,
        matched_null_stratum="CROSS_SECTIONAL|SUBSEQUENT_RETURN|H20|NUMERIC_SPLIT",
        matched_null_count=40,
        matched_null_percentile=Decimal("0.95"),
        follow_up_kind="SPLIT_BY_REGIME",
        follow_up_rationale="matched percentile 0.95; evidence grade SUGGESTIVE",
        follow_up_qualification="CALIBRATION_QUALIFIED",
        regime_selections=(
            _selection(
                role=RegimeSplitRole.PRIMARY, dimension=CrossMarketRegimeDimension.EQUITY_TREND
            ),
            _selection(
                role=RegimeSplitRole.SECONDARY,
                dimension=CrossMarketRegimeDimension.EQUITY_VOLATILITY,
            ),
        ),
        sample_scope_description="Uses the full retained historical panel.",
        created_at=NOW,
    )
    base.update(overrides)
    return base


def _spec() -> CorporateRegimeFollowUpSpecification:
    return CorporateRegimeFollowUpSpecification.create(**_spec_kwargs())


# ==========================================================================
# Freeze: exact parent lineage, immutable specification, no horizon/
# feature/outcome/benchmark mutation.
# ==========================================================================


def test_specification_freezes_exact_parent_lineage() -> None:
    spec = _spec()
    assert spec.follow_up_id == FOLLOW_UP_ID
    assert spec.parent_hypothesis_id == PARENT_HYPOTHESIS_ID
    assert spec.parent_conclusion_id == PARENT_CONCLUSION_ID
    assert spec.feature_id.value == "NET_INCOME_GROWTH_YOY"
    assert spec.outcome_id.value == "SUBSEQUENT_RETURN"
    assert spec.horizon_sessions == 20
    assert spec.benchmark_kind is BenchmarkKind.EQUAL_WEIGHT_COHORT
    assert spec.original_calibrated_grade is EvidenceGrade.SUGGESTIVE
    assert spec.matched_null_count == 40
    assert spec.matched_null_percentile == Decimal("0.95")
    assert spec.post_hoc_status == POST_HOC_CALIBRATION_QUALIFIED_FOLLOWUP
    assert spec.authority == "NONE"


def test_specification_is_immutable() -> None:
    spec = _spec()
    with pytest.raises(FrozenInstanceError):
        spec.horizon_sessions = 5  # type: ignore[misc]


def test_specification_rejects_outcome_other_than_subsequent_return() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpSpecification.create(**_spec_kwargs(outcome_id="DRAWDOWN"))


def test_specification_rejects_horizon_other_than_parent_twenty_sessions() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpSpecification.create(**_spec_kwargs(horizon_sessions=5))


def test_specification_rejects_benchmark_other_than_equal_weight_cohort() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpSpecification.create(
            **_spec_kwargs(benchmark_kind=BenchmarkKind.RANDOM_DATE_BASELINE)
        )


def test_specification_rejects_follow_up_kind_other_than_split_by_regime() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpSpecification.create(
            **_spec_kwargs(follow_up_kind="ALTERNATE_HORIZON")
        )


def test_specification_rejects_unqualified_follow_up() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpSpecification.create(
            **_spec_kwargs(follow_up_qualification="NO_LONGER_JUSTIFIED")
        )


def test_specification_rejects_bad_train_validation_ordering() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpSpecification.create(
            **_spec_kwargs(original_train_end="2025-01-01")  # after validation_start
        )


def test_specification_is_deterministic_given_identical_inputs() -> None:
    assert _spec().specification_id == _spec().specification_id
    assert _spec().canonical_hash == _spec().canonical_hash


def test_specification_independent_confirmation_is_structurally_pinned() -> None:
    spec = _spec()
    assert spec.independent_confirmation_required is True
    assert spec.independent_confirmation_reasons == INDEPENDENT_CONFIRMATION_REASONS
    assert "SAME_HISTORICAL_DATASET" in spec.independent_confirmation_reasons[0]
    with pytest.raises(TypeError):
        CorporateRegimeFollowUpSpecification.create(  # type: ignore[call-arg]
            **_spec_kwargs(), independent_confirmation_required=False
        )


# ==========================================================================
# Regime: predeclared split, disjoint groups, budget bounds.
# ==========================================================================


def test_regime_selection_groups_must_be_disjoint() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        RegimeDimensionSelection.create(
            split_role=RegimeSplitRole.PRIMARY,
            dimension=CrossMarketRegimeDimension.EQUITY_TREND,
            group_a_name="A",
            group_a_labels=("UPTREND",),
            group_b_name="B",
            group_b_labels=("UPTREND", "DOWNTREND"),
            excluded_labels=(),
            precedent_hypothesis_id="hyp-1",
            precedent_feature_id="REVENUE_GROWTH_YOY",
            selection_rationale="x" * 10,
            null_control_budget=30,
        )


def test_regime_selection_rejects_empty_group() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        RegimeDimensionSelection.create(
            split_role=RegimeSplitRole.PRIMARY,
            dimension=CrossMarketRegimeDimension.EQUITY_TREND,
            group_a_name="A",
            group_a_labels=(),
            group_b_name="B",
            group_b_labels=("DOWNTREND",),
            excluded_labels=(),
            precedent_hypothesis_id="hyp-1",
            precedent_feature_id="REVENUE_GROWTH_YOY",
            selection_rationale="x" * 10,
            null_control_budget=30,
        )


def test_regime_selection_rejects_budget_below_minimum_matched_null_count() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        _selection(budget=MINIMUM_MATCHED_NULL_COUNT - 1)


def test_regime_selection_rejects_unbounded_budget() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        _selection(budget=1000)


def test_specification_requires_exactly_one_primary_and_one_secondary_selection() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpSpecification.create(
            **_spec_kwargs(
                regime_selections=(
                    _selection(role=RegimeSplitRole.PRIMARY),
                    _selection(role=RegimeSplitRole.PRIMARY),
                )
            )
        )


def test_specification_rejects_duplicate_regime_dimensions_across_splits() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpSpecification.create(
            **_spec_kwargs(
                regime_selections=(
                    _selection(
                        role=RegimeSplitRole.PRIMARY,
                        dimension=CrossMarketRegimeDimension.EQUITY_TREND,
                    ),
                    _selection(
                        role=RegimeSplitRole.SECONDARY,
                        dimension=CrossMarketRegimeDimension.EQUITY_TREND,
                    ),
                )
            )
        )


def test_specification_multiplicity_family_size_is_two() -> None:
    assert _spec().multiple_testing_family_size == MULTIPLE_TESTING_FAMILY_SIZE == 2
    assert _spec().false_discovery_rate == FALSE_DISCOVERY_RATE


def test_specification_regime_group_minimum_distinct_dates_matches_panel_dependence_floor() -> None:
    # Section 6's adequacy floor reuses corporate_market_research's own
    # MINIMUM_EFFECTIVE_PANEL_CLUSTERS constant, not an independently
    # chosen number.
    assert _spec().regime_group_minimum_distinct_dates == REGIME_GROUP_MINIMUM_DISTINCT_DATES == 8


# ==========================================================================
# Null calibration: matched regime prevalence, deterministic controls,
# insufficient matched pool.
# ==========================================================================


def _null_control(**overrides: object) -> CorporateRegimeNullControl:
    base: dict[str, object] = dict(
        specification_id="corporate_regime_followup_specification_abc123",
        split_role=RegimeSplitRole.PRIMARY,
        ordinal=0,
        seed=20260809,
        mechanism=CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT,
        group_a_raw_count=100,
        group_a_effect=Decimal("0.01"),
        group_b_raw_count=400,
        group_b_effect=Decimal("0.02"),
        preserved_properties=("REAL_MARKET_DATES", "REAL_REGIME_GROUP_MEMBERSHIP_AND_PREVALENCE"),
        destroyed_relationship="feature-outcome pairing shuffled within date",
        created_at=NOW,
    )
    base.update(overrides)
    return CorporateRegimeNullControl.create(**base)  # type: ignore[arg-type]


def test_null_control_derives_interaction_from_both_group_effects() -> None:
    control = _null_control()
    assert control.interaction_effect == Decimal("-0.01")
    assert control.mechanism is CorporateNullMechanism.SEEDED_ISSUER_DERANGEMENT


def test_null_control_interaction_is_none_when_either_group_effect_missing() -> None:
    control = _null_control(group_a_effect=None)
    assert control.interaction_effect is None


def test_null_control_requires_preserved_properties() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        _null_control(preserved_properties=())


def test_null_control_rejects_negative_seed() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        _null_control(seed=-1)


def test_null_control_is_deterministic_given_identical_inputs() -> None:
    assert _null_control().control_id == _null_control().control_id


def test_matched_calibration_result_fails_closed_below_minimum_count() -> None:
    matched = MatchedCalibrationResult.evaluate(
        statistic=Decimal("0.05"),
        matched_null_statistics=tuple(Decimal("0.001") * i for i in range(5)),
        pooled_null_statistics=(),
        matched_stratum="REGIME_SPLIT|CROSS_SECTIONAL|SUBSEQUENT_RETURN|H20|EQUITY_TREND",
        minimum_matched_count=MINIMUM_MATCHED_NULL_COUNT,
    )
    assert matched.sufficient is False
    assert matched.matched_percentile is None  # fail-closed: never a fabricated percentile.


def test_matched_calibration_result_sufficient_at_or_above_minimum_count() -> None:
    nulls = tuple(Decimal("0.001") * i for i in range(MINIMUM_MATCHED_NULL_COUNT))
    matched = MatchedCalibrationResult.evaluate(
        statistic=Decimal("0.05"),
        matched_null_statistics=nulls,
        pooled_null_statistics=nulls,
        matched_stratum="REGIME_SPLIT|CROSS_SECTIONAL|SUBSEQUENT_RETURN|H20|EQUITY_TREND",
        minimum_matched_count=MINIMUM_MATCHED_NULL_COUNT,
    )
    assert matched.sufficient is True
    assert matched.matched_percentile is not None


# ==========================================================================
# Incremental contribution: regime-only explains result, genuine
# interaction, null variation, insufficient calibration.
# ==========================================================================


def _sufficient_matched(percentile: Decimal) -> MatchedCalibrationResult:
    nulls = tuple(Decimal("0.001") * i for i in range(MINIMUM_MATCHED_NULL_COUNT))
    return MatchedCalibrationResult(
        matched_stratum="s",
        matched_null_count=len(nulls),
        sufficient=True,
        matched_percentile=percentile,
        pooled_null_count=len(nulls),
        pooled_percentile=percentile,
    )


def _insufficient_matched() -> MatchedCalibrationResult:
    return MatchedCalibrationResult(
        matched_stratum="s",
        matched_null_count=5,
        sufficient=False,
        matched_percentile=None,
        pooled_null_count=5,
        pooled_percentile=None,
    )


def test_incremental_contribution_insufficiently_calibrated_below_minimum_pool() -> None:
    verdict = classify_incremental_contribution(
        matched=_insufficient_matched(),
        interaction_effect=Decimal("0.05"),
        regime_only_effect=Decimal("0.01"),
    )
    assert verdict is IncrementalContributionVerdict.INSUFFICIENTLY_CALIBRATED


def test_incremental_contribution_ordinary_null_variation_below_suggestive_floor() -> None:
    verdict = classify_incremental_contribution(
        matched=_sufficient_matched(Decimal("0.5")),
        interaction_effect=Decimal("0.05"),
        regime_only_effect=Decimal("0.01"),
    )
    assert verdict is IncrementalContributionVerdict.ORDINARY_NULL_VARIATION


def test_incremental_contribution_redundant_when_regime_alone_explains_result() -> None:
    verdict = classify_incremental_contribution(
        matched=_sufficient_matched(Decimal("0.95")),
        interaction_effect=Decimal("0.02"),
        regime_only_effect=Decimal("0.05"),  # regime-only >= interaction in magnitude
    )
    assert verdict is IncrementalContributionVerdict.REDUNDANT


def test_incremental_contribution_genuinely_incremental_when_regime_alone_is_smaller() -> None:
    verdict = classify_incremental_contribution(
        matched=_sufficient_matched(Decimal("0.95")),
        interaction_effect=Decimal("0.05"),
        regime_only_effect=Decimal("0.01"),
    )
    assert verdict is IncrementalContributionVerdict.GENUINELY_INCREMENTAL


def test_incremental_contribution_genuinely_incremental_when_regime_only_unavailable() -> None:
    verdict = classify_incremental_contribution(
        matched=_sufficient_matched(Decimal("0.95")),
        interaction_effect=Decimal("0.05"),
        regime_only_effect=None,
    )
    assert verdict is IncrementalContributionVerdict.GENUINELY_INCREMENTAL


# ==========================================================================
# Parent relationship preservation.
# ==========================================================================


def test_parent_relationship_disappeared_when_full_sample_effect_unavailable() -> None:
    behavior = classify_parent_relationship(
        original_validation_relative_result=Decimal("0.01"),
        full_sample_unsplit_relative_result=None,
    )
    assert behavior is ParentRelationshipBehavior.DISAPPEARED


def test_parent_relationship_reversed_on_sign_flip() -> None:
    behavior = classify_parent_relationship(
        original_validation_relative_result=Decimal("0.01"),
        full_sample_unsplit_relative_result=Decimal("-0.01"),
    )
    assert behavior is ParentRelationshipBehavior.REVERSED


def test_parent_relationship_consistent_when_magnitude_retained() -> None:
    behavior = classify_parent_relationship(
        original_validation_relative_result=Decimal("0.01"),
        full_sample_unsplit_relative_result=Decimal("0.008"),
    )
    assert behavior is ParentRelationshipBehavior.CONSISTENT


def test_parent_relationship_weakened_when_magnitude_shrinks_a_lot() -> None:
    behavior = classify_parent_relationship(
        original_validation_relative_result=Decimal("0.01"),
        full_sample_unsplit_relative_result=Decimal("0.001"),
    )
    assert behavior is ParentRelationshipBehavior.WEAKENED


# ==========================================================================
# Follow-up conclusion: explicit priority ordering, no significance-
# difference fallacy.
# ==========================================================================


def test_conclusion_insufficient_evidence_when_inadequate() -> None:
    conclusion = classify_regime_follow_up_conclusion(
        adequate=False,
        conclusion_state=None,
        evidence_grade=None,
        stability_state=None,
        incremental_verdict=None,
    )
    assert conclusion is RegimeFollowUpConclusion.INSUFFICIENT_EVIDENCE


def test_conclusion_incremental_value_absent_takes_priority_over_grade() -> None:
    conclusion = classify_regime_follow_up_conclusion(
        adequate=True,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        stability_state=StabilityState.STABLE,
        incremental_verdict=IncrementalContributionVerdict.REDUNDANT,
    )
    assert conclusion is RegimeFollowUpConclusion.INCREMENTAL_VALUE_ABSENT


def test_conclusion_unstable_on_robustness_failure() -> None:
    conclusion = classify_regime_follow_up_conclusion(
        adequate=True,
        conclusion_state=ResearchConclusionState.ROBUSTNESS_FAILED,
        evidence_grade=None,
        stability_state=None,
        incremental_verdict=IncrementalContributionVerdict.GENUINELY_INCREMENTAL,
    )
    assert conclusion is RegimeFollowUpConclusion.REGIME_EFFECT_UNSTABLE


def test_conclusion_null_compatible_on_rejected_raw_signal() -> None:
    conclusion = classify_regime_follow_up_conclusion(
        adequate=True,
        conclusion_state=ResearchConclusionState.REJECTED,
        evidence_grade=None,
        stability_state=None,
        incremental_verdict=IncrementalContributionVerdict.ORDINARY_NULL_VARIATION,
    )
    assert conclusion is RegimeFollowUpConclusion.REGIME_EFFECT_NULL_COMPATIBLE


def test_conclusion_null_compatible_when_grade_never_exceeds_exploratory() -> None:
    conclusion = classify_regime_follow_up_conclusion(
        adequate=True,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        evidence_grade=EvidenceGrade.EXPLORATORY,
        stability_state=StabilityState.STABLE,
        incremental_verdict=IncrementalContributionVerdict.GENUINELY_INCREMENTAL,
    )
    assert conclusion is RegimeFollowUpConclusion.REGIME_EFFECT_NULL_COMPATIBLE


def test_conclusion_unstable_when_stability_concentrated_despite_suggestive_grade() -> None:
    conclusion = classify_regime_follow_up_conclusion(
        adequate=True,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        stability_state=StabilityState.CONCENTRATED_IN_ONE_PERIOD,
        incremental_verdict=IncrementalContributionVerdict.GENUINELY_INCREMENTAL,
    )
    assert conclusion is RegimeFollowUpConclusion.REGIME_EFFECT_UNSTABLE


def test_conclusion_partial_regime_effect_on_ordinary_null_variation_incremental() -> None:
    conclusion = classify_regime_follow_up_conclusion(
        adequate=True,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        stability_state=StabilityState.STABLE,
        incremental_verdict=IncrementalContributionVerdict.ORDINARY_NULL_VARIATION,
    )
    assert conclusion is RegimeFollowUpConclusion.PARTIAL_REGIME_EFFECT


def test_conclusion_supported_requires_stable_and_genuinely_incremental_and_suggestive_plus() -> (
    None
):
    conclusion = classify_regime_follow_up_conclusion(
        adequate=True,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        stability_state=StabilityState.STABLE,
        incremental_verdict=IncrementalContributionVerdict.GENUINELY_INCREMENTAL,
    )
    assert conclusion is RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED


def test_conclusion_classifier_has_no_per_group_significance_parameter() -> None:
    # Section 8's guard, made structural rather than merely documented:
    # the classifier's only inputs are adequacy/conclusion-state (itself
    # derived from the *interaction's* own calibrated significance)/
    # evidence-grade/stability/incremental -- there is no "group A
    # significant" or "group B significant" parameter it could possibly
    # branch on.
    import inspect

    parameters = set(inspect.signature(classify_regime_follow_up_conclusion).parameters)
    assert parameters == {
        "adequate",
        "conclusion_state",
        "evidence_grade",
        "stability_state",
        "incremental_verdict",
    }
    assert SIGNIFICANCE_FALLACY_GUARD_NOTE in corporate_regime_followup_warnings()


# ==========================================================================
# Knowledge history: five-stage append-only chain.
# ==========================================================================


def test_knowledge_history_has_five_stages_in_order() -> None:
    history = build_knowledge_history(
        hypothesis_id=PARENT_HYPOTHESIS_ID,
        original_conclusion_id=PARENT_CONCLUSION_ID,
        original_conclusion_state="WEAK_SUPPORT",
        original_conclusion_created_at=NOW,
        reassessment_id="corporate_calibration_reassessment_9d58260f9e3d2edfbe21490f",
        calibrated_grade=EvidenceGrade.SUGGESTIVE,
        reassessment_created_at=NOW,
        follow_up_id=FOLLOW_UP_ID,
        qualification_id="research_calibration_v2_follow_up_qualification_75ab788b364836c1b09a726e",
        qualification="CALIBRATION_QUALIFIED",
        qualification_created_at=NOW,
        result_id="corporate_regime_followup_specification_abc123",
        result_conclusion=RegimeFollowUpConclusion.PARTIAL_REGIME_EFFECT,
        result_created_at=NOW,
    )
    assert [stage.stage_label for stage in history] == [
        "V3_HYPOTHESIS",
        "ORIGINAL_CONCLUSION",
        "DEEP_CALIBRATION_ASSESSMENT",
        "QUALIFIED_FOLLOW_UP",
        "REGIME_FOLLOW_UP_RESULT",
    ]
    assert history[0].reference_id == PARENT_HYPOTHESIS_ID
    assert history[0].recorded_at is None  # predeclared, not a timestamped fact.
    assert history[-1].state == RegimeFollowUpConclusion.PARTIAL_REGIME_EFFECT.value


def test_knowledge_history_never_overwrites_prior_stage_state() -> None:
    history = build_knowledge_history(
        hypothesis_id=PARENT_HYPOTHESIS_ID,
        original_conclusion_id=PARENT_CONCLUSION_ID,
        original_conclusion_state="WEAK_SUPPORT",
        original_conclusion_created_at=NOW,
        reassessment_id="r-1",
        calibrated_grade=EvidenceGrade.SUGGESTIVE,
        reassessment_created_at=NOW,
        follow_up_id=FOLLOW_UP_ID,
        qualification_id="q-1",
        qualification="CALIBRATION_QUALIFIED",
        qualification_created_at=NOW,
        result_id="result-1",
        result_conclusion=RegimeFollowUpConclusion.REGIME_EFFECT_NULL_COMPATIBLE,
        result_created_at=NOW,
    )
    # The original conclusion's own retained state is WEAK_SUPPORT no
    # matter what the follow-up eventually concludes.
    assert history[1].state == "WEAK_SUPPORT"
    assert history[2].state == EvidenceGrade.SUGGESTIVE.value


# ==========================================================================
# Interfaces: the full result's construction invariants.
# ==========================================================================


def _group_evidence(*, sufficient: bool = True) -> RegimeGroupEvidence:
    return RegimeGroupEvidence(
        group_name="REGIME_ADVANCING",
        labels=("UPTREND",),
        raw_observations=900 if sufficient else 10,
        distinct_effective_dates=14 if sufficient else 2,
        issuer_count=60 if sufficient else 3,
        sector_count=10,
        missing_feature_rate=Decimal("0.02"),
        sufficient=sufficient,
        insufficiency_reasons=() if sufficient else ("RAW_OBSERVATIONS_10_BELOW_MINIMUM_40",),
    )


def _group_effect(name: str) -> RegimeGroupEffect:
    return RegimeGroupEffect(
        group_name=name,
        treatment_count=400,
        benchmark_count=900,
        treatment_mean=Decimal("0.02"),
        benchmark_mean=Decimal("0.01"),
        effect=Decimal("0.01"),
        beats_benchmark=True,
        cohens_d=Decimal("0.1"),
        effective_sample=Decimal(14),
        standard_error=Decimal("0.001"),
        bootstrap_ci_lower=Decimal("-0.01"),
        bootstrap_ci_upper=Decimal("0.03"),
    )


def _interaction() -> RegimeInteractionResult:
    return RegimeInteractionResult(
        interaction_effect=Decimal("0.03"),
        combined_standard_error=Decimal("0.002"),
        interaction_cohens_d=Decimal("0.25"),
        effect_meets_floor=True,
        effect_floor_policy_id="CORPORATE_REGIME_FOLLOWUP_EFFECT_SIZE_FLOOR_V1_0.20",
        raw_p_value=Decimal("0.05"),
        raw_significant=True,
        matched_calibration=_sufficient_matched(Decimal("0.95")),
    )


def _split(role: RegimeSplitRole, *, adequate: bool = True) -> RegimeSplitResult:
    if not adequate:
        return RegimeSplitResult(
            role=role,
            selection=_selection(role=role),
            group_a_evidence=_group_evidence(sufficient=False),
            group_b_evidence=_group_evidence(sufficient=True),
            adequate=False,
            inadequacy_reason="RAW_OBSERVATIONS_10_BELOW_MINIMUM_40",
            group_a_effect=None,
            group_b_effect=None,
            interaction=None,
            interaction_stability=None,
            robustness=None,
            incremental=None,
            sample_quality=SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE,
            conclusion_state=ResearchConclusionState.INSUFFICIENT_EVIDENCE,
            evidence_grade=None,
            evidence_grade_rationale="inadequate",
        )
    return RegimeSplitResult(
        role=role,
        selection=_selection(role=role),
        group_a_evidence=_group_evidence(),
        group_b_evidence=_group_evidence(),
        adequate=True,
        inadequacy_reason=None,
        group_a_effect=_group_effect("REGIME_ADVANCING"),
        group_b_effect=_group_effect("REGIME_NOT_ADVANCING"),
        interaction=_interaction(),
        interaction_stability=None,
        robustness=None,
        incremental=RegimeIncrementalContribution(
            regime_only_effect=Decimal("0.005"),
            regime_only_sample=1000,
            corporate_only_effect=Decimal("0.01"),
            corporate_only_sample=1000,
            combined_interaction_effect=Decimal("0.03"),
            combined_sample=400,
            verdict=IncrementalContributionVerdict.GENUINELY_INCREMENTAL,
            rationale="genuinely incremental",
        ),
        sample_quality=SampleQualityState.SUFFICIENT,
        conclusion_state=ResearchConclusionState.WEAK_SUPPORT,
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        evidence_grade_rationale="suggestive",
    )


def _parent_relationship() -> ParentRelationshipComparison:
    return ParentRelationshipComparison(
        original_validation_relative_result=Decimal("0.01"),
        full_sample_unsplit_relative_result=Decimal("0.008"),
        group_a_effect=Decimal("0.03"),
        group_b_effect=Decimal("0.0"),
        behavior=ParentRelationshipBehavior.CONSISTENT,
        rationale="consistent",
    )


def _history(result_id: str = "result-placeholder") -> tuple[KnowledgeHistoryStage, ...]:
    return build_knowledge_history(
        hypothesis_id=PARENT_HYPOTHESIS_ID,
        original_conclusion_id=PARENT_CONCLUSION_ID,
        original_conclusion_state="WEAK_SUPPORT",
        original_conclusion_created_at=NOW,
        reassessment_id="r-1",
        calibrated_grade=EvidenceGrade.SUGGESTIVE,
        reassessment_created_at=NOW,
        follow_up_id=FOLLOW_UP_ID,
        qualification_id="q-1",
        qualification="CALIBRATION_QUALIFIED",
        qualification_created_at=NOW,
        result_id=result_id,
        result_conclusion=RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED,
        result_created_at=NOW,
    )


def test_result_requires_primary_and_secondary_roles_in_the_right_slots() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpResult.create(
            specification_id="spec-1",
            primary_split=_split(RegimeSplitRole.SECONDARY),
            secondary_split=_split(RegimeSplitRole.SECONDARY),
            multiple_testing_results=(),
            parent_relationship=_parent_relationship(),
            conclusion=RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED,
            conclusion_rationale="x",
            evidence_grade=EvidenceGrade.SUGGESTIVE,
            evidence_grade_rationale="x",
            knowledge_history=_history(),
            created_at=NOW,
        )


def test_result_requires_exactly_five_knowledge_history_stages() -> None:
    with pytest.raises(CorporateRegimeFollowUpError):
        CorporateRegimeFollowUpResult.create(
            specification_id="spec-1",
            primary_split=_split(RegimeSplitRole.PRIMARY),
            secondary_split=_split(RegimeSplitRole.SECONDARY),
            multiple_testing_results=(),
            parent_relationship=_parent_relationship(),
            conclusion=RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED,
            conclusion_rationale="x",
            evidence_grade=EvidenceGrade.SUGGESTIVE,
            evidence_grade_rationale="x",
            knowledge_history=_history()[:-1],
            created_at=NOW,
        )


def test_result_independent_confirmation_is_always_true_and_cannot_be_overridden() -> None:
    result = CorporateRegimeFollowUpResult.create(
        specification_id="spec-1",
        primary_split=_split(RegimeSplitRole.PRIMARY),
        secondary_split=_split(RegimeSplitRole.SECONDARY),
        multiple_testing_results=(),
        parent_relationship=_parent_relationship(),
        conclusion=RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED,
        conclusion_rationale="x",
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        evidence_grade_rationale="x",
        knowledge_history=_history(),
        created_at=NOW,
    )
    assert result.independent_confirmation_required is True
    assert result.independent_confirmation_reasons == INDEPENDENT_CONFIRMATION_REASONS
    assert "independent_confirmation_required" not in {
        field for field in result.__dataclass_fields__ if False
    }  # documents intent; the real guard is the .create() signature below.
    import inspect

    assert (
        "independent_confirmation_required"
        not in inspect.signature(CorporateRegimeFollowUpResult.create).parameters
    )


def test_result_is_immutable() -> None:
    result = CorporateRegimeFollowUpResult.create(
        specification_id="spec-1",
        primary_split=_split(RegimeSplitRole.PRIMARY),
        secondary_split=_split(RegimeSplitRole.SECONDARY),
        multiple_testing_results=(),
        parent_relationship=_parent_relationship(),
        conclusion=RegimeFollowUpConclusion.REGIME_EFFECT_SUPPORTED,
        conclusion_rationale="x",
        evidence_grade=EvidenceGrade.SUGGESTIVE,
        evidence_grade_rationale="x",
        knowledge_history=_history(),
        created_at=NOW,
    )
    with pytest.raises(FrozenInstanceError):
        result.conclusion = RegimeFollowUpConclusion.INSUFFICIENT_EVIDENCE  # type: ignore[misc]


def test_regime_group_adequacy_sparse_subgroup_is_representable() -> None:
    split = _split(RegimeSplitRole.SECONDARY, adequate=False)
    assert split.adequate is False
    assert split.interaction is None
    assert split.evidence_grade is None
    assert split.conclusion_state is ResearchConclusionState.INSUFFICIENT_EVIDENCE


def test_warnings_include_post_hoc_status_and_never_claim_independent_confirmation() -> None:
    warnings = corporate_regime_followup_warnings()
    assert POST_HOC_CALIBRATION_QUALIFIED_FOLLOWUP in warnings
    assert any("NOT INDEPENDENT REPLICATION" in item for item in warnings)
