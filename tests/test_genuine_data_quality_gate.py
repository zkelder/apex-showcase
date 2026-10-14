from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apex.domain.genuine_data_quality_gate import (
    GenuineCohortQualityGateReport,
    InstrumentQualityClassification,
    assess_instrument_quality,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
)

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def _bar(
    instrument_id: str, day: str, close: str, *, volume: str = "1000"
) -> SecuritiesBarEvidence:
    start = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    end = start + timedelta(hours=6, minutes=30)
    return SecuritiesBarEvidence(
        instrument_id=instrument_id,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=start,
        period_end=end,
        available_at=end + timedelta(minutes=30),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        source_identity="TEST",
    )


def _session(day: str) -> SecuritiesCalendarSession:
    opens = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    return SecuritiesCalendarSession.create(
        calendar_identity="XNYS",
        calendar_version="1",
        timezone="America/New_York",
        session_date=day,
        opens_at=opens,
        closes_at=opens + timedelta(hours=6, minutes=30),
        is_early_close=False,
        source_identity="TEST",
    )


_DAYS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]


def _weekdays(start: str, count: int) -> list[str]:
    """``count`` genuinely distinct Mon-Fri calendar dates starting at
    ``start`` (inclusive) -- no holiday calendar, just distinct weekdays,
    which is all this pure-domain module's own date bookkeeping needs."""

    cursor = datetime.fromisoformat(f"{start}T00:00:00+00:00")
    out: list[str] = []
    while len(out) < count:
        if cursor.weekday() < 5:
            out.append(cursor.date().isoformat())
        cursor += timedelta(days=1)
    return out


def test_clean_instrument_with_enough_history_is_usable() -> None:
    days = _weekdays("2024-01-02", 300)
    bars = tuple(_bar("iid", day, "100") for day in days)  # 300 clean bars, > minimum
    sessions = tuple(_session(day) for day in days)
    assessment = assess_instrument_quality(
        stable_security_id="stable-1",
        symbol="ZZZ",
        instrument_id="iid",
        bars=bars,
        calendar_sessions=sessions,
        source_batch_ids=("batch-1",),
    )
    assert assessment.classification is InstrumentQualityClassification.USABLE
    assert assessment.reasons == ()


def test_zero_observations_is_rejected_not_insufficient() -> None:
    assessment = assess_instrument_quality(
        stable_security_id="stable-1",
        symbol="ZZZ",
        instrument_id="iid",
        bars=(),
        calendar_sessions=(),
        source_batch_ids=(),
    )
    assert assessment.classification is InstrumentQualityClassification.REJECTED
    assert "ZERO_OBSERVATIONS" in assessment.reasons


def test_short_history_is_insufficient_not_rejected() -> None:
    bars = tuple(_bar("iid", day, "100") for day in _DAYS)  # only 5 bars
    assessment = assess_instrument_quality(
        stable_security_id="stable-1",
        symbol="ZZZ",
        instrument_id="iid",
        bars=bars,
        calendar_sessions=tuple(_session(day) for day in _DAYS),
        source_batch_ids=("batch-1",),
    )
    assert assessment.classification is InstrumentQualityClassification.INSUFFICIENT


def test_duplicate_timestamp_is_rejected() -> None:
    bars = tuple(_bar("iid", "2024-01-02", "100") for _ in range(300))
    assessment = assess_instrument_quality(
        stable_security_id="stable-1",
        symbol="ZZZ",
        instrument_id="iid",
        bars=bars,
        calendar_sessions=(_session("2024-01-02"),),
        source_batch_ids=("batch-1",),
    )
    assert assessment.classification is InstrumentQualityClassification.REJECTED
    assert "DUPLICATE_TIMESTAMPS_PRESENT" in assessment.reasons


def test_missing_session_before_own_coverage_start_is_not_counted_as_a_gap() -> None:
    """The instrument's genuine coverage starts on 2024-06-01; a calendar
    session that predates that (e.g. from the cohort's broader declared
    policy window) must never be counted as a missing session within its
    own coverage -- that is a coverage-window fact, not a defect."""

    days = _weekdays("2024-06-03", 260)  # >= 252 bars
    bars = tuple(_bar("iid", day, "100") for day in days)
    pre_coverage_sessions = tuple(_session(day) for day in _weekdays("2020-01-01", 4))
    own_sessions = tuple(_session(day) for day in days)
    assessment = assess_instrument_quality(
        stable_security_id="stable-1",
        symbol="ZZZ",
        instrument_id="iid",
        bars=bars,
        calendar_sessions=(*pre_coverage_sessions, *own_sessions),
        source_batch_ids=("batch-1",),
    )
    assert assessment.missing_sessions_within_coverage_count == 0
    assert assessment.classification is InstrumentQualityClassification.USABLE


def test_missing_session_within_own_coverage_is_a_warning() -> None:
    days = _weekdays("2024-06-03", 260)
    missing_day = days[100]
    days_with_gap = [day for day in days if day != missing_day]
    bars = tuple(_bar("iid", day, "100") for day in days_with_gap)
    sessions = tuple(_session(day) for day in days)  # calendar still expects the gap day
    assessment = assess_instrument_quality(
        stable_security_id="stable-1",
        symbol="ZZZ",
        instrument_id="iid",
        bars=bars,
        calendar_sessions=sessions,
        source_batch_ids=("batch-1",),
    )
    assert assessment.missing_sessions_within_coverage_count == 1
    assert assessment.classification is InstrumentQualityClassification.USABLE_WITH_WARNINGS


def test_known_split_discontinuity_is_explained_not_flagged() -> None:
    pre_split_days = _weekdays("2020-06-01", 65)  # runs up to (not through) 2020-08-31
    assert "2020-08-31" not in pre_split_days
    post_split_days = _weekdays("2020-08-31", 200)  # starts exactly on the split date
    bars = [_bar("iid", day, "500") for day in pre_split_days]
    bars.extend(
        _bar("iid", day, "129.11" if day == "2020-08-31" else "130") for day in post_split_days
    )
    assessment = assess_instrument_quality(
        stable_security_id="stable-1",
        symbol="AAPL",
        instrument_id="iid",
        bars=tuple(bars),
        calendar_sessions=tuple(_session(bar.period_start.date().isoformat()) for bar in bars),
        source_batch_ids=("batch-1",),
    )
    assert assessment.unexplained_discontinuity_count == 0
    assert assessment.explained_discontinuity_count == 1
    assert assessment.classification is not InstrumentQualityClassification.REJECTED


def test_report_build_counts_classifications_and_finds_missing_symbols() -> None:
    usable_bars = tuple(_bar("iid-a", day, "100") for day in _weekdays("2024-01-02", 300))
    usable = assess_instrument_quality(
        stable_security_id="stable-a",
        symbol="AAA",
        instrument_id="iid-a",
        bars=usable_bars,
        calendar_sessions=tuple(
            _session(bar.period_start.date().isoformat()) for bar in usable_bars
        ),
        source_batch_ids=("batch-1",),
    )
    rejected = assess_instrument_quality(
        stable_security_id="stable-b",
        symbol="BBB",
        instrument_id="iid-b",
        bars=(),
        calendar_sessions=(),
        source_batch_ids=(),
    )
    report = GenuineCohortQualityGateReport.build(
        snapshot_id="snap-1",
        cohort_policy_id="policy-1",
        requested_symbols=("AAA", "BBB", "CCC"),
        assessments=(usable, rejected),
        point_in_time_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
        source_hashes=("hash-1",),
        created_at=NOW,
    )
    assert report.requested_instrument_count == 3
    assert report.successful_instrument_count == 2
    assert report.missing_instrument_symbols == ("CCC",)
    assert report.usable_count == 1
    assert report.rejected_count == 1
    assert report.canonical_research_cohort_symbols() == ("AAA",)


def test_report_is_deterministic_given_identical_inputs() -> None:
    usable_bars = tuple(_bar("iid-a", day, "100") for day in _weekdays("2024-01-02", 300))
    usable = assess_instrument_quality(
        stable_security_id="stable-a",
        symbol="AAA",
        instrument_id="iid-a",
        bars=usable_bars,
        calendar_sessions=tuple(
            _session(bar.period_start.date().isoformat()) for bar in usable_bars
        ),
        source_batch_ids=("batch-1",),
    )
    first = GenuineCohortQualityGateReport.build(
        snapshot_id="snap-1",
        cohort_policy_id="policy-1",
        requested_symbols=("AAA",),
        assessments=(usable,),
        point_in_time_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
        source_hashes=("hash-1",),
        created_at=NOW,
    )
    second = GenuineCohortQualityGateReport.build(
        snapshot_id="snap-1",
        cohort_policy_id="policy-1",
        requested_symbols=("AAA",),
        assessments=(usable,),
        point_in_time_limitations=("POINT_IN_TIME_UNIVERSE_INCOMPLETE",),
        source_hashes=("hash-1",),
        created_at=NOW,
    )
    assert first.report_id == second.report_id
    assert first.canonical_hash == second.canonical_hash
