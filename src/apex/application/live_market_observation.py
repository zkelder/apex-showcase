"""Application services for Live Market Observation Foundation v1.

``ExecuteLiveMarketObservationRound`` is Phase C's capture orchestration;
``ObtainContemporaneousMarketObservation`` is Phase D's decision-time hook.
Neither imports anything venue-specific beyond the narrow ``LiveQuoteSource``
Protocol below, which the already-existing ``CoinbasePublicUniverseSource``
(and its ``FixtureCoinbaseUniverseSource`` test double) already satisfy --
this milestone introduces no new provider adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.application.crypto_universe import CapturedProductBook
from apex.domain.live_market_observation import (
    MAX_LIVE_OBSERVATION_ROUND_INSTRUMENTS,
    ContemporaneousObservationResult,
    LiveMarketObservation,
    LiveMarketObservationError,
    LiveObservationCaptureRound,
    LiveObservationQuality,
    LiveObservationRoundStatus,
    LiveObservationType,
    evaluate_contemporaneous_observation,
    live_observation_round_id,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue

# The research-universe share still mirrors ``domain.crypto_market_pulse.
# MAX_PULSE_PRODUCTS`` (10) exactly; PAPER Trial Closure Readiness v1 adds a
# small fixed headroom for execution-required instruments not already in the
# universe (``domain.live_market_observation.
# compose_live_observation_round_instruments``), so they never displace a
# universe product. Still a small fixed bound -- well inside Coinbase's
# public rate limits at a 1-minute cadence, storage growth a known worst case.
MAX_LIVE_OBSERVATION_INSTRUMENTS_PER_ROUND = MAX_LIVE_OBSERVATION_ROUND_INSTRUMENTS
# Top-of-book only, deliberately shallow -- this milestone captures a
# continuous quote signal, not a depth-of-book research dataset.
LIVE_OBSERVATION_MAX_BOOK_DEPTH = 5


class LiveQuoteSource(Protocol):
    """The narrow subset of ``CoinbasePublicUniverseSource`` this module
    depends on. Structural typing only."""

    evidence_class: EvidenceClass

    def capture_product_book(
        self, *, product_id: str, maximum_depth: int, requested_at: datetime
    ) -> CapturedProductBook: ...


class LiveMarketObservationRepository(Protocol):
    def capture_round(self, round_id: str) -> LiveObservationCaptureRound | None: ...

    def save_capture_round(
        self, value: LiveObservationCaptureRound
    ) -> LiveObservationCaptureRound: ...

    def observation(self, observation_id: str) -> LiveMarketObservation | None: ...

    def save_observation(self, value: LiveMarketObservation) -> LiveMarketObservation: ...

    def observations_for_round(self, round_id: str) -> tuple[LiveMarketObservation, ...]: ...

    def latest_observation(
        self,
        *,
        venue: SharedVenue,
        instrument_id: str,
        observation_type: LiveObservationType,
        as_of: datetime,
    ) -> LiveMarketObservation | None: ...

    def observations_for_instrument_window(
        self,
        *,
        instrument_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[LiveMarketObservation, ...]:
        """Every retained observation for one instrument with
        ``captured_at`` inside ``[window_start, window_end]`` (inclusive
        both ends), regardless of venue, observation_type, quality, or
        evidence_class -- Trial Evidence Coverage Disclosure v1's own
        ``domain.strategy_paper_trial_evidence_coverage.
        evaluate_trial_evidence_coverage`` applies its own fail-closed
        eligibility filter to whatever this returns, mirroring how
        ``latest_observation`` above trusts its caller for venue/type
        scoping but ``evaluate_contemporaneous_observation`` independently
        re-validates freshness."""
        ...


@dataclass(frozen=True, slots=True)
class LiveObservationRoundResult:
    round: LiveObservationCaptureRound
    observations: tuple[LiveMarketObservation, ...]
    idempotent_replay: bool


class ExecuteLiveMarketObservationRound:
    """Bounded, foreground capture of top-of-book QUOTE evidence for a small,
    caller-supplied instrument list.

    Mirrors ``ExecuteCryptoMarketPulseRefresh``'s per-instrument isolation
    exactly (``application/crypto_market_pulse.py``): a single bad
    instrument's provider call is individually try/excepted into a
    FAILED-quality observation row, never an aborted round. A fully-down
    provider therefore produces an all-FAILED round rather than a raised
    exception -- the caller's worker loop needs no top-level try/except of
    its own to tolerate a temporary provider outage.

    Idempotent by construction: ``round_id`` is a pure function of
    ``(plan_id, window_start)`` (see ``domain.live_market_observation.
    live_observation_round_id``). A replay within the same observation
    window -- including across a crash/restart -- returns the already-
    retained round and observations without a second provider call.
    """

    def __init__(
        self, repository: LiveMarketObservationRepository, source: LiveQuoteSource
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(
        self,
        *,
        plan_id: str,
        venue: SharedVenue,
        provider: str,
        instrument_ids: tuple[str, ...],
        window_start: datetime,
        requested_at: datetime,
        provider_schema_version: str,
        source_identity: str,
        maximum_book_depth: int = LIVE_OBSERVATION_MAX_BOOK_DEPTH,
    ) -> LiveObservationRoundResult:
        if not instrument_ids:
            raise LiveMarketObservationError("LIVE_OBSERVATION_ROUND_INSTRUMENTS_REQUIRED")

        round_id = live_observation_round_id(plan_id=plan_id, window_start=window_start)
        existing_round = self._repository.capture_round(round_id)
        if existing_round is not None:
            return LiveObservationRoundResult(
                round=existing_round,
                observations=self._repository.observations_for_round(round_id),
                idempotent_replay=True,
            )

        bounded_instruments = instrument_ids[:MAX_LIVE_OBSERVATION_INSTRUMENTS_PER_ROUND]
        succeeded = 0
        failed = 0
        # Captured in memory only for now -- NOT saved yet. Each observation
        # row carries a foreign key to this round's own row, and these
        # tables are append-only/immutable (no UPDATE after the fact is
        # possible), so the parent round row must exist before any child
        # observation row is inserted. Mirrors the exact "persist the parent
        # before its children" fix from Options Capture Parent Persistence
        # v1 -- the same class of FK-ordering bug, in a new subsystem.
        observations: list[LiveMarketObservation] = []
        for instrument_id in bounded_instruments:
            observation = self._capture_one(
                round_id=round_id,
                venue=venue,
                provider=provider,
                instrument_id=instrument_id,
                requested_at=requested_at,
                provider_schema_version=provider_schema_version,
                source_identity=source_identity,
                maximum_book_depth=maximum_book_depth,
            )
            if observation.quality is LiveObservationQuality.PASSED:
                succeeded += 1
            else:
                failed += 1
            observations.append(observation)

        if failed == 0:
            status = LiveObservationRoundStatus.COMPLETED
        elif succeeded > 0:
            status = LiveObservationRoundStatus.PARTIAL
        else:
            status = LiveObservationRoundStatus.FAILED

        round_value = LiveObservationCaptureRound.create(
            plan_id=plan_id,
            venue=venue,
            provider=provider,
            window_start=window_start,
            requested_at=requested_at,
            completed_at=requested_at,
            status=status,
            requested_instrument_count=len(bounded_instruments),
            succeeded_count=succeeded,
            failed_count=failed,
            evidence_class=self._source.evidence_class,
        )
        assert round_value.round_id == round_id
        self._repository.save_capture_round(round_value)
        for observation in observations:
            self._repository.save_observation(observation)
        return LiveObservationRoundResult(
            round=round_value, observations=tuple(observations), idempotent_replay=False
        )

    def _capture_one(
        self,
        *,
        round_id: str,
        venue: SharedVenue,
        provider: str,
        instrument_id: str,
        requested_at: datetime,
        provider_schema_version: str,
        source_identity: str,
        maximum_book_depth: int,
    ) -> LiveMarketObservation:
        try:
            book = self._source.capture_product_book(
                product_id=instrument_id,
                maximum_depth=maximum_book_depth,
                requested_at=requested_at,
            )
        # Deliberately broad: ``self._source`` is a ``LiveQuoteSource``
        # Protocol, not a fixed adapter, so this module never assumes which
        # exception vocabulary a given implementation raises on failure --
        # narrowing to specific types (as an earlier revision of this method
        # did, with (OSError, RuntimeError, ValueError)) both missed a real
        # CPython gap (``http.client.HTTPException``/``IncompleteRead`` are
        # NOT ``OSError`` subclasses) and would need updating for every new
        # provider's own exception types. This wraps exactly one external
        # call, so a broad catch here isolates any provider-side failure
        # into a FAILED-quality row for this one instrument without masking
        # bugs anywhere else in the round. Also keeps this file inside the
        # runtime capability-import boundary (``tests.security.
        # test_architecture_boundaries``): importing ``http.client`` here to
        # name it in an except-tuple would itself be a violation, since
        # network-capable modules are reserved for adapter files only.
        except Exception as error:
            return LiveMarketObservation.create(
                round_id=round_id,
                venue=venue,
                provider=provider,
                instrument_id=instrument_id,
                observation_type=LiveObservationType.QUOTE,
                captured_at=requested_at,
                evidence_class=self._source.evidence_class,
                provider_schema_version=provider_schema_version,
                source_identity=source_identity,
                quality=LiveObservationQuality.FAILED,
                quality_reasons=(f"PROVIDER_{type(error).__name__}",),
            )
        return LiveMarketObservation.create(
            round_id=round_id,
            venue=venue,
            provider=provider,
            instrument_id=instrument_id,
            observation_type=LiveObservationType.QUOTE,
            captured_at=book.captured_at,
            bid_price=book.best_bid,
            ask_price=book.best_ask,
            book_depth_bid=book.visible_bid_depth,
            book_depth_ask=book.visible_ask_depth,
            evidence_class=self._source.evidence_class,
            provider_schema_version=provider_schema_version,
            source_identity=source_identity,
            quality=LiveObservationQuality.PASSED,
        )


class ObtainContemporaneousMarketObservation:
    """Phase D decision-time capture hook.

    A future strategy/PAPER decision path calls this to obtain (and
    implicitly reference, via ``result.observation.observation_id``) the
    exact retained evidence supporting a decision -- reusing this codebase's
    existing "opaque evidence-anchor id" doctrine (e.g. ``application.
    interfaces.DecisionInputs.market_observation_source_id``) rather than
    inventing a parallel one. Never mutates retained evidence and never
    places an order; this module has no execution capability at all.

    Live Observation Point-in-Time Race Fix v1: ``as_of`` is passed through
    to the repository's own ``latest_observation`` lookup, not just to the
    domain staleness gate below -- a caller's ``as_of`` is frozen once (e.g.
    at the start of an operating cycle's sequential task dispatch) while an
    independently-scheduled live-observer worker keeps writing fresh rows in
    real time, so by the time this executes, a genuinely later-``captured_at``
    row can already exist. Bounding the repository query itself selects the
    latest row that was actually valid AT ``as_of`` instead of surfacing that
    future row and tripping ``evaluate_contemporaneous_observation``'s
    fail-closed ``LIVE_OBSERVATION_FUTURE_TIMESTAMP`` guard on a real,
    already-available past observation.
    """

    def __init__(self, repository: LiveMarketObservationRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        venue: SharedVenue,
        instrument_id: str,
        observation_type: LiveObservationType,
        as_of: datetime,
        max_staleness_seconds: int,
    ) -> ContemporaneousObservationResult:
        candidate = self._repository.latest_observation(
            venue=venue, instrument_id=instrument_id, observation_type=observation_type, as_of=as_of
        )
        return evaluate_contemporaneous_observation(
            candidate=candidate, as_of=as_of, max_staleness_seconds=max_staleness_seconds
        )
