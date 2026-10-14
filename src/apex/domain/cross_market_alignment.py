"""Cross-market alignment primitives (milestone section 11).

Deliberately thin. Most of what a cross-market dataset needs already
exists and is reused unmodified:

* point-in-time gating -- ``domain.historical_market_data.point_in_time_slice``
  / ``assert_no_lookahead`` (equity) and the identical ``available_at <=
  as_of`` rule ``application.historical_replay.crypto_return_and_volatility``
  already applies to crypto candles via ``HistoricalObservationView``.
* prior-session alignment -- ``application.securities.SecuritiesCalendarPort
  .previous_session``/``next_session`` (``adapters.xnys_calendar.
  ReviewedXNYSCalendar``).
* bounded context freshness -- ``domain.total_market_context
  .COMPONENT_FRESHNESS_LIMIT_SECONDS``/``ContextComponent.freshness_seconds``.

What is genuinely new here is the venue-agnostic layer *above* those: this
module operates on the shared ``HistoricalObservationView`` shape
(``domain.historical_market_data``) so the identical alignment/missingness
logic applies to an equity bar and a crypto candle without special-casing
either, plus one small, explicit, generalized bounded-forward-fill gate for
*context* values only -- its own type signature makes it structurally
impossible to pass a raw price series through it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from apex.domain.historical_market_data import HistoricalObservationView

CROSS_MARKET_ALIGNMENT_V1 = "CROSS_MARKET_ALIGNMENT_V1"
DEFAULT_MAXIMUM_OBSERVATION_AGE_DAYS = 10


class CrossMarketAlignmentError(ValueError):
    pass


class AvailabilityState(StrEnum):
    """Deliberately mirrors ``domain.historical_replay.
    EvidenceAvailabilityState`` (``CURRENT``/``STALE_LAST_KNOWN``/
    ``UNAVAILABLE``) so a caller already familiar with that per-security
    equity vocabulary reads this identically for any venue."""

    CURRENT = "CURRENT"
    STALE_LAST_KNOWN = "STALE_LAST_KNOWN"
    UNAVAILABLE = "UNAVAILABLE"


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise CrossMarketAlignmentError(f"{label} must be timezone-aware")
    return value


def point_in_time_views(
    views: Sequence[HistoricalObservationView], *, as_of: datetime
) -> tuple[HistoricalObservationView, ...]:
    """The venue-agnostic sibling of ``historical_market_data.
    point_in_time_slice`` -- keep only views visible at or before ``as_of``,
    ordered by event time. Works identically for an equity bar view or a
    crypto candle view; never forward-fills a value that is not yet visible."""

    _aware(as_of, "as_of")
    return tuple(
        sorted(
            (item for item in views if item.available_at <= as_of), key=lambda item: item.event_at
        )
    )


def calendar_date_alignment(
    *, target_session_date: str, available_event_dates: frozenset[str]
) -> bool:
    """Equity/crypto calendar-date alignment (section 11, first bullet):
    equity trades only on declared exchange sessions; crypto trades every
    calendar day, so an equity session date is always itself a valid
    calendar date to look a same-day crypto observation up by -- no
    interpolation or nearest-neighbor matching is needed or performed.
    Returns whether a genuine observation exists for that exact date; it
    is the caller's job to decide what to do when it does not (see
    ``generic_evidence_availability`` below for the bounded, honest
    answer -- never silently substituting a different date's value)."""

    date.fromisoformat(target_session_date)  # validates shape; raises if malformed
    return target_session_date in available_event_dates


def generic_observation_age_days(
    views: Sequence[HistoricalObservationView], *, as_of: datetime
) -> int | None:
    """Source-age measurement (section 11): calendar days between ``as_of``
    and the latest point-in-time-visible observation's own event date, for
    any venue's ``HistoricalObservationView`` series. ``None`` when nothing
    is visible yet -- never a fabricated age of zero."""

    visible = point_in_time_views(views, as_of=as_of)
    if not visible:
        return None
    return (as_of.date() - visible[-1].event_at.date()).days


def generic_evidence_availability(
    views: Sequence[HistoricalObservationView],
    *,
    as_of: datetime,
    target_date: str,
    maximum_age_days: int = DEFAULT_MAXIMUM_OBSERVATION_AGE_DAYS,
) -> AvailabilityState:
    """Missingness indicator (section 11): is there a *current* (exact same
    date), *stale-but-usable* (within ``maximum_age_days``), or genuinely
    *unavailable* observation visible at ``as_of`` for ``target_date``? This
    never forward-fills a price/candle value itself -- it only classifies
    whether one may honestly be treated as still-relevant context, mirroring
    ``domain.historical_replay.evidence_availability``'s equity-only logic
    for any venue's observation view."""

    if maximum_age_days < 0:
        raise CrossMarketAlignmentError("MAXIMUM_AGE_DAYS_MUST_BE_NONNEGATIVE")
    visible = point_in_time_views(views, as_of=as_of)
    if not visible:
        return AvailabilityState.UNAVAILABLE
    latest = visible[-1]
    if latest.event_at.date().isoformat() == target_date:
        return AvailabilityState.CURRENT
    age = (date.fromisoformat(target_date) - latest.event_at.date()).days
    if age < 0 or age > maximum_age_days:
        return AvailabilityState.UNAVAILABLE
    return AvailabilityState.STALE_LAST_KNOWN


def bounded_context_forward_fill(
    *, context_observed_at: datetime, target_as_of: datetime, freshness_limit_seconds: int
) -> bool:
    """The one explicitly-permitted forward-fill in this milestone (section
    11): a *context* classification (e.g. a ``TotalMarketContextSnapshot``
    component -- never a raw equity/crypto price) may still be treated as
    valid up to ``freshness_limit_seconds`` after it was actually observed.
    Deliberately takes only two timestamps and a bound, never a price or
    bar/candle value, so it is structurally impossible to launder a raw
    price series through this gate -- reuses the identical bound
    ``domain.total_market_context.COMPONENT_FRESHNESS_LIMIT_SECONDS``
    already establishes for live context; callers pass that constant (or
    their own tighter one) explicitly rather than this module silently
    picking a default."""

    _aware(context_observed_at, "context_observed_at")
    _aware(target_as_of, "target_as_of")
    if freshness_limit_seconds < 0:
        raise CrossMarketAlignmentError("FRESHNESS_LIMIT_SECONDS_MUST_BE_NONNEGATIVE")
    age_seconds = (target_as_of - context_observed_at).total_seconds()
    if age_seconds < 0:
        raise CrossMarketAlignmentError("CONTEXT_OBSERVED_AT_AFTER_TARGET_AS_OF")
    return age_seconds <= freshness_limit_seconds


@dataclass(frozen=True, slots=True)
class CrossMarketMissingnessIndicator:
    """One as_of's bounded missingness/age summary across every market this
    milestone's cross-market dataset spans -- the retained "BTC/equity/
    context availability" evidence section 12/15 asks for, in one small,
    reusable shape."""

    as_of: datetime
    equity_availability: AvailabilityState
    equity_age_days: int | None
    crypto_availability: AvailabilityState
    crypto_age_days: int | None
    context_available: bool
    context_age_seconds: int | None

    def __post_init__(self) -> None:
        _aware(self.as_of, "CrossMarketMissingnessIndicator.as_of")


def build_missingness_indicator(
    *,
    as_of: datetime,
    equity_views: Sequence[HistoricalObservationView],
    crypto_views: Sequence[HistoricalObservationView],
    equity_session_date: str,
    context_observed_at: datetime | None,
    context_freshness_limit_seconds: int,
) -> CrossMarketMissingnessIndicator:
    equity_state = generic_evidence_availability(
        equity_views, as_of=as_of, target_date=equity_session_date
    )
    crypto_state = generic_evidence_availability(
        crypto_views, as_of=as_of, target_date=equity_session_date
    )
    context_available = context_observed_at is not None and bounded_context_forward_fill(
        context_observed_at=context_observed_at,
        target_as_of=as_of,
        freshness_limit_seconds=context_freshness_limit_seconds,
    )
    context_age = (
        None if context_observed_at is None else int((as_of - context_observed_at).total_seconds())
    )
    return CrossMarketMissingnessIndicator(
        as_of=as_of,
        equity_availability=equity_state,
        equity_age_days=generic_observation_age_days(equity_views, as_of=as_of),
        crypto_availability=crypto_state,
        crypto_age_days=generic_observation_age_days(crypto_views, as_of=as_of),
        context_available=context_available,
        context_age_seconds=context_age,
    )
