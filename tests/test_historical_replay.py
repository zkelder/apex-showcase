"""Pure-domain proofs for the point-in-time historical replay engine: clock
semantics, point-in-time feature frames, the market-regime layer, outcome
isolation, research-quality/holdout controls, and statistical summaries.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.historical_market_data import PointInTimeError, point_in_time_slice
from apex.domain.historical_replay import (
    CalendarCoverageState,
    HistoricalReplayError,
    HistoricalResearchEvaluation,
    HoldoutViolationError,
    OutcomeCompletenessState,
    OutcomeIsolationError,
    OutcomeWindowDeclaration,
    ReplayCadence,
    ReplayCheckpoint,
    ReplayRun,
    ReplayRunCompletionState,
    ResearchMethodId,
    ResearchPeriodDeclaration,
    ResearchPeriodPolicy,
    ResearchPeriodRole,
    ResearchQualityError,
    RiskToneLabel,
    SummaryKind,
    VolatilityRegimeLabel,
    apply_cross_sectional_ranks,
    assert_minimum_sample,
    assess_outcome,
    build_market_regime_snapshot,
    build_security_feature_vector,
    classify_breadth_strength,
    classify_correlation_regime,
    classify_dispersion,
    classify_trend_strength,
    classify_volatility_regime,
    compute_breadth,
    detect_overlapping_outcomes,
    evaluate_breadth_vs_subsequent_return,
    group_summaries,
    guard_holdout_access,
    multi_window_returns,
    observation_age_days,
    step_replay_clock,
    summarize_values,
)
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
)

INSTRUMENT = "instrument-aaa"


def _bar(
    day: str,
    close: str,
    *,
    open_: str | None = None,
    high: str | None = None,
    low: str | None = None,
    volume: str = "1000",
    lag_hours: int = 1,
    instrument_id: str = INSTRUMENT,
) -> SecuritiesBarEvidence:
    period_start = datetime.fromisoformat(f"{day}T14:30:00+00:00")
    period_end = datetime.fromisoformat(f"{day}T21:00:00+00:00")
    return SecuritiesBarEvidence(
        instrument_id=instrument_id,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state="RAW",
        period_start=period_start,
        period_end=period_end,
        available_at=period_end + timedelta(hours=lag_hours),
        open=open_ or close,
        high=high or close,
        low=low or close,
        close=close,
        volume=volume,
        source_identity="test-fixture",
    )


def _session(day: str) -> SecuritiesCalendarSession:
    return SecuritiesCalendarSession.create(
        calendar_identity="XNYS",
        calendar_version="test-v1",
        timezone="America/New_York",
        session_date=day,
        opens_at=datetime.fromisoformat(f"{day}T14:30:00+00:00"),
        closes_at=datetime.fromisoformat(f"{day}T21:00:00+00:00"),
        is_early_close=False,
        source_identity="test-fixture-calendar",
    )


def _daily_series(
    start: str, count: int, *, base: Decimal = Decimal("100"), step: Decimal = Decimal("1")
) -> tuple[SecuritiesBarEvidence, ...]:
    from datetime import date as _date

    start_date = _date.fromisoformat(start)
    bars = []
    price = base
    for index in range(count):
        day = (start_date + timedelta(days=index)).isoformat()
        bars.append(_bar(day, str(price)))
        price += step
    return tuple(bars)


UTC_NOON = lambda day: datetime.fromisoformat(f"{day}T12:00:00+00:00")  # noqa: E731


# --------------------------------------------------------------------------
# Clock / slicing
# --------------------------------------------------------------------------


def test_future_observation_excluded_by_multi_window_returns() -> None:
    bars = _daily_series("2024-01-01", 5)
    as_of = bars[2].available_at
    sliced = point_in_time_slice(bars, as_of=as_of)
    assert len(sliced) == 3  # only bars 0, 1, 2 are visible at as_of
    returns = dict(multi_window_returns(sliced, as_of=as_of, windows=(1,)))
    assert returns[1] is not None
    # Raising a future bar's close must not move the computed return: it is
    # excluded from the point-in-time slice entirely.
    mutated = list(bars)
    mutated[-1] = _bar("2024-01-09", "999999")
    resliced = point_in_time_slice(tuple(mutated), as_of=as_of)
    returns_after = dict(multi_window_returns(resliced, as_of=as_of, windows=(1,)))
    assert returns_after[1] == returns[1]
    # A caller that forgets to slice at all is rejected loudly, not silently
    # corrected -- "reject future evidence loudly" (section 3).
    with pytest.raises(PointInTimeError):
        multi_window_returns(bars, as_of=as_of, windows=(1,))


def test_available_at_respected_not_period_end() -> None:
    # A bar whose period ends before as_of but whose available_at (publication
    # lag) is after as_of must be excluded.
    early_period_late_publish = _bar("2024-01-01", "100", lag_hours=48)
    as_of = early_period_late_publish.period_end + timedelta(hours=1)
    assert early_period_late_publish.available_at > as_of
    sliced = point_in_time_slice((early_period_late_publish,), as_of=as_of)
    assert sliced == ()
    age = observation_age_days(sliced, as_of=as_of)
    assert age is None  # excluded entirely: no observation is visible yet


def test_weekend_and_holiday_dates_are_never_sessions() -> None:
    # A calendar covering a Fri/Mon pair (skipping Sat/Sun) proves the clock
    # only ever steps on genuine calendar evidence, not a naive +1 day.
    friday = _session("2024-01-05")
    monday = _session("2024-01-08")
    clock, session_date, coverage, _warnings = step_replay_clock(
        after_date="2024-01-05",
        start_date="2024-01-01",
        end_date="2024-01-12",
        calendar_identity="XNYS",
        sessions=(friday, monday),
    )
    assert session_date == "2024-01-08"
    assert clock is not None and clock.as_of == monday.closes_at
    assert coverage is CalendarCoverageState.CALENDAR_EVIDENCE_AVAILABLE


def test_missing_session_date_reports_calendar_evidence_unavailable() -> None:
    clock, session_date, coverage, warnings = step_replay_clock(
        after_date=None,
        start_date="2024-01-01",
        end_date="2024-01-05",
        calendar_identity="XNYS",
        sessions=(),
    )
    assert clock is None
    assert session_date is None
    assert coverage is CalendarCoverageState.CALENDAR_EVIDENCE_UNAVAILABLE
    assert "NO_CALENDAR_EVIDENCE_FOR_DECLARED_RANGE" in warnings


def test_calendar_less_replay_steps_continuously_and_flags_fallback() -> None:
    clock, session_date, coverage, warnings = step_replay_clock(
        after_date="2024-01-01",
        start_date="2024-01-01",
        end_date="2024-01-10",
        calendar_identity=None,
        sessions=(),
    )
    assert session_date == "2024-01-02"
    assert clock is not None
    assert coverage is CalendarCoverageState.CALENDAR_EVIDENCE_UNAVAILABLE
    assert "NO_EXCHANGE_CALENDAR_DECLARED_CONTINUOUS_DAILY_STEPPING" in warnings


def test_deterministic_next_as_of_is_a_pure_function_of_inputs() -> None:
    sessions = tuple(_session(d) for d in ("2024-01-02", "2024-01-03", "2024-01-04"))
    first = step_replay_clock(
        after_date="2024-01-02",
        start_date="2024-01-01",
        end_date="2024-01-10",
        calendar_identity="XNYS",
        sessions=sessions,
    )
    second = step_replay_clock(
        after_date="2024-01-02",
        start_date="2024-01-01",
        end_date="2024-01-10",
        calendar_identity="XNYS",
        sessions=sessions,
    )
    assert first == second


def test_same_date_replay_requires_strictly_forward_cursor() -> None:
    run = ReplayRun.draft(
        dataset_snapshot_id="snap-1",
        universe=("stable-1",),
        start_date="2024-01-01",
        end_date="2024-01-10",
        cadence=ReplayCadence.DAILY,
        calendar_identity=None,
        feature_policy_id="fp-1",
        feature_policy_hash="a" * 64,
        research_policy_id="rp-1",
        research_policy_hash="b" * 64,
        initial_as_of=UTC_NOON("2024-01-01"),
        final_as_of=UTC_NOON("2024-01-10"),
        calendar_coverage_state=CalendarCoverageState.CALENDAR_EVIDENCE_UNAVAILABLE,
        calendar_warnings=(),
        hypothesis_version_id=None,
        operator_action_id="op-1",
        idempotency_key="idem-1",
        created_at=UTC_NOON("2024-01-01"),
    )
    advanced = run.advance(next_as_of=UTC_NOON("2024-01-02"), session_date="2024-01-02")
    with pytest.raises(HistoricalReplayError, match="CURSOR_MUST_ADVANCE_FORWARD"):
        advanced.advance(next_as_of=UTC_NOON("2024-01-02"), session_date="2024-01-02")


def test_replay_run_completes_exactly_at_final_as_of_and_rejects_overrun() -> None:
    run = ReplayRun.draft(
        dataset_snapshot_id="snap-1",
        universe=("stable-1",),
        start_date="2024-01-01",
        end_date="2024-01-02",
        cadence=ReplayCadence.DAILY,
        calendar_identity=None,
        feature_policy_id="fp-1",
        feature_policy_hash="a" * 64,
        research_policy_id="rp-1",
        research_policy_hash="b" * 64,
        initial_as_of=UTC_NOON("2024-01-01"),
        final_as_of=UTC_NOON("2024-01-02"),
        calendar_coverage_state=CalendarCoverageState.CALENDAR_EVIDENCE_UNAVAILABLE,
        calendar_warnings=(),
        hypothesis_version_id=None,
        operator_action_id="op-1",
        idempotency_key="idem-1",
        created_at=UTC_NOON("2024-01-01"),
    )
    assert run.completion_state is ReplayRunCompletionState.NOT_STARTED
    completed = run.advance(next_as_of=UTC_NOON("2024-01-02"), session_date="2024-01-02")
    assert completed.completion_state is ReplayRunCompletionState.COMPLETE
    with pytest.raises(HistoricalReplayError, match="ALREADY_COMPLETE"):
        completed.advance(next_as_of=UTC_NOON("2024-01-03"), session_date="2024-01-03")
    with pytest.raises(HistoricalReplayError, match="EXCEEDS_DECLARED_RANGE"):
        run.advance(next_as_of=UTC_NOON("2024-01-05"), session_date="2024-01-05")


# --------------------------------------------------------------------------
# Feature frames
# --------------------------------------------------------------------------


def test_returns_and_volatility_and_drawdown_and_liquidity_computed() -> None:
    bars = _daily_series("2024-01-01", 30, base=Decimal("100"), step=Decimal("1"))
    as_of = bars[-1].available_at
    vector = build_security_feature_vector(
        stable_security_id="stable-1",
        instrument_id=INSTRUMENT,
        bars=bars,
        benchmark_bars=None,
        as_of=as_of,
        session_date="2024-01-30",
    )
    one_session = dict(vector.returns)[1]
    assert one_session is not None and one_session > 0
    assert vector.rolling_volatility_20 is not None
    assert vector.drawdown is not None and vector.drawdown == Decimal("0.00000000")
    assert vector.rolling_liquidity_20 is not None
    assert vector.observation_count == 30


def test_cross_sectional_rank_orders_two_securities() -> None:
    strong = build_security_feature_vector(
        stable_security_id="strong",
        instrument_id="inst-strong",
        bars=_daily_series("2024-01-01", 25, base=Decimal("100"), step=Decimal("2")),
        benchmark_bars=None,
        as_of=datetime.fromisoformat("2024-01-25T22:00:00+00:00"),
        session_date="2024-01-25",
    )
    weak = build_security_feature_vector(
        stable_security_id="weak",
        instrument_id="inst-weak",
        bars=_daily_series("2024-01-01", 25, base=Decimal("100"), step=Decimal("-1")),
        benchmark_bars=None,
        as_of=datetime.fromisoformat("2024-01-25T22:00:00+00:00"),
        session_date="2024-01-25",
    )
    ranked = apply_cross_sectional_ranks((strong, weak))
    by_id = {item.stable_security_id: item for item in ranked}
    # Tie-aware fractional rank ((below + tied/2) / n) with n=2 puts the
    # unique top value at 0.75 and the unique bottom value at 0.25 -- see
    # domain.historical_market_data.cross_sectional_rank.
    assert by_id["strong"].cross_sectional_return_rank == Decimal("0.75000000")
    assert by_id["weak"].cross_sectional_return_rank == Decimal("0.25000000")
    assert by_id["strong"].cross_sectional_return_rank > by_id["weak"].cross_sectional_return_rank


def test_benchmark_correlation_computed_when_benchmark_supplied() -> None:
    bars = _daily_series("2024-01-01", 70, base=Decimal("50"), step=Decimal("1"))
    benchmark = _daily_series("2024-01-01", 70, base=Decimal("200"), step=Decimal("2"))
    as_of = bars[-1].available_at
    vector = build_security_feature_vector(
        stable_security_id="stable-1",
        instrument_id=INSTRUMENT,
        bars=bars,
        benchmark_bars=benchmark,
        as_of=as_of,
        session_date="2024-03-10",
    )
    assert vector.benchmark_correlation_60 is not None


def test_insufficient_lookback_reports_none_not_a_fabricated_value() -> None:
    bars = _daily_series("2024-01-01", 3)
    as_of = bars[-1].available_at
    vector = build_security_feature_vector(
        stable_security_id="stable-1",
        instrument_id=INSTRUMENT,
        bars=bars,
        benchmark_bars=None,
        as_of=as_of,
        session_date="2024-01-03",
    )
    assert dict(vector.returns)[252] is None
    assert vector.missing_history is True
    assert "INSUFFICIENT_LOOKBACK_FOR_ONE_OR_MORE_RETURN_WINDOWS" in vector.unavailable_reasons


def test_missing_evidence_yields_unavailable_vector_not_a_crash() -> None:
    vector = build_security_feature_vector(
        stable_security_id="stable-1",
        instrument_id=INSTRUMENT,
        bars=(),
        benchmark_bars=None,
        as_of=UTC_NOON("2024-01-05"),
        session_date="2024-01-05",
    )
    assert vector.observation_count == 0
    assert vector.evidence_availability.value == "UNAVAILABLE"
    assert "NO_RETAINED_OBSERVATIONS_AT_OR_BEFORE_AS_OF" in vector.unavailable_reasons


def test_breadth_from_advancing_declining_cohort() -> None:
    breadth = compute_breadth(
        universe_size=4,
        one_session_returns=(
            ("a", Decimal("0.01")),
            ("b", Decimal("0.02")),
            ("c", Decimal("-0.01")),
            ("d", Decimal("0")),
        ),
        above_moving_average=(("a", True), ("b", True), ("c", False), ("d", False)),
    )
    assert breadth.advancing_count == 2
    assert breadth.declining_count == 1
    assert breadth.unchanged_count == 1
    assert breadth.fraction_advancing == Decimal("0.50000000")
    assert breadth.fraction_above_moving_average == Decimal("0.50000000")


def test_breadth_with_no_evaluable_securities_is_honestly_unavailable() -> None:
    breadth = compute_breadth(universe_size=5, one_session_returns=(), above_moving_average=())
    assert breadth.fraction_advancing is None
    assert "NO_SECURITIES_WITH_ONE_SESSION_RETURN_EVIDENCE" in breadth.reasons


# --------------------------------------------------------------------------
# Regimes
# --------------------------------------------------------------------------


def test_regime_classification_is_evidence_derived() -> None:
    assert classify_breadth_strength(Decimal("0.70")).value == "BROADLY_ADVANCING"
    assert classify_breadth_strength(Decimal("0.30")).value == "BROADLY_DECLINING"
    assert classify_breadth_strength(Decimal("0.50")).value == "MIXED"
    assert classify_breadth_strength(None).value == "INSUFFICIENT_EVIDENCE"
    assert classify_volatility_regime(Decimal("0.50")).value == "HIGH_VOLATILITY"
    assert classify_volatility_regime(Decimal("0.10")).value == "LOW_VOLATILITY"
    assert classify_trend_strength(Decimal("0.20")).value == "STRONG_UPTREND"
    assert classify_dispersion(Decimal("0.10")).value == "HIGH_DISPERSION"
    assert classify_correlation_regime(Decimal("0.80")).value == "HIGH_CORRELATION"


def test_regime_snapshot_has_deterministic_identity_given_identical_evidence() -> None:
    breadth = compute_breadth(
        universe_size=2,
        one_session_returns=(("a", Decimal("0.01")), ("b", Decimal("0.02"))),
        above_moving_average=(("a", True), ("b", True)),
    )
    kwargs = dict(
        universe_size=2,
        breadth=breadth,
        cross_sectional_return_stdev=Decimal("0.01"),
        cross_sectional_return_evaluable=2,
        benchmark_annualized_volatility=Decimal("0.12"),
        benchmark_return_20_session=Decimal("0.08"),
        average_pairwise_correlation=Decimal("0.4"),
        correlation_evaluable=2,
    )
    first = build_market_regime_snapshot(**kwargs)
    second = build_market_regime_snapshot(**kwargs)
    assert first == second


def test_regime_changes_as_evidence_changes() -> None:
    calm_breadth = compute_breadth(
        universe_size=2,
        one_session_returns=(("a", Decimal("0.01")), ("b", Decimal("0.02"))),
        above_moving_average=(("a", True), ("b", True)),
    )
    calm = build_market_regime_snapshot(
        universe_size=2,
        breadth=calm_breadth,
        cross_sectional_return_stdev=Decimal("0.01"),
        cross_sectional_return_evaluable=2,
        benchmark_annualized_volatility=Decimal("0.10"),
        benchmark_return_20_session=Decimal("0.08"),
        average_pairwise_correlation=Decimal("0.2"),
        correlation_evaluable=2,
    )
    stressed_breadth = compute_breadth(
        universe_size=2,
        one_session_returns=(("a", Decimal("-0.05")), ("b", Decimal("-0.06"))),
        above_moving_average=(("a", False), ("b", False)),
    )
    stressed = build_market_regime_snapshot(
        universe_size=2,
        breadth=stressed_breadth,
        cross_sectional_return_stdev=Decimal("0.09"),
        cross_sectional_return_evaluable=2,
        benchmark_annualized_volatility=Decimal("0.55"),
        benchmark_return_20_session=Decimal("-0.20"),
        average_pairwise_correlation=Decimal("0.9"),
        correlation_evaluable=2,
    )
    from apex.domain.historical_replay import RegimeDimension

    assert calm.label_for(RegimeDimension.RISK_TONE) == RiskToneLabel.RISK_ON.value
    assert stressed.label_for(RegimeDimension.RISK_TONE) == RiskToneLabel.RISK_OFF.value
    assert (
        stressed.label_for(RegimeDimension.VOLATILITY_REGIME)
        == VolatilityRegimeLabel.HIGH_VOLATILITY.value
    )
    assert calm != stressed


def test_no_historical_event_is_hardcoded_in_regime_rules() -> None:
    import inspect

    from apex.domain import historical_replay

    source = inspect.getsource(historical_replay)
    # A handful of dates that would appear if any historically famous crash/
    # rally date were hardcoded into the classification rules.
    for forbidden in ("2020-03", "2008-09", "2008-10", "1987-10", "2000-03"):
        assert forbidden not in source


# --------------------------------------------------------------------------
# Outcome isolation
# --------------------------------------------------------------------------


def test_outcome_window_must_start_strictly_after_feature_cutoff() -> None:
    cutoff = UTC_NOON("2024-01-10")
    with pytest.raises(OutcomeIsolationError, match="STRICTLY_AFTER"):
        OutcomeWindowDeclaration.create(
            feature_cutoff=cutoff,
            outcome_start=cutoff,
            outcome_end=cutoff + timedelta(days=20),
            outcome_metric_id="m1",
        )


def test_future_outcome_bars_are_inaccessible_before_replay_clock_advances() -> None:
    cutoff = UTC_NOON("2024-01-10")
    window = OutcomeWindowDeclaration.create(
        feature_cutoff=cutoff,
        outcome_start=cutoff + timedelta(days=1),
        outcome_end=cutoff + timedelta(days=20),
        outcome_metric_id="m1",
    )
    outcome_bars = _daily_series("2024-01-11", 25)
    with pytest.raises(OutcomeIsolationError, match="HAS_NOT_ADVANCED"):
        assess_outcome(
            outcome_id="o1",
            window=window,
            outcome_bars=outcome_bars,
            replay_as_of=cutoff,  # clock has not moved past the cutoff at all
            observed_at=cutoff,
        )


def test_incomplete_outcome_is_reported_partial_then_complete() -> None:
    cutoff = UTC_NOON("2024-01-01")
    window = OutcomeWindowDeclaration.create(
        feature_cutoff=cutoff,
        outcome_start=cutoff + timedelta(days=1),
        outcome_end=cutoff + timedelta(days=20),
        outcome_metric_id="m1",
    )
    outcome_bars = _daily_series("2024-01-02", 30)
    partial_as_of = cutoff + timedelta(days=10)
    partial = assess_outcome(
        outcome_id="o1",
        window=window,
        outcome_bars=outcome_bars,
        replay_as_of=partial_as_of,
        observed_at=partial_as_of,
    )
    assert partial.completeness is OutcomeCompletenessState.PARTIAL
    assert partial.outcome_value is not None

    complete_as_of = cutoff + timedelta(days=25)
    complete = assess_outcome(
        outcome_id="o1",
        window=window,
        outcome_bars=outcome_bars,
        replay_as_of=complete_as_of,
        observed_at=complete_as_of,
    )
    assert complete.completeness is OutcomeCompletenessState.COMPLETE


def test_outcome_pending_before_window_opens() -> None:
    cutoff = UTC_NOON("2024-01-01")
    window = OutcomeWindowDeclaration.create(
        feature_cutoff=cutoff,
        outcome_start=cutoff + timedelta(days=5),
        outcome_end=cutoff + timedelta(days=20),
        outcome_metric_id="m1",
    )
    result = assess_outcome(
        outcome_id="o1",
        window=window,
        outcome_bars=(),
        replay_as_of=cutoff + timedelta(days=2),
        observed_at=cutoff + timedelta(days=2),
    )
    assert result.completeness is OutcomeCompletenessState.PENDING


def test_overlapping_outcome_windows_are_flagged() -> None:
    # Two consecutive-day evaluations of the same method both declare a
    # 30-calendar-day outcome window (see OUTCOME_HORIZON_CALENDAR_DAYS) --
    # those two windows necessarily overlap by 29 days.
    frame_bars = _daily_series("2024-01-01", 300)
    frame = _make_frame(frame_bars, as_of=frame_bars[260].available_at, session_date="2024-01-01")
    evaluation_a = evaluate_breadth_vs_subsequent_return(
        evaluation_id="",
        replay_run_id="run-1",
        frame=frame,
        hypothesis_id=None,
        created_at=frame.as_of,
    )
    assert evaluation_a.outcome_window is not None
    assessed_a = assess_outcome(
        outcome_id="oa",
        window=evaluation_a.outcome_window,
        outcome_bars=frame_bars,
        replay_as_of=frame.as_of + timedelta(days=35),
        observed_at=frame.as_of + timedelta(days=35),
    )
    evaluation_a = evaluation_a.attach_outcome(assessed_a)

    frame_2 = _make_frame(frame_bars, as_of=frame_bars[261].available_at, session_date="2024-01-02")
    evaluation_b = evaluate_breadth_vs_subsequent_return(
        evaluation_id="",
        replay_run_id="run-1",
        frame=frame_2,
        hypothesis_id=None,
        created_at=frame_2.as_of,
    )
    assert evaluation_b.outcome_window is not None
    assessed_b = assess_outcome(
        outcome_id="ob",
        window=evaluation_b.outcome_window,
        outcome_bars=frame_bars,
        replay_as_of=frame_2.as_of + timedelta(days=35),
        observed_at=frame_2.as_of + timedelta(days=35),
    )
    evaluation_b = evaluation_b.attach_outcome(assessed_b)

    warnings = detect_overlapping_outcomes((evaluation_a, evaluation_b))
    assert len(warnings) == 1
    assert warnings[0].overlap_days == 28
    assert warnings[0].overlap_days > 0


def _make_frame(bars, *, as_of, session_date):
    from apex.domain.historical_replay import ReplayFeatureFrame, apply_cross_sectional_ranks

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
    from apex.domain.values import content_digest

    payload = ("test-frame", as_of, session_date)
    return ReplayFeatureFrame(
        frame_id=f"frame-{session_date}-{as_of.isoformat()}",
        replay_run_id="run-1",
        dataset_snapshot_id="snap-1",
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


# --------------------------------------------------------------------------
# Research quality / holdout
# --------------------------------------------------------------------------


def test_train_validation_holdout_periods_must_not_overlap() -> None:
    with pytest.raises(ResearchQualityError, match="OVERLAP"):
        ResearchPeriodPolicy.create(
            replay_run_id="run-1",
            periods=(
                ResearchPeriodDeclaration(
                    ResearchPeriodRole.TRAIN, "2020-01-01", "2021-06-01", False
                ),
                ResearchPeriodDeclaration(
                    ResearchPeriodRole.VALIDATION, "2021-01-01", "2021-12-31", False
                ),
            ),
            minimum_sample_count=10,
            created_at=UTC_NOON("2024-01-01"),
        )


def test_valid_train_validation_holdout_policy_round_trips_role_lookup() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2020-01-01", "2021-12-31", False),
            ResearchPeriodDeclaration(
                ResearchPeriodRole.VALIDATION, "2022-01-01", "2022-12-31", False
            ),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2023-01-01", "2023-12-31", True),
        ),
        minimum_sample_count=30,
        created_at=UTC_NOON("2024-01-01"),
    )
    assert policy.role_for_date("2020-06-15") is ResearchPeriodRole.TRAIN
    assert policy.role_for_date("2022-06-15") is ResearchPeriodRole.VALIDATION
    assert policy.role_for_date("2023-06-15") is ResearchPeriodRole.HOLDOUT
    assert policy.role_for_date("2019-01-01") is None


def test_locked_holdout_fails_closed_for_development_purpose() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2020-01-01", "2021-12-31", False),
            ResearchPeriodDeclaration(ResearchPeriodRole.HOLDOUT, "2023-01-01", "2023-12-31", True),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    guard_holdout_access(policy, role=ResearchPeriodRole.HOLDOUT, purpose="EVALUATION_ONLY")
    with pytest.raises(HoldoutViolationError, match="CANNOT_BE_USED_FOR_DEVELOPMENT"):
        guard_holdout_access(policy, role=ResearchPeriodRole.HOLDOUT, purpose="FEATURE_DEVELOPMENT")
    # TRAIN is deliberately unlocked and may be iterated on freely.
    guard_holdout_access(policy, role=ResearchPeriodRole.TRAIN, purpose="FEATURE_DEVELOPMENT")


def test_forbidden_holdout_access_when_role_not_declared() -> None:
    policy = ResearchPeriodPolicy.create(
        replay_run_id="run-1",
        periods=(
            ResearchPeriodDeclaration(ResearchPeriodRole.TRAIN, "2020-01-01", "2021-12-31", False),
        ),
        minimum_sample_count=10,
        created_at=UTC_NOON("2024-01-01"),
    )
    with pytest.raises(HoldoutViolationError, match="NOT_DECLARED"):
        guard_holdout_access(policy, role=ResearchPeriodRole.HOLDOUT, purpose="EVALUATION_ONLY")


def test_checkpoint_retains_completed_frame_and_evaluation_identities() -> None:
    run = ReplayRun.draft(
        dataset_snapshot_id="snap-1",
        universe=("stable-1",),
        start_date="2024-01-01",
        end_date="2024-01-10",
        cadence=ReplayCadence.DAILY,
        calendar_identity=None,
        feature_policy_id="fp-1",
        feature_policy_hash="a" * 64,
        research_policy_id="rp-1",
        research_policy_hash="b" * 64,
        initial_as_of=UTC_NOON("2024-01-01"),
        final_as_of=UTC_NOON("2024-01-10"),
        calendar_coverage_state=CalendarCoverageState.CALENDAR_EVIDENCE_UNAVAILABLE,
        calendar_warnings=(),
        hypothesis_version_id=None,
        operator_action_id="op-1",
        idempotency_key="idem-1",
        created_at=UTC_NOON("2024-01-01"),
    ).advance(next_as_of=UTC_NOON("2024-01-02"), session_date="2024-01-02")
    checkpoint = ReplayCheckpoint.create(
        run=run,
        as_of=run.cursor_as_of,
        session_date=run.cursor_session_date,
        frame_ids=("frame-1",),
        evaluation_ids=("eval-1", "eval-2"),
        created_at=run.cursor_as_of,
    )
    assert checkpoint.frame_ids == ("frame-1",)
    assert checkpoint.evaluation_ids == ("eval-1", "eval-2")
    assert checkpoint.dataset_snapshot_id == "snap-1"
    replay = ReplayCheckpoint.create(
        run=run,
        as_of=run.cursor_as_of,
        session_date=run.cursor_session_date,
        frame_ids=("frame-1",),
        evaluation_ids=("eval-1", "eval-2"),
        created_at=run.cursor_as_of,
    )
    assert checkpoint.checkpoint_id == replay.checkpoint_id


def test_evaluation_created_via_research_method_carries_method_identity() -> None:
    bars = _daily_series("2024-01-01", 260)
    frame = _make_frame(bars, as_of=bars[-1].available_at, session_date="2024-12-01")
    evaluation = evaluate_breadth_vs_subsequent_return(
        evaluation_id="",
        replay_run_id="run-1",
        frame=frame,
        hypothesis_id="hyp-1",
        created_at=frame.as_of,
    )
    assert evaluation.method_id is ResearchMethodId.BREADTH_VS_SUBSEQUENT_RETURN_V1
    assert evaluation.outcome is None
    assert isinstance(evaluation, HistoricalResearchEvaluation)


def test_minimum_sample_requirement() -> None:
    assert_minimum_sample(10, 10)
    with pytest.raises(ResearchQualityError, match="MINIMUM_SAMPLE_NOT_MET"):
        assert_minimum_sample(5, 10)


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def test_deterministic_summary_of_known_values() -> None:
    values = tuple(Decimal(v) for v in ("0.01", "0.02", "-0.01", "0.03", "-0.02"))
    summary = summarize_values(values, kind=SummaryKind.EXPLORATORY, group_label="all")
    assert summary.sample_count == 5
    assert summary.mean == Decimal("0.00600000")
    assert summary.hit_rate == Decimal("0.60000000")
    assert summary.confidence_interval is not None
    assert summary.confidence_interval.method_id == "NORMAL_APPROXIMATION_V1"
    assert "SMALL_SAMPLE_NORMAL_APPROXIMATION_MAY_BE_UNRELIABLE" in summary.warnings


def test_grouping_by_regime_label() -> None:
    pairs = (
        ("RISK_ON", Decimal("0.02")),
        ("RISK_ON", Decimal("0.01")),
        ("RISK_OFF", Decimal("-0.03")),
    )
    grouped = dict(group_summaries(pairs, kind=SummaryKind.EXPLORATORY))
    assert grouped["RISK_ON"].sample_count == 2
    assert grouped["RISK_OFF"].sample_count == 1


def test_sparse_sample_confidence_interval_is_flagged_or_absent() -> None:
    single = summarize_values((Decimal("0.05"),), kind=SummaryKind.EXPLORATORY, group_label="one")
    assert single.sample_count == 1
    assert single.confidence_interval is None
    assert "INSUFFICIENT_SAMPLES_FOR_CONFIDENCE_INTERVAL" in single.warnings
    empty = summarize_values((), kind=SummaryKind.EXPLORATORY, group_label="none")
    assert empty.sample_count == 0
    assert "NO_SAMPLES" in empty.warnings


def test_confidence_interval_methodology_is_explicit() -> None:
    values = tuple(Decimal(v) / 100 for v in range(1, 40))
    summary = summarize_values(values, kind=SummaryKind.VALIDATION, group_label="all")
    assert summary.confidence_interval is not None
    lower, upper = summary.confidence_interval.lower, summary.confidence_interval.upper
    assert lower < summary.mean < upper
    assert summary.confidence_interval.method_id == "NORMAL_APPROXIMATION_V1"


def test_summary_kind_distinguishes_exploratory_validation_holdout() -> None:
    values = (Decimal("0.01"), Decimal("0.02"))
    exploratory = summarize_values(values, kind=SummaryKind.EXPLORATORY, group_label="x")
    holdout = summarize_values(values, kind=SummaryKind.HOLDOUT, group_label="x")
    assert exploratory.kind is SummaryKind.EXPLORATORY
    assert holdout.kind is SummaryKind.HOLDOUT
