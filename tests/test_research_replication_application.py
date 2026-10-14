"""Application-level proofs for Calibration-Qualified Interaction
Replication v1: the pure statistical helpers and the conclusion-derivation
rule. No panel, database, or network -- every input is a constructed
fixture."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from apex.application.research_replication import (
    CONSERVATIVE_ROUND_TRIP_COST_RATE,
    _cohens_d,
    _confound_plausible,
    _dominant_label,
    _effective_sample,
    _interaction_effect,
    _mean,
    _median,
    _Slice,
    _slice_for,
    _winsorize,
    derive_replication_conclusion,
)
from apex.domain.research_calibration import EvidenceGrade
from apex.domain.research_calibration_v2 import ConditioningShape, ResearchDesign
from apex.domain.research_replication import (
    DatasetExposureLabel,
    DatasetExposureSegment,
    PartitionKind,
    ReplicationConclusionState,
    ReplicationConfoundAssessment,
    ReplicationEffectProfile,
    ReplicationPartitionResult,
    ReplicationSpecification,
    TimingAlignmentResult,
    TimingConvention,
)

NOW = datetime(2026, 8, 8, tzinfo=UTC)

# --------------------------------------------------------------------------
# Pure statistical helpers
# --------------------------------------------------------------------------


def test_mean_and_median_basic() -> None:
    values = (Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"))
    assert _mean(values) == Decimal("2.5")
    assert _median(values) == Decimal("2.5")
    assert _mean(()) is None
    assert _median(()) is None


def test_effective_sample_uses_horizon_stride_adjustment() -> None:
    assert _effective_sample(0, 20) == Decimal(0)
    assert _effective_sample(196, 1) == Decimal(196)  # no overlap adjustment for horizon<=1
    assert _effective_sample(196, 20) == Decimal("9.8")
    assert _effective_sample(1, 20) == Decimal(1)  # floored at 1, never zero for raw>0


def test_cohens_d_matches_known_pooled_formula() -> None:
    sample = (Decimal("1"), Decimal("2"), Decimal("3"))
    benchmark = (Decimal("1"), Decimal("2"), Decimal("3"))
    assert _cohens_d(sample, benchmark) == Decimal("0")
    assert _cohens_d((Decimal("1"),), benchmark) is None  # too few sample values


def test_winsorize_clips_extreme_tails_but_preserves_count() -> None:
    values = tuple(Decimal(v) for v in range(1, 21))  # 1..20
    winsorized = _winsorize(values, tail=Decimal("0.05"))
    assert len(winsorized) == len(values)
    assert min(winsorized) > Decimal("1")
    assert max(winsorized) < Decimal("20")


def test_winsorize_is_a_no_op_for_tiny_samples() -> None:
    values = (Decimal("1"), Decimal("2"), Decimal("3"))
    assert _winsorize(values) == values


def test_slice_for_reproduces_sample_universe_partition() -> None:
    """Exactly RunSingleHypothesis's own closure: sample = period AND
    primary AND secondary; benchmark = period AND primary AND NOT
    secondary."""

    period = frozenset({"d1", "d2", "d3", "d4"})
    primary = frozenset({"d1", "d2", "d3"})
    secondary = frozenset({"d1", "d2"})
    outcome_by_date = {
        "d1": Decimal("0.1"),
        "d2": Decimal("0.2"),
        "d3": Decimal("0.3"),
        "d4": Decimal("0.4"),
    }
    result = _slice_for(
        period_dates=period,
        primary_dates=primary,
        secondary_dates=secondary,
        outcome_by_date=outcome_by_date,
    )
    assert result.sample_dates == ("d1", "d2")
    assert result.benchmark_dates == ("d3",)  # d4 excluded: not in primary


def test_interaction_effect_is_sample_minus_benchmark_mean() -> None:
    slice_ = _Slice(
        sample_dates=("d1", "d2"),
        benchmark_dates=("d3", "d4"),
        sample_values=(Decimal("0.10"), Decimal("0.20")),
        benchmark_values=(Decimal("0.05"), Decimal("0.05")),
    )
    assert _interaction_effect(slice_) == Decimal("0.10")


def test_dominant_label_picks_most_frequent_and_returns_fraction() -> None:
    class _FakeRegime:
        def __init__(self, label: str) -> None:
            self._label = label

        def label_for(self, _dimension: object) -> str:
            return self._label

    regime_by_date = {
        "d1": _FakeRegime("HIGH"),
        "d2": _FakeRegime("HIGH"),
        "d3": _FakeRegime("LOW"),
    }
    label, fraction, count = _dominant_label(["d1", "d2", "d3"], regime_by_date, "ANY_DIMENSION")
    assert label == "HIGH"
    assert count == 2
    assert fraction == Decimal("0.66666667")


def test_confound_plausible_requires_high_concentration_and_weakened_effect() -> None:
    # High concentration + sign flip -> plausible.
    assert (
        _confound_plausible(
            trial_fraction=Decimal("0.9"),
            overall_effect=Decimal("0.02"),
            effect_within=Decimal("-0.01"),
        )
        is True
    )
    # High concentration + material shrinkage -> plausible.
    assert (
        _confound_plausible(
            trial_fraction=Decimal("0.9"),
            overall_effect=Decimal("0.02"),
            effect_within=Decimal("0.005"),
        )
        is True
    )
    # High concentration but effect holds -> not plausible.
    assert (
        _confound_plausible(
            trial_fraction=Decimal("0.9"),
            overall_effect=Decimal("0.02"),
            effect_within=Decimal("0.019"),
        )
        is False
    )
    # Low concentration -> never plausible regardless of effect.
    assert (
        _confound_plausible(
            trial_fraction=Decimal("0.3"),
            overall_effect=Decimal("0.02"),
            effect_within=Decimal("-0.5"),
        )
        is False
    )


# --------------------------------------------------------------------------
# Conclusion derivation -- one fixture-driven scenario per retained state
# --------------------------------------------------------------------------


def _segment(**overrides: object) -> DatasetExposureSegment:
    kwargs: dict[str, object] = {
        "label": DatasetExposureLabel.LOCKED_HOLDOUT_CONSUMED,
        "start_date": "2025-08-11",
        "end_date": "2026-06-30",
        "note": "test",
    }
    kwargs.update(overrides)
    return DatasetExposureSegment(**kwargs)  # type: ignore[arg-type]


def _spec(**overrides: object) -> ReplicationSpecification:
    kwargs: dict[str, object] = {
        "follow_up_id": "cross_market_follow_up_944ab68eaccd07fc752f3fca",
        "parent_hypothesis_id": "cross_market_hypothesis_6e98f1f88b365873c0bdac50",
        "parent_conclusion_id": "research_conclusion_exact",
        "parent_agenda_id": "cross_market_research_agenda_exact",
        "v1_calibration_id": "research_calibration_plan_v1_exact",
        "v1_priority_assessment_id": "research_calibration_follow_up_priority_exact",
        "v2_calibration_id": "research_calibration_plan_v2_exact",
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
        "rationale": "test rationale",
        "original_agenda_objective": "test objective",
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return ReplicationSpecification.create(**kwargs)  # type: ignore[arg-type]


def _full_partition(**overrides: object) -> ReplicationPartitionResult:
    kwargs: dict[str, object] = {
        "replication_id": "repl-1",
        "partition_kind": PartitionKind.FULL_SAMPLE,
        "label": "FULL_SAMPLE",
        "start_date": "2020-07-27",
        "end_date": "2026-06-30",
        "sample_count": 249,
        "effective_sample_count": Decimal("12.45"),
        "benchmark_sample_count": 507,
        "sample_mean": Decimal("0.08"),
        "benchmark_mean": Decimal("0.057"),
        "interaction_effect": Decimal("0.023"),
        "cohens_d": Decimal("0.115"),
        "beats_benchmark": True,
        "matched_null_count": 15,
        "matched_percentile": Decimal("1"),
        "calibration_note": "test",
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return ReplicationPartitionResult.create(**kwargs)  # type: ignore[arg-type]


def _temporal_partition(
    label: str, effect: str, sample_count: int = 100
) -> ReplicationPartitionResult:
    return ReplicationPartitionResult.create(
        replication_id="repl-1",
        partition_kind=PartitionKind.TIME_PERIOD,
        label=label,
        start_date="2020-07-27",
        end_date="2026-06-30",
        sample_count=sample_count,
        effective_sample_count=Decimal(sample_count) / Decimal(20),
        benchmark_sample_count=200,
        sample_mean=Decimal("0.05"),
        benchmark_mean=Decimal("0.03"),
        interaction_effect=Decimal(effect),
        cohens_d=Decimal(effect),
        beats_benchmark=Decimal(effect) > 0,
        matched_null_count=0,
        matched_percentile=None,
        calibration_note="descriptive only",
        created_at=NOW,
    )


def _timing(
    *,
    ordering_valid: bool = True,
    convention: TimingConvention = TimingConvention.PRIMARY_SAME_DAY_V1,
) -> TimingAlignmentResult:
    return TimingAlignmentResult.create(
        replication_id="repl-1",
        convention=convention,
        ordering_valid=ordering_valid,
        checked_date_count=1489,
        violation_count=0 if ordering_valid else 3,
        sample_count=249,
        effective_sample_count=Decimal("12.45"),
        benchmark_sample_count=507,
        interaction_effect=Decimal("0.023"),
        cohens_d=Decimal("0.115"),
        note="test",
        created_at=NOW,
    )


def _effect_profile(**overrides: object) -> ReplicationEffectProfile:
    kwargs: dict[str, object] = {
        "replication_id": "repl-1",
        "unconditional_mean": Decimal("0.04"),
        "unconditional_sample_count": 1469,
        "low_vol_mean": Decimal("0.057"),
        "low_vol_sample_count": 507,
        "low_vol_median": Decimal("0.005"),
        "low_vol_hit_rate": Decimal("0.55"),
        "low_vol_stdev": Decimal("0.2"),
        "high_vol_mean": Decimal("0.08"),
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
        "winsorized_interaction_effect": Decimal("0.03"),
        "robust_median_interaction_effect": Decimal("0.06"),
        "leave_one_third_out_effects": (("EXCLUDE_EARLY", Decimal("0.02")),),
        "holdout_window_excluded_effect": Decimal("0.02"),
        "most_influential_dates": ("2020-11-27",),
        "outlier_robust": True,
        "outlier_dependence_note": "consistent",
        "created_at": NOW,
    }
    kwargs.update(overrides)
    return ReplicationEffectProfile.create(**kwargs)  # type: ignore[arg-type]


def _confound(*, resolved: bool = True) -> ReplicationConfoundAssessment:
    from apex.domain.research_replication import ConfoundDimensionResult

    dim = ConfoundDimensionResult(
        dimension="BTC_VOLATILITY",
        dominant_trial_label="HIGH_VOLATILITY",
        trial_concentration=Decimal("0.6") if not resolved else Decimal("0.3"),
        dominant_benchmark_label="HIGH_VOLATILITY",
        benchmark_concentration=Decimal("0.3"),
        same_dominant_label=True,
        effect_within_dominant_label=Decimal("-0.01") if not resolved else Decimal("0.02"),
        dominant_label_sample_count=100,
        confound_plausible=not resolved,
        note="test",
    )
    return ReplicationConfoundAssessment.create(
        replication_id="repl-1", dimensions=(dim,), note="test", created_at=NOW
    )


def test_timing_invalid_short_circuits_before_any_other_check() -> None:
    conclusion = derive_replication_conclusion(
        specification=_spec(),
        full_partition=_full_partition(),
        temporal_partitions=(),
        volatility_partitions=(),
        timing=(_timing(ordering_valid=False),),
        effect_profile=_effect_profile(),
        confound=_confound(resolved=True),
        created_at=NOW,
    )
    assert conclusion.conclusion_state is ReplicationConclusionState.TIMING_INVALID
    assert conclusion.evidence_grade is EvidenceGrade.UNRELIABLE
    assert conclusion.forward_confirmation_required is True


def test_insufficient_evidence_when_effective_sample_below_minimum() -> None:
    conclusion = derive_replication_conclusion(
        specification=_spec(minimum_effective_sample=Decimal("50")),
        full_partition=_full_partition(effective_sample_count=Decimal("12.45")),
        temporal_partitions=(),
        volatility_partitions=(),
        timing=(_timing(),),
        effect_profile=_effect_profile(),
        confound=_confound(resolved=True),
        created_at=NOW,
    )
    assert conclusion.conclusion_state is ReplicationConclusionState.INSUFFICIENT_EVIDENCE


def test_null_compatible_when_matched_percentile_below_ceiling() -> None:
    conclusion = derive_replication_conclusion(
        specification=_spec(v2_matched_percentile=Decimal("0.5")),
        full_partition=_full_partition(),
        temporal_partitions=(),
        volatility_partitions=(),
        timing=(_timing(),),
        effect_profile=_effect_profile(),
        confound=_confound(resolved=True),
        created_at=NOW,
    )
    assert conclusion.conclusion_state is ReplicationConclusionState.NULL_COMPATIBLE
    assert conclusion.forward_confirmation_required is False


def test_confounding_not_resolved_when_a_regime_dimension_explains_the_effect() -> None:
    conclusion = derive_replication_conclusion(
        specification=_spec(),
        full_partition=_full_partition(),
        temporal_partitions=(
            _temporal_partition("EARLY", "0.01"),
            _temporal_partition("MIDDLE", "0.02"),
            _temporal_partition("LATE", "0.03"),
        ),
        volatility_partitions=(),
        timing=(_timing(),),
        effect_profile=_effect_profile(),
        confound=_confound(resolved=False),
        created_at=NOW,
    )
    assert conclusion.conclusion_state is ReplicationConclusionState.CONFOUNDING_NOT_RESOLVED
    assert conclusion.qualification.value == "INSUFFICIENT"


def test_unstable_when_temporal_sign_flips_across_thirds() -> None:
    conclusion = derive_replication_conclusion(
        specification=_spec(),
        full_partition=_full_partition(interaction_effect=Decimal("0.02")),
        temporal_partitions=(
            _temporal_partition("EARLY", "0.01"),
            _temporal_partition("MIDDLE", "-0.02"),  # sign flip
            _temporal_partition("LATE", "0.03"),
        ),
        volatility_partitions=(),
        timing=(_timing(),),
        effect_profile=_effect_profile(),
        confound=_confound(resolved=True),
        created_at=NOW,
    )
    assert conclusion.conclusion_state is ReplicationConclusionState.UNSTABLE


def test_unstable_when_outlier_robustness_fails() -> None:
    conclusion = derive_replication_conclusion(
        specification=_spec(),
        full_partition=_full_partition(interaction_effect=Decimal("0.02")),
        temporal_partitions=(
            _temporal_partition("EARLY", "0.01"),
            _temporal_partition("MIDDLE", "0.015"),
            _temporal_partition("LATE", "0.03"),
        ),
        volatility_partitions=(),
        timing=(_timing(),),
        effect_profile=_effect_profile(outlier_robust=False, outlier_dependence_note="NOT robust"),
        confound=_confound(resolved=True),
        created_at=NOW,
    )
    assert conclusion.conclusion_state is ReplicationConclusionState.UNSTABLE


def test_replicated_when_temporally_stable_outlier_robust_and_meets_floor() -> None:
    conclusion = derive_replication_conclusion(
        specification=_spec(),
        full_partition=_full_partition(
            interaction_effect=Decimal("0.30"), cohens_d=Decimal("0.30")
        ),
        temporal_partitions=(
            _temporal_partition("EARLY", "0.20"),
            _temporal_partition("MIDDLE", "0.25"),
            _temporal_partition("LATE", "0.35"),
        ),
        volatility_partitions=(),
        timing=(_timing(),),
        effect_profile=_effect_profile(cohens_d=Decimal("0.30"), outlier_robust=True),
        confound=_confound(resolved=True),
        created_at=NOW,
    )
    assert conclusion.conclusion_state is ReplicationConclusionState.REPLICATED
    # Section 19: forward confirmation is always required for an
    # IN_SAMPLE_REPLICATION result, even a REPLICATED one.
    assert conclusion.forward_confirmation_required is True
    assert conclusion.qualification.value == "FORWARD_CONFIRMATION_REQUIRED"
    # Section 18: evidence grade is never silently raised past SUGGESTIVE.
    assert conclusion.evidence_grade is EvidenceGrade.SUGGESTIVE


def test_partially_replicated_when_stable_and_robust_but_below_practical_floor() -> None:
    conclusion = derive_replication_conclusion(
        specification=_spec(),
        full_partition=_full_partition(
            interaction_effect=Decimal("0.02"), cohens_d=Decimal("0.11")
        ),
        temporal_partitions=(
            _temporal_partition("EARLY", "0.01"),
            _temporal_partition("MIDDLE", "0.015"),
            _temporal_partition("LATE", "0.02"),
        ),
        volatility_partitions=(),
        timing=(_timing(),),
        effect_profile=_effect_profile(cohens_d=Decimal("0.11"), outlier_robust=True),
        confound=_confound(resolved=True),
        created_at=NOW,
    )
    assert conclusion.conclusion_state is ReplicationConclusionState.PARTIALLY_REPLICATED


def test_transaction_cost_relevance_uses_conservative_crypto_cost_constant() -> None:
    assert Decimal("0.014") == CONSERVATIVE_ROUND_TRIP_COST_RATE
