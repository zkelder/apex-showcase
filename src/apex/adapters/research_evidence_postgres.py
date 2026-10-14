"""PostgreSQL repository for the bounded durable research evidence lifecycle."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.application.research_evidence import ResearchEvidenceConflict
from apex.domain.research_evidence import (
    CaptureCheckpoint,
    CaptureStatus,
    CompletenessStatus,
    DatasetMemberDecision,
    EvidenceClass,
    ExperimentMetric,
    ExperimentOutcomeDecision,
    ExperimentResultArtifact,
    ExperimentState,
    HypothesisStatus,
    ObservationCapture,
    QualityStatus,
    ReproducibilityStatus,
    ResearchDatasetSnapshot,
    ResearchExperimentOutcome,
    ResearchExperimentRun,
    ResearchHypothesisVersion,
    RetainedObservation,
    StalenessStatus,
)
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()

research_hypothesis_version = sa.Table(
    "research_hypothesis_version",
    metadata,
    sa.Column("hypothesis_version_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_id", sa.String(160), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("supersedes_version_id", sa.String(160)),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
research_observation_capture = sa.Table(
    "research_observation_capture",
    metadata,
    sa.Column("capture_id", sa.String(160), primary_key=True),
    sa.Column("provider", sa.String(80), nullable=False),
    sa.Column("source_contract_version", sa.String(160), nullable=False),
    sa.Column("parser_version", sa.String(160), nullable=False),
    sa.Column("evidence_class", sa.String(40), nullable=False),
    sa.Column("source_locator", sa.String(500), nullable=False),
    sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("observation_at", sa.DateTime(timezone=True)),
    sa.Column("ingestion_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.String(48), nullable=False),
    sa.Column("raw_payload", sa.LargeBinary()),
    sa.Column("raw_sha256", sa.String(64)),
    sa.Column("byte_count", sa.Integer(), nullable=False),
    sa.Column("expected_observations", sa.Integer(), nullable=False),
    sa.Column("retained_observations", sa.Integer(), nullable=False),
    sa.Column("next_ordinal", sa.Integer(), nullable=False),
    sa.Column("quality_status", sa.String(32), nullable=False),
    sa.Column("completeness_status", sa.String(32), nullable=False),
    sa.Column("staleness_status", sa.String(32), nullable=False),
    sa.Column("limitations", sa.ARRAY(sa.Text()), nullable=False),
    sa.Column("failure_reason", sa.Text()),
    sa.Column("request_hash", sa.String(64), nullable=False),
    sa.Column("state_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
research_observation = sa.Table(
    "research_observation",
    metadata,
    sa.Column("observation_id", sa.String(160), primary_key=True),
    sa.Column("capture_id", sa.String(160), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("subject_id", sa.String(160), nullable=False),
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("raw_sha256", sa.String(64), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
research_capture_checkpoint = sa.Table(
    "research_capture_checkpoint",
    metadata,
    sa.Column("checkpoint_id", sa.String(160), primary_key=True),
    sa.Column("capture_id", sa.String(160), nullable=False),
    sa.Column("next_ordinal", sa.Integer(), nullable=False),
    sa.Column("retained_observations", sa.Integer(), nullable=False),
    sa.Column("raw_sha256", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
)
research_dataset_snapshot = sa.Table(
    "research_dataset_snapshot",
    metadata,
    sa.Column("snapshot_id", sa.String(160), primary_key=True),
    sa.Column("evidence_class", sa.String(40), nullable=False),
    sa.Column("component_capture_ids", sa.ARRAY(sa.String(160)), nullable=False),
    sa.Column("universe_id", sa.String(160), nullable=False),
    sa.Column("universe_policy_id", sa.String(160), nullable=False),
    sa.Column("manifest_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
research_experiment_run = sa.Table(
    "research_experiment_run",
    metadata,
    sa.Column("run_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
    sa.Column("snapshot_id", sa.String(160), nullable=False),
    sa.Column("executor_version", sa.String(160), nullable=False),
    sa.Column("configuration_hash", sa.String(64), nullable=False),
    sa.Column("code_revision", sa.String(160), nullable=False),
    sa.Column("state", sa.String(32), nullable=False),
    sa.Column("evidence_class", sa.String(40), nullable=False),
    sa.Column("reproducibility_status", sa.String(40), nullable=False),
    sa.Column("result_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
research_experiment_outcome = sa.Table(
    "research_experiment_outcome",
    metadata,
    sa.Column("outcome_id", sa.String(160), primary_key=True),
    sa.Column("run_id", sa.String(160), nullable=False),
    sa.Column("decision", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("paper_eligible", sa.Boolean(), nullable=False),
    sa.Column("live_eligible", sa.Boolean(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise ResearchEvidenceConflict("RETAINED_RESEARCH_PAYLOAD_INVALID")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ResearchEvidenceConflict("RETAINED_RESEARCH_TIMESTAMP_INVALID")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pairs(value: object) -> tuple[tuple[str, object], ...]:
    if not isinstance(value, list):
        raise ResearchEvidenceConflict("RETAINED_RESEARCH_PAIRS_INVALID")
    pairs: list[tuple[str, object]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
            raise ResearchEvidenceConflict("RETAINED_RESEARCH_PAIR_INVALID")
        pair_value = item[1]
        if item[0] == "source_timestamp" and isinstance(pair_value, str):
            pair_value = _timestamp(pair_value)
        if item[0] == "provider_disagreements" and isinstance(pair_value, list):
            pair_value = tuple(str(part) for part in pair_value)
        pairs.append((item[0], pair_value))
    return tuple(pairs)


def _hypothesis(row: RowMapping) -> ResearchHypothesisVersion:
    value = _decoded(row)
    return ResearchHypothesisVersion(
        hypothesis_id=str(value["hypothesis_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        revision=int(value["revision"]),
        question=str(value["question"]),
        motivation=str(value["motivation"]),
        actor=str(value["actor"]),
        market_scope=tuple(str(item) for item in value["market_scope"]),
        created_at=_timestamp(value["created_at"]),
        status=HypothesisStatus(value["status"]),
        supersedes_version_id=(
            str(value["supersedes_version_id"])
            if value["supersedes_version_id"] is not None
            else None
        ),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _capture(row: RowMapping) -> ObservationCapture:
    value = _decoded(row)
    return ObservationCapture(
        capture_id=str(value["capture_id"]),
        provider=str(value["provider"]),
        source_contract_version=str(value["source_contract_version"]),
        parser_version=str(value["parser_version"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        source_locator=str(value["source_locator"]),
        requested_at=_timestamp(value["requested_at"]),
        observation_at=(
            _timestamp(value["observation_at"]) if value["observation_at"] is not None else None
        ),
        ingestion_at=_timestamp(value["ingestion_at"]),
        completed_at=(
            _timestamp(value["completed_at"]) if value["completed_at"] is not None else None
        ),
        status=CaptureStatus(value["status"]),
        raw_sha256=str(value["raw_sha256"]) if value["raw_sha256"] is not None else None,
        byte_count=int(value["byte_count"]),
        expected_observations=int(value["expected_observations"]),
        retained_observations=int(value["retained_observations"]),
        next_ordinal=int(value["next_ordinal"]),
        quality_status=QualityStatus(value["quality_status"]),
        completeness_status=CompletenessStatus(value["completeness_status"]),
        staleness_status=StalenessStatus(value["staleness_status"]),
        limitations=tuple(str(item) for item in value["limitations"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        request_hash=str(value["request_hash"]),
        state_hash=str(value["state_hash"]),
        authority="NONE",
    )


def _observation(row: RowMapping) -> RetainedObservation:
    value = _decoded(row)
    return RetainedObservation(
        observation_id=str(value["observation_id"]),
        capture_id=str(value["capture_id"]),
        ordinal=int(value["ordinal"]),
        provider=str(value["provider"]),
        source_contract_version=str(value["source_contract_version"]),
        parser_version=str(value["parser_version"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        subject_id=str(value["subject_id"]),
        observed_at=_timestamp(value["observed_at"]),
        ingested_at=_timestamp(value["ingested_at"]),
        normalized_values=_pairs(value["normalized_values"]),
        raw_sha256=str(value["raw_sha256"]),
        quality_status=QualityStatus(value["quality_status"]),
        completeness_status=CompletenessStatus(value["completeness_status"]),
        staleness_status=StalenessStatus(value["staleness_status"]),
        limitations=tuple(str(item) for item in value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _checkpoint(row: RowMapping) -> CaptureCheckpoint:
    value = _decoded(row)
    return CaptureCheckpoint(
        checkpoint_id=str(value["checkpoint_id"]),
        capture_id=str(value["capture_id"]),
        next_ordinal=int(value["next_ordinal"]),
        retained_observations=int(value["retained_observations"]),
        raw_sha256=str(value["raw_sha256"]),
        created_at=_timestamp(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _snapshot(row: RowMapping) -> ResearchDatasetSnapshot:
    value = _decoded(row)
    return ResearchDatasetSnapshot(
        snapshot_id=str(value["snapshot_id"]),
        component_capture_ids=tuple(str(item) for item in value["component_capture_ids"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        normalized_security_ids=tuple(str(item) for item in value["normalized_security_ids"]),
        source_receipt_ids=tuple(str(item) for item in value["source_receipt_ids"]),
        source_versions=tuple(str(item) for item in value["source_versions"]),
        parser_versions=tuple(str(item) for item in value["parser_versions"]),
        universe_id=str(value["universe_id"]),
        universe_policy_id=str(value["universe_policy_id"]),
        universe_policy_hash=str(value["universe_policy_hash"]),
        total_considered=int(value["total_considered"]),
        eligible_count=int(value["eligible_count"]),
        decisions=tuple(
            DatasetMemberDecision(
                canonical_security_id=str(item["canonical_security_id"]),
                current_symbol=str(item["current_symbol"]),
                eligible=bool(item["eligible"]),
                reasons=tuple(str(reason) for reason in item["reasons"]),
                execution_capability=str(item["execution_capability"]),
                execution_reasons=tuple(str(reason) for reason in item["execution_reasons"]),
                security_hash=str(item["security_hash"]),
            )
            for item in value["decisions"]
        ),
        quality_status=QualityStatus(value["quality_status"]),
        completeness_status=CompletenessStatus(value["completeness_status"]),
        manifest_hash=str(value["manifest_hash"]),
        created_at=_timestamp(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _artifact(value: dict[str, Any]) -> ExperimentResultArtifact:
    payload = []
    for name, item in _pairs(value["payload"]):
        if name == "decisions" and isinstance(item, list):
            item = tuple(
                DatasetMemberDecision(
                    canonical_security_id=str(decision["canonical_security_id"]),
                    current_symbol=str(decision["current_symbol"]),
                    eligible=bool(decision["eligible"]),
                    reasons=tuple(str(reason) for reason in decision["reasons"]),
                    execution_capability=str(decision["execution_capability"]),
                    execution_reasons=tuple(
                        str(reason) for reason in decision["execution_reasons"]
                    ),
                    security_hash=str(decision["security_hash"]),
                )
                for decision in item
            )
        payload.append((name, item))
    return ExperimentResultArtifact(
        artifact_type=str(value["artifact_type"]),
        content_hash=str(value["content_hash"]),
        payload=tuple(payload),
    )


def _run(row: RowMapping) -> ResearchExperimentRun:
    value = _decoded(row)
    return ResearchExperimentRun(
        run_id=str(value["run_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        snapshot_id=str(value["snapshot_id"]),
        executor_version=str(value["executor_version"]),
        configuration=_pairs(value["configuration"]),
        configuration_hash=str(value["configuration_hash"]),
        feature_policy_versions=tuple(str(item) for item in value["feature_policy_versions"]),
        code_revision=str(value["code_revision"]),
        deterministic_seed=(
            int(value["deterministic_seed"]) if value["deterministic_seed"] is not None else None
        ),
        executed_at=_timestamp(value["executed_at"]),
        state=ExperimentState(value["state"]),
        warnings=tuple(str(item) for item in value["warnings"]),
        metrics=tuple(
            ExperimentMetric(str(item["name"]), str(item["value"])) for item in value["metrics"]
        ),
        artifacts=tuple(_artifact(item) for item in value["artifacts"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        reproducibility_status=ReproducibilityStatus(value["reproducibility_status"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        result_hash=str(value["result_hash"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _outcome(row: RowMapping) -> ResearchExperimentOutcome:
    value = _decoded(row)
    return ResearchExperimentOutcome(
        outcome_id=str(value["outcome_id"]),
        run_id=str(value["run_id"]),
        decision=ExperimentOutcomeDecision(value["decision"]),
        decided_at=_timestamp(value["decided_at"]),
        evidence_hashes=tuple(str(item) for item in value["evidence_hashes"]),
        threshold_rules=tuple(str(item) for item in value["threshold_rules"]),
        warnings=tuple(str(item) for item in value["warnings"]),
        actor=str(value["actor"]),
        reason=str(value["reason"]),
        canonical_hash=str(value["canonical_hash"]),
        paper_eligible=False,
        live_eligible=False,
        authority="NONE",
    )


class PostgresResearchEvidenceRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def hypothesis_version(self, version_id: str) -> ResearchHypothesisVersion | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_hypothesis_version).where(
                        research_hypothesis_version.c.hypothesis_version_id == version_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _hypothesis(row) if row is not None else None

    def hypothesis_revision(
        self, hypothesis_id: str, revision: int
    ) -> ResearchHypothesisVersion | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_hypothesis_version).where(
                        research_hypothesis_version.c.hypothesis_id == hypothesis_id,
                        research_hypothesis_version.c.revision == revision,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _hypothesis(row) if row is not None else None

    def save_hypothesis(self, value: ResearchHypothesisVersion) -> ResearchHypothesisVersion:
        values = {
            "hypothesis_version_id": value.hypothesis_version_id,
            "hypothesis_id": value.hypothesis_id,
            "revision": value.revision,
            "status": value.status.value,
            "supersedes_version_id": value.supersedes_version_id,
            "payload": _payload(value),
            "canonical_hash": value.canonical_hash,
            "created_at": value.created_at,
        }
        self._insert(research_hypothesis_version, values, "HYPOTHESIS_PERSISTENCE_CONFLICT")
        return value

    def hypotheses(self) -> tuple[ResearchHypothesisVersion, ...]:
        return tuple(
            _hypothesis(row)
            for row in self._rows(
                sa.select(research_hypothesis_version).order_by(
                    research_hypothesis_version.c.hypothesis_id,
                    research_hypothesis_version.c.revision,
                )
            )
        )

    def capture(self, capture_id: str) -> ObservationCapture | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_observation_capture).where(
                        research_observation_capture.c.capture_id == capture_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _capture(row) if row is not None else None

    def save_capture(
        self, value: ObservationCapture, raw_payload: bytes | None
    ) -> ObservationCapture:
        values = self._capture_values(value)
        values["raw_payload"] = raw_payload
        self._insert(research_observation_capture, values, "CAPTURE_PERSISTENCE_CONFLICT")
        return value

    def raw_payload(self, capture_id: str) -> bytes | None:
        with self._engine.connect() as connection:
            value = connection.scalar(
                sa.select(research_observation_capture.c.raw_payload).where(
                    research_observation_capture.c.capture_id == capture_id
                )
            )
        return bytes(value) if value is not None else None

    def append_capture_chunk(
        self,
        *,
        expected_state_hash: str,
        capture: ObservationCapture,
        observations: tuple[RetainedObservation, ...],
        checkpoint: CaptureCheckpoint,
    ) -> ObservationCapture:
        try:
            with self._engine.begin() as connection:
                current = connection.execute(
                    sa.select(research_observation_capture.c.state_hash)
                    .where(research_observation_capture.c.capture_id == capture.capture_id)
                    .with_for_update()
                ).scalar_one_or_none()
                if current != expected_state_hash:
                    raise ResearchEvidenceConflict("CAPTURE_CHECKPOINT_CONFLICT")
                for item in observations:
                    connection.execute(
                        research_observation.insert().values(
                            observation_id=item.observation_id,
                            capture_id=item.capture_id,
                            ordinal=item.ordinal,
                            subject_id=item.subject_id,
                            observed_at=item.observed_at,
                            ingested_at=item.ingested_at,
                            payload=_payload(item),
                            raw_sha256=item.raw_sha256,
                            canonical_hash=item.canonical_hash,
                            authority="NONE",
                        )
                    )
                connection.execute(
                    research_capture_checkpoint.insert().values(
                        checkpoint_id=checkpoint.checkpoint_id,
                        capture_id=checkpoint.capture_id,
                        next_ordinal=checkpoint.next_ordinal,
                        retained_observations=checkpoint.retained_observations,
                        raw_sha256=checkpoint.raw_sha256,
                        created_at=checkpoint.created_at,
                        canonical_hash=checkpoint.canonical_hash,
                        payload=_payload(checkpoint),
                    )
                )
                values = self._capture_values(capture)
                values.pop("capture_id")
                result = connection.execute(
                    research_observation_capture.update()
                    .where(
                        research_observation_capture.c.capture_id == capture.capture_id,
                        research_observation_capture.c.state_hash == expected_state_hash,
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise ResearchEvidenceConflict("CAPTURE_CHECKPOINT_CONFLICT")
        except IntegrityError as error:
            raise ResearchEvidenceConflict("CAPTURE_PERSISTENCE_CONFLICT") from error
        return capture

    @staticmethod
    def _capture_values(value: ObservationCapture) -> dict[str, object]:
        return {
            "capture_id": value.capture_id,
            "provider": value.provider,
            "source_contract_version": value.source_contract_version,
            "parser_version": value.parser_version,
            "evidence_class": value.evidence_class.value,
            "source_locator": value.source_locator,
            "requested_at": value.requested_at,
            "observation_at": value.observation_at,
            "ingestion_at": value.ingestion_at,
            "completed_at": value.completed_at,
            "status": value.status.value,
            "raw_sha256": value.raw_sha256,
            "byte_count": value.byte_count,
            "expected_observations": value.expected_observations,
            "retained_observations": value.retained_observations,
            "next_ordinal": value.next_ordinal,
            "quality_status": value.quality_status.value,
            "completeness_status": value.completeness_status.value,
            "staleness_status": value.staleness_status.value,
            "limitations": list(value.limitations),
            "failure_reason": value.failure_reason,
            "request_hash": value.request_hash,
            "state_hash": value.state_hash,
            "authority": "NONE",
            "payload": _payload(value),
        }

    def observations(self, capture_id: str) -> tuple[RetainedObservation, ...]:
        return tuple(
            _observation(row)
            for row in self._rows(
                sa.select(research_observation)
                .where(research_observation.c.capture_id == capture_id)
                .order_by(research_observation.c.ordinal)
            )
        )

    def checkpoints(self, capture_id: str) -> tuple[CaptureCheckpoint, ...]:
        return tuple(
            _checkpoint(row)
            for row in self._rows(
                sa.select(research_capture_checkpoint)
                .where(research_capture_checkpoint.c.capture_id == capture_id)
                .order_by(research_capture_checkpoint.c.next_ordinal)
            )
        )

    def captures(self) -> tuple[ObservationCapture, ...]:
        return tuple(
            _capture(row)
            for row in self._rows(
                sa.select(research_observation_capture).order_by(
                    research_observation_capture.c.capture_id
                )
            )
        )

    def snapshot(self, snapshot_id: str) -> ResearchDatasetSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_dataset_snapshot).where(
                        research_dataset_snapshot.c.snapshot_id == snapshot_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _snapshot(row) if row is not None else None

    def save_snapshot(self, value: ResearchDatasetSnapshot) -> ResearchDatasetSnapshot:
        self._insert(
            research_dataset_snapshot,
            {
                "snapshot_id": value.snapshot_id,
                "evidence_class": value.evidence_class.value,
                "component_capture_ids": list(value.component_capture_ids),
                "universe_id": value.universe_id,
                "universe_policy_id": value.universe_policy_id,
                "manifest_hash": value.manifest_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "created_at": value.created_at,
                "authority": "NONE",
            },
            "SNAPSHOT_PERSISTENCE_CONFLICT",
        )
        return value

    def snapshots(self) -> tuple[ResearchDatasetSnapshot, ...]:
        return tuple(
            _snapshot(row)
            for row in self._rows(
                sa.select(research_dataset_snapshot).order_by(
                    research_dataset_snapshot.c.snapshot_id
                )
            )
        )

    def run(self, run_id: str) -> ResearchExperimentRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_experiment_run).where(
                        research_experiment_run.c.run_id == run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _run(row) if row is not None else None

    def save_run(self, value: ResearchExperimentRun) -> ResearchExperimentRun:
        self._insert(
            research_experiment_run,
            {
                "run_id": value.run_id,
                "hypothesis_version_id": value.hypothesis_version_id,
                "snapshot_id": value.snapshot_id,
                "executor_version": value.executor_version,
                "configuration_hash": value.configuration_hash,
                "code_revision": value.code_revision,
                "state": value.state.value,
                "evidence_class": value.evidence_class.value,
                "reproducibility_status": value.reproducibility_status.value,
                "result_hash": value.result_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "executed_at": value.executed_at,
                "authority": "NONE",
            },
            "EXPERIMENT_RUN_PERSISTENCE_CONFLICT",
        )
        return value

    def runs(self) -> tuple[ResearchExperimentRun, ...]:
        return tuple(
            _run(row)
            for row in self._rows(
                sa.select(research_experiment_run).order_by(research_experiment_run.c.run_id)
            )
        )

    def outcome(self, outcome_id: str) -> ResearchExperimentOutcome | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_experiment_outcome).where(
                        research_experiment_outcome.c.outcome_id == outcome_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _outcome(row) if row is not None else None

    def outcome_for_run(self, run_id: str) -> ResearchExperimentOutcome | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_experiment_outcome).where(
                        research_experiment_outcome.c.run_id == run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _outcome(row) if row is not None else None

    def save_outcome(self, value: ResearchExperimentOutcome) -> ResearchExperimentOutcome:
        self._insert(
            research_experiment_outcome,
            {
                "outcome_id": value.outcome_id,
                "run_id": value.run_id,
                "decision": value.decision.value,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "decided_at": value.decided_at,
                "paper_eligible": False,
                "live_eligible": False,
                "authority": "NONE",
            },
            "OUTCOME_PERSISTENCE_CONFLICT",
        )
        return value

    def outcomes(self) -> tuple[ResearchExperimentOutcome, ...]:
        return tuple(
            _outcome(row)
            for row in self._rows(
                sa.select(research_experiment_outcome).order_by(
                    research_experiment_outcome.c.outcome_id
                )
            )
        )

    def _rows(self, statement: sa.Select[Any]) -> tuple[RowMapping, ...]:
        with self._engine.connect() as connection:
            return tuple(connection.execute(statement).mappings())

    def _insert(self, table: sa.Table, values: dict[str, object], code: str) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(table.insert().values(**values))
        except IntegrityError as error:
            raise ResearchEvidenceConflict(code) from error
