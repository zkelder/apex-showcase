"""Persist deterministic comparisons of completed public Kalshi market scans."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from apex.domain.public_comparison import (
    EventConsistencyTransition,
    EventConsistencyTransitionState,
    MarketSnapshotDelta,
    PersistedPublicScan,
    ScanComparison,
    ScanComparisonState,
    compare_event_observations,
    compare_market_observations,
    rank_shadow_research_priorities,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, content_digest, deterministic_id

COMPARISON_VERSION = "v1"


class PublicScanComparisonError(RuntimeError):
    """A comparison request was not safe or did not retain immutable evidence."""


class PublicScanComparisonConflictError(PublicScanComparisonError):
    """A deterministic comparison identity was reused with different evidence."""


@dataclass(frozen=True, slots=True)
class PublicScanComparisonReport:
    comparison: ScanComparison
    market_deltas: tuple[MarketSnapshotDelta, ...]
    event_transitions: tuple[EventConsistencyTransition, ...]
    ranked_priorities: tuple[EventConsistencyTransition, ...]
    idempotent_replay: bool


class PublicScanComparisonStore(Protocol):
    def load_completed_scan(self, ingestion_run_id: str) -> PersistedPublicScan | None: ...

    def get_comparison(self, comparison_id: str) -> PublicScanComparisonReport | None: ...

    def append_comparison(
        self,
        comparison: ScanComparison,
        market_deltas: tuple[MarketSnapshotDelta, ...],
        event_transitions: tuple[EventConsistencyTransition, ...],
    ) -> None: ...

    def latest_completed_scans(
        self, provider: PublicMarketProvider
    ) -> tuple[PersistedPublicScan, ...]: ...


def _by_exact_ticker[T](values: tuple[T, ...], attribute: str) -> dict[str, T]:
    result: dict[str, T] = {}
    for value in values:
        ticker = str(getattr(value, attribute))
        if ticker in result:
            raise PublicScanComparisonError(
                "scan contains duplicate exact provider ticker evidence"
            )
        result[ticker] = value
    return result


class ComparePublicMarketScans:
    """Compare two retained market runs without network, execution, or financial authority."""

    def __init__(
        self,
        *,
        store: PublicScanComparisonStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    @staticmethod
    def _validate(baseline: PersistedPublicScan, current: PersistedPublicScan) -> None:
        if baseline.ingestion_run_id == current.ingestion_run_id:
            raise PublicScanComparisonError("baseline and current scan runs must be distinct")
        if baseline.provider != current.provider:
            raise PublicScanComparisonError("scan comparison provider identities must match")
        if baseline.state != "COMPLETED" or current.state != "COMPLETED":
            raise PublicScanComparisonError("scan comparison requires completed ingestion runs")
        if baseline.observed_end_at >= current.observed_start_at:
            raise PublicScanComparisonError(
                "baseline observation range must precede the current observation range"
            )

    @staticmethod
    def comparison_id_for(baseline_ingestion_run_id: str, current_ingestion_run_id: str) -> str:
        return deterministic_id(
            "scan_comparison",
            {
                "baseline_ingestion_run_id": baseline_ingestion_run_id,
                "current_ingestion_run_id": current_ingestion_run_id,
                "comparison_version": COMPARISON_VERSION,
            },
        )

    def compare(
        self,
        *,
        baseline_ingestion_run_id: str,
        current_ingestion_run_id: str,
    ) -> PublicScanComparisonReport:
        baseline = self._store.load_completed_scan(baseline_ingestion_run_id)
        current = self._store.load_completed_scan(current_ingestion_run_id)
        if baseline is None or current is None:
            raise PublicScanComparisonError("scan comparison references an unknown ingestion run")
        self._validate(baseline, current)
        comparison_id = self.comparison_id_for(baseline.ingestion_run_id, current.ingestion_run_id)
        existing = self._store.get_comparison(comparison_id)
        if existing is not None:
            if (
                existing.comparison.baseline_ingestion_run_id != baseline.ingestion_run_id
                or existing.comparison.current_ingestion_run_id != current.ingestion_run_id
            ):
                raise PublicScanComparisonConflictError("scan comparison identity conflicts")
            return PublicScanComparisonReport(
                comparison=existing.comparison,
                market_deltas=existing.market_deltas,
                event_transitions=existing.event_transitions,
                ranked_priorities=existing.ranked_priorities,
                idempotent_replay=True,
            )
        baseline_markets = _by_exact_ticker(baseline.markets, "provider_market_ticker")
        current_markets = _by_exact_ticker(current.markets, "provider_market_ticker")
        market_deltas = tuple(
            compare_market_observations(
                comparison_id=comparison_id,
                baseline=baseline_markets.get(ticker),
                current=current_markets.get(ticker),
                provider_market_ticker=ticker,
            )
            for ticker in sorted(set(baseline_markets) | set(current_markets))
        )
        baseline_events = _by_exact_ticker(baseline.events, "provider_event_ticker")
        current_events = _by_exact_ticker(current.events, "provider_event_ticker")
        transitions = tuple(
            compare_event_observations(
                comparison_id=comparison_id,
                baseline=baseline_events.get(ticker),
                current=current_events.get(ticker),
                provider_event_ticker=ticker,
            )
            for ticker in sorted(set(baseline_events) | set(current_events))
        )
        candidate_counts = tuple(
            (state.value, sum(value.state is state for value in transitions))
            for state in (
                EventConsistencyTransitionState.NEW_CANDIDATE,
                EventConsistencyTransitionState.PERSISTING_CANDIDATE,
                EventConsistencyTransitionState.RESOLVED_CANDIDATE,
                EventConsistencyTransitionState.BECAME_INELIGIBLE,
                EventConsistencyTransitionState.BECAME_INVALID,
            )
        )
        configuration = {
            "provider": baseline.provider,
            "comparison_version": COMPARISON_VERSION,
            "baseline_ingestion_run_id": baseline.ingestion_run_id,
            "current_ingestion_run_id": current.ingestion_run_id,
        }
        input_payload = {
            "configuration": configuration,
            "baseline_market_snapshot_ids": tuple(value.snapshot_id for value in baseline.markets),
            "current_market_snapshot_ids": tuple(value.snapshot_id for value in current.markets),
            "baseline_event_snapshot_ids": tuple(
                value.event_snapshot_id for value in baseline.events
            ),
            "current_event_snapshot_ids": tuple(
                value.event_snapshot_id for value in current.events
            ),
        }
        comparison = ScanComparison(
            comparison_id=comparison_id,
            provider=baseline.provider,
            baseline_ingestion_run_id=baseline.ingestion_run_id,
            current_ingestion_run_id=current.ingestion_run_id,
            comparison_version=COMPARISON_VERSION,
            baseline_observed_start_at=baseline.observed_start_at,
            baseline_observed_end_at=baseline.observed_end_at,
            current_observed_start_at=current.observed_start_at,
            current_observed_end_at=current.observed_end_at,
            # The latest retained observation is the only defensible comparison time.
            compared_at=current.observed_end_at,
            state=ScanComparisonState.COMPLETED,
            matched_market_count=len(set(baseline_markets) & set(current_markets)),
            added_market_count=len(set(current_markets) - set(baseline_markets)),
            removed_market_count=len(set(baseline_markets) - set(current_markets)),
            changed_market_count=sum(value.state.value == "CHANGED" for value in market_deltas),
            matched_event_count=len(set(baseline_events) & set(current_events)),
            candidate_transition_counts=candidate_counts,
            canonical_configuration_hash=ContentHash(content_digest(configuration)),
            canonical_input_hash=ContentHash(content_digest(input_payload)),
        )
        try:
            self._store.append_comparison(comparison, market_deltas, transitions)
        except PublicScanComparisonConflictError:
            raise
        persisted = self._store.get_comparison(comparison_id)
        if persisted is None:
            raise PublicScanComparisonError("scan comparison was not retained atomically")
        return persisted

    def compare_latest(self) -> PublicScanComparisonReport:
        runs = self._store.latest_completed_scans(PublicMarketProvider.KALSHI)
        if len(runs) < 2:
            raise PublicScanComparisonError(
                "fewer than two completed compatible Kalshi scans exist"
            )
        selected = tuple(sorted(runs, key=lambda value: value.observed_end_at))[-2:]
        return self.compare(
            baseline_ingestion_run_id=selected[0].ingestion_run_id,
            current_ingestion_run_id=selected[1].ingestion_run_id,
        )


class InMemoryPublicScanComparisonStore:
    """Fixture-only immutable comparison store with injected completed scan evidence."""

    def __init__(self, scans: tuple[PersistedPublicScan, ...]) -> None:
        self._scans = {value.ingestion_run_id: value for value in scans}
        self._reports: dict[str, PublicScanComparisonReport] = {}

    def load_completed_scan(self, ingestion_run_id: str) -> PersistedPublicScan | None:
        return self._scans.get(ingestion_run_id)

    def get_comparison(self, comparison_id: str) -> PublicScanComparisonReport | None:
        return self._reports.get(comparison_id)

    def append_comparison(
        self,
        comparison: ScanComparison,
        market_deltas: tuple[MarketSnapshotDelta, ...],
        event_transitions: tuple[EventConsistencyTransition, ...],
    ) -> None:
        existing = self._reports.get(comparison.comparison_id)
        report = PublicScanComparisonReport(
            comparison=comparison,
            market_deltas=market_deltas,
            event_transitions=event_transitions,
            ranked_priorities=rank_shadow_research_priorities(event_transitions),
            idempotent_replay=False,
        )
        if existing is not None and existing != report:
            raise PublicScanComparisonConflictError("scan comparison evidence conflicts")
        self._reports[comparison.comparison_id] = report

    def latest_completed_scans(
        self, provider: PublicMarketProvider
    ) -> tuple[PersistedPublicScan, ...]:
        return tuple(
            sorted(
                (
                    value
                    for value in self._scans.values()
                    if value.provider is provider and value.state == "COMPLETED"
                ),
                key=lambda value: (value.observed_end_at, value.ingestion_run_id),
            )
        )
