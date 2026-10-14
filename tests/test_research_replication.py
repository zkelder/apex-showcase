"""Domain-level proofs for Calibration-Qualified Interaction Replication
v1. Pure, deterministic, no panel/database required."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.research_calibration import EvidenceGrade
from apex.domain.research_calibration_v2 import ConditioningShape, ResearchDesign
from apex.domain.research_replication import (
    REPLICATION_ROBUSTNESS_REQUIREMENTS,
    ConfoundDimensionResult,
    DatasetExposureLabel,
    DatasetExposureSegment,
    FollowUpReplicationQualification,
    PartitionKind,
    ReplicationConclusion,
    ReplicationConclusionState,
    ReplicationConfoundAssessment,
    ReplicationEffectProfile,
    ReplicationError,
    ReplicationPartitionResult,
    ReplicationSpecification,
    TimingAlignmentResult,
    TimingConvention,
    TransactionCostRelevance,
    classify_follow_up_replication,
    classify_transaction_cost_relevance,
    research_replication_warnings,
)

NOW = datetime(2026, 8, 8, tzinfo=UTC)


def _segment(**overrides: object) -> DatasetExposureSegment:
    kwargs: dict[str, object] = {
        "label": DatasetExposureLabel.CALIBRATION_V1_AND_V2_USED,
        "start_date": "2020-07-27",
        "end_date": "2026-06-30",
        "note": "test segment",
    }
    kwargs.update(overrides)
    return DatasetExposureSegment(**kwargs)  # type: ignore[arg-type]


def _spec(**overrides: object) -> ReplicationSpecification:
    kwargs: dict[str, object] = {
        "follow_up_id": "cross_market_follow_up_944ab68eaccd07fc752f3fca",
        "parent_hypothesis_id": "cross_market_hypothesis_6e98f1f88b365873c0bdac50",
        "parent_conclusion_id": "research_conclusion_f377fbbdbe355edacdf41db5",
        "parent_agenda_id": "cross_market_research_agenda_exact",
        "v1_calibration_id": "research_calibration_plan_2a84c4dc99182e1c68ad2531",
        "v1_priority_assessment_id": "research_calibration_follow_up_priority_exact",
        "v2_calibration_id": "research_calibration_plan_84b17abb2b37a9eb01dce4e8",
        "v2_classification_id": "research_calibration_v2_design_classification_exact",
        "v2_matched_stratum": "INTERACTION|BTC_RETURN|H20|BTC+US_EQUITY|NUMERIC_INTERACTION",
        "v2_matched_null_count": 15,
        "v2_matched_percentile": Decimal("1"),
        "research_design": ResearchDesign.INTERACTION,
        "conditioning_shape": ConditioningShape.NUMERIC_INTERACTION,
        "primary_feature_id": "BTC_RETURN_20",
        "primary_condition_rule": "ABOVE_TRAIN_MEDIAN",
        "secondary_feature_id": "EQUITY_VOLATILITY_20",
        "secondary_condition_rule": "ABOVE_TRAIN_MEDIAN",
        "market_scope": ("BTC", "US_EQUITY"),
        "outcome_id": "BTC_FORWARD_RETURN",
        "horizon_sessions": 20,
        "benchmark_kind": "PRIOR_REGIME_DISTRIBUTION",
        "dataset_snapshot_id": "historical_dataset_snapshot_exact",
        "replay_run_id": "historical_replay_run_exact",
        "cross_market_dataset_id": "cross_market_dataset_exact",
        "train_period_start": "2020-07-27",
        "train_period_end": "2023-10-25",
        "exposure_segments": (_segment(),),
        "is_in_sample_replication": True,
        "rationale": (
            "BTC's own momentum may behave differently when equity volatility is elevated "
            "versus not."
        ),
        "original_agenda_objective": "Systematically investigate a bounded set of questions.",
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return ReplicationSpecification.create(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Sections 1-3: immutable parent lineage / fixed configuration
# --------------------------------------------------------------------------


def test_specification_freezes_exact_parent_lineage() -> None:
    spec = _spec()
    assert spec.follow_up_id == "cross_market_follow_up_944ab68eaccd07fc752f3fca"
    assert spec.parent_hypothesis_id == "cross_market_hypothesis_6e98f1f88b365873c0bdac50"
    assert spec.primary_feature_id.value == "BTC_RETURN_20"
    assert spec.secondary_feature_id.value == "EQUITY_VOLATILITY_20"
    assert spec.horizon_sessions == 20
    assert spec.benchmark_kind == "PRIOR_REGIME_DISTRIBUTION"
    assert spec.v2_matched_null_count == 15
    assert spec.v2_matched_percentile == Decimal("1")
    assert spec.authority == "NONE"


def test_specification_is_deterministic_and_replays_identically() -> None:
    """Calling ``create`` twice with identical inputs produces the exact
    same id/hash -- the persistence-layer property that makes an idempotent
    re-run ("exact replay") possible."""

    first = _spec()
    second = _spec()
    assert first.replication_id == second.replication_id
    assert first.canonical_hash == second.canonical_hash


def test_specification_id_changes_if_any_frozen_field_changes() -> None:
    baseline = _spec()
    changed = _spec(horizon_sessions=5)
    assert baseline.replication_id != changed.replication_id


def test_specification_rejects_missing_parent_lineage() -> None:
    with pytest.raises(ReplicationError, match="REPLICATION_PARENT_LINEAGE_REQUIRED"):
        _spec(follow_up_id="")


def test_specification_rejects_missing_v1_lineage() -> None:
    with pytest.raises(ReplicationError, match="REPLICATION_V1_LINEAGE_REQUIRED"):
        _spec(v1_calibration_id="")


def test_specification_rejects_missing_v2_lineage() -> None:
    with pytest.raises(ReplicationError, match="REPLICATION_V2_LINEAGE_REQUIRED"):
        _spec(v2_matched_stratum="")


def test_specification_scope_is_interaction_design_only() -> None:
    """Section 24: this milestone runs exactly the one qualified
    INTERACTION follow-up -- never a CONDITIONAL or any other design."""

    with pytest.raises(ReplicationError, match="REPLICATION_SCOPE_IS_INTERACTION_DESIGN_ONLY"):
        _spec(research_design=ResearchDesign.CONDITIONAL)


def test_specification_scope_is_numeric_interaction_shape_only() -> None:
    with pytest.raises(
        ReplicationError, match="REPLICATION_SCOPE_IS_NUMERIC_INTERACTION_SHAPE_ONLY"
    ):
        _spec(conditioning_shape=ConditioningShape.NUMERIC_SPLIT)


def test_specification_rejects_invalid_train_period() -> None:
    with pytest.raises(ReplicationError, match="REPLICATION_TRAIN_PERIOD_INVALID"):
        _spec(train_period_start="2024-01-01", train_period_end="2020-01-01")


def test_specification_rejects_out_of_range_matched_percentile() -> None:
    with pytest.raises(ReplicationError, match="REPLICATION_V2_MATCHED_PERCENTILE_OUT_OF_RANGE"):
        _spec(v2_matched_percentile=Decimal("1.5"))


# --------------------------------------------------------------------------
# Section 4: no parameter search -- the robustness set must be small,
# predeclared, and non-repeating
# --------------------------------------------------------------------------


def test_default_robustness_requirements_are_small_and_predeclared() -> None:
    assert 1 <= len(REPLICATION_ROBUSTNESS_REQUIREMENTS) <= 8
    assert len(set(REPLICATION_ROBUSTNESS_REQUIREMENTS)) == len(REPLICATION_ROBUSTNESS_REQUIREMENTS)


def test_specification_rejects_oversized_robustness_set() -> None:
    """Guards against turning the predeclared sensitivity set into an
    open-ended parameter search."""

    oversized = tuple(f"CHECK_{i}" for i in range(9))
    with pytest.raises(
        ReplicationError, match="REPLICATION_ROBUSTNESS_SET_MUST_BE_SMALL_AND_PREDECLARED"
    ):
        _spec(robustness_requirements=oversized)


def test_specification_rejects_duplicate_robustness_entries() -> None:
    with pytest.raises(ReplicationError, match="REPLICATION_ROBUSTNESS_SET_MUST_NOT_REPEAT"):
        _spec(robustness_requirements=("A", "A"))


def test_specification_requires_exposure_segments() -> None:
    with pytest.raises(ReplicationError, match="REPLICATION_EXPOSURE_SEGMENTS_REQUIRED"):
        _spec(exposure_segments=())


# --------------------------------------------------------------------------
# Section 14/15: timing alignment
# --------------------------------------------------------------------------


def _timing(**overrides: object) -> TimingAlignmentResult:
    kwargs: dict[str, object] = {
        "replication_id": "research_replication_specification_exact",
        "convention": TimingConvention.PRIMARY_SAME_DAY_V1,
        "ordering_valid": True,
        "checked_date_count": 1489,
        "violation_count": 0,
        "sample_count": 249,
        "effective_sample_count": Decimal("12.45"),
        "benchmark_sample_count": 507,
        "interaction_effect": Decimal("0.023"),
        "cohens_d": Decimal("0.115"),
        "note": "test",
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return TimingAlignmentResult.create(**kwargs)  # type: ignore[arg-type]


def test_timing_alignment_valid_when_no_violations() -> None:
    result = _timing()
    assert result.ordering_valid is True
    assert result.violation_count == 0


def test_timing_alignment_rejects_inconsistent_counts() -> None:
    with pytest.raises(ReplicationError, match="TIMING_ALIGNMENT_COUNTS_INVALID"):
        _timing(checked_date_count=5, violation_count=10)


def test_timing_alignment_lagged_convention_is_distinct_identity() -> None:
    primary = _timing()
    lagged = _timing(convention=TimingConvention.LAGGED_EQUITY_ONE_SESSION_V1)
    assert primary.alignment_id != lagged.alignment_id


# --------------------------------------------------------------------------
# Sections 6/7: temporal and volatility-state partitions
# --------------------------------------------------------------------------


def _partition(**overrides: object) -> ReplicationPartitionResult:
    kwargs: dict[str, object] = {
        "replication_id": "research_replication_specification_exact",
        "partition_kind": PartitionKind.TIME_PERIOD,
        "label": "EARLY",
        "start_date": "2020-07-27",
        "end_date": "2022-07-14",
        "sample_count": 105,
        "effective_sample_count": Decimal("5.25"),
        "benchmark_sample_count": 220,
        "sample_mean": Decimal("0.02"),
        "benchmark_mean": Decimal("0.01"),
        "interaction_effect": Decimal("0.01"),
        "cohens_d": Decimal("0.03"),
        "beats_benchmark": True,
        "matched_null_count": 0,
        "matched_percentile": None,
        "calibration_note": "descriptive only",
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return ReplicationPartitionResult.create(**kwargs)  # type: ignore[arg-type]


def test_partition_result_sign_derived_from_effect() -> None:
    positive = _partition(interaction_effect=Decimal("0.05"))
    negative = _partition(interaction_effect=Decimal("-0.05"))
    zero = _partition(interaction_effect=Decimal("0"))
    unavailable = _partition(interaction_effect=None)
    assert positive.sign == 1
    assert negative.sign == -1
    assert zero.sign == 0
    assert unavailable.sign == 0


def test_full_sample_partition_may_carry_matched_percentile() -> None:
    full = _partition(
        partition_kind=PartitionKind.FULL_SAMPLE,
        label="FULL_SAMPLE",
        matched_null_count=15,
        matched_percentile=Decimal("1"),
    )
    assert full.matched_percentile == Decimal("1")
    assert full.matched_null_count == 15


def test_finer_partition_reports_zero_matched_null_count_by_convention() -> None:
    """Section 7: no per-partition matched-null battery was built; a finer
    slice must never claim a genuine matched percentile."""

    volatility = _partition(
        partition_kind=PartitionKind.VOLATILITY_STATE,
        label="HIGH_VOLATILITY",
        matched_null_count=0,
        matched_percentile=None,
    )
    assert volatility.matched_percentile is None
    assert volatility.matched_null_count == 0


def test_partition_result_rejects_inconsistent_matched_null_count() -> None:
    with pytest.raises(ReplicationError, match="PARTITION_RESULT_MATCHED_NULL_COUNT_INVALID"):
        _partition(matched_null_count=-1)


# --------------------------------------------------------------------------
# Section 16: transaction-cost relevance
# --------------------------------------------------------------------------


def test_transaction_cost_relevance_classifies_by_magnitude_ratio() -> None:
    cost_rate = Decimal("0.014")
    assert (
        classify_transaction_cost_relevance(effect=Decimal("0.001"), round_trip_cost_rate=cost_rate)
        is TransactionCostRelevance.BELOW_COST_SCALE
    )
    assert (
        classify_transaction_cost_relevance(effect=Decimal("0.02"), round_trip_cost_rate=cost_rate)
        is TransactionCostRelevance.COMPARABLE_TO_COST_SCALE
    )
    assert (
        classify_transaction_cost_relevance(effect=Decimal("0.20"), round_trip_cost_rate=cost_rate)
        is TransactionCostRelevance.ABOVE_COST_SCALE
    )
    assert (
        classify_transaction_cost_relevance(effect=None, round_trip_cost_rate=cost_rate)
        is TransactionCostRelevance.COST_EVIDENCE_UNAVAILABLE
    )


# --------------------------------------------------------------------------
# Section 13: regime-confound assessment
# --------------------------------------------------------------------------


def _confound_dimension(**overrides: object) -> ConfoundDimensionResult:
    kwargs: dict[str, object] = {
        "dimension": "BTC_VOLATILITY",
        "dominant_trial_label": "HIGH_VOLATILITY",
        "trial_concentration": Decimal("0.89"),
        "dominant_benchmark_label": "HIGH_VOLATILITY",
        "benchmark_concentration": Decimal("0.40"),
        "same_dominant_label": True,
        "effect_within_dominant_label": Decimal("0.008"),
        "dominant_label_sample_count": 214,
        "confound_plausible": True,
        "note": "test",
    }
    kwargs.update(overrides)
    return ConfoundDimensionResult(**kwargs)  # type: ignore[arg-type]


def test_confound_assessment_overall_resolved_when_no_dimension_plausible() -> None:
    dims = (
        _confound_dimension(confound_plausible=False),
        _confound_dimension(dimension="EQUITY_TREND", confound_plausible=False),
    )
    assessment = ReplicationConfoundAssessment.create(
        replication_id="research_replication_specification_exact",
        dimensions=dims,
        note="test",
        created_at=NOW,
    )
    assert assessment.overall_confound_resolved is True


def test_confound_assessment_unresolved_when_any_dimension_plausible() -> None:
    dims = (
        _confound_dimension(confound_plausible=False),
        _confound_dimension(dimension="BOND_RISK_RELATIONSHIP", confound_plausible=True),
    )
    assessment = ReplicationConfoundAssessment.create(
        replication_id="research_replication_specification_exact",
        dimensions=dims,
        note="test",
        created_at=NOW,
    )
    assert assessment.overall_confound_resolved is False


def test_confound_assessment_requires_at_least_one_dimension() -> None:
    with pytest.raises(ReplicationError, match="CONFOUND_ASSESSMENT_DIMENSIONS_REQUIRED"):
        ReplicationConfoundAssessment.create(
            replication_id="research_replication_specification_exact",
            dimensions=(),
            note="test",
            created_at=NOW,
        )


# --------------------------------------------------------------------------
# Sections 10-12: effect profile (practical floor, outlier robustness)
# --------------------------------------------------------------------------


def _effect_profile(**overrides: object) -> ReplicationEffectProfile:
    kwargs: dict[str, object] = {
        "replication_id": "research_replication_specification_exact",
        "unconditional_mean": Decimal("0.04"),
        "unconditional_sample_count": 1469,
        "low_vol_mean": Decimal("0.058"),
        "low_vol_sample_count": 507,
        "low_vol_median": Decimal("0.005"),
        "low_vol_hit_rate": Decimal("0.55"),
        "low_vol_stdev": Decimal("0.2"),
        "high_vol_mean": Decimal("0.081"),
        "high_vol_sample_count": 249,
        "high_vol_median": Decimal("0.066"),
        "high_vol_hit_rate": Decimal("0.60"),
        "high_vol_stdev": Decimal("0.19"),
        "high_vol_p10": Decimal("-0.1"),
        "high_vol_p90": Decimal("0.3"),
        "interaction_effect": Decimal("0.023"),
        "cohens_d": Decimal("0.115"),
        "benchmark_relative_effect": Decimal("0.023"),
        "practical_effect_floor": Decimal("0.20"),
        "winsorized_interaction_effect": Decimal("0.034"),
        "robust_median_interaction_effect": Decimal("0.061"),
        "leave_one_third_out_effects": (("EXCLUDE_EARLY", Decimal("0.007")),),
        "holdout_window_excluded_effect": Decimal("0.003"),
        "most_influential_dates": ("2020-11-27", "2020-12-01"),
        "outlier_robust": False,
        "outlier_dependence_note": "test",
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return ReplicationEffectProfile.create(**kwargs)  # type: ignore[arg-type]


def test_effect_profile_meets_practical_floor_only_above_threshold() -> None:
    below = _effect_profile(cohens_d=Decimal("0.11"), practical_effect_floor=Decimal("0.20"))
    above = _effect_profile(cohens_d=Decimal("0.35"), practical_effect_floor=Decimal("0.20"))
    assert below.meets_practical_floor is False
    assert above.meets_practical_floor is True


def test_effect_profile_conflates_neither_statistical_nor_practical_significance() -> None:
    """Section 10: statistical unusualness (matched percentile, retained on
    the partition) and practical materiality (this profile's own
    ``meets_practical_floor``) are two genuinely separate, independently
    retained facts."""

    profile = _effect_profile(cohens_d=Decimal("0.115"), practical_effect_floor=Decimal("0.20"))
    assert profile.meets_practical_floor is False  # small effect
    # yet nothing on this dataclass claims or implies statistical percentile


def test_effect_profile_rejects_non_positive_floor() -> None:
    with pytest.raises(ReplicationError, match="EFFECT_PROFILE_PRACTICAL_FLOOR_INVALID"):
        _effect_profile(practical_effect_floor=Decimal("0"))


# --------------------------------------------------------------------------
# Sections 17-20: replication conclusion / follow-up qualification
# --------------------------------------------------------------------------


def _conclusion(**overrides: object) -> ReplicationConclusion:
    kwargs: dict[str, object] = {
        "replication_id": "research_replication_specification_exact",
        "follow_up_id": "cross_market_follow_up_944ab68eaccd07fc752f3fca",
        "parent_hypothesis_id": "cross_market_hypothesis_6e98f1f88b365873c0bdac50",
        "conclusion_state": ReplicationConclusionState.REPLICATED,
        "evidence_grade": EvidenceGrade.SUGGESTIVE,
        "evidence_grade_upgraded": False,
        "period_stability_note": "test",
        "volatility_stability_note": "test",
        "timing_note": "test",
        "matched_null_note": "test",
        "effect_size_note": "test",
        "outlier_robustness_note": "test",
        "confound_note": "test",
        "transaction_cost_relevance": TransactionCostRelevance.COMPARABLE_TO_COST_SCALE,
        "forward_confirmation_required": True,
        "forward_confirmation_reason": "test",
        "minimum_forward_observation_policy": "test",
        "limitations": ("IN_SAMPLE_REPLICATION_NOT_INDEPENDENT_CONFIRMATION",),
        "rationale": "test",
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return ReplicationConclusion.create(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("state", "forward_required", "expected"),
    [
        (
            ReplicationConclusionState.REPLICATED,
            False,
            FollowUpReplicationQualification.REPLICATION_SUPPORTED,
        ),
        (
            ReplicationConclusionState.REPLICATED,
            True,
            FollowUpReplicationQualification.FORWARD_CONFIRMATION_REQUIRED,
        ),
        (
            ReplicationConclusionState.PARTIALLY_REPLICATED,
            True,
            FollowUpReplicationQualification.FORWARD_CONFIRMATION_REQUIRED,
        ),
        (ReplicationConclusionState.UNSTABLE, True, FollowUpReplicationQualification.RESEARCH_ONLY),
        (
            ReplicationConclusionState.NULL_COMPATIBLE,
            True,
            FollowUpReplicationQualification.REJECTED,
        ),
        (
            ReplicationConclusionState.TIMING_INVALID,
            True,
            FollowUpReplicationQualification.REJECTED,
        ),
        (
            ReplicationConclusionState.INSUFFICIENT_EVIDENCE,
            True,
            FollowUpReplicationQualification.INSUFFICIENT,
        ),
        (
            ReplicationConclusionState.CONFOUNDING_NOT_RESOLVED,
            True,
            FollowUpReplicationQualification.INSUFFICIENT,
        ),
    ],
)
def test_classify_follow_up_replication_is_deterministic(state, forward_required, expected) -> None:
    assert (
        classify_follow_up_replication(
            conclusion_state=state, forward_confirmation_required=forward_required
        )
        is expected
    )


def test_conclusion_qualification_is_derived_not_freely_chosen() -> None:
    """The ``.create`` classmethod always derives ``qualification`` itself
    -- a caller cannot pass an inconsistent one."""

    conclusion = _conclusion(
        conclusion_state=ReplicationConclusionState.UNSTABLE, forward_confirmation_required=True
    )
    assert conclusion.qualification is FollowUpReplicationQualification.RESEARCH_ONLY


def test_conclusion_never_claims_alpha_or_causality_in_retained_vocabulary() -> None:
    for state in ReplicationConclusionState:
        assert "ALPHA" not in state.value
        assert "CAUSE" not in state.value
        assert "CAUSAL" not in state.value


def test_conclusion_rejects_missing_lineage() -> None:
    with pytest.raises(ReplicationError, match="REPLICATION_CONCLUSION_LINEAGE_REQUIRED"):
        _conclusion(follow_up_id="")


def test_every_replication_record_has_authority_none() -> None:
    spec = _spec()
    conclusion = _conclusion()
    assert spec.authority == "NONE"
    assert conclusion.authority == "NONE"


def test_research_replication_warnings_disclose_in_sample_and_finite_sample_limits() -> None:
    warnings = research_replication_warnings()
    joined = " ".join(warnings)
    assert "IN_SAMPLE_REPLICATION" in joined
    assert "15" in joined
    assert "NO STRATEGY, PAPER, OR LIVE AUTHORITY" in joined
