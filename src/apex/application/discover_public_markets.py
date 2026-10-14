"""One-shot, bounded public-market discovery with no trading authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol

from apex.domain.public_market import (
    NormalizedPublicMarketSnapshot,
    PublicMarketDataQualityState,
    PublicMarketProvider,
    ShadowMarketEvaluation,
    evaluate_public_market_snapshot,
)
from apex.domain.values import content_digest, deterministic_id


class PublicMarketDiscoveryError(RuntimeError):
    """A bounded public market scan could not finish safely."""


class PublicMarketDataError(RuntimeError):
    """A public-market client failed without exposing credentials or payloads."""


class PublicMarketTimeoutError(PublicMarketDataError):
    """A bounded public-market request exceeded its configured deadline."""


class PublicMarketHttpError(PublicMarketDataError):
    """An allowlisted public-market endpoint returned an unacceptable status."""


class PublicMarketResponseError(PublicMarketDataError):
    """A public-market response was not compatible with the bounded protocol."""


class PublicMarketDiscoveryConflictError(PublicMarketDiscoveryError):
    """An immutable discovery identity was reused with different content."""


class PublicMarketPaginationError(PublicMarketDiscoveryError):
    """A cursor response is malformed, repeated, or cyclic."""


class PublicMarketIngestionState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PublicMarketScanRequest:
    """Explicit, bounded scan inputs; no endpoint or host can be supplied here."""

    run_key: str
    status: str = "open"
    page_size: int = 25
    maximum_pages: int = 3
    timeout_seconds: int = 10
    exclude_multivariate: bool = True

    def __post_init__(self) -> None:
        if not self.run_key or len(self.run_key) > 160:
            raise ValueError("public market scan run key must be non-empty and bounded")
        if self.status != "open":
            raise ValueError("this public market scan supports only open markets")
        if isinstance(self.page_size, bool) or not 1 <= self.page_size <= 1000:
            raise ValueError("public market page size must be between one and 1000")
        if isinstance(self.maximum_pages, bool) or not 1 <= self.maximum_pages <= 25:
            raise ValueError("public market maximum pages must be between one and 25")
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 60:
            raise ValueError("public market timeout must be between one and 60 seconds")
        if not self.exclude_multivariate:
            raise ValueError("this public market scan explicitly excludes multivariate markets")

    @property
    def canonical_configuration_hash(self) -> str:
        return content_digest(
            {
                "provider": PublicMarketProvider.KALSHI,
                "capability": "PUBLIC_REST_MARKET_DISCOVERY",
                "status": self.status,
                "page_size": self.page_size,
                "maximum_pages": self.maximum_pages,
                "timeout_seconds": self.timeout_seconds,
                "exclude_multivariate": self.exclude_multivariate,
            }
        )

    @property
    def ingestion_run_id(self) -> str:
        return deterministic_id(
            "provider_ingestion_run",
            {
                "run_key": self.run_key,
                "provider": PublicMarketProvider.KALSHI,
                "capability": "PUBLIC_REST_MARKET_DISCOVERY",
            },
        )


@dataclass(frozen=True, slots=True)
class CapturedPublicMarketPage:
    """Exact received response evidence already validated by the boundary adapter."""

    endpoint_identity: str
    request_cursor: str | None
    received_at: datetime
    http_status: int
    response_content_type: str
    raw_payload: bytes
    response_cursor: str | None
    document: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.endpoint_identity != "KALSHI_GET_MARKETS":
            raise ValueError("public discovery accepts only the Kalshi market endpoint identity")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("public market receive time must be timezone-aware")
        if self.http_status != 200:
            raise ValueError("captured public market page must have successful HTTP status")
        if not self.response_content_type.lower().split(";", 1)[0].strip() == "application/json":
            raise ValueError("captured public market page must be JSON")
        if not self.raw_payload:
            raise ValueError("captured public market page must retain non-empty raw bytes")
        if self.request_cursor is not None and not isinstance(self.request_cursor, str):
            raise ValueError("public market request cursor must be a string or absent")
        if self.response_cursor is not None and not isinstance(self.response_cursor, str):
            raise ValueError("public market response cursor must be a string or absent")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))

    @property
    def raw_payload_sha256(self) -> str:
        return sha256(self.raw_payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RawPublicMarketResponse:
    raw_response_id: str
    ingestion_run_id: str
    page_ordinal: int
    page: CapturedPublicMarketPage
    schema_version: str = "v1"

    def __post_init__(self) -> None:
        if self.page_ordinal < 1:
            raise ValueError("raw response page ordinal must be positive")
        if not self.raw_response_id or not self.ingestion_run_id:
            raise ValueError("raw response identities must not be empty")


@dataclass(frozen=True, slots=True)
class PublicMarketIngestionRun:
    ingestion_run_id: str
    configuration_hash: str
    state: PublicMarketIngestionState
    page_count: int
    market_count: int


@dataclass(frozen=True, slots=True)
class PublicMarketScanSummary:
    ingestion_run_id: str
    state: PublicMarketIngestionState
    page_count: int
    market_count: int
    eligible_count: int
    ineligible_count: int
    invalid_count: int
    idempotent_replay: bool


class PublicMarketPageClient(Protocol):
    def fetch(
        self,
        *,
        status: str,
        page_size: int,
        cursor: str | None,
        timeout_seconds: int,
        exclude_multivariate: bool,
    ) -> CapturedPublicMarketPage: ...


class PublicMarketNormalizer(Protocol):
    def normalize(
        self,
        *,
        raw_response_id: str,
        page: CapturedPublicMarketPage,
    ) -> tuple[NormalizedPublicMarketSnapshot, ...]: ...


class PublicMarketDiscoveryStore(Protocol):
    def get_run(self, ingestion_run_id: str) -> PublicMarketIngestionRun | None: ...

    def start_run(self, request: PublicMarketScanRequest, *, started_at: datetime) -> None: ...

    def append_raw_response(self, value: RawPublicMarketResponse) -> None: ...

    def append_snapshots(
        self,
        *,
        ingestion_run_id: str,
        snapshots: tuple[NormalizedPublicMarketSnapshot, ...],
        evaluations: tuple[ShadowMarketEvaluation, ...],
    ) -> None: ...

    def complete_run(
        self,
        ingestion_run_id: str,
        *,
        completed_at: datetime,
        page_count: int,
        market_count: int,
    ) -> None: ...

    def fail_run(self, ingestion_run_id: str, *, completed_at: datetime) -> None: ...

    def evaluations_for_run(self, ingestion_run_id: str) -> tuple[ShadowMarketEvaluation, ...]: ...


def raw_response_id_for(
    value: CapturedPublicMarketPage,
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


class DiscoverPublicPredictionMarkets:
    """Own a one-shot bounded scan and preserve received evidence before parsing."""

    evaluator_version = "public-market-structure-v1"

    def __init__(
        self,
        *,
        client: PublicMarketPageClient,
        normalizer: PublicMarketNormalizer,
        store: PublicMarketDiscoveryStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._normalizer = normalizer
        self._store = store
        self._clock = clock

    @staticmethod
    def _summary(
        run: PublicMarketIngestionRun,
        evaluations: tuple[ShadowMarketEvaluation, ...],
        *,
        idempotent_replay: bool,
    ) -> PublicMarketScanSummary:
        counts = {state: 0 for state in PublicMarketDataQualityState}
        for evaluation in evaluations:
            counts[evaluation.state] += 1
        return PublicMarketScanSummary(
            ingestion_run_id=run.ingestion_run_id,
            state=run.state,
            page_count=run.page_count,
            market_count=run.market_count,
            eligible_count=counts[PublicMarketDataQualityState.ELIGIBLE],
            ineligible_count=counts[PublicMarketDataQualityState.INELIGIBLE],
            invalid_count=counts[PublicMarketDataQualityState.INVALID],
            idempotent_replay=idempotent_replay,
        )

    def discover(self, request: PublicMarketScanRequest) -> PublicMarketScanSummary:
        existing = self._store.get_run(request.ingestion_run_id)
        if existing is not None:
            if existing.configuration_hash != request.canonical_configuration_hash:
                raise PublicMarketDiscoveryConflictError(
                    "public market ingestion identity conflicts with its configuration"
                )
            if existing.state is PublicMarketIngestionState.COMPLETED:
                return self._summary(
                    existing,
                    self._store.evaluations_for_run(existing.ingestion_run_id),
                    idempotent_replay=True,
                )
            raise PublicMarketDiscoveryConflictError(
                "public market ingestion identity is not available for replay"
            )

        started_at = self._clock().astimezone(UTC)
        self._store.start_run(request, started_at=started_at)
        cursor: str | None = None
        seen_response_cursors: set[str] = set()
        page_count = 0
        market_count = 0
        try:
            for page_ordinal in range(1, request.maximum_pages + 1):
                page = self._client.fetch(
                    status=request.status,
                    page_size=request.page_size,
                    cursor=cursor,
                    timeout_seconds=request.timeout_seconds,
                    exclude_multivariate=request.exclude_multivariate,
                )
                if page.request_cursor != cursor:
                    raise PublicMarketPaginationError(
                        "public market response cursor provenance changed"
                    )
                raw_response_id = raw_response_id_for(
                    page,
                    ingestion_run_id=request.ingestion_run_id,
                    page_ordinal=page_ordinal,
                )
                self._store.append_raw_response(
                    RawPublicMarketResponse(
                        raw_response_id=raw_response_id,
                        ingestion_run_id=request.ingestion_run_id,
                        page_ordinal=page_ordinal,
                        page=page,
                    )
                )
                snapshots = self._normalizer.normalize(raw_response_id=raw_response_id, page=page)
                evaluations = tuple(
                    evaluate_public_market_snapshot(
                        snapshot,
                        evaluated_at=page.received_at,
                        evaluator_version=self.evaluator_version,
                    )
                    for snapshot in snapshots
                )
                self._store.append_snapshots(
                    ingestion_run_id=request.ingestion_run_id,
                    snapshots=snapshots,
                    evaluations=evaluations,
                )
                page_count += 1
                market_count += len(snapshots)
                next_cursor = page.response_cursor
                if next_cursor in (None, ""):
                    break
                assert isinstance(next_cursor, str)
                if next_cursor in seen_response_cursors or next_cursor == cursor:
                    raise PublicMarketPaginationError(
                        "public market response cursor repeated or cycled"
                    )
                seen_response_cursors.add(next_cursor)
                cursor = next_cursor
            self._store.complete_run(
                request.ingestion_run_id,
                completed_at=self._clock().astimezone(UTC),
                page_count=page_count,
                market_count=market_count,
            )
        except Exception:
            self._store.fail_run(
                request.ingestion_run_id,
                completed_at=self._clock().astimezone(UTC),
            )
            raise
        completed = self._store.get_run(request.ingestion_run_id)
        if completed is None or completed.state is not PublicMarketIngestionState.COMPLETED:
            raise PublicMarketDiscoveryError("public market ingestion completion was not retained")
        return self._summary(
            completed,
            self._store.evaluations_for_run(completed.ingestion_run_id),
            idempotent_replay=False,
        )


class InMemoryPublicMarketDiscoveryStore:
    """A small deterministic store for fixture scans and focused application tests."""

    def __init__(self) -> None:
        self._runs: dict[str, PublicMarketIngestionRun] = {}
        self.raw_responses: dict[str, RawPublicMarketResponse] = {}
        self.snapshots: dict[str, NormalizedPublicMarketSnapshot] = {}
        self.evaluations: dict[str, ShadowMarketEvaluation] = {}

    def get_run(self, ingestion_run_id: str) -> PublicMarketIngestionRun | None:
        return self._runs.get(ingestion_run_id)

    def start_run(self, request: PublicMarketScanRequest, *, started_at: datetime) -> None:
        del started_at
        existing = self._runs.get(request.ingestion_run_id)
        if existing is not None:
            raise PublicMarketDiscoveryConflictError("public market ingestion already exists")
        self._runs[request.ingestion_run_id] = PublicMarketIngestionRun(
            ingestion_run_id=request.ingestion_run_id,
            configuration_hash=request.canonical_configuration_hash,
            state=PublicMarketIngestionState.RUNNING,
            page_count=0,
            market_count=0,
        )

    def append_raw_response(self, value: RawPublicMarketResponse) -> None:
        existing = self.raw_responses.get(value.raw_response_id)
        if existing is not None and existing != value:
            raise PublicMarketDiscoveryConflictError("raw public response identity conflicts")
        if existing is not None:
            return
        if any(
            row.ingestion_run_id == value.ingestion_run_id
            and row.page_ordinal == value.page_ordinal
            for row in self.raw_responses.values()
        ):
            raise PublicMarketDiscoveryConflictError("public response page ordinal conflicts")
        self.raw_responses[value.raw_response_id] = value

    def append_snapshots(
        self,
        *,
        ingestion_run_id: str,
        snapshots: tuple[NormalizedPublicMarketSnapshot, ...],
        evaluations: tuple[ShadowMarketEvaluation, ...],
    ) -> None:
        if len(snapshots) != len(evaluations):
            raise ValueError("public snapshot and evaluation counts must match")
        for snapshot, evaluation in zip(snapshots, evaluations, strict=True):
            if evaluation.snapshot_id != snapshot.snapshot_id:
                raise ValueError("shadow evaluation must reference its exact snapshot")
            existing = self.snapshots.get(snapshot.snapshot_id)
            if existing is not None and existing != snapshot:
                raise PublicMarketDiscoveryConflictError("public snapshot identity conflicts")
            self.snapshots[snapshot.snapshot_id] = snapshot
            existing_evaluation = self.evaluations.get(evaluation.evaluation_id)
            if existing_evaluation is not None and existing_evaluation != evaluation:
                raise PublicMarketDiscoveryConflictError("shadow evaluation identity conflicts")
            self.evaluations[evaluation.evaluation_id] = evaluation
        if ingestion_run_id not in self._runs:
            raise PublicMarketDiscoveryError("public market ingestion is missing")

    def complete_run(
        self,
        ingestion_run_id: str,
        *,
        completed_at: datetime,
        page_count: int,
        market_count: int,
    ) -> None:
        del completed_at
        run = self._runs[ingestion_run_id]
        if run.state is not PublicMarketIngestionState.RUNNING:
            raise PublicMarketDiscoveryConflictError("public market ingestion cannot be completed")
        self._runs[ingestion_run_id] = PublicMarketIngestionRun(
            ingestion_run_id=run.ingestion_run_id,
            configuration_hash=run.configuration_hash,
            state=PublicMarketIngestionState.COMPLETED,
            page_count=page_count,
            market_count=market_count,
        )

    def fail_run(self, ingestion_run_id: str, *, completed_at: datetime) -> None:
        del completed_at
        run = self._runs.get(ingestion_run_id)
        if run is None or run.state is not PublicMarketIngestionState.RUNNING:
            return
        self._runs[ingestion_run_id] = PublicMarketIngestionRun(
            ingestion_run_id=run.ingestion_run_id,
            configuration_hash=run.configuration_hash,
            state=PublicMarketIngestionState.FAILED,
            page_count=sum(
                value.ingestion_run_id == ingestion_run_id for value in self.raw_responses.values()
            ),
            market_count=sum(
                snapshot.raw_response_id in self.raw_responses
                and self.raw_responses[snapshot.raw_response_id].ingestion_run_id
                == ingestion_run_id
                for snapshot in self.snapshots.values()
            ),
        )

    def evaluations_for_run(self, ingestion_run_id: str) -> tuple[ShadowMarketEvaluation, ...]:
        snapshot_ids = {
            snapshot.snapshot_id
            for snapshot in self.snapshots.values()
            if snapshot.raw_response_id in self.raw_responses
            and self.raw_responses[snapshot.raw_response_id].ingestion_run_id == ingestion_run_id
        }
        return tuple(
            sorted(
                (value for value in self.evaluations.values() if value.snapshot_id in snapshot_ids),
                key=lambda value: value.evaluation_id,
            )
        )
