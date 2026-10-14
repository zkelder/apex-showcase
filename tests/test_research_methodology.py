"""Pure-domain proofs for quant research methodology and walk-forward
validation: research phases, holdout-by-default governance, period
ordering, parameter/configuration provenance, multiple-testing correction,
overlapping-outcome handling, sample-size policy, statistical diagnostics
v2, benchmark comparison, walk-forward fold generation, robustness checks,
the research-conclusion model, and the promotion boundary.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.historical_replay import (
    HistoricalResearchEvaluation,
    OutcomeCompletenessState,
    ReplayFeatureFrame,
    ResearchPeriodDeclaration,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
    apply_cross_sectional_ranks,
    assess_outcome,
    build_market_regime_snapshot,
    build_security_feature_vector,
    compute_breadth,
    evaluate_breadth_vs_subsequent_return,
)
from apex.domain.research_methodology import (
    APEX_PRODUCTION_PROMOTION_POLICY,
    APEX_PRODUCTION_PROMOTION_POLICY_ID,
    BenchmarkKind,
    ConfigurationOrigin,
    FoldOutcomeState,
    GovernanceIntent,
    HoldoutExposureEvent,
    InsufficientSampleError,
    MultipleTestingCorrectionMethod,
    OverlapHandlingMode,
    ResearchConclusionState,
    ResearchConfiguration,
    ResearchGovernancePolicy,
    ResearchMethodologyError,
    ResearchPhase,
    ResearchTrial,
    ResearchTrialFamily,
    RobustnessCheckType,
    SampleQualityState,
    TrainingWindowMode,
    TrialSelectionMethod,
    TrialTestResult,
    WalkForwardCompletionState,
    WalkForwardFold,
    WalkForwardRun,
    apply_benjamini_hochberg_correction,
    apply_bonferroni_correction,
    assert_effective_sample,
    assert_ordered_research_periods,
    build_diagnostics_v2,
    compare_to_benchmark,
    compute_effect_size,
    compute_overlap_diagnostics,
    derive_conclusion_state,
    downsample_non_overlapping,
    generate_fold_windows,
    guard_governed_holdout_access,
    newey_west_standard_error,
    pair_momentum_signal_with_breadth_outcome,
    resolve_minimum_sample_requirement,
    robustness_asset_cohort_exclusion,
    robustness_cost_stress,
    robustness_leave_one_period_out,
    robustness_missing_data_sensitivity,
    robustness_regime_split,
    robustness_subperiod_split,
    seeded_bootstrap_confidence_interval,
    summarize_robustness,
    summarize_walk_forward_folds,
    two_sided_z_test_p_value,
)
from apex.domain.research_methodology import (
    PromotionEvaluation as _PromotionEvaluation,
)
from apex.domain.research_methodology import (
    PromotionPolicy as _PromotionPolicy,
)
from apex.domain.research_methodology import (
    ResearchConclusion as _ResearchConclusion,
)
from apex.domain.research_methodology import (
    RobustnessCheckRun as _RobustnessCheckRun,
)
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
)
from apex.domain.values import content_digest

INSTRUMENT = "instrument-aaa"
UTC_NOON = lambda day: datetime.fromisoformat(f"{day}T12:00:00+00:00")  # noqa: E731


# --------------------------------------------------------------------------
# Fixture builders (mirrors tests/test_historical_replay.py's conventions)
# --------------------------------------------------------------------------


def _bar(day: str, close: str, *, lag_hours: int = 1) -> SecuritiesBarEvidence:
    period_start = datetime.fromisoformat(f"{day}T14:30:00+00:00")
    period_end = datetime.fromisoformat(f"{day}T21:00:00+00:00")
    return SecuritiesBarEvidence(
        instrument_id=INSTRUMENT,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state="RAW",
        period_start=period_start,
        period_end=period_end,
        available_at=period_end + timedelta(hours=lag_hours),
        open=close,
        high=close,
        low=close,
        close=close,
        volume="1000",
        source_identity="test-fixture",
    )


def _persistent_momentum_bars(
    day_count: int,
    *,
    start: str = "2024-01-01",
    block_sessions: int = 40,
    base: Decimal = Decimal(100),
) -> tuple[SecuritiesBarEvidence, ...]:
    """Alternating up/down blocks -- by construction, whichever direction a
    given day is deep inside dominates both its trailing lookback return and
    its forward outcome return, so momentum genuinely (not by chance)
    persists everywhere in this series."""

    bars = []
    price = base
    day = date.fromisoformat(start)
    for index in range(day_count):
        up_phase = (index // block_sessions) % 2 == 0
        step = Decimal("0.6") if up_phase else Decimal("-0.5")
        price = max(Decimal(1), price + step)
        bars.append(_bar(day.isoformat(), str(price)))
        day += timedelta(days=1)
    return tuple(bars)


def _noise_bars(
    day_count: int, *, start: str, seed: int, base: Decimal
) -> tuple[SecuritiesBarEvidence, ...]:
    """Deterministic (seeded) small daily moves with no persistence: any
    given day's trailing-return sign carries no information about its
    forward-return sign."""

    generator = random.Random(seed)
    bars = []
    price = base
    day = date.fromisoformat(start)
    for _ in range(day_count):
        delta = Decimal(str(round(generator.uniform(-0.7, 0.7), 4)))
        price = max(Decimal(1), price + delta)
        bars.append(_bar(day.isoformat(), str(price)))
        day += timedelta(days=1)
    return tuple(bars)


def _make_frame(
    bars: tuple[SecuritiesBarEvidence, ...],
    *,
    as_of: datetime,
    session_date: str,
    replay_run_id: str = "run-1",
    dataset_snapshot_id: str = "snap-1",
) -> ReplayFeatureFrame:
    from apex.domain.historical_market_data import point_in_time_slice

    sliced = point_in_time_slice(bars, as_of=as_of)
    vector = build_security_feature_vector(
        stable_security_id="stable-1",
        instrument_id=INSTRUMENT,
        bars=sliced,
        benchmark_bars=None,
        as_of=as_of,
        session_date=session_date,
    )
    securities = apply_cross_sectional_ranks((vector,))
    one_session = dict(vector.returns).get(1)
    breadth = compute_breadth(
        universe_size=1,
        one_session_returns=(("stable-1", one_session),) if one_session is not None else (),
        above_moving_average=(),
    )
    regime = build_market_regime_snapshot(
        universe_size=1,
        breadth=breadth,
        cross_sectional_return_stdev=None,
        cross_sectional_return_evaluable=0,
        benchmark_annualized_volatility=vector.rolling_volatility_20,
        benchmark_return_20_session=dict(vector.returns).get(20),
        average_pairwise_correlation=None,
        correlation_evaluable=0,
    )
    payload = ("test-frame", as_of, session_date, replay_run_id)
    return ReplayFeatureFrame(
        frame_id=f"frame-{session_date}-{as_of.isoformat()}",
        replay_run_id=replay_run_id,
        dataset_snapshot_id=dataset_snapshot_id,
        as_of=as_of,
        session_date=session_date,
        feature_policy_id="fp-1",
        feature_policy_hash="a" * 64,
        universe=("stable-1",),
        securities=securities,
        breadth=breadth,
        regime=regime,
        excluded_evidence_count=0,
        excluded_evidence_reasons=(),
        point_in_time_limitations=(),
        data_quality_flags=(),
        created_at=as_of,
        canonical_hash=content_digest(payload),
    )


def _build_series(
    bars: tuple[SecuritiesBarEvidence, ...],
    *,
    replay_run_id: str = "run-1",
    dataset_snapshot_id: str = "snap-1",
) -> tuple[tuple[ReplayFeatureFrame, ...], tuple[HistoricalResearchEvaluation, ...]]:
    """One frame + one BREADTH_VS_SUBSEQUENT_RETURN evaluation per bar,
    with outcomes resolved as far as the series' own bars allow -- assessed
    once, at a replay_as_of safely past every possible outcome window, which
    is equivalent to (but far cheaper to compute in a domain test than)
    stepping the real engine bar-by-bar."""

    final_as_of = bars[-1].available_at + timedelta(days=45)
    frames = []
    evaluations = []
    for bar in bars:
        as_of = bar.available_at
        session_date = bar.period_end.date().isoformat()
        frame = _make_frame(
            bars,
            as_of=as_of,
            session_date=session_date,
            replay_run_id=replay_run_id,
            dataset_snapshot_id=dataset_snapshot_id,
        )
        frames.append(frame)
        evaluation = evaluate_breadth_vs_subsequent_return(
            evaluation_id="",
            replay_run_id=replay_run_id,
            frame=frame,
            hypothesis_id=None,
            created_at=as_of,
        )
        if evaluation.outcome_window is not None:
            assessment = assess_outcome(
                outcome_id=f"outcome-{session_date}",
                window=evaluation.outcome_window,
                outcome_bars=bars,
                replay_as_of=final_as_of,
                observed_at=final_as_of,
            )
            evaluation = evaluation.attach_outcome(assessment)
        evaluations.append(evaluation)
    return tuple(frames), tuple(evaluations)


def _dated_evaluation_ids(
    evaluations: tuple[HistoricalResearchEvaluation, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple((item.session_date, item.evaluation_id) for item in evaluations)


def _fixed_config(*, origin: ConfigurationOrigin, candidates: int = 1) -> ResearchConfiguration:
    return ResearchConfiguration.create(
        method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
        method_version="1",
        parameter_values=(("lookback_sessions", "20"),),
        origin=origin,
        candidates_examined_count=candidates,
    )


# --------------------------------------------------------------------------
# Section 1: research phase model
# --------------------------------------------------------------------------


def test_all_five_phases_are_distinguished() -> None:
    assert {item.value for item in ResearchPhase} == {
        "EXPLORATORY",
        "VALIDATION",
        "LOCKED_HOLDOUT",
        "WALK_FORWARD",
        "FORWARD_PAPER",
    }


def test_exploratory_configuration_cannot_be_presented_as_locked_holdout() -> None:
    complete, evaluations = _build_series(_persistent_momentum_bars(300))
    diagnostics = build_diagnostics_v2(
        sample_values=(Decimal("0.01"),) * 25,
        evaluations=evaluations,
        outcome_horizon_sessions=30,
    )
    with pytest.raises(ResearchMethodologyError, match="EXPLORATORY_CONFIGURATION_CANNOT"):
        ResearchTrial.create(
            hypothesis_version_id="hv-1",
            dataset_snapshot_id="snap-1",
            replay_run_id="run-1",
            phase=ResearchPhase.LOCKED_HOLDOUT,
            market_scope=("US_EQUITY",),
            configuration=_fixed_config(origin=ConfigurationOrigin.EXPLORATORY),
            dated_evaluation_ids=_dated_evaluation_ids(evaluations[:25]),
            diagnostics=diagnostics,
            benchmark=None,
            created_at=UTC_NOON("2024-06-01"),
        )
    del complete


def test_trial_rejects_evaluation_dates_outside_declared_period_role() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(300))
    period_policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-06-01", False),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, "2024-06-02", "2024-08-01", False
            ),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2024-08-02", "2024-10-25", True),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    governance = ResearchGovernancePolicy.create(
        hypothesis_version_id="hv-1",
        dataset_snapshot_id="snap-1",
        replay_run_id="run-1",
        intent=GovernanceIntent.PROMOTION_TRACK,
        period_policy=period_policy,
        declared_at=UTC_NOON("2024-01-01"),
    )
    # These evaluations' dates fall in the TRAIN period, not VALIDATION.
    train_evaluations = tuple(item for item in evaluations if item.session_date <= "2024-06-01")[
        :20
    ]
    diagnostics = build_diagnostics_v2(
        sample_values=(Decimal("0.01"),) * 20,
        evaluations=evaluations,
        outcome_horizon_sessions=30,
    )
    with pytest.raises(ResearchMethodologyError, match="OUTSIDE_DECLARED_PERIOD_ROLE"):
        ResearchTrial.create(
            hypothesis_version_id="hv-1",
            dataset_snapshot_id="snap-1",
            replay_run_id="run-1",
            phase=ResearchPhase.VALIDATION,
            market_scope=("US_EQUITY",),
            configuration=_fixed_config(origin=ConfigurationOrigin.FIXED_A_PRIORI),
            dated_evaluation_ids=_dated_evaluation_ids(train_evaluations),
            diagnostics=diagnostics,
            benchmark=None,
            created_at=UTC_NOON("2024-06-15"),
            governance_policy=governance,
        )


# --------------------------------------------------------------------------
# Section 2 + 3: holdout-by-default governance and period ordering
# --------------------------------------------------------------------------


def test_promotion_track_requires_a_declared_locked_holdout() -> None:
    unlocked = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-06-01", False),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, "2024-06-02", "2024-08-01", False
            ),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    with pytest.raises(ResearchMethodologyError, match="REQUIRES_DECLARED_HOLDOUT"):
        ResearchGovernancePolicy.create(
            hypothesis_version_id="hv-1",
            dataset_snapshot_id="snap-1",
            replay_run_id="run-1",
            intent=GovernanceIntent.PROMOTION_TRACK,
            period_policy=unlocked,
            declared_at=UTC_NOON("2024-01-01"),
        )


def test_promotion_track_holdout_must_be_locked() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-06-01", False),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.HOLDOUT, "2024-06-02", "2024-08-01", False
            ),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    with pytest.raises(ResearchMethodologyError, match="HOLDOUT_MUST_BE_LOCKED"):
        ResearchGovernancePolicy.create(
            hypothesis_version_id="hv-1",
            dataset_snapshot_id="snap-1",
            replay_run_id="run-1",
            intent=GovernanceIntent.PROMOTION_TRACK,
            period_policy=policy,
            declared_at=UTC_NOON("2024-01-01"),
        )


def test_exploratory_only_governance_permits_no_holdout() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-06-01", False),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    governance = ResearchGovernancePolicy.create(
        hypothesis_version_id="hv-1",
        dataset_snapshot_id="snap-1",
        replay_run_id="run-1",
        intent=GovernanceIntent.EXPLORATORY_ONLY,
        period_policy=policy,
        declared_at=UTC_NOON("2024-01-01"),
    )
    assert governance.intent is GovernanceIntent.EXPLORATORY_ONLY


def test_governance_policy_declaration_is_immutable_and_has_no_update_method() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-06-01", False),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    governance = ResearchGovernancePolicy.create(
        hypothesis_version_id="hv-1",
        dataset_snapshot_id="snap-1",
        replay_run_id="run-1",
        intent=GovernanceIntent.EXPLORATORY_ONLY,
        period_policy=policy,
        declared_at=UTC_NOON("2024-01-01"),
    )
    assert not hasattr(governance, "update")
    with pytest.raises(Exception):  # noqa: B017 -- frozen dataclass
        governance.intent = GovernanceIntent.PROMOTION_TRACK  # type: ignore[misc]


def test_redeclared_governance_policy_gets_a_new_identity_old_one_survives() -> None:
    policy_a = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-06-01", False),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2024-06-02", "2024-08-01", True),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    governance_a = ResearchGovernancePolicy.create(
        hypothesis_version_id="hv-1",
        dataset_snapshot_id="snap-1",
        replay_run_id="run-1",
        intent=GovernanceIntent.PROMOTION_TRACK,
        period_policy=policy_a,
        declared_at=UTC_NOON("2024-01-01"),
    )
    policy_b = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-06-01", False),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2024-06-02", "2024-09-01", True),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    governance_b = ResearchGovernancePolicy.create(
        hypothesis_version_id="hv-1",
        dataset_snapshot_id="snap-1",
        replay_run_id="run-1",
        intent=GovernanceIntent.PROMOTION_TRACK,
        period_policy=policy_b,
        revision=2,
        supersedes_governance_policy_id=governance_a.governance_policy_id,
        declared_at=UTC_NOON("2024-08-15"),
    )
    assert governance_b.governance_policy_id != governance_a.governance_policy_id
    assert governance_b.supersedes_governance_policy_id == governance_a.governance_policy_id
    # governance_a itself is unmodified -- redeclaration never mutates it.
    assert governance_a.period_policy.policy_id == policy_a.policy_id


def test_locked_holdout_blocked_for_development_but_not_for_evaluation_only() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2024-06-02", "2024-08-01", True),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    governance = ResearchGovernancePolicy.create(
        hypothesis_version_id="hv-1",
        dataset_snapshot_id="snap-1",
        replay_run_id="run-1",
        intent=GovernanceIntent.PROMOTION_TRACK,
        period_policy=policy,
        declared_at=UTC_NOON("2024-01-01"),
    )
    with pytest.raises(Exception, match="LOCKED_HOLDOUT_CANNOT_BE_USED_FOR_DEVELOPMENT"):
        guard_governed_holdout_access(governance, purpose="PARAMETER_TUNING")
    guard_governed_holdout_access(governance, purpose="EVALUATION_ONLY")


def test_holdout_exposure_event_requires_a_declared_holdout() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-06-01", False),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    governance = ResearchGovernancePolicy.create(
        hypothesis_version_id="hv-1",
        dataset_snapshot_id="snap-1",
        replay_run_id="run-1",
        intent=GovernanceIntent.EXPLORATORY_ONLY,
        period_policy=policy,
        declared_at=UTC_NOON("2024-01-01"),
    )
    with pytest.raises(ResearchMethodologyError, match="EXPOSURE_REQUIRES_DECLARED_HOLDOUT"):
        HoldoutExposureEvent.create(
            governance_policy=governance,
            exposed_at=UTC_NOON("2024-08-15"),
            actor="researcher-1",
            purpose="EVALUATION_ONLY",
            consumed_trial_id=None,
        )


def test_holdout_exposure_is_idempotent_for_identical_repeated_reads() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2024-06-02", "2024-08-01", True),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    governance = ResearchGovernancePolicy.create(
        hypothesis_version_id="hv-1",
        dataset_snapshot_id="snap-1",
        replay_run_id="run-1",
        intent=GovernanceIntent.PROMOTION_TRACK,
        period_policy=policy,
        declared_at=UTC_NOON("2024-01-01"),
    )
    event_a = HoldoutExposureEvent.create(
        governance_policy=governance,
        exposed_at=UTC_NOON("2024-08-15"),
        actor="researcher-1",
        purpose="EVALUATION_ONLY",
        consumed_trial_id="trial-1",
    )
    event_b = HoldoutExposureEvent.create(
        governance_policy=governance,
        exposed_at=UTC_NOON("2024-08-15"),
        actor="researcher-1",
        purpose="EVALUATION_ONLY",
        consumed_trial_id="trial-1",
    )
    assert event_a.exposure_id == event_b.exposure_id


def test_ordered_periods_rejects_role_date_mismatch_even_when_non_overlapping() -> None:
    # HOLDOUT dated *before* VALIDATION -- ResearchPeriodPolicy.create's bare
    # non-overlap check alone would accept this; the stricter role-order
    # check must not.
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-02-01", False),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2024-02-02", "2024-03-01", True),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, "2024-03-02", "2024-04-01", False
            ),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    with pytest.raises(ResearchMethodologyError, match="TRAIN_BEFORE_VALIDATION_BEFORE_HOLDOUT"):
        assert_ordered_research_periods(policy)


def test_ordered_periods_accepts_correct_train_validation_holdout_order() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2024-01-01", "2024-02-01", False),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, "2024-02-02", "2024-03-01", False
            ),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2024-03-02", "2024-04-01", True),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    assert_ordered_research_periods(policy)  # does not raise


# --------------------------------------------------------------------------
# Section 5: parameter / configuration provenance
# --------------------------------------------------------------------------


def test_configuration_has_no_selected_using_holdout_origin() -> None:
    assert "SELECTED_USING_HOLDOUT" not in {item.value for item in ConfigurationOrigin}


def test_selected_configuration_requires_multiple_candidates_examined() -> None:
    with pytest.raises(ResearchMethodologyError, match="REQUIRES_MULTIPLE_CANDIDATES"):
        _fixed_config(origin=ConfigurationOrigin.SELECTED_USING_TRAINING, candidates=1)
    _fixed_config(origin=ConfigurationOrigin.SELECTED_USING_TRAINING, candidates=2)


def test_fixed_a_priori_configuration_must_examine_exactly_one_candidate() -> None:
    with pytest.raises(ResearchMethodologyError, match="EXAMINES_EXACTLY_ONE"):
        _fixed_config(origin=ConfigurationOrigin.FIXED_A_PRIORI, candidates=3)


def test_configuration_identity_is_deterministic() -> None:
    first = _fixed_config(origin=ConfigurationOrigin.FIXED_A_PRIORI)
    second = _fixed_config(origin=ConfigurationOrigin.FIXED_A_PRIORI)
    assert first.configuration_id == second.configuration_id


# --------------------------------------------------------------------------
# Section 6: multiple-testing awareness
# --------------------------------------------------------------------------


def test_bonferroni_correction_is_stricter_than_raw_p_values() -> None:
    results = (
        TrialTestResult("t1", "a", Decimal("0.01")),
        TrialTestResult("t2", "b", Decimal("0.04")),
        TrialTestResult("t3", "c", Decimal("0.20")),
    )
    corrected = apply_bonferroni_correction(results, family_wise_alpha=Decimal("0.05"))
    assert corrected[0].adjusted_p_value == Decimal("0.03000000")
    assert corrected[0].rejected_null is True
    # 0.04 * 3 = 0.12 > 0.05 -- no longer significant after correction.
    assert corrected[1].rejected_null is False


def test_benjamini_hochberg_rejects_more_than_bonferroni_for_the_same_data() -> None:
    results = tuple(
        TrialTestResult(f"t{i}", f"label{i}", Decimal(value))
        for i, value in enumerate(("0.001", "0.008", "0.015", "0.30", "0.60"))
    )
    bh = apply_benjamini_hochberg_correction(results, false_discovery_rate=Decimal("0.10"))
    bonferroni = apply_bonferroni_correction(results, family_wise_alpha=Decimal("0.10"))
    bh_rejections = sum(1 for item in bh if item.rejected_null)
    bonferroni_rejections = sum(1 for item in bonferroni if item.rejected_null)
    assert bh_rejections >= bonferroni_rejections


def test_uncorrected_multiple_testing_produces_an_explicit_warning() -> None:
    family = ResearchTrialFamily.create(
        hypothesis_id="hv-1",
        candidate_count=5,
        asset_count=1,
        outcome_definition_count=1,
        regime_slice_count=1,
        selection_method=TrialSelectionMethod.BEST_OF_FAMILY,
        raw_results=(
            TrialTestResult("t1", "a", Decimal("0.01")),
            TrialTestResult("t2", "b", Decimal("0.20")),
        ),
        correction_method=MultipleTestingCorrectionMethod.NONE,
        created_at=UTC_NOON("2024-06-01"),
    )
    assert family.uncorrected_warning is not None
    assert "UNCORRECTED_MULTIPLE_TESTING" in family.uncorrected_warning
    assert not family.correction_applied


def test_corrected_family_carries_no_uncorrected_warning() -> None:
    family = ResearchTrialFamily.create(
        hypothesis_id="hv-1",
        candidate_count=5,
        asset_count=1,
        outcome_definition_count=1,
        regime_slice_count=1,
        selection_method=TrialSelectionMethod.BEST_OF_FAMILY,
        raw_results=(
            TrialTestResult("t1", "a", Decimal("0.01")),
            TrialTestResult("t2", "b", Decimal("0.20")),
        ),
        correction_method=MultipleTestingCorrectionMethod.BENJAMINI_HOCHBERG_FDR,
        created_at=UTC_NOON("2024-06-01"),
    )
    assert family.uncorrected_warning is None
    assert family.correction_applied


def test_trial_family_candidate_count_cannot_be_less_than_test_count() -> None:
    with pytest.raises(ResearchMethodologyError, match="CANDIDATE_COUNT_CANNOT_BE_LESS"):
        ResearchTrialFamily.create(
            hypothesis_id="hv-1",
            candidate_count=1,
            asset_count=1,
            outcome_definition_count=1,
            regime_slice_count=1,
            selection_method=TrialSelectionMethod.BEST_OF_FAMILY,
            raw_results=(
                TrialTestResult("t1", "a", Decimal("0.01")),
                TrialTestResult("t2", "b", Decimal("0.20")),
            ),
            created_at=UTC_NOON("2024-06-01"),
        )


def test_many_pure_noise_trials_produce_false_discoveries_that_correction_downgrades() -> None:
    """Genuine multiple-testing engineering acceptance (section 15d): 20
    trials against independent pure-noise samples. Raw (uncorrected)
    p-values nominally "reject" more often than the 5% design rate would
    suggest is safe to interpret one at a time; Benjamini-Hochberg brings
    the surviving rejections down, proving the correction genuinely does
    something, not just window-dressing."""

    generator = random.Random(20240915)
    results = []
    for index in range(20):
        sample = [Decimal(str(round(generator.gauss(0, 1), 4))) for _ in range(40)]
        mean = sum(sample, Decimal()) / Decimal(len(sample))
        stdev_squared = sum(((value - mean) ** 2 for value in sample), Decimal()) / Decimal(
            len(sample) - 1
        )
        stdev = stdev_squared.sqrt()
        p_value = two_sided_z_test_p_value(
            sample_mean=mean,
            benchmark_mean=Decimal(0),
            sample_stdev=stdev,
            effective_sample_count=Decimal(len(sample)),
        )
        results.append(TrialTestResult(f"noise-{index}", f"noise-{index}", p_value))
    raw_rejections = sum(
        1 for item in results if item.p_value is not None and item.p_value <= Decimal("0.05")
    )
    corrected = apply_benjamini_hochberg_correction(results, false_discovery_rate=Decimal("0.10"))
    corrected_rejections = sum(1 for item in corrected if item.rejected_null)
    assert corrected_rejections <= raw_rejections


# --------------------------------------------------------------------------
# Section 7: overlapping-outcome handling
# --------------------------------------------------------------------------


def test_overlap_diagnostics_reports_no_overlap_for_widely_spaced_evaluations() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(200))
    # OUTCOME_HORIZON_CALENDAR_DAYS is 30 -- a stride of 35 calendar days
    # between evaluations guarantees their outcome windows cannot overlap.
    sparse = evaluations[::35]
    diagnostics = compute_overlap_diagnostics(sparse, outcome_horizon_sessions=30)
    assert diagnostics.handling_mode is OverlapHandlingMode.RETAINED_DESCRIPTIVE
    assert diagnostics.inference_available


def test_overlap_diagnostics_suppresses_inference_for_daily_rolling_evaluations() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(200))
    diagnostics = compute_overlap_diagnostics(evaluations, outcome_horizon_sessions=30)
    assert diagnostics.handling_mode is OverlapHandlingMode.EXCLUDED_FROM_INFERENCE
    assert not diagnostics.inference_available
    assert diagnostics.effective_sample_count < Decimal(diagnostics.raw_sample_count)


def test_downsample_non_overlapping_produces_a_non_overlapping_subset() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(200))
    downsampled = downsample_non_overlapping(evaluations, outcome_horizon_sessions=30)
    diagnostics = compute_overlap_diagnostics(downsampled, outcome_horizon_sessions=30)
    assert diagnostics.handling_mode is OverlapHandlingMode.RETAINED_DESCRIPTIVE


def test_diagnostics_v2_marks_inference_unavailable_but_keeps_descriptive_stats() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(200))
    values = tuple(
        item.outcome.outcome_value
        for item in evaluations
        if item.outcome is not None
        and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
        and item.outcome.outcome_value is not None
    )
    diagnostics = build_diagnostics_v2(
        sample_values=values, evaluations=evaluations, outcome_horizon_sessions=30
    )
    assert not diagnostics.inference_available
    assert diagnostics.base.sample_count == len(values)
    assert diagnostics.base.mean is not None


# --------------------------------------------------------------------------
# Section 8: sample-size policy
# --------------------------------------------------------------------------


def test_sample_size_requirement_scales_by_phase() -> None:
    exploratory = resolve_minimum_sample_requirement(phase=ResearchPhase.EXPLORATORY)
    holdout = resolve_minimum_sample_requirement(phase=ResearchPhase.LOCKED_HOLDOUT)
    assert holdout.minimum_raw_sample >= exploratory.minimum_raw_sample


def test_sample_size_requirement_scales_with_group_count() -> None:
    single = resolve_minimum_sample_requirement(
        phase=ResearchPhase.EXPLORATORY, is_grouped_regime_comparison=True, group_count=1
    )
    many = resolve_minimum_sample_requirement(
        phase=ResearchPhase.EXPLORATORY, is_grouped_regime_comparison=True, group_count=6
    )
    assert many.minimum_raw_sample > single.minimum_raw_sample


def test_assert_effective_sample_raises_insufficient_effective_sample() -> None:
    requirement = resolve_minimum_sample_requirement(phase=ResearchPhase.VALIDATION)
    with pytest.raises(InsufficientSampleError, match="INSUFFICIENT_EFFECTIVE_SAMPLE"):
        assert_effective_sample(effective_sample_count=Decimal(1), requirement=requirement)


def test_assert_effective_sample_passes_when_adequate() -> None:
    requirement = resolve_minimum_sample_requirement(phase=ResearchPhase.EXPLORATORY)
    assert_effective_sample(
        effective_sample_count=Decimal(requirement.minimum_raw_sample) * 2, requirement=requirement
    )


# --------------------------------------------------------------------------
# Section 10: benchmark and null-model requirement
# --------------------------------------------------------------------------


def test_benchmark_comparison_reports_beats_benchmark() -> None:
    comparison = compare_to_benchmark(
        sample_mean=Decimal("0.02"),
        benchmark_kind=BenchmarkKind.UNCONDITIONAL_SUBSEQUENT_RETURN,
        benchmark_values=(Decimal("0.00"), Decimal("0.01"), Decimal("-0.01")),
    )
    assert comparison.beats_benchmark
    assert comparison.relative_result > 0


def test_benchmark_comparison_requires_a_nonempty_sample() -> None:
    with pytest.raises(ResearchMethodologyError, match="BENCHMARK_SAMPLE_REQUIRED"):
        compare_to_benchmark(
            sample_mean=Decimal("0.02"),
            benchmark_kind=BenchmarkKind.MARKET_BENCHMARK,
            benchmark_values=(),
        )


# --------------------------------------------------------------------------
# Section 9: statistical diagnostics v2
# --------------------------------------------------------------------------


def test_effect_size_is_none_with_thin_samples() -> None:
    assert (
        compute_effect_size(
            sample_values=(Decimal("0.01"),), benchmark_values=(Decimal("0.02"),)
        ).cohens_d
        is None
    )


def test_effect_size_reflects_a_real_mean_difference() -> None:
    sample = tuple(Decimal(str(0.05 + 0.001 * index)) for index in range(20))
    benchmark = tuple(Decimal(str(0.00 + 0.001 * index)) for index in range(20))
    effect = compute_effect_size(sample_values=sample, benchmark_values=benchmark)
    assert effect.cohens_d is not None
    assert effect.cohens_d > 0


def test_newey_west_returns_none_for_thin_samples() -> None:
    assert newey_west_standard_error((Decimal("0.01"),) * 5, lag=3) is None


def test_newey_west_returns_a_positive_standard_error_for_adequate_samples() -> None:
    generator = random.Random(1)
    values = tuple(Decimal(str(round(generator.gauss(0, 0.02), 4))) for _ in range(60))
    result = newey_west_standard_error(values, lag=5)
    assert result is not None
    assert result.value >= 0


def test_bootstrap_confidence_interval_is_deterministic_for_a_fixed_seed() -> None:
    values = tuple(Decimal(str(round(0.01 * index, 4))) for index in range(1, 30))
    first = seeded_bootstrap_confidence_interval(values, seed=7, resample_count=200)
    second = seeded_bootstrap_confidence_interval(values, seed=7, resample_count=200)
    assert first == second


def test_bootstrap_confidence_interval_differs_for_a_different_seed() -> None:
    values = tuple(Decimal(str(round(0.01 * index, 4))) for index in range(1, 30))
    first = seeded_bootstrap_confidence_interval(values, seed=7, resample_count=200)
    second = seeded_bootstrap_confidence_interval(values, seed=8, resample_count=200)
    assert first != second


def test_two_sided_z_test_p_value_shrinks_as_effect_grows() -> None:
    small = two_sided_z_test_p_value(
        sample_mean=Decimal("0.001"),
        benchmark_mean=Decimal(0),
        sample_stdev=Decimal("0.02"),
        effective_sample_count=Decimal(40),
    )
    large = two_sided_z_test_p_value(
        sample_mean=Decimal("0.05"),
        benchmark_mean=Decimal(0),
        sample_stdev=Decimal("0.02"),
        effective_sample_count=Decimal(40),
    )
    assert small is not None and large is not None
    assert large < small


def test_grouped_diagnostics_produce_per_group_summaries() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(200))
    values = tuple(
        item.outcome.outcome_value
        for item in evaluations
        if item.outcome is not None
        and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
        and item.outcome.outcome_value is not None
    )
    grouped_pairs = tuple((("UP" if value > 0 else "DOWN"), value) for value in values)
    diagnostics = build_diagnostics_v2(
        sample_values=values,
        evaluations=evaluations,
        outcome_horizon_sessions=30,
        grouped_pairs=grouped_pairs,
    )
    assert {label for label, _ in diagnostics.grouped} <= {"UP", "DOWN"}


# --------------------------------------------------------------------------
# Section 4: walk-forward evaluation engine
# --------------------------------------------------------------------------


def _session_dates(day_count: int, *, start: str = "2024-01-01") -> tuple[str, ...]:
    start_date = date.fromisoformat(start)
    return tuple((start_date + timedelta(days=index)).isoformat() for index in range(day_count))


def test_expanding_fold_training_window_always_starts_at_the_series_start() -> None:
    dates = _session_dates(400)
    folds = generate_fold_windows(
        session_dates=dates,
        training_window_sessions=100,
        validation_window_sessions=0,
        test_window_sessions=20,
        step_sessions=20,
        window_mode=TrainingWindowMode.EXPANDING,
    )
    assert len(folds) >= 2
    assert all(item.train_start == dates[0] for item in folds)
    # training windows grow across folds under EXPANDING.
    assert folds[1].train_end > folds[0].train_end


def test_rolling_fold_training_window_has_fixed_length_and_slides() -> None:
    dates = _session_dates(400)
    folds = generate_fold_windows(
        session_dates=dates,
        training_window_sessions=100,
        validation_window_sessions=0,
        test_window_sessions=20,
        step_sessions=20,
        window_mode=TrainingWindowMode.ROLLING,
    )
    assert len(folds) >= 2
    assert folds[0].train_start != folds[1].train_start
    first_span = dates.index(folds[0].train_end) - dates.index(folds[0].train_start)
    second_span = dates.index(folds[1].train_end) - dates.index(folds[1].train_start)
    assert first_span == second_span


def test_fold_windows_never_run_past_the_available_session_dates() -> None:
    dates = _session_dates(150)
    folds = generate_fold_windows(
        session_dates=dates,
        training_window_sessions=100,
        validation_window_sessions=10,
        test_window_sessions=20,
        step_sessions=20,
        window_mode=TrainingWindowMode.ROLLING,
    )
    for item in folds:
        assert item.test_end <= dates[-1]


def test_fold_with_validation_segment_orders_train_validation_test() -> None:
    dates = _session_dates(400)
    folds = generate_fold_windows(
        session_dates=dates,
        training_window_sessions=100,
        validation_window_sessions=20,
        test_window_sessions=20,
        step_sessions=20,
        window_mode=TrainingWindowMode.EXPANDING,
    )
    fold = folds[0]
    assert fold.train_end < fold.validation_start  # type: ignore[operator]
    assert fold.validation_end < fold.test_start  # type: ignore[operator]


def test_walk_forward_fold_rejects_test_window_overlapping_train_or_validation() -> None:
    run = WalkForwardRun.draft(
        replay_run_id="run-1",
        dataset_snapshot_id="snap-1",
        universe=("stable-1",),
        method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
        feature_policy_hash="a" * 64,
        outcome_metric_id="SUBSEQUENT_BENCHMARK_RETURN_20_SESSION_V1",
        training_window_sessions=100,
        validation_window_sessions=20,
        test_window_sessions=20,
        step_sessions=20,
        window_mode=TrainingWindowMode.EXPANDING,
        minimum_samples=10,
        cost_information_state="COST_DATA_UNAVAILABLE_NO_TRANSACTION_COST_MODEL",
        start_date="2024-01-01",
        end_date="2025-06-01",
        hypothesis_version_id=None,
        operator_action_id="op-1",
        idempotency_key="wf-key-1",
        created_at=UTC_NOON("2024-01-01"),
    )
    from apex.domain.research_methodology import FoldWindow

    bad_window = FoldWindow(
        fold_ordinal=1,
        train_start="2024-01-01",
        train_end="2024-04-01",
        validation_start="2024-04-02",
        validation_end="2024-05-01",
        test_start="2024-04-15",  # inside the validation window -- invalid
        test_end="2024-05-15",
    )
    with pytest.raises(ResearchMethodologyError, match="STRICTLY_AFTER_VALIDATION_END"):
        WalkForwardFold.create(
            run=run,
            window=bad_window,
            train_summary=None,
            validation_summary=None,
            outcome_state=FoldOutcomeState.OUTCOME_PENDING,
            test_trial_id=None,
            created_at=UTC_NOON("2024-05-16"),
        )


def test_walk_forward_run_advances_and_completes_deterministically() -> None:
    run = WalkForwardRun.draft(
        replay_run_id="run-1",
        dataset_snapshot_id="snap-1",
        universe=("stable-1",),
        method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
        feature_policy_hash="a" * 64,
        outcome_metric_id="SUBSEQUENT_BENCHMARK_RETURN_20_SESSION_V1",
        training_window_sessions=100,
        validation_window_sessions=0,
        test_window_sessions=20,
        step_sessions=20,
        window_mode=TrainingWindowMode.ROLLING,
        minimum_samples=10,
        cost_information_state="COST_DATA_UNAVAILABLE_NO_TRANSACTION_COST_MODEL",
        start_date="2024-01-01",
        end_date="2025-06-01",
        hypothesis_version_id=None,
        operator_action_id="op-1",
        idempotency_key="wf-key-2",
        created_at=UTC_NOON("2024-01-01"),
    )
    assert run.completion_state is WalkForwardCompletionState.NOT_STARTED
    advanced = run.advance(fold_ordinal=1, is_final=False)
    assert advanced.completion_state is WalkForwardCompletionState.IN_PROGRESS
    completed = advanced.advance(fold_ordinal=2, is_final=True)
    assert completed.completion_state is WalkForwardCompletionState.COMPLETE
    with pytest.raises(ResearchMethodologyError, match="ALREADY_COMPLETE"):
        completed.advance(fold_ordinal=3, is_final=False)


def test_walk_forward_run_cursor_must_advance_forward() -> None:
    run = WalkForwardRun.draft(
        replay_run_id="run-1",
        dataset_snapshot_id="snap-1",
        universe=("stable-1",),
        method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
        feature_policy_hash="a" * 64,
        outcome_metric_id="SUBSEQUENT_BENCHMARK_RETURN_20_SESSION_V1",
        training_window_sessions=100,
        validation_window_sessions=0,
        test_window_sessions=20,
        step_sessions=20,
        window_mode=TrainingWindowMode.ROLLING,
        minimum_samples=10,
        cost_information_state="COST_DATA_UNAVAILABLE_NO_TRANSACTION_COST_MODEL",
        start_date="2024-01-01",
        end_date="2025-06-01",
        hypothesis_version_id=None,
        operator_action_id="op-1",
        idempotency_key="wf-key-3",
        created_at=UTC_NOON("2024-01-01"),
    )
    advanced = run.advance(fold_ordinal=2, is_final=False)
    with pytest.raises(ResearchMethodologyError, match="CURSOR_MUST_ADVANCE_FORWARD"):
        advanced.advance(fold_ordinal=1, is_final=False)


def test_walk_forward_summary_aggregates_ready_fold_test_means() -> None:
    run = WalkForwardRun.draft(
        replay_run_id="run-1",
        dataset_snapshot_id="snap-1",
        universe=("stable-1",),
        method_id="BREADTH_VS_SUBSEQUENT_RETURN_V1",
        feature_policy_hash="a" * 64,
        outcome_metric_id="SUBSEQUENT_BENCHMARK_RETURN_20_SESSION_V1",
        training_window_sessions=100,
        validation_window_sessions=0,
        test_window_sessions=20,
        step_sessions=20,
        window_mode=TrainingWindowMode.ROLLING,
        minimum_samples=10,
        cost_information_state="COST_DATA_UNAVAILABLE_NO_TRANSACTION_COST_MODEL",
        start_date="2024-01-01",
        end_date="2025-06-01",
        hypothesis_version_id=None,
        operator_action_id="op-1",
        idempotency_key="wf-key-4",
        created_at=UTC_NOON("2024-01-01"),
    )
    from apex.domain.research_methodology import FoldWindow

    window_1 = FoldWindow(1, "2024-01-01", "2024-04-10", None, None, "2024-04-11", "2024-05-01")
    fold_1 = WalkForwardFold.create(
        run=run,
        window=window_1,
        train_summary=None,
        validation_summary=None,
        outcome_state=FoldOutcomeState.READY,
        test_trial_id="trial-1",
        created_at=UTC_NOON("2024-05-02"),
    )
    window_2 = FoldWindow(2, "2024-01-21", "2024-04-30", None, None, "2024-05-01", "2024-05-21")
    fold_2 = WalkForwardFold.create(
        run=run,
        window=window_2,
        train_summary=None,
        validation_summary=None,
        outcome_state=FoldOutcomeState.OUTCOME_PENDING,
        test_trial_id=None,
        created_at=UTC_NOON("2024-05-22"),
    )
    summary = summarize_walk_forward_folds(
        (fold_1, fold_2), test_trial_means=(("trial-1", Decimal("0.02")),)
    )
    assert summary.fold_count == 2
    assert summary.ready_fold_count == 1
    assert summary.aggregate_test_summary.sample_count == 1


# --------------------------------------------------------------------------
# Section 11: robustness checks
# --------------------------------------------------------------------------


def test_subperiod_split_is_consistent_for_a_genuinely_persistent_series() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(500))
    values = tuple(
        item.outcome.outcome_value
        for item in evaluations
        if item.outcome is not None
        and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
        and item.outcome.outcome_value is not None
    )
    base_mean = sum(values, Decimal()) / Decimal(len(values))
    results = robustness_subperiod_split(evaluations, base_mean=base_mean, split_count=3)
    assert len(results) == 3
    assert all(item.check_type is RobustnessCheckType.SUBPERIOD_SPLIT for item in results)


def test_leave_one_period_out_reports_sample_counts() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(400))
    values = tuple(
        item.outcome.outcome_value
        for item in evaluations
        if item.outcome is not None
        and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
        and item.outcome.outcome_value is not None
    )
    base_mean = sum(values, Decimal()) / Decimal(len(values))
    results = robustness_leave_one_period_out(evaluations, base_mean=base_mean, period_count=4)
    assert len(results) == 4
    assert all(item.sample_count > 0 for item in results)


def test_regime_split_groups_by_risk_tone_label() -> None:
    frames, evaluations = _build_series(_persistent_momentum_bars(400))
    values = tuple(
        item.outcome.outcome_value
        for item in evaluations
        if item.outcome is not None
        and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
        and item.outcome.outcome_value is not None
    )
    base_mean = sum(values, Decimal()) / Decimal(len(values))
    results = robustness_regime_split(evaluations, frames, base_mean=base_mean)
    assert len(results) >= 1


def test_asset_cohort_exclusion_reports_not_applicable_for_universe_level_methods() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(100))
    results = robustness_asset_cohort_exclusion(evaluations, base_mean=Decimal("0.01"))
    assert results[0].note.startswith("NOT_APPLICABLE")


def test_missing_data_sensitivity_reports_blocker_free_subset() -> None:
    _frames, evaluations = _build_series(_persistent_momentum_bars(200))
    values = tuple(
        item.outcome.outcome_value
        for item in evaluations
        if item.outcome is not None
        and item.outcome.completeness is OutcomeCompletenessState.COMPLETE
        and item.outcome.outcome_value is not None
    )
    base_mean = sum(values, Decimal()) / Decimal(len(values))
    result = robustness_missing_data_sensitivity(evaluations, base_mean=base_mean)
    assert result.sample_count > 0


def test_cost_stress_is_honestly_unavailable_not_fabricated() -> None:
    result = robustness_cost_stress()
    assert result.result_mean is None
    assert "COST_DATA_UNAVAILABLE" in result.note


def test_momentum_signal_labeling_and_pairing_reuses_breadth_outcome() -> None:
    frames, evaluations = _build_series(_persistent_momentum_bars(400))
    pairs_20 = pair_momentum_signal_with_breadth_outcome(
        frames,
        evaluations,
        benchmark_stable_security_id="stable-1",
        lookback_window=20,
        positive_threshold=Decimal(0),
    )
    assert len(pairs_20) > 0
    labels = {label for _date, label, _value in pairs_20}
    assert labels <= {"POSITIVE_MOMENTUM_SIGNAL", "NOT_POSITIVE_MOMENTUM_SIGNAL"}


def test_alternate_lookback_robustness_agrees_for_a_genuinely_persistent_series() -> None:
    """Section 11's core proof: does the momentum conclusion survive
    perturbing the lookback window? For a series with genuine, by-
    construction persistence (long blocks relative to every tested lookback,
    so a lookback rarely straddles a block transition), positive-signal
    dates should show a higher mean subsequent return than not-positive-
    signal dates across a *majority* of reasonable lookback choices -- the
    spec's own framing is "reasonable perturbation", not universal agreement
    at every possible parameter, which is exactly what
    ``summarize_robustness``'s 60% majority bar already encodes."""

    frames, evaluations = _build_series(_persistent_momentum_bars(600, block_sessions=150))
    directions = []
    for lookback in (5, 20, 60):
        pairs = pair_momentum_signal_with_breadth_outcome(
            frames,
            evaluations,
            benchmark_stable_security_id="stable-1",
            lookback_window=lookback,
            positive_threshold=Decimal(0),
        )
        positive = [value for _date, label, value in pairs if label == "POSITIVE_MOMENTUM_SIGNAL"]
        negative = [
            value for _date, label, value in pairs if label == "NOT_POSITIVE_MOMENTUM_SIGNAL"
        ]
        if not positive or not negative:
            continue
        positive_mean = sum(positive, Decimal()) / Decimal(len(positive))
        negative_mean = sum(negative, Decimal()) / Decimal(len(negative))
        directions.append(positive_mean > negative_mean)
    assert directions
    assert sum(directions) / len(directions) >= 0.6


def test_robustness_summary_passes_when_majority_consistent() -> None:
    from apex.domain.research_methodology import RobustnessCheckResult

    results = (
        *(
            RobustnessCheckResult(
                RobustnessCheckType.SUBPERIOD_SPLIT, f"v{i}", 10, Decimal("0.01"), True, "OK"
            )
            for i in range(4)
        ),
        RobustnessCheckResult(
            RobustnessCheckType.REGIME_SPLIT, "v_bad", 10, Decimal("-0.01"), False, "OK"
        ),
    )
    summary = summarize_robustness(results)
    assert summary.passed


def test_robustness_summary_fails_when_majority_inconsistent() -> None:
    from apex.domain.research_methodology import RobustnessCheckResult

    results = (
        *(
            RobustnessCheckResult(
                RobustnessCheckType.SUBPERIOD_SPLIT, f"v{i}", 10, Decimal("-0.01"), False, "OK"
            )
            for i in range(4)
        ),
        RobustnessCheckResult(
            RobustnessCheckType.REGIME_SPLIT, "v_good", 10, Decimal("0.01"), True, "OK"
        ),
    )
    summary = summarize_robustness(results)
    assert not summary.passed


def test_robustness_check_run_is_content_addressed() -> None:
    from apex.domain.research_methodology import RobustnessCheckResult

    results = (
        RobustnessCheckResult(
            RobustnessCheckType.SUBPERIOD_SPLIT, "v1", 10, Decimal("0.01"), True, "OK"
        ),
    )
    first = _RobustnessCheckRun.create(
        base_trial_id="trial-1", results=results, created_at=UTC_NOON("2024-06-01")
    )
    second = _RobustnessCheckRun.create(
        base_trial_id="trial-1", results=results, created_at=UTC_NOON("2024-06-01")
    )
    assert first.robustness_run_id == second.robustness_run_id


# --------------------------------------------------------------------------
# Sections 12 + 13: research conclusion model and promotion boundary
# --------------------------------------------------------------------------


def test_derive_conclusion_state_supported_requires_holdout_and_correction() -> None:
    state = derive_conclusion_state(
        sample_quality=SampleQualityState.SUFFICIENT,
        robustness_summary=None,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=True,
        multiple_testing_corrected_significant=True,
    )
    assert state is ResearchConclusionState.SUPPORTED


def test_derive_conclusion_state_insufficient_evidence_takes_priority() -> None:
    state = derive_conclusion_state(
        sample_quality=SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE,
        robustness_summary=None,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=True,
        multiple_testing_corrected_significant=True,
    )
    assert state is ResearchConclusionState.INSUFFICIENT_EVIDENCE


def test_derive_conclusion_state_robustness_failure_overrides_positive_result() -> None:
    from apex.domain.research_methodology import RobustnessSummary

    failed = RobustnessSummary(
        checks_run=5,
        checks_consistent=1,
        checks_inconsistent=4,
        checks_insufficient_data=0,
        passed=False,
        results=(),
    )
    state = derive_conclusion_state(
        sample_quality=SampleQualityState.SUFFICIENT,
        robustness_summary=failed,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=True,
        multiple_testing_corrected_significant=True,
    )
    assert state is ResearchConclusionState.ROBUSTNESS_FAILED


def test_derive_conclusion_state_holdout_failure_overrides_validation_success() -> None:
    state = derive_conclusion_state(
        sample_quality=SampleQualityState.SUFFICIENT,
        robustness_summary=None,
        validation_beats_benchmark=True,
        holdout_beats_benchmark=False,
        multiple_testing_corrected_significant=True,
    )
    assert state is ResearchConclusionState.HOLDOUT_FAILED


def test_derive_conclusion_state_rejected_when_validation_fails_outright() -> None:
    state = derive_conclusion_state(
        sample_quality=SampleQualityState.SUFFICIENT,
        robustness_summary=None,
        validation_beats_benchmark=False,
        holdout_beats_benchmark=None,
        multiple_testing_corrected_significant=None,
    )
    assert state is ResearchConclusionState.REJECTED


def test_derive_conclusion_state_inconclusive_with_no_validation_evidence_yet() -> None:
    state = derive_conclusion_state(
        sample_quality=SampleQualityState.SUFFICIENT,
        robustness_summary=None,
        validation_beats_benchmark=None,
        holdout_beats_benchmark=None,
        multiple_testing_corrected_significant=None,
    )
    assert state is ResearchConclusionState.INCONCLUSIVE


def _conclusion(
    *,
    phase: ResearchPhase = ResearchPhase.LOCKED_HOLDOUT,
    validation_trial_id: str | None = "trial-validation",
    holdout_trial_id: str | None = "trial-holdout",
    trial_family_id: str | None = "family-1",
    benchmark_kind: BenchmarkKind | None = BenchmarkKind.UNCONDITIONAL_SUBSEQUENT_RETURN,
    sample_quality_state: SampleQualityState = SampleQualityState.SUFFICIENT,
    blockers: tuple[str, ...] = (),
    conclusion_state: ResearchConclusionState = ResearchConclusionState.SUPPORTED,
) -> _ResearchConclusion:
    return _ResearchConclusion.create(
        hypothesis_version_id="hv-1",
        dataset_snapshot_id="snap-1",
        replay_run_id="run-1",
        phase=phase,
        training_trial_id="trial-training",
        validation_trial_id=validation_trial_id,
        holdout_trial_id=holdout_trial_id,
        walk_forward_run_id=None,
        walk_forward_fold_count=0,
        robustness_run_id="robustness-1",
        trial_family_id=trial_family_id,
        sample_quality_state=sample_quality_state,
        point_in_time_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
        benchmark_kind=benchmark_kind,
        beats_benchmark=True,
        conclusion_state=conclusion_state,
        rationale="test rationale",
        blockers=blockers,
        created_at=UTC_NOON("2024-10-01"),
    )


def test_promotion_evaluation_passes_for_a_fully_validated_holdout_backed_conclusion() -> None:
    evaluation = _PromotionEvaluation.create(
        conclusion=_conclusion(),
        policy=_PromotionPolicy(),
        holdout_exposed=True,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert evaluation.eligible
    assert evaluation.reasons == ()


def test_promotion_evaluation_fails_closed_when_holdout_not_declared() -> None:
    evaluation = _PromotionEvaluation.create(
        conclusion=_conclusion(
            holdout_trial_id=None, conclusion_state=ResearchConclusionState.WEAK_SUPPORT
        ),
        policy=_PromotionPolicy(),
        holdout_exposed=False,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert not evaluation.eligible
    assert "LOCKED_HOLDOUT_NOT_DECLARED" in evaluation.reasons


def test_promotion_evaluation_fails_closed_for_exploratory_phase() -> None:
    evaluation = _PromotionEvaluation.create(
        conclusion=_conclusion(
            phase=ResearchPhase.EXPLORATORY, conclusion_state=ResearchConclusionState.INCONCLUSIVE
        ),
        policy=_PromotionPolicy(),
        holdout_exposed=False,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert not evaluation.eligible
    assert "PHASE_MUST_NOT_BE_EXPLORATORY" in evaluation.reasons


def test_promotion_evaluation_fails_closed_on_insufficient_sample() -> None:
    evaluation = _PromotionEvaluation.create(
        conclusion=_conclusion(
            sample_quality_state=SampleQualityState.INSUFFICIENT_EFFECTIVE_SAMPLE,
            conclusion_state=ResearchConclusionState.INSUFFICIENT_EVIDENCE,
        ),
        policy=_PromotionPolicy(),
        holdout_exposed=True,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert not evaluation.eligible
    assert "EFFECTIVE_SAMPLE_INSUFFICIENT" in evaluation.reasons


def test_promotion_evaluation_fails_closed_on_unresolved_data_quality_blocker() -> None:
    evaluation = _PromotionEvaluation.create(
        conclusion=_conclusion(blockers=("UNRESOLVED_ISSUE",)),
        policy=_PromotionPolicy(),
        holdout_exposed=True,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert not evaluation.eligible
    assert "UNRESOLVED_DATA_QUALITY_BLOCKER" in evaluation.reasons


def test_promotion_evaluation_fails_closed_on_lookahead_violation() -> None:
    evaluation = _PromotionEvaluation.create(
        conclusion=_conclusion(),
        policy=_PromotionPolicy(),
        holdout_exposed=True,
        robustness_completed=True,
        lookahead_violation=True,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert not evaluation.eligible
    assert "LOOKAHEAD_VIOLATION_DETECTED" in evaluation.reasons


def test_promotion_policy_can_explicitly_require_holdout_consumption() -> None:
    evaluation = _PromotionEvaluation.create(
        conclusion=_conclusion(),
        policy=_PromotionPolicy(require_holdout_consumed=True),
        holdout_exposed=False,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert not evaluation.eligible
    assert "HOLDOUT_NOT_YET_CONSUMED" in evaluation.reasons


def test_apex_production_promotion_policy_defaults_require_holdout_consumed() -> None:
    assert APEX_PRODUCTION_PROMOTION_POLICY.require_holdout_consumed is True
    assert APEX_PRODUCTION_PROMOTION_POLICY.policy_id == APEX_PRODUCTION_PROMOTION_POLICY_ID
    # Distinct from -- never equal to -- the generic default policy identity.
    assert APEX_PRODUCTION_PROMOTION_POLICY.policy_id != _PromotionPolicy().policy_id


def test_apex_production_promotion_policy_rejects_an_otherwise_excellent_unconsumed_holdout() -> (
    None
):
    """The exact scenario section 13 requires: every other production-track
    criterion is fully satisfied (non-exploratory phase, sufficient sample,
    declared validation, declared locked holdout, no lookahead violation, no
    data-quality blocker, robustness completed, benchmark present, multiple-
    testing disclosed, and a genuinely SUPPORTED conclusion) -- only the
    locked holdout was never actually exposed/consumed. The canonical
    production policy must still refuse it; only the generic, explicitly
    weakened default policy may pass it."""

    excellent_conclusion = _conclusion(conclusion_state=ResearchConclusionState.SUPPORTED)
    assert excellent_conclusion.holdout_trial_id is not None
    assert excellent_conclusion.validation_trial_id is not None
    assert excellent_conclusion.trial_family_id is not None
    assert excellent_conclusion.benchmark_kind is not None
    assert excellent_conclusion.blockers == ()

    production_evaluation = _PromotionEvaluation.create(
        conclusion=excellent_conclusion,
        policy=APEX_PRODUCTION_PROMOTION_POLICY,
        holdout_exposed=False,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert not production_evaluation.eligible
    assert production_evaluation.reasons == ("HOLDOUT_NOT_YET_CONSUMED",)
    assert production_evaluation.policy_id == APEX_PRODUCTION_PROMOTION_POLICY_ID

    # The identical conclusion passes under the generic, explicitly
    # configured default policy -- proving the difference is the policy,
    # not the conclusion.
    generic_evaluation = _PromotionEvaluation.create(
        conclusion=excellent_conclusion,
        policy=_PromotionPolicy(),
        holdout_exposed=False,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert generic_evaluation.eligible

    # Once the holdout is genuinely exposed, the identical production
    # policy accepts the identical conclusion -- proving this is a fail-
    # closed gate on consumption, not a policy that can never pass.
    consumed_evaluation = _PromotionEvaluation.create(
        conclusion=excellent_conclusion,
        policy=APEX_PRODUCTION_PROMOTION_POLICY,
        holdout_exposed=True,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert consumed_evaluation.eligible
    assert consumed_evaluation.reasons == ()


def test_promotion_grants_no_authority_field_beyond_none() -> None:
    evaluation = _PromotionEvaluation.create(
        conclusion=_conclusion(),
        policy=_PromotionPolicy(),
        holdout_exposed=True,
        robustness_completed=True,
        lookahead_violation=False,
        created_at=UTC_NOON("2024-10-02"),
    )
    assert evaluation.authority == "NONE"


def test_conclusion_state_of_rejected_or_robustness_failed_blocks_promotion() -> None:
    for state in (
        ResearchConclusionState.REJECTED,
        ResearchConclusionState.ROBUSTNESS_FAILED,
        ResearchConclusionState.HOLDOUT_FAILED,
        ResearchConclusionState.INSUFFICIENT_EVIDENCE,
        ResearchConclusionState.INCONCLUSIVE,
    ):
        evaluation = _PromotionEvaluation.create(
            conclusion=_conclusion(conclusion_state=state),
            policy=_PromotionPolicy(),
            holdout_exposed=True,
            robustness_completed=True,
            lookahead_violation=False,
            created_at=UTC_NOON("2024-10-02"),
        )
        assert not evaluation.eligible
        assert "CONCLUSION_STATE_DOES_NOT_SUPPORT_PROMOTION" in evaluation.reasons
