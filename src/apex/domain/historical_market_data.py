"""Historical market-data evidence: capture plans, dataset snapshots, point-in-time
calculations, and a minimal cross-venue replay seam.

Reuse, not a parallel backtesting architecture
------------------------------------------------
This module deliberately does **not** define a new bar/observation table. Daily
OHLCV evidence for U.S. equities/ETFs continues to live exclusively in
``domain.securities`` (``SecuritiesInstrument``/``SecuritiesBarEvidence``/
``SecuritiesCorporateActionEvidence``/``SecuritiesMarketDataBatch``, migration
``0025``), captured through the existing ``domain.real_securities_data`` /
``application.real_securities_data`` / ``adapters.alpaca_market_data`` provider
boundary (migration ``0031``) exactly as today's forward/engineering-validation
paths already do. That mirrors the established repository convention (crypto's
own ``crypto_daily_candle`` stays crypto's typed bar store; it is not folded
into the generic ``research_observation`` bag either -- see
``domain.crypto_research``) and satisfies "prefer reusing established bar/
evidence tables" and "do not duplicate an existing durable bar store".

What is genuinely new here is the layer *above* one bounded provider fetch: a
plan spanning many chunked, checkpointed, resumable fetches over a named
stable-security cohort; a deterministic, content-addressed dataset snapshot
binding that cohort/date-range/policy to the exact retained evidence it drew
on; explicit point-in-time/anti-lookahead guards; a small set of reusable
Decimal calculations that only ever read evidence available at a caller-
supplied ``as_of``; and a minimal cross-venue observation shape so a future
replay engine can iterate equity/ETF and crypto evidence uniformly. None of
this grants Strategy, Opportunity, PAPER, or LIVE authority; every retained
record below is ``authority = "NONE"``.

Identity anchor
----------------
Every instrument this module mints is keyed off ``stable_security_id`` (see
``domain.security_identity``) via ``historical_instrument_id`` -- a pure
function, so the link from a persisted ``SecuritiesInstrument.instrument_id``
back to its stable identity is always recomputable, never merely a foreign key
one has to trust. ``historical_instrument_identity`` (the adapter-layer table)
exists only to make that lineage directly queryable, not to define it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from itertools import pairwise

from apex.domain.real_securities_data import SecuritiesSurvivorshipState
from apex.domain.research_evidence import EvidenceClass
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
)
from apex.domain.values import (
    canonical_timestamp,
    content_digest,
    deterministic_id,
    parse_decimal,
)

HISTORICAL_MARKET_DATA_V1 = "HISTORICAL_MARKET_DATA_V1"
HISTORICAL_DATA_WARNING = (
    "HISTORICAL MARKET DATA IS RESEARCH EVIDENCE, NOT A TRADING EDGE OR EXECUTION AUTHORITY"
)
SURVIVORSHIP_WARNING = (
    "CAPTURE UNIVERSE USES TODAY'S STABLE SECURITY IDENTITIES APPLIED RETROACTIVELY -- "
    "NOT A VERIFIED POINT-IN-TIME HISTORICAL LISTING/DELISTING UNIVERSE"
)
POINT_IN_TIME_UNIVERSE_INCOMPLETE = "POINT_IN_TIME_UNIVERSE_INCOMPLETE"
MISSING_DATA_POLICY_V1 = "FAIL_CLOSED_NO_FORWARD_FILL_V1"
ALPACA_RAW_NO_ADJUSTMENT_V1 = "ALPACA_IEX_RAW_NO_ADJUSTMENT_V1"


class HistoricalMarketDataError(ValueError):
    """A historical market-data evidence invariant was violated."""


class PointInTimeError(HistoricalMarketDataError):
    """An operation would have let evidence after ``as_of`` enter a decision."""


class HistoricalObservationType(StrEnum):
    DAILY_BAR = "DAILY_BAR"


class HistoricalCapturePlanState(StrEnum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    CREDENTIALS_REQUIRED = "CREDENTIALS_REQUIRED"
    FAILED = "FAILED"


class HistoricalCaptureChunkState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CREDENTIALS_REQUIRED = "CREDENTIALS_REQUIRED"
    FAILED = "FAILED"


class HistoricalDatasetCompletenessState(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class PointInTimeUniverseState(StrEnum):
    """Named exactly per milestone spec so the limitation is impossible to miss."""

    POINT_IN_TIME_UNIVERSE_INCOMPLETE = "POINT_IN_TIME_UNIVERSE_INCOMPLETE"
    POINT_IN_TIME_UNIVERSE_VERIFIED = "POINT_IN_TIME_UNIVERSE_VERIFIED"


class SecurityCaptureOutcome(StrEnum):
    CAPTURED = "CAPTURED"
    NOT_YET_LISTED = "NOT_YET_LISTED"
    DELISTED = "DELISTED"
    PROVIDER_NO_DATA = "PROVIDER_NO_DATA"
    REQUEST_FAILED = "REQUEST_FAILED"
    TRADING_DAY_ABSENT = "TRADING_DAY_ABSENT"
    SUSPENDED_NO_TRADE = "SUSPENDED_NO_TRADE"
    IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
    UNIVERSE_MEMBERSHIP_UNKNOWN = "UNIVERSE_MEMBERSHIP_UNKNOWN"


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HistoricalMarketDataError(f"{label} must be timezone-aware")
    return value


def _iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise HistoricalMarketDataError(f"{label} must be an ISO date") from error


def _bounded_decimal(value: Decimal) -> Decimal:
    """The repository-wide numeric convention: 28-digit working precision,
    quantized to eight decimal places, re-validated as an authoritative
    ``Decimal`` (matches ``securities._bounded_decimal``)."""

    with localcontext() as context:
        context.prec = 28
        return parse_decimal(value.quantize(Decimal("0.00000001")), allow_negative=True)


def historical_instrument_id(*, stable_security_id: str, asset_class: str) -> str:
    """Deterministic instrument identity anchored to ``stable_security_id``.

    A pure function of only the stable identity and asset class, so any
    consumer can recompute the identical ``instrument_id`` a
    ``SecuritiesInstrument`` was minted with -- the stable-security lineage is
    always independently recoverable, never merely a foreign key one must
    trust unread.
    """

    if not stable_security_id:
        raise HistoricalMarketDataError("HISTORICAL_INSTRUMENT_STABLE_ID_REQUIRED")
    if not asset_class:
        raise HistoricalMarketDataError("HISTORICAL_INSTRUMENT_ASSET_CLASS_REQUIRED")
    return deterministic_id(
        "historical_instrument",
        (HISTORICAL_MARKET_DATA_V1, stable_security_id, asset_class),
    )


@dataclass(frozen=True, slots=True)
class HistoricalCaptureUniverseMember:
    """One capture-plan universe member, anchored on ``stable_security_id``."""

    stable_security_id: str
    symbol_at_capture: str
    asset_class: str
    instrument_id: str

    @classmethod
    def create(
        cls, *, stable_security_id: str, symbol_at_capture: str, asset_class: str
    ) -> HistoricalCaptureUniverseMember:
        if not stable_security_id:
            raise HistoricalMarketDataError("CAPTURE_UNIVERSE_STABLE_ID_REQUIRED")
        normalized_symbol = symbol_at_capture.strip().upper()
        if not normalized_symbol:
            raise HistoricalMarketDataError("CAPTURE_UNIVERSE_SYMBOL_REQUIRED")
        return cls(
            stable_security_id=stable_security_id,
            symbol_at_capture=normalized_symbol,
            asset_class=asset_class,
            instrument_id=historical_instrument_id(
                stable_security_id=stable_security_id, asset_class=asset_class
            ),
        )


@dataclass(frozen=True, slots=True)
class HistoricalCapturePolicy:
    """A bounded, explicit historical-capture policy.

    Every numeric bound below is required and finite -- there is deliberately
    no "unlimited" default. ``chunk_size`` is expected to be sourced from the
    existing ``SecuritiesProviderConfiguration.maximum_symbols_per_operation``
    so this module never invents a second, competing provider bound.
    """

    policy_id: str
    provider: str
    observation_type: HistoricalObservationType
    bar_interval: SecuritiesBarInterval
    adjustment_state: SecuritiesAdjustmentState
    adjustment_policy_id: str
    start_date: str
    end_date: str
    chunk_size: int
    maximum_instruments: int
    maximum_requests: int
    maximum_bytes: int
    missing_data_policy_id: str
    calendar_identity: str
    calendar_version: str
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        start_date: str,
        end_date: str,
        chunk_size: int,
        maximum_instruments: int,
        maximum_requests: int,
        maximum_bytes: int,
        observation_type: HistoricalObservationType | str = HistoricalObservationType.DAILY_BAR,
        bar_interval: SecuritiesBarInterval | str = SecuritiesBarInterval.ONE_DAY,
        adjustment_state: SecuritiesAdjustmentState | str = SecuritiesAdjustmentState.RAW,
        adjustment_policy_id: str = ALPACA_RAW_NO_ADJUSTMENT_V1,
        missing_data_policy_id: str = MISSING_DATA_POLICY_V1,
        calendar_identity: str = "XNYS",
        calendar_version: str = "1",
    ) -> HistoricalCapturePolicy:
        if not provider:
            raise HistoricalMarketDataError("CAPTURE_POLICY_PROVIDER_REQUIRED")
        parsed_observation_type = HistoricalObservationType(observation_type)
        parsed_interval = SecuritiesBarInterval(bar_interval)
        parsed_adjustment = SecuritiesAdjustmentState(adjustment_state)
        if parsed_observation_type is not HistoricalObservationType.DAILY_BAR:
            raise HistoricalMarketDataError("CAPTURE_POLICY_ONLY_DAILY_BAR_SUPPORTED_V1")
        if parsed_interval is not SecuritiesBarInterval.ONE_DAY:
            raise HistoricalMarketDataError("CAPTURE_POLICY_ONLY_ONE_DAY_INTERVAL_SUPPORTED_V1")
        if parsed_adjustment is not SecuritiesAdjustmentState.RAW:
            # HistoricalAlpacaCanonicalizer/AlpacaSecuritiesEvidenceNormalizer
            # always produce RAW bars (Alpaca's raw-only policy is unchanged
            # by this milestone) -- a policy claiming a different adjustment
            # state than the pipeline can actually produce would silently
            # mislabel every captured bar. Reject it here instead.
            raise HistoricalMarketDataError("CAPTURE_POLICY_ONLY_RAW_ADJUSTMENT_SUPPORTED_V1")
        start = _iso_date(start_date, "capture policy start_date")
        end = _iso_date(end_date, "capture policy end_date")
        if start > end:
            raise HistoricalMarketDataError("CAPTURE_POLICY_DATE_RANGE_INVALID")
        if not (1 <= chunk_size <= 10_000):
            raise HistoricalMarketDataError("CAPTURE_POLICY_CHUNK_SIZE_OUT_OF_BOUNDS")
        if not (1 <= maximum_instruments <= 10_000):
            raise HistoricalMarketDataError("CAPTURE_POLICY_MAXIMUM_INSTRUMENTS_OUT_OF_BOUNDS")
        if not (1 <= maximum_requests <= 1_000_000):
            raise HistoricalMarketDataError("CAPTURE_POLICY_MAXIMUM_REQUESTS_OUT_OF_BOUNDS")
        if not (1024 <= maximum_bytes <= 50_000_000_000):
            raise HistoricalMarketDataError("CAPTURE_POLICY_MAXIMUM_BYTES_OUT_OF_BOUNDS")
        if not missing_data_policy_id or not calendar_identity or not calendar_version:
            raise HistoricalMarketDataError("CAPTURE_POLICY_IDENTITY_REQUIRED")
        payload = (
            HISTORICAL_MARKET_DATA_V1,
            provider,
            parsed_observation_type,
            parsed_interval,
            parsed_adjustment,
            adjustment_policy_id,
            start.isoformat(),
            end.isoformat(),
            chunk_size,
            maximum_instruments,
            maximum_requests,
            maximum_bytes,
            missing_data_policy_id,
            calendar_identity,
            calendar_version,
        )
        return cls(
            policy_id=deterministic_id("historical_capture_policy", payload),
            provider=provider,
            observation_type=parsed_observation_type,
            bar_interval=parsed_interval,
            adjustment_state=parsed_adjustment,
            adjustment_policy_id=adjustment_policy_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            chunk_size=chunk_size,
            maximum_instruments=maximum_instruments,
            maximum_requests=maximum_requests,
            maximum_bytes=maximum_bytes,
            missing_data_policy_id=missing_data_policy_id,
            calendar_identity=calendar_identity,
            calendar_version=calendar_version,
            canonical_hash=content_digest(payload),
        )


def _chunked(
    values: Sequence[HistoricalCaptureUniverseMember], size: int
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(item.instrument_id for item in values[index : index + size])
        for index in range(0, len(values), size)
    )


@dataclass(frozen=True, slots=True)
class HistoricalCapturePlan:
    """One bounded, chunkable, resumable historical-capture plan.

    The universe is frozen at draft time; resuming a plan always replays the
    identical chunk plan (a deterministic function of the sorted universe and
    ``policy.chunk_size``), so a resumed run can never silently see a
    different partition than the one already checkpointed.
    """

    plan_id: str
    policy: HistoricalCapturePolicy
    universe: tuple[HistoricalCaptureUniverseMember, ...]
    chunk_plan: tuple[tuple[str, ...], ...]
    evidence_class: EvidenceClass
    state: HistoricalCapturePlanState
    operator_action_id: str
    idempotency_key: str
    created_at: datetime
    canonical_input_hash: str

    @classmethod
    def draft(
        cls,
        *,
        policy: HistoricalCapturePolicy,
        universe: Iterable[HistoricalCaptureUniverseMember],
        evidence_class: EvidenceClass | str,
        operator_action_id: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> HistoricalCapturePlan:
        _aware(created_at, "capture plan created_at")
        if not operator_action_id or not idempotency_key:
            raise HistoricalMarketDataError("CAPTURE_PLAN_IDENTITY_REQUIRED")
        deduped: dict[str, HistoricalCaptureUniverseMember] = {}
        for member in universe:
            if member.stable_security_id in deduped:
                raise HistoricalMarketDataError("CAPTURE_PLAN_UNIVERSE_DUPLICATE_STABLE_ID")
            deduped[member.stable_security_id] = member
        if not deduped:
            raise HistoricalMarketDataError("CAPTURE_PLAN_UNIVERSE_REQUIRED")
        if len(deduped) > policy.maximum_instruments:
            raise HistoricalMarketDataError("CAPTURE_PLAN_UNIVERSE_EXCEEDS_POLICY_BOUND")
        ordered = tuple(sorted(deduped.values(), key=lambda item: item.stable_security_id))
        chunk_plan = _chunked(ordered, policy.chunk_size)
        if len(chunk_plan) > policy.maximum_requests:
            raise HistoricalMarketDataError("CAPTURE_PLAN_CHUNK_COUNT_EXCEEDS_POLICY_BOUND")
        payload = (
            HISTORICAL_MARKET_DATA_V1,
            policy.policy_id,
            policy.canonical_hash,
            tuple(
                (item.stable_security_id, item.symbol_at_capture, item.asset_class)
                for item in ordered
            ),
            EvidenceClass(evidence_class),
            operator_action_id,
            idempotency_key,
        )
        return cls(
            plan_id=deterministic_id("historical_capture_plan", payload),
            policy=policy,
            universe=ordered,
            chunk_plan=chunk_plan,
            evidence_class=EvidenceClass(evidence_class),
            state=HistoricalCapturePlanState.DRAFT,
            operator_action_id=operator_action_id,
            idempotency_key=idempotency_key,
            created_at=created_at,
            canonical_input_hash=content_digest(payload),
        )


def derive_plan_state(
    chunk_states: Sequence[HistoricalCaptureChunkState],
) -> HistoricalCapturePlanState:
    """The plan's summary state is always a pure function of its chunks'
    states, recomputed on every read -- never a separately tracked workflow
    lock. That is what makes an interrupted plan honestly resumable: a
    ``CREDENTIALS_REQUIRED``/``FAILED``/``PARTIAL`` plan is not "terminal", it
    is just what the chunks say right now, and running the next pending chunk
    (once credentials are available, for example) legitimately moves it.
    """

    if not chunk_states:
        raise HistoricalMarketDataError("CAPTURE_PLAN_STATE_REQUIRES_CHUNKS")
    if all(state is HistoricalCaptureChunkState.PENDING for state in chunk_states):
        return HistoricalCapturePlanState.DRAFT
    if any(state is HistoricalCaptureChunkState.RUNNING for state in chunk_states):
        return HistoricalCapturePlanState.RUNNING
    completed = sum(1 for state in chunk_states if state is HistoricalCaptureChunkState.COMPLETED)
    if completed == len(chunk_states):
        return HistoricalCapturePlanState.COMPLETE
    if completed > 0:
        return HistoricalCapturePlanState.PARTIAL
    if any(state is HistoricalCaptureChunkState.CREDENTIALS_REQUIRED for state in chunk_states):
        return HistoricalCapturePlanState.CREDENTIALS_REQUIRED
    return HistoricalCapturePlanState.FAILED


@dataclass(frozen=True, slots=True)
class HistoricalCaptureChunk:
    """One bounded, checkpointed unit of a capture plan.

    ``previous_chunk_hash`` chains each chunk to the one before it (``None``
    for ordinal zero), so resuming a partially-run plan can cheaply verify it
    is continuing the exact retained sequence rather than a diverged one.
    """

    chunk_id: str
    plan_id: str
    ordinal: int
    instrument_ids: tuple[str, ...]
    symbols: tuple[str, ...]
    state: HistoricalCaptureChunkState
    operation_id: str | None
    raw_artifact_set_hash: str | None
    batch_id: str | None
    captured_instrument_count: int
    failed_symbols: tuple[str, ...]
    failure_code: str | None
    previous_chunk_hash: str | None
    retry_count: int
    started_at: datetime | None
    completed_at: datetime | None
    canonical_input_hash: str
    canonical_result_hash: str | None

    @property
    def attempt_idempotency_key(self) -> str:
        """A fresh key per attempt so a genuine retry (credentials became
        available, a transient failure cleared) issues a new provider fetch
        operation instead of replaying a stale ``CREDENTIALS_REQUIRED``/
        ``FAILED`` result back from that operation's own idempotency check."""

        return f"historical-chunk:{self.chunk_id}:attempt:{self.retry_count}"

    @classmethod
    def pending(
        cls,
        *,
        plan: HistoricalCapturePlan,
        ordinal: int,
        previous_chunk_hash: str | None,
    ) -> HistoricalCaptureChunk:
        if not (0 <= ordinal < len(plan.chunk_plan)):
            raise HistoricalMarketDataError("CAPTURE_CHUNK_ORDINAL_OUT_OF_RANGE")
        if (ordinal == 0) != (previous_chunk_hash is None):
            raise HistoricalMarketDataError("CAPTURE_CHUNK_CHAIN_INVALID")
        instrument_ids = plan.chunk_plan[ordinal]
        symbols_by_instrument = {
            item.instrument_id: item.symbol_at_capture for item in plan.universe
        }
        symbols = tuple(symbols_by_instrument[item] for item in instrument_ids)
        payload = (
            plan.plan_id,
            ordinal,
            instrument_ids,
            symbols,
            previous_chunk_hash,
        )
        return cls(
            chunk_id=deterministic_id("historical_capture_chunk", payload),
            plan_id=plan.plan_id,
            ordinal=ordinal,
            instrument_ids=instrument_ids,
            symbols=symbols,
            state=HistoricalCaptureChunkState.PENDING,
            operation_id=None,
            raw_artifact_set_hash=None,
            batch_id=None,
            captured_instrument_count=0,
            failed_symbols=(),
            failure_code=None,
            previous_chunk_hash=previous_chunk_hash,
            retry_count=0,
            started_at=None,
            completed_at=None,
            canonical_input_hash=content_digest(payload),
            canonical_result_hash=None,
        )

    def start(self, *, started_at: datetime) -> HistoricalCaptureChunk:
        retryable = {
            HistoricalCaptureChunkState.PENDING,
            HistoricalCaptureChunkState.FAILED,
            HistoricalCaptureChunkState.CREDENTIALS_REQUIRED,
        }
        if self.state not in retryable:
            raise HistoricalMarketDataError("CAPTURE_CHUNK_NOT_RETRYABLE")
        is_retry = self.state is not HistoricalCaptureChunkState.PENDING
        return replace(
            self,
            state=HistoricalCaptureChunkState.RUNNING,
            retry_count=self.retry_count + 1 if is_retry else self.retry_count,
            started_at=_aware(started_at, "chunk started_at"),
        )

    def complete(
        self,
        *,
        operation_id: str,
        raw_artifact_set_hash: str,
        batch_id: str,
        captured_instrument_count: int,
        failed_symbols: tuple[str, ...],
        completed_at: datetime,
    ) -> HistoricalCaptureChunk:
        if self.state is not HistoricalCaptureChunkState.RUNNING:
            raise HistoricalMarketDataError("CAPTURE_CHUNK_NOT_RUNNING")
        if captured_instrument_count < 0 or captured_instrument_count > len(self.instrument_ids):
            raise HistoricalMarketDataError("CAPTURE_CHUNK_CAPTURED_COUNT_INVALID")
        result_payload = (
            self.canonical_input_hash,
            operation_id,
            raw_artifact_set_hash,
            batch_id,
            captured_instrument_count,
            tuple(sorted(failed_symbols)),
        )
        return replace(
            self,
            state=HistoricalCaptureChunkState.COMPLETED,
            operation_id=operation_id,
            raw_artifact_set_hash=raw_artifact_set_hash,
            batch_id=batch_id,
            captured_instrument_count=captured_instrument_count,
            failed_symbols=tuple(sorted(failed_symbols)),
            completed_at=_aware(completed_at, "chunk completed_at"),
            canonical_result_hash=content_digest(result_payload),
        )

    def fail(
        self,
        *,
        state: HistoricalCaptureChunkState,
        failure_code: str,
        completed_at: datetime,
        operation_id: str | None = None,
    ) -> HistoricalCaptureChunk:
        """``operation_id`` is retained even on failure: a provider fetch
        operation is locally reserved (with a real identity) *before* the
        credentials check, so a ``CREDENTIALS_REQUIRED`` chunk still has
        genuine reservation lineage worth keeping -- "zero unauthorized
        requests" describes network I/O, not local reservation bookkeeping.
        """

        if state not in {
            HistoricalCaptureChunkState.CREDENTIALS_REQUIRED,
            HistoricalCaptureChunkState.FAILED,
        }:
            raise HistoricalMarketDataError("CAPTURE_CHUNK_FAILURE_STATE_INVALID")
        if self.state is not HistoricalCaptureChunkState.RUNNING:
            raise HistoricalMarketDataError("CAPTURE_CHUNK_NOT_RUNNING")
        result_payload = (self.canonical_input_hash, state, failure_code, operation_id)
        return replace(
            self,
            state=state,
            operation_id=operation_id,
            failure_code=failure_code,
            completed_at=_aware(completed_at, "chunk completed_at"),
            canonical_result_hash=content_digest(result_payload),
        )


@dataclass(frozen=True, slots=True)
class HistoricalDatasetSnapshot:
    """A deterministic, immutable historical-dataset identity.

    Two snapshots built from an identical stable-security universe, date
    range, provider, adjustment policy, bar interval, missing-data policy, and
    source captures always produce the identical ``snapshot_id``/
    ``canonical_hash``; changing any one of them changes both. Source batches
    are themselves content-addressed (``SecuritiesMarketDataBatch.batch_id``
    is a function of the canonical bar bytes), so a changed observation
    necessarily changes ``source_batch_ids`` and therefore this identity too.
    """

    snapshot_id: str
    plan_id: str
    evidence_class: EvidenceClass
    universe: tuple[str, ...]
    start_date: str
    end_date: str
    provider: str
    bar_interval: SecuritiesBarInterval
    adjustment_state: SecuritiesAdjustmentState
    adjustment_policy_id: str
    missing_data_policy_id: str
    source_batch_ids: tuple[str, ...]
    source_operation_ids: tuple[str, ...]
    observation_count: int
    completeness_state: HistoricalDatasetCompletenessState
    point_in_time_universe_state: PointInTimeUniverseState
    survivorship_state: SecuritiesSurvivorshipState
    earliest_observation_date: str | None
    latest_observation_date: str | None
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        plan: HistoricalCapturePlan,
        source_batch_ids: Iterable[str],
        source_operation_ids: Iterable[str],
        observation_count: int,
        completeness_state: HistoricalDatasetCompletenessState | str,
        earliest_observation_date: str | None,
        latest_observation_date: str | None,
        created_at: datetime,
    ) -> HistoricalDatasetSnapshot:
        _aware(created_at, "dataset snapshot created_at")
        if observation_count < 0:
            raise HistoricalMarketDataError("SNAPSHOT_OBSERVATION_COUNT_INVALID")
        batches = tuple(sorted(set(source_batch_ids)))
        operations = tuple(sorted(set(source_operation_ids)))
        if not operations:
            # At least one attempted capture (e.g. a CREDENTIALS_REQUIRED
            # provider operation) is required to materialize anything -- a
            # plan nothing has ever been attempted against has no evidence to
            # snapshot. ``batches`` may legitimately be empty: an honest
            # all-failed/all-credentials-required snapshot (zero observations)
            # is exactly the retained evidence a credential-absent acceptance
            # run must be able to produce.
            raise HistoricalMarketDataError("SNAPSHOT_REQUIRES_SOURCE_CAPTURES")
        if not batches and observation_count != 0:
            raise HistoricalMarketDataError("SNAPSHOT_OBSERVATION_COUNT_REQUIRES_BATCHES")
        parsed_completeness = HistoricalDatasetCompletenessState(completeness_state)
        earliest_parsed = (
            None
            if earliest_observation_date is None
            else _iso_date(earliest_observation_date, "snapshot earliest_observation_date")
        )
        latest_parsed = (
            None
            if latest_observation_date is None
            else _iso_date(latest_observation_date, "snapshot latest_observation_date")
        )
        if (earliest_parsed is None or latest_parsed is None) != (observation_count == 0):
            raise HistoricalMarketDataError("SNAPSHOT_DATE_BOUNDS_INCONSISTENT_WITH_COUNT")
        if (
            earliest_parsed is not None
            and latest_parsed is not None
            and earliest_parsed > latest_parsed
        ):
            raise HistoricalMarketDataError("SNAPSHOT_DATE_BOUNDS_INVERTED")
        universe = tuple(item.stable_security_id for item in plan.universe)
        payload = (
            HISTORICAL_MARKET_DATA_V1,
            plan.plan_id,
            EvidenceClass(plan.evidence_class),
            universe,
            plan.policy.start_date,
            plan.policy.end_date,
            plan.policy.provider,
            plan.policy.bar_interval,
            plan.policy.adjustment_state,
            plan.policy.adjustment_policy_id,
            plan.policy.missing_data_policy_id,
            batches,
            operations,
            observation_count,
            parsed_completeness,
            earliest_observation_date,
            latest_observation_date,
        )
        return cls(
            snapshot_id=deterministic_id("historical_dataset_snapshot", payload),
            plan_id=plan.plan_id,
            evidence_class=EvidenceClass(plan.evidence_class),
            universe=universe,
            start_date=plan.policy.start_date,
            end_date=plan.policy.end_date,
            provider=plan.policy.provider,
            bar_interval=plan.policy.bar_interval,
            adjustment_state=plan.policy.adjustment_state,
            adjustment_policy_id=plan.policy.adjustment_policy_id,
            missing_data_policy_id=plan.policy.missing_data_policy_id,
            source_batch_ids=batches,
            source_operation_ids=operations,
            observation_count=observation_count,
            completeness_state=parsed_completeness,
            # The universe is always today's stable-identity cohort applied
            # retroactively (SecuritiesUniverseSnapshot's own
            # PREDECLARED_FIXED_UNIVERSE/POINT_IN_TIME_MEMBERSHIP_VERIFIED
            # distinction has no verified-historical-membership option
            # implemented yet -- see module docstring and README/ARCHITECTURE).
            point_in_time_universe_state=PointInTimeUniverseState.POINT_IN_TIME_UNIVERSE_INCOMPLETE,
            survivorship_state=SecuritiesSurvivorshipState.SURVIVORSHIP_LIMITED,
            earliest_observation_date=earliest_observation_date,
            latest_observation_date=latest_observation_date,
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class HistoricalDataQualityReport:
    """Retained research evidence, not terminal log text.

    Wraps ``real_securities_data.evaluate_securities_data_quality`` (reused
    unmodified for OHLC/duplicate/missing-session diagnostics) with the
    security-level and capture-level facts that evaluator does not carry:
    requested-vs-captured security counts and per-security outcome, date
    coverage, raw capture hashes, and dataset-snapshot lineage.
    """

    report_id: str
    snapshot_id: str
    snapshot_hash: str
    requested_security_count: int
    captured_security_count: int
    security_outcomes: tuple[tuple[str, SecurityCaptureOutcome, str], ...]
    earliest_date: str | None
    latest_date: str | None
    observation_count: int
    duplicate_timestamp_count: int
    missing_session_count: int
    invalid_ohlc_count: int
    nonpositive_price_count: int
    volume_anomaly_count: int
    adjustment_state: SecuritiesAdjustmentState
    point_in_time_universe_state: PointInTimeUniverseState
    raw_capture_hashes: tuple[str, ...]
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        snapshot: HistoricalDatasetSnapshot,
        requested_security_count: int,
        security_outcomes: Sequence[tuple[str, SecurityCaptureOutcome | str, str]],
        duplicate_timestamp_count: int,
        missing_session_count: int,
        invalid_ohlc_count: int,
        nonpositive_price_count: int,
        volume_anomaly_count: int,
        raw_capture_hashes: Iterable[str],
        created_at: datetime,
    ) -> HistoricalDataQualityReport:
        _aware(created_at, "data quality report created_at")
        if requested_security_count < 0:
            raise HistoricalMarketDataError("QUALITY_REPORT_REQUESTED_COUNT_INVALID")
        normalized_outcomes = tuple(
            (stable_id, SecurityCaptureOutcome(outcome), reason)
            for stable_id, outcome, reason in security_outcomes
        )
        if len({item[0] for item in normalized_outcomes}) != len(normalized_outcomes):
            raise HistoricalMarketDataError("QUALITY_REPORT_DUPLICATE_SECURITY_OUTCOME")
        captured = sum(
            1
            for _stable_id, outcome, _reason in normalized_outcomes
            if outcome is SecurityCaptureOutcome.CAPTURED
        )
        for value in (
            duplicate_timestamp_count,
            missing_session_count,
            invalid_ohlc_count,
            nonpositive_price_count,
            volume_anomaly_count,
        ):
            if value < 0:
                raise HistoricalMarketDataError("QUALITY_REPORT_COUNT_NEGATIVE")
        hashes = tuple(sorted(set(raw_capture_hashes)))
        payload = (
            HISTORICAL_MARKET_DATA_V1,
            snapshot.snapshot_id,
            snapshot.canonical_hash,
            requested_security_count,
            tuple(sorted(normalized_outcomes)),
            snapshot.earliest_observation_date,
            snapshot.latest_observation_date,
            snapshot.observation_count,
            duplicate_timestamp_count,
            missing_session_count,
            invalid_ohlc_count,
            nonpositive_price_count,
            volume_anomaly_count,
            snapshot.adjustment_state,
            snapshot.point_in_time_universe_state,
            hashes,
        )
        return cls(
            report_id=deterministic_id("historical_data_quality_report", payload),
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.canonical_hash,
            requested_security_count=requested_security_count,
            captured_security_count=captured,
            security_outcomes=normalized_outcomes,
            earliest_date=snapshot.earliest_observation_date,
            latest_date=snapshot.latest_observation_date,
            observation_count=snapshot.observation_count,
            duplicate_timestamp_count=duplicate_timestamp_count,
            missing_session_count=missing_session_count,
            invalid_ohlc_count=invalid_ohlc_count,
            nonpositive_price_count=nonpositive_price_count,
            volume_anomaly_count=volume_anomaly_count,
            adjustment_state=snapshot.adjustment_state,
            point_in_time_universe_state=snapshot.point_in_time_universe_state,
            raw_capture_hashes=hashes,
            created_at=created_at,
            canonical_hash=content_digest(payload),
        )


# --------------------------------------------------------------------------
# Point-in-time / anti-lookahead guards
# --------------------------------------------------------------------------


def point_in_time_slice(
    bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime
) -> tuple[SecuritiesBarEvidence, ...]:
    """Construct a point-in-time-safe slice: keep only bars available at or
    before ``as_of``, ordered by event time. Never raises -- use this to build
    a slice from an unfiltered pool. Use ``assert_no_lookahead`` to validate a
    slice a caller claims is already point-in-time-safe."""

    _aware(as_of, "as_of")
    return tuple(
        sorted((bar for bar in bars if bar.available_at <= as_of), key=lambda b: b.period_end)
    )


def assert_no_lookahead(
    bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime
) -> tuple[SecuritiesBarEvidence, ...]:
    """Fail loudly if any supplied observation is not yet available at
    ``as_of``, then return the bars ordered by event time.

    This is the first-class research-safety boundary every calculation below
    is built on: a feature window can only ever see what point-in-time-safe
    evidence its caller actually assembled, never a future bar smuggled in.
    """

    _aware(as_of, "as_of")
    violations = tuple(bar for bar in bars if bar.available_at > as_of)
    if violations:
        raise PointInTimeError(
            f"{len(violations)} observation(s) are not available at or before as_of"
        )
    return tuple(sorted(bars, key=lambda b: b.period_end))


# --------------------------------------------------------------------------
# Reusable historical calculations -- Decimal, as_of-gated, deterministic
# --------------------------------------------------------------------------


def simple_return(bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime) -> Decimal | None:
    """Simple return from the first to the last point-in-time-safe close."""

    ordered = assert_no_lookahead(bars, as_of=as_of)
    if len(ordered) < 2:
        return None
    with localcontext() as context:
        context.prec = 28
        return _bounded_decimal((ordered[-1].close - ordered[0].close) / ordered[0].close)


def log_return(bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime) -> Decimal | None:
    """Natural-log return from the first to the last point-in-time-safe close."""

    ordered = assert_no_lookahead(bars, as_of=as_of)
    if len(ordered) < 2:
        return None
    with localcontext() as context:
        context.prec = 28
        return _bounded_decimal((ordered[-1].close / ordered[0].close).ln())


def _period_returns(ordered: Sequence[SecuritiesBarEvidence]) -> list[Decimal]:
    with localcontext() as context:
        context.prec = 28
        return [
            (later.close - earlier.close) / earlier.close for earlier, later in pairwise(ordered)
        ]


def _mean(values: Sequence[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return sum(values, Decimal(0)) / Decimal(len(values))


def _decimal_sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        if value < 0:
            raise HistoricalMarketDataError("HISTORICAL_SQRT_NEGATIVE_INPUT")
        return +value.sqrt()


def _stdev(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        raise HistoricalMarketDataError("HISTORICAL_STDEV_REQUIRES_TWO_VALUES")
    average = _mean(values)
    with localcontext() as context:
        context.prec = 28
        variance = sum(((value - average) ** 2 for value in values), Decimal(0)) / Decimal(
            len(values)
        )
    return _decimal_sqrt(variance)


def rolling_volatility(
    bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime, window: int
) -> Decimal | None:
    """Population standard deviation of simple returns over the trailing
    ``window`` point-in-time-safe sessions (returns require ``window + 1``
    closes)."""

    if window < 2:
        raise HistoricalMarketDataError("ROLLING_VOLATILITY_WINDOW_TOO_SMALL")
    ordered = assert_no_lookahead(bars, as_of=as_of)
    if len(ordered) < window + 1:
        return None
    tail_returns = _period_returns(ordered[-(window + 1) :])
    return _bounded_decimal(_stdev(tail_returns))


def rolling_liquidity(
    bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime, window: int
) -> Decimal | None:
    """Mean trailing dollar volume (``close * volume``) over ``window``
    point-in-time-safe sessions."""

    if window < 1:
        raise HistoricalMarketDataError("ROLLING_LIQUIDITY_WINDOW_TOO_SMALL")
    ordered = assert_no_lookahead(bars, as_of=as_of)
    if len(ordered) < window:
        return None
    tail = ordered[-window:]
    with localcontext() as context:
        context.prec = 28
        dollar_volumes = [bar.close * bar.volume for bar in tail]
    return _bounded_decimal(_mean(dollar_volumes))


def max_drawdown(bars: Sequence[SecuritiesBarEvidence], *, as_of: datetime) -> Decimal | None:
    """Maximum peak-to-trough drawdown over the point-in-time-safe series."""

    ordered = assert_no_lookahead(bars, as_of=as_of)
    if not ordered:
        return None
    peak = ordered[0].close
    worst = Decimal(0)
    with localcontext() as context:
        context.prec = 28
        for bar in ordered:
            peak = max(peak, bar.close)
            if peak > 0:
                worst = max(worst, (peak - bar.close) / peak)
    return _bounded_decimal(worst)


def cross_sectional_rank(values: Mapping[str, Decimal], *, instrument_id: str) -> Decimal:
    """Tie-aware fractional percentile rank of ``instrument_id`` within
    ``values`` (``(below + tied / 2) / n``), in ``[0, 1]``.

    Callers are responsible for point-in-time-filtering each instrument's own
    value (e.g. via ``simple_return``/a closing price already produced from an
    ``assert_no_lookahead``-checked slice) before it is placed in ``values``;
    this function itself has no time axis to guard.
    """

    if not values:
        raise HistoricalMarketDataError("CROSS_SECTIONAL_RANK_REQUIRES_VALUES")
    if instrument_id not in values:
        raise HistoricalMarketDataError("CROSS_SECTIONAL_RANK_INSTRUMENT_MISSING")
    target = values[instrument_id]
    below = sum(1 for value in values.values() if value < target)
    tied = sum(1 for value in values.values() if value == target)
    with localcontext() as context:
        context.prec = 28
        return _bounded_decimal(
            (Decimal(below) + Decimal(tied) / Decimal(2)) / Decimal(len(values))
        )


def rolling_correlation(
    bars_a: Sequence[SecuritiesBarEvidence],
    bars_b: Sequence[SecuritiesBarEvidence],
    *,
    as_of: datetime,
    window: int,
) -> Decimal | None:
    """Pearson correlation of trailing simple returns between two point-in-
    time-safe series, aligned by exact matching ``period_end``."""

    if window < 2:
        raise HistoricalMarketDataError("ROLLING_CORRELATION_WINDOW_TOO_SMALL")
    ordered_a = assert_no_lookahead(bars_a, as_of=as_of)
    ordered_b = assert_no_lookahead(bars_b, as_of=as_of)
    by_end_a = {bar.period_end: bar for bar in ordered_a}
    by_end_b = {bar.period_end: bar for bar in ordered_b}
    common = sorted(set(by_end_a) & set(by_end_b))
    if len(common) < window + 1:
        return None
    tail = common[-(window + 1) :]
    with localcontext() as context:
        context.prec = 28
        returns_a = [
            (by_end_a[later].close - by_end_a[earlier].close) / by_end_a[earlier].close
            for earlier, later in pairwise(tail)
        ]
        returns_b = [
            (by_end_b[later].close - by_end_b[earlier].close) / by_end_b[earlier].close
            for earlier, later in pairwise(tail)
        ]
        mean_a = _mean(returns_a)
        mean_b = _mean(returns_b)
        covariance = sum(
            ((a - mean_a) * (b - mean_b) for a, b in zip(returns_a, returns_b, strict=True)),
            Decimal(0),
        )
        variance_a = sum(((a - mean_a) ** 2 for a in returns_a), Decimal(0))
        variance_b = sum(((b - mean_b) ** 2 for b in returns_b), Decimal(0))
    denominator = _decimal_sqrt(variance_a) * _decimal_sqrt(variance_b)
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = 28
        value = covariance / denominator
    return _bounded_decimal(max(Decimal(-1), min(Decimal(1), value)))


# --------------------------------------------------------------------------
# Minimal cross-venue replay seam (section 13 / section 8)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoricalClock:
    """The single frozen ``as_of`` a replay step is allowed to see evidence
    through. Immutable and re-affirmed by every guard above."""

    as_of: datetime

    def __post_init__(self) -> None:
        _aware(self.as_of, "HistoricalClock.as_of")


@dataclass(frozen=True, slots=True)
class HistoricalObservationView:
    """Minimal cross-venue OHLCV shape.

    A future replay engine can iterate equity/ETF and crypto historical
    evidence uniformly through this one shape without special-casing either
    venue's own typed storage (``SecuritiesBarEvidence`` stays the equity/ETF
    bar of record; ``CryptoDailyCandle`` stays crypto's). Total Market Context
    is not itself an OHLCV series -- each ``TotalMarketContextSnapshot`` is
    already an immutable fact captured at one timestamp, so the same
    ``HistoricalClock``/``as_of`` convention applies to it directly without
    needing a view of this shape.
    """

    instrument_key: str
    venue: str
    event_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str


def observation_view_from_securities_bar(bar: SecuritiesBarEvidence) -> HistoricalObservationView:
    return HistoricalObservationView(
        instrument_key=bar.instrument_id,
        venue="EQUITY_ETF",
        event_at=bar.period_end,
        available_at=bar.available_at,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        source=bar.source_identity,
    )


def observation_view_from_crypto_daily_candle(
    *,
    product_id: str,
    bucket_start: datetime,
    open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
    provider: str,
) -> HistoricalObservationView:
    """Adapt a ``crypto_research.CryptoDailyCandle`` into the shared shape.

    Takes plain fields rather than the dataclass itself so this domain module
    never imports ``crypto_research`` (keeping the two evidence domains
    decoupled at the type level, per "implement only the minimum
    compatibility needed"); the adapter/application layer that already holds
    a genuine ``CryptoDailyCandle`` passes its fields through unchanged.

    ``CryptoDailyCandle`` retains no explicit availability timestamp of its
    own (unlike ``SecuritiesBarEvidence.available_at``, which is a first-class
    field). This conservatively treats a daily candle as available no earlier
    than the UTC end of its own bucket day -- a documented approximation, not
    a provider-observed receipt time.
    """

    event_at = _aware(bucket_start, "bucket_start")
    available_at = event_at.replace(hour=23, minute=59, second=59, microsecond=0)
    return HistoricalObservationView(
        instrument_key=product_id,
        venue="CRYPTO",
        event_at=event_at,
        available_at=available_at,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        source=provider,
    )


def canonical_timestamp_or_none(value: datetime | None) -> str | None:
    return None if value is None else canonical_timestamp(value)
