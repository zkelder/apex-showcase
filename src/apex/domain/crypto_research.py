"""Pure values and calculations for bounded historical crypto validation.

This module owns retrospective research evidence only. It creates no Strategy,
Opportunity, Campaign, allocation, reservation, Order, Position, fill, journal,
broker request, PAPER authority, or LIVE authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.research_evidence import (
    CaptureStatus,
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    ReproducibilityStatus,
    ResearchHypothesisVersion,
)
from apex.domain.values import (
    canonical_decimal,
    content_digest,
    deterministic_id,
    parse_decimal,
)

COINBASE_PROVIDER = "COINBASE"
COINBASE_PRODUCT = "BTC-USD"
COINBASE_GRANULARITY = "ONE_DAY"
COINBASE_PUBLIC_CANDLES_CONTRACT = "COINBASE_ADVANCED_TRADE_PUBLIC_CANDLES_BOUNDARY_CURSOR_V1"
COINBASE_CANDLE_PARSER_VERSION = "APEX_COINBASE_DAILY_CANDLE_PARSER_V1"
CRYPTO_CAPTURE_POLICY_VERSION = "APEX_BOUNDED_CRYPTO_CAPTURE_V1"
CRYPTO_QUALITY_POLICY_VERSION = "APEX_CRYPTO_DAILY_QUALITY_V1"
CRYPTO_SNAPSHOT_VERSION = "APEX_CRYPTO_HISTORICAL_SNAPSHOT_V1"
BTC_USD_DAILY_TREND_VALIDATION_V1 = "BTC_USD_DAILY_TREND_VALIDATION_V1"
BTC_USD_PRIOR_DAY_SMA_FEATURES_V1 = "BTC_USD_PRIOR_DAY_SMA_FEATURES_V1"
CRYPTO_RESEARCH_COST_MODEL_V1 = "CRYPTO_RESEARCH_COST_MODEL_V1"
CRYPTO_VALIDATION_POLICY_V1 = "CRYPTO_VALIDATION_POLICY_V1"

FIXED_DATA_START = datetime(2024, 7, 1, tzinfo=UTC)
FIXED_DATA_END = datetime(2026, 7, 1, tzinfo=UTC)
FIXED_VALIDATION_START = datetime(2025, 7, 1, tzinfo=UTC)
FIXED_VALIDATION_END = datetime(2026, 7, 1, tzinfo=UTC)

MAX_BUCKETS_PER_REQUEST = 300
MAX_NETWORK_REQUESTS = 4
MAX_CAPTURE_DAYS = MAX_BUCKETS_PER_REQUEST * MAX_NETWORK_REQUESTS
MAX_RESPONSE_BYTES = 1_000_000
MAX_REQUEST_TIMEOUT_SECONDS = 20
MAX_TOTAL_RUNTIME_MILLISECONDS = 60_000

STARTING_RESEARCH_NOTIONAL = Decimal("10000")
FAST_SMA_DAYS = 20
SLOW_SMA_DAYS = 100
DIAGNOSTIC_FEE_BPS = Decimal("0")
DIAGNOSTIC_SLIPPAGE_BPS = Decimal("0")
CONSERVATIVE_FEE_BPS = Decimal("60")
CONSERVATIVE_SLIPPAGE_BPS = Decimal("10")
ANNUALIZATION_DAYS = Decimal("365")
RESEARCH_METRIC_QUANTUM = Decimal("0.00000001")

RETROSPECTIVE_WARNING = (
    "RETROSPECTIVE HISTORICAL PUBLIC DATA — NOT VINTAGE POINT-IN-TIME POSSESSION"
)
HYPOTHETICAL_WARNING = "HISTORICAL SIMULATION — NOT EARNED OR ACCOUNT PERFORMANCE"
AUTHORITY_WARNING = "RESEARCH EVIDENCE ONLY — NO PAPER, LIVE, OR CAPITAL AUTHORITY"
PRIOR_DAY_WARNING = "SIGNALS USE ONLY PRIOR COMPLETED DAILY CANDLES"

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class CryptoResearchError(ValueError):
    """A crypto research invariant was violated."""


class CryptoAttemptStatus(StrEnum):
    COMPLETED = "COMPLETED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    FAILED = "FAILED"


class CryptoCaptureQuality(StrEnum):
    VALID = "VALID"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class CostScenarioName(StrEnum):
    DIAGNOSTIC_ZERO_COST = "DIAGNOSTIC_ZERO_COST"
    CONSERVATIVE_RESEARCH = "CONSERVATIVE_RESEARCH"


class PortfolioName(StrEnum):
    TREND_RULE = "TREND_RULE"
    CASH = "CASH"
    BUY_AND_HOLD = "BUY_AND_HOLD"


def _identity(value: str, code: str) -> str:
    normalized = value.strip()
    if _IDENTITY.fullmatch(normalized) is None:
        raise CryptoResearchError(code)
    return normalized


def _hash(value: str, code: str) -> str:
    if _HASH.fullmatch(value) is None:
        raise CryptoResearchError(code)
    return value


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CryptoResearchError(code)
    return value.astimezone(UTC)


def _daily(value: datetime, code: str) -> datetime:
    normalized = _aware(value, code)
    if normalized.hour or normalized.minute or normalized.second or normalized.microsecond:
        raise CryptoResearchError(code)
    return normalized


def expected_bucket_starts(start: datetime, end: datetime) -> tuple[datetime, ...]:
    first = _daily(start, "CRYPTO_INTERVAL_START_NOT_DAILY")
    last = _daily(end, "CRYPTO_INTERVAL_END_NOT_DAILY")
    if last <= first:
        raise CryptoResearchError("CRYPTO_INTERVAL_INVALID")
    days = (last - first).days
    if days > MAX_CAPTURE_DAYS:
        raise CryptoResearchError("CRYPTO_INTERVAL_EXCEEDS_REQUEST_BOUNDS")
    return tuple(first + timedelta(days=index) for index in range(days))


@dataclass(frozen=True, slots=True)
class CryptoCapturePartition:
    ordinal: int
    start: datetime
    end: datetime
    expected_buckets: int
    canonical_hash: str

    @classmethod
    def create(cls, *, ordinal: int, start: datetime, end: datetime) -> CryptoCapturePartition:
        buckets = expected_bucket_starts(start, end)
        if ordinal < 0 or len(buckets) > MAX_BUCKETS_PER_REQUEST:
            raise CryptoResearchError("CRYPTO_PARTITION_BOUND_INVALID")
        payload = (
            CRYPTO_CAPTURE_POLICY_VERSION,
            ordinal,
            _daily(start, "CRYPTO_PARTITION_START_INVALID"),
            _daily(end, "CRYPTO_PARTITION_END_INVALID"),
            len(buckets),
        )
        return cls(ordinal, payload[2], payload[3], len(buckets), content_digest(payload))


def capture_partitions(start: datetime, end: datetime) -> tuple[CryptoCapturePartition, ...]:
    buckets = expected_bucket_starts(start, end)
    partitions: list[CryptoCapturePartition] = []
    for ordinal, offset in enumerate(range(0, len(buckets), MAX_BUCKETS_PER_REQUEST)):
        partition_start = buckets[offset]
        partition_end = min(
            partition_start + timedelta(days=MAX_BUCKETS_PER_REQUEST),
            _daily(end, "CRYPTO_INTERVAL_END_NOT_DAILY"),
        )
        partitions.append(
            CryptoCapturePartition.create(ordinal=ordinal, start=partition_start, end=partition_end)
        )
    if not 1 <= len(partitions) <= MAX_NETWORK_REQUESTS:
        raise CryptoResearchError("CRYPTO_PARTITION_COUNT_INVALID")
    return tuple(partitions)


@dataclass(frozen=True, slots=True)
class CryptoHypothesisSpecification:
    hypothesis_version_id: str
    product_id: str
    granularity: str
    data_start: datetime
    data_end: datetime
    validation_start: datetime
    validation_end: datetime
    fast_sma_days: int
    slow_sma_days: int
    baseline: str
    conservative_fee_bps: Decimal
    conservative_slippage_bps: Decimal
    minimum_net_return: Decimal
    requires_lower_drawdown: bool
    requires_completed_round_trip: bool
    paper_eligible: bool
    live_eligible: bool
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def fixed(cls, hypothesis: ResearchHypothesisVersion) -> CryptoHypothesisSpecification:
        payload = (
            _identity(
                hypothesis.hypothesis_version_id,
                "CRYPTO_HYPOTHESIS_VERSION_ID_INVALID",
            ),
            COINBASE_PRODUCT,
            COINBASE_GRANULARITY,
            FIXED_DATA_START,
            FIXED_DATA_END,
            FIXED_VALIDATION_START,
            FIXED_VALIDATION_END,
            FAST_SMA_DAYS,
            SLOW_SMA_DAYS,
            PortfolioName.BUY_AND_HOLD.value,
            CONSERVATIVE_FEE_BPS,
            CONSERVATIVE_SLIPPAGE_BPS,
            Decimal("0"),
            True,
            True,
            False,
            False,
        )
        return cls(*payload, content_digest(payload))


@dataclass(frozen=True, slots=True)
class CryptoHistoricalCapture:
    capture_id: str
    product_id: str
    start: datetime
    end: datetime
    granularity: str
    evidence_class: EvidenceClass
    provider: str
    source_contract_version: str
    parser_version: str
    quality_policy_version: str
    status: CaptureStatus
    requested_at: datetime
    captured_at: datetime | None
    completed_at: datetime | None
    expected_partitions: int
    completed_partitions: int
    request_count: int
    expected_buckets: int
    accepted_buckets: int
    missing_bucket_starts: tuple[datetime, ...]
    rejected_records: int
    duplicate_records: int
    total_raw_bytes: int
    total_runtime_milliseconds: int
    quality: CryptoCaptureQuality
    limitations: tuple[str, ...]
    failure_reason: str | None
    request_hash: str
    state_hash: str
    authority: str = "NONE"

    @classmethod
    def initial(
        cls,
        *,
        capture_id: str,
        start: datetime,
        end: datetime,
        evidence_class: EvidenceClass,
        requested_at: datetime,
    ) -> CryptoHistoricalCapture:
        if evidence_class not in {EvidenceClass.FIXTURE, EvidenceClass.HISTORICAL_PUBLIC}:
            raise CryptoResearchError("CRYPTO_CAPTURE_EVIDENCE_CLASS_INVALID")
        partitions = capture_partitions(start, end)
        buckets = expected_bucket_starts(start, end)
        identity = _identity(capture_id, "CRYPTO_CAPTURE_ID_INVALID")
        requested = _aware(requested_at, "CRYPTO_CAPTURE_REQUESTED_AT_INVALID")
        request_payload = (
            CRYPTO_CAPTURE_POLICY_VERSION,
            identity,
            COINBASE_PROVIDER,
            COINBASE_PUBLIC_CANDLES_CONTRACT,
            COINBASE_CANDLE_PARSER_VERSION,
            CRYPTO_QUALITY_POLICY_VERSION,
            COINBASE_PRODUCT,
            _daily(start, "CRYPTO_CAPTURE_START_INVALID"),
            _daily(end, "CRYPTO_CAPTURE_END_INVALID"),
            COINBASE_GRANULARITY,
            evidence_class,
            requested,
            MAX_BUCKETS_PER_REQUEST,
            MAX_NETWORK_REQUESTS,
        )
        request_hash = content_digest(request_payload)
        state_payload = (
            request_hash,
            CaptureStatus.RUNNING,
            0,
            0,
            len(partitions),
            len(buckets),
            (),
            0,
            0,
            0,
            0,
            CryptoCaptureQuality.INCOMPLETE,
        )
        return cls(
            identity,
            COINBASE_PRODUCT,
            request_payload[7],
            request_payload[8],
            COINBASE_GRANULARITY,
            evidence_class,
            COINBASE_PROVIDER,
            COINBASE_PUBLIC_CANDLES_CONTRACT,
            COINBASE_CANDLE_PARSER_VERSION,
            CRYPTO_QUALITY_POLICY_VERSION,
            CaptureStatus.RUNNING,
            requested,
            None,
            None,
            len(partitions),
            0,
            0,
            len(buckets),
            0,
            (),
            0,
            0,
            0,
            0,
            CryptoCaptureQuality.INCOMPLETE,
            (
                "PUBLIC_MARKET_DATA_ONLY",
                "NO_AUTHENTICATION_OR_ACCOUNT_ACCESS",
                "PROVIDER_BOUNDARY_CURSOR_USES_ONE_SECOND_PREDECESSORS",
                RETROSPECTIVE_WARNING,
            )
            if evidence_class is EvidenceClass.HISTORICAL_PUBLIC
            else ("PROVIDER_SHAPED_FIXTURE_EVIDENCE",),
            None,
            request_hash,
            content_digest(state_payload),
        )


@dataclass(frozen=True, slots=True)
class CryptoDailyCandle:
    candle_id: str
    capture_id: str
    attempt_id: str
    product_id: str
    bucket_start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    provider: str
    evidence_class: EvidenceClass
    ingested_at: datetime
    raw_response_hash: str
    normalized_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        attempt_id: str,
        evidence_class: EvidenceClass,
        bucket_start: datetime,
        open: object,
        high: object,
        low: object,
        close: object,
        volume: object,
        ingested_at: datetime,
        raw_response_hash: str,
    ) -> CryptoDailyCandle:
        identity = _identity(capture_id, "CRYPTO_CANDLE_CAPTURE_ID_INVALID")
        attempt = _identity(attempt_id, "CRYPTO_CANDLE_ATTEMPT_ID_INVALID")
        bucket = _daily(bucket_start, "CRYPTO_CANDLE_BUCKET_NOT_DAILY")
        try:
            parsed_open = parse_decimal(open, allow_zero=False)  # type: ignore[arg-type]
            parsed_high = parse_decimal(high, allow_zero=False)  # type: ignore[arg-type]
            parsed_low = parse_decimal(low, allow_zero=False)  # type: ignore[arg-type]
            parsed_close = parse_decimal(close, allow_zero=False)  # type: ignore[arg-type]
            parsed_volume = parse_decimal(volume)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise CryptoResearchError("CRYPTO_CANDLE_DECIMAL_INVALID") from error
        if parsed_high < max(parsed_open, parsed_low, parsed_close):
            raise CryptoResearchError("CRYPTO_CANDLE_HIGH_INVALID")
        if parsed_low > min(parsed_open, parsed_high, parsed_close):
            raise CryptoResearchError("CRYPTO_CANDLE_LOW_INVALID")
        raw_hash = _hash(raw_response_hash, "CRYPTO_CANDLE_RAW_HASH_INVALID")
        payload = (
            identity,
            attempt,
            COINBASE_PRODUCT,
            bucket,
            parsed_open,
            parsed_high,
            parsed_low,
            parsed_close,
            parsed_volume,
            COINBASE_PROVIDER,
            evidence_class,
            _aware(ingested_at, "CRYPTO_CANDLE_INGESTION_INVALID"),
            raw_hash,
        )
        digest = content_digest(payload)
        return cls(
            deterministic_id("crypto_daily_candle", payload),
            identity,
            attempt,
            COINBASE_PRODUCT,
            bucket,
            parsed_open,
            parsed_high,
            parsed_low,
            parsed_close,
            parsed_volume,
            COINBASE_PROVIDER,
            evidence_class,
            payload[11],
            raw_hash,
            digest,
        )


@dataclass(frozen=True, slots=True)
class CryptoRequestAttempt:
    attempt_id: str
    capture_id: str
    request_ordinal: int
    partition_ordinal: int
    partition_start: datetime
    partition_end: datetime
    requested_at: datetime
    completed_at: datetime
    status: CryptoAttemptStatus
    response_status: int | None
    raw_response_hash: str | None
    raw_bytes: int
    accepted_buckets: int
    missing_bucket_starts: tuple[datetime, ...]
    rejected_records: int
    duplicate_records: int
    runtime_milliseconds: int
    source_locator: str
    failure_reason: str | None
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        request_ordinal: int,
        partition: CryptoCapturePartition,
        requested_at: datetime,
        completed_at: datetime,
        status: CryptoAttemptStatus,
        response_status: int | None,
        raw_response_hash: str | None,
        raw_bytes: int,
        accepted_buckets: int,
        missing_bucket_starts: tuple[datetime, ...],
        rejected_records: int,
        duplicate_records: int,
        runtime_milliseconds: int,
        source_locator: str,
        failure_reason: str | None = None,
    ) -> CryptoRequestAttempt:
        if (
            request_ordinal < 0
            or raw_bytes < 0
            or accepted_buckets < 0
            or rejected_records < 0
            or duplicate_records < 0
            or runtime_milliseconds < 0
        ):
            raise CryptoResearchError("CRYPTO_ATTEMPT_COUNTS_INVALID")
        raw_hash = (
            _hash(raw_response_hash, "CRYPTO_ATTEMPT_RAW_HASH_INVALID")
            if raw_response_hash is not None
            else None
        )
        if status is CryptoAttemptStatus.COMPLETED and (
            response_status != 200 or raw_hash is None or raw_bytes == 0
        ):
            raise CryptoResearchError("CRYPTO_ATTEMPT_COMPLETION_INVALID")
        if status is not CryptoAttemptStatus.COMPLETED and not failure_reason:
            raise CryptoResearchError("CRYPTO_ATTEMPT_FAILURE_REASON_REQUIRED")
        payload = (
            _identity(capture_id, "CRYPTO_ATTEMPT_CAPTURE_ID_INVALID"),
            request_ordinal,
            partition.ordinal,
            partition.start,
            partition.end,
            _aware(requested_at, "CRYPTO_ATTEMPT_REQUESTED_AT_INVALID"),
            _aware(completed_at, "CRYPTO_ATTEMPT_COMPLETED_AT_INVALID"),
            status,
            response_status,
            raw_hash,
            raw_bytes,
            accepted_buckets,
            tuple(
                sorted(
                    _daily(item, "CRYPTO_MISSING_BUCKET_INVALID") for item in missing_bucket_starts
                )
            ),
            rejected_records,
            duplicate_records,
            runtime_milliseconds,
            source_locator,
            failure_reason,
        )
        digest = content_digest(payload)
        return cls(
            deterministic_id("crypto_capture_attempt", payload),
            payload[0],
            request_ordinal,
            partition.ordinal,
            partition.start,
            partition.end,
            payload[5],
            payload[6],
            status,
            response_status,
            raw_hash,
            raw_bytes,
            accepted_buckets,
            payload[12],
            rejected_records,
            duplicate_records,
            runtime_milliseconds,
            source_locator,
            failure_reason,
            digest,
        )


@dataclass(frozen=True, slots=True)
class CryptoCaptureCheckpoint:
    checkpoint_id: str
    capture_id: str
    completed_partitions: int
    next_partition_ordinal: int
    request_count: int
    accepted_buckets: int
    state_hash: str
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls, *, capture: CryptoHistoricalCapture, created_at: datetime
    ) -> CryptoCaptureCheckpoint:
        payload = (
            capture.capture_id,
            capture.completed_partitions,
            capture.completed_partitions,
            capture.request_count,
            capture.accepted_buckets,
            _hash(capture.state_hash, "CRYPTO_CHECKPOINT_STATE_HASH_INVALID"),
            _aware(created_at, "CRYPTO_CHECKPOINT_TIMESTAMP_INVALID"),
        )
        return cls(
            deterministic_id("crypto_capture_checkpoint", payload),
            capture.capture_id,
            capture.completed_partitions,
            capture.completed_partitions,
            capture.request_count,
            capture.accepted_buckets,
            capture.state_hash,
            payload[6],
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class CryptoHistoricalSnapshot:
    snapshot_id: str
    capture_id: str
    product_id: str
    start: datetime
    end: datetime
    earliest_bucket: datetime | None
    latest_bucket: datetime | None
    expected_buckets: int
    accepted_buckets: int
    missing_bucket_starts: tuple[datetime, ...]
    rejected_records: int
    duplicate_records: int
    evidence_class: EvidenceClass
    provider_contract_version: str
    parser_version: str
    quality_policy_version: str
    candle_hashes: tuple[str, ...]
    response_hashes: tuple[str, ...]
    quality: CryptoCaptureQuality
    manifest_hash: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        capture: CryptoHistoricalCapture,
        candles: tuple[CryptoDailyCandle, ...],
        attempts: tuple[CryptoRequestAttempt, ...],
        created_at: datetime,
    ) -> CryptoHistoricalSnapshot:
        if capture.status is not CaptureStatus.COMPLETED:
            raise CryptoResearchError("CRYPTO_SNAPSHOT_CAPTURE_NOT_COMPLETED")
        ordered = tuple(sorted(candles, key=lambda item: item.bucket_start))
        if len({item.bucket_start for item in ordered}) != len(ordered):
            raise CryptoResearchError("CRYPTO_SNAPSHOT_DUPLICATE_BUCKET")
        response_hashes = tuple(
            item.raw_response_hash
            for item in sorted(attempts, key=lambda item: item.partition_ordinal)
            if item.status is CryptoAttemptStatus.COMPLETED and item.raw_response_hash is not None
        )
        manifest_payload = (
            CRYPTO_SNAPSHOT_VERSION,
            capture.capture_id,
            capture.product_id,
            capture.start,
            capture.end,
            capture.expected_buckets,
            tuple(item.normalized_hash for item in ordered),
            capture.missing_bucket_starts,
            capture.rejected_records,
            capture.duplicate_records,
            response_hashes,
            capture.evidence_class,
            capture.source_contract_version,
            capture.parser_version,
            capture.quality_policy_version,
        )
        manifest = content_digest(manifest_payload)
        created = _aware(created_at, "CRYPTO_SNAPSHOT_CREATED_AT_INVALID")
        canonical_payload = (
            _identity(snapshot_id, "CRYPTO_SNAPSHOT_ID_INVALID"),
            manifest,
            created,
        )
        return cls(
            canonical_payload[0],
            capture.capture_id,
            capture.product_id,
            capture.start,
            capture.end,
            ordered[0].bucket_start if ordered else None,
            ordered[-1].bucket_start if ordered else None,
            capture.expected_buckets,
            len(ordered),
            capture.missing_bucket_starts,
            capture.rejected_records,
            capture.duplicate_records,
            capture.evidence_class,
            capture.source_contract_version,
            capture.parser_version,
            capture.quality_policy_version,
            tuple(item.normalized_hash for item in ordered),
            response_hashes,
            capture.quality,
            manifest,
            created,
            content_digest(canonical_payload),
        )


@dataclass(frozen=True, slots=True)
class CryptoCostAssumption:
    scenario: CostScenarioName
    fee_bps_per_side: Decimal
    slippage_bps_per_side: Decimal
    canonical_hash: str

    @classmethod
    def create(
        cls,
        scenario: CostScenarioName,
        fee_bps_per_side: object,
        slippage_bps_per_side: object,
    ) -> CryptoCostAssumption:
        fee = parse_decimal(fee_bps_per_side)  # type: ignore[arg-type]
        slippage = parse_decimal(slippage_bps_per_side)  # type: ignore[arg-type]
        if fee > Decimal("1000") or slippage > Decimal("1000"):
            raise CryptoResearchError("CRYPTO_COST_ASSUMPTION_OUT_OF_BOUNDS")
        payload = (CRYPTO_RESEARCH_COST_MODEL_V1, scenario, fee, slippage)
        return cls(scenario, fee, slippage, content_digest(payload))


@dataclass(frozen=True, slots=True)
class CryptoSignalDecision:
    signal_for: datetime
    information_through: datetime
    fast_average: Decimal
    slow_average: Decimal
    desired_long: bool
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CryptoPerformanceMetrics:
    portfolio: PortfolioName
    scenario: CostScenarioName
    validation_start: datetime
    validation_end: datetime
    starting_notional: Decimal
    ending_notional: Decimal
    gross_return: Decimal
    net_return: Decimal
    maximum_drawdown: Decimal
    realized_volatility: Decimal
    entries: int
    exits: int
    completed_round_trips: int
    total_turnover: Decimal
    exposure_percentage: Decimal
    cost_paid: Decimal
    missing_or_excluded_buckets: int
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CryptoExperimentRun:
    run_id: str
    hypothesis_version_id: str
    snapshot_id: str
    executor_version: str
    configuration: tuple[tuple[str, object], ...]
    configuration_hash: str
    feature_definition_version: str
    cost_model_version: str
    validation_policy_version: str
    code_revision: str
    deterministic_seed: int
    executed_at: datetime
    state: ExperimentState
    complete: bool
    reproducibility_status: ReproducibilityStatus
    evidence_class: EvidenceClass
    signals: tuple[CryptoSignalDecision, ...]
    metrics: tuple[CryptoPerformanceMetrics, ...]
    warnings: tuple[str, ...]
    failure_reason: str | None
    result_hash: str
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CryptoExperimentOutcome:
    outcome_id: str
    run_id: str
    decision: ExperimentOutcomeDecision
    decided_at: datetime
    actor: str
    reason: str
    evidence_hashes: tuple[str, ...]
    threshold_rules: tuple[str, ...]
    warnings: tuple[str, ...]
    paper_eligible: bool
    live_eligible: bool
    canonical_hash: str
    authority: str = "NONE"


def fixed_cost_assumptions() -> tuple[CryptoCostAssumption, CryptoCostAssumption]:
    return (
        CryptoCostAssumption.create(
            CostScenarioName.DIAGNOSTIC_ZERO_COST,
            DIAGNOSTIC_FEE_BPS,
            DIAGNOSTIC_SLIPPAGE_BPS,
        ),
        CryptoCostAssumption.create(
            CostScenarioName.CONSERVATIVE_RESEARCH,
            CONSERVATIVE_FEE_BPS,
            CONSERVATIVE_SLIPPAGE_BPS,
        ),
    )


def build_prior_day_signals(
    candles: tuple[CryptoDailyCandle, ...],
    *,
    validation_start: datetime,
    validation_end: datetime,
) -> tuple[CryptoSignalDecision, ...]:
    ordered = tuple(sorted(candles, key=lambda item: item.bucket_start))
    by_bucket = {item.bucket_start: item for item in ordered}
    signals: list[CryptoSignalDecision] = []
    for signal_for in expected_bucket_starts(validation_start, validation_end):
        if signal_for not in by_bucket:
            raise CryptoResearchError("CRYPTO_SIGNAL_EXECUTION_BUCKET_MISSING")
        prior = tuple(item for item in ordered if item.bucket_start < signal_for)
        if len(prior) < SLOW_SMA_DAYS:
            raise CryptoResearchError("CRYPTO_SIGNAL_WARMUP_INSUFFICIENT")
        information_through = signal_for - timedelta(days=1)
        if prior[-1].bucket_start != information_through:
            raise CryptoResearchError("CRYPTO_SIGNAL_PRIOR_BUCKET_MISSING")
        fast = sum((item.close for item in prior[-FAST_SMA_DAYS:]), Decimal(0)) / Decimal(
            FAST_SMA_DAYS
        )
        slow = sum((item.close for item in prior[-SLOW_SMA_DAYS:]), Decimal(0)) / Decimal(
            SLOW_SMA_DAYS
        )
        payload = (
            BTC_USD_PRIOR_DAY_SMA_FEATURES_V1,
            signal_for,
            information_through,
            fast,
            slow,
            fast > slow,
        )
        signals.append(
            CryptoSignalDecision(
                signal_for,
                information_through,
                fast,
                slow,
                fast > slow,
                content_digest(payload),
            )
        )
    return tuple(signals)


def _maximum_drawdown(values: tuple[Decimal, ...]) -> Decimal:
    peak = values[0]
    maximum = Decimal(0)
    for value in values:
        peak = max(peak, value)
        drawdown = (peak - value) / peak if peak else Decimal(0)
        maximum = max(maximum, drawdown)
    return maximum


def _realized_volatility(values: tuple[Decimal, ...]) -> Decimal:
    if len(values) < 3:
        return Decimal(0)
    returns = tuple(
        values[index] / values[index - 1] - Decimal(1)
        for index in range(1, len(values))
        if values[index - 1] != 0
    )
    if len(returns) < 2:
        return Decimal(0)
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum(((item - mean) ** 2 for item in returns), Decimal(0)) / Decimal(len(returns) - 1)
    return (variance * ANNUALIZATION_DAYS).sqrt()


def _research_metric(value: Decimal) -> Decimal:
    """Bound derived research metrics to the authoritative Decimal precision."""

    return value.quantize(RESEARCH_METRIC_QUANTUM)


def simulate_portfolio(
    *,
    portfolio: PortfolioName,
    candles: tuple[CryptoDailyCandle, ...],
    signals: tuple[CryptoSignalDecision, ...],
    cost: CryptoCostAssumption,
    diagnostic_gross_return: Decimal | None = None,
    validation_start: datetime = FIXED_VALIDATION_START,
    validation_end: datetime = FIXED_VALIDATION_END,
) -> CryptoPerformanceMetrics:
    validation_candles = tuple(
        item
        for item in sorted(candles, key=lambda item: item.bucket_start)
        if validation_start <= item.bucket_start < validation_end
    )
    expected_validation = expected_bucket_starts(validation_start, validation_end)
    if len(validation_candles) != len(expected_validation):
        raise CryptoResearchError("CRYPTO_SIMULATION_VALIDATION_COVERAGE_INCOMPLETE")
    signal_map = {item.signal_for: item for item in signals}
    if portfolio is PortfolioName.TREND_RULE and set(signal_map) != {
        item.bucket_start for item in validation_candles
    }:
        raise CryptoResearchError("CRYPTO_SIMULATION_SIGNAL_COVERAGE_INCOMPLETE")

    fee_rate = cost.fee_bps_per_side / Decimal("10000")
    slippage_rate = cost.slippage_bps_per_side / Decimal("10000")
    cash = STARTING_RESEARCH_NOTIONAL
    units = Decimal(0)
    entries = exits = exposure_days = 0
    turnover_notional = Decimal(0)
    cost_paid = Decimal(0)
    marked_values: list[Decimal] = [STARTING_RESEARCH_NOTIONAL]

    for candle in validation_candles:
        desired_long = (
            False
            if portfolio is PortfolioName.CASH
            else True
            if portfolio is PortfolioName.BUY_AND_HOLD
            else signal_map[candle.bucket_start].desired_long
        )
        if desired_long and units == 0:
            execution_price = candle.open * (Decimal(1) + slippage_rate)
            units = cash / (execution_price * (Decimal(1) + fee_rate))
            gross_notional = units * execution_price
            fee = gross_notional * fee_rate
            slippage_cost = units * candle.open * slippage_rate
            turnover_notional += units * candle.open
            cost_paid += fee + slippage_cost
            cash = Decimal(0)
            entries += 1
        elif not desired_long and units > 0:
            execution_price = candle.open * (Decimal(1) - slippage_rate)
            gross_proceeds = units * execution_price
            fee = gross_proceeds * fee_rate
            slippage_cost = units * candle.open * slippage_rate
            turnover_notional += units * candle.open
            cost_paid += fee + slippage_cost
            cash = gross_proceeds - fee
            units = Decimal(0)
            exits += 1
        if units > 0:
            exposure_days += 1
        marked_values.append(cash + units * candle.close)

    if units > 0:
        final_close = validation_candles[-1].close
        execution_price = final_close * (Decimal(1) - slippage_rate)
        gross_proceeds = units * execution_price
        fee = gross_proceeds * fee_rate
        slippage_cost = units * final_close * slippage_rate
        turnover_notional += units * final_close
        cost_paid += fee + slippage_cost
        cash = gross_proceeds - fee
        units = Decimal(0)
        exits += 1
        marked_values[-1] = cash

    ending = _research_metric(cash)
    net_return = _research_metric(ending / STARTING_RESEARCH_NOTIONAL - Decimal(1))
    gross_return = (
        net_return if diagnostic_gross_return is None else _research_metric(diagnostic_gross_return)
    )
    round_trips = min(entries, exits)
    payload_without_hash = (
        portfolio,
        cost.scenario,
        validation_start,
        validation_end,
        STARTING_RESEARCH_NOTIONAL,
        ending,
        gross_return,
        net_return,
        _research_metric(_maximum_drawdown(tuple(marked_values))),
        _research_metric(_realized_volatility(tuple(marked_values))),
        entries,
        exits,
        round_trips,
        _research_metric(turnover_notional / STARTING_RESEARCH_NOTIONAL),
        _research_metric(
            Decimal(exposure_days) / Decimal(len(validation_candles)) * Decimal("100")
        ),
        _research_metric(cost_paid),
        0,
    )
    return CryptoPerformanceMetrics(*payload_without_hash, content_digest(payload_without_hash))


def calculate_validation_metrics(
    candles: tuple[CryptoDailyCandle, ...],
    signals: tuple[CryptoSignalDecision, ...],
    *,
    validation_start: datetime = FIXED_VALIDATION_START,
    validation_end: datetime = FIXED_VALIDATION_END,
) -> tuple[CryptoPerformanceMetrics, ...]:
    diagnostic, conservative = fixed_cost_assumptions()
    metrics: list[CryptoPerformanceMetrics] = []
    for portfolio in (
        PortfolioName.TREND_RULE,
        PortfolioName.CASH,
        PortfolioName.BUY_AND_HOLD,
    ):
        zero = simulate_portfolio(
            portfolio=portfolio,
            candles=candles,
            signals=signals,
            cost=diagnostic,
            validation_start=validation_start,
            validation_end=validation_end,
        )
        costly = simulate_portfolio(
            portfolio=portfolio,
            candles=candles,
            signals=signals,
            cost=conservative,
            diagnostic_gross_return=zero.gross_return,
            validation_start=validation_start,
            validation_end=validation_end,
        )
        metrics.extend((zero, costly))
    return tuple(metrics)


def normalized_metric_summary(value: CryptoPerformanceMetrics) -> tuple[tuple[str, str], ...]:
    return (
        ("portfolio", value.portfolio.value),
        ("scenario", value.scenario.value),
        ("starting_notional", canonical_decimal(value.starting_notional)),
        ("ending_notional", canonical_decimal(value.ending_notional)),
        ("gross_return", canonical_decimal(value.gross_return)),
        ("net_return", canonical_decimal(value.net_return)),
        ("maximum_drawdown", canonical_decimal(value.maximum_drawdown)),
        ("realized_volatility", canonical_decimal(value.realized_volatility)),
        ("entries", str(value.entries)),
        ("exits", str(value.exits)),
        ("completed_round_trips", str(value.completed_round_trips)),
        ("total_turnover", canonical_decimal(value.total_turnover)),
        ("exposure_percentage", canonical_decimal(value.exposure_percentage)),
        ("cost_paid", canonical_decimal(value.cost_paid)),
    )
