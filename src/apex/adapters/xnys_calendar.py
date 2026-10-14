"""Narrow retained XNYS session evidence built with pandas-market-calendars."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.metadata import version

import pandas_market_calendars as market_calendars  # type: ignore[import-untyped]

from apex.application.securities import SecuritiesCalendarPort
from apex.domain.securities import SecuritiesCalendarSession

XNYS_CALENDAR_IDENTITY = "XNYS"
XNYS_CALENDAR_IMPLEMENTATION = "pandas-market-calendars"
XNYS_CALENDAR_VERSION = version("pandas-market-calendars")


class ReviewedXNYSCalendar(SecuritiesCalendarPort):
    """One deterministic, bounded XNYS schedule; it is not a calendar platform."""

    def __init__(self, *, start_date: str, end_date: str) -> None:
        if start_date > end_date:
            raise ValueError("XNYS calendar end precedes start")
        schedule = market_calendars.get_calendar(XNYS_CALENDAR_IDENTITY).schedule(
            start_date=start_date, end_date=end_date
        )
        self._sessions = tuple(
            SecuritiesCalendarSession.create(
                calendar_identity=XNYS_CALENDAR_IDENTITY,
                calendar_version=f"{XNYS_CALENDAR_IMPLEMENTATION}=={XNYS_CALENDAR_VERSION}",
                timezone="America/New_York",
                session_date=session_date.date().isoformat(),
                opens_at=_utc(open_at.to_pydatetime()),
                closes_at=_utc(close_at.to_pydatetime()),
                is_early_close=_utc(close_at.to_pydatetime()).hour < 20,
                source_identity=(
                    f"reviewed-xnys:{XNYS_CALENDAR_IMPLEMENTATION}=={XNYS_CALENDAR_VERSION}"
                ),
            )
            for session_date, row in schedule.iterrows()
            for open_at, close_at in ((row["market_open"], row["market_close"]),)
        )

    @property
    def sessions(self) -> tuple[SecuritiesCalendarSession, ...]:
        return self._sessions

    def sessions_between(
        self, *, calendar_identity: str, start_date: str, end_date: str
    ) -> tuple[SecuritiesCalendarSession, ...]:
        _require_xnys(calendar_identity)
        return tuple(item for item in self._sessions if start_date <= item.session_date <= end_date)

    def is_session(self, *, calendar_identity: str, session_date: str) -> bool:
        _require_xnys(calendar_identity)
        return any(item.session_date == session_date for item in self._sessions)

    def previous_session(
        self, *, calendar_identity: str, session_date: str
    ) -> SecuritiesCalendarSession | None:
        _require_xnys(calendar_identity)
        values = [item for item in self._sessions if item.session_date < session_date]
        return values[-1] if values else None

    def next_session(
        self, *, calendar_identity: str, session_date: str
    ) -> SecuritiesCalendarSession | None:
        _require_xnys(calendar_identity)
        return next((item for item in self._sessions if item.session_date > session_date), None)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _require_xnys(value: str) -> None:
    if value != XNYS_CALENDAR_IDENTITY:
        raise ValueError("unsupported reviewed securities calendar")
