from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.cross_market_alignment import (
    AvailabilityState,
    CrossMarketAlignmentError,
    bounded_context_forward_fill,
    build_missingness_indicator,
    calendar_date_alignment,
    generic_evidence_availability,
    generic_observation_age_days,
    point_in_time_views,
)
from apex.domain.historical_market_data import HistoricalObservationView


def _view(event_date: str, *, available_date: str | None = None) -> HistoricalObservationView:
    event_at = datetime.fromisoformat(f"{event_date}T23:59:59+00:00")
    available_at = (
        event_at
        if available_date is None
        else datetime.fromisoformat(f"{available_date}T23:59:59+00:00")
    )
    return HistoricalObservationView(
        instrument_key="ZZZ",
        venue="EQUITY_ETF",
        event_at=event_at,
        available_at=available_at,
        open=Decimal(100),
        high=Decimal(100),
        low=Decimal(100),
        close=Decimal(100),
        volume=Decimal(1),
        source="TEST",
    )


def test_point_in_time_views_excludes_future_evidence() -> None:
    views = (_view("2024-01-01"), _view("2024-01-02"), _view("2024-01-03"))
    visible = point_in_time_views(views, as_of=datetime(2024, 1, 2, 23, 59, 59, tzinfo=UTC))
    assert [item.event_at.date().isoformat() for item in visible] == ["2024-01-01", "2024-01-02"]


def test_calendar_date_alignment_true_when_same_day_observation_exists() -> None:
    assert calendar_date_alignment(
        target_session_date="2024-01-02", available_event_dates=frozenset({"2024-01-02"})
    )


def test_calendar_date_alignment_false_when_no_same_day_observation() -> None:
    assert not calendar_date_alignment(
        target_session_date="2024-01-02", available_event_dates=frozenset({"2024-01-01"})
    )


def test_calendar_date_alignment_rejects_malformed_date() -> None:
    with pytest.raises(ValueError):
        calendar_date_alignment(target_session_date="not-a-date", available_event_dates=frozenset())


def test_generic_observation_age_days_none_when_nothing_visible() -> None:
    views = (_view("2024-06-01"),)
    assert generic_observation_age_days(views, as_of=datetime(2024, 5, 1, tzinfo=UTC)) is None


def test_generic_observation_age_days_measures_from_latest_visible() -> None:
    views = (_view("2024-06-01"), _view("2024-06-03"))
    age = generic_observation_age_days(views, as_of=datetime(2024, 6, 6, 12, tzinfo=UTC))
    assert age == 3


def test_generic_evidence_availability_current_when_exact_date_present() -> None:
    views = (_view("2024-06-03"),)
    state = generic_evidence_availability(
        views, as_of=datetime(2024, 6, 3, 23, 59, 59, tzinfo=UTC), target_date="2024-06-03"
    )
    assert state is AvailabilityState.CURRENT


def test_generic_evidence_availability_stale_within_bound() -> None:
    views = (_view("2024-06-01"),)
    state = generic_evidence_availability(
        views,
        as_of=datetime(2024, 6, 5, 23, 59, 59, tzinfo=UTC),
        target_date="2024-06-05",
        maximum_age_days=10,
    )
    assert state is AvailabilityState.STALE_LAST_KNOWN


def test_generic_evidence_availability_unavailable_beyond_bound() -> None:
    views = (_view("2024-06-01"),)
    state = generic_evidence_availability(
        views,
        as_of=datetime(2024, 7, 1, 23, 59, 59, tzinfo=UTC),
        target_date="2024-07-01",
        maximum_age_days=10,
    )
    assert state is AvailabilityState.UNAVAILABLE


def test_generic_evidence_availability_unavailable_when_no_evidence_at_all() -> None:
    state = generic_evidence_availability(
        (), as_of=datetime(2024, 7, 1, tzinfo=UTC), target_date="2024-07-01"
    )
    assert state is AvailabilityState.UNAVAILABLE


def test_bounded_context_forward_fill_true_within_limit() -> None:
    observed = datetime(2024, 1, 1, tzinfo=UTC)
    target = observed + timedelta(hours=1)
    assert bounded_context_forward_fill(
        context_observed_at=observed, target_as_of=target, freshness_limit_seconds=3600
    )


def test_bounded_context_forward_fill_false_beyond_limit() -> None:
    observed = datetime(2024, 1, 1, tzinfo=UTC)
    target = observed + timedelta(hours=2)
    assert not bounded_context_forward_fill(
        context_observed_at=observed, target_as_of=target, freshness_limit_seconds=3600
    )


def test_bounded_context_forward_fill_rejects_context_observed_after_target() -> None:
    observed = datetime(2024, 1, 2, tzinfo=UTC)
    target = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(CrossMarketAlignmentError, match="AFTER_TARGET_AS_OF"):
        bounded_context_forward_fill(
            context_observed_at=observed, target_as_of=target, freshness_limit_seconds=3600
        )


def test_build_missingness_indicator_reports_each_market_independently() -> None:
    as_of = datetime(2024, 6, 3, 23, 59, 59, tzinfo=UTC)
    equity_views = (_view("2024-06-03"),)
    crypto_views = ()  # genuinely absent -- never fabricated
    indicator = build_missingness_indicator(
        as_of=as_of,
        equity_views=equity_views,
        crypto_views=crypto_views,
        equity_session_date="2024-06-03",
        context_observed_at=datetime(2024, 6, 3, 12, tzinfo=UTC),
        context_freshness_limit_seconds=259_200,
    )
    assert indicator.equity_availability is AvailabilityState.CURRENT
    assert indicator.equity_age_days == 0
    assert indicator.crypto_availability is AvailabilityState.UNAVAILABLE
    assert indicator.crypto_age_days is None
    assert indicator.context_available is True


def test_build_missingness_indicator_handles_absent_context_honestly() -> None:
    as_of = datetime(2024, 6, 3, 23, 59, 59, tzinfo=UTC)
    indicator = build_missingness_indicator(
        as_of=as_of,
        equity_views=(),
        crypto_views=(),
        equity_session_date="2024-06-03",
        context_observed_at=None,
        context_freshness_limit_seconds=259_200,
    )
    assert indicator.context_available is False
    assert indicator.context_age_seconds is None
