from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.crypto_universe import CapturedProductBook
from apex.application.live_market_observation import ObtainContemporaneousMarketObservation
from apex.application.strategy_paper_trial import obtain_decision_price_evidence
from apex.domain.live_market_observation import (
    LiveMarketObservation,
    LiveObservationQuality,
    LiveObservationType,
    live_observation_round_id,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_paper_trial import StrategyPaperTrialError

NOW = datetime(2026, 8, 25, tzinfo=UTC)
INSTRUMENT_ID = "BTC-USD"


class _SingleObservationRepository:
    """Minimal ``LiveMarketObservationRepository`` double -- only the one
    method ``ObtainContemporaneousMarketObservation`` actually reads. Honors
    the point-in-time ``as_of`` bound the same way
    ``PostgresLiveMarketObservationRepository`` does (a future-relative-to-
    ``as_of`` seeded observation is never surfaced), even though none of this
    file's own tests currently seed one -- so this double stays a faithful
    stand-in for the real repository contract rather than a shortcut around
    it."""

    def __init__(self, observation: LiveMarketObservation | None) -> None:
        self._observation = observation

    def latest_observation(
        self,
        *,
        venue: SharedVenue,
        instrument_id: str,
        observation_type: LiveObservationType,
        as_of: datetime,
    ) -> LiveMarketObservation | None:
        if self._observation is None or self._observation.captured_at > as_of:
            return None
        return self._observation

    def observations_for_instrument_window(
        self,
        *,
        instrument_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[LiveMarketObservation, ...]:
        if (
            self._observation is None
            or self._observation.instrument_id != instrument_id
            or not window_start <= self._observation.captured_at <= window_end
        ):
            return ()
        return (self._observation,)


class _RefusingQuoteSource:
    """A ``LiveQuoteSource`` double that fails the test if it is ever
    called -- proves the substrate-satisfied path never reaches the direct
    provider fallback."""

    evidence_class = EvidenceClass.FIXTURE

    def capture_product_book(self, *, product_id: str, maximum_depth: int, requested_at: datetime):
        raise AssertionError("fallback_source must not be called when the substrate is SATISFIED")


class _FixtureQuoteSource:
    """A ``LiveQuoteSource`` double standing in for a real direct-provider
    capture (the bounded fallback path) -- never touches the network."""

    evidence_class = EvidenceClass.FIXTURE

    def __init__(self, *, price: Decimal) -> None:
        self._price = price

    def capture_product_book(
        self, *, product_id: str, maximum_depth: int, requested_at: datetime
    ) -> CapturedProductBook:
        bid = self._price - Decimal("0.01")
        ask = self._price + Decimal("0.01")
        return CapturedProductBook(
            captured_at=requested_at,
            raw_payload=b"{}",
            product_id=product_id,
            best_bid=bid,
            best_ask=ask,
            midpoint=self._price,
            spread_bps=Decimal("2"),
            visible_bid_depth=Decimal("1"),
            visible_ask_depth=Decimal("1"),
            source_locator="fixture://test",
            runtime_milliseconds=1,
        )


def _observation(*, price: Decimal, captured_at: datetime) -> LiveMarketObservation:
    round_id = live_observation_round_id(plan_id="test-plan", window_start=captured_at)
    return LiveMarketObservation.create(
        round_id=round_id,
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_id=INSTRUMENT_ID,
        observation_type=LiveObservationType.QUOTE,
        captured_at=captured_at,
        bid_price=price - Decimal("0.01"),
        ask_price=price + Decimal("0.01"),
        book_depth_ask=Decimal("1"),
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        provider_schema_version="TEST_V1",
        source_identity="TEST_SOURCE",
        quality=LiveObservationQuality.PASSED,
    )


class TestObtainDecisionPriceEvidence:
    # Review A (Strategy/PAPER Activation Integration Readiness Review v1):
    # the preferred decision-time price-evidence entry point must consult
    # the canonical Live Market Observation substrate before ever reaching
    # for a fresh, unanchored direct-provider capture -- and must never let
    # a STALE/ABSENT verdict be silently bypassed by one. Seeds an isolated,
    # in-memory observation directly (no production live-observer worker
    # needs to be installed/running to prove this).

    def test_satisfied_observation_is_preferred_and_its_id_is_retained(self) -> None:
        observation = _observation(price=Decimal("100.00"), captured_at=NOW)
        contemporaneous = ObtainContemporaneousMarketObservation(
            _SingleObservationRepository(observation)
        )

        evidence = obtain_decision_price_evidence(
            contemporaneous=contemporaneous,
            venue=SharedVenue.CRYPTO,
            product_id=INSTRUMENT_ID,
            as_of=NOW + timedelta(seconds=5),
            max_staleness_seconds=60,
            fallback_source=_RefusingQuoteSource(),
        )

        assert observation.observation_id in evidence.evidence_id
        assert evidence.price == Decimal("100.00")
        assert evidence.observed_at == NOW

    def test_absent_observation_fails_closed_without_a_fallback_source(self) -> None:
        contemporaneous = ObtainContemporaneousMarketObservation(_SingleObservationRepository(None))

        with pytest.raises(StrategyPaperTrialError, match="ABSENT"):
            obtain_decision_price_evidence(
                contemporaneous=contemporaneous,
                venue=SharedVenue.CRYPTO,
                product_id=INSTRUMENT_ID,
                as_of=NOW,
                max_staleness_seconds=60,
            )

    def test_stale_observation_does_not_silently_fall_back(self) -> None:
        observation = _observation(price=Decimal("100.00"), captured_at=NOW)
        contemporaneous = ObtainContemporaneousMarketObservation(
            _SingleObservationRepository(observation)
        )

        with pytest.raises(StrategyPaperTrialError, match="STALE"):
            obtain_decision_price_evidence(
                contemporaneous=contemporaneous,
                venue=SharedVenue.CRYPTO,
                product_id=INSTRUMENT_ID,
                as_of=NOW + timedelta(hours=1),
                max_staleness_seconds=60,
            )

    def test_stale_observation_with_an_explicit_fallback_is_distinctly_labeled(self) -> None:
        observation = _observation(price=Decimal("100.00"), captured_at=NOW)
        contemporaneous = ObtainContemporaneousMarketObservation(
            _SingleObservationRepository(observation)
        )
        fallback = _FixtureQuoteSource(price=Decimal("101.00"))

        evidence = obtain_decision_price_evidence(
            contemporaneous=contemporaneous,
            venue=SharedVenue.CRYPTO,
            product_id=INSTRUMENT_ID,
            as_of=NOW + timedelta(hours=1),
            max_staleness_seconds=60,
            fallback_source=fallback,
        )

        # A fallback-sourced evidence never carries the stale observation's
        # id -- it is a distinct, freshly-labeled direct capture, not a
        # resurrection of the stale row.
        assert observation.observation_id not in evidence.evidence_id
        assert evidence.evidence_id.startswith("strategy-paper-trial-")
        assert evidence.price == Decimal("101.00")
