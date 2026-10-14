from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.corporate_action_consistency import (
    KNOWN_SPLIT_FACTS_V1,
    KnownSplitFact,
    SplitConsistencyOutcome,
    applicable_known_splits,
    check_split_consistency,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
)

INSTRUMENT_ID = "historical_instrument_test"


def _bar(day: str, close: str) -> SecuritiesBarEvidence:
    start = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    end = start + timedelta(hours=6, minutes=30)
    return SecuritiesBarEvidence(
        instrument_id=INSTRUMENT_ID,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=start,
        period_end=end,
        available_at=end + timedelta(minutes=30),
        open=close,
        high=close,
        low=close,
        close=close,
        volume="1000",
        source_identity="TEST",
    )


def _bars(*pairs: tuple[str, str]) -> tuple[SecuritiesBarEvidence, ...]:
    return tuple(_bar(day, close) for day, close in pairs)


def test_known_split_facts_are_a_small_versioned_public_table() -> None:
    assert 1 <= len(KNOWN_SPLIT_FACTS_V1) <= 50
    for fact in KNOWN_SPLIT_FACTS_V1:
        assert fact.forward_split_ratio > 1


def test_consistent_four_for_one_split_within_tolerance() -> None:
    fact = KnownSplitFact("ZZZ", "2020-08-31", Decimal(4), "test 4:1")
    bars = _bars(
        ("2020-08-27", "499.75"),
        ("2020-08-28", "499.345"),
        ("2020-08-31", "129.11"),
        ("2020-09-01", "134.46"),
    )
    result = check_split_consistency(bars, fact=fact)
    assert result.outcome is SplitConsistencyOutcome.CONSISTENT
    assert result.pre_split_date == "2020-08-28"
    assert result.post_split_date == "2020-08-31"
    assert result.observed_ratio is not None
    assert abs(result.observed_ratio - Decimal(4)) / Decimal(4) <= Decimal("0.25")


def test_wrong_ratio_is_reported_inconsistent_not_silently_accepted() -> None:
    fact = KnownSplitFact("ZZZ", "2020-08-31", Decimal(4), "test 4:1 mislabeled as 2:1 move")
    bars = _bars(
        ("2020-08-28", "100"),
        ("2020-08-31", "50"),  # genuinely a 2:1 discontinuity, not 4:1
    )
    result = check_split_consistency(bars, fact=fact)
    assert result.outcome is SplitConsistencyOutcome.INCONSISTENT_RATIO


def test_no_discontinuity_present_is_reported_honestly() -> None:
    fact = KnownSplitFact("ZZZ", "2020-08-31", Decimal(4), "test 4:1 that never happened here")
    bars = _bars(
        ("2020-08-28", "100"),
        ("2020-08-31", "101"),
    )
    result = check_split_consistency(bars, fact=fact)
    assert result.outcome is SplitConsistencyOutcome.NO_DISCONTINUITY_DETECTED


def test_missing_one_side_of_the_window_reports_outside_retained_coverage() -> None:
    fact = KnownSplitFact("ZZZ", "2020-08-31", Decimal(4), "only post-split evidence retained")
    bars = _bars(("2020-09-01", "134.46"), ("2020-09-02", "131.58"))
    result = check_split_consistency(bars, fact=fact)
    assert result.outcome is SplitConsistencyOutcome.OUTSIDE_RETAINED_COVERAGE
    assert result.observed_ratio is None


def test_empty_bars_reports_outside_retained_coverage_never_raises() -> None:
    fact = KnownSplitFact("ZZZ", "2020-08-31", Decimal(4), "no evidence at all")
    result = check_split_consistency((), fact=fact)
    assert result.outcome is SplitConsistencyOutcome.OUTSIDE_RETAINED_COVERAGE


def test_tolerance_must_be_positive() -> None:
    fact = KnownSplitFact("ZZZ", "2020-08-31", Decimal(4), "x")
    with pytest.raises(ValueError, match="TOLERANCE_MUST_BE_POSITIVE"):
        check_split_consistency(
            _bars(("2020-08-28", "1"), ("2020-08-31", "1")), fact=fact, tolerance=Decimal(0)
        )


def test_applicable_known_splits_filters_by_cohort_and_coverage_window() -> None:
    applicable = applicable_known_splits(
        symbols=("AAPL", "MSFT"), coverage_start="2020-07-27", coverage_end="2026-08-06"
    )
    assert {item.symbol for item in applicable} == {"AAPL"}


def test_applicable_known_splits_excludes_dates_outside_the_window() -> None:
    applicable = applicable_known_splits(
        symbols=("AAPL", "TSLA", "GOOGL", "NVDA"),
        coverage_start="2021-01-01",
        coverage_end="2021-12-31",
    )
    assert {item.symbol for item in applicable} == {"NVDA"}
    assert applicable[0].effective_date == "2021-07-20"
