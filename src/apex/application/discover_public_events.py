"""Bounded public-event discovery and deterministic consistency shadow evaluation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from apex.application.discover_public_markets import InMemoryPublicMarketDiscoveryStore
from apex.domain.public_event import (
    ConsistencyEvaluation,
    ConsistencyEvaluationState,
    EventEvidenceState,
    EventMarketRelationship,
    NormalizedPublicEventSnapshot,
    evaluate_event_consistency,
    link_event_markets,
    rank_consistency_candidates,
)
from apex.domain.public_market import NormalizedPublicMarketSnapshot, PublicMarketProvider
from apex.domain.values import content_digest, deterministic_id


class PublicEventDiscoveryError(RuntimeError):
    """A bounded public event scan could not finish safely."""


class PublicEventDiscoveryConflictError(PublicEventDiscoveryError):
    """An immutable event discovery identity was reused with different evidence."""


class PublicEventPaginationError(PublicEventDiscoveryError):
    """An event cursor response is malformed, repeated, or cyclic."""


class PublicEventIngestionState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PublicEventScanRequest:
    """Explicit bounded event inputs; callers cannot select an endpoint or host."""

    run_key: str
    status: str = "open"
    page_size: int = 25
    event_limit: int = 25
    maximum_pages: int = 3
    timeout_seconds: int = 10
    with_nested_markets: bool = True
    market_ingestion_run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.run_key or len(self.run_key) > 160:
            raise ValueError("public event scan run key must be non-empty and bounded")
        if self.status != "open":
            raise ValueError("this public event scan supports only open events")
        if isinstance(self.page_size, bool) or not 1 <= self.page_size <= 200:
            raise ValueError("public event page size must be between one and 200")
        if isinstance(self.event_limit, bool) or not 1 <= self.event_limit <= 500:
            raise ValueError("public event limit must be between one and 500")
        if isinstance(self.maximum_pages, bool) or not 1 <= self.maximum_pages <= 25:
            raise ValueError("public event maximum pages must be between one and 25")
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 60:
            raise ValueError("public event timeout must be between one and 60 seconds")
        if self.market_ingestion_run_id is not None and (
            not self.market_ingestion_run_id or len(self.market_ingestion_run_id) > 160
        ):
            raise ValueError("event scan market ingestion run identity is invalid")
        if not self.with_nested_markets:
            raise ValueError("public event consistency requires nested market evidence")

    @property
    def canonical_configuration_hash(self) -> str:
        return content_digest(
            {
                "provider": PublicMarketProvider.KALSHI,
                "capability": "PUBLIC_REST_EVENT_DISCOVERY",
                "status": self.status,
                "page_size": self.page_size,
                "event_limit": self.event_limit,
                "maximum_pages": self.maximum_pages,
                "timeout_seconds": self.timeout_seconds,
                "with_nested_markets": self.with_nested_markets,
                "market_ingestion_run_id": self.market_ingestion_run_id,
            }
        )

    @property
    def ingestion_run_id(self) -> str:
        return deterministic_id(
            "provider_ingestion_run",
            {
                "run_key": self.run_key,
                "provider": PublicMarketProvider.KALSHI,
                "capability": "PUBLIC_REST_EVENT_DISCOVERY",
            },
        )


@dataclass(frozen=True, slots=True)
class CapturedPublicEventPage:
    """Exact event response evidence plus fixture-only evidence annotations."""

    endpoint_identity: str
    request_cursor: str | None
    received_at: datetime
    http_status: int
    response_content_type: str
    raw_payload: bytes
    response_cursor: str | None
    document: Mapping[str, object]
    fixture_exhaustiveness: Mapping[str, EventEvidenceState] | None = None
    fixture_nested_settlement_hashes: Mapping[str, Mapping[str, str]] | None = None

    def __post_init__(self) -> None:
        if self.endpoint_identity != "KALSHI_GET_EVENTS":
            raise ValueError(
                "public event discovery accepts only the Kalshi event endpoint identity"
            )
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("public event receive time must be timezone-aware")
        if self.http_status != 200:
            raise ValueError("captured public event page must have successful HTTP status")
        if self.response_content_type.lower().split(";", 1)[0].strip() != "application/json":
            raise ValueError("captured public event page must be JSON")
        if not self.raw_payload:
            raise ValueError("captured public event page must retain non-empty raw bytes")
        if self.request_cursor is not None and not isinstance(self.request_cursor, str):
            raise ValueError("public event request cursor must be a string or absent")
        if self.response_cursor is not None and not isinstance(self.response_cursor, str):
            raise ValueError("public event response cursor must be a string or absent")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))

    @property
    def raw_payload_sha256(self) -> str:
        return sha256(self.raw_payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RawPublicEventResponse:
    raw_response_id: str
    ingestion_run_id: str
    page_ordinal: int
    page: CapturedPublicEventPage
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if self.page_ordinal < 1:
            raise ValueError("raw event response page ordinal must be positive")
        if not self.raw_response_id or not self.ingestion_run_id:
            raise ValueError("raw event response identities must not be empty")


@dataclass(frozen=True, slots=True)
class PublicEventIngestionRun:
    ingestion_run_id: str
    configuration_hash: str
    state: PublicEventIngestionState
    page_count: int
    event_count: int


@dataclass(frozen=True, slots=True)
class PublicEventConsistencyScanSummary:
    ingestion_run_id: str
    state: PublicEventIngestionState
    page_count: int
    event_count: int
    consistent_count: int
    candidate_count: int
    ineligible_count: int
    invalid_count: int
    ranked_event_tickers: tuple[str, ...]
    idempotent_replay: bool


class PublicEventPageClient(Protocol):
    def fetch_events(
        self,
        *,
        status: str,
        page_size: int,
        cursor: str | None,
        timeout_seconds: int,
        with_nested_markets: bool,
    ) -> CapturedPublicEventPage: ...


class PublicEventNormalizer(Protocol):
    def normalize_events(
        self,
        *,
        raw_response_id: str,
        page: CapturedPublicEventPage,
    ) -> tuple[NormalizedPublicEventSnapshot, ...]: ...


class PublicEventConsistencyStore(Protocol):
    def get_event_run(self, ingestion_run_id: str) -> PublicEventIngestionRun | None: ...

    def start_event_run(self, request: PublicEventScanRequest, *, started_at: datetime) -> None: ...

    def append_event_raw_response(self, value: RawPublicEventResponse) -> None: ...

    def market_snapshots_for_event(
        self,
        provider_event_ticker: str | None,
        *,
        market_ingestion_run_id: str | None,
    ) -> tuple[NormalizedPublicMarketSnapshot, ...]: ...

    def append_event_snapshots(
        self,
        *,
        ingestion_run_id: str,
        snapshots: tuple[NormalizedPublicEventSnapshot, ...],
        relationships: tuple[EventMarketRelationship, ...],
        evaluations: tuple[ConsistencyEvaluation, ...],
    ) -> None: ...

    def complete_event_run(
        self,
        ingestion_run_id: str,
        *,
        completed_at: datetime,
        page_count: int,
        event_count: int,
    ) -> None: ...

    def fail_event_run(self, ingestion_run_id: str, *, completed_at: datetime) -> None: ...

    def evaluations_for_event_run(
        self, ingestion_run_id: str
    ) -> tuple[tuple[NormalizedPublicEventSnapshot, ConsistencyEvaluation], ...]: ...


def event_raw_response_id_for(
    value: CapturedPublicEventPage,
    *,
    ingestion_run_id: str,
    page_ordinal: int,
) -> str:
    return deterministic_id(
        "raw_provider_response",
        {
            "ingestion_run_id": ingestion_run_id,
            "page_ordinal": page_ordinal,
            "raw_payload_sha256": value.raw_payload_sha256,
        },
    )


class DiscoverPublicEventConsistency:
    """Own one bounded event scan over already-captured public market snapshots."""

    evaluator_version = "public-event-consistency-v1"

    def __init__(
        self,
        *,
        client: PublicEventPageClient,
        normalizer: PublicEventNormalizer,
        store: PublicEventConsistencyStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._normalizer = normalizer
        self._store = store
        self._clock = clock

    @staticmethod
    def _summary(
        run: PublicEventIngestionRun,
        values: tuple[tuple[NormalizedPublicEventSnapshot, ConsistencyEvaluation], ...],
        *,
        idempotent_replay: bool,
    ) -> PublicEventConsistencyScanSummary:
        counts = {state: 0 for state in ConsistencyEvaluationState}
        for _, evaluation in values:
            counts[evaluation.state] += 1
        ranked = rank_consistency_candidates(values)
        return PublicEventConsistencyScanSummary(
            ingestion_run_id=run.ingestion_run_id,
            state=run.state,
            page_count=run.page_count,
            event_count=run.event_count,
            consistent_count=counts[ConsistencyEvaluationState.CONSISTENT],
            candidate_count=counts[ConsistencyEvaluationState.CANDIDATE],
            ineligible_count=counts[ConsistencyEvaluationState.INELIGIBLE],
            invalid_count=counts[ConsistencyEvaluationState.INVALID],
            ranked_event_tickers=tuple(
                event.provider_event_ticker or "<missing>" for event, _ in ranked
            ),
            idempotent_replay=idempotent_replay,
        )

    def discover(self, request: PublicEventScanRequest) -> PublicEventConsistencyScanSummary:
        existing = self._store.get_event_run(request.ingestion_run_id)
        if existing is not None:
            if existing.configuration_hash != request.canonical_configuration_hash:
                raise PublicEventDiscoveryConflictError(
                    "public event ingestion identity conflicts with its configuration"
                )
            if existing.state is PublicEventIngestionState.COMPLETED:
                return self._summary(
                    existing,
                    self._store.evaluations_for_event_run(existing.ingestion_run_id),
                    idempotent_replay=True,
                )
            raise PublicEventDiscoveryConflictError(
                "public event ingestion identity is not available for replay"
            )

        self._store.start_event_run(request, started_at=self._clock().astimezone(UTC))
        cursor: str | None = None
        seen_response_cursors: set[str] = set()
        page_count = 0
        event_count = 0
        try:
            for page_ordinal in range(1, request.maximum_pages + 1):
                page = self._client.fetch_events(
                    status=request.status,
                    page_size=request.page_size,
                    cursor=cursor,
                    timeout_seconds=request.timeout_seconds,
                    with_nested_markets=request.with_nested_markets,
                )
                if page.request_cursor != cursor:
                    raise PublicEventPaginationError(
                        "public event response cursor provenance changed"
                    )
                raw_response_id = event_raw_response_id_for(
                    page,
                    ingestion_run_id=request.ingestion_run_id,
                    page_ordinal=page_ordinal,
                )
                self._store.append_event_raw_response(
                    RawPublicEventResponse(
                        raw_response_id=raw_response_id,
                        ingestion_run_id=request.ingestion_run_id,
                        page_ordinal=page_ordinal,
                        page=page,
                    )
                )
                remaining = request.event_limit - event_count
                if remaining <= 0:
                    break
                snapshots = self._normalizer.normalize_events(
                    raw_response_id=raw_response_id, page=page
                )[:remaining]
                relationships: list[EventMarketRelationship] = []
                evaluations: list[ConsistencyEvaluation] = []
                for snapshot in snapshots:
                    markets = self._store.market_snapshots_for_event(
                        snapshot.provider_event_ticker,
                        market_ingestion_run_id=request.market_ingestion_run_id,
                    )
                    linked = link_event_markets(snapshot, markets)
                    relationships.extend(linked)
                    linked_market_ids = {relationship.market_snapshot_id for relationship in linked}
                    evaluation_markets = tuple(
                        market for market in markets if market.snapshot_id in linked_market_ids
                    )
                    evaluations.append(
                        evaluate_event_consistency(
                            snapshot,
                            relationships=linked,
                            markets=evaluation_markets,
                            evaluated_at=page.received_at,
                            evaluator_version=self.evaluator_version,
                        )
                    )
                self._store.append_event_snapshots(
                    ingestion_run_id=request.ingestion_run_id,
                    snapshots=snapshots,
                    relationships=tuple(relationships),
                    evaluations=tuple(evaluations),
                )
                page_count += 1
                event_count += len(snapshots)
                if event_count >= request.event_limit:
                    break
                next_cursor = page.response_cursor
                if next_cursor in (None, ""):
                    break
                assert isinstance(next_cursor, str)
                if next_cursor in seen_response_cursors or next_cursor == cursor:
                    raise PublicEventPaginationError(
                        "public event response cursor repeated or cycled"
                    )
                seen_response_cursors.add(next_cursor)
                cursor = next_cursor
            self._store.complete_event_run(
                request.ingestion_run_id,
                completed_at=self._clock().astimezone(UTC),
                page_count=page_count,
                event_count=event_count,
            )
        except Exception:
            self._store.fail_event_run(
                request.ingestion_run_id, completed_at=self._clock().astimezone(UTC)
            )
            raise
        completed = self._store.get_event_run(request.ingestion_run_id)
        if completed is None or completed.state is not PublicEventIngestionState.COMPLETED:
            raise PublicEventDiscoveryError("public event ingestion completion was not retained")
        return self._summary(
            completed,
            self._store.evaluations_for_event_run(completed.ingestion_run_id),
            idempotent_replay=False,
        )


class InMemoryPublicEventConsistencyStore:
    """Small append-only fixture store shared with the market discovery store."""

    def __init__(self, market_store: InMemoryPublicMarketDiscoveryStore) -> None:
        self._market_store = market_store
        self._runs: dict[str, PublicEventIngestionRun] = {}
        self.raw_responses: dict[str, RawPublicEventResponse] = {}
        self.snapshots: dict[str, NormalizedPublicEventSnapshot] = {}
        self.relationships: dict[str, EventMarketRelationship] = {}
        self.evaluations: dict[str, ConsistencyEvaluation] = {}

    def get_event_run(self, ingestion_run_id: str) -> PublicEventIngestionRun | None:
        return self._runs.get(ingestion_run_id)

    def start_event_run(self, request: PublicEventScanRequest, *, started_at: datetime) -> None:
        del started_at
        if request.ingestion_run_id in self._runs:
            raise PublicEventDiscoveryConflictError("public event ingestion already exists")
        self._runs[request.ingestion_run_id] = PublicEventIngestionRun(
            ingestion_run_id=request.ingestion_run_id,
            configuration_hash=request.canonical_configuration_hash,
            state=PublicEventIngestionState.RUNNING,
            page_count=0,
            event_count=0,
        )

    def append_event_raw_response(self, value: RawPublicEventResponse) -> None:
        existing = self.raw_responses.get(value.raw_response_id)
        if existing is not None:
            if existing != value:
                raise PublicEventDiscoveryConflictError(
                    "raw public event response identity conflicts"
                )
            return
        if any(
            row.ingestion_run_id == value.ingestion_run_id
            and row.page_ordinal == value.page_ordinal
            for row in self.raw_responses.values()
        ):
            raise PublicEventDiscoveryConflictError("public event response page ordinal conflicts")
        self.raw_responses[value.raw_response_id] = value

    def market_snapshots_for_event(
        self,
        provider_event_ticker: str | None,
        *,
        market_ingestion_run_id: str | None,
    ) -> tuple[NormalizedPublicMarketSnapshot, ...]:
        return tuple(
            sorted(
                (
                    value
                    for value in self._market_store.snapshots.values()
                    if value.provider is PublicMarketProvider.KALSHI
                    and value.event_ticker == provider_event_ticker
                    and (
                        market_ingestion_run_id is None
                        or (
                            value.raw_response_id in self._market_store.raw_responses
                            and self._market_store.raw_responses[
                                value.raw_response_id
                            ].ingestion_run_id
                            == market_ingestion_run_id
                        )
                    )
                ),
                key=lambda value: value.snapshot_id,
            )
        )

    @staticmethod
    def _append_immutable[T](
        collection: dict[str, T], identity: str, value: T, *, message: str
    ) -> None:
        existing = collection.get(identity)
        if existing is not None and existing != value:
            raise PublicEventDiscoveryConflictError(message)
        collection[identity] = value

    def append_event_snapshots(
        self,
        *,
        ingestion_run_id: str,
        snapshots: tuple[NormalizedPublicEventSnapshot, ...],
        relationships: tuple[EventMarketRelationship, ...],
        evaluations: tuple[ConsistencyEvaluation, ...],
    ) -> None:
        if ingestion_run_id not in self._runs:
            raise PublicEventDiscoveryError("public event ingestion is missing")
        snapshot_ids = {snapshot.snapshot_id for snapshot in snapshots}
        if {evaluation.event_snapshot_id for evaluation in evaluations} != snapshot_ids:
            raise ValueError("each event snapshot requires exactly one consistency evaluation")
        for relationship in relationships:
            if relationship.event_snapshot_id not in snapshot_ids:
                raise ValueError("event relationship must reference this exact event snapshot")
            if relationship.market_snapshot_id not in self._market_store.snapshots:
                raise ValueError("event relationship must reference a persisted market snapshot")
        for snapshot in snapshots:
            self._append_immutable(
                self.snapshots,
                snapshot.snapshot_id,
                snapshot,
                message="public event snapshot identity conflicts",
            )
        for relationship in relationships:
            if any(
                existing.event_snapshot_id == relationship.event_snapshot_id
                and existing.market_snapshot_id == relationship.market_snapshot_id
                and existing.relationship_id != relationship.relationship_id
                for existing in self.relationships.values()
            ):
                raise PublicEventDiscoveryConflictError(
                    "event-market relationship identity conflicts"
                )
            self._append_immutable(
                self.relationships,
                relationship.relationship_id,
                relationship,
                message="event-market relationship evidence conflicts",
            )
        for evaluation in evaluations:
            if any(
                existing.event_snapshot_id == evaluation.event_snapshot_id
                and existing.evaluation_id != evaluation.evaluation_id
                for existing in self.evaluations.values()
            ):
                raise PublicEventDiscoveryConflictError("consistency evaluation identity conflicts")
            self._append_immutable(
                self.evaluations,
                evaluation.evaluation_id,
                evaluation,
                message="consistency evaluation evidence conflicts",
            )

    def complete_event_run(
        self,
        ingestion_run_id: str,
        *,
        completed_at: datetime,
        page_count: int,
        event_count: int,
    ) -> None:
        del completed_at
        run = self._runs[ingestion_run_id]
        if run.state is not PublicEventIngestionState.RUNNING:
            raise PublicEventDiscoveryConflictError("public event ingestion cannot be completed")
        self._runs[ingestion_run_id] = PublicEventIngestionRun(
            ingestion_run_id=run.ingestion_run_id,
            configuration_hash=run.configuration_hash,
            state=PublicEventIngestionState.COMPLETED,
            page_count=page_count,
            event_count=event_count,
        )

    def fail_event_run(self, ingestion_run_id: str, *, completed_at: datetime) -> None:
        del completed_at
        run = self._runs.get(ingestion_run_id)
        if run is None or run.state is not PublicEventIngestionState.RUNNING:
            return
        event_ids = {
            value.snapshot_id
            for value in self.snapshots.values()
            if value.raw_response_id in self.raw_responses
            and self.raw_responses[value.raw_response_id].ingestion_run_id == ingestion_run_id
        }
        self._runs[ingestion_run_id] = PublicEventIngestionRun(
            ingestion_run_id=run.ingestion_run_id,
            configuration_hash=run.configuration_hash,
            state=PublicEventIngestionState.FAILED,
            page_count=sum(
                value.ingestion_run_id == ingestion_run_id for value in self.raw_responses.values()
            ),
            event_count=len(event_ids),
        )

    def evaluations_for_event_run(
        self, ingestion_run_id: str
    ) -> tuple[tuple[NormalizedPublicEventSnapshot, ConsistencyEvaluation], ...]:
        values = tuple(
            (snapshot, evaluation)
            for snapshot in self.snapshots.values()
            if snapshot.raw_response_id in self.raw_responses
            and self.raw_responses[snapshot.raw_response_id].ingestion_run_id == ingestion_run_id
            for evaluation in self.evaluations.values()
            if evaluation.event_snapshot_id == snapshot.snapshot_id
        )
        return tuple(sorted(values, key=lambda value: value[0].snapshot_id))
