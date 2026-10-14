"""Adapter-free orchestration for bounded real Securities provider evidence.

Every operation reserves its durable identity before provider I/O.  The
services below have no dependency on financial, broker, PAPER, ledger, or
campaign repositories; an operation can only retain research evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Protocol

from apex.domain.real_securities_data import (
    DERIVED_EOD_AVAILABLE_V1,
    HISTORICAL_AUTHORITY_WARNING,
    IEX_COVERAGE_WARNING,
    REAL_DATA_WARNING,
    SecuritiesDataQualityEvaluation,
    SecuritiesDataSuitability,
    SecuritiesForwardValidationPlan,
    SecuritiesHistoricalValidationRecommendation,
    SecuritiesHistoricalValidationRun,
    SecuritiesProviderCapabilitySnapshot,
    SecuritiesProviderCapabilityState,
    SecuritiesProviderConfiguration,
    SecuritiesProviderFetchOperation,
    SecuritiesProviderFetchOperationState,
    SecuritiesProviderFetchOperationType,
    SecuritiesUniverseSnapshot,
    evaluate_securities_data_quality,
)
from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
    SecuritiesCorporateActionEvidence,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
)
from apex.domain.values import content_digest


class RealSecuritiesDataConflict(RuntimeError):
    """An idempotent real-data operation was replayed with different input."""


class RealSecuritiesDataError(RuntimeError):
    """A real-data operation is blocked without creating financial state."""


class RealSecuritiesDataRepository(Protocol):
    def active_configuration(self) -> SecuritiesProviderConfiguration | None: ...

    def save_configuration(
        self, value: SecuritiesProviderConfiguration
    ) -> SecuritiesProviderConfiguration: ...

    def save_capability(
        self, value: SecuritiesProviderCapabilitySnapshot
    ) -> SecuritiesProviderCapabilitySnapshot: ...

    def latest_capability(
        self, configuration_id: str
    ) -> SecuritiesProviderCapabilitySnapshot | None: ...

    def universe(self, universe_id: str) -> SecuritiesUniverseSnapshot | None: ...

    def universe_by_idempotency(self, key: str) -> SecuritiesUniverseSnapshot | None: ...

    def list_universes(self) -> tuple[SecuritiesUniverseSnapshot, ...]: ...

    def save_universe(self, value: SecuritiesUniverseSnapshot) -> SecuritiesUniverseSnapshot: ...

    def operation_by_idempotency(self, key: str) -> SecuritiesProviderFetchOperation | None: ...

    def list_operations(self) -> tuple[SecuritiesProviderFetchOperation, ...]: ...

    def universe_for_symbols(
        self, symbols: tuple[str, ...]
    ) -> SecuritiesUniverseSnapshot | None: ...

    def quality(self, evaluation_id: str) -> SecuritiesDataQualityEvaluation | None: ...

    def list_quality(self) -> tuple[SecuritiesDataQualityEvaluation, ...]: ...

    def save_quality(
        self, value: SecuritiesDataQualityEvaluation
    ) -> SecuritiesDataQualityEvaluation: ...

    def validation_by_idempotency(self, key: str) -> SecuritiesHistoricalValidationRun | None: ...

    def list_validations(self) -> tuple[SecuritiesHistoricalValidationRun, ...]: ...

    def save_reserved_validation(
        self, value: SecuritiesHistoricalValidationRun
    ) -> SecuritiesHistoricalValidationRun: ...

    def transition_validation(
        self, value: SecuritiesHistoricalValidationRun
    ) -> SecuritiesHistoricalValidationRun: ...

    def plan_by_idempotency(self, key: str) -> SecuritiesForwardValidationPlan | None: ...

    def list_forward_plans(self) -> tuple[SecuritiesForwardValidationPlan, ...]: ...

    def save_forward_plan(
        self, value: SecuritiesForwardValidationPlan
    ) -> SecuritiesForwardValidationPlan: ...

    def save_reserved_operation(
        self, value: SecuritiesProviderFetchOperation
    ) -> SecuritiesProviderFetchOperation: ...

    def transition_operation(
        self, value: SecuritiesProviderFetchOperation
    ) -> SecuritiesProviderFetchOperation: ...


class SecuritiesEvidencePort(Protocol):
    def get_batch(self, batch_id: str) -> SecuritiesMarketDataBatch: ...

    def get_dataset(
        self, batch_id: str
    ) -> tuple[
        tuple[SecuritiesInstrument, ...],
        tuple[SecuritiesCalendarSession, ...],
        tuple[SecuritiesBarEvidence, ...],
        tuple[SecuritiesCorporateActionEvidence, ...],
    ]: ...


class SecuritiesProviderPage(Protocol):
    @property
    def endpoint_id(self) -> str: ...

    @property
    def query(self) -> Mapping[str, str]: ...

    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def raw_sha256(self) -> str: ...

    @property
    def received_at(self) -> datetime: ...

    @property
    def retry_delays_seconds(self) -> tuple[str, ...]: ...

    @property
    def provider_request_id(self) -> str | None: ...

    @property
    def body(self) -> bytes: ...

    @property
    def page_ordinal(self) -> int: ...

    @property
    def next_page_token_hash(self) -> str | None: ...


class SecuritiesRawArtifactSet(Protocol):
    @property
    def relative_path(self) -> str: ...

    @property
    def manifest_sha256(self) -> str: ...

    @property
    def byte_count(self) -> int: ...

    @property
    def page_count(self) -> int: ...


class SecuritiesProviderDataPort(Protocol):
    @property
    def credentials_available(self) -> bool: ...

    def fetch_pages(
        self,
        *,
        endpoint_id: str,
        query: Mapping[str, str],
        maximum_pages: int,
        maximum_response_bytes: int,
        timeout_seconds: int,
    ) -> tuple[SecuritiesProviderPage, ...]: ...


class SecuritiesRawArtifactPort(Protocol):
    def retain(
        self,
        *,
        operation_id: str,
        pages: Sequence[SecuritiesProviderPage],
        adapter_version: str,
    ) -> SecuritiesRawArtifactSet: ...


@dataclass(frozen=True, slots=True)
class CreateRealProviderConfigurationCommand:
    environment: str
    adapter_version: str
    endpoint_policy_id: str
    endpoint_policy_hash: str
    operator_action_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CreateSecuritiesUniverseSnapshotCommand:
    name: str
    symbols: tuple[str, ...]
    reason: str
    operator_action_id: str
    idempotency_key: str
    frozen_at: datetime


@dataclass(frozen=True, slots=True)
class FetchSecuritiesProviderOperationCommand:
    configuration_id: str
    operation_type: SecuritiesProviderFetchOperationType
    symbols: tuple[str, ...]
    universe_id: str | None
    start_at: datetime | None
    end_at: datetime | None
    idempotency_key: str
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class EvaluateSecuritiesDataQualityCommand:
    data_batch_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunSecuritiesHistoricalValidationCommand:
    data_batch_id: str
    universe_id: str
    start_at: datetime
    end_at: datetime
    idempotency_key: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class CreateSecuritiesForwardValidationPlanCommand:
    universe_id: str
    feature_schema_id: str
    feature_schema_hash: str
    prediction_horizon_sessions: int
    reason: str
    idempotency_key: str
    created_at: datetime


def initial_provider_configuration(
    *,
    environment: str,
    adapter_version: str,
    endpoint_policy_id: str,
    endpoint_policy_hash: str,
    operator_action_id: str,
    created_at: datetime,
) -> SecuritiesProviderConfiguration:
    return SecuritiesProviderConfiguration.create(
        adapter_version=adapter_version,
        environment=environment,
        feed="IEX",
        endpoint_policy_id=endpoint_policy_id,
        endpoint_policy_hash=endpoint_policy_hash,
        request_timeout_seconds=15,
        retry_policy={
            "identity": "ALPACA_BOUNDED_RETRY_V1",
            "maximum_attempts": 3,
            "retry_statuses": (429, 500, 502, 503, 504),
            "no_retry_authentication": True,
        },
        rate_limit_policy={
            "identity": "ALPACA_RETRY_AFTER_BOUNDED_V1",
            "maximum_delay_seconds": 30,
        },
        maximum_pages_per_operation=20,
        maximum_symbols_per_operation=24,
        maximum_response_bytes=5_000_000,
        raw_artifact_policy={
            "identity": "LOCAL_IMMUTABLE_ALPACA_RAW_V1",
            "root": ".local/securities/providers/alpaca",
            "local_research_only": True,
            "retain_authentication_headers": False,
        },
        operator_action_id=operator_action_id,
        created_at=created_at,
    )


class CreateRealProviderConfiguration:
    def __init__(self, repository: RealSecuritiesDataRepository) -> None:
        self._repository = repository

    def execute(
        self, command: CreateRealProviderConfigurationCommand
    ) -> SecuritiesProviderConfiguration:
        configuration = initial_provider_configuration(
            environment=command.environment,
            adapter_version=command.adapter_version,
            endpoint_policy_id=command.endpoint_policy_id,
            endpoint_policy_hash=command.endpoint_policy_hash,
            operator_action_id=command.operator_action_id,
            created_at=command.created_at,
        )
        current = self._repository.active_configuration()
        if current is not None:
            if current.canonical_payload_hash != configuration.canonical_payload_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_ACTIVE_CONFIGURATION_CONFLICT")
            return current
        return self._repository.save_configuration(configuration)


class CreateSecuritiesUniverseSnapshot:
    def __init__(self, repository: RealSecuritiesDataRepository) -> None:
        self._repository = repository

    def execute(
        self, command: CreateSecuritiesUniverseSnapshotCommand
    ) -> SecuritiesUniverseSnapshot:
        value = SecuritiesUniverseSnapshot.fixed(
            display_name=command.name,
            symbols=command.symbols,
            reason=command.reason,
            frozen_at=command.frozen_at,
            operator_action_id=command.operator_action_id,
            idempotency_key=command.idempotency_key,
        )
        existing = self._repository.universe_by_idempotency(command.idempotency_key)
        if existing is not None:
            if existing.canonical_input_hash != value.canonical_input_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_UNIVERSE_IDEMPOTENCY_CONFLICT")
            return existing
        return self._repository.save_universe(value)


class CheckSecuritiesProviderCapabilities:
    """Check only the reviewed market-data and asset-master reads.

    No account, trading, position, order, portfolio, or activity endpoint is
    attempted.  Missing credentials is a successful, persisted fail-closed
    capability result rather than an exception.
    """

    def __init__(
        self,
        *,
        repository: RealSecuritiesDataRepository,
        provider: SecuritiesProviderDataPort,
    ) -> None:
        self._repository = repository
        self._provider = provider

    def execute(
        self, *, configuration: SecuritiesProviderConfiguration, checked_at: datetime
    ) -> SecuritiesProviderCapabilitySnapshot:
        if not self._provider.credentials_available:
            result = SecuritiesProviderCapabilitySnapshot.create(
                configuration=configuration,
                checked_at=checked_at,
                credentials_available=False,
                state=SecuritiesProviderCapabilityState.CREDENTIALS_REQUIRED,
                authentication_state="CREDENTIALS_REQUIRED",
                historical_bars_state="NOT_ATTEMPTED",
                snapshots_state="NOT_ATTEMPTED",
                corporate_actions_state="NOT_ATTEMPTED",
                asset_master_state="NOT_ATTEMPTED",
                endpoint_reason_codes={"credentials": ("ALPACA_CREDENTIALS_REQUIRED",)},
            )
            return self._repository.save_capability(result)
        results: dict[str, str] = {}
        reasons: dict[str, tuple[str, ...]] = {}
        probes = (
            (
                "historical_bars",
                "historical_daily_bars",
                {
                    "symbols": "SPY",
                    "timeframe": "1Day",
                    "start": "2024-01-02",
                    "end": "2024-01-02",
                    "feed": "iex",
                    "adjustment": "raw",
                    "limit": "1",
                },
            ),
            ("snapshots", "latest_snapshots", {"symbols": "SPY", "feed": "iex"}),
            (
                "corporate_actions",
                "corporate_actions",
                {"symbols": "SPY", "start": "2024-01-02", "end": "2024-01-02", "limit": "1"},
            ),
            (
                "asset_master",
                "asset_master",
                {"status": "active", "asset_class": "us_equity", "exchange": "NYSE"},
            ),
        )
        for label, endpoint, query in probes:
            try:
                self._provider.fetch_pages(
                    endpoint_id=endpoint,
                    query=query,
                    maximum_pages=1,
                    maximum_response_bytes=configuration.maximum_response_bytes,
                    timeout_seconds=configuration.request_timeout_seconds,
                )
                results[label] = "READY"
                reasons[label] = ()
            except Exception as error:
                code = _provider_error_code(error)
                results[label] = _capability_endpoint_state(code)
                reasons[label] = (code,)
        endpoint_values = tuple(results.values())
        if all(value == "READY" for value in endpoint_values):
            state = SecuritiesProviderCapabilityState.READY
            authentication = "AUTHENTICATED"
        elif any(value == "READY" for value in endpoint_values):
            # A successful data-only request proves the credentials were
            # accepted.  A 401/403 from another reviewed endpoint is retained
            # as that endpoint's limitation, never promoted to a global
            # credential failure.
            results = {
                label: ("PERMISSION_LIMITED" if value == "AUTHENTICATION_REJECTED" else value)
                for label, value in results.items()
            }
            state = SecuritiesProviderCapabilityState.PARTIAL
            authentication = "AUTHENTICATED"
        elif any(value == "AUTHENTICATION_REJECTED" for value in endpoint_values):
            state = SecuritiesProviderCapabilityState.AUTHENTICATION_REJECTED
            authentication = "AUTHENTICATION_REJECTED"
        elif any(value == "PROVIDER_UNAVAILABLE" for value in endpoint_values):
            state = SecuritiesProviderCapabilityState.PROVIDER_UNAVAILABLE
            authentication = "UNKNOWN"
        elif any(value == "PERMISSION_LIMITED" for value in endpoint_values):
            state = SecuritiesProviderCapabilityState.PERMISSION_LIMITED
            authentication = "AUTHENTICATED"
        else:
            state = SecuritiesProviderCapabilityState.PARTIAL
            authentication = "AUTHENTICATED"
        result = SecuritiesProviderCapabilitySnapshot.create(
            configuration=configuration,
            checked_at=checked_at,
            credentials_available=True,
            state=state,
            authentication_state=authentication,
            historical_bars_state=results["historical_bars"],
            snapshots_state=results["snapshots"],
            corporate_actions_state=results["corporate_actions"],
            asset_master_state=results["asset_master"],
            endpoint_reason_codes=reasons,
        )
        return self._repository.save_capability(result)


class FetchSecuritiesProviderOperation:
    """Reserve then retain bounded raw provider pages; no financial mutation."""

    def __init__(
        self,
        *,
        repository: RealSecuritiesDataRepository,
        provider: SecuritiesProviderDataPort,
        artifacts: SecuritiesRawArtifactPort,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._artifacts = artifacts

    def execute(
        self, command: FetchSecuritiesProviderOperationCommand
    ) -> SecuritiesProviderFetchOperation:
        configuration = self._repository.active_configuration()
        if configuration is None or configuration.configuration_id != command.configuration_id:
            raise RealSecuritiesDataError("REAL_SECURITIES_PROVIDER_CONFIGURATION_NOT_ACTIVE")
        universe = (
            None if command.universe_id is None else self._repository.universe(command.universe_id)
        )
        if command.universe_id is not None and universe is None:
            raise RealSecuritiesDataError("REAL_SECURITIES_UNIVERSE_NOT_FOUND")
        adjustment = (
            "raw"
            if command.operation_type
            in {
                SecuritiesProviderFetchOperationType.HISTORICAL_DAILY_BARS,
                SecuritiesProviderFetchOperationType.OVERLAP_REVISION_CHECK,
                SecuritiesProviderFetchOperationType.FORWARD_DAILY_CAPTURE,
            }
            else None
        )
        reserved = SecuritiesProviderFetchOperation.reserve(
            configuration=configuration,
            operation_type=command.operation_type,
            symbols=command.symbols,
            universe=universe,
            start_at=command.start_at,
            end_at=command.end_at,
            adjustment=adjustment,
            idempotency_key=command.idempotency_key,
            started_at=command.requested_at,
        )
        existing = self._repository.operation_by_idempotency(command.idempotency_key)
        if existing is not None:
            if existing.canonical_input_hash != reserved.canonical_input_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_FETCH_IDEMPOTENCY_CONFLICT")
            return existing
        self._repository.save_reserved_operation(reserved)
        if not self._provider.credentials_available:
            return self._repository.transition_operation(
                _terminal_operation(
                    reserved,
                    state=SecuritiesProviderFetchOperationState.CREDENTIALS_REQUIRED,
                    failure_code="ALPACA_CREDENTIALS_REQUIRED",
                    completed_at=command.requested_at,
                )
            )
        fetching = replace(reserved, state=SecuritiesProviderFetchOperationState.FETCHING)
        self._repository.transition_operation(fetching)
        try:
            pages = self._provider.fetch_pages(
                endpoint_id=_endpoint_for_operation(command.operation_type),
                query=_query_for_operation(command),
                maximum_pages=configuration.maximum_pages_per_operation,
                maximum_response_bytes=configuration.maximum_response_bytes,
                timeout_seconds=configuration.request_timeout_seconds,
            )
            raw_set = self._artifacts.retain(
                operation_id=reserved.operation_id,
                pages=pages,
                adapter_version=configuration.adapter_version,
            )
            record_count = sum(_record_count(page) for page in pages)
            completed_at = max(page.received_at for page in pages)
            result_payload = {
                "operation": reserved.canonical_input_hash,
                "page_hashes": tuple(page.raw_sha256 for page in pages),
                "raw_artifact_set_hash": raw_set.manifest_sha256,
                "received_record_count": record_count,
            }
            completed = replace(
                fetching,
                request_count=len(pages),
                page_count=len(pages),
                raw_byte_count=raw_set.byte_count,
                received_record_count=record_count,
                completed_at=completed_at,
                state=SecuritiesProviderFetchOperationState.COMPLETED,
                retry_count=sum(len(page.retry_delays_seconds) for page in pages),
                rate_limit_evidence={
                    "retry_delays_seconds": tuple(
                        delay for page in pages for delay in page.retry_delays_seconds
                    )
                },
                provider_request_ids=tuple(
                    item.provider_request_id
                    for item in pages
                    if item.provider_request_id is not None
                ),
                raw_artifact_relative_path=raw_set.relative_path,
                raw_artifact_set_hash=raw_set.manifest_sha256,
                canonical_result_hash=content_digest(result_payload),
            )
            return self._repository.transition_operation(completed)
        except Exception as error:
            code = _provider_error_code(error)
            return self._repository.transition_operation(
                _terminal_operation(
                    fetching,
                    state=(
                        SecuritiesProviderFetchOperationState.CREDENTIALS_REQUIRED
                        if code == "ALPACA_CREDENTIALS_REQUIRED"
                        else SecuritiesProviderFetchOperationState.FAILED
                    ),
                    failure_code=code,
                    completed_at=datetime.now(tz=UTC),
                )
            )


class EvaluateSecuritiesDataQuality:
    """Classify one existing canonical Securities batch without rewriting it."""

    def __init__(
        self,
        *,
        repository: RealSecuritiesDataRepository,
        evidence: SecuritiesEvidencePort,
    ) -> None:
        self._repository = repository
        self._evidence = evidence

    def execute(
        self, command: EvaluateSecuritiesDataQualityCommand
    ) -> SecuritiesDataQualityEvaluation:
        batch = self._evidence.get_batch(command.data_batch_id)
        instruments, sessions, bars, actions = self._evidence.get_dataset(command.data_batch_id)
        universe = self._repository.universe_for_symbols(
            tuple(sorted(item.symbol for item in instruments))
        )
        if universe is None:
            raise RealSecuritiesDataError("REAL_SECURITIES_QUALITY_REQUIRES_EXACT_FROZEN_UNIVERSE")
        value = evaluate_securities_data_quality(
            market_data_batch_id=batch.batch_id,
            market_data_batch_hash=batch.canonical_hash,
            universe=universe,
            instruments=instruments,
            sessions=sessions,
            bars=bars,
            corporate_actions=actions,
            availability_policy_id=DERIVED_EOD_AVAILABLE_V1,
            availability_policy={"delay_minutes": 30, "derived_not_provider_timestamp": True},
            revision_count=0,
            created_at=command.created_at,
        )
        existing = self._repository.quality(value.evaluation_id)
        if existing is not None:
            if existing.canonical_input_hash != value.canonical_input_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_QUALITY_CONFLICT")
            return existing
        return self._repository.save_quality(value)


class RunSecuritiesHistoricalValidation:
    """Persist a data-gated historical validation result without capital authority.

    The provider ingress deliberately retains raw bars first.  Baseline and
    model research require separately retained, explicit total-return lineage;
    raw bars alone therefore create a visible blocked run instead of a silent
    price transformation or an unsupported historical claim.
    """

    def __init__(
        self, *, repository: RealSecuritiesDataRepository, evidence: SecuritiesEvidencePort
    ) -> None:
        self._repository = repository
        self._evidence = evidence

    def execute(
        self, command: RunSecuritiesHistoricalValidationCommand
    ) -> SecuritiesHistoricalValidationRun:
        configuration = self._repository.active_configuration()
        if configuration is None:
            raise RealSecuritiesDataError("REAL_SECURITIES_PROVIDER_CONFIGURATION_NOT_ACTIVE")
        universe = self._repository.universe(command.universe_id)
        if universe is None:
            raise RealSecuritiesDataError("REAL_SECURITIES_UNIVERSE_NOT_FOUND")
        qualities = tuple(
            value
            for value in self._repository.list_quality()
            if value.market_data_batch_id == command.data_batch_id
            and value.universe_id == universe.universe_id
        )
        if not qualities:
            raise RealSecuritiesDataError("REAL_SECURITIES_HISTORICAL_VALIDATION_REQUIRES_QUALITY")
        quality = qualities[-1]
        _instruments, _sessions, bars, _actions = self._evidence.get_dataset(command.data_batch_id)
        adjustment_states = {bar.adjustment_state.value for bar in bars}
        price_adjustment_state = (
            next(iter(adjustment_states))
            if len(adjustment_states) == 1
            else "INCONSISTENT_ADJUSTMENT_LINEAGE"
        )
        value = SecuritiesHistoricalValidationRun.request(
            configuration=configuration,
            universe=universe,
            quality=quality,
            start_at=command.start_at,
            end_at=command.end_at,
            cutoff_at=command.end_at,
            price_adjustment_state=price_adjustment_state,
            idempotency_key=command.idempotency_key,
            started_at=command.started_at,
        )
        existing = self._repository.validation_by_idempotency(command.idempotency_key)
        if existing is not None:
            if existing.canonical_input_hash != value.canonical_input_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_VALIDATION_IDEMPOTENCY_CONFLICT")
            return existing
        self._repository.save_reserved_validation(value)
        running = self._repository.transition_validation(replace(value, state="RUNNING"))
        blockers: list[str] = []
        if SecuritiesDataSuitability.HISTORICAL_BASELINE_READY not in quality.suitability_states:
            blockers.append("QUALITY_NOT_HISTORICAL_BASELINE_READY")
        if price_adjustment_state not in {"RAW", "TOTAL_RETURN_ADJUSTED"}:
            blockers.append("CONSISTENT_RETAINED_PRICE_LINEAGE_REQUIRED")
        if blockers:
            recommendation = SecuritiesHistoricalValidationRecommendation.BLOCKED_DATA_QUALITY
            state = "BLOCKED_INTEGRITY"
        else:
            # The subsequent baseline/conformance and Strategy Lab commands are
            # deliberately explicit operator operations; validation never starts
            # a search or deploys a candidate by itself.
            recommendation = SecuritiesHistoricalValidationRecommendation.BASELINES_ONLY
            state = "COMPLETED"
        completed_reasons = tuple(blockers) or (
            (
                "EXPLICIT_BASELINE_RUN_REQUIRED",
                "RAW_PRICE_RESEARCH_WITH_RETROSPECTIVE_ACTION_LIMITATIONS",
            )
            if price_adjustment_state == "RAW"
            else ("EXPLICIT_BASELINE_RUN_REQUIRED",)
        )
        completed = replace(
            running,
            state=state,
            recommendation=recommendation,
            reason_codes=completed_reasons,
            completed_at=command.started_at,
            canonical_result_hash=content_digest(
                {
                    "input": value.canonical_input_hash,
                    "state": state,
                    "recommendation": recommendation,
                    "reason_codes": completed_reasons,
                }
            ),
        )
        return self._repository.transition_validation(completed)


class CreateSecuritiesForwardValidationPlan:
    """Create only a DRAFT plan; activation remains an existing explicit control."""

    def __init__(self, repository: RealSecuritiesDataRepository) -> None:
        self._repository = repository

    def execute(
        self, command: CreateSecuritiesForwardValidationPlanCommand
    ) -> SecuritiesForwardValidationPlan:
        configuration = self._repository.active_configuration()
        if configuration is None:
            raise RealSecuritiesDataError("REAL_SECURITIES_PROVIDER_CONFIGURATION_NOT_ACTIVE")
        universe = self._repository.universe(command.universe_id)
        if universe is None:
            raise RealSecuritiesDataError("REAL_SECURITIES_UNIVERSE_NOT_FOUND")
        value = SecuritiesForwardValidationPlan.draft(
            configuration=configuration,
            universe=universe,
            feature_schema_id=command.feature_schema_id,
            feature_schema_hash=command.feature_schema_hash,
            prediction_horizon_sessions=command.prediction_horizon_sessions,
            operator_action_id="local-real-securities-forward-plan-v1",
            idempotency_key=command.idempotency_key,
            created_at=command.created_at,
        )
        existing = self._repository.plan_by_idempotency(command.idempotency_key)
        if existing is not None:
            if existing.canonical_input_hash != value.canonical_input_hash:
                raise RealSecuritiesDataConflict(
                    "REAL_SECURITIES_FORWARD_PLAN_IDEMPOTENCY_CONFLICT"
                )
            return existing
        return self._repository.save_forward_plan(value)


def _terminal_operation(
    operation: SecuritiesProviderFetchOperation,
    *,
    state: SecuritiesProviderFetchOperationState,
    failure_code: str,
    completed_at: datetime,
) -> SecuritiesProviderFetchOperation:
    return replace(
        operation,
        completed_at=completed_at,
        state=state,
        failure_code=failure_code,
        canonical_result_hash=content_digest(
            {
                "operation": operation.canonical_input_hash,
                "state": state,
                "failure_code": failure_code,
            }
        ),
    )


def _endpoint_for_operation(value: SecuritiesProviderFetchOperationType) -> str:
    return {
        SecuritiesProviderFetchOperationType.ASSET_SNAPSHOT: "asset_master",
        SecuritiesProviderFetchOperationType.HISTORICAL_DAILY_BARS: "historical_daily_bars",
        SecuritiesProviderFetchOperationType.CORPORATE_ACTIONS: "corporate_actions",
        SecuritiesProviderFetchOperationType.LATEST_SNAPSHOTS: "latest_snapshots",
        SecuritiesProviderFetchOperationType.FORWARD_DAILY_CAPTURE: "historical_daily_bars",
        SecuritiesProviderFetchOperationType.OVERLAP_REVISION_CHECK: "historical_daily_bars",
    }[value]


def _query_for_operation(command: FetchSecuritiesProviderOperationCommand) -> dict[str, str]:
    symbols = ",".join(command.symbols)
    if command.operation_type is SecuritiesProviderFetchOperationType.ASSET_SNAPSHOT:
        return {"status": "active", "asset_class": "us_equity"}
    if command.operation_type is SecuritiesProviderFetchOperationType.LATEST_SNAPSHOTS:
        return {"symbols": symbols, "feed": "iex"}
    if command.operation_type is SecuritiesProviderFetchOperationType.CORPORATE_ACTIONS:
        return {
            "symbols": symbols,
            "start": _date_text(command.start_at),
            "end": _date_text(command.end_at),
            "limit": "1000",
            "sort": "asc",
            "region": "us",
        }
    return {
        "symbols": symbols,
        "timeframe": "1Day",
        "start": _date_text(command.start_at),
        "end": _date_text(command.end_at),
        "feed": "iex",
        "adjustment": "raw",
        "limit": "10000",
        "sort": "asc",
    }


def _date_text(value: datetime | None) -> str:
    if value is None:
        raise RealSecuritiesDataError("REAL_SECURITIES_DATE_RANGE_REQUIRED")
    if value.date() < date(2018, 1, 1):
        raise RealSecuritiesDataError("REAL_SECURITIES_DATE_BEFORE_INITIAL_SCOPE")
    return value.date().isoformat()


def _record_count(page: SecuritiesProviderPage) -> int:
    try:
        payload = json.loads(page.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 0
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return 0
    for name in ("bars", "corporate_actions", "snapshots"):
        value = payload.get(name)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            return len(value)
    return len(payload)


def _capability_endpoint_state(code: str) -> str:
    if code.startswith("ALPACA_AUTHENTICATION_REJECTED_HTTP_"):
        return "AUTHENTICATION_REJECTED"
    if code.startswith("PROVIDER_HTTP_403"):
        return "PERMISSION_LIMITED"
    if code in {"PROVIDER_NETWORK_TIMEOUT", "PROVIDER_RETRY_EXHAUSTED"} or code.startswith(
        "PROVIDER_HTTP_5"
    ):
        return "PROVIDER_UNAVAILABLE"
    return "PARTIAL"


def _provider_error_code(error: Exception) -> str:
    value = getattr(error, "code", None)
    if isinstance(value, str) and value:
        return value
    return "PROVIDER_UNEXPECTED_FAILURE"


def real_data_warnings() -> tuple[str, ...]:
    return (
        REAL_DATA_WARNING,
        IEX_COVERAGE_WARNING,
        HISTORICAL_AUTHORITY_WARNING,
        DERIVED_EOD_AVAILABLE_V1,
    )
