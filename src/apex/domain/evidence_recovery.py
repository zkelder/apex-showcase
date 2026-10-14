"""Historical Evidence Gap Recovery v1 (Phase A): provenance and gap ledger.

Pure rules for telling contemporaneous evidence from late-acquired evidence,
for recording what Apex failed to capture while it was off, and for refusing
to let recovered evidence masquerade as live evidence.

Design rules:

* **Evidence only, authority ``NONE``.** Nothing here creates a decision,
  opportunity, order, fill, reconciliation, cycle or live observation.
* **No parallel market-data model.** Recovered bars/candles land in the
  existing canonical evidence tables. This module adds only a sidecar
  (``EvidenceVintage``) saying how and when each retained fact was acquired,
  plus a gap ledger. Historical rows are never rewritten to populate it; a
  fact without a vintage is ``UNKNOWN_LEGACY``, never assumed live.
* **Only two families are recoverable.** A provider's historical record
  represents the same economic fact for a daily candle/bar. It does not for a
  quote, an order book, an option chain, a live observation or a PAPER
  execution fact, so those are recorded ``IRRECOVERABLE`` and never
  approximated.
* **Recovered evidence is never live evidence.** ``RECOVERY_BACKFILL`` can
  never satisfy a freshness requirement, and the vintage itself refuses to be
  constructed for a live-only family.
* **Deterministic identity.** Run and gap identities are pure functions of
  their scope; ``canonical_hash`` never covers wall-clock timestamps, so exact
  replay is byte-identical.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from apex.domain.values import content_digest, deterministic_id


class EvidenceRecoveryError(ValueError):
    """Stable machine-readable failure for the recovery ledger."""


class EvidenceFamily(StrEnum):
    CRYPTO_DAILY_CANDLE = "CRYPTO_DAILY_CANDLE"
    SECURITIES_DAILY_BAR = "SECURITIES_DAILY_BAR"
    OPTIONS_CHAIN_SESSION = "OPTIONS_CHAIN_SESSION"
    KALSHI_REFRESH_DATE = "KALSHI_REFRESH_DATE"
    LIVE_OBSERVATION_WINDOW = "LIVE_OBSERVATION_WINDOW"
    CRYPTO_ORDER_BOOK = "CRYPTO_ORDER_BOOK"
    PAPER_EXECUTION_FACT = "PAPER_EXECUTION_FACT"


class AcquisitionPath(StrEnum):
    SCHEDULED_LIVE = "SCHEDULED_LIVE"
    RECOVERY_BACKFILL = "RECOVERY_BACKFILL"
    OPERATOR_IMPORT = "OPERATOR_IMPORT"
    # A retained fact written before this ledger existed. Never assumed live.
    UNKNOWN_LEGACY = "UNKNOWN_LEGACY"


class GapDisposition(StrEnum):
    OPEN = "OPEN"
    RECOVERED = "RECOVERED"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    IRRECOVERABLE = "IRRECOVERABLE"
    # A recovered provider record disagrees with an already-retained one. The
    # retained row is never overwritten; the disagreement needs operator review.
    CONFLICTED = "CONFLICTED"


class RecoveryRunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    NOTHING_TO_DO = "NOTHING_TO_DO"
    BLOCKED_CREDENTIALS = "BLOCKED_CREDENTIALS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    FAILED = "FAILED"


RECOVERABLE_FAMILIES: Final[frozenset[EvidenceFamily]] = frozenset(
    {EvidenceFamily.CRYPTO_DAILY_CANDLE, EvidenceFamily.SECURITIES_DAILY_BAR}
)
LIVE_ONLY_FAMILIES: Final[frozenset[EvidenceFamily]] = frozenset(
    set(EvidenceFamily) - RECOVERABLE_FAMILIES
)

REASON_SELF_HEALED_BY_ROLLING_WINDOW: Final = "SELF_HEALED_BY_ROLLING_WINDOW"
REASON_RECOVERED_FROM_PROVIDER_HISTORY: Final = "RECOVERED_FROM_PROVIDER_HISTORY"
REASON_PROVIDER_RETURNED_NO_BAR: Final = "PROVIDER_RETURNED_NO_BAR_FOR_SESSION"
REASON_PROVIDER_REVISION_CONFLICT: Final = "PROVIDER_REVISION_CONFLICT"
REASON_PARTIAL_PROVIDER_COVERAGE: Final = "PARTIAL_PROVIDER_COVERAGE"
REASON_BLOCKED_CREDENTIALS: Final = "ALPACA_CREDENTIALS_REQUIRED"

IRRECOVERABLE_REASONS: Final[dict[EvidenceFamily, str]] = {
    EvidenceFamily.OPTIONS_CHAIN_SESSION: "OPTION_CHAIN_NOT_RECONSTRUCTIBLE",
    EvidenceFamily.CRYPTO_ORDER_BOOK: "HISTORICAL_ORDER_BOOK_NOT_AVAILABLE",
    EvidenceFamily.LIVE_OBSERVATION_WINDOW: "LIVE_OBSERVATION_IS_CONTEMPORANEOUS_ONLY",
    EvidenceFamily.KALSHI_REFRESH_DATE: "KALSHI_HISTORICAL_QUOTES_NOT_RETAINED",
    EvidenceFamily.PAPER_EXECUTION_FACT: "EXECUTION_FACT",
}

# Recovered crypto candles are retained in the existing observation store but
# under this capture-id prefix, and the repository hides them from every
# ordinary consumer. A future consumer must opt in with vintage awareness.
RECOVERY_CAPTURE_ID_PREFIX: Final = "evidence-recovery-"

# Timeliness policy, independent of acquisition path. A session's bar is "due"
# for nominal capture until this long after its availability time; the same
# bound separates a ``NOMINAL`` vintage from a ``LATE`` one. It never decides
# how the evidence entered Apex (``AcquisitionPath``), which is fixed by the
# acquiring mechanism alone.
NOMINAL_CAPTURE_LAG: Final = timedelta(hours=36)

_TERMINAL = frozenset({GapDisposition.RECOVERED, GapDisposition.IRRECOVERABLE})
_ALLOWED_TRANSITIONS: Final[dict[GapDisposition, frozenset[GapDisposition]]] = {
    GapDisposition.OPEN: frozenset(
        {
            GapDisposition.OPEN,
            GapDisposition.RECOVERED,
            GapDisposition.PARTIALLY_RECOVERED,
            GapDisposition.IRRECOVERABLE,
            GapDisposition.CONFLICTED,
        }
    ),
    GapDisposition.PARTIALLY_RECOVERED: frozenset(
        {
            GapDisposition.PARTIALLY_RECOVERED,
            GapDisposition.RECOVERED,
            GapDisposition.IRRECOVERABLE,
            GapDisposition.CONFLICTED,
        }
    ),
    GapDisposition.CONFLICTED: frozenset({GapDisposition.CONFLICTED, GapDisposition.RECOVERED}),
    GapDisposition.RECOVERED: frozenset(),
    GapDisposition.IRRECOVERABLE: frozenset(),
}


class VintageTimeliness(StrEnum):
    """Whether a fact was obtained inside the nominal capture window.

    Derived from timestamps only; says nothing about acquisition path."""

    NOMINAL = "NOMINAL"
    LATE = "LATE"


def classify_vintage_timeliness(
    *, available_at: datetime, acquired_at: datetime, nominal_lag: timedelta = NOMINAL_CAPTURE_LAG
) -> VintageTimeliness:
    _aware(available_at, "available_at")
    _aware(acquired_at, "acquired_at")
    if acquired_at - available_at <= nominal_lag:
        return VintageTimeliness.NOMINAL
    return VintageTimeliness.LATE


@dataclass(frozen=True, slots=True)
class EvidenceVintage:
    """How and when one retained fact was acquired.

    ``market_time`` is the economic time the fact describes, ``available_at``
    the earliest time the fact could economically be known (``None`` when the
    source has no honest availability time), ``acquired_at`` when Apex actually
    obtained it. Never rewrites the fact it describes.
    """

    vintage_id: str
    evidence_family: EvidenceFamily
    evidence_id: str
    market_time: datetime
    available_at: datetime | None
    acquired_at: datetime
    acquisition_path: AcquisitionPath
    recovery_run_id: str | None
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        evidence_family: EvidenceFamily,
        evidence_id: str,
        market_time: datetime,
        available_at: datetime | None,
        acquired_at: datetime,
        acquisition_path: AcquisitionPath,
        recovery_run_id: str | None = None,
    ) -> EvidenceVintage:
        _aware(market_time, "market_time")
        _aware(acquired_at, "acquired_at")
        if available_at is not None:
            _aware(available_at, "available_at")
            if available_at < market_time:
                raise EvidenceRecoveryError("EVIDENCE_VINTAGE_AVAILABLE_BEFORE_MARKET_TIME")
            if acquired_at < available_at:
                raise EvidenceRecoveryError("EVIDENCE_VINTAGE_ACQUIRED_BEFORE_AVAILABLE")
        elif acquired_at < market_time:
            raise EvidenceRecoveryError("EVIDENCE_VINTAGE_ACQUIRED_BEFORE_MARKET_TIME")
        if not evidence_id:
            raise EvidenceRecoveryError("EVIDENCE_VINTAGE_EVIDENCE_ID_REQUIRED")
        if acquisition_path is AcquisitionPath.RECOVERY_BACKFILL:
            if evidence_family not in RECOVERABLE_FAMILIES:
                raise EvidenceRecoveryError("RECOVERED_EVIDENCE_REFUSED_FOR_LIVE_ONLY_FAMILY")
            if not recovery_run_id:
                raise EvidenceRecoveryError("RECOVERY_BACKFILL_REQUIRES_RECOVERY_RUN")
        elif acquisition_path is AcquisitionPath.UNKNOWN_LEGACY:
            raise EvidenceRecoveryError("UNKNOWN_LEGACY_IS_IMPLICIT_AND_NEVER_STORED")
        vintage_id = deterministic_id(
            "evidence_vintage", (evidence_family.value, evidence_id, acquisition_path.value)
        )
        draft = cls(
            vintage_id=vintage_id,
            evidence_family=evidence_family,
            evidence_id=evidence_id,
            market_time=market_time,
            available_at=available_at,
            acquired_at=acquired_at,
            acquisition_path=acquisition_path,
            recovery_run_id=recovery_run_id,
            canonical_hash="",
        )
        return replace(draft, canonical_hash=_vintage_hash(draft))

    @property
    def lag_seconds(self) -> int:
        anchor = self.available_at or self.market_time
        return int((self.acquired_at - anchor).total_seconds())

    @property
    def timeliness(self) -> VintageTimeliness:
        anchor = self.available_at or self.market_time
        return classify_vintage_timeliness(available_at=anchor, acquired_at=self.acquired_at)

    @property
    def canonical_acquisition_path(self) -> AcquisitionPath:
        """The path a reader must treat as true.

        A ``SCHEDULED_LIVE`` row that carries a recovery run id was mislabelled
        by the pre-correction lag classifier: only a recovery run writes a run
        id, so the acquiring mechanism was recovery."""

        if (
            self.acquisition_path is AcquisitionPath.SCHEDULED_LIVE
            and self.recovery_run_id
            and self.evidence_family in RECOVERABLE_FAMILIES
        ):
            return AcquisitionPath.RECOVERY_BACKFILL
        return self.acquisition_path

    def may_satisfy_live_freshness(
        self, *, decision_time: datetime, maximum_age: timedelta
    ) -> bool:
        """Only a scheduled-live fact acquired no later than the decision and
        within the age bound is contemporaneous. A recovered fact never is."""

        if self.canonical_acquisition_path is not AcquisitionPath.SCHEDULED_LIVE:
            return False
        if self.evidence_family in RECOVERABLE_FAMILIES:
            # Daily candles/bars are research evidence, not quote freshness.
            return False
        return timedelta(0) <= decision_time - self.acquired_at <= maximum_age


def _vintage_hash(value: EvidenceVintage) -> str:
    return content_digest(
        (
            value.vintage_id,
            value.evidence_family.value,
            value.evidence_id,
            value.market_time,
            value.available_at,
            value.acquired_at,
            value.acquisition_path.value,
            value.recovery_run_id,
        )
    )


def verify_evidence_vintage(value: EvidenceVintage) -> EvidenceVintage:
    rebuilt = EvidenceVintage.create(
        evidence_family=value.evidence_family,
        evidence_id=value.evidence_id,
        market_time=value.market_time,
        available_at=value.available_at,
        acquired_at=value.acquired_at,
        acquisition_path=value.acquisition_path,
        recovery_run_id=value.recovery_run_id,
    )
    if rebuilt != value or value.authority != "NONE":
        raise EvidenceRecoveryError("EVIDENCE_VINTAGE_INTEGRITY_INVALID")
    return value


def build_acquisition_path_correction(vintage: EvidenceVintage) -> EvidenceVintage:
    """The append-only ``RECOVERY_BACKFILL`` counterpart of a mislabelled row.

    Refuses anything that is not a recoverable-family ``SCHEDULED_LIVE`` row
    carrying a recovery run id. Every market/timing field is copied unchanged;
    only the path (and therefore the deterministic identity) differs."""

    if (
        vintage.acquisition_path is not AcquisitionPath.SCHEDULED_LIVE
        or not vintage.recovery_run_id
        or vintage.evidence_family not in RECOVERABLE_FAMILIES
    ):
        raise EvidenceRecoveryError("ACQUISITION_PATH_CORRECTION_NOT_APPLICABLE")
    return EvidenceVintage.create(
        evidence_family=vintage.evidence_family,
        evidence_id=vintage.evidence_id,
        market_time=vintage.market_time,
        available_at=vintage.available_at,
        acquired_at=vintage.acquired_at,
        acquisition_path=AcquisitionPath.RECOVERY_BACKFILL,
        recovery_run_id=vintage.recovery_run_id,
    )


def assert_live_evidence_admissible(vintage: EvidenceVintage | None) -> None:
    """Guard for every live-only writer. A missing vintage is the ordinary
    contemporaneous path; any recovered or imported vintage is refused."""

    if vintage is None:
        return
    if vintage.canonical_acquisition_path is not AcquisitionPath.SCHEDULED_LIVE:
        raise EvidenceRecoveryError("RECOVERED_EVIDENCE_CANNOT_ENTER_LIVE_ONLY_TABLE")


@dataclass(frozen=True, slots=True)
class EvidenceRecoveryRun:
    run_id: str
    evidence_family: EvidenceFamily
    acquisition_path: AcquisitionPath
    cycle_key: str
    scope_start: str
    scope_end: str
    gap_keys: tuple[str, ...]
    status: RecoveryRunStatus
    request_ceiling: int
    provider_requests: int
    gaps_examined: int
    gaps_recovered: int
    gaps_partial: int
    gaps_irrecoverable: int
    gaps_conflicted: int
    reason_codes: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        evidence_family: EvidenceFamily,
        cycle_key: str,
        scope_start: str,
        scope_end: str,
        gap_keys: Iterable[str],
        status: RecoveryRunStatus,
        request_ceiling: int,
        provider_requests: int,
        gaps_recovered: int = 0,
        gaps_partial: int = 0,
        gaps_irrecoverable: int = 0,
        gaps_conflicted: int = 0,
        reason_codes: Iterable[str] = (),
    ) -> EvidenceRecoveryRun:
        if evidence_family not in RECOVERABLE_FAMILIES:
            raise EvidenceRecoveryError("RECOVERY_RUN_FAMILY_NOT_RECOVERABLE")
        keys = tuple(sorted(set(gap_keys)))
        if request_ceiling < 0 or provider_requests < 0:
            raise EvidenceRecoveryError("RECOVERY_RUN_REQUEST_COUNT_INVALID")
        if provider_requests > request_ceiling:
            raise EvidenceRecoveryError("RECOVERY_RUN_REQUEST_CEILING_EXCEEDED")
        if min(gaps_recovered, gaps_partial, gaps_irrecoverable, gaps_conflicted) < 0:
            raise EvidenceRecoveryError("RECOVERY_RUN_COUNT_INVALID")
        run_id = deterministic_id(
            "evidence_recovery_run", (evidence_family.value, cycle_key, scope_start, keys)
        )
        draft = cls(
            run_id=run_id,
            evidence_family=evidence_family,
            acquisition_path=AcquisitionPath.RECOVERY_BACKFILL,
            cycle_key=cycle_key,
            scope_start=scope_start,
            scope_end=scope_end,
            gap_keys=keys,
            status=status,
            request_ceiling=request_ceiling,
            provider_requests=provider_requests,
            gaps_examined=len(keys),
            gaps_recovered=gaps_recovered,
            gaps_partial=gaps_partial,
            gaps_irrecoverable=gaps_irrecoverable,
            gaps_conflicted=gaps_conflicted,
            reason_codes=tuple(sorted(set(reason_codes))),
            canonical_hash="",
        )
        return replace(draft, canonical_hash=_run_hash(draft))


def _run_hash(value: EvidenceRecoveryRun) -> str:
    return content_digest(
        (
            value.run_id,
            value.evidence_family.value,
            value.acquisition_path.value,
            value.cycle_key,
            value.scope_start,
            value.scope_end,
            value.gap_keys,
            value.status.value,
            value.request_ceiling,
            value.provider_requests,
            value.gaps_examined,
            value.gaps_recovered,
            value.gaps_partial,
            value.gaps_irrecoverable,
            value.gaps_conflicted,
            value.reason_codes,
        )
    )


def verify_evidence_recovery_run(value: EvidenceRecoveryRun) -> EvidenceRecoveryRun:
    if (
        value.canonical_hash != _run_hash(value)
        or value.authority != "NONE"
        or value.acquisition_path is not AcquisitionPath.RECOVERY_BACKFILL
    ):
        raise EvidenceRecoveryError("EVIDENCE_RECOVERY_RUN_INTEGRITY_INVALID")
    return value


def gap_key_for(*, evidence_family: EvidenceFamily, subject_id: str, interval_start: str) -> str:
    """One logical gap per (family, subject, first missing point). A longer
    outage extends the same gap by appending an ``OPEN`` event."""

    return deterministic_id(
        "evidence_coverage_gap", (evidence_family.value, subject_id, interval_start)
    )


@dataclass(frozen=True, slots=True)
class EvidenceCoverageGapEvent:
    """Append-only event in one gap's life. The current state of a gap is its
    highest-ordinal event; nothing is ever updated."""

    event_id: str
    gap_key: str
    ordinal: int
    evidence_family: EvidenceFamily
    subject_id: str
    interval_start: str
    interval_end: str
    disposition: GapDisposition
    reason_codes: tuple[str, ...]
    recovery_run_id: str | None
    recovered_evidence_ids: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def detect(
        cls,
        *,
        evidence_family: EvidenceFamily,
        subject_id: str,
        interval_start: str,
        interval_end: str,
    ) -> EvidenceCoverageGapEvent:
        """First event of a gap. A live-only family is recorded
        ``IRRECOVERABLE`` immediately, with an explicit reason, never ``OPEN``."""

        if evidence_family in LIVE_ONLY_FAMILIES:
            return cls._build(
                evidence_family=evidence_family,
                subject_id=subject_id,
                interval_start=interval_start,
                interval_end=interval_end,
                ordinal=1,
                disposition=GapDisposition.IRRECOVERABLE,
                reason_codes=(IRRECOVERABLE_REASONS[evidence_family],),
                recovery_run_id=None,
                recovered_evidence_ids=(),
            )
        return cls._build(
            evidence_family=evidence_family,
            subject_id=subject_id,
            interval_start=interval_start,
            interval_end=interval_end,
            ordinal=1,
            disposition=GapDisposition.OPEN,
            reason_codes=(),
            recovery_run_id=None,
            recovered_evidence_ids=(),
        )

    @classmethod
    def detect_conflict(
        cls,
        *,
        evidence_family: EvidenceFamily,
        subject_id: str,
        interval_start: str,
        interval_end: str,
        recovery_run_id: str,
        reason_codes: Iterable[str],
    ) -> EvidenceCoverageGapEvent:
        """A provider record that disagrees with a retained one. It is its own
        gap identity (the caller scopes ``subject_id``), never a fake ``OPEN``
        event, and requires the run that found it."""

        if evidence_family not in RECOVERABLE_FAMILIES:
            raise EvidenceRecoveryError("LIVE_ONLY_GAP_CANNOT_BE_CONFLICTED")
        if not recovery_run_id:
            raise EvidenceRecoveryError("EVIDENCE_GAP_RESOLUTION_REQUIRES_RECOVERY_RUN")
        return cls._build(
            evidence_family=evidence_family,
            subject_id=subject_id,
            interval_start=interval_start,
            interval_end=interval_end,
            ordinal=1,
            disposition=GapDisposition.CONFLICTED,
            reason_codes=tuple(reason_codes),
            recovery_run_id=recovery_run_id,
            recovered_evidence_ids=(),
        )

    def transition(
        self,
        *,
        disposition: GapDisposition,
        reason_codes: Iterable[str],
        recovery_run_id: str | None,
        recovered_evidence_ids: Iterable[str] = (),
        interval_end: str | None = None,
    ) -> EvidenceCoverageGapEvent:
        if disposition not in _ALLOWED_TRANSITIONS[self.disposition]:
            raise EvidenceRecoveryError("EVIDENCE_GAP_TRANSITION_INVALID")
        if (
            self.evidence_family in LIVE_ONLY_FAMILIES
            and disposition is not GapDisposition.IRRECOVERABLE
        ):
            raise EvidenceRecoveryError("LIVE_ONLY_GAP_CANNOT_BE_RECOVERED")
        if disposition is not GapDisposition.OPEN and not recovery_run_id:
            raise EvidenceRecoveryError("EVIDENCE_GAP_RESOLUTION_REQUIRES_RECOVERY_RUN")
        new_end = interval_end or self.interval_end
        if disposition is GapDisposition.OPEN and new_end <= self.interval_end:
            raise EvidenceRecoveryError("EVIDENCE_GAP_EXTENSION_MUST_GROW")
        return self._build(
            evidence_family=self.evidence_family,
            subject_id=self.subject_id,
            interval_start=self.interval_start,
            interval_end=new_end,
            ordinal=self.ordinal + 1,
            disposition=disposition,
            reason_codes=tuple(reason_codes),
            recovery_run_id=recovery_run_id,
            recovered_evidence_ids=tuple(recovered_evidence_ids),
        )

    @classmethod
    def _build(
        cls,
        *,
        evidence_family: EvidenceFamily,
        subject_id: str,
        interval_start: str,
        interval_end: str,
        ordinal: int,
        disposition: GapDisposition,
        reason_codes: tuple[str, ...],
        recovery_run_id: str | None,
        recovered_evidence_ids: tuple[str, ...],
    ) -> EvidenceCoverageGapEvent:
        if interval_end < interval_start:
            raise EvidenceRecoveryError("EVIDENCE_GAP_INTERVAL_INVALID")
        if disposition is GapDisposition.IRRECOVERABLE and not reason_codes:
            raise EvidenceRecoveryError("IRRECOVERABLE_GAP_REQUIRES_EXPLICIT_REASON")
        key = gap_key_for(
            evidence_family=evidence_family, subject_id=subject_id, interval_start=interval_start
        )
        draft = cls(
            event_id=deterministic_id("evidence_coverage_gap_event", (key, ordinal)),
            gap_key=key,
            ordinal=ordinal,
            evidence_family=evidence_family,
            subject_id=subject_id,
            interval_start=interval_start,
            interval_end=interval_end,
            disposition=disposition,
            reason_codes=tuple(sorted(set(reason_codes))),
            recovery_run_id=recovery_run_id,
            recovered_evidence_ids=tuple(sorted(set(recovered_evidence_ids))),
            canonical_hash="",
        )
        return replace(draft, canonical_hash=_gap_hash(draft))

    @property
    def is_terminal(self) -> bool:
        return self.disposition in _TERMINAL


def _gap_hash(value: EvidenceCoverageGapEvent) -> str:
    return content_digest(
        (
            value.event_id,
            value.gap_key,
            value.ordinal,
            value.evidence_family.value,
            value.subject_id,
            value.interval_start,
            value.interval_end,
            value.disposition.value,
            value.reason_codes,
            value.recovery_run_id,
            value.recovered_evidence_ids,
        )
    )


def verify_evidence_gap_event(value: EvidenceCoverageGapEvent) -> EvidenceCoverageGapEvent:
    if value.canonical_hash != _gap_hash(value) or value.authority != "NONE":
        raise EvidenceRecoveryError("EVIDENCE_GAP_EVENT_INTEGRITY_INVALID")
    return value


# --------------------------------------------------------------------------
# Pure gap detection
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionRun:
    """A contiguous run of missing trading sessions (contiguous by position in
    the exchange calendar, so weekends and holidays never split a run)."""

    first: date
    last: date
    sessions: tuple[date, ...]


def missing_sessions(
    *, expected_sessions: Sequence[date], retained_sessions: Iterable[date]
) -> tuple[date, ...]:
    retained = set(retained_sessions)
    return tuple(day for day in sorted(set(expected_sessions)) if day not in retained)


def group_contiguous_sessions(
    *, expected_sessions: Sequence[date], missing: Iterable[date]
) -> tuple[SessionRun, ...]:
    ordered = sorted(set(expected_sessions))
    position = {day: index for index, day in enumerate(ordered)}
    missing_sorted = sorted(set(missing))
    if any(day not in position for day in missing_sorted):
        raise EvidenceRecoveryError("MISSING_SESSION_NOT_IN_EXCHANGE_CALENDAR")
    runs: list[list[date]] = []
    for day in missing_sorted:
        if runs and position[day] == position[runs[-1][-1]] + 1:
            runs[-1].append(day)
        else:
            runs.append([day])
    return tuple(SessionRun(first=run[0], last=run[-1], sessions=tuple(run)) for run in runs)


def missing_utc_days(
    *, start: date, end_exclusive: date, retained_days: Iterable[date], first_listed: date | None
) -> tuple[date, ...]:
    """Crypto trades every UTC day, so every day is expected from the later of
    ``start`` and the product's first retained day (never before listing)."""

    retained = set(retained_days)
    floor = start if first_listed is None else max(start, first_listed)
    day = floor
    result: list[date] = []
    while day < end_exclusive:
        if day not in retained:
            result.append(day)
        day += timedelta(days=1)
    return tuple(result)


def group_contiguous_days(days: Iterable[date]) -> tuple[tuple[date, date], ...]:
    ordered = sorted(set(days))
    runs: list[list[date]] = []
    for day in ordered:
        if runs and day - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(day)
        else:
            runs.append([day])
    return tuple((run[0], run[-1]) for run in runs)


@dataclass(frozen=True, slots=True)
class BarValues:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def provider_revision_conflict(retained: BarValues, recovered: BarValues) -> bool:
    """True when two provider records for the same instrument/session differ.
    The retained record is never overwritten; the difference is flagged."""

    return retained != recovered


def assert_backdated_execution_allowed(
    *, as_of: datetime, executed_at: datetime, cycle_already_retained: bool
) -> None:
    """A production cycle's ``as_of`` is its own UTC day. Creating a *new*
    cycle for a past day would stamp present-day evidence onto it, so it is
    refused. Replaying an already-retained cycle is exempt."""

    _aware(as_of, "as_of")
    _aware(executed_at, "executed_at")
    if cycle_already_retained:
        return
    if as_of > executed_at:
        raise EvidenceRecoveryError("OPERATING_CYCLE_AS_OF_IN_FUTURE_REFUSED")
    if as_of.astimezone(UTC).date() < executed_at.astimezone(UTC).date():
        raise EvidenceRecoveryError("OPERATING_CYCLE_BACKDATED_EXECUTION_REFUSED")


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceRecoveryError(f"EVIDENCE_RECOVERY_{label.upper()}_MUST_BE_TIMEZONE_AWARE")
