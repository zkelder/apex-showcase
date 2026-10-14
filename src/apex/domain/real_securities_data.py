"""Pure evidence contracts for bounded real Securities market-data research.

This module deliberately models provider observations, not broker state.  It
contains no Order, Position, reservation, account, ledger, PAPER, or LIVE
semantics.  Provider retrieval time is explicit and is never substituted for
the original availability time of a historical bar.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
    SecuritiesCorporateActionEvidence,
    SecuritiesInstrument,
)
from apex.domain.values import canonical_timestamp, content_digest, deterministic_id

REAL_DATA_WARNING = "REAL MARKET DATA DOES NOT IMPLY A REAL TRADING EDGE"
IEX_COVERAGE_WARNING = "IEX DATA IS SINGLE-VENUE COVERAGE, NOT THE FULL CONSOLIDATED MARKET"
ACTION_TIMING_WARNING = (
    "RETROSPECTIVELY FETCHED CORPORATE ACTIONS ARE NOT POINT-IN-TIME ANNOUNCEMENTS"
)
HISTORICAL_AUTHORITY_WARNING = "HISTORICAL RESULTS DO NOT GRANT PAPER OR LIVE AUTHORITY"


class RealSecuritiesDataError(ValueError):
    """Raised when an immutable provider-evidence invariant is violated."""


class SecuritiesProviderIdentity(StrEnum):
    ALPACA_MARKET_DATA_V2 = "ALPACA_MARKET_DATA_V2"


class SecuritiesProviderConfigurationState(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    DISABLED = "DISABLED"


class SecuritiesProviderCapabilityState(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    CREDENTIALS_REQUIRED = "CREDENTIALS_REQUIRED"
    AUTHENTICATION_REJECTED = "AUTHENTICATION_REJECTED"
    PERMISSION_LIMITED = "PERMISSION_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    BLOCKED_CONFIGURATION = "BLOCKED_CONFIGURATION"


class SecuritiesProviderFetchOperationType(StrEnum):
    ASSET_SNAPSHOT = "ASSET_SNAPSHOT"
    HISTORICAL_DAILY_BARS = "HISTORICAL_DAILY_BARS"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    LATEST_SNAPSHOTS = "LATEST_SNAPSHOTS"
    FORWARD_DAILY_CAPTURE = "FORWARD_DAILY_CAPTURE"
    OVERLAP_REVISION_CHECK = "OVERLAP_REVISION_CHECK"


class SecuritiesProviderFetchOperationState(StrEnum):
    REQUESTED = "REQUESTED"
    FETCHING = "FETCHING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"
    CREDENTIALS_REQUIRED = "CREDENTIALS_REQUIRED"


class SecuritiesHistoricalMembershipClaim(StrEnum):
    PREDECLARED_FIXED_UNIVERSE = "PREDECLARED_FIXED_UNIVERSE"
    PROVIDER_CURRENT_ASSET_SNAPSHOT = "PROVIDER_CURRENT_ASSET_SNAPSHOT"
    POINT_IN_TIME_MEMBERSHIP_VERIFIED = "POINT_IN_TIME_MEMBERSHIP_VERIFIED"


class SecuritiesSurvivorshipState(StrEnum):
    NOT_APPLICABLE_FIXED_SCOPE = "NOT_APPLICABLE_FIXED_SCOPE"
    CURRENT_SNAPSHOT_ONLY = "CURRENT_SNAPSHOT_ONLY"
    SURVIVORSHIP_LIMITED = "SURVIVORSHIP_LIMITED"
    POINT_IN_TIME_VERIFIED = "POINT_IN_TIME_VERIFIED"


class SecuritiesUniverseSnapshotState(StrEnum):
    READY = "READY"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"


class SecuritiesDataSuitability(StrEnum):
    ENGINEERING_VALIDATION_READY = "ENGINEERING_VALIDATION_READY"
    HISTORICAL_BASELINE_READY = "HISTORICAL_BASELINE_READY"
    HISTORICAL_MODEL_RESEARCH_READY = "HISTORICAL_MODEL_RESEARCH_READY"
    FORWARD_SHADOW_READY = "FORWARD_SHADOW_READY"
    POINT_IN_TIME_UNIVERSE_READY = "POINT_IN_TIME_UNIVERSE_READY"
    NOT_READY = "NOT_READY"


class SecuritiesDataIntegrityState(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class SecuritiesHistoricalClaimClassification(StrEnum):
    ENGINEERING_ONLY = "ENGINEERING_ONLY"
    SURVIVORSHIP_LIMITED_RESEARCH = "SURVIVORSHIP_LIMITED_RESEARCH"
    FIXED_UNIVERSE_RESEARCH = "FIXED_UNIVERSE_RESEARCH"
    POINT_IN_TIME_VALIDATED_RESEARCH = "POINT_IN_TIME_VALIDATED_RESEARCH"


class SecuritiesHistoricalValidationRecommendation(StrEnum):
    NOT_READY = "NOT_READY"
    BASELINES_ONLY = "BASELINES_ONLY"
    CONTINUE_HISTORICAL_RESEARCH = "CONTINUE_HISTORICAL_RESEARCH"
    ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW = "ELIGIBLE_FOR_FORWARD_SHADOW_REVIEW"
    REQUIRE_POINT_IN_TIME_UNIVERSE = "REQUIRE_POINT_IN_TIME_UNIVERSE"
    BLOCKED_DATA_QUALITY = "BLOCKED_DATA_QUALITY"
    BLOCKED_SIMULATOR_MISMATCH = "BLOCKED_SIMULATOR_MISMATCH"
    REQUIRE_MORE_EVIDENCE = "REQUIRE_MORE_EVIDENCE"


class SecuritiesForwardValidationPlanState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REVOKED = "REVOKED"
    COMPLETED = "COMPLETED"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


class SecuritiesForwardCaptureBatchState(StrEnum):
    CAPTURED = "CAPTURED"
    PARTIAL = "PARTIAL"
    NO_NEW_SESSION = "NO_NEW_SESSION"
    CREDENTIALS_REQUIRED = "CREDENTIALS_REQUIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"


DERIVED_EOD_AVAILABLE_V1 = "DERIVED_EOD_AVAILABLE_V1"
OBSERVED_PROVIDER_RECEIPT_V1 = "OBSERVED_PROVIDER_RECEIPT_V1"
RETROSPECTIVE_CORPORATE_ACTION_V1 = "RETROSPECTIVE_CORPORATE_ACTION_V1"
SECURITIES_DAILY_DIRECTION_DATASET_V1 = "SECURITIES_DAILY_DIRECTION_DATASET_V1"


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RealSecuritiesDataError(f"{label} must be timezone-aware")
    return value


def _hash(value: object) -> str:
    return content_digest(value)


@dataclass(frozen=True, slots=True)
class SecuritiesProviderConfiguration:
    configuration_id: str
    provider: SecuritiesProviderIdentity
    adapter_version: str
    environment: str
    feed: str
    asset_classes: tuple[str, ...]
    endpoint_policy_id: str
    endpoint_policy_hash: str
    request_timeout_seconds: int
    retry_policy: dict[str, object]
    rate_limit_policy: dict[str, object]
    maximum_pages_per_operation: int
    maximum_symbols_per_operation: int
    maximum_response_bytes: int
    raw_artifact_policy: dict[str, object]
    raw_artifact_policy_hash: str
    local_research_only: bool
    state: SecuritiesProviderConfigurationState
    created_at: datetime
    superseded_at: datetime | None
    operator_action_id: str
    canonical_payload_hash: str

    @classmethod
    def create(
        cls,
        *,
        adapter_version: str,
        environment: str,
        feed: str,
        endpoint_policy_id: str,
        endpoint_policy_hash: str,
        request_timeout_seconds: int,
        retry_policy: dict[str, object],
        rate_limit_policy: dict[str, object],
        maximum_pages_per_operation: int,
        maximum_symbols_per_operation: int,
        maximum_response_bytes: int,
        raw_artifact_policy: dict[str, object],
        operator_action_id: str,
        created_at: datetime,
    ) -> SecuritiesProviderConfiguration:
        _aware(created_at, "created_at")
        if feed != "IEX":
            raise RealSecuritiesDataError("initial real-data provider configuration requires IEX")
        if (
            not adapter_version
            or not environment
            or not endpoint_policy_id
            or not operator_action_id
        ):
            raise RealSecuritiesDataError("provider configuration identity is required")
        if not (1 <= request_timeout_seconds <= 120):
            raise RealSecuritiesDataError("provider timeout must be between 1 and 120 seconds")
        if not (1 <= maximum_pages_per_operation <= 100):
            raise RealSecuritiesDataError("provider maximum pages must be between 1 and 100")
        if not (1 <= maximum_symbols_per_operation <= 100):
            raise RealSecuritiesDataError("provider maximum symbols must be between 1 and 100")
        if not (1024 <= maximum_response_bytes <= 100_000_000):
            raise RealSecuritiesDataError("provider maximum response bytes is out of bounds")
        payload = {
            "provider": SecuritiesProviderIdentity.ALPACA_MARKET_DATA_V2,
            "adapter_version": adapter_version,
            "environment": environment,
            "feed": feed,
            "asset_classes": ("COMMON_STOCK", "ETF"),
            "endpoint_policy_id": endpoint_policy_id,
            "endpoint_policy_hash": endpoint_policy_hash,
            "request_timeout_seconds": request_timeout_seconds,
            "retry_policy": retry_policy,
            "rate_limit_policy": rate_limit_policy,
            "maximum_pages_per_operation": maximum_pages_per_operation,
            "maximum_symbols_per_operation": maximum_symbols_per_operation,
            "maximum_response_bytes": maximum_response_bytes,
            "raw_artifact_policy": raw_artifact_policy,
            "raw_artifact_policy_hash": _hash(raw_artifact_policy),
            "local_research_only": True,
            "created_at": canonical_timestamp(created_at),
            "operator_action_id": operator_action_id,
        }
        return cls(
            configuration_id=deterministic_id("securities_provider_configuration", payload),
            provider=SecuritiesProviderIdentity.ALPACA_MARKET_DATA_V2,
            adapter_version=adapter_version,
            environment=environment,
            feed=feed,
            asset_classes=("COMMON_STOCK", "ETF"),
            endpoint_policy_id=endpoint_policy_id,
            endpoint_policy_hash=endpoint_policy_hash,
            request_timeout_seconds=request_timeout_seconds,
            retry_policy=dict(retry_policy),
            rate_limit_policy=dict(rate_limit_policy),
            maximum_pages_per_operation=maximum_pages_per_operation,
            maximum_symbols_per_operation=maximum_symbols_per_operation,
            maximum_response_bytes=maximum_response_bytes,
            raw_artifact_policy=dict(raw_artifact_policy),
            raw_artifact_policy_hash=_hash(raw_artifact_policy),
            local_research_only=True,
            state=SecuritiesProviderConfigurationState.ACTIVE,
            created_at=created_at,
            superseded_at=None,
            operator_action_id=operator_action_id,
            canonical_payload_hash=_hash(payload),
        )


@dataclass(frozen=True, slots=True)
class SecuritiesUniverseSnapshot:
    universe_id: str
    display_name: str
    construction_policy_id: str
    construction_policy_payload: dict[str, object]
    construction_policy_hash: str
    symbols: tuple[str, ...]
    symbols_hash: str
    asset_identities: tuple[str, ...]
    asset_identities_hash: str
    frozen_at: datetime
    historical_membership_claim: SecuritiesHistoricalMembershipClaim
    survivorship_state: SecuritiesSurvivorshipState
    provider_asset_snapshot_id: str | None
    exclusions: tuple[str, ...]
    state: SecuritiesUniverseSnapshotState
    operator_action_id: str
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str

    @classmethod
    def fixed(
        cls,
        *,
        display_name: str,
        symbols: tuple[str, ...],
        reason: str,
        frozen_at: datetime,
        operator_action_id: str,
        idempotency_key: str,
    ) -> SecuritiesUniverseSnapshot:
        _aware(frozen_at, "frozen_at")
        normalized = tuple(sorted({item.strip().upper() for item in symbols if item.strip()}))
        if not (1 <= len(normalized) <= 100):
            raise RealSecuritiesDataError("fixed universe must contain one to one hundred symbols")
        if len(normalized) != len(symbols):
            raise RealSecuritiesDataError("fixed universe symbols must be unique nonempty tickers")
        if not display_name or not reason or not operator_action_id or not idempotency_key:
            raise RealSecuritiesDataError("fixed universe identity and reason are required")
        policy = {
            "identity": SecuritiesHistoricalMembershipClaim.PREDECLARED_FIXED_UNIVERSE,
            "version": "1",
            "reason": reason,
            "no_return_optimization": True,
        }
        input_payload = {
            "display_name": display_name,
            "symbols": normalized,
            "policy": policy,
            "frozen_at": canonical_timestamp(frozen_at),
            "operator_action_id": operator_action_id,
            "idempotency_key": idempotency_key,
        }
        result_payload = {
            **input_payload,
            "survivorship_state": SecuritiesSurvivorshipState.NOT_APPLICABLE_FIXED_SCOPE,
            "state": SecuritiesUniverseSnapshotState.READY,
        }
        return cls(
            universe_id=deterministic_id("securities_universe_snapshot", input_payload),
            display_name=display_name,
            construction_policy_id=SecuritiesHistoricalMembershipClaim.PREDECLARED_FIXED_UNIVERSE,
            construction_policy_payload=policy,
            construction_policy_hash=_hash(policy),
            symbols=normalized,
            symbols_hash=_hash(normalized),
            asset_identities=(),
            asset_identities_hash=_hash(()),
            frozen_at=frozen_at,
            historical_membership_claim=SecuritiesHistoricalMembershipClaim.PREDECLARED_FIXED_UNIVERSE,
            survivorship_state=SecuritiesSurvivorshipState.NOT_APPLICABLE_FIXED_SCOPE,
            provider_asset_snapshot_id=None,
            exclusions=(),
            state=SecuritiesUniverseSnapshotState.READY,
            operator_action_id=operator_action_id,
            idempotency_key=idempotency_key,
            canonical_input_hash=_hash(input_payload),
            canonical_result_hash=_hash(result_payload),
        )


@dataclass(frozen=True, slots=True)
class SecuritiesProviderCapabilitySnapshot:
    snapshot_id: str
    configuration_id: str
    configuration_hash: str
    checked_at: datetime
    credentials_available: bool
    authentication_state: str
    historical_bars_state: str
    snapshots_state: str
    corporate_actions_state: str
    asset_master_state: str
    feed: str
    earliest_requestable_date: str | None
    rate_limit_observations: dict[str, object]
    endpoint_reason_codes: dict[str, tuple[str, ...]]
    provider_runtime_version: str
    state: SecuritiesProviderCapabilityState
    canonical_result_hash: str

    @classmethod
    def create(
        cls,
        *,
        configuration: SecuritiesProviderConfiguration,
        checked_at: datetime,
        credentials_available: bool,
        state: SecuritiesProviderCapabilityState,
        authentication_state: str,
        historical_bars_state: str,
        snapshots_state: str,
        corporate_actions_state: str,
        asset_master_state: str,
        endpoint_reason_codes: dict[str, tuple[str, ...]],
        rate_limit_observations: dict[str, object] | None = None,
        earliest_requestable_date: str | None = None,
    ) -> SecuritiesProviderCapabilitySnapshot:
        _aware(checked_at, "checked_at")
        payload = {
            "configuration_id": configuration.configuration_id,
            "configuration_hash": configuration.canonical_payload_hash,
            "checked_at": canonical_timestamp(checked_at),
            "credentials_available": credentials_available,
            "authentication_state": authentication_state,
            "historical_bars_state": historical_bars_state,
            "snapshots_state": snapshots_state,
            "corporate_actions_state": corporate_actions_state,
            "asset_master_state": asset_master_state,
            "feed": configuration.feed,
            "earliest_requestable_date": earliest_requestable_date,
            "rate_limit_observations": rate_limit_observations or {},
            "endpoint_reason_codes": endpoint_reason_codes,
            "provider_runtime_version": configuration.adapter_version,
            "state": state,
        }
        return cls(
            snapshot_id=deterministic_id("securities_provider_capability", payload),
            configuration_id=configuration.configuration_id,
            configuration_hash=configuration.canonical_payload_hash,
            checked_at=checked_at,
            credentials_available=credentials_available,
            authentication_state=authentication_state,
            historical_bars_state=historical_bars_state,
            snapshots_state=snapshots_state,
            corporate_actions_state=corporate_actions_state,
            asset_master_state=asset_master_state,
            feed=configuration.feed,
            earliest_requestable_date=earliest_requestable_date,
            rate_limit_observations=dict(rate_limit_observations or {}),
            endpoint_reason_codes={
                key: tuple(value) for key, value in endpoint_reason_codes.items()
            },
            provider_runtime_version=configuration.adapter_version,
            state=state,
            canonical_result_hash=_hash(payload),
        )


@dataclass(frozen=True, slots=True)
class SecuritiesProviderFetchOperation:
    operation_id: str
    configuration_id: str
    configuration_hash: str
    operation_type: SecuritiesProviderFetchOperationType
    universe_id: str | None
    universe_hash: str | None
    symbols: tuple[str, ...]
    symbols_hash: str
    start_at: datetime | None
    end_at: datetime | None
    feed: str
    adjustment: str | None
    request_count: int
    page_count: int
    raw_byte_count: int
    received_record_count: int
    started_at: datetime
    completed_at: datetime | None
    state: SecuritiesProviderFetchOperationState
    retry_count: int
    rate_limit_evidence: dict[str, object]
    provider_request_ids: tuple[str, ...]
    raw_artifact_relative_path: str | None
    raw_artifact_set_hash: str | None
    normalized_batch_id: str | None
    normalized_batch_hash: str | None
    idempotency_key: str
    failure_code: str | None
    canonical_input_hash: str
    canonical_result_hash: str | None

    @classmethod
    def reserve(
        cls,
        *,
        configuration: SecuritiesProviderConfiguration,
        operation_type: SecuritiesProviderFetchOperationType,
        symbols: tuple[str, ...],
        universe: SecuritiesUniverseSnapshot | None,
        start_at: datetime | None,
        end_at: datetime | None,
        adjustment: str | None,
        idempotency_key: str,
        started_at: datetime,
    ) -> SecuritiesProviderFetchOperation:
        _aware(started_at, "started_at")
        if start_at is not None:
            _aware(start_at, "start_at")
        if end_at is not None:
            _aware(end_at, "end_at")
        if start_at is not None and end_at is not None and end_at < start_at:
            raise RealSecuritiesDataError("provider operation end precedes start")
        normalized = tuple(sorted({item.strip().upper() for item in symbols if item.strip()}))
        if operation_type is SecuritiesProviderFetchOperationType.ASSET_SNAPSHOT:
            if normalized:
                raise RealSecuritiesDataError("asset-master reads do not accept market symbols")
        elif not normalized or len(normalized) != len(symbols):
            raise RealSecuritiesDataError("provider operation symbols must be unique and nonempty")
        if len(normalized) > configuration.maximum_symbols_per_operation:
            raise RealSecuritiesDataError("provider operation exceeds configured symbol bound")
        if universe is not None and not set(normalized).issubset(set(universe.symbols)):
            raise RealSecuritiesDataError("provider operation symbols are outside frozen universe")
        payload = {
            "configuration_id": configuration.configuration_id,
            "configuration_hash": configuration.canonical_payload_hash,
            "operation_type": operation_type,
            "universe_id": universe.universe_id if universe else None,
            "universe_hash": universe.canonical_result_hash if universe else None,
            "symbols": normalized,
            "start_at": canonical_timestamp(start_at) if start_at else None,
            "end_at": canonical_timestamp(end_at) if end_at else None,
            "feed": configuration.feed,
            "adjustment": adjustment,
            "idempotency_key": idempotency_key,
        }
        return cls(
            operation_id=deterministic_id("securities_provider_fetch_operation", payload),
            configuration_id=configuration.configuration_id,
            configuration_hash=configuration.canonical_payload_hash,
            operation_type=operation_type,
            universe_id=universe.universe_id if universe else None,
            universe_hash=universe.canonical_result_hash if universe else None,
            symbols=normalized,
            symbols_hash=_hash(normalized),
            start_at=start_at,
            end_at=end_at,
            feed=configuration.feed,
            adjustment=adjustment,
            request_count=0,
            page_count=0,
            raw_byte_count=0,
            received_record_count=0,
            started_at=started_at,
            completed_at=None,
            state=SecuritiesProviderFetchOperationState.REQUESTED,
            retry_count=0,
            rate_limit_evidence={},
            provider_request_ids=(),
            raw_artifact_relative_path=None,
            raw_artifact_set_hash=None,
            normalized_batch_id=None,
            normalized_batch_hash=None,
            idempotency_key=idempotency_key,
            failure_code=None,
            canonical_input_hash=_hash(payload),
            canonical_result_hash=None,
        )


@dataclass(frozen=True, slots=True)
class SecuritiesDataQualityEvaluation:
    """Immutable quality classification for one retained canonical data batch.

    It describes evidence completeness; it does not fabricate prices, infer a
    historical universe, or grant research, PAPER, or LIVE authority.
    """

    evaluation_id: str
    market_data_batch_id: str
    market_data_batch_hash: str
    universe_id: str
    universe_hash: str
    calendar_identity: str
    calendar_version: str
    expected_session_count: int
    observed_bar_count: int
    missing_bar_session_count: int
    duplicate_count: int
    zero_or_negative_price_count: int
    invalid_ohlc_count: int
    zero_volume_count: int
    stale_series_count: int
    extreme_return_count: int
    action_consistency_count: int
    symbol_lifecycle_issue_count: int
    revision_count: int
    feed_coverage_limitations: tuple[str, ...]
    availability_policy_id: str
    availability_policy_hash: str
    integrity_state: SecuritiesDataIntegrityState
    suitability_states: tuple[SecuritiesDataSuitability, ...]
    reason_codes: tuple[str, ...]
    canonical_input_hash: str
    canonical_result_hash: str
    created_at: datetime


def evaluate_securities_data_quality(
    *,
    market_data_batch_id: str,
    market_data_batch_hash: str,
    universe: SecuritiesUniverseSnapshot,
    instruments: Sequence[SecuritiesInstrument],
    sessions: Sequence[SecuritiesCalendarSession],
    bars: Sequence[SecuritiesBarEvidence],
    corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    availability_policy_id: str,
    availability_policy: Mapping[str, object],
    revision_count: int,
    created_at: datetime,
) -> SecuritiesDataQualityEvaluation:
    """Evaluate retained evidence without substituting a missing observation.

    ``SecuritiesBarEvidence`` already rejects malformed price geometry, but
    this evaluator still reports any impossible values defensively so a damaged
    adapter/repository boundary blocks later research instead of silently
    making it suitable.
    """

    _aware(created_at, "created_at")
    if not market_data_batch_id or not market_data_batch_hash:
        raise RealSecuritiesDataError("canonical market-data batch identity is required")
    if not sessions:
        raise RealSecuritiesDataError("quality evaluation requires retained calendar sessions")
    calendar_identities = {(item.calendar_identity, item.calendar_version) for item in sessions}
    if len(calendar_identities) != 1:
        raise RealSecuritiesDataError("quality evaluation requires one exact retained calendar")
    calendar_identity, calendar_version = next(iter(calendar_identities))
    exact_instruments = {item.instrument_id: item for item in instruments}
    if len(exact_instruments) != len(instruments):
        raise RealSecuritiesDataError("quality evaluation instruments must be unique")
    if len(exact_instruments) != len(universe.symbols):
        raise RealSecuritiesDataError(
            "quality evaluation must retain complete frozen universe coverage"
        )
    symbols = {item.symbol for item in instruments}
    if symbols != set(universe.symbols):
        raise RealSecuritiesDataError("quality evaluation instruments do not match frozen universe")
    dates = {item.session_date for item in sessions}
    by_instrument_date: dict[tuple[str, str], list[SecuritiesBarEvidence]] = defaultdict(list)
    zero_or_negative = 0
    invalid_ohlc = 0
    zero_volume = 0
    lifecycle = 0
    for bar in bars:
        session_date = bar.period_end.date().isoformat()
        by_instrument_date[(bar.instrument_id, session_date)].append(bar)
        if any(value <= 0 for value in (bar.open, bar.high, bar.low, bar.close)):
            zero_or_negative += 1
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            invalid_ohlc += 1
        if bar.volume == 0:
            zero_volume += 1
        instrument = exact_instruments.get(bar.instrument_id)
        if instrument is None or (
            instrument.valid_to is not None and session_date > instrument.valid_to
        ):
            lifecycle += 1
    duplicates = sum(max(0, len(value) - 1) for value in by_instrument_date.values())
    missing = sum(
        1
        for instrument_id in exact_instruments
        for session_date in dates
        if (instrument_id, session_date) not in by_instrument_date
    )
    stale = 0
    extreme = 0
    for instrument_id in exact_instruments:
        ordered = sorted(
            (
                (session_date, values[0])
                for (candidate, session_date), values in by_instrument_date.items()
                if candidate == instrument_id and values
            ),
            key=lambda item: item[0],
        )
        consecutive_same = 1
        previous: Decimal | None = None
        for _session_date, bar in ordered:
            if previous is not None:
                if bar.close == previous:
                    consecutive_same += 1
                else:
                    consecutive_same = 1
                if previous != 0 and abs((bar.close - previous) / previous) > Decimal("0.50"):
                    extreme += 1
            if consecutive_same >= 3:
                stale += 1
            previous = bar.close
    action_consistency = sum(
        1
        for action in corporate_actions
        if action.instrument_id not in exact_instruments
        or action.effective_date < min(dates)
        or action.effective_date > max(dates)
    )
    reasons: list[str] = ["IEX_SINGLE_VENUE_COVERAGE"]
    if missing:
        reasons.append("MISSING_INSTRUMENT_SESSION_BARS")
    if duplicates:
        reasons.append("DUPLICATE_BAR_PERIODS")
    if zero_or_negative or invalid_ohlc:
        reasons.append("INVALID_PRICE_EVIDENCE")
    if lifecycle:
        reasons.append("SYMBOL_LIFECYCLE_CONTRADICTION")
    if revision_count:
        reasons.append("OVERLAP_REVISION_RETAINED")
    blocking = bool(missing or duplicates or zero_or_negative or invalid_ohlc or lifecycle)
    integrity = (
        SecuritiesDataIntegrityState.BLOCKED if blocking else SecuritiesDataIntegrityState.READY
    )
    suitability: list[SecuritiesDataSuitability]
    if blocking:
        suitability = [SecuritiesDataSuitability.NOT_READY]
    else:
        suitability = [
            SecuritiesDataSuitability.ENGINEERING_VALIDATION_READY,
            SecuritiesDataSuitability.HISTORICAL_BASELINE_READY,
            SecuritiesDataSuitability.HISTORICAL_MODEL_RESEARCH_READY,
        ]
        if (
            universe.historical_membership_claim
            is SecuritiesHistoricalMembershipClaim.POINT_IN_TIME_MEMBERSHIP_VERIFIED
        ):
            suitability.append(SecuritiesDataSuitability.POINT_IN_TIME_UNIVERSE_READY)
        else:
            reasons.append("POINT_IN_TIME_UNIVERSE_NOT_PROVEN")
    input_payload = {
        "market_data_batch_id": market_data_batch_id,
        "market_data_batch_hash": market_data_batch_hash,
        "universe_id": universe.universe_id,
        "universe_hash": universe.canonical_result_hash,
        "calendar_identity": calendar_identity,
        "calendar_version": calendar_version,
        "availability_policy_id": availability_policy_id,
        "availability_policy": dict(availability_policy),
        "revision_count": revision_count,
    }
    result_payload = {
        **input_payload,
        "counts": {
            "expected_session_count": len(sessions) * len(exact_instruments),
            "observed_bar_count": len(bars),
            "missing_bar_session_count": missing,
            "duplicate_count": duplicates,
            "zero_or_negative_price_count": zero_or_negative,
            "invalid_ohlc_count": invalid_ohlc,
            "zero_volume_count": zero_volume,
            "stale_series_count": stale,
            "extreme_return_count": extreme,
            "action_consistency_count": action_consistency,
            "symbol_lifecycle_issue_count": lifecycle,
            "revision_count": revision_count,
        },
        "integrity_state": integrity,
        "suitability_states": tuple(suitability),
        "reason_codes": tuple(sorted(set(reasons))),
    }
    return SecuritiesDataQualityEvaluation(
        evaluation_id=deterministic_id("securities_data_quality", input_payload),
        market_data_batch_id=market_data_batch_id,
        market_data_batch_hash=market_data_batch_hash,
        universe_id=universe.universe_id,
        universe_hash=universe.canonical_result_hash,
        calendar_identity=calendar_identity,
        calendar_version=calendar_version,
        expected_session_count=len(sessions) * len(exact_instruments),
        observed_bar_count=len(bars),
        missing_bar_session_count=missing,
        duplicate_count=duplicates,
        zero_or_negative_price_count=zero_or_negative,
        invalid_ohlc_count=invalid_ohlc,
        zero_volume_count=zero_volume,
        stale_series_count=stale,
        extreme_return_count=extreme,
        action_consistency_count=action_consistency,
        symbol_lifecycle_issue_count=lifecycle,
        revision_count=revision_count,
        feed_coverage_limitations=(IEX_COVERAGE_WARNING,),
        availability_policy_id=availability_policy_id,
        availability_policy_hash=_hash(dict(availability_policy)),
        integrity_state=integrity,
        suitability_states=tuple(suitability),
        reason_codes=tuple(sorted(set(reasons))),
        canonical_input_hash=_hash(input_payload),
        canonical_result_hash=_hash(result_payload),
        created_at=created_at,
    )


@dataclass(frozen=True, slots=True)
class SecuritiesHistoricalValidationRun:
    """Immutable binding for one real-evidence historical validation attempt.

    A run carries the exact quality decision into later baseline and model work.
    It is deliberately not a return claim, a deployment decision, or a capital
    authority.  The initial implementation fails closed until an explicit
    adjusted-price lineage is retained alongside the required raw provider
    evidence.
    """

    validation_run_id: str
    configuration_id: str
    configuration_hash: str
    universe_id: str
    universe_hash: str
    market_data_batch_id: str
    market_data_batch_hash: str
    quality_evaluation_id: str
    quality_evaluation_hash: str
    start_at: datetime
    end_at: datetime
    cutoff_at: datetime
    availability_policy_id: str
    availability_policy_hash: str
    price_adjustment_state: str
    baseline_suite_id: str | None
    baseline_suite_hash: str | None
    baseline_run_ids: tuple[str, ...]
    scorecard_ids: tuple[str, ...]
    simulator_conformance_ids: tuple[str, ...]
    dataset_id: str | None
    dataset_hash: str | None
    experiment_id: str | None
    experiment_hash: str | None
    candidate_search_run_id: str | None
    multiple_testing_evaluation_id: str | None
    state: str
    historical_claim_classification: SecuritiesHistoricalClaimClassification
    recommendation: SecuritiesHistoricalValidationRecommendation
    reason_codes: tuple[str, ...]
    started_at: datetime
    completed_at: datetime | None
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str | None

    @classmethod
    def request(
        cls,
        *,
        configuration: SecuritiesProviderConfiguration,
        universe: SecuritiesUniverseSnapshot,
        quality: SecuritiesDataQualityEvaluation,
        start_at: datetime,
        end_at: datetime,
        cutoff_at: datetime,
        price_adjustment_state: str,
        idempotency_key: str,
        started_at: datetime,
    ) -> SecuritiesHistoricalValidationRun:
        for value, label in (
            (start_at, "start_at"),
            (end_at, "end_at"),
            (cutoff_at, "cutoff_at"),
            (started_at, "started_at"),
        ):
            _aware(value, label)
        if end_at < start_at or cutoff_at < end_at:
            raise RealSecuritiesDataError("historical validation window is invalid")
        if not idempotency_key:
            raise RealSecuritiesDataError("historical validation idempotency key is required")
        classification = {
            SecuritiesHistoricalMembershipClaim.PREDECLARED_FIXED_UNIVERSE: (
                SecuritiesHistoricalClaimClassification.FIXED_UNIVERSE_RESEARCH
            ),
            SecuritiesHistoricalMembershipClaim.PROVIDER_CURRENT_ASSET_SNAPSHOT: (
                SecuritiesHistoricalClaimClassification.SURVIVORSHIP_LIMITED_RESEARCH
            ),
            SecuritiesHistoricalMembershipClaim.POINT_IN_TIME_MEMBERSHIP_VERIFIED: (
                SecuritiesHistoricalClaimClassification.POINT_IN_TIME_VALIDATED_RESEARCH
            ),
        }[universe.historical_membership_claim]
        payload = {
            "configuration_id": configuration.configuration_id,
            "configuration_hash": configuration.canonical_payload_hash,
            "universe_id": universe.universe_id,
            "universe_hash": universe.canonical_result_hash,
            "market_data_batch_id": quality.market_data_batch_id,
            "market_data_batch_hash": quality.market_data_batch_hash,
            "quality_evaluation_id": quality.evaluation_id,
            "quality_evaluation_hash": quality.canonical_result_hash,
            "start_at": canonical_timestamp(start_at),
            "end_at": canonical_timestamp(end_at),
            "cutoff_at": canonical_timestamp(cutoff_at),
            "availability_policy_id": quality.availability_policy_id,
            "availability_policy_hash": quality.availability_policy_hash,
            "price_adjustment_state": price_adjustment_state,
            "historical_claim_classification": classification,
            "idempotency_key": idempotency_key,
        }
        return cls(
            validation_run_id=deterministic_id("securities_historical_validation", payload),
            configuration_id=configuration.configuration_id,
            configuration_hash=configuration.canonical_payload_hash,
            universe_id=universe.universe_id,
            universe_hash=universe.canonical_result_hash,
            market_data_batch_id=quality.market_data_batch_id,
            market_data_batch_hash=quality.market_data_batch_hash,
            quality_evaluation_id=quality.evaluation_id,
            quality_evaluation_hash=quality.canonical_result_hash,
            start_at=start_at,
            end_at=end_at,
            cutoff_at=cutoff_at,
            availability_policy_id=quality.availability_policy_id,
            availability_policy_hash=quality.availability_policy_hash,
            price_adjustment_state=price_adjustment_state,
            baseline_suite_id=None,
            baseline_suite_hash=None,
            baseline_run_ids=(),
            scorecard_ids=(),
            simulator_conformance_ids=(),
            dataset_id=None,
            dataset_hash=None,
            experiment_id=None,
            experiment_hash=None,
            candidate_search_run_id=None,
            multiple_testing_evaluation_id=None,
            state="REQUESTED",
            historical_claim_classification=classification,
            recommendation=SecuritiesHistoricalValidationRecommendation.NOT_READY,
            reason_codes=(),
            started_at=started_at,
            completed_at=None,
            idempotency_key=idempotency_key,
            canonical_input_hash=_hash(payload),
            canonical_result_hash=None,
        )


@dataclass(frozen=True, slots=True)
class SecuritiesForwardValidationPlan:
    """A draft-only forward research binding pending explicit confirmation."""

    plan_id: str
    configuration_id: str
    configuration_hash: str
    universe_id: str
    universe_hash: str
    feature_schema_id: str
    feature_schema_hash: str
    champion_deployment_id: str | None
    challenger_deployment_id: str | None
    capture_interval_seconds: int
    capture_cutoff_policy: dict[str, object]
    prediction_horizon_sessions: int
    minimum_predictions: int
    minimum_resolved_outcomes: int
    maximum_duration_seconds: int
    corporate_action_polling_policy: dict[str, object]
    missing_session_policy: dict[str, object]
    state: SecuritiesForwardValidationPlanState
    current_watermark: str | None
    created_at: datetime
    activated_at: datetime | None
    paused_at: datetime | None
    revoked_at: datetime | None
    operator_action_id: str
    idempotency_key: str
    canonical_input_hash: str
    canonical_result_hash: str

    @classmethod
    def draft(
        cls,
        *,
        configuration: SecuritiesProviderConfiguration,
        universe: SecuritiesUniverseSnapshot,
        feature_schema_id: str,
        feature_schema_hash: str,
        prediction_horizon_sessions: int,
        operator_action_id: str,
        idempotency_key: str,
        created_at: datetime,
        champion_deployment_id: str | None = None,
        challenger_deployment_id: str | None = None,
    ) -> SecuritiesForwardValidationPlan:
        _aware(created_at, "created_at")
        if not feature_schema_id or len(feature_schema_hash) != 64:
            raise RealSecuritiesDataError("exact feature schema identity and hash are required")
        if not operator_action_id or not idempotency_key:
            raise RealSecuritiesDataError(
                "forward plan operator identity and idempotency key are required"
            )
        if not 1 <= prediction_horizon_sessions <= 30:
            raise RealSecuritiesDataError(
                "forward prediction horizon must be one to thirty sessions"
            )
        payload = {
            "configuration_id": configuration.configuration_id,
            "configuration_hash": configuration.canonical_payload_hash,
            "universe_id": universe.universe_id,
            "universe_hash": universe.canonical_result_hash,
            "feature_schema_id": feature_schema_id,
            "feature_schema_hash": feature_schema_hash,
            "champion_deployment_id": champion_deployment_id,
            "challenger_deployment_id": challenger_deployment_id,
            "capture_interval_seconds": 86400,
            "capture_cutoff_policy": {"identity": OBSERVED_PROVIDER_RECEIPT_V1},
            "prediction_horizon_sessions": prediction_horizon_sessions,
            "minimum_predictions": 100,
            "minimum_resolved_outcomes": 100,
            "maximum_duration_seconds": 31_536_000,
            "corporate_action_polling_policy": {"identity": RETROSPECTIVE_CORPORATE_ACTION_V1},
            "missing_session_policy": {"identity": "FAIL_CLOSED_NO_FORWARD_FILL_V1"},
            "operator_action_id": operator_action_id,
            "idempotency_key": idempotency_key,
        }
        result = {**payload, "state": SecuritiesForwardValidationPlanState.DRAFT}
        return cls(
            plan_id=deterministic_id("securities_forward_validation_plan", payload),
            configuration_id=configuration.configuration_id,
            configuration_hash=configuration.canonical_payload_hash,
            universe_id=universe.universe_id,
            universe_hash=universe.canonical_result_hash,
            feature_schema_id=feature_schema_id,
            feature_schema_hash=feature_schema_hash,
            champion_deployment_id=champion_deployment_id,
            challenger_deployment_id=challenger_deployment_id,
            capture_interval_seconds=86400,
            capture_cutoff_policy={"identity": OBSERVED_PROVIDER_RECEIPT_V1},
            prediction_horizon_sessions=prediction_horizon_sessions,
            minimum_predictions=100,
            minimum_resolved_outcomes=100,
            maximum_duration_seconds=31_536_000,
            corporate_action_polling_policy={"identity": RETROSPECTIVE_CORPORATE_ACTION_V1},
            missing_session_policy={"identity": "FAIL_CLOSED_NO_FORWARD_FILL_V1"},
            state=SecuritiesForwardValidationPlanState.DRAFT,
            current_watermark=None,
            created_at=created_at,
            activated_at=None,
            paused_at=None,
            revoked_at=None,
            operator_action_id=operator_action_id,
            idempotency_key=idempotency_key,
            canonical_input_hash=_hash(payload),
            canonical_result_hash=_hash(result),
        )


def derived_eod_available(*, session_close: datetime, delay_minutes: int = 30) -> datetime:
    """Return conservative derived availability for a historical daily bar."""

    _aware(session_close, "session_close")
    if not (1 <= delay_minutes <= 24 * 60):
        raise RealSecuritiesDataError("derived EOD delay must be between one minute and one day")
    return session_close + timedelta(minutes=delay_minutes)


def forward_observed_available(*, received_at: datetime) -> datetime:
    """Forward data cannot be made available before the retained response receipt."""

    return _aware(received_at, "received_at")


def retrospective_action_warning(*, first_observed_at: datetime | None) -> tuple[str, ...]:
    if first_observed_at is not None:
        _aware(first_observed_at, "first_observed_at")
    return (ACTION_TIMING_WARNING, RETROSPECTIVE_CORPORATE_ACTION_V1)
