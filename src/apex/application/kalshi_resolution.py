"""Application-layer repository contract and in-memory implementation for
Kalshi Settlement & Resolution Evidence Maturation v1.

See ``domain.kalshi_resolution`` for the full evidence-model rationale.
This module owns no provider client, credential, or network capability --
every ``KalshiMarketResolutionFact`` a caller ``record``s here must already
have been built by ``domain.kalshi_resolution.build_kalshi_resolution_fact``
from a genuine, already-captured raw Kalshi read performed elsewhere
(``adapters.counterfactual_followup_sources``, orchestrated by
``application.counterfactual_followup.SyncFollowUpObservations``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from apex.domain.kalshi_resolution import KalshiMarketResolutionFact, KalshiResolutionError


class KalshiResolutionFactRepository(Protocol):
    def latest_fact(
        self, *, market_ticker: str, as_of: datetime
    ) -> KalshiMarketResolutionFact | None:
        """The most-recently-captured retained fact for ``market_ticker``
        whose own ``captured_at`` is at or before ``as_of`` -- never a fact
        Apex captured *after* the caller's own point-in-time boundary, no
        matter how much more recent or how much more authoritative it is.
        ``None`` if no qualifying fact is retained. Ties (identical
        ``captured_at``) break on ``resolution_fact_id`` descending, purely
        for deterministic replay -- an implementation detail no caller
        should depend on beyond "deterministic," since a genuine same-
        instant tie is not expected under real provider capture cadence.

        Mirrors ``application.live_market_observation.
        LiveMarketObservationRepository.latest_observation``'s own
        ``as_of``-bounded contract exactly, and exists for the identical
        reason (Live Observation Point-in-Time Race Fix v1): a caller's
        ``as_of`` can be frozen well before this method is actually called,
        while resolution facts keep being captured in real time by later,
        independent cycles."""

    def record(self, *, fact: KalshiMarketResolutionFact) -> bool:
        """Persist one resolution fact idempotently. Returns ``True`` on
        idempotent replay of an already-identical retained row (same
        ``resolution_fact_id``), ``False`` on a genuine new write. Raises
        ``KalshiResolutionError`` if ``resolution_fact_id`` already exists
        with a different ``canonical_hash`` -- defense in depth only, since
        ``resolution_fact_id`` is itself content-derived (see
        ``domain.kalshi_resolution`` module docstring) and should therefore
        never collide with differing content in practice."""

    def facts_for_ticker(self, *, market_ticker: str) -> tuple[KalshiMarketResolutionFact, ...]:
        """Every retained fact for one ticker, oldest ``captured_at``
        first -- full historical lineage for audit/debugging. Never used to
        answer "what is currently true" (use ``latest_fact`` for that)."""

    def census(self) -> dict[str, object]:
        """Bounded operator diagnostic: total retained fact rows, how many
        distinct market tickers are retained at all, each distinct
        ticker's *current* (latest-fact) resolution state, and the most
        recent capture timestamp."""


class InMemoryKalshiResolutionFactRepository:
    """Reference/test implementation. Also ``SyncFollowUpObservations``'
    own safe default when no durable repository is injected (see its
    ``__init__``) -- correct, bounded-request-avoiding behavior within one
    process's lifetime; only a durable (Postgres) implementation persists
    across process restarts / separate recurring-cycle invocations, which
    is what the live operating-cycle wiring always injects."""

    def __init__(self) -> None:
        self._facts: dict[str, KalshiMarketResolutionFact] = {}

    def latest_fact(
        self, *, market_ticker: str, as_of: datetime
    ) -> KalshiMarketResolutionFact | None:
        candidates = [
            fact
            for fact in self._facts.values()
            if fact.market_ticker == market_ticker and fact.captured_at <= as_of
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda fact: (fact.captured_at, fact.resolution_fact_id), reverse=True)
        return candidates[0]

    def record(self, *, fact: KalshiMarketResolutionFact) -> bool:
        existing = self._facts.get(fact.resolution_fact_id)
        if existing is not None:
            if existing.canonical_hash != fact.canonical_hash:
                raise KalshiResolutionError("KALSHI_RESOLUTION_FACT_IDENTITY_CONFLICT")
            return True
        self._facts[fact.resolution_fact_id] = fact
        return False

    def facts_for_ticker(self, *, market_ticker: str) -> tuple[KalshiMarketResolutionFact, ...]:
        return tuple(
            sorted(
                (fact for fact in self._facts.values() if fact.market_ticker == market_ticker),
                key=lambda fact: (fact.captured_at, fact.resolution_fact_id),
            )
        )

    def census(self) -> dict[str, object]:
        """Backs the operator-observability surface this milestone adds
        (Part 12). ``current_state_by_market`` is deliberately a snapshot
        over each distinct ticker's *latest* retained fact -- never a raw
        tally of historical fact rows -- so it answers "how many tracked
        markets are currently unresolved" honestly even for a market with
        several retained observations (e.g. closed -> determined ->
        finalized all genuinely retained for one ticker would otherwise
        double- or triple-count it)."""

        latest_by_ticker: dict[str, KalshiMarketResolutionFact] = {}
        for fact in self._facts.values():
            current = latest_by_ticker.get(fact.market_ticker)
            if current is None or (fact.captured_at, fact.resolution_fact_id) > (
                current.captured_at,
                current.resolution_fact_id,
            ):
                latest_by_ticker[fact.market_ticker] = fact
        by_state: dict[str, int] = {}
        for fact in latest_by_ticker.values():
            by_state[fact.resolution_state.value] = by_state.get(fact.resolution_state.value, 0) + 1
        latest_captured_at = max((fact.captured_at for fact in self._facts.values()), default=None)
        return {
            "total_fact_rows": len(self._facts),
            "distinct_market_tickers": len(latest_by_ticker),
            "current_state_by_market": by_state,
            "latest_captured_at": (
                None if latest_captured_at is None else latest_captured_at.isoformat()
            ),
        }
