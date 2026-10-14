"""Live Market Observation Foundation v1.

Durable, provider-agnostic evidence of contemporaneous market state, captured
on an intraday cadence -- distinct from every existing capture path in this
codebase, which is scheduled *snapshot* evidence (once/day for CRYPTO market
pulse, once/7-days for the CRYPTO universe catalog, once/trading-session for
SECURITIES options, by deliberate research-design choice in that last case).

Two retained shapes, mirroring ``domain.crypto_market_pulse``'s
refresh/checkpoint split -- the closest existing structural analog, because
this module shares its exact bounded-round-over-many-instruments shape:

* ``LiveObservationCaptureRound`` -- one row per bounded capture round (one
  worker tick over a bounded instrument list), with round-level summary
  counts. Mirrors ``CryptoMarketPulseRefresh``.
* ``LiveMarketObservation`` -- one row per ``(round_id, instrument_id,
  observation_type)``, mirroring ``CryptoPulseCheckpoint``.

Deliberately narrow: this module owns retained observation evidence only. It
grants no capital or execution authority (``authority`` is always
``"NONE"``), places no order, and creates no Opportunity, Decision, or PAPER
state. A future strategy/PAPER decision path references a retained
observation only by its ``observation_id`` string -- the same "opaque
evidence-anchor id" doctrine this codebase already uses elsewhere (e.g.
``application.interfaces.DecisionInputs.market_observation_source_id``) --
never by importing this module's types into that decision path.

``EvidenceClass`` (genuine vs. synthetic/fixture) and ``deterministic_id``
(idempotent identity) are reused exactly as declared elsewhere in this
codebase; this module invents neither.

Deliberately excluded from this milestone's schema, per the "do not invent a
giant universal tick schema" instruction: option-specific quote/structure
fields (no SECURITIES provider is activated against this substrate yet -- see
this module's own activation code for the documented capability gap) and a
multi-level order book (top-of-book depth only; a deeper book already has its
own established home in ``domain.public_threshold.PublicOrderbookSnapshot``
for KALSHI and can be composed with this substrate later without reworking
it). ``LiveObservationType.TRADE``/``BAR`` are declared now, schema-ready, so
a later milestone can populate them without a migration -- but this
milestone's own activation (Phase C) only ever produces ``QUOTE`` rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.values import canonical_timestamp, content_digest, deterministic_id

LIVE_MARKET_OBSERVATION_V1 = "LIVE_MARKET_OBSERVATION_V1"
LIVE_MARKET_OBSERVATION_WARNING = (
    "LIVE MARKET OBSERVATION — RESEARCH EVIDENCE ONLY — NO CAPITAL OR EXECUTION AUTHORITY"
)


class LiveMarketObservationError(ValueError):
    """A live-market-observation invariant was violated."""


class LiveObservationType(StrEnum):
    QUOTE = "QUOTE"
    TRADE = "TRADE"
    BAR = "BAR"


class LiveObservationQuality(StrEnum):
    """Mirrors ``domain.crypto_market_pulse.PulseQuality`` exactly: a binary
    gate plus named reasons, never an opaque score."""

    PASSED = "PASSED"
    FAILED = "FAILED"


class LiveObservationRoundStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ContemporaneousObservationDisposition(StrEnum):
    """The Phase D decision-time capability's outcome. Fail-closed: only
    ``SATISFIED`` carries an observation a caller may act on."""

    SATISFIED = "SATISFIED"
    STALE = "STALE"
    ABSENT = "ABSENT"


def live_observation_round_id(*, plan_id: str, window_start: datetime) -> str:
    """Pure identity formula, factored out of ``LiveObservationCaptureRound.
    create`` so a caller can compute the id an in-progress round *would* have
    (to check for an already-completed replay) without constructing and
    discarding a throwaway round object first."""

    return deterministic_id(
        "live_market_observation_round", (plan_id, canonical_timestamp(window_start))
    )


@dataclass(frozen=True, slots=True)
class LiveMarketObservation:
    observation_id: str
    round_id: str
    venue: SharedVenue
    provider: str
    instrument_id: str
    observation_type: LiveObservationType
    captured_at: datetime
    provider_event_at: datetime | None
    session_context: str | None
    bid_price: Decimal | None
    ask_price: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    last_trade_price: Decimal | None
    bar_open: Decimal | None
    bar_high: Decimal | None
    bar_low: Decimal | None
    bar_close: Decimal | None
    bar_volume: Decimal | None
    bar_interval_seconds: int | None
    book_depth_bid: Decimal | None
    book_depth_ask: Decimal | None
    evidence_class: EvidenceClass
    provider_schema_version: str
    source_identity: str
    quality: LiveObservationQuality
    quality_reasons: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        round_id: str,
        venue: SharedVenue,
        provider: str,
        instrument_id: str,
        observation_type: LiveObservationType,
        captured_at: datetime,
        provider_event_at: datetime | None = None,
        session_context: str | None = None,
        bid_price: Decimal | None = None,
        ask_price: Decimal | None = None,
        bid_size: Decimal | None = None,
        ask_size: Decimal | None = None,
        last_trade_price: Decimal | None = None,
        bar_open: Decimal | None = None,
        bar_high: Decimal | None = None,
        bar_low: Decimal | None = None,
        bar_close: Decimal | None = None,
        bar_volume: Decimal | None = None,
        bar_interval_seconds: int | None = None,
        book_depth_bid: Decimal | None = None,
        book_depth_ask: Decimal | None = None,
        evidence_class: EvidenceClass,
        provider_schema_version: str,
        source_identity: str,
        quality: LiveObservationQuality,
        quality_reasons: tuple[str, ...] = (),
    ) -> LiveMarketObservation:
        if not instrument_id:
            raise LiveMarketObservationError("LIVE_OBSERVATION_INSTRUMENT_ID_REQUIRED")
        if not provider:
            raise LiveMarketObservationError("LIVE_OBSERVATION_PROVIDER_REQUIRED")
        if not provider_schema_version:
            raise LiveMarketObservationError("LIVE_OBSERVATION_SCHEMA_VERSION_REQUIRED")
        if not source_identity:
            raise LiveMarketObservationError("LIVE_OBSERVATION_SOURCE_IDENTITY_REQUIRED")
        # Validated for tz-awareness but deliberately excluded from
        # canonical_hash below: a crash-resume replay of the identical
        # logical observation at a different wall-clock instant must not
        # manufacture a spurious identity conflict (mirrors
        # ``KalshiBaselineQuoteEvidence.captured_at`` exactly).
        canonical_timestamp(captured_at)
        event_stamp = None if provider_event_at is None else canonical_timestamp(provider_event_at)
        if bid_price is not None and ask_price is not None and bid_price > ask_price:
            raise LiveMarketObservationError("LIVE_OBSERVATION_BID_EXCEEDS_ASK")
        for label, value in (
            ("bid_size", bid_size),
            ("ask_size", ask_size),
            ("bar_volume", bar_volume),
            ("book_depth_bid", book_depth_bid),
            ("book_depth_ask", book_depth_ask),
        ):
            if value is not None and value < 0:
                raise LiveMarketObservationError(f"LIVE_OBSERVATION_{label.upper()}_NEGATIVE")
        bar_fields = (bar_open, bar_high, bar_low, bar_close)
        if observation_type is LiveObservationType.BAR:
            if any(field is None for field in bar_fields) or bar_interval_seconds is None:
                raise LiveMarketObservationError("LIVE_OBSERVATION_BAR_FIELDS_INCOMPLETE")
            if bar_interval_seconds <= 0:
                raise LiveMarketObservationError("LIVE_OBSERVATION_BAR_INTERVAL_INVALID")
        if any(field is not None for field in bar_fields):
            assert bar_open is not None
            assert bar_high is not None
            assert bar_low is not None
            assert bar_close is not None
            if bar_high < max(bar_open, bar_close) or bar_low > min(bar_open, bar_close):
                raise LiveMarketObservationError("LIVE_OBSERVATION_BAR_OHLC_INCONSISTENT")
            if bar_high < bar_low:
                raise LiveMarketObservationError("LIVE_OBSERVATION_BAR_HIGH_BELOW_LOW")
        if quality is LiveObservationQuality.FAILED and not quality_reasons:
            raise LiveMarketObservationError("LIVE_OBSERVATION_FAILED_QUALITY_REQUIRES_REASONS")

        observation_id = deterministic_id(
            "live_market_observation",
            (round_id, instrument_id, observation_type.value),
        )
        canonical_payload = (
            LIVE_MARKET_OBSERVATION_V1,
            round_id,
            venue.value,
            provider,
            instrument_id,
            observation_type.value,
            event_stamp,
            session_context,
            None if bid_price is None else str(bid_price),
            None if ask_price is None else str(ask_price),
            None if bid_size is None else str(bid_size),
            None if ask_size is None else str(ask_size),
            None if last_trade_price is None else str(last_trade_price),
            None if bar_open is None else str(bar_open),
            None if bar_high is None else str(bar_high),
            None if bar_low is None else str(bar_low),
            None if bar_close is None else str(bar_close),
            None if bar_volume is None else str(bar_volume),
            bar_interval_seconds,
            None if book_depth_bid is None else str(book_depth_bid),
            None if book_depth_ask is None else str(book_depth_ask),
            evidence_class.value,
            provider_schema_version,
            source_identity,
            quality.value,
            tuple(sorted(quality_reasons)),
        )
        return cls(
            observation_id=observation_id,
            round_id=round_id,
            venue=venue,
            provider=provider,
            instrument_id=instrument_id,
            observation_type=observation_type,
            captured_at=captured_at,
            provider_event_at=provider_event_at,
            session_context=session_context,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
            last_trade_price=last_trade_price,
            bar_open=bar_open,
            bar_high=bar_high,
            bar_low=bar_low,
            bar_close=bar_close,
            bar_volume=bar_volume,
            bar_interval_seconds=bar_interval_seconds,
            book_depth_bid=book_depth_bid,
            book_depth_ask=book_depth_ask,
            evidence_class=evidence_class,
            provider_schema_version=provider_schema_version,
            source_identity=source_identity,
            quality=quality,
            quality_reasons=tuple(sorted(quality_reasons)),
            canonical_hash=content_digest(canonical_payload),
            authority="NONE",
        )


@dataclass(frozen=True, slots=True)
class LiveObservationCaptureRound:
    """One bounded worker tick over a bounded instrument list. Deliberately
    excludes ``captured_at``-shaped wall-clock fields from its own identity
    the same way ``LiveMarketObservation`` does: ``round_id`` is a pure
    function of ``(plan_id, window_start)`` so a crash-resume replay within
    the same observation-window-minute reuses the identical round rather than
    manufacturing a duplicate."""

    round_id: str
    plan_id: str
    venue: SharedVenue
    provider: str
    window_start: datetime
    requested_at: datetime
    completed_at: datetime | None
    status: LiveObservationRoundStatus
    requested_instrument_count: int
    succeeded_count: int
    failed_count: int
    evidence_class: EvidenceClass
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        plan_id: str,
        venue: SharedVenue,
        provider: str,
        window_start: datetime,
        requested_at: datetime,
        completed_at: datetime | None,
        status: LiveObservationRoundStatus,
        requested_instrument_count: int,
        succeeded_count: int,
        failed_count: int,
        evidence_class: EvidenceClass,
    ) -> LiveObservationCaptureRound:
        if requested_instrument_count < 0 or succeeded_count < 0 or failed_count < 0:
            raise LiveMarketObservationError("LIVE_OBSERVATION_ROUND_COUNT_NEGATIVE")
        if succeeded_count + failed_count > requested_instrument_count:
            raise LiveMarketObservationError("LIVE_OBSERVATION_ROUND_COUNT_EXCEEDS_REQUESTED")
        window_stamp = canonical_timestamp(window_start)
        round_id = live_observation_round_id(plan_id=plan_id, window_start=window_start)
        canonical_payload = (
            LIVE_MARKET_OBSERVATION_V1,
            plan_id,
            venue.value,
            provider,
            window_stamp,
            status.value,
            requested_instrument_count,
            succeeded_count,
            failed_count,
            evidence_class.value,
        )
        return cls(
            round_id=round_id,
            plan_id=plan_id,
            venue=venue,
            provider=provider,
            window_start=window_start,
            requested_at=requested_at,
            completed_at=completed_at,
            status=status,
            requested_instrument_count=requested_instrument_count,
            succeeded_count=succeeded_count,
            failed_count=failed_count,
            evidence_class=evidence_class,
            canonical_hash=content_digest(canonical_payload),
            authority="NONE",
        )


@dataclass(frozen=True, slots=True)
class ContemporaneousObservationResult:
    """The Phase D decision-time capability's return value. When
    ``disposition`` is ``SATISFIED``, ``observation.observation_id`` is the
    exact evidence-anchor id a future decision retains/references -- this
    module never invents a separate anchor-id shape."""

    disposition: ContemporaneousObservationDisposition
    observation: LiveMarketObservation | None
    staleness_seconds: int | None
    reasons: tuple[str, ...]


def evaluate_contemporaneous_observation(
    *,
    candidate: LiveMarketObservation | None,
    as_of: datetime,
    max_staleness_seconds: int,
) -> ContemporaneousObservationResult:
    """Pure fail-closed staleness gate: abstain (``ABSENT``/``STALE``) unless
    a genuinely fresh, quality-passed observation is retained. Never infers,
    extrapolates, or widens the bound on a caller's behalf."""

    if max_staleness_seconds <= 0:
        raise LiveMarketObservationError("LIVE_OBSERVATION_STALENESS_BOUND_INVALID")
    if candidate is None:
        return ContemporaneousObservationResult(
            disposition=ContemporaneousObservationDisposition.ABSENT,
            observation=None,
            staleness_seconds=None,
            reasons=("NO_RETAINED_OBSERVATION",),
        )
    if candidate.quality is LiveObservationQuality.FAILED:
        return ContemporaneousObservationResult(
            disposition=ContemporaneousObservationDisposition.ABSENT,
            observation=None,
            staleness_seconds=None,
            reasons=("LATEST_OBSERVATION_FAILED_QUALITY_GATE",),
        )
    age_seconds = (as_of - candidate.captured_at).total_seconds()
    if age_seconds < 0:
        raise LiveMarketObservationError("LIVE_OBSERVATION_FUTURE_TIMESTAMP")
    if age_seconds > max_staleness_seconds:
        return ContemporaneousObservationResult(
            disposition=ContemporaneousObservationDisposition.STALE,
            observation=None,
            staleness_seconds=int(age_seconds),
            reasons=("OBSERVATION_EXCEEDS_STALENESS_BOUND",),
        )
    return ContemporaneousObservationResult(
        disposition=ContemporaneousObservationDisposition.SATISFIED,
        observation=candidate,
        staleness_seconds=int(age_seconds),
        reasons=(),
    )


# ---- PAPER Trial Closure Readiness v1: execution-required instruments ----
#
# The unattended live observer originally captured exactly the latest
# retained CRYPTO universe's eligible products. That universe is a RESEARCH
# selection (one representative route per canonical base asset, re-ranked by
# 24h volume on every refresh), so a refresh can legitimately swap BTC-USD
# for BTC-USDC -- and did, on 2026-09-22 -- while an open strategy PAPER
# position is pinned to BTC-USD. Every round kept reporting COMPLETED 10/10
# while the one instrument PAPER execution actually depends on silently
# stopped being observed.
#
# The invariant these two functions establish: every round's requested
# instrument list ALWAYS contains every instrument a present PAPER
# execution/trial lifecycle depends on, ahead of (and never displaced by)
# the research universe. Because a required instrument is part of the
# requested set, any failure to observe it is counted in the round's own
# ``failed_count``, so a round can only be ``COMPLETED`` if every required
# instrument was genuinely observed -- "COMPLETED" can no longer mean
# "research universe healthy, execution evidence absent".

#: The research-universe share of a round -- identical to the original
#: per-round bound (``domain.crypto_market_pulse.MAX_PULSE_PRODUCTS``).
MAX_LIVE_OBSERVATION_UNIVERSE_INSTRUMENTS = 10
#: Headroom reserved for execution-required instruments that are NOT already
#: in the universe, so they never displace a universe product. Today's
#: strategy PAPER catalog targets exactly one instrument.
MAX_LIVE_OBSERVATION_EXECUTION_REQUIRED_HEADROOM = 2
MAX_LIVE_OBSERVATION_ROUND_INSTRUMENTS = (
    MAX_LIVE_OBSERVATION_UNIVERSE_INSTRUMENTS + MAX_LIVE_OBSERVATION_EXECUTION_REQUIRED_HEADROOM
)


class ExecutionObservationDependency(StrEnum):
    """Why a present PAPER lifecycle needs contemporaneous evidence for an
    instrument. Only lifecycles that genuinely consume ``LiveMarketObservation``
    evidence (the strategy PAPER path: placement, close, fill) are sources."""

    ACTIVE_STRATEGY_PAPER_TRIAL = "ACTIVE_STRATEGY_PAPER_TRIAL"
    OPEN_PAPER_ORDER = "OPEN_PAPER_ORDER"
    OPEN_PAPER_POSITION = "OPEN_PAPER_POSITION"
    STRATEGY_PAPER_PLACEMENT_TARGET = "STRATEGY_PAPER_PLACEMENT_TARGET"


@dataclass(frozen=True, slots=True)
class ExecutionRequiredInstrument:
    instrument_id: str
    dependencies: tuple[ExecutionObservationDependency, ...]


def execution_required_live_instruments(
    *,
    active_trial_instrument_ids: tuple[str, ...],
    open_order_instrument_ids: tuple[str, ...],
    open_position_instrument_ids: tuple[str, ...],
    placement_target_instrument_ids: tuple[str, ...],
) -> tuple[ExecutionRequiredInstrument, ...]:
    """Pure, deterministic: the exact instruments present PAPER execution
    depends on, each with every dependency that requires it, sorted by
    instrument id. An instrument with no present dependency is absent --
    nothing is retained "forever" merely because it was once traded."""

    sources = (
        (ExecutionObservationDependency.ACTIVE_STRATEGY_PAPER_TRIAL, active_trial_instrument_ids),
        (ExecutionObservationDependency.OPEN_PAPER_ORDER, open_order_instrument_ids),
        (ExecutionObservationDependency.OPEN_PAPER_POSITION, open_position_instrument_ids),
        (
            ExecutionObservationDependency.STRATEGY_PAPER_PLACEMENT_TARGET,
            placement_target_instrument_ids,
        ),
    )
    by_instrument: dict[str, list[ExecutionObservationDependency]] = {}
    for dependency, instrument_ids in sources:
        for instrument_id in instrument_ids:
            if not instrument_id or instrument_id != instrument_id.strip():
                raise LiveMarketObservationError("LIVE_OBSERVATION_REQUIRED_INSTRUMENT_INVALID")
            reasons = by_instrument.setdefault(instrument_id, [])
            if dependency not in reasons:
                reasons.append(dependency)
    return tuple(
        ExecutionRequiredInstrument(instrument_id=key, dependencies=tuple(by_instrument[key]))
        for key in sorted(by_instrument)
    )


def compose_live_observation_round_instruments(
    *, required: tuple[str, ...], universe: tuple[str, ...]
) -> tuple[str, ...]:
    """The exact, deduplicated instrument list for one round: every
    execution-required instrument first (never truncated), then the research
    universe's own first ``MAX_LIVE_OBSERVATION_UNIVERSE_INSTRUMENTS``
    products that are not already present (exact-id dedupe, so an instrument
    in both lists is requested exactly once). Fails loudly rather than
    silently dropping a required instrument when required alone would exceed
    the round bound."""

    ordered_required = tuple(dict.fromkeys(required))
    if len(ordered_required) > MAX_LIVE_OBSERVATION_ROUND_INSTRUMENTS:
        raise LiveMarketObservationError("LIVE_OBSERVATION_REQUIRED_INSTRUMENTS_EXCEED_ROUND_BOUND")
    seen = set(ordered_required)
    composed = list(ordered_required)
    for instrument_id in tuple(dict.fromkeys(universe))[:MAX_LIVE_OBSERVATION_UNIVERSE_INSTRUMENTS]:
        if instrument_id not in seen and len(composed) < MAX_LIVE_OBSERVATION_ROUND_INSTRUMENTS:
            seen.add(instrument_id)
            composed.append(instrument_id)
    return tuple(composed)
