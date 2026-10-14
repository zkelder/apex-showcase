"""PostgreSQL persistence for real Securities provider evidence only."""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import (
    securities_data_quality_evaluation,
    securities_forward_validation_plan,
    securities_historical_validation_run,
    securities_provider_capability_snapshot,
    securities_provider_configuration,
    securities_provider_fetch_operation,
    securities_universe_snapshot,
)
from apex.application.real_securities_data import RealSecuritiesDataConflict
from apex.domain.real_securities_data import (
    SecuritiesDataIntegrityState,
    SecuritiesDataQualityEvaluation,
    SecuritiesDataSuitability,
    SecuritiesForwardValidationPlan,
    SecuritiesForwardValidationPlanState,
    SecuritiesHistoricalClaimClassification,
    SecuritiesHistoricalMembershipClaim,
    SecuritiesHistoricalValidationRecommendation,
    SecuritiesHistoricalValidationRun,
    SecuritiesProviderCapabilitySnapshot,
    SecuritiesProviderCapabilityState,
    SecuritiesProviderConfiguration,
    SecuritiesProviderConfigurationState,
    SecuritiesProviderFetchOperation,
    SecuritiesProviderFetchOperationState,
    SecuritiesProviderFetchOperationType,
    SecuritiesProviderIdentity,
    SecuritiesSurvivorshipState,
    SecuritiesUniverseSnapshot,
    SecuritiesUniverseSnapshotState,
)
from apex.domain.values import canonical_json_bytes


def _json(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _mapping(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RealSecuritiesDataConflict("REAL_SECURITIES_JSON_OBJECT_REQUIRED")
    return parsed


class PostgresRealSecuritiesDataRepository:
    """Narrow provider evidence repository, intentionally financial-repository-free."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def configuration(self, configuration_id: str) -> SecuritiesProviderConfiguration | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_provider_configuration).where(
                        securities_provider_configuration.c.configuration_id == configuration_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _configuration(row)

    def active_configuration(self) -> SecuritiesProviderConfiguration | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_provider_configuration)
                    .where(securities_provider_configuration.c.state == "ACTIVE")
                    .order_by(securities_provider_configuration.c.created_at.desc())
                )
                .mappings()
                .first()
            )
        return None if row is None else _configuration(row)

    def save_configuration(
        self, value: SecuritiesProviderConfiguration
    ) -> SecuritiesProviderConfiguration:
        prior = self.configuration(value.configuration_id)
        if prior is not None:
            if prior.canonical_payload_hash != value.canonical_payload_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_PROVIDER_CONFIGURATION_CONFLICT")
            return prior
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    securities_provider_configuration.insert().values(
                        **_configuration_values(value)
                    )
                )
        except sa.exc.IntegrityError as error:
            raise RealSecuritiesDataConflict(
                "REAL_SECURITIES_PROVIDER_CONFIGURATION_CONFLICT"
            ) from error
        return value

    def save_capability(
        self, value: SecuritiesProviderCapabilitySnapshot
    ) -> SecuritiesProviderCapabilitySnapshot:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(securities_provider_capability_snapshot.c.canonical_result_hash).where(
                    securities_provider_capability_snapshot.c.snapshot_id == value.snapshot_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if str(existing) != value.canonical_result_hash:
                    raise RealSecuritiesDataConflict("REAL_SECURITIES_CAPABILITY_CONFLICT")
                return value
            connection.execute(
                securities_provider_capability_snapshot.insert().values(**_capability_values(value))
            )
        return value

    def latest_capability(
        self, configuration_id: str
    ) -> SecuritiesProviderCapabilitySnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_provider_capability_snapshot)
                    .where(
                        securities_provider_capability_snapshot.c.configuration_id
                        == configuration_id
                    )
                    .order_by(securities_provider_capability_snapshot.c.checked_at.desc())
                )
                .mappings()
                .first()
            )
        return None if row is None else _capability(row)

    def universe(self, universe_id: str) -> SecuritiesUniverseSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_universe_snapshot).where(
                        securities_universe_snapshot.c.universe_id == universe_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _universe(row)

    def universe_by_idempotency(self, key: str) -> SecuritiesUniverseSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_universe_snapshot).where(
                        securities_universe_snapshot.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _universe(row)

    def list_universes(self) -> tuple[SecuritiesUniverseSnapshot, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(securities_universe_snapshot).order_by(
                    securities_universe_snapshot.c.frozen_at,
                    securities_universe_snapshot.c.universe_id,
                )
            ).mappings()
            return tuple(_universe(row) for row in rows)

    def universe_for_symbols(self, symbols: tuple[str, ...]) -> SecuritiesUniverseSnapshot | None:
        normalized = tuple(sorted(symbols))
        for value in self.list_universes():
            if value.symbols == normalized:
                return value
        return None

    def save_universe(self, value: SecuritiesUniverseSnapshot) -> SecuritiesUniverseSnapshot:
        prior = self.universe_by_idempotency(value.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_UNIVERSE_IDEMPOTENCY_CONFLICT")
            return prior
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    securities_universe_snapshot.insert().values(**_universe_values(value))
                )
        except sa.exc.IntegrityError as error:
            raise RealSecuritiesDataConflict("REAL_SECURITIES_UNIVERSE_CONFLICT") from error
        return value

    def operation_by_idempotency(self, key: str) -> SecuritiesProviderFetchOperation | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_provider_fetch_operation).where(
                        securities_provider_fetch_operation.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _operation(row)

    def list_operations(self) -> tuple[SecuritiesProviderFetchOperation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(securities_provider_fetch_operation).order_by(
                    securities_provider_fetch_operation.c.started_at,
                    securities_provider_fetch_operation.c.operation_id,
                )
            ).mappings()
            return tuple(_operation(row) for row in rows)

    def quality(self, evaluation_id: str) -> SecuritiesDataQualityEvaluation | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_data_quality_evaluation).where(
                        securities_data_quality_evaluation.c.evaluation_id == evaluation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _quality(row)

    def list_quality(self) -> tuple[SecuritiesDataQualityEvaluation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(securities_data_quality_evaluation).order_by(
                    securities_data_quality_evaluation.c.created_at,
                    securities_data_quality_evaluation.c.evaluation_id,
                )
            ).mappings()
            return tuple(_quality(row) for row in rows)

    def save_quality(
        self, value: SecuritiesDataQualityEvaluation
    ) -> SecuritiesDataQualityEvaluation:
        existing = self.quality(value.evaluation_id)
        if existing is not None:
            if existing.canonical_result_hash != value.canonical_result_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_QUALITY_CONFLICT")
            return existing
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    securities_data_quality_evaluation.insert().values(**_quality_values(value))
                )
        except sa.exc.IntegrityError as error:
            raise RealSecuritiesDataConflict("REAL_SECURITIES_QUALITY_CONFLICT") from error
        return value

    def validation_by_idempotency(self, key: str) -> SecuritiesHistoricalValidationRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_historical_validation_run).where(
                        securities_historical_validation_run.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _validation(row)

    def list_validations(self) -> tuple[SecuritiesHistoricalValidationRun, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(securities_historical_validation_run).order_by(
                    securities_historical_validation_run.c.started_at,
                    securities_historical_validation_run.c.validation_run_id,
                )
            ).mappings()
            return tuple(_validation(row) for row in rows)

    def save_reserved_validation(
        self, value: SecuritiesHistoricalValidationRun
    ) -> SecuritiesHistoricalValidationRun:
        prior = self.validation_by_idempotency(value.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_VALIDATION_IDEMPOTENCY_CONFLICT")
            return prior
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    securities_historical_validation_run.insert().values(
                        **_validation_values(value)
                    )
                )
        except sa.exc.IntegrityError as error:
            raise RealSecuritiesDataConflict("REAL_SECURITIES_VALIDATION_CONFLICT") from error
        return value

    def transition_validation(
        self, value: SecuritiesHistoricalValidationRun
    ) -> SecuritiesHistoricalValidationRun:
        with self._engine.begin() as connection:
            result = connection.execute(
                securities_historical_validation_run.update()
                .where(
                    securities_historical_validation_run.c.validation_run_id
                    == value.validation_run_id,
                    securities_historical_validation_run.c.canonical_input_hash
                    == value.canonical_input_hash,
                )
                .values(
                    state=value.state,
                    baseline_run_ids=list(value.baseline_run_ids),
                    scorecard_ids=list(value.scorecard_ids),
                    simulator_conformance_ids=list(value.simulator_conformance_ids),
                    dataset_id=value.dataset_id,
                    dataset_hash=value.dataset_hash,
                    experiment_id=value.experiment_id,
                    experiment_hash=value.experiment_hash,
                    candidate_search_run_id=value.candidate_search_run_id,
                    multiple_testing_evaluation_id=value.multiple_testing_evaluation_id,
                    recommendation=value.recommendation.value,
                    reason_codes=list(value.reason_codes),
                    completed_at=value.completed_at,
                    canonical_result_hash=value.canonical_result_hash,
                )
            )
            if result.rowcount != 1:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_VALIDATION_STATE_CONFLICT")
        return value

    def plan_by_idempotency(self, key: str) -> SecuritiesForwardValidationPlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_forward_validation_plan).where(
                        securities_forward_validation_plan.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _forward_plan(row)

    def list_forward_plans(self) -> tuple[SecuritiesForwardValidationPlan, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(securities_forward_validation_plan).order_by(
                    securities_forward_validation_plan.c.created_at,
                    securities_forward_validation_plan.c.plan_id,
                )
            ).mappings()
            return tuple(_forward_plan(row) for row in rows)

    def save_forward_plan(
        self, value: SecuritiesForwardValidationPlan
    ) -> SecuritiesForwardValidationPlan:
        prior = self.plan_by_idempotency(value.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise RealSecuritiesDataConflict(
                    "REAL_SECURITIES_FORWARD_PLAN_IDEMPOTENCY_CONFLICT"
                )
            return prior
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    securities_forward_validation_plan.insert().values(**_plan_values(value))
                )
        except sa.exc.IntegrityError as error:
            raise RealSecuritiesDataConflict("REAL_SECURITIES_FORWARD_PLAN_CONFLICT") from error
        return value

    def save_reserved_operation(
        self, value: SecuritiesProviderFetchOperation
    ) -> SecuritiesProviderFetchOperation:
        prior = self.operation_by_idempotency(value.idempotency_key)
        if prior is not None:
            if prior.canonical_input_hash != value.canonical_input_hash:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_FETCH_IDEMPOTENCY_CONFLICT")
            return prior
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    securities_provider_fetch_operation.insert().values(**_operation_values(value))
                )
        except sa.exc.IntegrityError as error:
            raise RealSecuritiesDataConflict("REAL_SECURITIES_FETCH_CONFLICT") from error
        return value

    def transition_operation(
        self, value: SecuritiesProviderFetchOperation
    ) -> SecuritiesProviderFetchOperation:
        with self._engine.begin() as connection:
            result = connection.execute(
                securities_provider_fetch_operation.update()
                .where(
                    securities_provider_fetch_operation.c.operation_id == value.operation_id,
                    securities_provider_fetch_operation.c.canonical_input_hash
                    == value.canonical_input_hash,
                )
                .values(
                    request_count=value.request_count,
                    page_count=value.page_count,
                    raw_byte_count=value.raw_byte_count,
                    received_record_count=value.received_record_count,
                    completed_at=value.completed_at,
                    state=value.state.value,
                    retry_count=value.retry_count,
                    rate_limit_evidence=_json(value.rate_limit_evidence),
                    provider_request_ids=list(value.provider_request_ids),
                    raw_artifact_relative_path=value.raw_artifact_relative_path,
                    raw_artifact_set_hash=value.raw_artifact_set_hash,
                    normalized_batch_id=value.normalized_batch_id,
                    normalized_batch_hash=value.normalized_batch_hash,
                    failure_code=value.failure_code,
                    canonical_result_hash=value.canonical_result_hash,
                )
            )
            if result.rowcount != 1:
                raise RealSecuritiesDataConflict("REAL_SECURITIES_FETCH_STATE_CONFLICT")
        return value


def _configuration_values(value: SecuritiesProviderConfiguration) -> dict[str, object]:
    return {
        "configuration_id": value.configuration_id,
        "provider": value.provider.value,
        "adapter_version": value.adapter_version,
        "environment": value.environment,
        "feed": value.feed,
        "asset_classes": list(value.asset_classes),
        "endpoint_policy_id": value.endpoint_policy_id,
        "endpoint_policy_hash": value.endpoint_policy_hash,
        "request_timeout_seconds": value.request_timeout_seconds,
        "retry_policy": _json(value.retry_policy),
        "rate_limit_policy": _json(value.rate_limit_policy),
        "maximum_pages_per_operation": value.maximum_pages_per_operation,
        "maximum_symbols_per_operation": value.maximum_symbols_per_operation,
        "maximum_response_bytes": value.maximum_response_bytes,
        "raw_artifact_policy": _json(value.raw_artifact_policy),
        "raw_artifact_policy_hash": value.raw_artifact_policy_hash,
        "local_research_only": value.local_research_only,
        "state": value.state.value,
        "created_at": value.created_at,
        "superseded_at": value.superseded_at,
        "operator_action_id": value.operator_action_id,
        "canonical_payload_hash": value.canonical_payload_hash,
    }


def _configuration(row: RowMapping) -> SecuritiesProviderConfiguration:
    return SecuritiesProviderConfiguration(
        configuration_id=str(row["configuration_id"]),
        provider=SecuritiesProviderIdentity(str(row["provider"])),
        adapter_version=str(row["adapter_version"]),
        environment=str(row["environment"]),
        feed=str(row["feed"]),
        asset_classes=tuple(str(item) for item in row["asset_classes"]),
        endpoint_policy_id=str(row["endpoint_policy_id"]),
        endpoint_policy_hash=str(row["endpoint_policy_hash"]),
        request_timeout_seconds=int(row["request_timeout_seconds"]),
        retry_policy=_mapping(str(row["retry_policy"])),
        rate_limit_policy=_mapping(str(row["rate_limit_policy"])),
        maximum_pages_per_operation=int(row["maximum_pages_per_operation"]),
        maximum_symbols_per_operation=int(row["maximum_symbols_per_operation"]),
        maximum_response_bytes=int(row["maximum_response_bytes"]),
        raw_artifact_policy=_mapping(str(row["raw_artifact_policy"])),
        raw_artifact_policy_hash=str(row["raw_artifact_policy_hash"]),
        local_research_only=bool(row["local_research_only"]),
        state=SecuritiesProviderConfigurationState(str(row["state"])),
        created_at=row["created_at"],
        superseded_at=row["superseded_at"],
        operator_action_id=str(row["operator_action_id"]),
        canonical_payload_hash=str(row["canonical_payload_hash"]),
    )


def _capability_values(value: SecuritiesProviderCapabilitySnapshot) -> dict[str, object]:
    return {
        "snapshot_id": value.snapshot_id,
        "configuration_id": value.configuration_id,
        "configuration_hash": value.configuration_hash,
        "checked_at": value.checked_at,
        "credentials_available": value.credentials_available,
        "authentication_state": value.authentication_state,
        "historical_bars_state": value.historical_bars_state,
        "snapshots_state": value.snapshots_state,
        "corporate_actions_state": value.corporate_actions_state,
        "asset_master_state": value.asset_master_state,
        "feed": value.feed,
        "earliest_requestable_date": value.earliest_requestable_date,
        "rate_limit_observations": _json(value.rate_limit_observations),
        "endpoint_reason_codes": _json(value.endpoint_reason_codes),
        "provider_runtime_version": value.provider_runtime_version,
        "state": value.state.value,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _capability(row: RowMapping) -> SecuritiesProviderCapabilitySnapshot:
    reason_codes = _mapping(str(row["endpoint_reason_codes"]))
    return SecuritiesProviderCapabilitySnapshot(
        snapshot_id=str(row["snapshot_id"]),
        configuration_id=str(row["configuration_id"]),
        configuration_hash=str(row["configuration_hash"]),
        checked_at=row["checked_at"],
        credentials_available=bool(row["credentials_available"]),
        authentication_state=str(row["authentication_state"]),
        historical_bars_state=str(row["historical_bars_state"]),
        snapshots_state=str(row["snapshots_state"]),
        corporate_actions_state=str(row["corporate_actions_state"]),
        asset_master_state=str(row["asset_master_state"]),
        feed=str(row["feed"]),
        earliest_requestable_date=(
            None
            if row["earliest_requestable_date"] is None
            else str(row["earliest_requestable_date"])
        ),
        rate_limit_observations=_mapping(str(row["rate_limit_observations"])),
        endpoint_reason_codes={
            str(key): tuple(str(code) for code in value)
            for key, value in reason_codes.items()
            if isinstance(value, list)
        },
        provider_runtime_version=str(row["provider_runtime_version"]),
        state=SecuritiesProviderCapabilityState(str(row["state"])),
        canonical_result_hash=str(row["canonical_result_hash"]),
    )


def _universe_values(value: SecuritiesUniverseSnapshot) -> dict[str, object]:
    return {
        "universe_id": value.universe_id,
        "display_name": value.display_name,
        "construction_policy_id": str(value.construction_policy_id),
        "construction_policy_payload": _json(value.construction_policy_payload),
        "construction_policy_hash": value.construction_policy_hash,
        "symbol_count": len(value.symbols),
        "symbols": list(value.symbols),
        "symbols_hash": value.symbols_hash,
        "asset_identities": list(value.asset_identities),
        "asset_identities_hash": value.asset_identities_hash,
        "frozen_at": value.frozen_at,
        "historical_membership_claim": value.historical_membership_claim.value,
        "survivorship_state": value.survivorship_state.value,
        "provider_asset_snapshot_id": value.provider_asset_snapshot_id,
        "exclusions": list(value.exclusions),
        "state": value.state.value,
        "operator_action_id": value.operator_action_id,
        "idempotency_key": value.idempotency_key,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _universe(row: RowMapping) -> SecuritiesUniverseSnapshot:
    return SecuritiesUniverseSnapshot(
        universe_id=str(row["universe_id"]),
        display_name=str(row["display_name"]),
        construction_policy_id=SecuritiesHistoricalMembershipClaim(
            str(row["construction_policy_id"])
        ),
        construction_policy_payload=_mapping(str(row["construction_policy_payload"])),
        construction_policy_hash=str(row["construction_policy_hash"]),
        symbols=tuple(str(item) for item in row["symbols"]),
        symbols_hash=str(row["symbols_hash"]),
        asset_identities=tuple(str(item) for item in row["asset_identities"]),
        asset_identities_hash=str(row["asset_identities_hash"]),
        frozen_at=row["frozen_at"],
        historical_membership_claim=SecuritiesHistoricalMembershipClaim(
            str(row["historical_membership_claim"])
        ),
        survivorship_state=SecuritiesSurvivorshipState(str(row["survivorship_state"])),
        provider_asset_snapshot_id=(
            None
            if row["provider_asset_snapshot_id"] is None
            else str(row["provider_asset_snapshot_id"])
        ),
        exclusions=tuple(str(item) for item in row["exclusions"]),
        state=SecuritiesUniverseSnapshotState(str(row["state"])),
        operator_action_id=str(row["operator_action_id"]),
        idempotency_key=str(row["idempotency_key"]),
        canonical_input_hash=str(row["canonical_input_hash"]),
        canonical_result_hash=str(row["canonical_result_hash"]),
    )


def _operation_values(value: SecuritiesProviderFetchOperation) -> dict[str, object]:
    return {
        "operation_id": value.operation_id,
        "configuration_id": value.configuration_id,
        "configuration_hash": value.configuration_hash,
        "operation_type": value.operation_type.value,
        "universe_id": value.universe_id,
        "universe_hash": value.universe_hash,
        "symbols": list(value.symbols),
        "symbols_hash": value.symbols_hash,
        "start_at": value.start_at,
        "end_at": value.end_at,
        "feed": value.feed,
        "adjustment": value.adjustment,
        "request_count": value.request_count,
        "page_count": value.page_count,
        "raw_byte_count": value.raw_byte_count,
        "received_record_count": value.received_record_count,
        "started_at": value.started_at,
        "completed_at": value.completed_at,
        "state": value.state.value,
        "retry_count": value.retry_count,
        "rate_limit_evidence": _json(value.rate_limit_evidence),
        "provider_request_ids": list(value.provider_request_ids),
        "raw_artifact_relative_path": value.raw_artifact_relative_path,
        "raw_artifact_set_hash": value.raw_artifact_set_hash,
        "normalized_batch_id": value.normalized_batch_id,
        "normalized_batch_hash": value.normalized_batch_hash,
        "idempotency_key": value.idempotency_key,
        "failure_code": value.failure_code,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _operation(row: RowMapping) -> SecuritiesProviderFetchOperation:
    return SecuritiesProviderFetchOperation(
        operation_id=str(row["operation_id"]),
        configuration_id=str(row["configuration_id"]),
        configuration_hash=str(row["configuration_hash"]),
        operation_type=SecuritiesProviderFetchOperationType(str(row["operation_type"])),
        universe_id=None if row["universe_id"] is None else str(row["universe_id"]),
        universe_hash=None if row["universe_hash"] is None else str(row["universe_hash"]),
        symbols=tuple(str(item) for item in row["symbols"]),
        symbols_hash=str(row["symbols_hash"]),
        start_at=row["start_at"],
        end_at=row["end_at"],
        feed=str(row["feed"]),
        adjustment=None if row["adjustment"] is None else str(row["adjustment"]),
        request_count=int(row["request_count"]),
        page_count=int(row["page_count"]),
        raw_byte_count=int(row["raw_byte_count"]),
        received_record_count=int(row["received_record_count"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        state=SecuritiesProviderFetchOperationState(str(row["state"])),
        retry_count=int(row["retry_count"]),
        rate_limit_evidence=_mapping(str(row["rate_limit_evidence"])),
        provider_request_ids=tuple(str(item) for item in row["provider_request_ids"]),
        raw_artifact_relative_path=(
            None
            if row["raw_artifact_relative_path"] is None
            else str(row["raw_artifact_relative_path"])
        ),
        raw_artifact_set_hash=(
            None if row["raw_artifact_set_hash"] is None else str(row["raw_artifact_set_hash"])
        ),
        normalized_batch_id=None
        if row["normalized_batch_id"] is None
        else str(row["normalized_batch_id"]),
        normalized_batch_hash=None
        if row["normalized_batch_hash"] is None
        else str(row["normalized_batch_hash"]),
        idempotency_key=str(row["idempotency_key"]),
        failure_code=None if row["failure_code"] is None else str(row["failure_code"]),
        canonical_input_hash=str(row["canonical_input_hash"]),
        canonical_result_hash=(
            None if row["canonical_result_hash"] is None else str(row["canonical_result_hash"])
        ),
    )


def _quality_values(value: SecuritiesDataQualityEvaluation) -> dict[str, object]:
    return {
        "evaluation_id": value.evaluation_id,
        "market_data_batch_id": value.market_data_batch_id,
        "market_data_batch_hash": value.market_data_batch_hash,
        "universe_id": value.universe_id,
        "universe_hash": value.universe_hash,
        "calendar_identity": value.calendar_identity,
        "calendar_version": value.calendar_version,
        "expected_session_count": value.expected_session_count,
        "observed_bar_count": value.observed_bar_count,
        "missing_bar_session_count": value.missing_bar_session_count,
        "duplicate_count": value.duplicate_count,
        "zero_or_negative_price_count": value.zero_or_negative_price_count,
        "invalid_ohlc_count": value.invalid_ohlc_count,
        "zero_volume_count": value.zero_volume_count,
        "stale_series_count": value.stale_series_count,
        "extreme_return_count": value.extreme_return_count,
        "action_consistency_count": value.action_consistency_count,
        "symbol_lifecycle_issue_count": value.symbol_lifecycle_issue_count,
        "revision_count": value.revision_count,
        "feed_coverage_limitations": list(value.feed_coverage_limitations),
        "availability_policy_id": value.availability_policy_id,
        "availability_policy_hash": value.availability_policy_hash,
        "integrity_state": value.integrity_state.value,
        "suitability_states": [item.value for item in value.suitability_states],
        "reason_codes": list(value.reason_codes),
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
        "created_at": value.created_at,
    }


def _quality(row: RowMapping) -> SecuritiesDataQualityEvaluation:
    return SecuritiesDataQualityEvaluation(
        evaluation_id=str(row["evaluation_id"]),
        market_data_batch_id=str(row["market_data_batch_id"]),
        market_data_batch_hash=str(row["market_data_batch_hash"]),
        universe_id=str(row["universe_id"]),
        universe_hash=str(row["universe_hash"]),
        calendar_identity=str(row["calendar_identity"]),
        calendar_version=str(row["calendar_version"]),
        expected_session_count=int(row["expected_session_count"]),
        observed_bar_count=int(row["observed_bar_count"]),
        missing_bar_session_count=int(row["missing_bar_session_count"]),
        duplicate_count=int(row["duplicate_count"]),
        zero_or_negative_price_count=int(row["zero_or_negative_price_count"]),
        invalid_ohlc_count=int(row["invalid_ohlc_count"]),
        zero_volume_count=int(row["zero_volume_count"]),
        stale_series_count=int(row["stale_series_count"]),
        extreme_return_count=int(row["extreme_return_count"]),
        action_consistency_count=int(row["action_consistency_count"]),
        symbol_lifecycle_issue_count=int(row["symbol_lifecycle_issue_count"]),
        revision_count=int(row["revision_count"]),
        feed_coverage_limitations=tuple(str(item) for item in row["feed_coverage_limitations"]),
        availability_policy_id=str(row["availability_policy_id"]),
        availability_policy_hash=str(row["availability_policy_hash"]),
        integrity_state=SecuritiesDataIntegrityState(str(row["integrity_state"])),
        suitability_states=tuple(
            SecuritiesDataSuitability(str(item)) for item in row["suitability_states"]
        ),
        reason_codes=tuple(str(item) for item in row["reason_codes"]),
        canonical_input_hash=str(row["canonical_input_hash"]),
        canonical_result_hash=str(row["canonical_result_hash"]),
        created_at=row["created_at"],
    )


def _validation_values(value: SecuritiesHistoricalValidationRun) -> dict[str, object]:
    return {
        "validation_run_id": value.validation_run_id,
        "configuration_id": value.configuration_id,
        "configuration_hash": value.configuration_hash,
        "universe_id": value.universe_id,
        "universe_hash": value.universe_hash,
        "market_data_batch_id": value.market_data_batch_id,
        "market_data_batch_hash": value.market_data_batch_hash,
        "quality_evaluation_id": value.quality_evaluation_id,
        "quality_evaluation_hash": value.quality_evaluation_hash,
        "start_at": value.start_at,
        "end_at": value.end_at,
        "cutoff_at": value.cutoff_at,
        "availability_policy_id": value.availability_policy_id,
        "availability_policy_hash": value.availability_policy_hash,
        "price_adjustment_state": value.price_adjustment_state,
        "baseline_suite_id": value.baseline_suite_id,
        "baseline_suite_hash": value.baseline_suite_hash,
        "baseline_run_ids": list(value.baseline_run_ids),
        "scorecard_ids": list(value.scorecard_ids),
        "simulator_conformance_ids": list(value.simulator_conformance_ids),
        "dataset_id": value.dataset_id,
        "dataset_hash": value.dataset_hash,
        "experiment_id": value.experiment_id,
        "experiment_hash": value.experiment_hash,
        "candidate_search_run_id": value.candidate_search_run_id,
        "multiple_testing_evaluation_id": value.multiple_testing_evaluation_id,
        "state": value.state,
        "historical_claim_classification": value.historical_claim_classification.value,
        "recommendation": value.recommendation.value,
        "reason_codes": list(value.reason_codes),
        "started_at": value.started_at,
        "completed_at": value.completed_at,
        "idempotency_key": value.idempotency_key,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _validation(row: RowMapping) -> SecuritiesHistoricalValidationRun:
    return SecuritiesHistoricalValidationRun(
        validation_run_id=str(row["validation_run_id"]),
        configuration_id=str(row["configuration_id"]),
        configuration_hash=str(row["configuration_hash"]),
        universe_id=str(row["universe_id"]),
        universe_hash=str(row["universe_hash"]),
        market_data_batch_id=str(row["market_data_batch_id"]),
        market_data_batch_hash=str(row["market_data_batch_hash"]),
        quality_evaluation_id=str(row["quality_evaluation_id"]),
        quality_evaluation_hash=str(row["quality_evaluation_hash"]),
        start_at=row["start_at"],
        end_at=row["end_at"],
        cutoff_at=row["cutoff_at"],
        availability_policy_id=str(row["availability_policy_id"]),
        availability_policy_hash=str(row["availability_policy_hash"]),
        price_adjustment_state=str(row["price_adjustment_state"]),
        baseline_suite_id=None
        if row["baseline_suite_id"] is None
        else str(row["baseline_suite_id"]),
        baseline_suite_hash=None
        if row["baseline_suite_hash"] is None
        else str(row["baseline_suite_hash"]),
        baseline_run_ids=tuple(str(item) for item in row["baseline_run_ids"]),
        scorecard_ids=tuple(str(item) for item in row["scorecard_ids"]),
        simulator_conformance_ids=tuple(str(item) for item in row["simulator_conformance_ids"]),
        dataset_id=None if row["dataset_id"] is None else str(row["dataset_id"]),
        dataset_hash=None if row["dataset_hash"] is None else str(row["dataset_hash"]),
        experiment_id=None if row["experiment_id"] is None else str(row["experiment_id"]),
        experiment_hash=None if row["experiment_hash"] is None else str(row["experiment_hash"]),
        candidate_search_run_id=(
            None if row["candidate_search_run_id"] is None else str(row["candidate_search_run_id"])
        ),
        multiple_testing_evaluation_id=(
            None
            if row["multiple_testing_evaluation_id"] is None
            else str(row["multiple_testing_evaluation_id"])
        ),
        state=str(row["state"]),
        historical_claim_classification=SecuritiesHistoricalClaimClassification(
            str(row["historical_claim_classification"])
        ),
        recommendation=SecuritiesHistoricalValidationRecommendation(str(row["recommendation"])),
        reason_codes=tuple(str(item) for item in row["reason_codes"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        idempotency_key=str(row["idempotency_key"]),
        canonical_input_hash=str(row["canonical_input_hash"]),
        canonical_result_hash=(
            None if row["canonical_result_hash"] is None else str(row["canonical_result_hash"])
        ),
    )


def _plan_values(value: SecuritiesForwardValidationPlan) -> dict[str, object]:
    return {
        "plan_id": value.plan_id,
        "configuration_id": value.configuration_id,
        "configuration_hash": value.configuration_hash,
        "universe_id": value.universe_id,
        "universe_hash": value.universe_hash,
        "feature_schema_id": value.feature_schema_id,
        "feature_schema_hash": value.feature_schema_hash,
        "champion_deployment_id": value.champion_deployment_id,
        "challenger_deployment_id": value.challenger_deployment_id,
        "capture_interval_seconds": value.capture_interval_seconds,
        "capture_cutoff_policy": _json(value.capture_cutoff_policy),
        "prediction_horizon_sessions": value.prediction_horizon_sessions,
        "minimum_predictions": value.minimum_predictions,
        "minimum_resolved_outcomes": value.minimum_resolved_outcomes,
        "maximum_duration_seconds": value.maximum_duration_seconds,
        "corporate_action_polling_policy": _json(value.corporate_action_polling_policy),
        "missing_session_policy": _json(value.missing_session_policy),
        "state": value.state.value,
        "current_watermark": value.current_watermark,
        "created_at": value.created_at,
        "activated_at": value.activated_at,
        "paused_at": value.paused_at,
        "revoked_at": value.revoked_at,
        "operator_action_id": value.operator_action_id,
        "idempotency_key": value.idempotency_key,
        "canonical_input_hash": value.canonical_input_hash,
        "canonical_result_hash": value.canonical_result_hash,
    }


def _forward_plan(row: RowMapping) -> SecuritiesForwardValidationPlan:
    return SecuritiesForwardValidationPlan(
        plan_id=str(row["plan_id"]),
        configuration_id=str(row["configuration_id"]),
        configuration_hash=str(row["configuration_hash"]),
        universe_id=str(row["universe_id"]),
        universe_hash=str(row["universe_hash"]),
        feature_schema_id=str(row["feature_schema_id"]),
        feature_schema_hash=str(row["feature_schema_hash"]),
        champion_deployment_id=(
            None if row["champion_deployment_id"] is None else str(row["champion_deployment_id"])
        ),
        challenger_deployment_id=(
            None
            if row["challenger_deployment_id"] is None
            else str(row["challenger_deployment_id"])
        ),
        capture_interval_seconds=int(row["capture_interval_seconds"]),
        capture_cutoff_policy=_mapping(str(row["capture_cutoff_policy"])),
        prediction_horizon_sessions=int(row["prediction_horizon_sessions"]),
        minimum_predictions=int(row["minimum_predictions"]),
        minimum_resolved_outcomes=int(row["minimum_resolved_outcomes"]),
        maximum_duration_seconds=int(row["maximum_duration_seconds"]),
        corporate_action_polling_policy=_mapping(str(row["corporate_action_polling_policy"])),
        missing_session_policy=_mapping(str(row["missing_session_policy"])),
        state=SecuritiesForwardValidationPlanState(str(row["state"])),
        current_watermark=None
        if row["current_watermark"] is None
        else str(row["current_watermark"]),
        created_at=row["created_at"],
        activated_at=row["activated_at"],
        paused_at=row["paused_at"],
        revoked_at=row["revoked_at"],
        operator_action_id=str(row["operator_action_id"]),
        idempotency_key=str(row["idempotency_key"]),
        canonical_input_hash=str(row["canonical_input_hash"]),
        canonical_result_hash=str(row["canonical_result_hash"]),
    )
