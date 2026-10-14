from __future__ import annotations

import http.client
from datetime import UTC, datetime, timedelta

import pytest

from apex.adapters.coinbase_universe_public import FixtureCoinbaseUniverseSource
from apex.application.live_market_observation import (
    MAX_LIVE_OBSERVATION_INSTRUMENTS_PER_ROUND,
    ExecuteLiveMarketObservationRound,
    ObtainContemporaneousMarketObservation,
)
from apex.domain.live_market_observation import (
    ContemporaneousObservationDisposition,
    LiveMarketObservation,
    LiveMarketObservationError,
    LiveObservationCaptureRound,
    LiveObservationQuality,
    LiveObservationRoundStatus,
    LiveObservationType,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD")


class InMemoryLiveMarketObservationRepository:
    def __init__(self) -> None:
        self._rounds: dict[str, LiveObservationCaptureRound] = {}
        self._observations: dict[str, LiveMarketObservation] = {}

    def capture_round(self, round_id: str) -> LiveObservationCaptureRound | None:
        return self._rounds.get(round_id)

    def save_capture_round(self, value: LiveObservationCaptureRound) -> LiveObservationCaptureRound:
        existing = self._rounds.get(value.round_id)
        if existing is not None:
            if existing.canonical_hash != value.canonical_hash:
                raise LiveMarketObservationError("LIVE_OBSERVATION_ROUND_IDENTITY_CONFLICT")
            return existing
        self._rounds[value.round_id] = value
        return value

    def observation(self, observation_id: str) -> LiveMarketObservation | None:
        return self._observations.get(observation_id)

    def save_observation(self, value: LiveMarketObservation) -> LiveMarketObservation:
        existing = self._observations.get(value.observation_id)
        if existing is not None:
            if existing.canonical_hash != value.canonical_hash:
                raise LiveMarketObservationError("LIVE_OBSERVATION_IDENTITY_CONFLICT")
            return existing
        self._observations[value.observation_id] = value
        return value

    def observations_for_round(self, round_id: str) -> tuple[LiveMarketObservation, ...]:
        return tuple(
            sorted(
                (item for item in self._observations.values() if item.round_id == round_id),
                key=lambda item: item.instrument_id,
            )
        )

    def latest_observation(
        self,
        *,
        venue: SharedVenue,
        instrument_id: str,
        observation_type: LiveObservationType,
        as_of: datetime,
    ) -> LiveMarketObservation | None:
        # Point-in-time bound, mirroring PostgresLiveMarketObservationRepository
        # exactly: never surface a row captured after the caller's own as_of,
        # even if a later-captured row is otherwise retained. Ties on
        # captured_at break on observation_id, matching the Postgres
        # implementation's deterministic tie-breaker.
        candidates = [
            item
            for item in self._observations.values()
            if item.venue is venue
            and item.instrument_id == instrument_id
            and item.observation_type is observation_type
            and item.captured_at <= as_of
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.captured_at, item.observation_id))

    def observations_for_instrument_window(
        self,
        *,
        instrument_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[LiveMarketObservation, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._observations.values()
                    if item.instrument_id == instrument_id
                    and window_start <= item.captured_at <= window_end
                ),
                key=lambda item: (item.captured_at, item.observation_id),
            )
        )


class _CountingQuoteSource:
    evidence_class = EvidenceClass.FIXTURE

    def __init__(self) -> None:
        self._delegate = FixtureCoinbaseUniverseSource()
        self.call_count = 0

    def capture_product_book(self, *, product_id: str, maximum_depth: int, requested_at: datetime):
        self.call_count += 1
        return self._delegate.capture_product_book(
            product_id=product_id, maximum_depth=maximum_depth, requested_at=requested_at
        )


class _FlakyQuoteSource:
    """Delegates to FixtureCoinbaseUniverseSource but raises for one
    designated product id -- proves per-instrument failure isolation."""

    evidence_class = EvidenceClass.FIXTURE

    def __init__(self, *, failing_product_id: str) -> None:
        self._delegate = FixtureCoinbaseUniverseSource()
        self._failing_product_id = failing_product_id

    def capture_product_book(self, *, product_id: str, maximum_depth: int, requested_at: datetime):
        if product_id == self._failing_product_id:
            raise OSError("simulated provider outage for this instrument")
        return self._delegate.capture_product_book(
            product_id=product_id, maximum_depth=maximum_depth, requested_at=requested_at
        )


class _HttpProtocolFlakyQuoteSource:
    """Delegates to FixtureCoinbaseUniverseSource but raises a raw
    ``http.client.HTTPException`` (e.g. a truncated/chunked-encoding
    response) for one designated product id. ``http.client.IncompleteRead``
    and its siblings are NOT ``OSError`` subclasses -- a real, reachable
    CPython ``http.client`` failure mode distinct from the connection-level
    failures ``OSError`` covers (refused/reset/timeout). Proves per-instrument
    isolation holds for this exception family too, not only for ``OSError``."""

    evidence_class = EvidenceClass.FIXTURE

    def __init__(self, *, failing_product_id: str) -> None:
        self._delegate = FixtureCoinbaseUniverseSource()
        self._failing_product_id = failing_product_id

    def capture_product_book(self, *, product_id: str, maximum_depth: int, requested_at: datetime):
        if product_id == self._failing_product_id:
            raise http.client.IncompleteRead(b"")
        return self._delegate.capture_product_book(
            product_id=product_id, maximum_depth=maximum_depth, requested_at=requested_at
        )


class _AlwaysFailingQuoteSource:
    evidence_class = EvidenceClass.FIXTURE

    def capture_product_book(self, *, product_id: str, maximum_depth: int, requested_at: datetime):
        raise OSError("simulated full provider outage")


def test_execute_captures_passed_quote_observations_for_each_instrument() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())

    result = service.execute(
        plan_id="live-plan-test",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=PRODUCTS,
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    assert result.round.status is LiveObservationRoundStatus.COMPLETED
    assert result.round.succeeded_count == len(PRODUCTS)
    assert result.round.failed_count == 0
    assert not result.idempotent_replay
    assert len(result.observations) == len(PRODUCTS)
    assert all(item.quality is LiveObservationQuality.PASSED for item in result.observations)
    assert all(
        item.bid_price is not None and item.ask_price is not None for item in result.observations
    )
    assert all(item.evidence_class is EvidenceClass.FIXTURE for item in result.observations)
    assert {item.instrument_id for item in result.observations} == set(PRODUCTS)


def test_execute_isolates_one_failing_instrument_without_aborting_the_round() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    source = _FlakyQuoteSource(failing_product_id="ETH-USD")
    service = ExecuteLiveMarketObservationRound(repository, source)

    result = service.execute(
        plan_id="live-plan-test",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=PRODUCTS,
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    assert result.round.status is LiveObservationRoundStatus.PARTIAL
    assert result.round.succeeded_count == 2
    assert result.round.failed_count == 1
    by_instrument = {item.instrument_id: item for item in result.observations}
    assert by_instrument["ETH-USD"].quality is LiveObservationQuality.FAILED
    assert by_instrument["ETH-USD"].quality_reasons == ("PROVIDER_OSError",)
    assert by_instrument["ETH-USD"].bid_price is None
    assert by_instrument["BTC-USD"].quality is LiveObservationQuality.PASSED
    assert by_instrument["SOL-USD"].quality is LiveObservationQuality.PASSED


def test_execute_isolates_one_instrument_that_raises_a_raw_http_protocol_exception() -> None:
    """``http.client.IncompleteRead`` (and ``HTTPException`` generally) are
    NOT ``OSError`` subclasses -- a genuinely reachable CPython failure mode
    for a truncated/chunked-encoding response, distinct from the
    connection-level failures ``OSError`` already covers. Isolation must hold
    for this family too, not only for ``OSError``/``RuntimeError``/
    ``ValueError``."""
    repository = InMemoryLiveMarketObservationRepository()
    source = _HttpProtocolFlakyQuoteSource(failing_product_id="ETH-USD")
    service = ExecuteLiveMarketObservationRound(repository, source)

    result = service.execute(
        plan_id="live-plan-test",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=PRODUCTS,
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    assert result.round.status is LiveObservationRoundStatus.PARTIAL
    assert result.round.succeeded_count == 2
    assert result.round.failed_count == 1
    by_instrument = {item.instrument_id: item for item in result.observations}
    assert by_instrument["ETH-USD"].quality is LiveObservationQuality.FAILED
    assert by_instrument["ETH-USD"].quality_reasons == ("PROVIDER_IncompleteRead",)
    assert by_instrument["BTC-USD"].quality is LiveObservationQuality.PASSED
    assert by_instrument["SOL-USD"].quality is LiveObservationQuality.PASSED


def test_execute_tolerates_a_fully_down_provider_without_raising() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    service = ExecuteLiveMarketObservationRound(repository, _AlwaysFailingQuoteSource())

    result = service.execute(
        plan_id="live-plan-test",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=PRODUCTS,
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    assert result.round.status is LiveObservationRoundStatus.FAILED
    assert result.round.succeeded_count == 0
    assert result.round.failed_count == len(PRODUCTS)
    assert all(item.quality is LiveObservationQuality.FAILED for item in result.observations)


def test_execute_is_idempotent_within_the_same_observation_window() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    source = _CountingQuoteSource()
    service = ExecuteLiveMarketObservationRound(repository, source)
    kwargs = dict(
        plan_id="live-plan-test",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=PRODUCTS,
        window_start=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    first = service.execute(requested_at=NOW, **kwargs)
    assert not first.idempotent_replay
    assert source.call_count == len(PRODUCTS)

    replay = service.execute(requested_at=NOW + timedelta(seconds=20), **kwargs)
    assert replay.idempotent_replay
    assert replay.round == first.round
    assert replay.observations == first.observations
    # No second round of provider calls for a replay within the same window.
    assert source.call_count == len(PRODUCTS)


def test_execute_rejects_an_empty_instrument_list() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    with pytest.raises(LiveMarketObservationError, match="INSTRUMENTS_REQUIRED"):
        service.execute(
            plan_id="live-plan-test",
            venue=SharedVenue.CRYPTO,
            provider="COINBASE",
            instrument_ids=(),
            window_start=NOW,
            requested_at=NOW,
            provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
            source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
        )


def test_execute_bounds_instrument_count_per_round() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    many_instruments = tuple(
        f"COIN{i}-USD" for i in range(MAX_LIVE_OBSERVATION_INSTRUMENTS_PER_ROUND + 5)
    )

    result = service.execute(
        plan_id="live-plan-test",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=many_instruments,
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    assert result.round.requested_instrument_count == MAX_LIVE_OBSERVATION_INSTRUMENTS_PER_ROUND
    assert len(result.observations) == MAX_LIVE_OBSERVATION_INSTRUMENTS_PER_ROUND


def test_round_bound_is_universe_share_plus_execution_headroom() -> None:
    # PAPER Trial Closure Readiness v1: the research-universe share stays
    # exactly the original 10 (MAX_PULSE_PRODUCTS); execution-required
    # instruments get 2 extra slots so they never displace a universe product.
    assert MAX_LIVE_OBSERVATION_INSTRUMENTS_PER_ROUND == 12


def test_obtain_contemporaneous_market_observation_satisfied_after_capture() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource()).execute(
        plan_id="live-plan-test",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=PRODUCTS,
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    hook = ObtainContemporaneousMarketObservation(repository)
    result = hook.execute(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=NOW + timedelta(seconds=10),
        max_staleness_seconds=60,
    )

    assert result.disposition is ContemporaneousObservationDisposition.SATISFIED
    assert result.observation is not None
    assert result.observation.instrument_id == "BTC-USD"


def test_obtain_contemporaneous_market_observation_stale_beyond_bound() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource()).execute(
        plan_id="live-plan-test",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=PRODUCTS,
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    hook = ObtainContemporaneousMarketObservation(repository)
    result = hook.execute(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=NOW + timedelta(minutes=10),
        max_staleness_seconds=60,
    )

    assert result.disposition is ContemporaneousObservationDisposition.STALE
    assert result.observation is None


def test_obtain_contemporaneous_market_observation_absent_when_never_observed() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    hook = ObtainContemporaneousMarketObservation(repository)

    result = hook.execute(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=NOW,
        max_staleness_seconds=60,
    )

    assert result.disposition is ContemporaneousObservationDisposition.ABSENT
    assert result.observation is None


# ---- Live Observation Point-in-Time Race Fix v1 -----------------------
#
# The same three scenarios are reproduced verbatim against real Postgres in
# tests/persistence/test_postgres_live_market_observation.py, so both
# implementations of LiveMarketObservationRepository are proven to agree.
# Reproduces the exact race shape that froze the first real
# STRATEGY_PAPER_TRIAL_EXECUTION dispatch on 2026-08-27: an independently-
# scheduled live-observer worker writes a fresh row a few milliseconds AFTER
# a caller's own (already-frozen) as_of, alongside a genuinely valid
# observation captured shortly before it.


def test_latest_observation_bounds_by_as_of_and_prefers_the_valid_past_row() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    as_of = NOW

    valid_past = service.execute(
        plan_id="race-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=as_of - timedelta(seconds=60),
        requested_at=as_of - timedelta(seconds=60),
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )
    future_row = service.execute(
        plan_id="race-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=as_of + timedelta(milliseconds=22),
        requested_at=as_of + timedelta(milliseconds=22),
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )
    assert future_row.round.window_start > as_of  # sanity: genuinely future relative to as_of

    latest = repository.latest_observation(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
    )
    assert latest is not None
    assert latest.observation_id == valid_past.observations[0].observation_id
    assert latest.observation_id != future_row.observations[0].observation_id

    # The domain staleness gate must accept it cleanly -- the whole point of
    # the fix is that Phase D no longer raises LIVE_OBSERVATION_FUTURE_
    # TIMESTAMP on a real, already-available past observation merely because
    # a newer row also exists.
    hook_result = ObtainContemporaneousMarketObservation(repository).execute(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
        max_staleness_seconds=90,
    )
    assert hook_result.disposition is ContemporaneousObservationDisposition.SATISFIED
    assert hook_result.observation is not None
    assert hook_result.observation.observation_id == valid_past.observations[0].observation_id


def test_latest_observation_with_only_future_rows_is_absent_not_the_future_row() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    as_of = NOW

    service.execute(
        plan_id="race-only-future-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=as_of + timedelta(seconds=1),
        requested_at=as_of + timedelta(seconds=1),
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    latest = repository.latest_observation(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
    )
    assert latest is None

    hook_result = ObtainContemporaneousMarketObservation(repository).execute(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
        max_staleness_seconds=90,
    )
    assert hook_result.disposition is ContemporaneousObservationDisposition.ABSENT
    assert hook_result.observation is None


def test_latest_observation_includes_a_row_captured_exactly_at_as_of() -> None:
    repository = InMemoryLiveMarketObservationRepository()
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    as_of = NOW

    exact = service.execute(
        plan_id="race-exact-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=as_of,
        requested_at=as_of,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    latest = repository.latest_observation(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
    )
    assert latest is not None
    assert latest.observation_id == exact.observations[0].observation_id
