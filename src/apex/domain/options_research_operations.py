"""Fresh Options Evidence Operations and Structure Research Baseline v1.

Research-intelligence evidence only. Nothing in this module creates an
Order, Strategy, Shared Opportunity, PAPER order, or LIVE authority; every
persisted fact this module produces carries ``authority = "NONE"``.

This module is the *operational* layer sitting above the already-existing
genuine options evidence stack (``domain.options_quote_intelligence`` for
quote-chain capture/IV/Greeks/skew/term-structure/expected-move/IV-vs-RV,
``domain.options_structures`` for defined-risk structure economics). It
answers four questions those modules deliberately leave open:

* *when* is a genuine options-chain capture actually due, reconciled with
  the already-declared 900-second quote-freshness threshold and the
  already-reviewed XNYS exchange calendar (``OptionsCaptureWindowPolicy`` /
  ``determine_options_capture_due_state``) -- never a hand-rolled
  weekday-only check;
* how good is one capture's stratified chain coverage
  (``ChainCoverageQualityPolicy`` / ``build_chain_coverage_diagnostic``);
* how much genuine, *distinct-session* forward history has accumulated per
  underlying, and what does that imply for longitudinal research readiness
  (``evaluate_options_history_readiness``) -- deliberately independent of
  ``options_quote_intelligence.build_iv_rank``'s own, narrower "how many
  prior snapshots" percentile-history count, which stays untouched;
* what does one session's evidence look like end to end, retained as one
  bounded daily checkpoint (``OptionsForwardEvidenceCheckpoint``) and, once
  a session is genuinely fresh, as one descriptive cross-sectional baseline
  (``OptionsCrossSectionalBaseline`` -- a baseline, never a hypothesis test,
  never "best trade" language).

Stale-versus-fresh separation is preserved, not weakened: this module never
recomputes or overrides ``options_quote_intelligence``'s own
``STALENESS_THRESHOLD_SECONDS``/``QuoteObservationStatus`` classification,
and never folds a stale-reproduction snapshot into a "current" count.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum

from apex.domain.options_quote_intelligence import (
    IV_RANK_MINIMUM_HISTORY,
    STALENESS_THRESHOLD_SECONDS,
    StratifiedChainSelection,
)
from apex.domain.securities import SecuritiesCalendarSession
from apex.domain.values import DecimalInput, content_digest, deterministic_id, parse_decimal

# --------------------------------------------------------------------------
# Content-hash namespaces (versioned, one per persisted/immutable fact)
# --------------------------------------------------------------------------

OPTIONS_CAPTURE_WINDOW_POLICY_V1 = "OPTIONS_CAPTURE_WINDOW_POLICY_V1"
CHAIN_COVERAGE_QUALITY_POLICY_V1 = "CHAIN_COVERAGE_QUALITY_POLICY_V1"
OPTIONS_FORWARD_EVIDENCE_CHECKPOINT_V1 = "OPTIONS_FORWARD_EVIDENCE_CHECKPOINT_V1"
OPTIONS_CROSS_SECTIONAL_BASELINE_V1 = "OPTIONS_CROSS_SECTIONAL_BASELINE_V1"

# Reused, not re-declared: the longitudinal research-readiness gate uses the
# identical minimum retained-history count as the existing IV-rank feature
# (``options_quote_intelligence.IV_RANK_MINIMUM_HISTORY``), per the
# milestone's own instruction to keep the existing minimum unless a
# separate evidence-backed policy change is justified. The *counting rule*
# is deliberately different (distinct trading sessions, never raw snapshot
# rows or same-session replay -- see ``evaluate_options_history_readiness``)
# even though the *threshold number* is shared.
MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH = IV_RANK_MINIMUM_HISTORY

HISTORICAL_OPTIONS_PRICING_UNAVAILABLE = "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE"

OPTIONS_RESEARCH_OPERATIONS_WARNINGS = (
    "OPTIONS RESEARCH EVIDENCE OPERATIONS ARE RESEARCH ONLY",
    "NO OPTIONS OPPORTUNITY, PAPER ORDER, OR LIVE ORDER IS EVER CREATED HERE",
    "A STALE STRUCTURE OR SNAPSHOT NEVER COUNTS AS CURRENT EVIDENCE",
    "ONE FRESH SESSION IS A CROSS-SECTIONAL BASELINE, NOT LONGITUDINAL RESEARCH READINESS",
    "NO OPTION ORDER AUTHORITY IS ENABLED",
    "LIVE TRADING IS NOT ENABLED",
)


class OptionsResearchOperationsError(ValueError):
    pass


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None:
        raise OptionsResearchOperationsError(f"{label}_MUST_BE_TIMEZONE_AWARE")
    return value


def _exact(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.quantize(Decimal("0.00000001"))


# ==========================================================================
# Section 1: capture-due policy -- reconciles market-session timing with the
# existing 900-second quote-freshness threshold (milestone sections 1-2)
# ==========================================================================


@dataclass(frozen=True, slots=True)
class OptionsCaptureWindowPolicy:
    """When a genuine options-chain capture can actually satisfy the
    already-declared quote-freshness rule.

    Derivation, not a guess: the retained 2026-08-08 (Saturday) capture
    evidence shows every quote observed roughly two days after Friday's
    close reading ``STALE`` -- consistent with each contract's indicative
    quote timestamp freezing at (or near) that prior session's official
    close, because no further quote can occur while the market is shut. A
    capture that begins any time from official close through
    ``freshness_threshold_seconds`` later can therefore still land within
    the freshness bound; one that begins later cannot, because there is no
    more recent genuine quote for that session left to capture -- widening
    this window would not produce fresher evidence, only a false pass.

    The window intentionally opens exactly at official close, not earlier
    and not delayed by a further grace period. An intraday capture is
    trivially fresh (the market is live), but it produces a
    time-of-day-dependent snapshot that is not comparable session to
    session; anchoring solely on the calendar's official close -- including
    genuine early closes -- keeps every session's snapshot semantically the
    same "closing chain" fact, which is what the longitudinal research this
    milestone is building toward needs. If a genuine fresh capture's
    observed provider-timestamp behavior disagrees with this assumption,
    that evidence is retained and reported (see
    ``OptionsForwardEvidenceCheckpoint``) rather than this threshold being
    silently loosened to make a schedule pass.
    """

    policy_id: str
    version: str
    calendar_identity: str
    freshness_threshold_seconds: int
    post_close_grace_seconds: int
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        calendar_identity: str = "XNYS",
        freshness_threshold_seconds: int = STALENESS_THRESHOLD_SECONDS,
    ) -> OptionsCaptureWindowPolicy:
        if not calendar_identity:
            raise OptionsResearchOperationsError("CAPTURE_WINDOW_CALENDAR_IDENTITY_REQUIRED")
        if not (1 <= freshness_threshold_seconds <= 86_400):
            raise OptionsResearchOperationsError("CAPTURE_WINDOW_FRESHNESS_THRESHOLD_OUT_OF_RANGE")
        payload = (OPTIONS_CAPTURE_WINDOW_POLICY_V1, calendar_identity, freshness_threshold_seconds)
        return cls(
            policy_id=deterministic_id("options_capture_window_policy", payload),
            version=OPTIONS_CAPTURE_WINDOW_POLICY_V1,
            calendar_identity=calendar_identity,
            freshness_threshold_seconds=freshness_threshold_seconds,
            post_close_grace_seconds=freshness_threshold_seconds,
            canonical_hash=content_digest(payload),
        )

    def window_for_session(self, session: SecuritiesCalendarSession) -> tuple[datetime, datetime]:
        """``[window_open_at, window_close_at]``, both inclusive, both
        derived only from this exact session's own retained official close
        -- an early close narrows the window automatically, with no
        separate early-close special case anywhere in this module."""

        if session.calendar_identity != self.calendar_identity:
            raise OptionsResearchOperationsError("CAPTURE_WINDOW_CALENDAR_MISMATCH")
        window_open = session.closes_at
        window_close = session.closes_at + timedelta(seconds=self.post_close_grace_seconds)
        return window_open, window_close


class OptionsCaptureDueStatus(StrEnum):
    DUE = "DUE"
    NOT_DUE = "NOT_DUE"
    MISSED_CAPTURE_WINDOW = "MISSED_CAPTURE_WINDOW"
    ALREADY_CAPTURED = "ALREADY_CAPTURED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class OptionsCaptureDueDecision:
    status: OptionsCaptureDueStatus
    session_id: str | None
    window_open_at: datetime | None
    window_close_at: datetime | None
    reasons: tuple[str, ...]
    # Options Capture Temporal Reevaluation Correctness v2: the earliest
    # still-eligible session's ``window_open_at`` strictly after ``now``,
    # among the bounded ``sessions`` considered -- ``None`` when no such
    # session exists in range. Deliberately orthogonal to ``status``/
    # ``session_id``/``reasons`` above (which stay exactly the existing
    # "single most relevant session" operator-facing report: current, else
    # most recently missed, else next upcoming -- unchanged by this field):
    # this is the raw calendar fact a caller needs to answer its own
    # "could this become due later within MY horizon" question, without
    # this module needing to know what a caller's horizon even is (that
    # belongs to whatever schedules capture attempts, e.g.
    # ``domain.operating_cycle``'s UTC-day cycle horizon -- deliberately not
    # a concept this module imports or reasons about).
    next_capture_window_open_at: datetime | None


def determine_options_capture_due_state(
    *,
    sessions: tuple[SecuritiesCalendarSession, ...],
    policy: OptionsCaptureWindowPolicy,
    now: datetime,
    capability_supported: bool,
    captured_session_ids: frozenset[str] = frozenset(),
) -> OptionsCaptureDueDecision:
    """Genuine calendar-evidence-driven due state -- weekends and exchange
    holidays are excluded because retained XNYS session evidence never
    lists them as sessions, never by a hand-rolled weekday check; an early
    close narrows the window because ``session.closes_at`` already reflects
    it. Reports only the single most relevant session (the one currently in
    its window, else the most recently missed one, else the next upcoming
    one) -- a full multi-session history belongs to
    ``evaluate_options_history_readiness``/the retained checkpoint index,
    not this due-state check.
    """

    _aware(now, "CAPTURE_DUE_NOW")
    if not capability_supported:
        return OptionsCaptureDueDecision(
            OptionsCaptureDueStatus.UNAVAILABLE,
            None,
            None,
            None,
            ("OPTIONS_CAPTURE_CAPABILITY_UNAVAILABLE",),
            None,
        )
    relevant = tuple(
        item for item in sessions if item.calendar_identity == policy.calendar_identity
    )
    ordered = tuple(sorted(relevant, key=lambda item: item.closes_at))
    windows = tuple((item, policy.window_for_session(item)) for item in ordered)

    # Computed once, unconditionally, and threaded into every return below:
    # the single most relevant *reported* session (current/missed/upcoming)
    # still follows the exact priority order this function has always used,
    # but "when could this next genuinely become due" is a plain fact about
    # the bounded calendar evidence already in hand, independent of which
    # branch below ends up firing -- never a second, separately-prioritized
    # scan.
    future = tuple((s, w) for s, w in windows if w[0] > now)
    next_open = future[0][1][0] if future else None

    current = next(((s, w) for s, w in windows if w[0] <= now <= w[1]), None)
    if current is not None:
        session, (window_open, window_close) = current
        if session.session_date in captured_session_ids:
            return OptionsCaptureDueDecision(
                OptionsCaptureDueStatus.ALREADY_CAPTURED,
                session.session_date,
                window_open,
                window_close,
                ("ALREADY_CAPTURED_THIS_SESSION",),
                next_open,
            )
        return OptionsCaptureDueDecision(
            OptionsCaptureDueStatus.DUE,
            session.session_date,
            window_open,
            window_close,
            ("WITHIN_POST_CLOSE_CAPTURE_WINDOW",),
            next_open,
        )

    past = tuple((s, w) for s, w in windows if w[1] < now)
    if past:
        session, (window_open, window_close) = past[-1]
        if session.session_date not in captured_session_ids:
            return OptionsCaptureDueDecision(
                OptionsCaptureDueStatus.MISSED_CAPTURE_WINDOW,
                session.session_date,
                window_open,
                window_close,
                ("CAPTURE_WINDOW_ALREADY_PASSED",),
                next_open,
            )

    if future:
        session, (window_open, window_close) = future[0]
        # A best-effort, cosmetic-only distinction between "today's window
        # just hasn't opened yet" and "no session today at all" (weekend /
        # holiday) -- comparing a UTC instant's calendar date against the
        # exchange-local session date is safe for this purpose because
        # regular XNYS hours never cross a UTC midnight boundary; either
        # way the DUE/NOT_DUE outcome itself is decided above, only the
        # reported reason differs.
        if session.session_date == now.date().isoformat():
            return OptionsCaptureDueDecision(
                OptionsCaptureDueStatus.NOT_DUE,
                session.session_date,
                window_open,
                window_close,
                ("BEFORE_CAPTURE_WINDOW",),
                next_open,
            )
        return OptionsCaptureDueDecision(
            OptionsCaptureDueStatus.NOT_DUE,
            None,
            None,
            None,
            ("NOT_A_TRADING_SESSION", f"NEXT_ELIGIBLE_SESSION_{session.session_date}"),
            next_open,
        )
    return OptionsCaptureDueDecision(
        OptionsCaptureDueStatus.NOT_DUE,
        None,
        None,
        None,
        ("NO_SESSION_EVIDENCE_IN_BOUNDED_RANGE",),
        next_open,
    )


def session_id_for_captured_at(
    *,
    captured_at: datetime,
    sessions: tuple[SecuritiesCalendarSession, ...],
    policy: OptionsCaptureWindowPolicy,
) -> str | None:
    """The distinct-session identity of a genuinely fresh capture: the
    session whose own due window (see ``OptionsCaptureWindowPolicy``)
    contains ``captured_at``, else ``None``. A snapshot captured outside
    every session's window -- e.g. the retained 2026-08-08 Saturday
    stale-evidence-acceptance reproduction, captured roughly two days after
    the session it reused evidence from actually closed -- deliberately
    resolves to ``None`` here, so it can never be counted toward forward
    (distinct-session) history no matter how many raw snapshot rows exist
    (milestone section 12/13)."""

    _aware(captured_at, "SESSION_LOOKUP_CAPTURED_AT")
    relevant = tuple(
        item for item in sessions if item.calendar_identity == policy.calendar_identity
    )
    match = next(
        (
            item
            for item in relevant
            if policy.window_for_session(item)[0]
            <= captured_at
            <= policy.window_for_session(item)[1]
        ),
        None,
    )
    return match.session_date if match is not None else None


# ==========================================================================
# Section 2: chain-coverage-quality policy (milestone sections 5-6)
# ==========================================================================


class ChainCoverageQuality(StrEnum):
    GOOD_COVERAGE = "GOOD_COVERAGE"
    PARTIAL_COVERAGE = "PARTIAL_COVERAGE"
    CAP_CONSTRAINED = "CAP_CONSTRAINED"
    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"


@dataclass(frozen=True, slots=True)
class ChainCoverageQualityPolicy:
    """Declared before any capture's coverage is reviewed -- fixed
    thresholds, never tuned in response to a given underlying's result."""

    policy_id: str
    version: str
    minimum_expiration_count_selected: int
    minimum_dte_buckets_selected: int
    require_both_call_and_put: bool
    cap_pressure_threshold: Decimal
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        minimum_expiration_count_selected: int = 3,
        minimum_dte_buckets_selected: int = 2,
        require_both_call_and_put: bool = True,
        cap_pressure_threshold: DecimalInput = "0.30",
    ) -> ChainCoverageQualityPolicy:
        if minimum_expiration_count_selected < 1:
            raise OptionsResearchOperationsError("COVERAGE_POLICY_MINIMUM_EXPIRATIONS_INVALID")
        if minimum_dte_buckets_selected < 1:
            raise OptionsResearchOperationsError("COVERAGE_POLICY_MINIMUM_DTE_BUCKETS_INVALID")
        threshold = parse_decimal(cap_pressure_threshold, allow_zero=False)
        if not (Decimal("0") < threshold <= Decimal("1")):
            raise OptionsResearchOperationsError("COVERAGE_POLICY_CAP_PRESSURE_THRESHOLD_INVALID")
        payload = (
            CHAIN_COVERAGE_QUALITY_POLICY_V1,
            minimum_expiration_count_selected,
            minimum_dte_buckets_selected,
            require_both_call_and_put,
            threshold,
        )
        return cls(
            policy_id=deterministic_id("chain_coverage_quality_policy", payload),
            version=CHAIN_COVERAGE_QUALITY_POLICY_V1,
            minimum_expiration_count_selected=minimum_expiration_count_selected,
            minimum_dte_buckets_selected=minimum_dte_buckets_selected,
            require_both_call_and_put=require_both_call_and_put,
            cap_pressure_threshold=threshold,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ChainCoverageDiagnostic:
    """Reviewable per-underlying coverage evidence (milestone section 5's
    exact required field list). Deliberately not persisted as its own
    immutable row -- like ``StratifiedChainSelection`` it is fully and
    deterministically reproducible from its declared inputs; a summary of
    it is folded into the retained ``OptionsForwardEvidenceCheckpoint``."""

    underlying_symbol: str
    underlying_canonical_security_id: str
    contracts_returned_by_provider: int
    contracts_eligible_before_cap: int
    contracts_selected: int
    contracts_excluded_by_cap: int
    expiration_count_available: int
    expiration_count_selected: int
    dte_buckets_available: tuple[str, ...]
    dte_buckets_selected: tuple[str, ...]
    call_count_selected: int
    put_count_selected: int
    moneyness_buckets_selected: tuple[str, ...]
    quality: ChainCoverageQuality
    quality_reasons: tuple[str, ...]


def build_chain_coverage_diagnostic(
    *,
    underlying_symbol: str,
    underlying_canonical_security_id: str,
    contracts_returned_by_provider: int,
    selection: StratifiedChainSelection,
    policy: ChainCoverageQualityPolicy,
) -> ChainCoverageDiagnostic:
    dte_available = tuple(
        sorted(
            item.key
            for item in selection.coverage_by_expiration_bucket
            if item.contracts_available > 0
        )
    )
    dte_selected = tuple(
        sorted(
            item.key
            for item in selection.coverage_by_expiration_bucket
            if item.contracts_selected > 0
        )
    )
    call_selected = next(
        (
            item.contracts_selected
            for item in selection.coverage_by_option_type
            if item.key == "CALL"
        ),
        0,
    )
    put_selected = next(
        (
            item.contracts_selected
            for item in selection.coverage_by_option_type
            if item.key == "PUT"
        ),
        0,
    )
    moneyness_selected = tuple(
        sorted(item.key for item in selection.coverage_by_moneyness if item.contracts_selected > 0)
    )
    excluded_fraction = (
        _exact(
            Decimal(selection.contracts_excluded_by_cap) / Decimal(selection.contracts_available)
        )
        if selection.contracts_available > 0
        else Decimal(0)
    )

    reasons: list[str] = []
    breadth_insufficient = (
        selection.expiration_count_selected < policy.minimum_expiration_count_selected
        or len(dte_selected) < policy.minimum_dte_buckets_selected
        or (policy.require_both_call_and_put and (call_selected == 0 or put_selected == 0))
    )
    if breadth_insufficient:
        quality = ChainCoverageQuality.INSUFFICIENT_COVERAGE
        if selection.expiration_count_selected < policy.minimum_expiration_count_selected:
            reasons.append("EXPIRATION_BREADTH_BELOW_MINIMUM")
        if len(dte_selected) < policy.minimum_dte_buckets_selected:
            reasons.append("DTE_BUCKET_BREADTH_BELOW_MINIMUM")
        if policy.require_both_call_and_put and (call_selected == 0 or put_selected == 0):
            reasons.append("MISSING_CALL_OR_PUT_REPRESENTATION")
    elif (
        selection.contracts_excluded_by_cap > 0
        and excluded_fraction >= policy.cap_pressure_threshold
    ):
        quality = ChainCoverageQuality.CAP_CONSTRAINED
        reasons.append("CAP_PRESSURE_AT_OR_ABOVE_THRESHOLD")
    elif selection.contracts_excluded_by_cap > 0:
        quality = ChainCoverageQuality.PARTIAL_COVERAGE
        reasons.append("SOME_ELIGIBLE_COVERAGE_EXCLUDED_BY_CAP_BELOW_PRESSURE_THRESHOLD")
    else:
        quality = ChainCoverageQuality.GOOD_COVERAGE
        reasons.append("FULL_ELIGIBLE_COVERAGE_RETAINED")

    return ChainCoverageDiagnostic(
        underlying_symbol=underlying_symbol,
        underlying_canonical_security_id=underlying_canonical_security_id,
        contracts_returned_by_provider=contracts_returned_by_provider,
        contracts_eligible_before_cap=selection.contracts_available,
        contracts_selected=selection.contracts_selected,
        contracts_excluded_by_cap=selection.contracts_excluded_by_cap,
        expiration_count_available=selection.expiration_count_available,
        expiration_count_selected=selection.expiration_count_selected,
        dte_buckets_available=dte_available,
        dte_buckets_selected=dte_selected,
        call_count_selected=call_selected,
        put_count_selected=put_selected,
        moneyness_buckets_selected=moneyness_selected,
        quality=quality,
        quality_reasons=tuple(reasons),
    )


# ==========================================================================
# Section 3: distinct-session history and longitudinal research readiness
# (milestone sections 12-16)
# ==========================================================================


class ResearchReadinessState(StrEnum):
    CURRENT_SNAPSHOT_AVAILABLE = "CURRENT_SNAPSHOT_AVAILABLE"
    FORWARD_HISTORY_ACCUMULATING = "FORWARD_HISTORY_ACCUMULATING"
    LONGITUDINAL_RESEARCH_READY = "LONGITUDINAL_RESEARCH_READY"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


@dataclass(frozen=True, slots=True)
class OptionsHistoryReadiness:
    underlying_canonical_security_id: str
    valid_snapshot_count: int
    distinct_session_count: int
    first_session_id: str | None
    latest_session_id: str | None
    minimum_required_sessions: int
    state: ResearchReadinessState
    reasons: tuple[str, ...]


def evaluate_options_history_readiness(
    *,
    underlying_canonical_security_id: str,
    session_ids: tuple[str, ...],
    minimum_required_sessions: int = MINIMUM_DISTINCT_SESSIONS_FOR_LONGITUDINAL_RESEARCH,
) -> OptionsHistoryReadiness:
    """``session_ids`` is one entry per snapshot the caller has already
    decided genuinely counts toward forward history -- a genuinely fresh
    capture from a distinct trading session, never a same-session replay,
    duplicate, or stale-reproduction snapshot (that exclusion is the
    caller's responsibility, mirroring exactly what the retained
    ``STALE_EVIDENCE_ACCEPTANCE_RESEARCH_POLICY_V1`` structures already
    demonstrate must never be counted as current). Distinct-session
    identity is the ``session_id`` string itself (an XNYS session date);
    two snapshots sharing one ``session_id`` count once, not twice.
    """

    if minimum_required_sessions < 1:
        raise OptionsResearchOperationsError("HISTORY_READINESS_MINIMUM_INVALID")
    distinct = tuple(sorted(set(session_ids)))
    valid_count = len(session_ids)
    if not distinct:
        return OptionsHistoryReadiness(
            underlying_canonical_security_id,
            0,
            0,
            None,
            None,
            minimum_required_sessions,
            ResearchReadinessState.INSUFFICIENT_HISTORY,
            ("NO_RETAINED_FORWARD_SNAPSHOT",),
        )
    if len(distinct) >= minimum_required_sessions:
        state = ResearchReadinessState.LONGITUDINAL_RESEARCH_READY
        reasons = (
            f"DISTINCT_SESSION_COUNT_{len(distinct)}_MEETS_MINIMUM_{minimum_required_sessions}",
        )
    elif len(distinct) == 1:
        state = ResearchReadinessState.CURRENT_SNAPSHOT_AVAILABLE
        reasons = ("EXACTLY_ONE_DISTINCT_SESSION_RETAINED",)
    else:
        state = ResearchReadinessState.FORWARD_HISTORY_ACCUMULATING
        reasons = (
            f"DISTINCT_SESSION_COUNT_{len(distinct)}_BELOW_MINIMUM_{minimum_required_sessions}",
        )
    return OptionsHistoryReadiness(
        underlying_canonical_security_id,
        valid_count,
        len(distinct),
        distinct[0],
        distinct[-1],
        minimum_required_sessions,
        state,
        reasons,
    )


# ==========================================================================
# Section 4: the retained daily longitudinal evidence checkpoint (milestone
# sections 14 and 25 -- combined into one retained fact, since the two
# field lists overlap almost entirely and this codebase prefers one
# well-designed immutable evidence row over several duplicating ones)
# ==========================================================================


@dataclass(frozen=True, slots=True)
class OptionsForwardEvidenceCheckpoint:
    checkpoint_id: str
    plan_id: str
    session_id: str
    capture_window_policy_id: str
    window_open_at: datetime
    window_close_at: datetime
    provider: str
    feed: str
    feed_entitlement_state: str
    underlying_symbols_captured: tuple[str, ...]
    underlyings_requested_count: int
    underlyings_captured_count: int
    fresh_snapshot_ids: tuple[str, ...]
    stale_snapshot_count: int
    quote_quality_counts: tuple[tuple[str, int], ...]
    chain_coverage_quality_counts: tuple[tuple[str, int], ...]
    cap_constrained_underlyings: tuple[str, ...]
    structure_snapshot_ids: tuple[str, ...]
    structures_evaluated: int
    structures_retained: int
    fee_policy_id: str
    fee_evidence_status: str
    opra_entitlement_state: str
    open_interest_limitation: str
    historical_pricing_limitation: str
    iv_history_minimum_required: int
    iv_history_ready_underlying_count: int
    forward_history_underlying_count: int
    errors: tuple[str, ...]
    limitations: tuple[str, ...]
    authority: str
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        session_id: str,
        capture_window_policy_id: str,
        window_open_at: datetime,
        window_close_at: datetime,
        provider: str,
        feed: str,
        feed_entitlement_state: str,
        underlying_symbols_captured: tuple[str, ...],
        underlyings_requested_count: int,
        underlyings_captured_count: int,
        fresh_snapshot_ids: tuple[str, ...],
        stale_snapshot_count: int,
        quote_quality_counts: tuple[tuple[str, int], ...],
        chain_coverage_quality_counts: tuple[tuple[str, int], ...],
        cap_constrained_underlyings: tuple[str, ...],
        structure_snapshot_ids: tuple[str, ...],
        structures_evaluated: int,
        structures_retained: int,
        fee_policy_id: str,
        fee_evidence_status: str,
        opra_entitlement_state: str,
        open_interest_limitation: str,
        iv_history_minimum_required: int,
        iv_history_ready_underlying_count: int,
        forward_history_underlying_count: int,
        created_at: datetime,
        errors: tuple[str, ...] = (),
        limitations: tuple[str, ...] = OPTIONS_RESEARCH_OPERATIONS_WARNINGS,
        historical_pricing_limitation: str = HISTORICAL_OPTIONS_PRICING_UNAVAILABLE,
    ) -> OptionsForwardEvidenceCheckpoint:
        _aware(window_open_at, "CHECKPOINT_WINDOW_OPEN_AT")
        _aware(window_close_at, "CHECKPOINT_WINDOW_CLOSE_AT")
        _aware(created_at, "CHECKPOINT_CREATED_AT")
        if underlyings_captured_count > underlyings_requested_count:
            raise OptionsResearchOperationsError("CHECKPOINT_CAPTURED_EXCEEDS_REQUESTED")
        if structures_retained > structures_evaluated:
            raise OptionsResearchOperationsError("CHECKPOINT_RETAINED_EXCEEDS_EVALUATED")
        # Identity is deliberately narrower than full content -- mirrors
        # ``OptionsChainSnapshot``'s identical "retained content but never
        # part of identity" design for its own history-dependent IV-rank
        # feature. ``chain_coverage_quality_counts``/``cap_constrained_
        # underlyings`` are excluded here for the same reason:
        # ``ChainCoverageDiagnostic`` is only recomputed for an underlying
        # actually freshly captured *this* invocation (never for one
        # reused from earlier in the same still-open session, since
        # recomputing it would require re-fetching from the provider,
        # defeating the point of reuse) -- so a poll that captures nothing
        # new but merely reuses everything already retained would otherwise
        # mint a spuriously "different" checkpoint on every replay, purely
        # because that poll's coverage tally came up empty. Every other
        # field here is a stable fact of which underlyings/structures are
        # genuinely retained, so it stays part of identity.
        payload = (
            OPTIONS_FORWARD_EVIDENCE_CHECKPOINT_V1,
            plan_id,
            session_id,
            capture_window_policy_id,
            window_open_at,
            window_close_at,
            provider,
            feed,
            feed_entitlement_state,
            tuple(sorted(underlying_symbols_captured)),
            underlyings_requested_count,
            underlyings_captured_count,
            tuple(sorted(fresh_snapshot_ids)),
            stale_snapshot_count,
            tuple(sorted(quote_quality_counts)),
            tuple(sorted(structure_snapshot_ids)),
            structures_evaluated,
            structures_retained,
            fee_policy_id,
            fee_evidence_status,
            opra_entitlement_state,
            open_interest_limitation,
            historical_pricing_limitation,
            iv_history_minimum_required,
            iv_history_ready_underlying_count,
            forward_history_underlying_count,
            tuple(sorted(set(errors))),
        )
        return cls(
            checkpoint_id=deterministic_id("options_forward_evidence_checkpoint", payload),
            plan_id=plan_id,
            session_id=session_id,
            capture_window_policy_id=capture_window_policy_id,
            window_open_at=window_open_at,
            window_close_at=window_close_at,
            provider=provider,
            feed=feed,
            feed_entitlement_state=feed_entitlement_state,
            underlying_symbols_captured=tuple(sorted(underlying_symbols_captured)),
            underlyings_requested_count=underlyings_requested_count,
            underlyings_captured_count=underlyings_captured_count,
            fresh_snapshot_ids=tuple(sorted(fresh_snapshot_ids)),
            stale_snapshot_count=stale_snapshot_count,
            quote_quality_counts=tuple(sorted(quote_quality_counts)),
            chain_coverage_quality_counts=tuple(sorted(chain_coverage_quality_counts)),
            cap_constrained_underlyings=tuple(sorted(cap_constrained_underlyings)),
            structure_snapshot_ids=tuple(sorted(structure_snapshot_ids)),
            structures_evaluated=structures_evaluated,
            structures_retained=structures_retained,
            fee_policy_id=fee_policy_id,
            fee_evidence_status=fee_evidence_status,
            opra_entitlement_state=opra_entitlement_state,
            open_interest_limitation=open_interest_limitation,
            historical_pricing_limitation=historical_pricing_limitation,
            iv_history_minimum_required=iv_history_minimum_required,
            iv_history_ready_underlying_count=iv_history_ready_underlying_count,
            forward_history_underlying_count=forward_history_underlying_count,
            errors=tuple(sorted(set(errors))),
            limitations=limitations,
            authority="NONE",
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 5: forward storage projection (milestone section 20)
# ==========================================================================


@dataclass(frozen=True, slots=True)
class StorageProjection:
    sessions: int
    observations_per_session: int
    projected_observations: int


def project_forward_storage(
    *, observations_per_session: int, sessions: tuple[int, ...] = (20, 60, 252)
) -> tuple[StorageProjection, ...]:
    if observations_per_session < 0:
        raise OptionsResearchOperationsError("STORAGE_PROJECTION_OBSERVATIONS_INVALID")
    if any(item < 1 for item in sessions):
        raise OptionsResearchOperationsError("STORAGE_PROJECTION_SESSION_COUNT_INVALID")
    return tuple(
        StorageProjection(item, observations_per_session, observations_per_session * item)
        for item in sessions
    )


# ==========================================================================
# Section 6: descriptive cross-sectional baseline (milestone section 18) --
# a baseline, never a hypothesis test; no "best trade" language
# ==========================================================================


@dataclass(frozen=True, slots=True)
class DescriptiveDistribution:
    metric: str
    count: int
    minimum: Decimal | None
    maximum: Decimal | None
    mean: Decimal | None
    median: Decimal | None


def _describe(metric: str, values: tuple[Decimal, ...]) -> DescriptiveDistribution:
    if not values:
        return DescriptiveDistribution(metric, 0, None, None, None, None)
    ordered = tuple(sorted(values))
    count = len(ordered)
    mean = _exact(sum(ordered, Decimal(0)) / Decimal(count))
    middle = count // 2
    median = (
        ordered[middle] if count % 2 == 1 else _exact((ordered[middle - 1] + ordered[middle]) / 2)
    )
    return DescriptiveDistribution(metric, count, ordered[0], ordered[-1], mean, median)


@dataclass(frozen=True, slots=True)
class OptionsCrossSectionalBaseline:
    baseline_id: str
    session_id: str
    underlying_count: int
    distributions: tuple[DescriptiveDistribution, ...]
    quality_counts: tuple[tuple[str, int], ...]
    authority: str
    created_at: datetime
    canonical_hash: str
    warnings: tuple[str, ...] = (
        "DESCRIPTIVE CROSS-SECTIONAL BASELINE ONLY -- NOT A HYPOTHESIS TEST",
        "NO 'BEST TRADE' OR RANKING LANGUAGE IS PRODUCED HERE",
        "ONE SESSION IS A CROSS-SECTIONAL SNAPSHOT, NOT LONGITUDINAL RESEARCH",
    )


def build_options_cross_sectional_baseline(
    *,
    session_id: str,
    created_at: datetime,
    underlying_count: int,
    metric_values: tuple[tuple[str, tuple[Decimal, ...]], ...],
    quality_counts: tuple[tuple[str, int], ...],
) -> OptionsCrossSectionalBaseline:
    _aware(created_at, "BASELINE_CREATED_AT")
    if underlying_count < 1:
        raise OptionsResearchOperationsError("BASELINE_UNDERLYING_COUNT_INVALID")
    distributions = tuple(_describe(name, values) for name, values in metric_values)
    payload = (
        OPTIONS_CROSS_SECTIONAL_BASELINE_V1,
        session_id,
        underlying_count,
        tuple(
            (item.metric, item.count, item.minimum, item.maximum, item.mean, item.median)
            for item in distributions
        ),
        tuple(sorted(quality_counts)),
        created_at,
    )
    return OptionsCrossSectionalBaseline(
        baseline_id=deterministic_id("options_cross_sectional_baseline", payload),
        session_id=session_id,
        underlying_count=underlying_count,
        distributions=distributions,
        quality_counts=tuple(sorted(quality_counts)),
        authority="NONE",
        created_at=created_at,
        canonical_hash=content_digest(payload),
    )
