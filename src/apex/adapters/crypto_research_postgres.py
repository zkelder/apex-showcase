"""PostgreSQL repository for immutable historical crypto research evidence."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.research_evidence_postgres import (
    PostgresResearchEvidenceRepository,
)
from apex.application.crypto_research import CryptoResearchConflict
from apex.domain.crypto_research import (
    CostScenarioName,
    CryptoAttemptStatus,
    CryptoCaptureCheckpoint,
    CryptoCaptureQuality,
    CryptoDailyCandle,
    CryptoExperimentOutcome,
    CryptoExperimentRun,
    CryptoHistoricalCapture,
    CryptoHistoricalSnapshot,
    CryptoHypothesisSpecification,
    CryptoPerformanceMetrics,
    CryptoRequestAttempt,
    CryptoSignalDecision,
    PortfolioName,
)
from apex.domain.research_evidence import (
    CaptureStatus,
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    ReproducibilityStatus,
    ResearchHypothesisVersion,
)
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()

crypto_hypothesis_specification = sa.Table(
    "crypto_hypothesis_specification",
    metadata,
    sa.Column("hypothesis_version_id", sa.String(160), primary_key=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
crypto_historical_capture = sa.Table(
    "crypto_historical_capture",
    metadata,
    sa.Column("capture_id", sa.String(160), primary_key=True),
    sa.Column("evidence_class", sa.String(40), nullable=False),
    sa.Column("status", sa.String(48), nullable=False),
    sa.Column("state_hash", sa.String(64), nullable=False),
    sa.Column("request_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
crypto_capture_attempt = sa.Table(
    "crypto_capture_attempt",
    metadata,
    sa.Column("attempt_id", sa.String(160), primary_key=True),
    sa.Column("capture_id", sa.String(160), nullable=False),
    sa.Column("request_ordinal", sa.Integer(), nullable=False),
    sa.Column("partition_ordinal", sa.Integer(), nullable=False),
    sa.Column("raw_payload", sa.LargeBinary()),
    sa.Column("raw_response_hash", sa.String(64)),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
)
crypto_daily_candle = sa.Table(
    "crypto_daily_candle",
    metadata,
    sa.Column("normalized_hash", sa.String(64), primary_key=True),
    sa.Column("capture_id", sa.String(160), nullable=False),
    sa.Column("attempt_id", sa.String(160), nullable=False),
    sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
crypto_capture_checkpoint = sa.Table(
    "crypto_capture_checkpoint",
    metadata,
    sa.Column("checkpoint_id", sa.String(160), primary_key=True),
    sa.Column("capture_id", sa.String(160), nullable=False),
    sa.Column("next_partition_ordinal", sa.Integer(), nullable=False),
    sa.Column("state_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
)
crypto_historical_snapshot = sa.Table(
    "crypto_historical_snapshot",
    metadata,
    sa.Column("snapshot_id", sa.String(160), primary_key=True),
    sa.Column("capture_id", sa.String(160), nullable=False),
    sa.Column("manifest_hash", sa.String(64), nullable=False),
    sa.Column("evidence_class", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
crypto_experiment_run = sa.Table(
    "crypto_experiment_run",
    metadata,
    sa.Column("run_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
    sa.Column("snapshot_id", sa.String(160), nullable=False),
    sa.Column("result_hash", sa.String(64), nullable=False),
    sa.Column("configuration_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
crypto_experiment_outcome = sa.Table(
    "crypto_experiment_outcome",
    metadata,
    sa.Column("outcome_id", sa.String(160), primary_key=True),
    sa.Column("run_id", sa.String(160), nullable=False),
    sa.Column("decision", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("paper_eligible", sa.Boolean(), nullable=False),
    sa.Column("live_eligible", sa.Boolean(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise CryptoResearchConflict("CRYPTO_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise CryptoResearchConflict("CRYPTO_RETAINED_TIMESTAMP_INVALID")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _times(value: object) -> tuple[datetime, ...]:
    if not isinstance(value, list):
        raise CryptoResearchConflict("CRYPTO_RETAINED_TIMESTAMPS_INVALID")
    return tuple(_time(item) for item in value)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CryptoResearchConflict("CRYPTO_RETAINED_STRINGS_INVALID")
    return tuple(str(item) for item in value)


def _capture(row: RowMapping) -> CryptoHistoricalCapture:
    value = _data(row)
    return CryptoHistoricalCapture(
        capture_id=str(value["capture_id"]),
        product_id=str(value["product_id"]),
        start=_time(value["start"]),
        end=_time(value["end"]),
        granularity=str(value["granularity"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        provider=str(value["provider"]),
        source_contract_version=str(value["source_contract_version"]),
        parser_version=str(value["parser_version"]),
        quality_policy_version=str(value["quality_policy_version"]),
        status=CaptureStatus(value["status"]),
        requested_at=_time(value["requested_at"]),
        captured_at=_optional_time(value["captured_at"]),
        completed_at=_optional_time(value["completed_at"]),
        expected_partitions=int(value["expected_partitions"]),
        completed_partitions=int(value["completed_partitions"]),
        request_count=int(value["request_count"]),
        expected_buckets=int(value["expected_buckets"]),
        accepted_buckets=int(value["accepted_buckets"]),
        missing_bucket_starts=_times(value["missing_bucket_starts"]),
        rejected_records=int(value["rejected_records"]),
        duplicate_records=int(value["duplicate_records"]),
        total_raw_bytes=int(value["total_raw_bytes"]),
        total_runtime_milliseconds=int(value["total_runtime_milliseconds"]),
        quality=CryptoCaptureQuality(value["quality"]),
        limitations=_strings(value["limitations"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        request_hash=str(value["request_hash"]),
        state_hash=str(value["state_hash"]),
        authority="NONE",
    )


def _attempt(row: RowMapping) -> CryptoRequestAttempt:
    value = _data(row)
    return CryptoRequestAttempt(
        attempt_id=str(value["attempt_id"]),
        capture_id=str(value["capture_id"]),
        request_ordinal=int(value["request_ordinal"]),
        partition_ordinal=int(value["partition_ordinal"]),
        partition_start=_time(value["partition_start"]),
        partition_end=_time(value["partition_end"]),
        requested_at=_time(value["requested_at"]),
        completed_at=_time(value["completed_at"]),
        status=CryptoAttemptStatus(value["status"]),
        response_status=(
            int(value["response_status"]) if value["response_status"] is not None else None
        ),
        raw_response_hash=(
            str(value["raw_response_hash"]) if value["raw_response_hash"] is not None else None
        ),
        raw_bytes=int(value["raw_bytes"]),
        accepted_buckets=int(value["accepted_buckets"]),
        missing_bucket_starts=_times(value["missing_bucket_starts"]),
        rejected_records=int(value["rejected_records"]),
        duplicate_records=int(value["duplicate_records"]),
        runtime_milliseconds=int(value["runtime_milliseconds"]),
        source_locator=str(value["source_locator"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        canonical_hash=str(value["canonical_hash"]),
    )


def _candle(row: RowMapping) -> CryptoDailyCandle:
    value = _data(row)
    return CryptoDailyCandle(
        candle_id=str(value["candle_id"]),
        capture_id=str(value["capture_id"]),
        attempt_id=str(value["attempt_id"]),
        product_id=str(value["product_id"]),
        bucket_start=_time(value["bucket_start"]),
        open=Decimal(str(value["open"])),
        high=Decimal(str(value["high"])),
        low=Decimal(str(value["low"])),
        close=Decimal(str(value["close"])),
        volume=Decimal(str(value["volume"])),
        provider=str(value["provider"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        ingested_at=_time(value["ingested_at"]),
        raw_response_hash=str(value["raw_response_hash"]),
        normalized_hash=str(value["normalized_hash"]),
        authority="NONE",
    )


def _checkpoint(row: RowMapping) -> CryptoCaptureCheckpoint:
    value = _data(row)
    return CryptoCaptureCheckpoint(
        checkpoint_id=str(value["checkpoint_id"]),
        capture_id=str(value["capture_id"]),
        completed_partitions=int(value["completed_partitions"]),
        next_partition_ordinal=int(value["next_partition_ordinal"]),
        request_count=int(value["request_count"]),
        accepted_buckets=int(value["accepted_buckets"]),
        state_hash=str(value["state_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _specification(row: RowMapping) -> CryptoHypothesisSpecification:
    value = _data(row)
    return CryptoHypothesisSpecification(
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        product_id=str(value["product_id"]),
        granularity=str(value["granularity"]),
        data_start=_time(value["data_start"]),
        data_end=_time(value["data_end"]),
        validation_start=_time(value["validation_start"]),
        validation_end=_time(value["validation_end"]),
        fast_sma_days=int(value["fast_sma_days"]),
        slow_sma_days=int(value["slow_sma_days"]),
        baseline=str(value["baseline"]),
        conservative_fee_bps=Decimal(str(value["conservative_fee_bps"])),
        conservative_slippage_bps=Decimal(str(value["conservative_slippage_bps"])),
        minimum_net_return=Decimal(str(value["minimum_net_return"])),
        requires_lower_drawdown=bool(value["requires_lower_drawdown"]),
        requires_completed_round_trip=bool(value["requires_completed_round_trip"]),
        paper_eligible=False,
        live_eligible=False,
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _snapshot(row: RowMapping) -> CryptoHistoricalSnapshot:
    value = _data(row)
    return CryptoHistoricalSnapshot(
        snapshot_id=str(value["snapshot_id"]),
        capture_id=str(value["capture_id"]),
        product_id=str(value["product_id"]),
        start=_time(value["start"]),
        end=_time(value["end"]),
        earliest_bucket=_optional_time(value["earliest_bucket"]),
        latest_bucket=_optional_time(value["latest_bucket"]),
        expected_buckets=int(value["expected_buckets"]),
        accepted_buckets=int(value["accepted_buckets"]),
        missing_bucket_starts=_times(value["missing_bucket_starts"]),
        rejected_records=int(value["rejected_records"]),
        duplicate_records=int(value["duplicate_records"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        provider_contract_version=str(value["provider_contract_version"]),
        parser_version=str(value["parser_version"]),
        quality_policy_version=str(value["quality_policy_version"]),
        candle_hashes=_strings(value["candle_hashes"]),
        response_hashes=_strings(value["response_hashes"]),
        quality=CryptoCaptureQuality(value["quality"]),
        manifest_hash=str(value["manifest_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _signal(value: dict[str, Any]) -> CryptoSignalDecision:
    return CryptoSignalDecision(
        signal_for=_time(value["signal_for"]),
        information_through=_time(value["information_through"]),
        fast_average=Decimal(str(value["fast_average"])),
        slow_average=Decimal(str(value["slow_average"])),
        desired_long=bool(value["desired_long"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _metric(value: dict[str, Any]) -> CryptoPerformanceMetrics:
    return CryptoPerformanceMetrics(
        portfolio=PortfolioName(value["portfolio"]),
        scenario=CostScenarioName(value["scenario"]),
        validation_start=_time(value["validation_start"]),
        validation_end=_time(value["validation_end"]),
        starting_notional=Decimal(str(value["starting_notional"])),
        ending_notional=Decimal(str(value["ending_notional"])),
        gross_return=Decimal(str(value["gross_return"])),
        net_return=Decimal(str(value["net_return"])),
        maximum_drawdown=Decimal(str(value["maximum_drawdown"])),
        realized_volatility=Decimal(str(value["realized_volatility"])),
        entries=int(value["entries"]),
        exits=int(value["exits"]),
        completed_round_trips=int(value["completed_round_trips"]),
        total_turnover=Decimal(str(value["total_turnover"])),
        exposure_percentage=Decimal(str(value["exposure_percentage"])),
        cost_paid=Decimal(str(value["cost_paid"])),
        missing_or_excluded_buckets=int(value["missing_or_excluded_buckets"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _configuration(value: object) -> tuple[tuple[str, object], ...]:
    if not isinstance(value, list):
        raise CryptoResearchConflict("CRYPTO_RETAINED_CONFIGURATION_INVALID")
    result: list[tuple[str, object]] = []
    time_keys = {"data_start", "data_end", "validation_start", "validation_end"}
    decimal_keys = {
        "conservative_fee_bps_per_side",
        "conservative_slippage_bps_per_side",
    }
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise CryptoResearchConflict("CRYPTO_RETAINED_CONFIGURATION_INVALID")
        key = str(pair[0])
        item: object = pair[1]
        if key in time_keys:
            item = _time(item)
        elif key in decimal_keys:
            item = Decimal(str(item))
        result.append((key, item))
    return tuple(result)


def _run(row: RowMapping) -> CryptoExperimentRun:
    value = _data(row)
    raw_signals = value["signals"]
    raw_metrics = value["metrics"]
    if not isinstance(raw_signals, list) or not isinstance(raw_metrics, list):
        raise CryptoResearchConflict("CRYPTO_RETAINED_RUN_COLLECTION_INVALID")
    return CryptoExperimentRun(
        run_id=str(value["run_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        snapshot_id=str(value["snapshot_id"]),
        executor_version=str(value["executor_version"]),
        configuration=_configuration(value["configuration"]),
        configuration_hash=str(value["configuration_hash"]),
        feature_definition_version=str(value["feature_definition_version"]),
        cost_model_version=str(value["cost_model_version"]),
        validation_policy_version=str(value["validation_policy_version"]),
        code_revision=str(value["code_revision"]),
        deterministic_seed=int(value["deterministic_seed"]),
        executed_at=_time(value["executed_at"]),
        state=ExperimentState(value["state"]),
        complete=bool(value["complete"]),
        reproducibility_status=ReproducibilityStatus(value["reproducibility_status"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        signals=tuple(_signal(item) for item in raw_signals),
        metrics=tuple(_metric(item) for item in raw_metrics),
        warnings=_strings(value["warnings"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        result_hash=str(value["result_hash"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _outcome(row: RowMapping) -> CryptoExperimentOutcome:
    value = _data(row)
    return CryptoExperimentOutcome(
        outcome_id=str(value["outcome_id"]),
        run_id=str(value["run_id"]),
        decision=ExperimentOutcomeDecision(value["decision"]),
        decided_at=_time(value["decided_at"]),
        actor=str(value["actor"]),
        reason=str(value["reason"]),
        evidence_hashes=_strings(value["evidence_hashes"]),
        threshold_rules=_strings(value["threshold_rules"]),
        warnings=_strings(value["warnings"]),
        paper_eligible=False,
        live_eligible=False,
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


class PostgresCryptoResearchRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._research = PostgresResearchEvidenceRepository(engine)

    def hypothesis_version(self, version_id: str) -> ResearchHypothesisVersion | None:
        return self._research.hypothesis_version(version_id)

    def hypothesis_revision(
        self, hypothesis_id: str, revision: int
    ) -> ResearchHypothesisVersion | None:
        return self._research.hypothesis_revision(hypothesis_id, revision)

    def hypothesis_specification(self, version_id: str) -> CryptoHypothesisSpecification | None:
        row = self._one(
            sa.select(crypto_hypothesis_specification).where(
                crypto_hypothesis_specification.c.hypothesis_version_id == version_id
            )
        )
        return _specification(row) if row is not None else None

    def save_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: CryptoHypothesisSpecification,
    ) -> CryptoHypothesisSpecification:
        self._research.save_hypothesis(hypothesis)
        self._insert(
            crypto_hypothesis_specification,
            {
                "hypothesis_version_id": hypothesis.hypothesis_version_id,
                "payload": _payload(specification),
                "canonical_hash": specification.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_HYPOTHESIS_PERSISTENCE_CONFLICT",
        )
        return specification

    def hypotheses(
        self,
    ) -> tuple[tuple[ResearchHypothesisVersion, CryptoHypothesisSpecification], ...]:
        rows = self._rows(
            sa.select(crypto_hypothesis_specification).order_by(
                crypto_hypothesis_specification.c.hypothesis_version_id
            )
        )
        result = []
        for row in rows:
            specification = _specification(row)
            hypothesis = self.hypothesis_version(specification.hypothesis_version_id)
            if hypothesis is None:
                raise CryptoResearchConflict("CRYPTO_HYPOTHESIS_LINEAGE_MISSING")
            result.append((hypothesis, specification))
        return tuple(result)

    def capture(self, capture_id: str) -> CryptoHistoricalCapture | None:
        row = self._one(
            sa.select(crypto_historical_capture).where(
                crypto_historical_capture.c.capture_id == capture_id
            )
        )
        return _capture(row) if row is not None else None

    def save_capture(self, capture: CryptoHistoricalCapture) -> CryptoHistoricalCapture:
        self._insert(
            crypto_historical_capture,
            {
                "capture_id": capture.capture_id,
                "evidence_class": capture.evidence_class.value,
                "status": capture.status.value,
                "state_hash": capture.state_hash,
                "request_hash": capture.request_hash,
                "payload": _payload(capture),
                "authority": "NONE",
            },
            "CRYPTO_CAPTURE_PERSISTENCE_CONFLICT",
        )
        return capture

    def append_attempt(
        self,
        *,
        expected_state_hash: str,
        capture: CryptoHistoricalCapture,
        attempt: CryptoRequestAttempt,
        raw_payload: bytes | None,
        candles: tuple[CryptoDailyCandle, ...],
        checkpoint: CryptoCaptureCheckpoint | None,
    ) -> CryptoHistoricalCapture:
        try:
            with self._engine.begin() as connection:
                current = connection.execute(
                    sa.select(crypto_historical_capture.c.state_hash)
                    .where(crypto_historical_capture.c.capture_id == capture.capture_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if current != expected_state_hash:
                    raise CryptoResearchConflict("CRYPTO_CAPTURE_CHECKPOINT_CONFLICT")
                connection.execute(
                    crypto_capture_attempt.insert().values(
                        attempt_id=attempt.attempt_id,
                        capture_id=attempt.capture_id,
                        request_ordinal=attempt.request_ordinal,
                        partition_ordinal=attempt.partition_ordinal,
                        raw_payload=raw_payload,
                        raw_response_hash=attempt.raw_response_hash,
                        payload=_payload(attempt),
                        canonical_hash=attempt.canonical_hash,
                    )
                )
                for candle in candles:
                    connection.execute(
                        crypto_daily_candle.insert().values(
                            normalized_hash=candle.normalized_hash,
                            capture_id=candle.capture_id,
                            attempt_id=candle.attempt_id,
                            bucket_start=candle.bucket_start,
                            payload=_payload(candle),
                            authority="NONE",
                        )
                    )
                if checkpoint is not None:
                    connection.execute(
                        crypto_capture_checkpoint.insert().values(
                            checkpoint_id=checkpoint.checkpoint_id,
                            capture_id=checkpoint.capture_id,
                            next_partition_ordinal=checkpoint.next_partition_ordinal,
                            state_hash=checkpoint.state_hash,
                            payload=_payload(checkpoint),
                            canonical_hash=checkpoint.canonical_hash,
                        )
                    )
                connection.execute(
                    crypto_historical_capture.update()
                    .where(
                        crypto_historical_capture.c.capture_id == capture.capture_id,
                        crypto_historical_capture.c.state_hash == expected_state_hash,
                    )
                    .values(
                        evidence_class=capture.evidence_class.value,
                        status=capture.status.value,
                        state_hash=capture.state_hash,
                        request_hash=capture.request_hash,
                        payload=_payload(capture),
                    )
                )
        except IntegrityError as error:
            raise CryptoResearchConflict("CRYPTO_CAPTURE_APPEND_CONFLICT") from error
        return capture

    def captures(self) -> tuple[CryptoHistoricalCapture, ...]:
        return tuple(
            _capture(row)
            for row in self._rows(
                sa.select(crypto_historical_capture).order_by(
                    crypto_historical_capture.c.capture_id
                )
            )
        )

    def attempts(self, capture_id: str) -> tuple[CryptoRequestAttempt, ...]:
        return tuple(
            _attempt(row)
            for row in self._rows(
                sa.select(crypto_capture_attempt)
                .where(crypto_capture_attempt.c.capture_id == capture_id)
                .order_by(crypto_capture_attempt.c.request_ordinal)
            )
        )

    def raw_response(self, attempt_id: str) -> bytes | None:
        row = self._one(
            sa.select(crypto_capture_attempt.c.raw_payload).where(
                crypto_capture_attempt.c.attempt_id == attempt_id
            )
        )
        return None if row is None else row["raw_payload"]

    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]:
        return tuple(
            _candle(row)
            for row in self._rows(
                sa.select(crypto_daily_candle)
                .where(crypto_daily_candle.c.capture_id == capture_id)
                .order_by(crypto_daily_candle.c.bucket_start)
            )
        )

    def checkpoints(self, capture_id: str) -> tuple[CryptoCaptureCheckpoint, ...]:
        return tuple(
            _checkpoint(row)
            for row in self._rows(
                sa.select(crypto_capture_checkpoint)
                .where(crypto_capture_checkpoint.c.capture_id == capture_id)
                .order_by(crypto_capture_checkpoint.c.next_partition_ordinal)
            )
        )

    def snapshot(self, snapshot_id: str) -> CryptoHistoricalSnapshot | None:
        row = self._one(
            sa.select(crypto_historical_snapshot).where(
                crypto_historical_snapshot.c.snapshot_id == snapshot_id
            )
        )
        return _snapshot(row) if row is not None else None

    def save_snapshot(self, snapshot: CryptoHistoricalSnapshot) -> CryptoHistoricalSnapshot:
        self._insert(
            crypto_historical_snapshot,
            {
                "snapshot_id": snapshot.snapshot_id,
                "capture_id": snapshot.capture_id,
                "manifest_hash": snapshot.manifest_hash,
                "evidence_class": snapshot.evidence_class.value,
                "payload": _payload(snapshot),
                "canonical_hash": snapshot.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_SNAPSHOT_PERSISTENCE_CONFLICT",
        )
        return snapshot

    def snapshots(self) -> tuple[CryptoHistoricalSnapshot, ...]:
        return tuple(
            _snapshot(row)
            for row in self._rows(
                sa.select(crypto_historical_snapshot).order_by(
                    crypto_historical_snapshot.c.snapshot_id
                )
            )
        )

    def run(self, run_id: str) -> CryptoExperimentRun | None:
        row = self._one(
            sa.select(crypto_experiment_run).where(crypto_experiment_run.c.run_id == run_id)
        )
        return _run(row) if row is not None else None

    def save_run(self, run: CryptoExperimentRun) -> CryptoExperimentRun:
        self._insert(
            crypto_experiment_run,
            {
                "run_id": run.run_id,
                "hypothesis_version_id": run.hypothesis_version_id,
                "snapshot_id": run.snapshot_id,
                "result_hash": run.result_hash,
                "configuration_hash": run.configuration_hash,
                "payload": _payload(run),
                "canonical_hash": run.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_RUN_PERSISTENCE_CONFLICT",
        )
        return run

    def runs(self) -> tuple[CryptoExperimentRun, ...]:
        return tuple(
            _run(row)
            for row in self._rows(
                sa.select(crypto_experiment_run).order_by(crypto_experiment_run.c.run_id)
            )
        )

    def outcome(self, outcome_id: str) -> CryptoExperimentOutcome | None:
        row = self._one(
            sa.select(crypto_experiment_outcome).where(
                crypto_experiment_outcome.c.outcome_id == outcome_id
            )
        )
        return _outcome(row) if row is not None else None

    def outcome_for_run(self, run_id: str) -> CryptoExperimentOutcome | None:
        row = self._one(
            sa.select(crypto_experiment_outcome).where(crypto_experiment_outcome.c.run_id == run_id)
        )
        return _outcome(row) if row is not None else None

    def save_outcome(self, outcome: CryptoExperimentOutcome) -> CryptoExperimentOutcome:
        self._insert(
            crypto_experiment_outcome,
            {
                "outcome_id": outcome.outcome_id,
                "run_id": outcome.run_id,
                "decision": outcome.decision.value,
                "payload": _payload(outcome),
                "canonical_hash": outcome.canonical_hash,
                "paper_eligible": False,
                "live_eligible": False,
                "authority": "NONE",
            },
            "CRYPTO_OUTCOME_PERSISTENCE_CONFLICT",
        )
        return outcome

    def outcomes(self) -> tuple[CryptoExperimentOutcome, ...]:
        return tuple(
            _outcome(row)
            for row in self._rows(
                sa.select(crypto_experiment_outcome).order_by(
                    crypto_experiment_outcome.c.outcome_id
                )
            )
        )

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def _rows(self, statement: sa.Select[Any]) -> tuple[RowMapping, ...]:
        with self._engine.connect() as connection:
            return tuple(connection.execute(statement).mappings())

    def _insert(self, table: sa.Table, values: dict[str, object], code: str) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(table.insert().values(**values))
        except IntegrityError as error:
            raise CryptoResearchConflict(code) from error


__all__ = [
    "PostgresCryptoResearchRepository",
    "crypto_capture_attempt",
    "crypto_capture_checkpoint",
    "crypto_daily_candle",
    "crypto_experiment_outcome",
    "crypto_experiment_run",
    "crypto_historical_capture",
    "crypto_historical_snapshot",
    "crypto_hypothesis_specification",
]
