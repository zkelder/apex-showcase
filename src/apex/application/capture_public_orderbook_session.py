"""Bounded foreground capture of one public Kalshi orderbook series.

The service deliberately owns timing and session state, but never a network
connection and a database transaction at the same time.  It reuses the
existing normalizers and conservative complement evaluator; a capture is only
retained public evidence and has no campaign, ledger, risk, order, or execution
authority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, cast

from apex.application.build_orderbook_observation_history import (
    BuildOrderbookObservationHistory,
    OrderbookHistoryReport,
    OrderbookHistoryRequest,
    OrderbookObservationHistoryError,
    OrderbookObservationHistoryStore,
)
from apex.application.discover_public_markets import (
    CapturedPublicMarketPage,
    PublicMarketHttpError,
    PublicMarketNormalizer,
    PublicMarketResponseError,
    PublicMarketTimeoutError,
)
from apex.application.scan_public_binary_complements import (
    COMPLEMENT_EVALUATOR_VERSION,
    CapturedKalshiPublicResponse,
    ComplementMarket,
    PublicOrderbookClient,
    RawComplementResponse,
    _active_fee_change,
    _fee_snapshot,
    _normalize_orderbook,
    _raw_response_id,
)
from apex.domain.public_complement import (
    BinaryComplementEvaluation,
    ComplementFillSlice,
    PublicOrderbookLevel,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
    evaluate_binary_complement,
)
from apex.domain.public_market import (
    NormalizedPublicMarketSnapshot,
    PublicMarketDataQualityState,
    PublicMarketProvider,
    ShadowMarketEvaluation,
    evaluate_public_market_snapshot,
)
from apex.domain.public_orderbook_history import OrderbookHistoryEvidence
from apex.domain.values import ContentHash, content_digest, deterministic_id, parse_decimal

CAPTURE_VERSION = "public-orderbook-capture-session-v1"
MAXIMUM_FAILURE_COUNT = 1
STALE_SESSION_GRACE = timedelta(seconds=30)
SINGLE_MARKET_ENDPOINT_IDENTITY = "KALSHI_GET_MARKET"
MARKETS_ENDPOINT_IDENTITY = "KALSHI_GET_MARKETS"
EVENTS_ENDPOINT_IDENTITY = "KALSHI_GET_EVENTS"


class PublicOrderbookCaptureError(RuntimeError):
    """A bounded public capture cannot proceed safely."""


class PublicOrderbookCaptureConflictError(PublicOrderbookCaptureError):
    """An immutable capture identity was reused with incompatible evidence."""


class PublicOrderbookCaptureTransientError(PublicOrderbookCaptureError):
    """A synthetic or classified provider failure that may consume one allowance."""


class PublicOrderbookCaptureTerminalError(PublicOrderbookCaptureError):
    """A provider, rule, or fee failure that must stop the foreground session."""


class _CandidateEnrichmentRejected(PublicOrderbookCaptureError):
    def __init__(
        self,
        code: str,
        *,
        event_succeeded: bool = False,
        series_succeeded: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.event_succeeded = event_succeeded
        self.series_succeeded = series_succeeded


class OrderbookCaptureSessionState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class OrderbookCaptureObservationState(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
    EVALUATION_FAILED = "EVALUATION_FAILED"
    SKIPPED_DEADLINE = "SKIPPED_DEADLINE"


@dataclass(frozen=True, slots=True)
class BoundedOrderbookCaptureRequest:
    """All foreground-capture limits, including its deterministic identity."""

    session_id: str
    market_ticker: str | None = None
    latest_open_market: bool = False
    discover_first: bool = False
    observations: int = 3
    interval_seconds: int = 10
    maximum_depth: int = 5
    maximum_bundle_quantity: str | int | Decimal = "10"
    timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if not self.session_id or len(self.session_id) > 160:
            raise ValueError("bounded capture session identity is required")
        if bool(self.market_ticker) == self.latest_open_market:
            raise ValueError("choose exactly one exact ticker or latest open market")
        if self.market_ticker is not None and (
            not self.market_ticker
            or len(self.market_ticker) > 300
            or any(character.isspace() for character in self.market_ticker)
        ):
            raise ValueError("exact market ticker is invalid")
        if self.discover_first and not self.latest_open_market:
            raise ValueError("discover-first is available only with latest open market")
        if isinstance(self.observations, bool) or not 3 <= self.observations <= 20:
            raise ValueError("capture observations must be between three and twenty")
        if isinstance(self.interval_seconds, bool) or not 5 <= self.interval_seconds <= 300:
            raise ValueError("capture interval must be between five and three hundred seconds")
        if isinstance(self.maximum_depth, bool) or not 1 <= self.maximum_depth <= 20:
            raise ValueError("capture depth must be between one and twenty")
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 30:
            raise ValueError("capture timeout must be between one and thirty seconds")
        quantity = parse_decimal(self.maximum_bundle_quantity, allow_zero=False)
        if quantity > 100:
            raise ValueError("capture maximum bundle quantity must not exceed one hundred")
        object.__setattr__(self, "maximum_bundle_quantity", quantity)
        if self.maximum_duration > timedelta(hours=2):
            raise ValueError("derived capture duration exceeds the bounded two-hour maximum")

    @property
    def maximum_duration(self) -> timedelta:
        return timedelta(
            seconds=((self.observations - 1) * self.interval_seconds) + self.timeout_seconds
        )

    @property
    def configuration_hash(self) -> ContentHash:
        return ContentHash(
            content_digest(
                {
                    "capture_version": CAPTURE_VERSION,
                    "provider": PublicMarketProvider.KALSHI,
                    "market_ticker": self.market_ticker,
                    "latest_open_market": self.latest_open_market,
                    "discover_first": self.discover_first,
                    "observations": self.observations,
                    "interval_seconds": self.interval_seconds,
                    "maximum_depth": self.maximum_depth,
                    "maximum_bundle_quantity": self.maximum_bundle_quantity,
                    "timeout_seconds": self.timeout_seconds,
                    "maximum_failure_count": MAXIMUM_FAILURE_COUNT,
                    "late_observation_policy": "skip-if-at-least-one-interval-late-v1",
                }
            )
        )


@dataclass(frozen=True, slots=True)
class OrderbookCaptureSession:
    session_id: str
    evidence_ingestion_run_id: str
    provider_market_ticker: str
    market_snapshot_id: str
    market_raw_response_id: str | None
    requested_observation_count: int
    requested_interval_seconds: int
    requested_depth: int
    requested_maximum_bundle_quantity: Decimal
    request_timeout_seconds: int
    maximum_duration: timedelta
    started_at: datetime
    completed_at: datetime | None
    state: OrderbookCaptureSessionState
    attempted_observation_count: int
    successful_observation_count: int
    failed_observation_count: int
    first_successful_observed_at: datetime | None
    last_successful_observed_at: datetime | None
    history_analysis_id: str | None
    canonical_configuration_hash: ContentHash
    canonical_input_hash: ContentHash
    canonical_result_hash: ContentHash | None
    created_at: datetime
    last_error_code: str | None

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("capture session start must be timezone-aware")
        object.__setattr__(self, "started_at", self.started_at.astimezone(UTC))
        if self.completed_at is not None:
            if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
                raise ValueError("capture session completion must be timezone-aware")
            object.__setattr__(self, "completed_at", self.completed_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class OrderbookCaptureObservation:
    observation_id: str
    session_id: str
    ordinal: int
    scheduled_at: datetime
    attempted_at: datetime | None
    completed_at: datetime | None
    state: OrderbookCaptureObservationState
    raw_response_id: str | None
    orderbook_snapshot_id: str | None
    fee_snapshot_id: str | None
    evaluation_id: str | None
    provider_received_at: datetime | None
    request_duration_milliseconds: int | None
    result_code: str
    canonical_input_hash: ContentHash
    canonical_observation_hash: ContentHash | None


@dataclass(frozen=True, slots=True)
class OrderbookCaptureReport:
    session: OrderbookCaptureSession
    observations: tuple[OrderbookCaptureObservation, ...]
    history: OrderbookHistoryReport | None
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class CaptureMarketCandidate:
    """A list-market candidate whose series identity awaits exact event evidence."""

    market_snapshot_id: str
    ingestion_run_id: str
    provider_market_ticker: str
    provider_event_ticker: str
    status: str
    observed_at: datetime
    close_time: datetime | None
    is_provisional: bool
    rule_content_hash: ContentHash
    data_quality_state: str
    volume: Decimal | None = None
    volume_24h: Decimal | None = None
    open_interest: Decimal | None = None
    liquidity: Decimal | None = None
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    no_bid: Decimal | None = None
    no_ask: Decimal | None = None
    yes_bid_size: Decimal | None = None
    yes_ask_size: Decimal | None = None
    series_identity_state: str = "PENDING_EVENT_ENRICHMENT"

    def __post_init__(self) -> None:
        if (
            not self.market_snapshot_id
            or not self.ingestion_run_id
            or not self.provider_market_ticker
            or not self.provider_event_ticker
            or self.status != "open"
            or self.series_identity_state != "PENDING_EVENT_ENRICHMENT"
        ):
            raise ValueError("capture candidate identity or lifecycle is invalid")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("capture candidate observation must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        if self.close_time is not None:
            if self.close_time.tzinfo is None or self.close_time.utcoffset() is None:
                raise ValueError("capture candidate close time must be timezone-aware")
            object.__setattr__(self, "close_time", self.close_time.astimezone(UTC))
        for field in ("volume", "volume_24h", "open_interest", "liquidity"):
            value = getattr(self, field)
            if value is not None:
                source = format(value.normalize(), "f") if isinstance(value, Decimal) else value
                object.__setattr__(self, field, parse_decimal(source, allow_negative=False))
        for field in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            value = getattr(self, field)
            if value is not None:
                source = format(value.normalize(), "f") if isinstance(value, Decimal) else value
                parsed = parse_decimal(source)
                if not Decimal(0) <= parsed <= Decimal(1):
                    raise ValueError("capture candidate list quote is outside the binary range")
                object.__setattr__(self, field, parsed)
        for field in ("yes_bid_size", "yes_ask_size"):
            value = getattr(self, field)
            if value is not None:
                source = format(value.normalize(), "f") if isinstance(value, Decimal) else value
                object.__setattr__(self, field, parse_decimal(source, allow_negative=False))

    @property
    def has_complete_list_two_sided_quotes(self) -> bool:
        """Whether retained list evidence has both bid/ask pairs coherently."""

        return (
            self.yes_bid is not None
            and self.yes_ask is not None
            and self.no_bid is not None
            and self.no_ask is not None
            and self.yes_bid <= self.yes_ask
            and self.no_bid <= self.no_ask
        )

    @property
    def list_immediate_cross_spread(self) -> Decimal | None:
        """A hint only: reproduce the later exact-book spread from list bids."""

        if not self.has_complete_list_two_sided_quotes:
            return None
        assert self.yes_bid is not None and self.no_bid is not None
        derived_yes_ask = Decimal(1) - self.no_bid
        if derived_yes_ask < self.yes_bid:
            return None
        return derived_yes_ask - self.yes_bid


@dataclass(frozen=True, slots=True)
class MarketRefresh:
    market: ComplementMarket
    raw_response_id: str
    response: CapturedKalshiPublicResponse
    snapshot: NormalizedPublicMarketSnapshot
    evaluation: ShadowMarketEvaluation
    raw_responses: tuple[RawComplementResponse, ...]
    initial_fee: SeriesFeeSnapshot | None


@dataclass(frozen=True, slots=True)
class CaptureEnrichmentReport:
    """Bounded exact-event candidate enrichment; no session is created here."""

    candidates_considered: int
    event_enrichments_attempted: int
    event_enrichments_succeeded: int
    event_membership_failures: int
    series_enrichments_succeeded: int
    fee_evidence_failures: int
    exact_market_refresh_successes: int
    rejection_counts: tuple[tuple[str, int], ...]
    selected: MarketRefresh | None


@dataclass(frozen=True, slots=True)
class CaptureCandidateEnrichmentResult:
    """One exact candidate-enrichment attempt without starting capture work.

    This is deliberately separate from orderbook capture.  A caller may build a
    shared market cohort from the exact event, series, and market lineage before
    deciding whether any strategy needs an orderbook.
    """

    selected: MarketRefresh | None
    reason_codes: tuple[str, ...]
    event_enrichment_succeeded: bool
    series_enrichment_succeeded: bool


@dataclass(frozen=True, slots=True)
class SharedOrderbookEvidence:
    """One generic orderbook observation with no complement evaluation."""

    raw: RawComplementResponse
    orderbook: PublicOrderbookSnapshot
    levels: tuple[PublicOrderbookLevel, ...]


@dataclass(frozen=True, slots=True)
class SuccessfulCaptureEvidence:
    observation: OrderbookCaptureObservation
    raw_responses: tuple[RawComplementResponse, ...]
    orderbook: PublicOrderbookSnapshot
    levels: tuple[PublicOrderbookLevel, ...]
    fee: SeriesFeeSnapshot | None
    evaluation: BinaryComplementEvaluation
    slices: tuple[ComplementFillSlice, ...]
    rule_evidence_hash: ContentHash


class PublicOrderbookCaptureClient(PublicOrderbookClient, Protocol):
    def fetch_event(
        self, *, event_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse: ...

    def fetch_market(
        self, *, market_ticker: str, timeout_seconds: int
    ) -> CapturedKalshiPublicResponse: ...


class PublicOrderbookCaptureStore(Protocol):
    def get_session(self, session_id: str) -> OrderbookCaptureReport | None: ...

    def select_capture_candidates(
        self, *, ingestion_run_id: str | None, limit: int
    ) -> tuple[CaptureMarketCandidate, ...]: ...

    def create_session(self, session: OrderbookCaptureSession, refresh: MarketRefresh) -> None: ...

    def transition_running(self, session_id: str) -> OrderbookCaptureSession: ...

    def append_success(self, evidence: SuccessfulCaptureEvidence) -> OrderbookCaptureSession: ...

    def append_failure(
        self,
        observation: OrderbookCaptureObservation,
        raw_responses: tuple[RawComplementResponse, ...],
    ) -> OrderbookCaptureSession: ...

    def session_evidence(self, session_id: str) -> tuple[OrderbookHistoryEvidence, ...]: ...

    def complete_with_history(
        self,
        *,
        session_id: str,
        completed_at: datetime,
        history: OrderbookHistoryReport,
    ) -> OrderbookCaptureReport: ...

    def terminate(
        self,
        *,
        session_id: str,
        state: OrderbookCaptureSessionState,
        completed_at: datetime,
        error_code: str,
    ) -> OrderbookCaptureReport: ...

    def reconcile_stale(
        self, *, now: datetime, grace: timedelta
    ) -> tuple[OrderbookCaptureSession, ...]: ...


def _response(value: object) -> CapturedKalshiPublicResponse:
    """Keep the protocol small while validating the existing captured response shape."""

    required = ("endpoint_identity", "received_at", "raw_payload", "document", "raw_payload_sha256")
    if not isinstance(value, CapturedKalshiPublicResponse) or not all(
        hasattr(value, name) for name in required
    ):
        raise PublicOrderbookCaptureTerminalError("provider response shape is incompatible")
    return value


def _response_id(response: CapturedKalshiPublicResponse, *, run_id: str, ordinal: int) -> str:
    return _raw_response_id(response, run_id=run_id, ordinal=ordinal)


def _result_hash(session: OrderbookCaptureSession, history: OrderbookHistoryReport) -> ContentHash:
    return ContentHash(
        content_digest(
            {
                "session_input_hash": session.canonical_input_hash,
                "attempted": session.attempted_observation_count,
                "successful": session.successful_observation_count,
                "failed": session.failed_observation_count,
                "history_analysis_id": history.analysis.analysis_id,
                "history_result_hash": history.analysis.canonical_result_hash,
            }
        )
    )


class CaptureBoundedPublicOrderbookSession:
    """Sequentially capture one exact market without background work or retries."""

    def __init__(
        self,
        *,
        client: PublicOrderbookCaptureClient,
        store: PublicOrderbookCaptureStore,
        wall_clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float],
        sleeper: Callable[[float], None],
        normalizer: PublicMarketNormalizer,
        progress: Callable[[str], None] | None = None,
        discover_first: Callable[[str, int], tuple[CaptureMarketCandidate, ...]] | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._wall_clock = wall_clock
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._normalizer = normalizer
        self._progress = progress or (lambda value: None)
        self._discover_first = discover_first

    @staticmethod
    def _input_hash(request: BoundedOrderbookCaptureRequest, refresh: MarketRefresh) -> ContentHash:
        response = _response(refresh.response)
        return ContentHash(
            content_digest(
                {
                    "configuration_hash": request.configuration_hash,
                    "session_id": request.session_id,
                    "market_snapshot_id": refresh.market.market_snapshot_id,
                    "market_raw_response_id": refresh.raw_response_id,
                    "market_raw_payload_sha256": response.raw_payload_sha256,
                    "rule_content_hash": refresh.market.rule_content_hash,
                    "series_ticker": refresh.market.series_ticker,
                    "enrichment_raw_payload_sha256": tuple(
                        value.response.raw_payload_sha256 for value in refresh.raw_responses
                    ),
                    "initial_fee_snapshot_id": (
                        refresh.initial_fee.fee_snapshot_id if refresh.initial_fee else None
                    ),
                }
            )
        )

    @staticmethod
    def _evidence_run_id(request: BoundedOrderbookCaptureRequest) -> str:
        return deterministic_id(
            "provider_ingestion_run",
            {
                "capture_session_id": request.session_id,
                "capability": "PUBLIC_REST_ORDERBOOK_CAPTURE",
            },
        )

    @staticmethod
    def _raw_response(
        response: CapturedKalshiPublicResponse, *, run_id: str, ordinal: int
    ) -> RawComplementResponse:
        return RawComplementResponse(
            raw_response_id=_response_id(response, run_id=run_id, ordinal=ordinal),
            ingestion_run_id=run_id,
            request_ordinal=ordinal,
            response=response,
        )

    @staticmethod
    def _provider_identifier(value: object) -> str | None:
        return (
            value
            if isinstance(value, str)
            and value
            and len(value) <= 300
            and not any(character.isspace() for character in value)
            else None
        )

    @staticmethod
    def _is_terminal_provider_boundary(error: Exception) -> bool:
        if isinstance(error, PublicMarketResponseError):
            return True
        if not isinstance(error, PublicMarketHttpError):
            return False
        message = str(error).lower()
        return "redirect" in message or any(
            f"http {status}" in message for status in (401, 403, 407)
        )

    def _refresh_exact_market(
        self,
        *,
        request: BoundedOrderbookCaptureRequest,
        market_ticker: str,
        started_at: datetime,
        selected_candidate: CaptureMarketCandidate | None = None,
        series_ticker: str | None = None,
        raw_responses: tuple[RawComplementResponse, ...] = (),
        initial_fee: SeriesFeeSnapshot | None = None,
    ) -> MarketRefresh:
        response = _response(
            self._client.fetch_market(
                market_ticker=market_ticker, timeout_seconds=request.timeout_seconds
            )
        )
        if response.endpoint_identity != SINGLE_MARKET_ENDPOINT_IDENTITY:
            raise PublicOrderbookCaptureTerminalError("single-market response endpoint is invalid")
        market_document = response.document.get("market")
        if (
            not isinstance(market_document, Mapping)
            or market_document.get("ticker") != market_ticker
        ):
            raise PublicOrderbookCaptureTerminalError("single-market response ticker conflicts")
        listed_series_ticker = self._provider_identifier(market_document.get("series_ticker"))
        effective_series_ticker = series_ticker or listed_series_ticker
        if effective_series_ticker is None:
            raise PublicOrderbookCaptureTerminalError("single-market response has no exact series")
        if listed_series_ticker is not None and listed_series_ticker != effective_series_ticker:
            raise PublicOrderbookCaptureTerminalError(
                "single-market response series conflicts with event lineage"
            )
        if raw_responses and response.received_at < raw_responses[-1].response.received_at:
            raise PublicOrderbookCaptureTerminalError(
                "single-market response predates its enrichment evidence"
            )
        evidence_run_id = self._evidence_run_id(request)
        raw = self._raw_response(response, run_id=evidence_run_id, ordinal=len(raw_responses) + 1)
        page = CapturedPublicMarketPage(
            endpoint_identity=MARKETS_ENDPOINT_IDENTITY,
            request_cursor=None,
            received_at=response.received_at,
            http_status=200,
            response_content_type=response.response_content_type,
            raw_payload=response.raw_payload,
            response_cursor=None,
            document={"markets": [market_document]},
        )
        snapshot = self._normalizer.normalize(raw_response_id=raw.raw_response_id, page=page)[0]
        evaluation = evaluate_public_market_snapshot(
            snapshot,
            evaluated_at=response.received_at,
            evaluator_version="public-market-structure-v1",
        )
        if (
            snapshot.provider_market_ticker != market_ticker
            or snapshot.status != "open"
            or snapshot.rule_content_hash is None
            or evaluation.state is not PublicMarketDataQualityState.ELIGIBLE
        ):
            raise PublicOrderbookCaptureTerminalError(
                "refreshed exact market is not structurally eligible and open"
            )
        if selected_candidate is not None:
            if selected_candidate.provider_market_ticker != snapshot.provider_market_ticker:
                raise PublicOrderbookCaptureTerminalError(
                    "refreshed exact market ticker conflicts with selected candidate"
                )
            if selected_candidate.provider_event_ticker != snapshot.event_ticker:
                raise PublicOrderbookCaptureTerminalError(
                    "refreshed exact market event lineage conflicts with selected candidate"
                )
            if selected_candidate.rule_content_hash != snapshot.rule_content_hash:
                raise PublicOrderbookCaptureTerminalError(
                    "refreshed exact market rule lineage conflicts with selected candidate"
                )
            if response.received_at < selected_candidate.observed_at:
                raise PublicOrderbookCaptureTerminalError(
                    "refreshed exact market predates selected candidate"
                )
        return MarketRefresh(
            market=ComplementMarket(
                market_snapshot_id=snapshot.snapshot_id,
                ingestion_run_id=evidence_run_id,
                provider_market_ticker=market_ticker,
                provider_event_ticker=snapshot.event_ticker,
                series_ticker=effective_series_ticker,
                status=snapshot.status,
                observed_at=response.received_at,
                rule_content_hash=snapshot.rule_content_hash,
                data_quality_state=evaluation.state.value,
            ),
            raw_response_id=raw.raw_response_id,
            response=response,
            snapshot=snapshot,
            evaluation=evaluation,
            raw_responses=(*raw_responses, raw),
            initial_fee=initial_fee,
        )

    def _enrich_candidate(
        self,
        *,
        request: BoundedOrderbookCaptureRequest,
        candidate: CaptureMarketCandidate,
    ) -> MarketRefresh:
        evidence_run_id = self._evidence_run_id(request)
        try:
            event_response = _response(
                self._client.fetch_event(
                    event_ticker=candidate.provider_event_ticker,
                    timeout_seconds=request.timeout_seconds,
                )
            )
        except Exception as error:
            if self._is_terminal_provider_boundary(error):
                raise PublicOrderbookCaptureTerminalError(
                    "event enrichment hit a terminal provider boundary"
                ) from error
            raise _CandidateEnrichmentRejected("EVENT_FETCH_FAILED") from error
        if event_response.endpoint_identity != EVENTS_ENDPOINT_IDENTITY:
            raise PublicOrderbookCaptureTerminalError("exact event response endpoint is invalid")
        event = event_response.document.get("event")
        if (
            not isinstance(event, Mapping)
            or event.get("event_ticker") != candidate.provider_event_ticker
        ):
            raise _CandidateEnrichmentRejected("EVENT_TICKER_MISMATCH")
        if event_response.received_at < candidate.observed_at:
            raise _CandidateEnrichmentRejected("EVENT_OBSERVATION_ORDER_INVALID")
        series_ticker = self._provider_identifier(event.get("series_ticker"))
        if series_ticker is None:
            raise _CandidateEnrichmentRejected(
                "EVENT_SERIES_IDENTITY_MISSING", event_succeeded=True
            )
        nested_markets = event.get("markets")
        if not isinstance(nested_markets, list):
            raise _CandidateEnrichmentRejected(
                "EVENT_MARKET_MEMBERSHIP_MISSING", event_succeeded=True
            )
        nested = next(
            (
                value
                for value in nested_markets
                if isinstance(value, Mapping)
                and value.get("ticker") == candidate.provider_market_ticker
            ),
            None,
        )
        if nested is None:
            raise _CandidateEnrichmentRejected(
                "EVENT_MARKET_MEMBERSHIP_MISSING", event_succeeded=True
            )
        nested_rule = nested.get("rules_primary")
        if (
            nested.get("event_ticker") != candidate.provider_event_ticker
            or not isinstance(nested_rule, str)
            or ContentHash(sha256(nested_rule.encode("utf-8")).hexdigest())
            != candidate.rule_content_hash
            or (
                (nested_series_ticker := self._provider_identifier(nested.get("series_ticker")))
                is not None
                and nested_series_ticker != series_ticker
            )
        ):
            raise _CandidateEnrichmentRejected(
                "EVENT_MARKET_LINEAGE_MISMATCH", event_succeeded=True
            )
        event_raw = self._raw_response(event_response, run_id=evidence_run_id, ordinal=1)
        try:
            series_response = _response(
                self._client.fetch_series(
                    series_ticker=series_ticker, timeout_seconds=request.timeout_seconds
                )
            )
        except Exception as error:
            if self._is_terminal_provider_boundary(error):
                raise PublicOrderbookCaptureTerminalError(
                    "series enrichment hit a terminal provider boundary"
                ) from error
            raise _CandidateEnrichmentRejected(
                "SERIES_FETCH_FAILED", event_succeeded=True
            ) from error
        series = series_response.document.get("series")
        if not isinstance(series, Mapping) or series.get("ticker") != series_ticker:
            raise _CandidateEnrichmentRejected("SERIES_IDENTITY_MISMATCH", event_succeeded=True)
        if series_response.received_at < event_response.received_at:
            raise _CandidateEnrichmentRejected(
                "SERIES_OBSERVATION_ORDER_INVALID", event_succeeded=True
            )
        series_raw = self._raw_response(series_response, run_id=evidence_run_id, ordinal=2)
        fee = _fee_snapshot(
            response=series_response,
            raw_response_id=series_raw.raw_response_id,
            run_id=evidence_run_id,
            series_ticker=series_ticker,
        )
        raw_responses: tuple[RawComplementResponse, ...] = (event_raw, series_raw)
        if fee.fee_type is None or fee.fee_multiplier is None:
            try:
                changes_response = _response(
                    self._client.fetch_series_fee_changes(
                        series_ticker=series_ticker, timeout_seconds=request.timeout_seconds
                    )
                )
            except Exception as error:
                if self._is_terminal_provider_boundary(error):
                    raise PublicOrderbookCaptureTerminalError(
                        "series-fee enrichment hit a terminal provider boundary"
                    ) from error
                raise _CandidateEnrichmentRejected(
                    "SERIES_FEE_EVIDENCE_UNSUPPORTED", event_succeeded=True
                ) from error
            changes_raw = self._raw_response(changes_response, run_id=evidence_run_id, ordinal=3)
            fee = _fee_snapshot(
                response=changes_response,
                raw_response_id=changes_raw.raw_response_id,
                run_id=evidence_run_id,
                series_ticker=series_ticker,
                fallback=_active_fee_change(
                    entries=changes_response.document.get("series_fee_change_arr"),
                    series_ticker=series_ticker,
                    observed_at=changes_response.received_at,
                ),
            )
            raw_responses = (*raw_responses, changes_raw)
        if fee.fee_type is None or fee.fee_multiplier is None:
            raise _CandidateEnrichmentRejected(
                "SERIES_FEE_EVIDENCE_UNSUPPORTED",
                event_succeeded=True,
            )
        try:
            return self._refresh_exact_market(
                request=request,
                market_ticker=candidate.provider_market_ticker,
                started_at=self._wall_clock().astimezone(UTC),
                selected_candidate=candidate,
                series_ticker=series_ticker,
                raw_responses=raw_responses,
                initial_fee=fee,
            )
        except PublicOrderbookCaptureTerminalError as error:
            message = str(error)
            code = (
                "MARKET_NO_LONGER_ACTIVE"
                if "not structurally eligible and open" in message
                else (
                    "RULE_EVIDENCE_CHANGED"
                    if "rule lineage" in message
                    else "MARKET_REFRESH_LINEAGE_MISMATCH"
                )
            )
            raise _CandidateEnrichmentRejected(
                code,
                event_succeeded=True,
                series_succeeded=True,
            ) from error
        except Exception as error:
            if self._is_terminal_provider_boundary(error):
                raise PublicOrderbookCaptureTerminalError(
                    "market refresh hit a terminal provider boundary"
                ) from error
            raise _CandidateEnrichmentRejected(
                "MARKET_REFRESH_FAILED",
                event_succeeded=True,
                series_succeeded=True,
            ) from error

    def enrich_candidates(
        self,
        *,
        request: BoundedOrderbookCaptureRequest,
        candidates: tuple[CaptureMarketCandidate, ...],
    ) -> CaptureEnrichmentReport:
        rejection_counts: dict[str, int] = {}
        attempted = 0
        event_succeeded = 0
        membership_failures = 0
        series_succeeded = 0
        fee_failures = 0
        refresh_succeeded = 0
        for candidate in candidates[:10]:
            attempted += 1
            try:
                selected = self._enrich_candidate(request=request, candidate=candidate)
            except _CandidateEnrichmentRejected as error:
                rejection_counts[error.code] = rejection_counts.get(error.code, 0) + 1
                event_succeeded += int(error.event_succeeded)
                series_succeeded += int(error.series_succeeded)
                membership_failures += int(error.code.startswith("EVENT_MARKET_"))
                fee_failures += int(error.code == "SERIES_FEE_EVIDENCE_UNSUPPORTED")
                continue
            event_succeeded += 1
            series_succeeded += 1
            refresh_succeeded += 1
            return CaptureEnrichmentReport(
                candidates_considered=min(len(candidates), 10),
                event_enrichments_attempted=attempted,
                event_enrichments_succeeded=event_succeeded,
                event_membership_failures=membership_failures,
                series_enrichments_succeeded=series_succeeded,
                fee_evidence_failures=fee_failures,
                exact_market_refresh_successes=refresh_succeeded,
                rejection_counts=tuple(sorted(rejection_counts.items())),
                selected=selected,
            )
        return CaptureEnrichmentReport(
            candidates_considered=min(len(candidates), 10),
            event_enrichments_attempted=attempted,
            event_enrichments_succeeded=event_succeeded,
            event_membership_failures=membership_failures,
            series_enrichments_succeeded=series_succeeded,
            fee_evidence_failures=fee_failures,
            exact_market_refresh_successes=refresh_succeeded,
            rejection_counts=tuple(sorted(rejection_counts.items())),
            selected=None,
        )

    def enrich_candidate(
        self,
        *,
        request: BoundedOrderbookCaptureRequest,
        candidate: CaptureMarketCandidate,
    ) -> CaptureCandidateEnrichmentResult:
        """Enrich exactly one list-market candidate before orderbook capture.

        Candidate-local data defects are represented as retained reason codes so
        a bounded cohort builder can continue with the next candidate.  Terminal
        provider and security boundaries still raise and stop the caller.
        """

        try:
            selected = self._enrich_candidate(request=request, candidate=candidate)
        except _CandidateEnrichmentRejected as error:
            return CaptureCandidateEnrichmentResult(
                selected=None,
                reason_codes=(error.code,),
                event_enrichment_succeeded=error.event_succeeded,
                series_enrichment_succeeded=error.series_succeeded,
            )
        return CaptureCandidateEnrichmentResult(
            selected=selected,
            reason_codes=(),
            event_enrichment_succeeded=True,
            series_enrichment_succeeded=True,
        )

    def capture_shared_orderbook(
        self,
        *,
        refresh: MarketRefresh,
        maximum_depth: int,
        timeout_seconds: int,
        request_ordinal: int,
    ) -> SharedOrderbookEvidence:
        """Fetch and normalize one shared book without complement research.

        The caller owns persistence and makes the evidence available to every
        compatible strategy.  No binary-complement evaluator, proposal, or
        financial action is involved.
        """

        if not 1 <= maximum_depth <= 20 or not 1 <= timeout_seconds <= 60:
            raise ValueError("shared orderbook capture bounds are invalid")
        if request_ordinal < 1:
            raise ValueError("shared orderbook request ordinal must be positive")
        try:
            response = _response(
                self._client.fetch_orderbook(
                    market_ticker=refresh.market.provider_market_ticker,
                    maximum_depth=maximum_depth,
                    timeout_seconds=timeout_seconds,
                )
            )
        except Exception as error:
            if self._is_terminal_provider_boundary(error):
                raise PublicOrderbookCaptureTerminalError(
                    "shared orderbook capture hit a terminal provider boundary"
                ) from error
            raise
        raw = RawComplementResponse(
            raw_response_id=_response_id(
                response,
                run_id=refresh.market.ingestion_run_id,
                ordinal=request_ordinal,
            ),
            ingestion_run_id=refresh.market.ingestion_run_id,
            request_ordinal=request_ordinal,
            response=response,
        )
        orderbook, levels = _normalize_orderbook(
            response=response,
            raw_response_id=raw.raw_response_id,
            market=refresh.market,
            maximum_depth=maximum_depth,
        )
        return SharedOrderbookEvidence(raw=raw, orderbook=orderbook, levels=levels)

    def _resolve_market(
        self, request: BoundedOrderbookCaptureRequest, started_at: datetime
    ) -> MarketRefresh:
        ticker = request.market_ticker
        if ticker is None:
            candidates = self._store.select_capture_candidates(ingestion_run_id=None, limit=10)
            if request.discover_first:
                if self._discover_first is None:
                    raise PublicOrderbookCaptureError(
                        "discover-first is unavailable in this capture composition"
                    )
                candidates = self._discover_first(request.session_id, request.timeout_seconds)
            if not candidates:
                raise PublicOrderbookCaptureError(
                    "no persisted structurally eligible open market exists; use --discover-first"
                )
            enrichment = self.enrich_candidates(request=request, candidates=candidates)
            if enrichment.selected is None:
                raise PublicOrderbookCaptureError(
                    "no capture candidate survived exact event enrichment"
                )
            self._progress(f"selected market {enrichment.selected.market.provider_market_ticker}")
            return enrichment.selected
        self._progress(f"selected market {ticker}")
        return self._refresh_exact_market(
            request=request,
            market_ticker=ticker,
            started_at=started_at,
        )

    @staticmethod
    def _new_session(
        request: BoundedOrderbookCaptureRequest, refresh: MarketRefresh, started_at: datetime
    ) -> OrderbookCaptureSession:
        return OrderbookCaptureSession(
            session_id=request.session_id,
            evidence_ingestion_run_id=refresh.market.ingestion_run_id,
            provider_market_ticker=refresh.market.provider_market_ticker,
            market_snapshot_id=refresh.market.market_snapshot_id,
            market_raw_response_id=refresh.raw_response_id,
            requested_observation_count=request.observations,
            requested_interval_seconds=request.interval_seconds,
            requested_depth=request.maximum_depth,
            requested_maximum_bundle_quantity=cast(Decimal, request.maximum_bundle_quantity),
            request_timeout_seconds=request.timeout_seconds,
            maximum_duration=request.maximum_duration,
            started_at=started_at,
            completed_at=None,
            state=OrderbookCaptureSessionState.CREATED,
            attempted_observation_count=0,
            successful_observation_count=0,
            failed_observation_count=0,
            first_successful_observed_at=None,
            last_successful_observed_at=None,
            history_analysis_id=None,
            canonical_configuration_hash=request.configuration_hash,
            canonical_input_hash=CaptureBoundedPublicOrderbookSession._input_hash(request, refresh),
            canonical_result_hash=None,
            created_at=started_at,
            last_error_code=None,
        )

    @staticmethod
    def _observation(
        *,
        session: OrderbookCaptureSession,
        ordinal: int,
        scheduled_at: datetime,
        attempted_at: datetime | None,
        completed_at: datetime | None,
        state: OrderbookCaptureObservationState,
        raw_response_id: str | None,
        orderbook_snapshot_id: str | None,
        fee_snapshot_id: str | None,
        evaluation_id: str | None,
        provider_received_at: datetime | None,
        request_duration_milliseconds: int | None,
        result_code: str,
    ) -> OrderbookCaptureObservation:
        payload = {
            "session_id": session.session_id,
            "ordinal": ordinal,
            "scheduled_at": scheduled_at,
            "state": state,
            "raw_response_id": raw_response_id,
            "orderbook_snapshot_id": orderbook_snapshot_id,
            "fee_snapshot_id": fee_snapshot_id,
            "evaluation_id": evaluation_id,
            "provider_received_at": provider_received_at,
            "request_duration_milliseconds": request_duration_milliseconds,
            "result_code": result_code,
        }
        input_hash = ContentHash(content_digest(payload))
        completed = state is OrderbookCaptureObservationState.SUCCEEDED
        return OrderbookCaptureObservation(
            observation_id=deterministic_id("orderbook_capture_observation", payload),
            session_id=session.session_id,
            ordinal=ordinal,
            scheduled_at=scheduled_at,
            attempted_at=attempted_at,
            completed_at=completed_at,
            state=state,
            raw_response_id=raw_response_id,
            orderbook_snapshot_id=orderbook_snapshot_id,
            fee_snapshot_id=fee_snapshot_id,
            evaluation_id=evaluation_id,
            provider_received_at=provider_received_at,
            request_duration_milliseconds=request_duration_milliseconds,
            result_code=result_code,
            canonical_input_hash=input_hash,
            canonical_observation_hash=ContentHash(content_digest(payload)) if completed else None,
        )

    @staticmethod
    def _transient(error: Exception) -> bool:
        if isinstance(error, (PublicOrderbookCaptureTransientError, PublicMarketTimeoutError)):
            return True
        if isinstance(error, PublicMarketHttpError):
            return any(f"HTTP {status}" in str(error) for status in (429, 500, 502, 503, 504))
        return False

    def _failure(
        self,
        *,
        session: OrderbookCaptureSession,
        ordinal: int,
        scheduled_at: datetime,
        attempted_at: datetime | None,
        state: OrderbookCaptureObservationState,
        code: str,
        raw_responses: tuple[RawComplementResponse, ...] = (),
        duration_milliseconds: int | None = None,
    ) -> OrderbookCaptureSession:
        observation = self._observation(
            session=session,
            ordinal=ordinal,
            scheduled_at=scheduled_at,
            attempted_at=attempted_at,
            completed_at=self._wall_clock().astimezone(UTC),
            state=state,
            raw_response_id=raw_responses[0].raw_response_id if raw_responses else None,
            orderbook_snapshot_id=None,
            fee_snapshot_id=None,
            evaluation_id=None,
            provider_received_at=(
                _response(raw_responses[0].response).received_at if raw_responses else None
            ),
            request_duration_milliseconds=duration_milliseconds,
            result_code=code,
        )
        return self._store.append_failure(observation, raw_responses)

    def _capture_one(
        self,
        *,
        request: BoundedOrderbookCaptureRequest,
        session: OrderbookCaptureSession,
        refresh: MarketRefresh,
        ordinal: int,
        scheduled_at: datetime,
        fee: SeriesFeeSnapshot | None,
    ) -> tuple[OrderbookCaptureSession, SeriesFeeSnapshot | None, bool]:
        attempted_at = self._wall_clock().astimezone(UTC)
        request_started = self._monotonic_clock()
        raw_responses: list[RawComplementResponse] = []
        try:
            orderbook_response = _response(
                self._client.fetch_orderbook(
                    market_ticker=session.provider_market_ticker,
                    maximum_depth=request.maximum_depth,
                    timeout_seconds=request.timeout_seconds,
                )
            )
        except Exception as error:
            updated = self._failure(
                session=session,
                ordinal=ordinal,
                scheduled_at=scheduled_at,
                attempted_at=attempted_at,
                state=OrderbookCaptureObservationState.PROVIDER_FAILED,
                code="PROVIDER_TRANSIENT_FAILURE"
                if self._transient(error)
                else "PROVIDER_TERMINAL_FAILURE",
                duration_milliseconds=max(
                    0, int((self._monotonic_clock() - request_started) * 1000)
                ),
            )
            if self._is_terminal_provider_boundary(error):
                self._store.terminate(
                    session_id=updated.session_id,
                    state=OrderbookCaptureSessionState.FAILED,
                    completed_at=self._wall_clock().astimezone(UTC),
                    error_code="PROVIDER_TERMINAL_FAILURE",
                )
                raise PublicOrderbookCaptureTerminalError(
                    "orderbook capture hit a terminal provider boundary"
                ) from error
            return updated, fee, self._transient(error)

        orderbook_raw_id = _response_id(
            orderbook_response,
            run_id=session.evidence_ingestion_run_id,
            ordinal=10_000 + ordinal * 10,
        )
        raw_responses.append(
            RawComplementResponse(
                raw_response_id=orderbook_raw_id,
                ingestion_run_id=session.evidence_ingestion_run_id,
                request_ordinal=10_000 + ordinal * 10,
                response=orderbook_response,
            )
        )
        try:
            orderbook, levels = _normalize_orderbook(
                response=orderbook_response,
                raw_response_id=orderbook_raw_id,
                market=refresh.market,
                maximum_depth=request.maximum_depth,
            )
        except Exception:
            updated = self._failure(
                session=session,
                ordinal=ordinal,
                scheduled_at=scheduled_at,
                attempted_at=attempted_at,
                state=OrderbookCaptureObservationState.NORMALIZATION_FAILED,
                code="ORDERBOOK_NORMALIZATION_FAILED",
                raw_responses=tuple(raw_responses),
                duration_milliseconds=max(
                    0, int((self._monotonic_clock() - request_started) * 1000)
                ),
            )
            return updated, fee, False

        try:
            if fee is None:
                series_response = _response(
                    self._client.fetch_series(
                        series_ticker=cast(str, refresh.market.series_ticker),
                        timeout_seconds=request.timeout_seconds,
                    )
                )
                series_raw_id = _response_id(
                    series_response,
                    run_id=session.evidence_ingestion_run_id,
                    ordinal=10_001 + ordinal * 10,
                )
                raw_responses.append(
                    RawComplementResponse(
                        raw_response_id=series_raw_id,
                        ingestion_run_id=session.evidence_ingestion_run_id,
                        request_ordinal=10_001 + ordinal * 10,
                        response=series_response,
                    )
                )
                fee = _fee_snapshot(
                    response=series_response,
                    raw_response_id=series_raw_id,
                    run_id=session.evidence_ingestion_run_id,
                    series_ticker=cast(str, refresh.market.series_ticker),
                )
                if fee.fee_type is None or fee.fee_multiplier is None:
                    changes = _response(
                        self._client.fetch_series_fee_changes(
                            series_ticker=cast(str, refresh.market.series_ticker),
                            timeout_seconds=request.timeout_seconds,
                        )
                    )
                    changes_raw_id = _response_id(
                        changes,
                        run_id=session.evidence_ingestion_run_id,
                        ordinal=10_002 + ordinal * 10,
                    )
                    raw_responses.append(
                        RawComplementResponse(
                            raw_response_id=changes_raw_id,
                            ingestion_run_id=session.evidence_ingestion_run_id,
                            request_ordinal=10_002 + ordinal * 10,
                            response=changes,
                        )
                    )
                    fee = _fee_snapshot(
                        response=changes,
                        raw_response_id=changes_raw_id,
                        run_id=session.evidence_ingestion_run_id,
                        series_ticker=cast(str, refresh.market.series_ticker),
                        fallback=_active_fee_change(
                            entries=changes.document.get("series_fee_change_arr"),
                            series_ticker=cast(str, refresh.market.series_ticker),
                            observed_at=changes.received_at,
                        ),
                    )
                if fee.fee_type is None or fee.fee_multiplier is None:
                    raise PublicOrderbookCaptureTerminalError("fee evidence is incompatible")
        except Exception as error:
            updated = self._failure(
                session=session,
                ordinal=ordinal,
                scheduled_at=scheduled_at,
                attempted_at=attempted_at,
                state=OrderbookCaptureObservationState.PROVIDER_FAILED,
                code="FEE_TRANSIENT_FAILURE"
                if self._transient(error)
                else "FEE_EVIDENCE_INCOMPATIBLE",
                raw_responses=tuple(raw_responses),
                duration_milliseconds=max(
                    0, int((self._monotonic_clock() - request_started) * 1000)
                ),
            )
            if self._is_terminal_provider_boundary(error):
                self._store.terminate(
                    session_id=updated.session_id,
                    state=OrderbookCaptureSessionState.FAILED,
                    completed_at=self._wall_clock().astimezone(UTC),
                    error_code="FEE_PROVIDER_TERMINAL_FAILURE",
                )
                raise PublicOrderbookCaptureTerminalError(
                    "series-fee capture hit a terminal provider boundary"
                ) from error
            return updated, fee, self._transient(error)

        try:
            evaluation, slices = evaluate_binary_complement(
                orderbook=orderbook,
                fee=fee,
                levels=levels,
                market_is_open=True,
                rule_content_hash=refresh.market.rule_content_hash,
                maximum_bundle_quantity=cast(Decimal, request.maximum_bundle_quantity),
                evaluated_at=orderbook_response.received_at,
                evaluator_version=COMPLEMENT_EVALUATOR_VERSION,
            )
        except Exception:
            updated = self._failure(
                session=session,
                ordinal=ordinal,
                scheduled_at=scheduled_at,
                attempted_at=attempted_at,
                state=OrderbookCaptureObservationState.EVALUATION_FAILED,
                code="COMPLEMENT_EVALUATION_FAILED",
                raw_responses=tuple(raw_responses),
                duration_milliseconds=max(
                    0, int((self._monotonic_clock() - request_started) * 1000)
                ),
            )
            return updated, fee, False

        duration = max(0, int((self._monotonic_clock() - request_started) * 1000))
        observation = self._observation(
            session=session,
            ordinal=ordinal,
            scheduled_at=scheduled_at,
            attempted_at=attempted_at,
            completed_at=self._wall_clock().astimezone(UTC),
            state=OrderbookCaptureObservationState.SUCCEEDED,
            raw_response_id=orderbook_raw_id,
            orderbook_snapshot_id=orderbook.orderbook_snapshot_id,
            fee_snapshot_id=fee.fee_snapshot_id,
            evaluation_id=evaluation.evaluation_id,
            provider_received_at=orderbook_response.received_at,
            request_duration_milliseconds=duration,
            result_code=evaluation.state.value,
        )
        updated = self._store.append_success(
            SuccessfulCaptureEvidence(
                observation=observation,
                raw_responses=tuple(raw_responses),
                orderbook=orderbook,
                levels=levels,
                fee=fee,
                evaluation=evaluation,
                slices=slices,
                rule_evidence_hash=cast(ContentHash, refresh.market.rule_content_hash),
            )
        )
        return updated, fee, True

    def capture(self, request: BoundedOrderbookCaptureRequest) -> OrderbookCaptureReport:
        existing = self._store.get_session(request.session_id)
        if existing is not None:
            if existing.session.canonical_configuration_hash != request.configuration_hash:
                raise PublicOrderbookCaptureConflictError("capture session identity conflicts")
            return replace(existing, idempotent_replay=True)

        started_at = self._wall_clock().astimezone(UTC)
        refresh = self._resolve_market(request, started_at)
        return self.capture_prepared(request, refresh)

    def capture_candidates(
        self, *, ingestion_run_id: str | None, limit: int
    ) -> tuple[CaptureMarketCandidate, ...]:
        """Expose retained structural candidates without duplicating store selection."""

        return self._store.select_capture_candidates(ingestion_run_id=ingestion_run_id, limit=limit)

    def session_evidence(self, session_id: str) -> tuple[OrderbookHistoryEvidence, ...]:
        """Return the exact retained evidence behind one completed or failed session."""

        return self._store.session_evidence(session_id)

    def capture_report(self, session_id: str) -> OrderbookCaptureReport | None:
        """Load a terminally failed session without reusing its capture identity."""

        return self._store.get_session(session_id)

    def capture_prepared(
        self,
        request: BoundedOrderbookCaptureRequest,
        refresh: MarketRefresh,
    ) -> OrderbookCaptureReport:
        """Capture a previously exact-enriched market without another selection request."""

        existing = self._store.get_session(request.session_id)
        if existing is not None:
            if existing.session.canonical_configuration_hash != request.configuration_hash:
                raise PublicOrderbookCaptureConflictError("capture session identity conflicts")
            return replace(existing, idempotent_replay=True)
        if (
            (
                request.market_ticker is not None
                and request.market_ticker != refresh.market.provider_market_ticker
            )
            or refresh.market.status != "open"
            or refresh.market.rule_content_hash is None
            or refresh.market.series_ticker is None
        ):
            raise PublicOrderbookCaptureTerminalError(
                "prepared capture refresh conflicts with its exact request"
            )

        started_at = self._wall_clock().astimezone(UTC)
        created = self._new_session(request, refresh, started_at)
        self._store.create_session(created, refresh)
        session = self._store.transition_running(created.session_id)
        self._progress(f"session {session.session_id}")
        start_monotonic = self._monotonic_clock()
        fee = refresh.initial_fee

        for ordinal in range(request.observations):
            scheduled_monotonic = start_monotonic + (ordinal * request.interval_seconds)
            scheduled_at = started_at + timedelta(seconds=ordinal * request.interval_seconds)
            now = self._monotonic_clock()
            if now < scheduled_monotonic:
                self._sleeper(scheduled_monotonic - now)
                now = self._monotonic_clock()
            self._progress(f"observation {ordinal + 1}/{request.observations}")
            if now - scheduled_monotonic >= request.interval_seconds:
                session = self._failure(
                    session=session,
                    ordinal=ordinal,
                    scheduled_at=scheduled_at,
                    attempted_at=None,
                    state=OrderbookCaptureObservationState.SKIPPED_DEADLINE,
                    code="SCHEDULING_DEADLINE_SKIPPED",
                )
                continue
            if now - start_monotonic > request.maximum_duration.total_seconds():
                self._failure(
                    session=session,
                    ordinal=ordinal,
                    scheduled_at=scheduled_at,
                    attempted_at=None,
                    state=OrderbookCaptureObservationState.SKIPPED_DEADLINE,
                    code="SESSION_DURATION_EXCEEDED",
                )
                return self._store.terminate(
                    session_id=session.session_id,
                    state=OrderbookCaptureSessionState.FAILED,
                    completed_at=self._wall_clock().astimezone(UTC),
                    error_code="SESSION_DURATION_EXCEEDED",
                )
            try:
                session, fee, transient = self._capture_one(
                    request=request,
                    session=session,
                    refresh=refresh,
                    ordinal=ordinal,
                    scheduled_at=scheduled_at,
                    fee=fee,
                )
            except KeyboardInterrupt:
                return self._store.terminate(
                    session_id=session.session_id,
                    state=OrderbookCaptureSessionState.INTERRUPTED,
                    completed_at=self._wall_clock().astimezone(UTC),
                    error_code="INTERRUPTED",
                )
            if not transient and session.last_error_code is not None:
                return self._store.terminate(
                    session_id=session.session_id,
                    state=OrderbookCaptureSessionState.FAILED,
                    completed_at=self._wall_clock().astimezone(UTC),
                    error_code=session.last_error_code,
                )
            if session.failed_observation_count > MAXIMUM_FAILURE_COUNT:
                return self._store.terminate(
                    session_id=session.session_id,
                    state=OrderbookCaptureSessionState.FAILED,
                    completed_at=self._wall_clock().astimezone(UTC),
                    error_code="MAXIMUM_PROVIDER_FAILURES_EXCEEDED",
                )

        evidence = self._store.session_evidence(session.session_id)
        if len(evidence) < 3:
            return self._store.terminate(
                session_id=session.session_id,
                state=OrderbookCaptureSessionState.FAILED,
                completed_at=self._wall_clock().astimezone(UTC),
                error_code="INSUFFICIENT_SUCCESSFUL_OBSERVATIONS",
            )
        history_request = OrderbookHistoryRequest(
            provider_market_ticker=session.provider_market_ticker,
            ingestion_run_id=session.evidence_ingestion_run_id,
            maximum_observations=request.observations,
            continuity_gap=timedelta(seconds=max(request.interval_seconds * 2, 5)),
        )
        try:
            history = BuildOrderbookObservationHistory(
                store=cast("OrderbookObservationHistoryStore", self._store)
            ).prepare(history_request, evidence)
        except OrderbookObservationHistoryError as error:
            return self._store.terminate(
                session_id=session.session_id,
                state=OrderbookCaptureSessionState.FAILED,
                completed_at=self._wall_clock().astimezone(UTC),
                error_code=f"HISTORY_{type(error).__name__.upper()}",
            )
        completed = self._store.complete_with_history(
            session_id=session.session_id,
            completed_at=self._wall_clock().astimezone(UTC),
            history=history,
        )
        return completed


class InMemoryPublicOrderbookCaptureStore:
    """Synthetic test store with no database, financial, or execution surface."""

    def __init__(self, *, candidates: tuple[CaptureMarketCandidate, ...] = ()) -> None:
        self.candidates = candidates
        self._reports: dict[str, OrderbookCaptureReport] = {}
        self._evidence: dict[str, list[OrderbookHistoryEvidence]] = {}
        self._observations: dict[str, list[OrderbookCaptureObservation]] = {}

    def get_session(self, session_id: str) -> OrderbookCaptureReport | None:
        return self._reports.get(session_id)

    def select_capture_candidates(
        self, *, ingestion_run_id: str | None, limit: int
    ) -> tuple[CaptureMarketCandidate, ...]:
        values = (
            self.candidates
            if ingestion_run_id is None
            else tuple(
                value for value in self.candidates if value.ingestion_run_id == ingestion_run_id
            )
        )
        return values[:limit]

    def create_session(self, session: OrderbookCaptureSession, refresh: MarketRefresh) -> None:
        del refresh
        if session.session_id in self._reports:
            raise PublicOrderbookCaptureConflictError("capture session already exists")
        self._reports[session.session_id] = OrderbookCaptureReport(session, (), None, False)
        self._observations[session.session_id] = []
        self._evidence[session.session_id] = []

    def _replace(self, session: OrderbookCaptureSession) -> None:
        prior = self._reports[session.session_id]
        self._reports[session.session_id] = OrderbookCaptureReport(
            session, tuple(self._observations[session.session_id]), prior.history, False
        )

    def transition_running(self, session_id: str) -> OrderbookCaptureSession:
        session = self._reports[session_id].session
        if session.state is not OrderbookCaptureSessionState.CREATED:
            raise PublicOrderbookCaptureConflictError("capture session cannot start")
        updated = replace(session, state=OrderbookCaptureSessionState.RUNNING)
        self._replace(updated)
        return updated

    def _append(self, observation: OrderbookCaptureObservation) -> OrderbookCaptureSession:
        report = self._reports[observation.session_id]
        session = report.session
        existing = next(
            (
                value
                for value in self._observations[observation.session_id]
                if value.ordinal == observation.ordinal
            ),
            None,
        )
        if existing is not None:
            if existing != observation:
                raise PublicOrderbookCaptureConflictError("capture observation identity conflicts")
            return session
        if session.state is not OrderbookCaptureSessionState.RUNNING:
            raise PublicOrderbookCaptureConflictError("capture session is not running")
        successful = observation.state is OrderbookCaptureObservationState.SUCCEEDED
        failed = not successful
        updated = replace(
            session,
            attempted_observation_count=session.attempted_observation_count + 1,
            successful_observation_count=session.successful_observation_count + int(successful),
            failed_observation_count=session.failed_observation_count + int(failed),
            first_successful_observed_at=(
                observation.provider_received_at
                if successful and session.first_successful_observed_at is None
                else session.first_successful_observed_at
            ),
            last_successful_observed_at=(
                observation.provider_received_at
                if successful
                else session.last_successful_observed_at
            ),
            last_error_code=None if successful else observation.result_code,
        )
        self._observations[observation.session_id].append(observation)
        self._replace(updated)
        return updated

    def append_success(self, evidence: SuccessfulCaptureEvidence) -> OrderbookCaptureSession:
        updated = self._append(evidence.observation)
        if evidence.observation.orderbook_snapshot_id not in {
            value.orderbook.orderbook_snapshot_id
            for value in self._evidence[evidence.observation.session_id]
        }:
            self._evidence[evidence.observation.session_id].append(
                OrderbookHistoryEvidence(
                    orderbook=evidence.orderbook,
                    evaluation=evidence.evaluation,
                    fee=evidence.fee,
                    rule_evidence_hash=evidence.rule_evidence_hash,
                )
            )
        return updated

    def append_failure(
        self,
        observation: OrderbookCaptureObservation,
        raw_responses: tuple[RawComplementResponse, ...],
    ) -> OrderbookCaptureSession:
        del raw_responses
        return self._append(observation)

    def session_evidence(self, session_id: str) -> tuple[OrderbookHistoryEvidence, ...]:
        return tuple(self._evidence[session_id])

    def complete_with_history(
        self,
        *,
        session_id: str,
        completed_at: datetime,
        history: OrderbookHistoryReport,
    ) -> OrderbookCaptureReport:
        report = self._reports[session_id]
        session = report.session
        if session.state is not OrderbookCaptureSessionState.RUNNING:
            raise PublicOrderbookCaptureConflictError("capture session cannot complete")
        complete = replace(
            session,
            state=OrderbookCaptureSessionState.COMPLETED,
            completed_at=completed_at,
            history_analysis_id=history.analysis.analysis_id,
            canonical_result_hash=_result_hash(session, history),
            last_error_code=None,
        )
        completed_report = OrderbookCaptureReport(
            complete, tuple(self._observations[session_id]), history, False
        )
        self._reports[session_id] = completed_report
        return completed_report

    def terminate(
        self,
        *,
        session_id: str,
        state: OrderbookCaptureSessionState,
        completed_at: datetime,
        error_code: str,
    ) -> OrderbookCaptureReport:
        if state not in {
            OrderbookCaptureSessionState.FAILED,
            OrderbookCaptureSessionState.INTERRUPTED,
        }:
            raise ValueError("capture termination state is invalid")
        report = self._reports[session_id]
        session = report.session
        if session.state is not OrderbookCaptureSessionState.RUNNING:
            return report
        final = replace(session, state=state, completed_at=completed_at, last_error_code=error_code)
        result = OrderbookCaptureReport(final, tuple(self._observations[session_id]), None, False)
        self._reports[session_id] = result
        return result

    def reconcile_stale(
        self, *, now: datetime, grace: timedelta
    ) -> tuple[OrderbookCaptureSession, ...]:
        values: list[OrderbookCaptureSession] = []
        for session_id, report in tuple(self._reports.items()):
            session = report.session
            if (
                session.state is OrderbookCaptureSessionState.RUNNING
                and now >= session.started_at + session.maximum_duration + grace
            ):
                values.append(
                    self.terminate(
                        session_id=session_id,
                        state=OrderbookCaptureSessionState.INTERRUPTED,
                        completed_at=now,
                        error_code="STALE_RUNNING_SESSION",
                    ).session
                )
        return tuple(values)
