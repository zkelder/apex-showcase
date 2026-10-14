"""Offline proofs for Historical Evidence Gap Recovery v1.

Domain rules, the gap ledger state machine, and the bounded recovery use cases
against in-memory ports and the real reviewed XNYS calendar.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apex.adapters.evidence_recovery_sources import XnysExchangeCalendar
from apex.application.evidence_recovery import (
    CryptoCandleValue,
    CryptoFetchOutcome,
    CryptoProductCoverage,
    RecoverCryptoCandles,
    RecoverSecuritiesBars,
    RecoveryBudget,
    RetainedBar,
    RetainedInstrument,
    SecuritiesFetchOutcome,
    kalshi_refresh_date_gaps,
    live_observation_gaps,
    options_session_gaps,
    paper_execution_gaps,
    record_irrecoverable_gaps,
)
from apex.domain.evidence_recovery import (
    LIVE_ONLY_FAMILIES,
    RECOVERABLE_FAMILIES,
    AcquisitionPath,
    BarValues,
    EvidenceCoverageGapEvent,
    EvidenceFamily,
    EvidenceRecoveryError,
    EvidenceRecoveryRun,
    EvidenceVintage,
    GapDisposition,
    RecoveryRunStatus,
    VintageTimeliness,
    assert_backdated_execution_allowed,
    assert_live_evidence_admissible,
    build_acquisition_path_correction,
    classify_vintage_timeliness,
    gap_key_for,
    group_contiguous_sessions,
    missing_sessions,
    verify_evidence_gap_event,
    verify_evidence_recovery_run,
    verify_evidence_vintage,
)


class _CachedCalendar(XnysExchangeCalendar):
    """The real reviewed XNYS calendar, memoized: building it is the slow part."""

    def __init__(self) -> None:
        self._cache: dict[tuple[date, date], tuple[object, ...]] = {}

    def sessions(self, *, start: date, end: date):  # type: ignore[no-untyped-def]
        key = (start, end)
        if key not in self._cache:
            self._cache[key] = super().sessions(start=start, end=end)
        return self._cache[key]


CALENDAR = _CachedCalendar()
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "apex"


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _values(seed: int) -> BarValues:
    base = Decimal(100 + seed)
    return BarValues(
        open=base, high=base + 2, low=base - 2, close=base + 1, volume=Decimal(1000 + seed)
    )


class MemoryRepository:
    """In-memory ``EvidenceRecoveryRepository`` with the same append-only and
    state-machine behaviour the PostgreSQL adapter enforces."""

    def __init__(self) -> None:
        self.runs: dict[str, EvidenceRecoveryRun] = {}
        self.events: dict[str, list[EvidenceCoverageGapEvent]] = {}
        self.vintages: dict[str, EvidenceVintage] = {}

    def register_run(self, *, run: EvidenceRecoveryRun, recorded_at: datetime) -> bool:
        existing = self.runs.get(run.run_id)
        if existing is not None:
            if existing.canonical_hash != run.canonical_hash:
                raise EvidenceRecoveryError("EVIDENCE_RECOVERY_RUN_IDENTITY_CONFLICT")
            return True
        self.runs[run.run_id] = run
        return False

    def run(self, run_id: str) -> EvidenceRecoveryRun | None:
        return self.runs.get(run_id)

    def register_gap_event(self, *, event: EvidenceCoverageGapEvent, recorded_at: datetime) -> bool:
        history = self.events.setdefault(event.gap_key, [])
        for prior in history:
            if prior.ordinal == event.ordinal:
                if prior.canonical_hash != event.canonical_hash:
                    raise EvidenceRecoveryError("EVIDENCE_GAP_EVENT_IDENTITY_CONFLICT")
                return True
        if event.ordinal != len(history) + 1:
            raise EvidenceRecoveryError("EVIDENCE_GAP_EVENT_OUT_OF_SEQUENCE")
        if history:
            history[-1].transition(
                disposition=event.disposition,
                reason_codes=event.reason_codes,
                recovery_run_id=event.recovery_run_id,
                recovered_evidence_ids=event.recovered_evidence_ids,
                interval_end=event.interval_end,
            )
        history.append(event)
        return False

    def gap_history(self, gap_key: str) -> tuple[EvidenceCoverageGapEvent, ...]:
        return tuple(self.events.get(gap_key, ()))

    def current_gaps(
        self, *, evidence_family: EvidenceFamily | None = None
    ) -> tuple[EvidenceCoverageGapEvent, ...]:
        return tuple(self.current(evidence_family))

    def commit_recovery(
        self,
        *,
        run: EvidenceRecoveryRun,
        events: tuple[EvidenceCoverageGapEvent, ...],
        vintages: tuple[EvidenceVintage, ...],
        recorded_at: datetime,
    ) -> int:
        self.register_run(run=run, recorded_at=recorded_at)
        for event in events:
            self.register_gap_event(event=event, recorded_at=recorded_at)
        return self.register_vintages(vintages=vintages, recorded_at=recorded_at)

    def current(self, family: EvidenceFamily | None = None) -> list[EvidenceCoverageGapEvent]:
        heads = [history[-1] for history in self.events.values()]
        return sorted(
            (item for item in heads if family is None or item.evidence_family is family),
            key=lambda item: (item.interval_start, item.subject_id),
        )

    def unresolved_gaps(
        self, *, evidence_family: EvidenceFamily
    ) -> tuple[EvidenceCoverageGapEvent, ...]:
        return tuple(
            item
            for item in self.current(evidence_family)
            if item.disposition in (GapDisposition.OPEN, GapDisposition.PARTIALLY_RECOVERED)
        )

    def register_vintages(
        self, *, vintages: tuple[EvidenceVintage, ...], recorded_at: datetime
    ) -> int:
        written = 0
        for item in vintages:
            if item.vintage_id not in self.vintages:
                self.vintages[item.vintage_id] = item
                written += 1
        return written


@dataclass
class World:
    """A fake securities universe: retained bars, provider history, and the
    fetch port that lands provider bars as a new retained batch."""

    instruments: list[RetainedInstrument]
    bars: list[RetainedBar] = field(default_factory=list)
    provider: dict[tuple[str, date], BarValues] = field(default_factory=dict)
    fetch_calls: list[dict[str, object]] = field(default_factory=list)
    state: str = "COMPLETED"
    requests_per_fetch: int = 2

    def add_retained(
        self, instrument_id: str, session: date, values: BarValues, batch: str
    ) -> None:
        sessions = {
            item.session_date: item for item in CALENDAR.sessions(start=session, end=session)
        }
        self.bars.append(
            RetainedBar(
                instrument_id=instrument_id,
                bar_id=f"bar-{instrument_id}-{session.isoformat()}-{batch}",
                batch_id=batch,
                session_date=session,
                available_at=sessions[session].available_at,
                values=values,
            )
        )

    # SecuritiesCoveragePort
    def bars_between(self, *, start: date, end: date) -> tuple[RetainedBar, ...]:
        return tuple(item for item in self.bars if start <= item.session_date <= end)

    # The coverage port adapter below calls these through composition.
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
    ) -> SecuritiesFetchOutcome:
        self.fetch_calls.append(
            {"symbols": symbols, "start": start, "end": end, "run_key": run_key}
        )
        if self.state != "COMPLETED":
            return SecuritiesFetchOutcome(
                self.state,
                0,
                0,
                1,
                failure_code="ALPACA_CREDENTIALS_REQUIRED"
                if self.state == "BLOCKED_CREDENTIALS"
                else "BOOM",
            )
        batch = f"batch-{len(self.fetch_calls)}"
        by_symbol = {item.symbol: item for item in self.instruments}
        for session in CALENDAR.sessions(start=start, end=end):
            for symbol in symbols:
                instrument = by_symbol[symbol]
                key = (instrument.instrument_id, session.session_date)
                if key in self.provider:
                    self.bars.append(
                        RetainedBar(
                            instrument_id=instrument.instrument_id,
                            bar_id=f"bar-{instrument.instrument_id}-{session.session_date}-{batch}",
                            batch_id=batch,
                            session_date=session.session_date,
                            available_at=session.available_at,
                            values=self.provider[key],
                        )
                    )
        return SecuritiesFetchOutcome(
            "COMPLETED",
            self.requests_per_fetch,
            1,
            1,
            new_batch_ids=(batch,),
            acquired_at=requested_at,
        )


class CoverageAdapter:
    def __init__(self, world: World) -> None:
        self._world = world

    def instruments(self) -> tuple[RetainedInstrument, ...]:
        return tuple(self._world.instruments)

    def bars(self, *, start: date, end: date) -> tuple[RetainedBar, ...]:
        return self._world.bars_between(start=start, end=end)


def _world(
    *,
    last_retained: date = date(2026, 8, 6),
    instruments: int = 3,
    provider_through: date = date(2026, 10, 2),
) -> World:
    items = [RetainedInstrument(f"inst-{n}", f"SYM{n}", "ETF") for n in range(instruments)]
    world = World(instruments=items)
    history = CALENDAR.sessions(start=date(2026, 6, 1), end=last_retained)
    for position, instrument in enumerate(items):
        for session in history:
            world.add_retained(
                instrument.instrument_id, session.session_date, _values(position), "batch-seed"
            )
    for position, instrument in enumerate(items):
        for session in CALENDAR.sessions(
            start=last_retained + timedelta(days=1), end=provider_through
        ):
            world.provider[(instrument.instrument_id, session.session_date)] = _values(
                position + 50
            )
    return world


def _recoverer(world: World, repository: MemoryRepository) -> RecoverSecuritiesBars:
    return RecoverSecuritiesBars(
        repository=repository,
        coverage=CoverageAdapter(world),
        calendar=CALENDAR,
        fetcher=world,
    )


# ---------------------------------------------------------------------------
# Domain: vintage, runs, ledger state machine
# ---------------------------------------------------------------------------


def test_vintage_distinguishes_live_from_recovered_and_never_rewrites_history() -> None:
    market = _utc(2026, 9, 10, 13, 30)
    available = _utc(2026, 9, 10, 20, 30)
    live = EvidenceVintage.create(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        evidence_id="bar-1",
        market_time=market,
        available_at=available,
        acquired_at=available + timedelta(hours=4),
        acquisition_path=AcquisitionPath.SCHEDULED_LIVE,
    )
    recovered = EvidenceVintage.create(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        evidence_id="bar-1",
        market_time=market,
        available_at=available,
        acquired_at=_utc(2026, 10, 2),
        acquisition_path=AcquisitionPath.RECOVERY_BACKFILL,
        recovery_run_id="run-1",
    )
    assert live.vintage_id != recovered.vintage_id
    assert live.lag_seconds < recovered.lag_seconds
    assert verify_evidence_vintage(recovered) == recovered
    with pytest.raises(EvidenceRecoveryError, match="ACQUIRED_BEFORE_AVAILABLE"):
        EvidenceVintage.create(
            evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
            evidence_id="bar-2",
            market_time=market,
            available_at=available,
            acquired_at=market + timedelta(hours=1),
            acquisition_path=AcquisitionPath.SCHEDULED_LIVE,
        )
    with pytest.raises(EvidenceRecoveryError, match="UNKNOWN_LEGACY"):
        EvidenceVintage.create(
            evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
            evidence_id="bar-3",
            market_time=market,
            available_at=available,
            acquired_at=available,
            acquisition_path=AcquisitionPath.UNKNOWN_LEGACY,
        )


def _bar_vintage(path: AcquisitionPath, lag: timedelta, run: str | None) -> EvidenceVintage:
    available = _utc(2026, 9, 10, 20, 30)
    return EvidenceVintage.create(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        evidence_id="bar-x",
        market_time=_utc(2026, 9, 10, 13, 30),
        available_at=available,
        acquired_at=available + lag,
        acquisition_path=path,
        recovery_run_id=run,
    )


@pytest.mark.parametrize("lag", [timedelta(hours=1), timedelta(hours=24), timedelta(days=10)])
def test_recovery_path_is_independent_of_lateness(lag: timedelta) -> None:
    vintage = _bar_vintage(AcquisitionPath.RECOVERY_BACKFILL, lag, "run-1")
    assert vintage.acquisition_path is AcquisitionPath.RECOVERY_BACKFILL
    assert vintage.canonical_acquisition_path is AcquisitionPath.RECOVERY_BACKFILL
    expected = VintageTimeliness.NOMINAL if lag <= timedelta(hours=36) else VintageTimeliness.LATE
    assert vintage.timeliness is expected
    # Daily bars never satisfy live freshness, however promptly recovered.
    assert not vintage.may_satisfy_live_freshness(
        decision_time=vintage.acquired_at + timedelta(seconds=1), maximum_age=timedelta(days=30)
    )


def test_scheduled_and_operator_paths_are_explicit_and_timeliness_varies_independently() -> None:
    for path in (AcquisitionPath.SCHEDULED_LIVE, AcquisitionPath.OPERATOR_IMPORT):
        for lag in (timedelta(hours=1), timedelta(days=10)):
            vintage = _bar_vintage(path, lag, None)
            assert vintage.acquisition_path is path
            assert vintage.canonical_acquisition_path is path
    available = _utc(2026, 9, 10, 20, 30)
    assert (
        classify_vintage_timeliness(available_at=available, acquired_at=available)
        is VintageTimeliness.NOMINAL
    )
    assert (
        classify_vintage_timeliness(
            available_at=available, acquired_at=available + timedelta(days=5)
        )
        is VintageTimeliness.LATE
    )


def test_legacy_mislabelled_row_is_canonically_recovery_and_correction_is_exact() -> None:
    legacy = _bar_vintage(AcquisitionPath.SCHEDULED_LIVE, timedelta(hours=4), "run-1")
    assert legacy.canonical_acquisition_path is AcquisitionPath.RECOVERY_BACKFILL
    assert not legacy.may_satisfy_live_freshness(
        decision_time=legacy.acquired_at, maximum_age=timedelta(days=30)
    )
    with pytest.raises(EvidenceRecoveryError, match="CANNOT_ENTER_LIVE_ONLY_TABLE"):
        assert_live_evidence_admissible(legacy)
    fixed = build_acquisition_path_correction(legacy)
    assert fixed.acquisition_path is AcquisitionPath.RECOVERY_BACKFILL
    assert fixed.vintage_id != legacy.vintage_id
    assert (fixed.market_time, fixed.available_at, fixed.acquired_at, fixed.recovery_run_id) == (
        legacy.market_time,
        legacy.available_at,
        legacy.acquired_at,
        legacy.recovery_run_id,
    )
    assert build_acquisition_path_correction(legacy) == fixed
    for unrelated in (
        _bar_vintage(AcquisitionPath.SCHEDULED_LIVE, timedelta(hours=4), None),
        _bar_vintage(AcquisitionPath.RECOVERY_BACKFILL, timedelta(hours=4), "run-1"),
        _bar_vintage(AcquisitionPath.OPERATOR_IMPORT, timedelta(hours=4), None),
    ):
        with pytest.raises(EvidenceRecoveryError, match="CORRECTION_NOT_APPLICABLE"):
            build_acquisition_path_correction(unrelated)


@pytest.mark.parametrize("family", sorted(LIVE_ONLY_FAMILIES, key=str))
def test_recovered_vintage_is_refused_for_every_live_only_family(family: EvidenceFamily) -> None:
    """Test 6: recovered evidence cannot be recorded for quote/depth/option/live/PAPER."""

    with pytest.raises(EvidenceRecoveryError, match="LIVE_ONLY_FAMILY"):
        EvidenceVintage.create(
            evidence_family=family,
            evidence_id="x",
            market_time=_utc(2026, 9, 10),
            available_at=None,
            acquired_at=_utc(2026, 10, 1),
            acquisition_path=AcquisitionPath.RECOVERY_BACKFILL,
            recovery_run_id="run",
        )


def test_recovered_evidence_never_satisfies_live_freshness() -> None:
    """Test 7."""

    for family in EvidenceFamily:
        recovered_ok = family in RECOVERABLE_FAMILIES
        if not recovered_ok:
            continue
        vintage = EvidenceVintage.create(
            evidence_family=family,
            evidence_id="c",
            market_time=_utc(2026, 9, 10),
            available_at=_utc(2026, 9, 11),
            acquired_at=_utc(2026, 9, 11, 0, 1),
            acquisition_path=AcquisitionPath.RECOVERY_BACKFILL,
            recovery_run_id="run",
        )
        assert not vintage.may_satisfy_live_freshness(
            decision_time=_utc(2026, 9, 11, 0, 2), maximum_age=timedelta(days=30)
        )
        with pytest.raises(EvidenceRecoveryError, match="CANNOT_ENTER_LIVE_ONLY_TABLE"):
            assert_live_evidence_admissible(vintage)
    assert_live_evidence_admissible(None)
    live = EvidenceVintage.create(
        evidence_family=EvidenceFamily.CRYPTO_ORDER_BOOK,
        evidence_id="book",
        market_time=_utc(2026, 9, 10),
        available_at=None,
        acquired_at=_utc(
            2026,
            9,
            10,
            0,
            0,
        )
        + timedelta(seconds=5),
        acquisition_path=AcquisitionPath.SCHEDULED_LIVE,
    )
    assert live.may_satisfy_live_freshness(
        decision_time=_utc(2026, 9, 10, 0, 1), maximum_age=timedelta(minutes=5)
    )


def test_gap_state_machine_allows_only_forward_transitions() -> None:
    """Test 13."""

    base = EvidenceCoverageGapEvent.detect(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        subject_id="inst",
        interval_start="2026-08-07",
        interval_end="2026-08-10",
    )
    assert base.disposition is GapDisposition.OPEN and base.ordinal == 1
    partial = base.transition(
        disposition=GapDisposition.PARTIALLY_RECOVERED,
        reason_codes=("PARTIAL_PROVIDER_COVERAGE",),
        recovery_run_id="run-1",
    )
    done = partial.transition(
        disposition=GapDisposition.RECOVERED,
        reason_codes=("RECOVERED_FROM_PROVIDER_HISTORY",),
        recovery_run_id="run-2",
    )
    assert [base.ordinal, partial.ordinal, done.ordinal] == [1, 2, 3]
    for terminal in (done,):
        with pytest.raises(EvidenceRecoveryError, match="TRANSITION_INVALID"):
            terminal.transition(
                disposition=GapDisposition.OPEN,
                reason_codes=(),
                recovery_run_id=None,
                interval_end="2026-08-20",
            )
    irrecoverable = base.transition(
        disposition=GapDisposition.IRRECOVERABLE,
        reason_codes=("PROVIDER_RETURNED_NO_BAR_FOR_SESSION",),
        recovery_run_id="run-1",
    )
    with pytest.raises(EvidenceRecoveryError, match="TRANSITION_INVALID"):
        irrecoverable.transition(
            disposition=GapDisposition.RECOVERED, reason_codes=("x",), recovery_run_id="r"
        )
    with pytest.raises(EvidenceRecoveryError, match="REQUIRES_EXPLICIT_REASON"):
        base.transition(
            disposition=GapDisposition.IRRECOVERABLE, reason_codes=(), recovery_run_id="r"
        )
    with pytest.raises(EvidenceRecoveryError, match="REQUIRES_RECOVERY_RUN"):
        base.transition(
            disposition=GapDisposition.RECOVERED, reason_codes=("x",), recovery_run_id=None
        )
    with pytest.raises(EvidenceRecoveryError, match="MUST_GROW"):
        base.transition(
            disposition=GapDisposition.OPEN,
            reason_codes=(),
            recovery_run_id=None,
            interval_end="2026-08-10",
        )
    grown = base.transition(
        disposition=GapDisposition.OPEN,
        reason_codes=(),
        recovery_run_id=None,
        interval_end="2026-08-12",
    )
    assert grown.interval_end == "2026-08-12" and grown.ordinal == 2
    assert verify_evidence_gap_event(grown) == grown


@pytest.mark.parametrize("family", sorted(LIVE_ONLY_FAMILIES, key=str))
def test_live_only_gap_is_immediately_irrecoverable_with_explicit_reason(
    family: EvidenceFamily,
) -> None:
    """Test 5 (option chain, order book, live observation, Kalshi, PAPER fact)."""

    event = EvidenceCoverageGapEvent.detect(
        evidence_family=family,
        subject_id="s",
        interval_start="2026-09-08",
        interval_end="2026-09-18",
    )
    assert event.disposition is GapDisposition.IRRECOVERABLE
    assert event.reason_codes
    with pytest.raises(EvidenceRecoveryError):
        event.transition(
            disposition=GapDisposition.RECOVERED, reason_codes=("x",), recovery_run_id="r"
        )


def test_option_chain_gap_reason_and_paper_execution_fact_reason() -> None:
    repository = MemoryRepository()
    written = record_irrecoverable_gaps(
        repository=repository,
        family=EvidenceFamily.OPTIONS_CHAIN_SESSION,
        intervals=(("OPTIONS_FORWARD", "2026-09-08", "2026-09-18"),),
        recorded_at=_utc(2026, 10, 2),
    )
    assert written == 1
    head = repository.current(EvidenceFamily.OPTIONS_CHAIN_SESSION)[0]
    assert head.disposition is GapDisposition.IRRECOVERABLE
    assert head.reason_codes == ("OPTION_CHAIN_NOT_RECONSTRUCTIBLE",)
    # Idempotent.
    assert (
        record_irrecoverable_gaps(
            repository=repository,
            family=EvidenceFamily.OPTIONS_CHAIN_SESSION,
            intervals=(("OPTIONS_FORWARD", "2026-09-08", "2026-09-18"),),
            recorded_at=_utc(2026, 10, 3),
        )
        == 0
    )
    execution = paper_execution_gaps(
        closures=(
            ("trial-late", _utc(2026, 9, 29), _utc(2026, 9, 30, 0, 6)),
            ("trial-on-time", _utc(2026, 9, 29), _utc(2026, 9, 29, 0, 6)),
        )
    )
    assert [item[0] for item in execution] == ["trial-late"]
    record_irrecoverable_gaps(
        repository=repository,
        family=EvidenceFamily.PAPER_EXECUTION_FACT,
        intervals=execution,
        recorded_at=_utc(2026, 10, 2),
    )
    paper = repository.current(EvidenceFamily.PAPER_EXECUTION_FACT)[0]
    assert paper.reason_codes == ("EXECUTION_FACT",)


def test_run_identity_is_deterministic_and_authority_none() -> None:
    """Test 12 and 16."""

    def make(cycle: str = "cycle-1", keys: tuple[str, ...] = ("a", "b")) -> EvidenceRecoveryRun:
        return EvidenceRecoveryRun.create(
            evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
            cycle_key=cycle,
            scope_start="2026-08-07",
            scope_end="2026-10-01",
            gap_keys=keys,
            status=RecoveryRunStatus.COMPLETED,
            request_ceiling=10,
            provider_requests=2,
        )

    first, second = make(), make(keys=("b", "a"))
    assert first.run_id == second.run_id and first.canonical_hash == second.canonical_hash
    assert make(cycle="cycle-2").run_id != first.run_id
    assert first.authority == "NONE"
    assert verify_evidence_recovery_run(first) == first
    with pytest.raises(EvidenceRecoveryError):
        verify_evidence_recovery_run(replace(first, authority="LIVE"))
    with pytest.raises(EvidenceRecoveryError, match="CEILING_EXCEEDED"):
        EvidenceRecoveryRun.create(
            evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
            cycle_key="c",
            scope_start="a",
            scope_end="b",
            gap_keys=(),
            status=RecoveryRunStatus.COMPLETED,
            request_ceiling=1,
            provider_requests=2,
        )
    with pytest.raises(EvidenceRecoveryError, match="NOT_RECOVERABLE"):
        EvidenceRecoveryRun.create(
            evidence_family=EvidenceFamily.OPTIONS_CHAIN_SESSION,
            cycle_key="c",
            scope_start="a",
            scope_end="b",
            gap_keys=(),
            status=RecoveryRunStatus.COMPLETED,
            request_ceiling=1,
            provider_requests=0,
        )


def test_backdated_execution_guard() -> None:
    now = _utc(2026, 10, 2, 0, 5)
    assert_backdated_execution_allowed(
        as_of=_utc(2026, 10, 2), executed_at=now, cycle_already_retained=False
    )
    with pytest.raises(EvidenceRecoveryError, match="BACKDATED_EXECUTION_REFUSED"):
        assert_backdated_execution_allowed(
            as_of=_utc(2026, 9, 10), executed_at=now, cycle_already_retained=False
        )
    with pytest.raises(EvidenceRecoveryError, match="IN_FUTURE"):
        assert_backdated_execution_allowed(
            as_of=_utc(2026, 10, 3), executed_at=now, cycle_already_retained=False
        )
    # Replaying an already retained cycle stays exempt.
    assert_backdated_execution_allowed(
        as_of=_utc(2026, 9, 10), executed_at=now, cycle_already_retained=True
    )


# ---------------------------------------------------------------------------
# Calendar: XNYS sessions, not weekdays
# ---------------------------------------------------------------------------


def test_xnys_holiday_and_weekend_handling() -> None:
    """Test 9."""

    sessions = [
        item.session_date
        for item in CALENDAR.sessions(start=date(2026, 9, 3), end=date(2026, 9, 9))
    ]
    # Thu 3, Fri 4, [Sat 5, Sun 6, Mon 7 Labor Day], Tue 8, Wed 9.
    assert sessions == [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9)]
    runs = group_contiguous_sessions(
        expected_sessions=sessions, missing=[date(2026, 9, 4), date(2026, 9, 8)]
    )
    assert len(runs) == 1, "a weekend/holiday never splits a run of missing sessions"
    assert missing_sessions(expected_sessions=sessions, retained_sessions=[date(2026, 9, 3)]) == (
        date(2026, 9, 4),
        date(2026, 9, 8),
        date(2026, 9, 9),
    )
    with pytest.raises(EvidenceRecoveryError, match="NOT_IN_EXCHANGE_CALENDAR"):
        group_contiguous_sessions(expected_sessions=sessions, missing=[date(2026, 9, 7)])
    early = CALENDAR.sessions(start=date(2026, 11, 27), end=date(2026, 11, 27))[0]
    assert early.closes_at.hour == 18, "the day after Thanksgiving is an early close"


# ---------------------------------------------------------------------------
# Securities recovery
# ---------------------------------------------------------------------------


def test_missing_securities_sessions_are_detected_by_xnys_sessions() -> None:
    """Test 1."""

    world = _world()
    preview = _recoverer(world, MemoryRepository()).preview(as_of=_utc(2026, 8, 12, 12))
    assert preview["missing_sessions"] == ["2026-08-07", "2026-08-10", "2026-08-11"]
    assert preview["instruments_with_missing_sessions"] == 3


def test_september_gap_production_shaped_example() -> None:
    """Test 17 and 10: a multi-week workstation outage on a production-shaped universe."""

    world = _world(instruments=4)
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 2)
    summary = _recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of + timedelta(minutes=5), cycle_key="cycle-20261002"
    )
    assert summary.status is RecoveryRunStatus.COMPLETED
    preview_sessions = {
        item.session_date
        for item in CALENDAR.sessions(start=date(2026, 8, 7), end=date(2026, 10, 1))
    }
    assert date(2026, 9, 7) not in preview_sessions  # Labor Day
    assert summary.bars_acquired == 4 * len(preview_sessions)
    gaps = repository.current(EvidenceFamily.SECURITIES_DAILY_BAR)
    assert gaps and all(item.disposition is GapDisposition.RECOVERED for item in gaps)
    # The long outage was one gap per instrument, from the first missed session.
    assert {item.interval_start for item in gaps} == {"2026-08-07"}
    paths = {item.acquisition_path for item in repository.vintages.values()}
    assert AcquisitionPath.RECOVERY_BACKFILL in paths
    # Every recovery-fetched bar is RECOVERY_BACKFILL; the two sessions inside
    # the nominal capture lag are merely NOMINAL in timeliness.
    assert paths == {AcquisitionPath.RECOVERY_BACKFILL}
    nominal = [
        item
        for item in repository.vintages.values()
        if item.timeliness is VintageTimeliness.NOMINAL
    ]
    assert nominal
    assert all(item.lag_seconds <= 36 * 3600 for item in nominal)


def test_bar_recovered_exactly_and_second_recovery_is_idempotent() -> None:
    """Tests 2 and 3."""

    world = _world(instruments=2)
    repository = MemoryRepository()
    as_of = _utc(2026, 9, 1)
    first = _recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of, cycle_key="cycle-a"
    )
    recovered = {
        (bar.instrument_id, bar.session_date): bar.values
        for bar in world.bars
        if bar.batch_id.startswith("batch-") and bar.batch_id != "batch-seed"
    }
    assert recovered
    for key, values in recovered.items():
        assert values == world.provider[key], "recovered exactly as the provider reported"
    assert first.run is not None and first.provider_requests == 2
    calls, runs, vintages = len(world.fetch_calls), len(repository.runs), len(repository.vintages)
    second = _recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of + timedelta(minutes=5), cycle_key="cycle-a"
    )
    assert second.status is RecoveryRunStatus.NOTHING_TO_DO or second.vintages_written == 0
    assert len(world.fetch_calls) == calls, "no second provider request"
    assert len(repository.runs) == runs and len(repository.vintages) == vintages


def test_exact_run_replay_makes_no_request() -> None:
    """The same cycle and scope replays the retained run without a request."""

    world = _world(instruments=1)
    repository = MemoryRepository()
    as_of = _utc(2026, 9, 1)
    service = _recoverer(world, repository)
    first = service.execute(as_of=as_of, executed_at=as_of, cycle_key="cycle-r")
    # Simulate a crashed retry that lost the retained bars but kept the run row.
    world.bars = [bar for bar in world.bars if bar.batch_id == "batch-seed"]
    calls = len(world.fetch_calls)
    # Gap events are retained, so detection finds the same scope again.
    replay = service.execute(as_of=as_of, executed_at=as_of, cycle_key="cycle-r")
    assert first.run is not None
    assert len(world.fetch_calls) == calls
    assert replay.warnings == ("EVIDENCE_RECOVERY_RUN_REPLAYED",)
    assert replay.provider_requests == 0


def test_conflicting_provider_revision_is_flagged_never_overwritten() -> None:
    """Tests 4 and 14."""

    world = _world(instruments=1, last_retained=date(2026, 8, 6))
    instrument = world.instruments[0]
    # One interior hole after a retained, later session, so the window re-fetches a retained day.
    world.bars = [bar for bar in world.bars if bar.session_date != date(2026, 8, 5)]
    retained_values = _values(0)
    revised = _values(7)
    world.provider[(instrument.instrument_id, date(2026, 8, 5))] = retained_values
    world.provider[(instrument.instrument_id, date(2026, 8, 6))] = (
        revised  # disagrees with retained
    )
    world.provider[(instrument.instrument_id, date(2026, 8, 7))] = _values(3)
    repository = MemoryRepository()
    as_of = _utc(2026, 8, 20)
    summary = _recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of, cycle_key="cycle-conflict"
    )
    assert summary.gaps_conflicted == 1
    assert summary.run is not None
    assert "PROVIDER_REVISION_CONFLICT" in summary.run.reason_codes
    seed = [
        bar
        for bar in world.bars
        if bar.batch_id == "batch-seed" and bar.session_date == date(2026, 8, 6)
    ]
    assert seed and seed[0].values == retained_values, "the retained row is never overwritten"
    conflicted = [
        item for item in repository.current() if item.disposition is GapDisposition.CONFLICTED
    ]
    assert len(conflicted) == 1 and conflicted[0].interval_start == "2026-08-06"
    # A revision has its own gap identity and never touches coverage gaps.
    assert conflicted[0].subject_id.endswith("#revision")
    # The conflicted session earns no recovery vintage.
    assert not any(
        item.market_time.date() == date(2026, 8, 6) for item in repository.vintages.values()
    )


def test_credentials_absent_blocks_without_inventing_evidence() -> None:
    world = _world(instruments=2)
    world.state = "BLOCKED_CREDENTIALS"
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 2)
    summary = _recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of, cycle_key="cycle-nocreds"
    )
    assert summary.status is RecoveryRunStatus.BLOCKED_CREDENTIALS
    assert summary.provider_requests == 0 and not repository.vintages
    assert all(
        item.disposition is GapDisposition.OPEN
        for item in repository.current(EvidenceFamily.SECURITIES_DAILY_BAR)
    ), "gaps stay open, nothing is approximated"


def test_provider_request_ceiling_is_respected() -> None:
    """Test 15."""

    world = _world(instruments=1)
    world.requests_per_fetch = 5
    with pytest.raises(ValueError, match="REQUEST_CEILING_EXCEEDED"):
        _recoverer(world, MemoryRepository()).execute(
            as_of=_utc(2026, 9, 1),
            executed_at=_utc(2026, 9, 1),
            cycle_key="cycle-ceiling",
            budget=RecoveryBudget(request_ceiling=3, chunk_budget=1),
        )


def test_provider_with_no_bar_marks_irrecoverable_never_approximated() -> None:
    world = _world(instruments=1)
    world.provider.clear()  # provider answers completely but holds no bar
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 2)
    summary = _recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of, cycle_key="cycle-none"
    )
    assert summary.gaps_irrecoverable == 1
    head = repository.current(EvidenceFamily.SECURITIES_DAILY_BAR)[0]
    assert head.disposition is GapDisposition.IRRECOVERABLE
    assert head.reason_codes == ("PROVIDER_RETURNED_NO_BAR_FOR_SESSION",)


def test_recovery_resumes_normal_capture_and_recurring_bars_populate() -> None:
    """Tests 11 and 18: after recovery, each later day fetches only the newest
    session as ordinary scheduled capture, and no gap remains."""

    world = _world(instruments=2)
    repository = MemoryRepository()
    day = _utc(2026, 10, 2)
    _recoverer(world, repository).execute(as_of=day, executed_at=day, cycle_key="cycle-1")
    retained_before = {(bar.instrument_id, bar.session_date) for bar in world.bars}
    calls = len(world.fetch_calls)
    assert not repository.unresolved_gaps(evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR)
    # Next trading day: one new session, nominal lag, no gap.
    world.provider.update(
        {
            (instrument.instrument_id, date(2026, 10, 2)): _values(90)
            for instrument in world.instruments
        }
    )
    next_day = _utc(2026, 10, 3, 1)
    summary = _recoverer(world, repository).execute(
        as_of=next_day, executed_at=next_day, cycle_key="cycle-2"
    )
    assert len(world.fetch_calls) == calls + 1
    assert world.fetch_calls[-1]["start"] == world.fetch_calls[-1]["end"] == date(2026, 10, 2)
    assert summary.bars_acquired == 2
    newest = [
        item
        for item in repository.vintages.values()
        if item.market_time.date() == date(2026, 10, 2)
    ]
    assert newest and all(
        item.acquisition_path is AcquisitionPath.RECOVERY_BACKFILL for item in newest
    )
    assert not repository.unresolved_gaps(evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR)
    after = {(bar.instrument_id, bar.session_date) for bar in world.bars}
    assert after - retained_before == {
        (instrument.instrument_id, date(2026, 10, 2)) for instrument in world.instruments
    }
    # And a further same-day call is a no-op.
    again = _recoverer(world, repository).execute(
        as_of=next_day, executed_at=next_day, cycle_key="cycle-2"
    )
    assert len(world.fetch_calls) == calls + 1
    assert again.vintages_written == 0


def test_healed_open_gaps_resolve_as_self_healed_without_requests() -> None:
    world = _world(instruments=1)
    world.state = "FAILED"
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 2)
    _recoverer(world, repository).execute(as_of=as_of, executed_at=as_of, cycle_key="cycle-x")
    assert repository.unresolved_gaps(evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR)
    # Ordinary capture later backfills the bars on its own.
    for (iid, session), values in world.provider.items():
        world.add_retained(iid, session, values, "batch-ordinary")
    world.state = "COMPLETED"
    calls = len(world.fetch_calls)
    summary = _recoverer(world, repository).execute(
        as_of=_utc(2026, 10, 3), executed_at=_utc(2026, 10, 3), cycle_key="cycle-y"
    )
    assert len(world.fetch_calls) == calls
    assert summary.gaps_recovered >= 1 and summary.provider_requests == 0
    head = repository.current(EvidenceFamily.SECURITIES_DAILY_BAR)[0]
    assert head.disposition is GapDisposition.RECOVERED
    assert head.reason_codes == ("SELF_HEALED_BY_ROLLING_WINDOW",)


# ---------------------------------------------------------------------------
# Live-only gap detection helpers
# ---------------------------------------------------------------------------


def test_live_only_gap_detection_helpers() -> None:
    sessions = [
        item.session_date
        for item in CALENDAR.sessions(start=date(2026, 9, 7), end=date(2026, 9, 11))
    ]
    closed = {day: True for day in sessions}
    gaps = options_session_gaps(
        expected_sessions=sessions, retained_session_ids=["2026-09-08"], window_closed=closed
    )
    assert gaps == (("OPTIONS_FORWARD", "2026-09-09", "2026-09-11"),)
    closed[date(2026, 9, 11)] = False  # still-open window is not yet a gap
    assert options_session_gaps(
        expected_sessions=sessions, retained_session_ids=["2026-09-08"], window_closed=closed
    ) == (("OPTIONS_FORWARD", "2026-09-09", "2026-09-10"),)
    assert kalshi_refresh_date_gaps(
        retained_dates=[date(2026, 9, 5), date(2026, 9, 9)], through_exclusive=date(2026, 9, 11)
    ) == (
        ("KALSHI_REFRESH", "2026-09-06", "2026-09-08"),
        ("KALSHI_REFRESH", "2026-09-10", "2026-09-10"),
    )
    starts = [_utc(2026, 9, 5, 0, n) for n in range(5)] + [_utc(2026, 9, 5, 6, 0)]
    found = live_observation_gaps(
        subject_id="CRYPTO:COINBASE", window_starts=starts, as_of=_utc(2026, 9, 5, 6, 1)
    )
    assert found == (("CRYPTO:COINBASE", "2026-09-05T00:04:00+00:00", "2026-09-05T06:00:00+00:00"),)


# ---------------------------------------------------------------------------
# Crypto
# ---------------------------------------------------------------------------


@dataclass
class CryptoWorld:
    coverage_rows: list[CryptoProductCoverage]
    provider: dict[date, tuple[str, str]] = field(default_factory=dict)
    calls: int = 0
    state: str = "COMPLETED"

    def coverage(self) -> tuple[CryptoProductCoverage, ...]:
        return tuple(self.coverage_rows)

    def fetch_missing(
        self, *, product_id: str, days: tuple[date, ...], run_key: str, requested_at: datetime
    ) -> CryptoFetchOutcome:
        self.calls += 1
        if self.state != "COMPLETED":
            return CryptoFetchOutcome("FAILED", 1, failure_code="HTTPError")
        candles = tuple(
            CryptoCandleValue(day, *self.provider[day]) for day in days if day in self.provider
        )
        return CryptoFetchOutcome(
            "COMPLETED", 1, candles=candles, observation_ids=("obs-1",), acquired_at=requested_at
        )


def _crypto_recoverer(world: CryptoWorld, repository: MemoryRepository) -> RecoverCryptoCandles:
    return RecoverCryptoCandles(repository=repository, coverage=world, fetcher=world)


def _days(first: date, count: int) -> list[date]:
    return [first + timedelta(days=n) for n in range(count)]


def test_crypto_gap_is_recovered_from_provider_history() -> None:
    retained = set(_days(date(2026, 1, 1), 300)) - set(_days(date(2026, 4, 1), 5))
    world = CryptoWorld(
        [CryptoProductCoverage("BTC-USD", date(2026, 1, 1), frozenset(retained), {})],
        provider={day: ("100", "1") for day in _days(date(2026, 4, 1), 5)},
    )
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 1)
    summary = _crypto_recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of, cycle_key="c-crypto"
    )
    assert world.calls >= 1 and summary.gaps_recovered >= 1
    head = next(
        item
        for item in repository.current(EvidenceFamily.CRYPTO_DAILY_CANDLE)
        if item.interval_start == "2026-04-01"
    )
    assert head.disposition is GapDisposition.RECOVERED
    assert all(
        item.acquisition_path is AcquisitionPath.RECOVERY_BACKFILL
        for item in repository.vintages.values()
    )


def test_crypto_gap_that_self_healed_is_resolved_without_a_provider_request() -> None:
    first_days = set(_days(date(2026, 1, 1), 300)) - set(_days(date(2026, 4, 1), 5))
    world = CryptoWorld(
        [CryptoProductCoverage("BTC-USD", date(2026, 1, 1), frozenset(first_days), {})],
        state="FAILED",
    )
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 1)
    _crypto_recoverer(world, repository).execute(as_of=as_of, executed_at=as_of, cycle_key="c1")
    assert repository.unresolved_gaps(evidence_family=EvidenceFamily.CRYPTO_DAILY_CANDLE)
    # The rolling pulse window later covers every day.
    world.coverage_rows = [
        CryptoProductCoverage(
            "BTC-USD", date(2026, 1, 1), frozenset(_days(date(2026, 1, 1), 300)), {}
        )
    ]
    calls = world.calls
    summary = _crypto_recoverer(world, repository).execute(
        as_of=_utc(2026, 10, 2), executed_at=_utc(2026, 10, 2), cycle_key="c2"
    )
    assert world.calls == calls and summary.provider_requests == 0
    assert summary.gaps_recovered >= 1
    head = repository.current(EvidenceFamily.CRYPTO_DAILY_CANDLE)[0]
    assert head.reason_codes == ("SELF_HEALED_BY_ROLLING_WINDOW",)


def test_crypto_request_ceiling_is_a_hard_bound() -> None:
    retained = set(_days(date(2026, 1, 1), 300))
    holes = [date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)]
    world = CryptoWorld(
        [CryptoProductCoverage("BTC-USD", date(2026, 1, 1), frozenset(retained - set(holes)), {})],
        provider={day: ("1", "1") for day in holes},
    )
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 1)
    summary = _crypto_recoverer(world, repository).execute(
        as_of=as_of,
        executed_at=as_of,
        cycle_key="c-bound",
        budget=RecoveryBudget(request_ceiling=2),
    )
    assert summary.provider_requests == 2 and world.calls == 2
    assert summary.status is RecoveryRunStatus.BUDGET_EXHAUSTED


# ---------------------------------------------------------------------------
# Architecture: recovery has no PAPER/order/live path
# ---------------------------------------------------------------------------


def test_recovery_modules_have_no_paper_order_or_live_dependency() -> None:
    """Test 8: nothing here can manufacture an order, fill, or reconciliation."""

    forbidden = (
        "crypto_paper",
        "kalshi_operating",
        "securities_paper",
        "paper_trial",
        "live_market_observation",
        "place_order",
        "reconcil",
        "submit",
    )
    for name in (
        "domain/evidence_recovery.py",
        "application/evidence_recovery.py",
        "adapters/evidence_recovery_postgres.py",
        "adapters/evidence_recovery_sources.py",
        "adapters/evidence_recovery_runner.py",
    ):
        text = (SOURCE_ROOT / name).read_text(encoding="utf-8")
        imports = "\n".join(
            line for line in text.splitlines() if line.startswith(("import ", "from "))
        )
        for token in forbidden:
            assert token not in imports, f"{name} imports {token}"
    # The only PAPER-adjacent read is the closed-trial timing used to record an
    # irrecoverable execution-window gap; it never writes.
    assert gap_key_for(
        evidence_family=EvidenceFamily.PAPER_EXECUTION_FACT, subject_id="t", interval_start="a"
    )


# ---------------------------------------------------------------------------
# Review hardening: terminal sessions, windows, late arrivals, ledger purity
# ---------------------------------------------------------------------------


def test_irrecoverable_sessions_never_pin_the_fetch_window_again() -> None:
    """A session the provider has no bar for is settled once; later runs do not
    re-request it, so the window moves forward instead of growing forever."""

    world = _world(instruments=1, provider_through=date(2026, 10, 9))
    instrument = world.instruments[0]
    # The provider has no bar for 2026-09-30 (an illiquid-name hole).
    del world.provider[(instrument.instrument_id, date(2026, 9, 30))]
    repository = MemoryRepository()
    first_day = _utc(2026, 10, 6)
    # Earlier sessions were already captured, so only 09-30 and the tail are open.
    for (iid, session), values in list(world.provider.items()):
        if session < date(2026, 10, 1):
            world.add_retained(iid, session, values, "batch-ordinary")
    _recoverer(world, repository).execute(
        as_of=first_day, executed_at=first_day, cycle_key="cycle-hole"
    )
    heads = repository.current(EvidenceFamily.SECURITIES_DAILY_BAR)
    assert any(item.disposition is GapDisposition.IRRECOVERABLE for item in heads)
    calls = len(world.fetch_calls)
    later = _utc(2026, 10, 8)
    preview = _recoverer(world, repository).preview(as_of=later)
    assert "2026-09-30" not in preview["missing_sessions"]
    _recoverer(world, repository).execute(
        as_of=later, executed_at=later, cycle_key="cycle-after-hole"
    )
    assert world.fetch_calls[calls]["start"] >= date(2026, 10, 6), "window did not pin at 09-30"


def test_late_arrival_for_a_terminal_gap_is_not_a_mid_write_failure() -> None:
    gap = EvidenceCoverageGapEvent.detect(
        evidence_family=EvidenceFamily.SECURITIES_DAILY_BAR,
        subject_id="inst",
        interval_start="2026-09-30",
        interval_end="2026-09-30",
    ).transition(
        disposition=GapDisposition.IRRECOVERABLE,
        reason_codes=("PROVIDER_RETURNED_NO_BAR_FOR_SESSION",),
        recovery_run_id="run-1",
    )
    from apex.application.evidence_recovery import _Ledger

    assert (
        _Ledger.resolution(
            gap,
            disposition=GapDisposition.RECOVERED,
            reasons=("RECOVERED_FROM_PROVIDER_HISTORY",),
            run_id="run-2",
        )
        is None
    )


def test_instruments_with_different_windows_are_fetched_separately_oldest_first() -> None:
    world = _world(instruments=2)
    late, early = world.instruments[1], world.instruments[0]
    # ``late`` already holds every bar up to 2026-09-25; ``early`` stopped 08-06.
    for session in CALENDAR.sessions(start=date(2026, 8, 7), end=date(2026, 9, 25)):
        world.add_retained(late.instrument_id, session.session_date, _values(5), "batch-ordinary")
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 2)
    _recoverer(world, repository).execute(as_of=as_of, executed_at=as_of, cycle_key="cycle-windows")
    assert len(world.fetch_calls) == 2
    first, second = world.fetch_calls
    assert first["symbols"] == (early.symbol,) and first["start"] == date(2026, 8, 7)
    assert second["symbols"] == (late.symbol,) and second["start"] == date(2026, 9, 28)


def test_live_observation_trailing_gap_is_not_frozen_as_irrecoverable() -> None:
    starts = [_utc(2026, 9, 5, 0, n) for n in range(3)]
    assert (
        live_observation_gaps(
            subject_id="CRYPTO:COINBASE", window_starts=starts, as_of=_utc(2026, 9, 9)
        )
        == ()
    )
    resumed = [*starts, _utc(2026, 9, 9, 0, 0)]
    assert live_observation_gaps(
        subject_id="CRYPTO:COINBASE", window_starts=resumed, as_of=_utc(2026, 9, 9, 0, 1)
    ) == (("CRYPTO:COINBASE", "2026-09-05T00:02:00+00:00", "2026-09-09T00:00:00+00:00"),)


def test_crypto_revision_conflict_is_flagged_under_its_own_identity() -> None:
    retained = set(_days(date(2026, 1, 1), 300)) - set(_days(date(2026, 4, 1), 3))
    values = {day: ("100", "1") for day in retained}
    world = CryptoWorld(
        [CryptoProductCoverage("BTC-USD", date(2026, 1, 1), frozenset(retained), values)],
        provider={
            **{day: ("100", "1") for day in _days(date(2026, 4, 1), 3)},
            date(2026, 3, 31): ("999", "1"),  # disagrees with a retained candle
        },
    )

    def fetch(**kwargs):  # type: ignore[no-untyped-def]
        world.calls += 1
        candles = tuple(
            CryptoCandleValue(day, *value) for day, value in sorted(world.provider.items())
        )
        return CryptoFetchOutcome("COMPLETED", 1, candles=candles, observation_ids=("o",))

    world.fetch_missing = fetch  # type: ignore[method-assign]
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 1)
    summary = _crypto_recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of, cycle_key="c-conflict"
    )
    assert summary.gaps_conflicted == 1
    conflicted = [
        item
        for item in repository.current(EvidenceFamily.CRYPTO_DAILY_CANDLE)
        if item.disposition is GapDisposition.CONFLICTED
    ]
    assert len(conflicted) == 1 and conflicted[0].subject_id == "BTC-USD#revision"


def test_crypto_newest_candle_is_due_not_a_gap() -> None:
    """The pulse retains day D in the refresh of D + 2, so on D + 1 the newest
    candle is merely due: it must neither become a gap nor trigger a request."""

    retained = set(_days(date(2026, 1, 1), 273))  # through 2026-09-30 minus nothing earlier
    last = max(retained)
    world = CryptoWorld(
        [CryptoProductCoverage("BTC-USD", date(2026, 1, 1), frozenset(retained), {})]
    )
    repository = MemoryRepository()
    # Cycle of 2026-10-01 expects candles through 09-29; 09-30 is due, not missing.
    as_of = datetime.combine(last + timedelta(days=1), datetime.min.time(), UTC)
    summary = _crypto_recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of, cycle_key="c-lag"
    )
    assert summary.status is RecoveryRunStatus.NOTHING_TO_DO and world.calls == 0
    # Two days later the same absence is an overdue gap.
    retained_before = set(_days(date(2026, 1, 1), 272))  # through 09-29, 09-30 absent
    world.coverage_rows = [
        CryptoProductCoverage("BTC-USD", date(2026, 1, 1), frozenset(retained_before), {})
    ]
    later = as_of + timedelta(days=1)
    world.provider = {last: ("1", "1")}
    _crypto_recoverer(world, repository).execute(
        as_of=later, executed_at=later, cycle_key="c-lag-2"
    )
    assert world.calls == 1


def test_crypto_days_the_provider_has_no_candle_for_are_settled_once() -> None:
    retained = set(_days(date(2026, 1, 1), 300)) - {date(2026, 4, 1)}
    world = CryptoWorld(
        [CryptoProductCoverage("THIN-USD", date(2026, 1, 1), frozenset(retained), {})]
    )  # provider has nothing for 04-01
    repository = MemoryRepository()
    as_of = _utc(2026, 10, 1)
    first = _crypto_recoverer(world, repository).execute(
        as_of=as_of, executed_at=as_of, cycle_key="c-thin-1"
    )
    assert first.gaps_irrecoverable == 1 and world.calls == 1
    next_day = as_of + timedelta(days=1)
    second = _crypto_recoverer(world, repository).execute(
        as_of=next_day, executed_at=next_day, cycle_key="c-thin-2"
    )
    assert world.calls == 1, "a settled day is never re-requested"
    assert second.status is RecoveryRunStatus.NOTHING_TO_DO
