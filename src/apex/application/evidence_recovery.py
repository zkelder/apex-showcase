"""Historical Evidence Gap Recovery v1: detection and bounded recovery.

Adapter-free orchestration. Detects what Apex failed to retain while it was
off, records every gap honestly, recovers only the two families whose
provider history is the same economic fact (daily securities bars, daily
crypto candles), and records everything else ``IRRECOVERABLE``.

Hard boundaries:

* No decision, opportunity, cycle, live observation, order, fill, journal or
  reconciliation is ever created for a missed interval.
* Recovered evidence is labeled ``RECOVERY_BACKFILL`` in the vintage sidecar
  and can never satisfy a live freshness requirement.
* A provider record that disagrees with a retained one is flagged
  ``CONFLICTED`` and never overwrites anything.
* Every run is bounded by an explicit request ceiling and a deterministic
  order, and replays are idempotent.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Protocol

from apex.domain.evidence_recovery import (
    IRRECOVERABLE_REASONS,
    NOMINAL_CAPTURE_LAG,
    REASON_BLOCKED_CREDENTIALS,
    REASON_PARTIAL_PROVIDER_COVERAGE,
    REASON_PROVIDER_RETURNED_NO_BAR,
    REASON_PROVIDER_REVISION_CONFLICT,
    REASON_RECOVERED_FROM_PROVIDER_HISTORY,
    REASON_SELF_HEALED_BY_ROLLING_WINDOW,
    AcquisitionPath,
    BarValues,
    EvidenceCoverageGapEvent,
    EvidenceFamily,
    EvidenceRecoveryRun,
    EvidenceVintage,
    GapDisposition,
    RecoveryRunStatus,
    gap_key_for,
    group_contiguous_days,
    group_contiguous_sessions,
    missing_sessions,
    missing_utc_days,
    provider_revision_conflict,
)

# How many sessions before an instrument's last retained bar are re-examined
# for interior holes. Older history is never re-opened by this task.
INTERIOR_HOLE_LOOKBACK_SESSIONS = 60
# Bars become economically available this long after the session close.
BAR_AVAILABILITY_DELAY = timedelta(minutes=30)
CRYPTO_RECOVERY_LOOKBACK_DAYS = 365
# The pulse retains day D's candle in the refresh whose as_of date is D + 2
# (a refresh covers only completed days before its own as_of), so the newest
# candle is "due" for one more cycle, never a gap.
CRYPTO_NOMINAL_LAG_DAYS = 1
# A live-observation round cadence gap larger than this is a gap.
LIVE_OBSERVATION_GAP_THRESHOLD = timedelta(minutes=10)
LIVE_OBSERVATION_LOOKBACK = timedelta(days=30)
MAXIMUM_IRRECOVERABLE_GAPS_PER_RUN = 500
# A strategy PAPER horizon close later than this after its target is a
# missed execution window (the execution fact itself cannot be recovered).
PAPER_CLOSE_DELAY_THRESHOLD = timedelta(hours=6)


class EvidenceRecoveryRepository(Protocol):
    def register_run(self, *, run: EvidenceRecoveryRun, recorded_at: datetime) -> bool: ...

    def run(self, run_id: str) -> EvidenceRecoveryRun | None: ...

    def register_gap_event(
        self, *, event: EvidenceCoverageGapEvent, recorded_at: datetime
    ) -> bool: ...

    def gap_history(self, gap_key: str) -> tuple[EvidenceCoverageGapEvent, ...]: ...

    def current_gaps(
        self, *, evidence_family: EvidenceFamily | None = None
    ) -> tuple[EvidenceCoverageGapEvent, ...]: ...

    def unresolved_gaps(
        self, *, evidence_family: EvidenceFamily
    ) -> tuple[EvidenceCoverageGapEvent, ...]: ...

    def register_vintages(
        self, *, vintages: tuple[EvidenceVintage, ...], recorded_at: datetime
    ) -> int: ...

    def commit_recovery(
        self,
        *,
        run: EvidenceRecoveryRun,
        events: tuple[EvidenceCoverageGapEvent, ...],
        vintages: tuple[EvidenceVintage, ...],
        recorded_at: datetime,
    ) -> int:
        """Atomically retain a run, its gap resolutions and its vintages (all
        idempotent). Returns the number of vintages newly written."""
        ...


# --------------------------------------------------------------------------
# Securities ports
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExchangeSession:
    session_date: date
    closes_at: datetime

    @property
    def available_at(self) -> datetime:
        return self.closes_at + BAR_AVAILABILITY_DELAY


class ExchangeCalendarPort(Protocol):
    def sessions(self, *, start: date, end: date) -> tuple[ExchangeSession, ...]:
        """Trading sessions (never weekdays) with ``start <= session <= end``."""
        ...


@dataclass(frozen=True, slots=True)
class RetainedInstrument:
    instrument_id: str
    symbol: str
    asset_class: str


@dataclass(frozen=True, slots=True)
class RetainedBar:
    instrument_id: str
    bar_id: str
    batch_id: str
    session_date: date
    available_at: datetime
    values: BarValues


class SecuritiesCoveragePort(Protocol):
    def instruments(self) -> tuple[RetainedInstrument, ...]: ...

    def bars(self, *, start: date, end: date) -> tuple[RetainedBar, ...]:
        """Every retained READY RAW daily bar in ``[start, end]``, across batches."""
        ...


@dataclass(frozen=True, slots=True)
class SecuritiesFetchOutcome:
    state: str  # COMPLETED | PARTIAL | BLOCKED_CREDENTIALS | FAILED
    provider_requests: int
    chunks_completed: int
    chunks_total: int
    new_batch_ids: tuple[str, ...] = ()
    acquired_at: datetime | None = None
    failure_code: str | None = None
    unresolved_symbols: tuple[str, ...] = ()


class SecuritiesRecoveryFetchPort(Protocol):
    def fetch(
        self,
        *,
        symbols: tuple[str, ...],
        etf_symbols: frozenset[str],
        start: date,
        end: date,
        run_key: str,
        chunk_budget: int,
        requested_at: datetime,
    ) -> SecuritiesFetchOutcome: ...


@dataclass(frozen=True, slots=True)
class RecoveryBudget:
    request_ceiling: int = 120
    chunk_budget: int = 6
    maximum_gaps: int = 400
    # Crypto candles are one Coinbase GET each; keep the recurring worker's
    # wall-clock exposure small (the worker is a 5-minute systemd oneshot).
    crypto_request_ceiling: int = 20


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    status: RecoveryRunStatus
    run: EvidenceRecoveryRun | None
    provider_requests: int
    gaps_detected: int
    gaps_recovered: int
    gaps_partial: int
    gaps_irrecoverable: int
    gaps_conflicted: int
    bars_acquired: int
    vintages_written: int
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Shared ledger helpers
# --------------------------------------------------------------------------


class _Ledger:
    """Append-only gap bookkeeping over the repository. ``detect`` writes only
    idempotent ``OPEN`` events; resolutions are built purely here and written
    atomically with their run by ``commit_recovery``."""

    def __init__(self, repository: EvidenceRecoveryRepository, recorded_at: datetime) -> None:
        self._repository = repository
        self._recorded_at = recorded_at

    def head(self, gap_key: str) -> EvidenceCoverageGapEvent | None:
        history = self._repository.gap_history(gap_key)
        return history[-1] if history else None

    def detect(
        self,
        *,
        family: EvidenceFamily,
        subject_id: str,
        interval_start: str,
        interval_end: str,
    ) -> EvidenceCoverageGapEvent:
        """Idempotently record a gap, extending an open one that grew."""

        key = gap_key_for(
            evidence_family=family, subject_id=subject_id, interval_start=interval_start
        )
        head = self.head(key)
        if head is None:
            event = EvidenceCoverageGapEvent.detect(
                evidence_family=family,
                subject_id=subject_id,
                interval_start=interval_start,
                interval_end=interval_end,
            )
            self._repository.register_gap_event(event=event, recorded_at=self._recorded_at)
            return event
        if head.disposition is GapDisposition.OPEN and interval_end > head.interval_end:
            event = head.transition(
                disposition=GapDisposition.OPEN,
                reason_codes=(),
                recovery_run_id=None,
                interval_end=interval_end,
            )
            self._repository.register_gap_event(event=event, recorded_at=self._recorded_at)
            return event
        return head

    @staticmethod
    def resolution(
        head: EvidenceCoverageGapEvent,
        *,
        disposition: GapDisposition,
        reasons: Iterable[str],
        run_id: str,
        evidence_ids: Iterable[str] = (),
    ) -> EvidenceCoverageGapEvent | None:
        """The next event for ``head``, or ``None`` when the gap is already
        terminal or already in that exact state (a late-arriving bar for an
        ``IRRECOVERABLE`` gap is therefore never a mid-write failure)."""

        if head.is_terminal:
            return None
        if head.disposition is disposition and head.reason_codes == tuple(sorted(set(reasons))):
            return None
        return head.transition(
            disposition=disposition,
            reason_codes=reasons,
            recovery_run_id=run_id,
            recovered_evidence_ids=evidence_ids,
        )

    def conflict(
        self,
        *,
        family: EvidenceFamily,
        subject_id: str,
        session: str,
        run_id: str,
    ) -> EvidenceCoverageGapEvent | None:
        """A provider revision gets its own gap identity (``subject#revision``),
        so it can never collide with the instrument's coverage gap."""

        scoped = f"{subject_id}#revision"
        key = gap_key_for(evidence_family=family, subject_id=scoped, interval_start=session)
        if self.head(key) is not None:
            return None
        return EvidenceCoverageGapEvent.detect_conflict(
            evidence_family=family,
            subject_id=scoped,
            interval_start=session,
            interval_end=session,
            recovery_run_id=run_id,
            reason_codes=(REASON_PROVIDER_REVISION_CONFLICT,),
        )


def record_irrecoverable_gaps(
    *,
    repository: EvidenceRecoveryRepository,
    family: EvidenceFamily,
    intervals: Sequence[tuple[str, str, str]],
    recorded_at: datetime,
) -> int:
    """``(subject_id, start, end)`` intervals of a live-only family, recorded
    ``IRRECOVERABLE`` with the family's explicit reason. Never attempts any
    approximation or provider request. Returns the number newly recorded."""

    if family not in IRRECOVERABLE_REASONS:
        raise ValueError("FAMILY_HAS_NO_IRRECOVERABLE_REASON")
    ledger = _Ledger(repository, recorded_at)
    written = 0
    for subject_id, start, end in intervals[:MAXIMUM_IRRECOVERABLE_GAPS_PER_RUN]:
        key = gap_key_for(evidence_family=family, subject_id=subject_id, interval_start=start)
        if ledger.head(key) is not None:
            continue
        ledger.detect(family=family, subject_id=subject_id, interval_start=start, interval_end=end)
        written += 1
    return written


# --------------------------------------------------------------------------
# Detection of live-only gaps from retained timestamps (pure helpers)
# --------------------------------------------------------------------------


def options_session_gaps(
    *,
    expected_sessions: Sequence[date],
    retained_session_ids: Iterable[str],
    window_closed: dict[date, bool],
) -> tuple[tuple[str, str, str], ...]:
    """Missing options-forward capture sessions whose capture window has
    already closed (an open window is not yet a gap)."""

    retained = {date.fromisoformat(item) for item in retained_session_ids}
    closed_expected = [day for day in expected_sessions if window_closed.get(day, False)]
    runs = group_contiguous_sessions(
        expected_sessions=closed_expected,
        missing=missing_sessions(expected_sessions=closed_expected, retained_sessions=retained),
    )
    return tuple(("OPTIONS_FORWARD", run.first.isoformat(), run.last.isoformat()) for run in runs)


def kalshi_refresh_date_gaps(
    *, retained_dates: Iterable[date], through_exclusive: date
) -> tuple[tuple[str, str, str], ...]:
    dates = sorted(set(retained_dates))
    if not dates:
        return ()
    missing = missing_utc_days(
        start=dates[0], end_exclusive=through_exclusive, retained_days=dates, first_listed=None
    )
    return tuple(
        ("KALSHI_REFRESH", first.isoformat(), last.isoformat())
        for first, last in group_contiguous_days(missing)
    )


def live_observation_gaps(
    *,
    subject_id: str,
    window_starts: Iterable[datetime],
    as_of: datetime,
    threshold: timedelta = LIVE_OBSERVATION_GAP_THRESHOLD,
    lookback: timedelta = LIVE_OBSERVATION_LOOKBACK,
) -> tuple[tuple[str, str, str], ...]:
    """Gaps between consecutive retained observation rounds within the
    lookback. Round starts are the retained facts; the gap interval is the
    exclusive span nothing was observed."""

    floor = as_of - lookback
    ordered = sorted(item for item in set(window_starts) if item >= floor)
    result: list[tuple[str, str, str]] = []
    for previous, following in pairwise(ordered):
        if following - previous > threshold:
            result.append((subject_id, previous.isoformat(), following.isoformat()))
    # The trailing span (last round to ``as_of``) is deliberately not recorded:
    # an in-progress outage has no known end, and a terminal row frozen at the
    # first detection would permanently understate it. It becomes a pairwise
    # gap once observation resumes.
    return tuple(result)


def paper_execution_gaps(
    *, closures: Iterable[tuple[str, datetime, datetime]]
) -> tuple[tuple[str, str, str], ...]:
    """``(trial_id, horizon_target_at, closed_at)``: a close later than the
    threshold after its target missed its execution window; the fact itself is
    not recoverable."""

    return tuple(
        (trial_id, target.isoformat(), closed.isoformat())
        for trial_id, target, closed in closures
        if closed - target > PAPER_CLOSE_DELAY_THRESHOLD
    )


def _replayed_summary(run: EvidenceRecoveryRun, *, gaps_detected: int) -> RecoverySummary:
    """An exact re-execution of an already-retained run: no provider request,
    no new evidence."""

    return RecoverySummary(
        status=run.status,
        run=run,
        provider_requests=0,
        gaps_detected=gaps_detected,
        gaps_recovered=run.gaps_recovered,
        gaps_partial=run.gaps_partial,
        gaps_irrecoverable=run.gaps_irrecoverable,
        gaps_conflicted=run.gaps_conflicted,
        bars_acquired=0,
        vintages_written=0,
        warnings=("EVIDENCE_RECOVERY_RUN_REPLAYED",),
    )


# --------------------------------------------------------------------------
# Securities bar recovery
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _InstrumentNeed:
    instrument: RetainedInstrument
    missing: tuple[date, ...]
    overdue: tuple[date, ...]


class RecoverSecuritiesBars:
    """Detect missing XNYS sessions per instrument, fetch them through the
    existing historical capture pipeline, reconcile, and record the ledger."""

    def __init__(
        self,
        *,
        repository: EvidenceRecoveryRepository,
        coverage: SecuritiesCoveragePort,
        calendar: ExchangeCalendarPort,
        fetcher: SecuritiesRecoveryFetchPort,
    ) -> None:
        self._repository = repository
        self._coverage = coverage
        self._calendar = calendar
        self._fetcher = fetcher

    def _terminal_sessions(self) -> dict[str, set[date]]:
        """Sessions the ledger already settled as ``IRRECOVERABLE`` per
        instrument. They are never re-requested, so a session the provider
        genuinely has no bar for cannot pin the fetch window forever."""

        settled: dict[str, set[date]] = {}
        for gap in self._repository.current_gaps(
            evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR
        ):
            if gap.disposition is not GapDisposition.IRRECOVERABLE or "#" in gap.subject_id:
                continue
            first, last = (
                date.fromisoformat(gap.interval_start),
                date.fromisoformat(gap.interval_end),
            )
            bucket = settled.setdefault(gap.subject_id, set())
            day = first
            while day <= last:
                bucket.add(day)
                day += timedelta(days=1)
        return settled

    def _needs(self, *, as_of: datetime) -> tuple[list[_InstrumentNeed], tuple[date, ...]]:
        instruments = self._coverage.instruments()
        calendar_start = as_of.date() - timedelta(days=INTERIOR_HOLE_LOOKBACK_SESSIONS * 2)
        sessions = self._calendar.sessions(start=calendar_start, end=as_of.date())
        complete = tuple(item for item in sessions if item.available_at <= as_of)
        by_date = {item.session_date: item for item in complete}
        ordered_dates = tuple(sorted(by_date))
        if not ordered_dates:
            return [], ()
        bars = self._coverage.bars(start=ordered_dates[0], end=ordered_dates[-1])
        retained: dict[str, set[date]] = {}
        for bar in bars:
            retained.setdefault(bar.instrument_id, set()).add(bar.session_date)
        settled = self._terminal_sessions()
        needs: list[_InstrumentNeed] = []
        for instrument in sorted(instruments, key=lambda item: item.instrument_id):
            have = retained.get(instrument.instrument_id)
            if not have:
                # Never retained in the lookback window: not a continuity gap.
                continue
            last = max(have)
            floor_index = max(0, ordered_dates.index(last) - INTERIOR_HOLE_LOOKBACK_SESSIONS)
            first = min(day for day in have if day >= ordered_dates[floor_index])
            expected = tuple(day for day in ordered_dates if day >= first)
            skip = settled.get(instrument.instrument_id, set())
            missing = tuple(
                day
                for day in missing_sessions(expected_sessions=expected, retained_sessions=have)
                if day not in skip
            )
            if not missing:
                continue
            overdue = tuple(
                day for day in missing if by_date[day].available_at + NOMINAL_CAPTURE_LAG < as_of
            )
            needs.append(_InstrumentNeed(instrument, missing, overdue))
        return needs, ordered_dates

    def preview(self, *, as_of: datetime) -> dict[str, object]:
        """Read-only: what a recovery pass would target. No ledger write, no
        provider request."""

        needs, _ordered = self._needs(as_of=as_of)
        return {
            "instruments_with_missing_sessions": len(needs),
            "missing_session_count": sum(len(item.missing) for item in needs),
            "overdue_session_count": sum(len(item.overdue) for item in needs),
            "first_missing_session": (
                min(day for item in needs for day in item.missing).isoformat() if needs else None
            ),
            "last_missing_session": (
                max(day for item in needs for day in item.missing).isoformat() if needs else None
            ),
            "missing_sessions": sorted({day.isoformat() for item in needs for day in item.missing}),
            "sample_symbols": sorted(item.instrument.symbol for item in needs)[:10],
        }

    def execute(
        self,
        *,
        as_of: datetime,
        executed_at: datetime,
        cycle_key: str,
        budget: RecoveryBudget | None = None,
    ) -> RecoverySummary:
        family = EvidenceFamily.SECURITIES_DAILY_BAR
        budget = budget or RecoveryBudget()
        ledger = _Ledger(self._repository, executed_at)
        needs, ordered_dates = self._needs(as_of=as_of)
        warnings: list[str] = []
        if not needs:
            return self._reconcile_without_gaps(
                ledger=ledger, family=family, cycle_key=cycle_key, executed_at=executed_at
            ) or RecoverySummary(RecoveryRunStatus.NOTHING_TO_DO, None, 0, 0, 0, 0, 0, 0, 0, 0)

        # Overdue runs are real gaps; sessions inside the nominal capture lag
        # are simply due and are fetched without being called gaps.
        detected: list[EvidenceCoverageGapEvent] = []
        for need in needs[: budget.maximum_gaps]:
            for run in group_contiguous_sessions(
                expected_sessions=ordered_dates, missing=need.overdue
            ):
                detected.append(
                    ledger.detect(
                        family=family,
                        subject_id=need.instrument.instrument_id,
                        interval_start=run.first.isoformat(),
                        interval_end=run.last.isoformat(),
                    )
                )
        window_start = min(day for need in needs for day in need.missing)
        window_end = max(day for need in needs for day in need.missing)
        scope_gap_keys = tuple(sorted(item.gap_key for item in detected))
        provisional = EvidenceRecoveryRun.create(
            evidence_family=family,
            cycle_key=cycle_key,
            scope_start=window_start.isoformat(),
            scope_end=window_end.isoformat(),
            gap_keys=scope_gap_keys,
            status=RecoveryRunStatus.COMPLETED,
            request_ceiling=budget.request_ceiling,
            provider_requests=0,
        )
        run_id = provisional.run_id
        replay = self._repository.run(run_id)
        if replay is not None:
            return _replayed_summary(replay, gaps_detected=len(detected))

        # Instruments missing the same window are fetched together, oldest
        # window first, so a short recent gap never re-downloads a long outage.
        groups: dict[tuple[date, date], list[_InstrumentNeed]] = {}
        for need in needs:
            groups.setdefault((min(need.missing), max(need.missing)), []).append(need)
        outcomes: list[SecuritiesFetchOutcome] = []
        chunks_left = budget.chunk_budget
        requests = 0
        for (first, last), members in sorted(groups.items()):
            if chunks_left <= 0:
                break
            outcome = self._fetcher.fetch(
                symbols=tuple(sorted(item.instrument.symbol for item in members)),
                etf_symbols=frozenset(
                    item.instrument.symbol
                    for item in members
                    if item.instrument.asset_class == "ETF"
                ),
                start=first,
                end=last,
                run_key=f"{cycle_key}:{first.isoformat()}:{last.isoformat()}",
                chunk_budget=chunks_left,
                requested_at=executed_at,
            )
            outcomes.append(outcome)
            requests += outcome.provider_requests
            if requests > budget.request_ceiling:
                raise ValueError("RECOVERY_PROVIDER_REQUEST_CEILING_EXCEEDED")
            chunks_left -= max(outcome.chunks_completed, 1)
            if outcome.state != "COMPLETED":
                break
        states = {item.state for item in outcomes}
        if not outcomes or "FAILED" in states:
            status = RecoveryRunStatus.FAILED
        elif "BLOCKED_CREDENTIALS" in states:
            status = RecoveryRunStatus.BLOCKED_CREDENTIALS
        elif "PARTIAL" in states or len(outcomes) < len(groups):
            status = RecoveryRunStatus.BUDGET_EXHAUSTED
        else:
            status = RecoveryRunStatus.COMPLETED
        reasons: set[str] = set()
        for outcome in outcomes:
            if outcome.failure_code:
                reasons.add(outcome.failure_code)
            if outcome.state == "BLOCKED_CREDENTIALS":
                reasons.add(REASON_BLOCKED_CREDENTIALS)
            if outcome.unresolved_symbols:
                reasons.add(
                    "INSTRUMENT_IDENTITY_UNRESOLVED:" + ",".join(outcome.unresolved_symbols)
                )
        unresolved_symbols = {symbol for item in outcomes for symbol in item.unresolved_symbols}
        new_batches = {batch for item in outcomes for batch in item.new_batch_ids}
        acquired_at = max((item.acquired_at for item in outcomes if item.acquired_at), default=None)
        any_ok = any(item.state in ("COMPLETED", "PARTIAL") for item in outcomes)

        # Re-read coverage after the fetches and reconcile each gap.
        after = self._coverage.bars(start=window_start, end=window_end)
        values_by_key: dict[tuple[str, date], list[RetainedBar]] = {}
        for bar in after:
            values_by_key.setdefault((bar.instrument_id, bar.session_date), []).append(bar)
        # A re-fetched session that disagrees with an already-retained record
        # is flagged, never overwritten (both immutable rows stay).
        conflict_keys: list[tuple[str, date]] = []
        for bar_key, candidates in sorted(values_by_key.items()):
            if not any(item.batch_id in new_batches for item in candidates):
                continue
            distinct = {item.values for item in candidates}
            if len(distinct) > 1 and any(
                provider_revision_conflict(a, b) for a in distinct for b in distinct
            ):
                conflict_keys.append(bar_key)
        conflicted_sessions = set(conflict_keys)
        if conflict_keys:
            reasons.add(REASON_PROVIDER_REVISION_CONFLICT)

        vintages: list[EvidenceVintage] = []
        events: list[EvidenceCoverageGapEvent] = []
        recovered = partial = irrecoverable = 0
        for need in needs:
            instrument_id = need.instrument.instrument_id
            fetched_sessions: set[date] = set()
            for session in need.missing:
                candidates = values_by_key.get((instrument_id, session), [])
                if not candidates:
                    continue
                fetched_sessions.add(session)
                if (instrument_id, session) in conflicted_sessions:
                    continue
                for bar in candidates:
                    if bar.batch_id not in new_batches:
                        continue
                    acquired = max(acquired_at or executed_at, bar.available_at)
                    vintages.append(
                        EvidenceVintage.create(
                            evidence_family=family,
                            evidence_id=bar.bar_id,
                            market_time=datetime.combine(session, datetime.min.time(), UTC),
                            available_at=bar.available_at,
                            acquired_at=acquired,
                            # Fixed by the acquiring mechanism, never by lag.
                            acquisition_path=AcquisitionPath.RECOVERY_BACKFILL,
                            recovery_run_id=run_id,
                        )
                    )
            for run in group_contiguous_sessions(
                expected_sessions=ordered_dates, missing=need.overdue
            ):
                key = gap_key_for(
                    evidence_family=family,
                    subject_id=instrument_id,
                    interval_start=run.first.isoformat(),
                )
                head = ledger.head(key)
                if head is None:
                    continue
                run_remaining = [day for day in run.sessions if day not in fetched_sessions]
                evidence = [
                    bar.bar_id
                    for day in run.sessions
                    for bar in values_by_key.get((instrument_id, day), [])
                ]
                provider_answered = (
                    status is RecoveryRunStatus.COMPLETED
                    and any_ok
                    and need.instrument.symbol not in unresolved_symbols
                )
                if not run_remaining:
                    steps = [(GapDisposition.RECOVERED, {REASON_RECOVERED_FROM_PROVIDER_HISTORY})]
                elif len(run_remaining) < len(run.sessions):
                    steps = [
                        (GapDisposition.PARTIALLY_RECOVERED, {REASON_PARTIAL_PROVIDER_COVERAGE})
                    ]
                    if provider_answered:
                        # The provider answered completely: what is still absent
                        # is settled, never re-requested, never approximated.
                        steps.append(
                            (GapDisposition.IRRECOVERABLE, {REASON_PROVIDER_RETURNED_NO_BAR})
                        )
                elif provider_answered:
                    steps = [(GapDisposition.IRRECOVERABLE, {REASON_PROVIDER_RETURNED_NO_BAR})]
                else:
                    continue  # nothing learned this run; the gap stays open
                current = head
                for disposition, reason in steps:
                    event = ledger.resolution(
                        current,
                        disposition=disposition,
                        reasons=reason,
                        run_id=run_id,
                        evidence_ids=evidence,
                    )
                    if event is None:
                        break
                    events.append(event)
                    current = event
                if current is head:
                    continue
                if current.disposition is GapDisposition.RECOVERED:
                    recovered += 1
                elif current.disposition is GapDisposition.PARTIALLY_RECOVERED:
                    partial += 1
                else:
                    irrecoverable += 1
        # Earlier open gaps whose sessions are no longer missing for their
        # instrument healed themselves (ordinary capture) -- no request needed.
        missing_by_instrument = {need.instrument.instrument_id: set(need.missing) for need in needs}
        resolved_keys = {item.gap_key for item in events}
        for gap in self._repository.unresolved_gaps(evidence_family=family):
            if gap.gap_key in resolved_keys or "#" in gap.subject_id:
                continue
            still_missing = missing_by_instrument.get(gap.subject_id, set())
            first, last = (
                date.fromisoformat(gap.interval_start),
                date.fromisoformat(gap.interval_end),
            )
            if any(first <= day <= last for day in still_missing):
                continue
            healed = ledger.resolution(
                gap,
                disposition=GapDisposition.RECOVERED,
                reasons=(REASON_SELF_HEALED_BY_ROLLING_WINDOW,),
                run_id=run_id,
            )
            if healed is not None:
                events.append(healed)
                recovered += 1
        conflicted = 0
        for instrument_id, session in conflict_keys:
            conflict = ledger.conflict(
                family=family,
                subject_id=instrument_id,
                session=session.isoformat(),
                run_id=run_id,
            )
            if conflict is not None:
                events.append(conflict)
                conflicted += 1

        final_run = EvidenceRecoveryRun.create(
            evidence_family=family,
            cycle_key=cycle_key,
            scope_start=window_start.isoformat(),
            scope_end=window_end.isoformat(),
            gap_keys=scope_gap_keys,
            status=status,
            request_ceiling=budget.request_ceiling,
            provider_requests=requests,
            gaps_recovered=recovered,
            gaps_partial=partial,
            gaps_irrecoverable=irrecoverable,
            gaps_conflicted=conflicted,
            reason_codes=reasons,
        )
        if final_run.run_id != run_id:
            raise ValueError("RECOVERY_RUN_IDENTITY_UNSTABLE")
        written = self._repository.commit_recovery(
            run=final_run, events=tuple(events), vintages=tuple(vintages), recorded_at=executed_at
        )
        if status is RecoveryRunStatus.BLOCKED_CREDENTIALS:
            warnings.append("SECURITIES_RECOVERY_BLOCKED_CREDENTIALS_ABSENT")
        return RecoverySummary(
            status=status,
            run=final_run,
            provider_requests=requests,
            gaps_detected=len(detected),
            gaps_recovered=recovered,
            gaps_partial=partial,
            gaps_irrecoverable=irrecoverable,
            gaps_conflicted=conflicted,
            bars_acquired=sum(1 for item in vintages),
            vintages_written=written,
            warnings=tuple(warnings),
        )

    def _reconcile_without_gaps(
        self,
        *,
        ledger: _Ledger,
        family: EvidenceFamily,
        cycle_key: str,
        executed_at: datetime,
    ) -> RecoverySummary | None:
        """No instrument is missing anything now: any still-open gap healed
        itself (for example through ordinary daily capture)."""

        unresolved = self._repository.unresolved_gaps(evidence_family=family)
        if not unresolved:
            return None
        run = EvidenceRecoveryRun.create(
            evidence_family=family,
            cycle_key=cycle_key,
            scope_start=min(item.interval_start for item in unresolved),
            scope_end=max(item.interval_end for item in unresolved),
            gap_keys=(item.gap_key for item in unresolved),
            status=RecoveryRunStatus.COMPLETED,
            request_ceiling=0,
            provider_requests=0,
            gaps_recovered=len(unresolved),
            reason_codes=(REASON_SELF_HEALED_BY_ROLLING_WINDOW,),
        )
        events = tuple(
            event
            for gap in unresolved
            if (
                event := ledger.resolution(
                    gap,
                    disposition=GapDisposition.RECOVERED,
                    reasons=(REASON_SELF_HEALED_BY_ROLLING_WINDOW,),
                    run_id=run.run_id,
                )
            )
            is not None
        )
        self._repository.commit_recovery(
            run=run, events=events, vintages=(), recorded_at=executed_at
        )
        return RecoverySummary(
            RecoveryRunStatus.COMPLETED, run, 0, 0, len(unresolved), 0, 0, 0, 0, 0
        )


# --------------------------------------------------------------------------
# Crypto daily-candle recovery
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CryptoCandleValue:
    day: date
    close: str
    volume: str


@dataclass(frozen=True, slots=True)
class CryptoProductCoverage:
    product_id: str
    first_day: date | None
    retained_days: frozenset[date]
    retained_values: dict[date, tuple[str, str]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CryptoFetchOutcome:
    state: str  # COMPLETED | FAILED
    provider_requests: int
    candles: tuple[CryptoCandleValue, ...] = ()
    observation_ids: tuple[str, ...] = ()
    failure_code: str | None = None
    acquired_at: datetime | None = None


class CryptoCoveragePort(Protocol):
    def coverage(self) -> tuple[CryptoProductCoverage, ...]:
        """Union of every retained candle day per product across all observations."""
        ...


class CryptoRecoveryFetchPort(Protocol):
    def fetch_missing(
        self,
        *,
        product_id: str,
        days: tuple[date, ...],
        run_key: str,
        requested_at: datetime,
    ) -> CryptoFetchOutcome:
        """Fetch (bounded) and durably retain the missing days as a recovery
        observation, never touching the live pulse observations."""
        ...


class RecoverCryptoCandles:
    """Daily crypto candles already self-heal through the rolling pulse window,
    so this only (1) resolves earlier gaps that healed and (2) fetches days
    still absent from every retained observation, bounded per run."""

    def __init__(
        self,
        *,
        repository: EvidenceRecoveryRepository,
        coverage: CryptoCoveragePort,
        fetcher: CryptoRecoveryFetchPort,
    ) -> None:
        self._repository = repository
        self._coverage = coverage
        self._fetcher = fetcher

    def _settled_days(self) -> dict[str, set[date]]:
        """Days already settled ``IRRECOVERABLE`` (the provider has no candle):
        never re-requested, never re-detected."""

        settled: dict[str, set[date]] = {}
        for gap in self._repository.current_gaps(
            evidence_family=EvidenceFamily.CRYPTO_DAILY_CANDLE
        ):
            if gap.disposition is not GapDisposition.IRRECOVERABLE or "#" in gap.subject_id:
                continue
            day, last = date.fromisoformat(gap.interval_start), date.fromisoformat(gap.interval_end)
            bucket = settled.setdefault(gap.subject_id, set())
            while day <= last:
                bucket.add(day)
                day += timedelta(days=1)
        return settled

    def execute(
        self,
        *,
        as_of: datetime,
        executed_at: datetime,
        cycle_key: str,
        budget: RecoveryBudget | None = None,
    ) -> RecoverySummary:
        family = EvidenceFamily.CRYPTO_DAILY_CANDLE
        budget = budget or RecoveryBudget()
        ceiling = min(budget.request_ceiling, budget.crypto_request_ceiling)
        ledger = _Ledger(self._repository, executed_at)
        today = as_of.astimezone(UTC).date()
        target = today - timedelta(days=CRYPTO_NOMINAL_LAG_DAYS)  # candles < target are overdue
        start = today - timedelta(days=CRYPTO_RECOVERY_LOOKBACK_DAYS)
        settled = self._settled_days()
        missing_by_product: dict[str, tuple[date, ...]] = {}
        coverage_by_product = {item.product_id: item for item in self._coverage.coverage()}
        for product_id, item in sorted(coverage_by_product.items()):
            if not item.retained_days:
                continue
            days = tuple(
                day
                for day in missing_utc_days(
                    start=start,
                    end_exclusive=target,
                    retained_days=item.retained_days,
                    first_listed=item.first_day,
                )
                if day not in settled.get(product_id, set())
            )
            if days:
                missing_by_product[product_id] = days

        detected: list[EvidenceCoverageGapEvent] = []
        for product_id, days in missing_by_product.items():
            for first, last in group_contiguous_days(days):
                detected.append(
                    ledger.detect(
                        family=family,
                        subject_id=product_id,
                        interval_start=first.isoformat(),
                        interval_end=last.isoformat(),
                    )
                )
        # Earlier gaps no longer missing anywhere healed through the rolling
        # window (or left the active universe, which makes them out of scope).
        open_now = {item.gap_key for item in detected}
        healed = [
            gap
            for gap in self._repository.unresolved_gaps(evidence_family=family)
            if gap.gap_key not in open_now and "#" not in gap.subject_id
        ]
        if not detected and not healed:
            return RecoverySummary(RecoveryRunStatus.NOTHING_TO_DO, None, 0, 0, 0, 0, 0, 0, 0, 0)

        everything = detected + healed
        gap_keys = tuple(item.gap_key for item in everything)
        run_scope = (
            min(item.interval_start for item in everything),
            max(item.interval_end for item in everything),
        )
        provisional = EvidenceRecoveryRun.create(
            evidence_family=family,
            cycle_key=cycle_key,
            scope_start=run_scope[0],
            scope_end=run_scope[1],
            gap_keys=gap_keys,
            status=RecoveryRunStatus.COMPLETED,
            request_ceiling=ceiling,
            provider_requests=0,
        )
        run_id = provisional.run_id
        replay = self._repository.run(run_id)
        if replay is not None:
            return _replayed_summary(replay, gaps_detected=len(detected))

        requests = 0
        recovered = partial = irrecoverable = conflicted = 0
        reasons: set[str] = {REASON_SELF_HEALED_BY_ROLLING_WINDOW} if healed else set()
        events: list[EvidenceCoverageGapEvent] = []
        for gap in healed:
            event = ledger.resolution(
                gap,
                disposition=GapDisposition.RECOVERED,
                reasons=(REASON_SELF_HEALED_BY_ROLLING_WINDOW,),
                run_id=run_id,
            )
            if event is not None:
                events.append(event)
                recovered += 1
        vintages: list[EvidenceVintage] = []
        exhausted = False
        failed = False
        for gap in sorted(detected, key=lambda item: (item.interval_start, item.subject_id)):
            if requests >= ceiling:
                exhausted = True
                break
            first = date.fromisoformat(gap.interval_start)
            last = date.fromisoformat(gap.interval_end)
            days = tuple(first + timedelta(days=n) for n in range((last - first).days + 1))
            outcome = self._fetcher.fetch_missing(
                product_id=gap.subject_id,
                days=days,
                run_key=f"{cycle_key}:{gap.gap_key}",
                requested_at=executed_at,
            )
            requests += outcome.provider_requests
            if requests > ceiling:
                raise ValueError("RECOVERY_PROVIDER_REQUEST_CEILING_EXCEEDED")
            if outcome.state != "COMPLETED":
                failed = True
                if outcome.failure_code:
                    reasons.add(outcome.failure_code)
                continue
            got = {item.day: item for item in outcome.candles}
            retained = coverage_by_product[gap.subject_id].retained_values
            # A returned candle that disagrees with an already-retained one is
            # flagged under its own gap identity, never overwritten.
            for day, candle in sorted(got.items()):
                existing = retained.get(day)
                if existing is not None and (candle.close, candle.volume) != existing:
                    conflict = ledger.conflict(
                        family=family,
                        subject_id=gap.subject_id,
                        session=day.isoformat(),
                        run_id=run_id,
                    )
                    if conflict is not None:
                        events.append(conflict)
                        conflicted += 1
                        reasons.add(REASON_PROVIDER_REVISION_CONFLICT)
            returned = [day for day in days if day in got]
            for day in returned:
                market_time = datetime.combine(day, datetime.min.time(), UTC)
                acquired = max(outcome.acquired_at or executed_at, market_time + timedelta(days=1))
                vintages.append(
                    EvidenceVintage.create(
                        evidence_family=family,
                        evidence_id=f"{gap.subject_id}:{day.isoformat()}",
                        market_time=market_time,
                        available_at=market_time + timedelta(days=1),
                        acquired_at=acquired,
                        acquisition_path=AcquisitionPath.RECOVERY_BACKFILL,
                        recovery_run_id=run_id,
                    )
                )
            if len(returned) == len(days):
                disposition = GapDisposition.RECOVERED
                reason = {REASON_RECOVERED_FROM_PROVIDER_HISTORY}
                recovered += 1
            elif returned:
                disposition = GapDisposition.PARTIALLY_RECOVERED
                reason = {REASON_PARTIAL_PROVIDER_COVERAGE}
                partial += 1
            else:
                disposition = GapDisposition.IRRECOVERABLE
                reason = {REASON_PROVIDER_RETURNED_NO_BAR}
                irrecoverable += 1
            resolved = ledger.resolution(
                gap,
                disposition=disposition,
                reasons=reason,
                run_id=run_id,
                evidence_ids=outcome.observation_ids,
            )
            if resolved is not None:
                events.append(resolved)

        status = (
            RecoveryRunStatus.BUDGET_EXHAUSTED
            if exhausted
            else RecoveryRunStatus.FAILED
            if failed and not (recovered or partial or irrecoverable)
            else RecoveryRunStatus.COMPLETED
        )
        final = EvidenceRecoveryRun.create(
            evidence_family=family,
            cycle_key=cycle_key,
            scope_start=run_scope[0],
            scope_end=run_scope[1],
            gap_keys=gap_keys,
            status=status,
            request_ceiling=ceiling,
            provider_requests=requests,
            gaps_recovered=recovered,
            gaps_partial=partial,
            gaps_irrecoverable=irrecoverable,
            gaps_conflicted=conflicted,
            reason_codes=reasons,
        )
        if final.run_id != run_id:
            raise ValueError("RECOVERY_RUN_IDENTITY_UNSTABLE")
        written = self._repository.commit_recovery(
            run=final, events=tuple(events), vintages=tuple(vintages), recorded_at=executed_at
        )
        return RecoverySummary(
            status,
            final,
            requests,
            len(detected),
            recovered,
            partial,
            irrecoverable,
            conflicted,
            len(vintages),
            written,
        )
