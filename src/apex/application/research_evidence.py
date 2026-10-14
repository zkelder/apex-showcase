"""Application services for durable, reproducible, authority-free research."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Protocol

from apex.domain.options_intelligence import OptionUnderlyingLinkRejection, UnderlyingLinkOutcome
from apex.domain.research_evidence import (
    BROAD_UNIVERSE_ELIGIBILITY_EXPERIMENT_V1,
    RESEARCH_ONLY_WARNING,
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
from apex.domain.security_reference import (
    CURRENT_LISTED_WARNING,
    CurrentEquityUniversePolicy,
    EvidenceState,
    SecurityReference,
    SecurityType,
    create_current_equity_universe_snapshot,
    reconcile_security_references,
)
from apex.domain.values import content_digest

NASDAQ_PROVIDER = "NASDAQ_TRADER"
NASDAQ_SOURCE_CONTRACT = "NASDAQ_TRADER_SYMBOL_DIRECTORY_V1"
NASDAQ_PARSER_VERSION = "APEX_NASDAQ_DIRECTORY_PARSER_V1"
SEC_PROVIDER = "SEC_EDGAR"
SEC_SOURCE_CONTRACT = "SEC_EDGAR_COMPANY_IDENTITY_V1"
SEC_PARSER_VERSION = "APEX_SEC_TICKERS_PARSER_V1"
NASDAQ_OPTIONS_PROVIDER = "NASDAQ_OPTIONS_DIRECTORY"
NASDAQ_OPTIONS_SOURCE_CONTRACT = "NASDAQ_OPTIONS_DIRECTORY_V1"
NASDAQ_OPTIONS_PARSER_VERSION = "APEX_NASDAQ_OPTIONS_DIRECTORY_PARSER_V1"
MAX_CAPTURE_RECORDS = 20_000
MAX_CAPTURE_CHUNK = 1_000


class ResearchEvidenceApplicationError(RuntimeError):
    pass


class ResearchEvidenceConflict(ResearchEvidenceApplicationError):
    pass


class ResearchEvidenceNotFound(ResearchEvidenceApplicationError):
    pass


class ResearchEvidenceRepository(Protocol):
    def hypothesis_version(self, version_id: str) -> ResearchHypothesisVersion | None: ...
    def hypothesis_revision(
        self, hypothesis_id: str, revision: int
    ) -> ResearchHypothesisVersion | None: ...
    def save_hypothesis(self, value: ResearchHypothesisVersion) -> ResearchHypothesisVersion: ...
    def hypotheses(self) -> tuple[ResearchHypothesisVersion, ...]: ...

    def capture(self, capture_id: str) -> ObservationCapture | None: ...
    def save_capture(
        self, value: ObservationCapture, raw_payload: bytes | None
    ) -> ObservationCapture: ...
    def raw_payload(self, capture_id: str) -> bytes | None: ...
    def append_capture_chunk(
        self,
        *,
        expected_state_hash: str,
        capture: ObservationCapture,
        observations: tuple[RetainedObservation, ...],
        checkpoint: CaptureCheckpoint,
    ) -> ObservationCapture: ...
    def observations(self, capture_id: str) -> tuple[RetainedObservation, ...]: ...
    def checkpoints(self, capture_id: str) -> tuple[CaptureCheckpoint, ...]: ...
    def captures(self) -> tuple[ObservationCapture, ...]: ...

    def snapshot(self, snapshot_id: str) -> ResearchDatasetSnapshot | None: ...
    def save_snapshot(self, value: ResearchDatasetSnapshot) -> ResearchDatasetSnapshot: ...
    def snapshots(self) -> tuple[ResearchDatasetSnapshot, ...]: ...

    def run(self, run_id: str) -> ResearchExperimentRun | None: ...
    def save_run(self, value: ResearchExperimentRun) -> ResearchExperimentRun: ...
    def runs(self) -> tuple[ResearchExperimentRun, ...]: ...

    def outcome(self, outcome_id: str) -> ResearchExperimentOutcome | None: ...
    def outcome_for_run(self, run_id: str) -> ResearchExperimentOutcome | None: ...
    def save_outcome(self, value: ResearchExperimentOutcome) -> ResearchExperimentOutcome: ...
    def outcomes(self) -> tuple[ResearchExperimentOutcome, ...]: ...


class InMemoryResearchEvidenceRepository:
    def __init__(self) -> None:
        self._hypotheses: dict[str, ResearchHypothesisVersion] = {}
        self._captures: dict[str, ObservationCapture] = {}
        self._raw_payloads: dict[str, bytes] = {}
        self._observations: dict[str, list[RetainedObservation]] = {}
        self._checkpoints: dict[str, list[CaptureCheckpoint]] = {}
        self._snapshots: dict[str, ResearchDatasetSnapshot] = {}
        self._runs: dict[str, ResearchExperimentRun] = {}
        self._outcomes: dict[str, ResearchExperimentOutcome] = {}

    def hypothesis_version(self, version_id: str) -> ResearchHypothesisVersion | None:
        return self._hypotheses.get(version_id)

    def hypothesis_revision(
        self, hypothesis_id: str, revision: int
    ) -> ResearchHypothesisVersion | None:
        return next(
            (
                item
                for item in self._hypotheses.values()
                if item.hypothesis_id == hypothesis_id and item.revision == revision
            ),
            None,
        )

    def save_hypothesis(self, value: ResearchHypothesisVersion) -> ResearchHypothesisVersion:
        self._hypotheses[value.hypothesis_version_id] = value
        return value

    def hypotheses(self) -> tuple[ResearchHypothesisVersion, ...]:
        return tuple(
            sorted(
                self._hypotheses.values(),
                key=lambda item: (item.hypothesis_id, item.revision),
            )
        )

    def capture(self, capture_id: str) -> ObservationCapture | None:
        return self._captures.get(capture_id)

    def save_capture(
        self, value: ObservationCapture, raw_payload: bytes | None
    ) -> ObservationCapture:
        self._captures[value.capture_id] = value
        if raw_payload is not None:
            self._raw_payloads[value.capture_id] = raw_payload
        return value

    def raw_payload(self, capture_id: str) -> bytes | None:
        return self._raw_payloads.get(capture_id)

    def append_capture_chunk(
        self,
        *,
        expected_state_hash: str,
        capture: ObservationCapture,
        observations: tuple[RetainedObservation, ...],
        checkpoint: CaptureCheckpoint,
    ) -> ObservationCapture:
        prior = self._captures.get(capture.capture_id)
        if prior is None or prior.state_hash != expected_state_hash:
            raise ResearchEvidenceConflict("CAPTURE_CHECKPOINT_CONFLICT")
        existing = self._observations.setdefault(capture.capture_id, [])
        expected = len(existing)
        for item in observations:
            if item.ordinal != expected:
                raise ResearchEvidenceConflict("CAPTURE_OBSERVATION_ORDINAL_CONFLICT")
            existing.append(item)
            expected += 1
        self._checkpoints.setdefault(capture.capture_id, []).append(checkpoint)
        self._captures[capture.capture_id] = capture
        return capture

    def observations(self, capture_id: str) -> tuple[RetainedObservation, ...]:
        return tuple(self._observations.get(capture_id, ()))

    def checkpoints(self, capture_id: str) -> tuple[CaptureCheckpoint, ...]:
        return tuple(self._checkpoints.get(capture_id, ()))

    def captures(self) -> tuple[ObservationCapture, ...]:
        return tuple(sorted(self._captures.values(), key=lambda item: item.capture_id))

    def snapshot(self, snapshot_id: str) -> ResearchDatasetSnapshot | None:
        return self._snapshots.get(snapshot_id)

    def save_snapshot(self, value: ResearchDatasetSnapshot) -> ResearchDatasetSnapshot:
        self._snapshots[value.snapshot_id] = value
        return value

    def snapshots(self) -> tuple[ResearchDatasetSnapshot, ...]:
        return tuple(sorted(self._snapshots.values(), key=lambda item: item.snapshot_id))

    def run(self, run_id: str) -> ResearchExperimentRun | None:
        return self._runs.get(run_id)

    def save_run(self, value: ResearchExperimentRun) -> ResearchExperimentRun:
        self._runs[value.run_id] = value
        return value

    def runs(self) -> tuple[ResearchExperimentRun, ...]:
        return tuple(sorted(self._runs.values(), key=lambda item: item.run_id))

    def outcome(self, outcome_id: str) -> ResearchExperimentOutcome | None:
        return self._outcomes.get(outcome_id)

    def outcome_for_run(self, run_id: str) -> ResearchExperimentOutcome | None:
        return next((item for item in self._outcomes.values() if item.run_id == run_id), None)

    def save_outcome(self, value: ResearchExperimentOutcome) -> ResearchExperimentOutcome:
        self._outcomes[value.outcome_id] = value
        return value

    def outcomes(self) -> tuple[ResearchExperimentOutcome, ...]:
        return tuple(sorted(self._outcomes.values(), key=lambda item: item.outcome_id))


class RegisterResearchHypothesis:
    def __init__(self, repository: ResearchEvidenceRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        hypothesis_id: str,
        revision: int,
        question: str,
        motivation: str,
        actor: str,
        market_scope: tuple[str, ...],
        created_at: datetime,
        status: HypothesisStatus | str = HypothesisStatus.REGISTERED,
        supersedes_version_id: str | None = None,
    ) -> ResearchHypothesisVersion:
        candidate = ResearchHypothesisVersion.create(
            hypothesis_id=hypothesis_id,
            revision=revision,
            question=question,
            motivation=motivation,
            actor=actor,
            market_scope=market_scope,
            created_at=created_at,
            status=status,
            supersedes_version_id=supersedes_version_id,
        )
        prior_revision = self._repository.hypothesis_revision(hypothesis_id, revision)
        if prior_revision is not None:
            if prior_revision.canonical_hash != candidate.canonical_hash:
                raise ResearchEvidenceConflict("HYPOTHESIS_REVISION_CONFLICT")
            return prior_revision
        if revision > 1:
            parent = self._repository.hypothesis_version(supersedes_version_id or "")
            if (
                parent is None
                or parent.hypothesis_id != hypothesis_id
                or parent.revision != revision - 1
            ):
                raise ResearchEvidenceConflict("HYPOTHESIS_PARENT_CONFLICT")
        return self._repository.save_hypothesis(candidate)


class CaptureNasdaqDirectory:
    def __init__(self, repository: ResearchEvidenceRepository) -> None:
        self._repository = repository

    def start(
        self,
        *,
        capture_id: str,
        evidence_class: EvidenceClass | str,
        source_locator: str,
        raw_payload: bytes,
        records: tuple[SecurityReference, ...],
        requested_at: datetime,
        ingested_at: datetime,
        chunk_size: int,
        limitations: tuple[str, ...] = (),
    ) -> ObservationCapture:
        self._validate_chunk_size(chunk_size)
        if not raw_payload or not records or len(records) > MAX_CAPTURE_RECORDS:
            raise ResearchEvidenceApplicationError("CAPTURE_BOUNDS_INVALID")
        evidence = EvidenceClass(evidence_class)
        if evidence not in {EvidenceClass.FIXTURE, EvidenceClass.GENUINE_PUBLIC}:
            raise ResearchEvidenceApplicationError("NASDAQ_EVIDENCE_CLASS_INVALID")
        raw_hash = hashlib.sha256(raw_payload).hexdigest()
        observed_at = max(item.source_timestamp for item in records)
        candidate = ObservationCapture.create(
            capture_id=capture_id,
            provider=NASDAQ_PROVIDER,
            source_contract_version=NASDAQ_SOURCE_CONTRACT,
            parser_version=NASDAQ_PARSER_VERSION,
            evidence_class=evidence,
            source_locator=source_locator,
            requested_at=requested_at,
            observation_at=observed_at,
            ingestion_at=ingested_at,
            completed_at=None,
            status=CaptureStatus.RUNNING,
            raw_sha256=raw_hash,
            byte_count=len(raw_payload),
            expected_observations=len(records),
            retained_observations=0,
            next_ordinal=0,
            quality_status=QualityStatus.VALID,
            completeness_status=CompletenessStatus.PARTIAL,
            staleness_status=StalenessStatus.CURRENT_REFERENCE,
            limitations=limitations,
        )
        prior = self._repository.capture(capture_id)
        if prior is not None:
            if prior.request_hash != candidate.request_hash or prior.raw_sha256 != raw_hash:
                raise ResearchEvidenceConflict("CAPTURE_SOURCE_CONTENT_CONFLICT")
            return self.resume(
                capture_id=capture_id,
                raw_payload=raw_payload,
                records=records,
                resumed_at=ingested_at,
                chunk_size=chunk_size,
            )
        self._repository.save_capture(candidate, raw_payload)
        return self._append(candidate, records, ingested_at, chunk_size)

    def record_unavailable(
        self,
        *,
        capture_id: str,
        source_locator: str,
        requested_at: datetime,
        recorded_at: datetime,
        reason: str,
    ) -> ObservationCapture:
        return self._record_terminal(
            capture_id=capture_id,
            source_locator=source_locator,
            requested_at=requested_at,
            recorded_at=recorded_at,
            status=CaptureStatus.CAPABILITY_UNAVAILABLE,
            quality_status=QualityStatus.UNAVAILABLE,
            reason=reason,
            limitations=("GENUINE_SOURCE_NOT_SUBSTITUTED_WITH_FIXTURE",),
        )

    def record_failed(
        self,
        *,
        capture_id: str,
        source_locator: str,
        requested_at: datetime,
        recorded_at: datetime,
        reason: str,
    ) -> ObservationCapture:
        return self._record_terminal(
            capture_id=capture_id,
            source_locator=source_locator,
            requested_at=requested_at,
            recorded_at=recorded_at,
            status=CaptureStatus.FAILED,
            quality_status=QualityStatus.REJECTED,
            reason=reason,
            limitations=("MALFORMED_INCOMPLETE_STALE_OR_INCOMPATIBLE_SOURCE_REJECTED",),
        )

    def _record_terminal(
        self,
        *,
        capture_id: str,
        source_locator: str,
        requested_at: datetime,
        recorded_at: datetime,
        status: CaptureStatus,
        quality_status: QualityStatus,
        reason: str,
        limitations: tuple[str, ...],
    ) -> ObservationCapture:
        candidate = ObservationCapture.create(
            capture_id=capture_id,
            provider=NASDAQ_PROVIDER,
            source_contract_version=NASDAQ_SOURCE_CONTRACT,
            parser_version=NASDAQ_PARSER_VERSION,
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            source_locator=source_locator,
            requested_at=requested_at,
            observation_at=None,
            ingestion_at=recorded_at,
            completed_at=recorded_at,
            status=status,
            raw_sha256=None,
            byte_count=0,
            expected_observations=0,
            retained_observations=0,
            next_ordinal=0,
            quality_status=quality_status,
            completeness_status=CompletenessStatus.UNAVAILABLE,
            staleness_status=StalenessStatus.UNKNOWN,
            limitations=limitations,
            failure_reason=reason,
        )
        prior = self._repository.capture(capture_id)
        if prior is not None:
            if prior.request_hash != candidate.request_hash:
                raise ResearchEvidenceConflict("CAPTURE_REQUEST_CONFLICT")
            return prior
        return self._repository.save_capture(candidate, None)

    def resume(
        self,
        *,
        capture_id: str,
        raw_payload: bytes,
        records: tuple[SecurityReference, ...],
        resumed_at: datetime,
        chunk_size: int,
    ) -> ObservationCapture:
        self._validate_chunk_size(chunk_size)
        capture = self._repository.capture(capture_id)
        if capture is None:
            raise ResearchEvidenceNotFound("CAPTURE_NOT_FOUND")
        if capture.status is CaptureStatus.COMPLETED:
            if hashlib.sha256(raw_payload).hexdigest() != capture.raw_sha256:
                raise ResearchEvidenceConflict("CAPTURE_SOURCE_CONTENT_CONFLICT")
            return capture
        if capture.status is not CaptureStatus.RUNNING:
            raise ResearchEvidenceConflict("CAPTURE_NOT_RESUMABLE")
        if (
            hashlib.sha256(raw_payload).hexdigest() != capture.raw_sha256
            or len(records) != capture.expected_observations
            or any(item.source_payload_hash != capture.raw_sha256 for item in records)
        ):
            raise ResearchEvidenceConflict("CAPTURE_SOURCE_CONTENT_CONFLICT")
        retained = self._repository.observations(capture_id)
        if retained:
            retained_receipt = dict(retained[0].normalized_values)["source_receipt_id"]
            if any(item.source_receipt_id != retained_receipt for item in records):
                raise ResearchEvidenceConflict("CAPTURE_SOURCE_RECEIPT_CONFLICT")
        return self._append(capture, records, resumed_at, chunk_size)

    @staticmethod
    def _validate_chunk_size(chunk_size: int) -> None:
        if not 1 <= chunk_size <= MAX_CAPTURE_CHUNK:
            raise ResearchEvidenceApplicationError("CAPTURE_CHUNK_SIZE_INVALID")

    def _append(
        self,
        capture: ObservationCapture,
        records: tuple[SecurityReference, ...],
        now: datetime,
        chunk_size: int,
    ) -> ObservationCapture:
        start = capture.next_ordinal
        selected = records[start : start + chunk_size]
        observations = tuple(
            RetainedObservation.create(
                capture=capture,
                ordinal=ordinal,
                subject_id=record.canonical_security_id,
                observed_at=record.source_timestamp,
                ingested_at=now,
                normalized_values=_normalized_security_values(record),
                limitations=record.limitations,
            )
            for ordinal, record in enumerate(selected, start=start)
        )
        next_ordinal = start + len(observations)
        completed = next_ordinal == capture.expected_observations
        replacement = ObservationCapture.create(
            capture_id=capture.capture_id,
            provider=capture.provider,
            source_contract_version=capture.source_contract_version,
            parser_version=capture.parser_version,
            evidence_class=capture.evidence_class,
            source_locator=capture.source_locator,
            requested_at=capture.requested_at,
            observation_at=capture.observation_at,
            ingestion_at=capture.ingestion_at,
            completed_at=now if completed else None,
            status=CaptureStatus.COMPLETED if completed else CaptureStatus.RUNNING,
            raw_sha256=capture.raw_sha256,
            byte_count=capture.byte_count,
            expected_observations=capture.expected_observations,
            retained_observations=next_ordinal,
            next_ordinal=next_ordinal,
            quality_status=QualityStatus.VALID,
            completeness_status=(
                CompletenessStatus.COMPLETE if completed else CompletenessStatus.PARTIAL
            ),
            staleness_status=capture.staleness_status,
            limitations=capture.limitations,
        )
        checkpoint = CaptureCheckpoint.create(
            capture_id=capture.capture_id,
            next_ordinal=next_ordinal,
            retained_observations=next_ordinal,
            raw_sha256=capture.raw_sha256 or "",
            created_at=now,
        )
        return self._repository.append_capture_chunk(
            expected_state_hash=capture.state_hash,
            capture=replacement,
            observations=observations,
            checkpoint=checkpoint,
        )


class CaptureSecCompanyTickers:
    """SEC EDGAR company-tickers capture over the same generic lifecycle.

    This is a second bounded official source retained through the identical
    ``ObservationCapture``/``RetainedObservation``/``CaptureCheckpoint``
    machinery ``CaptureNasdaqDirectory`` uses; only the provider identity and
    parser differ.
    """

    def __init__(self, repository: ResearchEvidenceRepository) -> None:
        self._repository = repository

    def start(
        self,
        *,
        capture_id: str,
        evidence_class: EvidenceClass | str,
        source_locator: str,
        raw_payload: bytes,
        records: tuple[SecurityReference, ...],
        requested_at: datetime,
        ingested_at: datetime,
        chunk_size: int,
        limitations: tuple[str, ...] = (),
    ) -> ObservationCapture:
        self._validate_chunk_size(chunk_size)
        if not raw_payload or not records or len(records) > MAX_CAPTURE_RECORDS:
            raise ResearchEvidenceApplicationError("CAPTURE_BOUNDS_INVALID")
        evidence = EvidenceClass(evidence_class)
        if evidence not in {EvidenceClass.FIXTURE, EvidenceClass.GENUINE_PUBLIC}:
            raise ResearchEvidenceApplicationError("SEC_EVIDENCE_CLASS_INVALID")
        raw_hash = hashlib.sha256(raw_payload).hexdigest()
        observed_at = max(item.source_timestamp for item in records)
        candidate = ObservationCapture.create(
            capture_id=capture_id,
            provider=SEC_PROVIDER,
            source_contract_version=SEC_SOURCE_CONTRACT,
            parser_version=SEC_PARSER_VERSION,
            evidence_class=evidence,
            source_locator=source_locator,
            requested_at=requested_at,
            observation_at=observed_at,
            ingestion_at=ingested_at,
            completed_at=None,
            status=CaptureStatus.RUNNING,
            raw_sha256=raw_hash,
            byte_count=len(raw_payload),
            expected_observations=len(records),
            retained_observations=0,
            next_ordinal=0,
            quality_status=QualityStatus.VALID,
            completeness_status=CompletenessStatus.PARTIAL,
            staleness_status=StalenessStatus.CURRENT_REFERENCE,
            limitations=limitations,
        )
        prior = self._repository.capture(capture_id)
        if prior is not None:
            if prior.request_hash != candidate.request_hash or prior.raw_sha256 != raw_hash:
                raise ResearchEvidenceConflict("CAPTURE_SOURCE_CONTENT_CONFLICT")
            return self.resume(
                capture_id=capture_id,
                raw_payload=raw_payload,
                records=records,
                resumed_at=ingested_at,
                chunk_size=chunk_size,
            )
        self._repository.save_capture(candidate, raw_payload)
        return self._append(candidate, records, ingested_at, chunk_size)

    def record_unavailable(
        self,
        *,
        capture_id: str,
        source_locator: str,
        requested_at: datetime,
        recorded_at: datetime,
        reason: str,
    ) -> ObservationCapture:
        return self._record_terminal(
            capture_id=capture_id,
            source_locator=source_locator,
            requested_at=requested_at,
            recorded_at=recorded_at,
            status=CaptureStatus.CAPABILITY_UNAVAILABLE,
            quality_status=QualityStatus.UNAVAILABLE,
            reason=reason,
            limitations=("GENUINE_SOURCE_NOT_SUBSTITUTED_WITH_FIXTURE",),
        )

    def record_failed(
        self,
        *,
        capture_id: str,
        source_locator: str,
        requested_at: datetime,
        recorded_at: datetime,
        reason: str,
    ) -> ObservationCapture:
        return self._record_terminal(
            capture_id=capture_id,
            source_locator=source_locator,
            requested_at=requested_at,
            recorded_at=recorded_at,
            status=CaptureStatus.FAILED,
            quality_status=QualityStatus.REJECTED,
            reason=reason,
            limitations=("MALFORMED_INCOMPLETE_STALE_OR_INCOMPATIBLE_SOURCE_REJECTED",),
        )

    def _record_terminal(
        self,
        *,
        capture_id: str,
        source_locator: str,
        requested_at: datetime,
        recorded_at: datetime,
        status: CaptureStatus,
        quality_status: QualityStatus,
        reason: str,
        limitations: tuple[str, ...],
    ) -> ObservationCapture:
        candidate = ObservationCapture.create(
            capture_id=capture_id,
            provider=SEC_PROVIDER,
            source_contract_version=SEC_SOURCE_CONTRACT,
            parser_version=SEC_PARSER_VERSION,
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            source_locator=source_locator,
            requested_at=requested_at,
            observation_at=None,
            ingestion_at=recorded_at,
            completed_at=recorded_at,
            status=status,
            raw_sha256=None,
            byte_count=0,
            expected_observations=0,
            retained_observations=0,
            next_ordinal=0,
            quality_status=quality_status,
            completeness_status=CompletenessStatus.UNAVAILABLE,
            staleness_status=StalenessStatus.UNKNOWN,
            limitations=limitations,
            failure_reason=reason,
        )
        prior = self._repository.capture(capture_id)
        if prior is not None:
            if prior.request_hash != candidate.request_hash:
                raise ResearchEvidenceConflict("CAPTURE_REQUEST_CONFLICT")
            return prior
        return self._repository.save_capture(candidate, None)

    def resume(
        self,
        *,
        capture_id: str,
        raw_payload: bytes,
        records: tuple[SecurityReference, ...],
        resumed_at: datetime,
        chunk_size: int,
    ) -> ObservationCapture:
        self._validate_chunk_size(chunk_size)
        capture = self._repository.capture(capture_id)
        if capture is None:
            raise ResearchEvidenceNotFound("CAPTURE_NOT_FOUND")
        if capture.status is CaptureStatus.COMPLETED:
            if hashlib.sha256(raw_payload).hexdigest() != capture.raw_sha256:
                raise ResearchEvidenceConflict("CAPTURE_SOURCE_CONTENT_CONFLICT")
            return capture
        if capture.status is not CaptureStatus.RUNNING:
            raise ResearchEvidenceConflict("CAPTURE_NOT_RESUMABLE")
        if (
            hashlib.sha256(raw_payload).hexdigest() != capture.raw_sha256
            or len(records) != capture.expected_observations
            or any(item.source_payload_hash != capture.raw_sha256 for item in records)
        ):
            raise ResearchEvidenceConflict("CAPTURE_SOURCE_CONTENT_CONFLICT")
        retained = self._repository.observations(capture_id)
        if retained:
            retained_receipt = dict(retained[0].normalized_values)["source_receipt_id"]
            if any(item.source_receipt_id != retained_receipt for item in records):
                raise ResearchEvidenceConflict("CAPTURE_SOURCE_RECEIPT_CONFLICT")
        return self._append(capture, records, resumed_at, chunk_size)

    @staticmethod
    def _validate_chunk_size(chunk_size: int) -> None:
        if not 1 <= chunk_size <= MAX_CAPTURE_CHUNK:
            raise ResearchEvidenceApplicationError("CAPTURE_CHUNK_SIZE_INVALID")

    def _append(
        self,
        capture: ObservationCapture,
        records: tuple[SecurityReference, ...],
        now: datetime,
        chunk_size: int,
    ) -> ObservationCapture:
        start = capture.next_ordinal
        selected = records[start : start + chunk_size]
        observations = tuple(
            RetainedObservation.create(
                capture=capture,
                ordinal=ordinal,
                subject_id=record.canonical_security_id,
                observed_at=record.source_timestamp,
                ingested_at=now,
                normalized_values=_normalized_security_values(record),
                limitations=record.limitations,
            )
            for ordinal, record in enumerate(selected, start=start)
        )
        next_ordinal = start + len(observations)
        completed = next_ordinal == capture.expected_observations
        replacement = ObservationCapture.create(
            capture_id=capture.capture_id,
            provider=capture.provider,
            source_contract_version=capture.source_contract_version,
            parser_version=capture.parser_version,
            evidence_class=capture.evidence_class,
            source_locator=capture.source_locator,
            requested_at=capture.requested_at,
            observation_at=capture.observation_at,
            ingestion_at=capture.ingestion_at,
            completed_at=now if completed else None,
            status=CaptureStatus.COMPLETED if completed else CaptureStatus.RUNNING,
            raw_sha256=capture.raw_sha256,
            byte_count=capture.byte_count,
            expected_observations=capture.expected_observations,
            retained_observations=next_ordinal,
            next_ordinal=next_ordinal,
            quality_status=QualityStatus.VALID,
            completeness_status=(
                CompletenessStatus.COMPLETE if completed else CompletenessStatus.PARTIAL
            ),
            staleness_status=capture.staleness_status,
            limitations=capture.limitations,
        )
        checkpoint = CaptureCheckpoint.create(
            capture_id=capture.capture_id,
            next_ordinal=next_ordinal,
            retained_observations=next_ordinal,
            raw_sha256=capture.raw_sha256 or "",
            created_at=now,
        )
        return self._repository.append_capture_chunk(
            expected_state_hash=capture.state_hash,
            capture=replacement,
            observations=observations,
            checkpoint=checkpoint,
        )


class CaptureNasdaqOptionsDirectory:
    """Nasdaq options-directory raw-artifact and rejection-ledger capture.

    Reuses the identical ``ObservationCapture``/``RetainedObservation``/
    ``CaptureCheckpoint`` lifecycle ``CaptureNasdaqDirectory`` and
    ``CaptureSecCompanyTickers`` already use, over a third, much smaller
    provider identity.  Unlike those two, its retained "records" are
    ``OptionUnderlyingLinkRejection`` entries -- one per distinct raw
    underlying symbol this capture could not link to exactly one reconciled
    security identity -- not option contracts: every successfully linked
    contract is retained separately, with full fidelity, in
    ``options_intelligence_contract``.  This keeps the bounded per-capture
    observation ledger sized by distinct-symbol-rejection count (small)
    rather than by total option-series row count (large, and already fully
    covered by the contract table), while ``byte_count``/``raw_sha256`` on
    the capture itself still cover the complete raw payload -- the genuine
    source-scale raw artifact this milestone must retain.  A capture with
    zero rejections (every row linked) still retains its raw artifact and
    reaches ``COMPLETED`` with an empty ledger, not a special case.
    """

    def __init__(self, repository: ResearchEvidenceRepository) -> None:
        self._repository = repository

    def start(
        self,
        *,
        capture_id: str,
        evidence_class: EvidenceClass | str,
        source_locator: str,
        raw_payload: bytes,
        rejections: tuple[OptionUnderlyingLinkRejection, ...],
        source_timestamp: datetime,
        requested_at: datetime,
        ingested_at: datetime,
        chunk_size: int,
        limitations: tuple[str, ...] = (),
    ) -> ObservationCapture:
        self._validate_chunk_size(chunk_size)
        if not raw_payload or len(rejections) > MAX_CAPTURE_RECORDS:
            raise ResearchEvidenceApplicationError("CAPTURE_BOUNDS_INVALID")
        evidence = EvidenceClass(evidence_class)
        if evidence not in {EvidenceClass.FIXTURE, EvidenceClass.GENUINE_PUBLIC}:
            raise ResearchEvidenceApplicationError("NASDAQ_OPTIONS_EVIDENCE_CLASS_INVALID")
        raw_hash = hashlib.sha256(raw_payload).hexdigest()
        if any(item.source_payload_hash != raw_hash for item in rejections):
            raise ResearchEvidenceApplicationError("NASDAQ_OPTIONS_REJECTION_HASH_MISMATCH")
        candidate = ObservationCapture.create(
            capture_id=capture_id,
            provider=NASDAQ_OPTIONS_PROVIDER,
            source_contract_version=NASDAQ_OPTIONS_SOURCE_CONTRACT,
            parser_version=NASDAQ_OPTIONS_PARSER_VERSION,
            evidence_class=evidence,
            source_locator=source_locator,
            requested_at=requested_at,
            observation_at=source_timestamp,
            ingestion_at=ingested_at,
            completed_at=None,
            status=CaptureStatus.RUNNING,
            raw_sha256=raw_hash,
            byte_count=len(raw_payload),
            expected_observations=len(rejections),
            retained_observations=0,
            next_ordinal=0,
            quality_status=QualityStatus.VALID,
            completeness_status=CompletenessStatus.PARTIAL,
            staleness_status=StalenessStatus.CURRENT_REFERENCE,
            limitations=limitations,
        )
        prior = self._repository.capture(capture_id)
        if prior is not None:
            if prior.request_hash != candidate.request_hash or prior.raw_sha256 != raw_hash:
                raise ResearchEvidenceConflict("CAPTURE_SOURCE_CONTENT_CONFLICT")
            return self.resume(
                capture_id=capture_id,
                raw_payload=raw_payload,
                rejections=rejections,
                resumed_at=ingested_at,
                chunk_size=chunk_size,
            )
        self._repository.save_capture(candidate, raw_payload)
        return self._append(candidate, rejections, ingested_at, chunk_size)

    def record_unavailable(
        self,
        *,
        capture_id: str,
        source_locator: str,
        requested_at: datetime,
        recorded_at: datetime,
        reason: str,
    ) -> ObservationCapture:
        return self._record_terminal(
            capture_id=capture_id,
            source_locator=source_locator,
            requested_at=requested_at,
            recorded_at=recorded_at,
            status=CaptureStatus.CAPABILITY_UNAVAILABLE,
            quality_status=QualityStatus.UNAVAILABLE,
            reason=reason,
            limitations=("GENUINE_SOURCE_NOT_SUBSTITUTED_WITH_FIXTURE",),
        )

    def record_failed(
        self,
        *,
        capture_id: str,
        source_locator: str,
        requested_at: datetime,
        recorded_at: datetime,
        reason: str,
    ) -> ObservationCapture:
        return self._record_terminal(
            capture_id=capture_id,
            source_locator=source_locator,
            requested_at=requested_at,
            recorded_at=recorded_at,
            status=CaptureStatus.FAILED,
            quality_status=QualityStatus.REJECTED,
            reason=reason,
            limitations=("MALFORMED_INCOMPLETE_STALE_OR_INCOMPATIBLE_SOURCE_REJECTED",),
        )

    def _record_terminal(
        self,
        *,
        capture_id: str,
        source_locator: str,
        requested_at: datetime,
        recorded_at: datetime,
        status: CaptureStatus,
        quality_status: QualityStatus,
        reason: str,
        limitations: tuple[str, ...],
    ) -> ObservationCapture:
        candidate = ObservationCapture.create(
            capture_id=capture_id,
            provider=NASDAQ_OPTIONS_PROVIDER,
            source_contract_version=NASDAQ_OPTIONS_SOURCE_CONTRACT,
            parser_version=NASDAQ_OPTIONS_PARSER_VERSION,
            evidence_class=EvidenceClass.GENUINE_PUBLIC,
            source_locator=source_locator,
            requested_at=requested_at,
            observation_at=None,
            ingestion_at=recorded_at,
            completed_at=recorded_at,
            status=status,
            raw_sha256=None,
            byte_count=0,
            expected_observations=0,
            retained_observations=0,
            next_ordinal=0,
            quality_status=quality_status,
            completeness_status=CompletenessStatus.UNAVAILABLE,
            staleness_status=StalenessStatus.UNKNOWN,
            limitations=limitations,
            failure_reason=reason,
        )
        prior = self._repository.capture(capture_id)
        if prior is not None:
            if prior.request_hash != candidate.request_hash:
                raise ResearchEvidenceConflict("CAPTURE_REQUEST_CONFLICT")
            return prior
        return self._repository.save_capture(candidate, None)

    def resume(
        self,
        *,
        capture_id: str,
        raw_payload: bytes,
        rejections: tuple[OptionUnderlyingLinkRejection, ...],
        resumed_at: datetime,
        chunk_size: int,
    ) -> ObservationCapture:
        self._validate_chunk_size(chunk_size)
        capture = self._repository.capture(capture_id)
        if capture is None:
            raise ResearchEvidenceNotFound("CAPTURE_NOT_FOUND")
        if capture.status is CaptureStatus.COMPLETED:
            if hashlib.sha256(raw_payload).hexdigest() != capture.raw_sha256:
                raise ResearchEvidenceConflict("CAPTURE_SOURCE_CONTENT_CONFLICT")
            return capture
        if capture.status is not CaptureStatus.RUNNING:
            raise ResearchEvidenceConflict("CAPTURE_NOT_RESUMABLE")
        if (
            hashlib.sha256(raw_payload).hexdigest() != capture.raw_sha256
            or len(rejections) != capture.expected_observations
            or any(item.source_payload_hash != capture.raw_sha256 for item in rejections)
        ):
            raise ResearchEvidenceConflict("CAPTURE_SOURCE_CONTENT_CONFLICT")
        return self._append(capture, rejections, resumed_at, chunk_size)

    @staticmethod
    def _validate_chunk_size(chunk_size: int) -> None:
        if not 1 <= chunk_size <= MAX_CAPTURE_CHUNK:
            raise ResearchEvidenceApplicationError("CAPTURE_CHUNK_SIZE_INVALID")

    def _append(
        self,
        capture: ObservationCapture,
        rejections: tuple[OptionUnderlyingLinkRejection, ...],
        now: datetime,
        chunk_size: int,
    ) -> ObservationCapture:
        start = capture.next_ordinal
        selected = rejections[start : start + chunk_size]
        observations = tuple(
            RetainedObservation.create(
                capture=capture,
                ordinal=ordinal,
                subject_id=item.canonical_rejection_id,
                observed_at=capture.observation_at or now,
                ingested_at=now,
                normalized_values=_normalized_option_rejection_values(item),
                limitations=item.reasons,
            )
            for ordinal, item in enumerate(selected, start=start)
        )
        next_ordinal = start + len(observations)
        completed = next_ordinal == capture.expected_observations
        replacement = ObservationCapture.create(
            capture_id=capture.capture_id,
            provider=capture.provider,
            source_contract_version=capture.source_contract_version,
            parser_version=capture.parser_version,
            evidence_class=capture.evidence_class,
            source_locator=capture.source_locator,
            requested_at=capture.requested_at,
            observation_at=capture.observation_at,
            ingestion_at=capture.ingestion_at,
            completed_at=now if completed else None,
            status=CaptureStatus.COMPLETED if completed else CaptureStatus.RUNNING,
            raw_sha256=capture.raw_sha256,
            byte_count=capture.byte_count,
            expected_observations=capture.expected_observations,
            retained_observations=next_ordinal,
            next_ordinal=next_ordinal,
            quality_status=QualityStatus.VALID,
            completeness_status=(
                CompletenessStatus.COMPLETE if completed else CompletenessStatus.PARTIAL
            ),
            staleness_status=capture.staleness_status,
            limitations=capture.limitations,
        )
        checkpoint = CaptureCheckpoint.create(
            capture_id=capture.capture_id,
            next_ordinal=next_ordinal,
            retained_observations=next_ordinal,
            raw_sha256=capture.raw_sha256 or "",
            created_at=now,
        )
        return self._repository.append_capture_chunk(
            expected_state_hash=capture.state_hash,
            capture=replacement,
            observations=observations,
            checkpoint=checkpoint,
        )


def _normalized_option_rejection_values(
    value: OptionUnderlyingLinkRejection,
) -> tuple[tuple[str, object], ...]:
    return (
        ("candidate_security_ids", value.candidate_security_ids),
        ("outcome", value.outcome.value),
        ("raw_underlying_symbol", value.raw_underlying_symbol),
        ("reasons", value.reasons),
        ("row_count", value.row_count),
        ("source_payload_hash", value.source_payload_hash),
    )


def option_underlying_rejection_from_observation(
    value: RetainedObservation,
) -> OptionUnderlyingLinkRejection:
    fields = dict(value.normalized_values)
    return OptionUnderlyingLinkRejection.create(
        raw_underlying_symbol=str(fields["raw_underlying_symbol"]),
        outcome=UnderlyingLinkOutcome(str(fields["outcome"])),
        row_count=int(fields["row_count"]),  # type: ignore[call-overload]
        candidate_security_ids=tuple(fields["candidate_security_ids"]),  # type: ignore[arg-type]
        reasons=tuple(fields["reasons"]),  # type: ignore[arg-type]
        source_payload_hash=str(fields["source_payload_hash"]),
    )


def latest_completed_capture(
    captures: tuple[ObservationCapture, ...],
    provider: str,
    *,
    source_locator: str | None = None,
) -> ObservationCapture | None:
    """The most recent ``COMPLETED`` capture for one provider, if any.

    Shared by the CLI and the read-only Operator API/Console projections so
    both surfaces agree on "most recent" without duplicating the lookup.
    ``source_locator``, when given, narrows to one exact source within a
    provider that captures more than one distinct file -- Nasdaq Trader's
    ``nasdaqlisted.txt`` and ``otherlisted.txt`` both report
    ``provider == "NASDAQ_TRADER"``, so ``provider`` alone cannot tell them
    apart; their ``source_locator`` URLs always can.
    """
    candidates = [
        item
        for item in captures
        if item.provider == provider
        and item.status is CaptureStatus.COMPLETED
        and (source_locator is None or item.source_locator == source_locator)
    ]
    return sorted(candidates, key=lambda item: item.capture_id)[-1] if candidates else None


def _normalized_security_values(
    value: SecurityReference,
) -> tuple[tuple[str, object], ...]:
    return (
        ("active", value.active.value),
        ("adr", value.adr.value),
        ("cik", value.cik),
        ("common_stock", value.common_stock.value),
        ("current_symbol", value.current_symbol),
        ("etf", value.etf.value),
        ("issuer_name", value.issuer_name),
        ("listing_category", value.listing_category),
        ("listing_exchange", value.listing_exchange),
        ("listing_status", value.listing_status),
        ("marginable", value.marginable.value),
        ("provider_disagreements", value.provider_disagreements),
        ("provider_symbol", value.provider_symbol),
        ("reit", value.reit.value),
        ("round_lot_size", value.round_lot_size),
        ("security_type", value.security_type.value),
        ("shortable", value.shortable.value),
        ("source_payload_hash", value.source_payload_hash),
        ("source_provider", value.source_provider),
        ("source_receipt_id", value.source_receipt_id),
        ("source_timestamp", value.source_timestamp),
        ("test_issue", value.test_issue.value),
        ("tradable", value.tradable.value),
    )


def security_from_observation(value: RetainedObservation) -> SecurityReference:
    fields = dict(value.normalized_values)
    round_lot_value = fields["round_lot_size"]
    if round_lot_value is not None and (
        isinstance(round_lot_value, bool) or not isinstance(round_lot_value, int)
    ):
        raise ResearchEvidenceConflict("OBSERVATION_ROUND_LOT_INVALID")
    return SecurityReference.create(
        current_symbol=str(fields["current_symbol"]),
        provider_symbol=str(fields["provider_symbol"]),
        issuer_name=str(fields["issuer_name"]),
        listing_exchange=str(fields["listing_exchange"]),
        listing_category=str(fields["listing_category"]),
        security_type=SecurityType(str(fields["security_type"])),
        common_stock=EvidenceState(str(fields["common_stock"])),
        etf=EvidenceState(str(fields["etf"])),
        adr=EvidenceState(str(fields["adr"])),
        reit=EvidenceState(str(fields["reit"])),
        test_issue=EvidenceState(str(fields["test_issue"])),
        listing_status=str(fields["listing_status"]),
        round_lot_size=round_lot_value,
        tradable=EvidenceState(str(fields["tradable"])),
        shortable=EvidenceState(str(fields["shortable"])),
        marginable=EvidenceState(str(fields["marginable"])),
        active=EvidenceState(str(fields["active"])),
        cik=str(fields["cik"]) if fields["cik"] is not None else None,
        source_provider=str(fields["source_provider"]),
        source_receipt_id=str(fields["source_receipt_id"]),
        source_timestamp=fields["source_timestamp"],  # type: ignore[arg-type]
        source_payload_hash=str(fields["source_payload_hash"]),
        provider_disagreements=tuple(fields["provider_disagreements"]),  # type: ignore[arg-type]
        limitations=value.limitations,
    )


class MaterializeResearchDatasetSnapshot:
    def __init__(self, repository: ResearchEvidenceRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        snapshot_id: str,
        component_capture_ids: tuple[str, ...],
        created_at: datetime,
    ) -> ResearchDatasetSnapshot:
        captures = tuple(self._capture_or_raise(capture_id) for capture_id in component_capture_ids)
        evidence_classes = {item.evidence_class for item in captures}
        if len(evidence_classes) != 1:
            raise ResearchEvidenceConflict("SNAPSHOT_EVIDENCE_CLASS_MIXING_PROHIBITED")
        if any(item.status is not CaptureStatus.COMPLETED for item in captures):
            raise ResearchEvidenceConflict("SNAPSHOT_CAPTURE_INCOMPLETE")
        observations = tuple(
            observation
            for capture in captures
            for observation in self._repository.observations(capture.capture_id)
        )
        if not observations or any(
            len(self._repository.observations(capture.capture_id)) != capture.expected_observations
            for capture in captures
        ):
            raise ResearchEvidenceConflict("SNAPSHOT_OBSERVATION_SET_INCOMPLETE")
        securities = tuple(security_from_observation(item) for item in observations)
        reconciled = reconcile_security_references(securities)
        policy = CurrentEquityUniversePolicy.initial()
        universe = create_current_equity_universe_snapshot(
            securities=reconciled,
            policy=policy,
            source_receipt_ids=tuple(item.source_receipt_id for item in securities),
            as_of_date=max(item.observed_at for item in observations).date(),
            created_at=max(item.ingested_at for item in observations),
        )
        candidate = ResearchDatasetSnapshot.create(
            snapshot_id=snapshot_id,
            component_capture_ids=tuple(item.capture_id for item in captures),
            evidence_class=next(iter(evidence_classes)),
            normalized_security_ids=tuple(item.canonical_security_id for item in reconciled),
            source_receipt_ids=universe.source_receipt_ids,
            source_versions=tuple(item.source_contract_version for item in captures),
            parser_versions=tuple(item.parser_version for item in captures),
            universe_id=universe.universe_id,
            universe_policy_id=universe.policy_id,
            universe_policy_hash=universe.policy_hash,
            total_considered=universe.total_considered,
            eligible_count=universe.research_eligible_count,
            decisions=tuple(
                DatasetMemberDecision(
                    item.canonical_security_id,
                    item.current_symbol,
                    item.research_eligible,
                    item.research_reasons,
                    item.execution_capability.value,
                    item.execution_reasons,
                    item.security_hash,
                )
                for item in universe.decisions
            ),
            created_at=created_at,
        )
        prior = self._repository.snapshot(snapshot_id)
        if prior is not None:
            if prior.manifest_hash != candidate.manifest_hash:
                raise ResearchEvidenceConflict("SNAPSHOT_IDENTITY_CONFLICT")
            return prior
        return self._repository.save_snapshot(candidate)

    def _capture_or_raise(self, capture_id: str) -> ObservationCapture:
        value = self._repository.capture(capture_id)
        if value is None:
            raise ResearchEvidenceNotFound(f"CAPTURE_NOT_FOUND:{capture_id}")
        return value


class ExecuteBroadUniverseExperiment:
    def __init__(self, repository: ResearchEvidenceRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        run_id: str,
        hypothesis_version_id: str,
        snapshot_id: str,
        minimum_eligible_count: int,
        code_revision: str,
        executed_at: datetime,
        deterministic_seed: int | None = 0,
        executor_version: str = BROAD_UNIVERSE_ELIGIBILITY_EXPERIMENT_V1,
    ) -> ResearchExperimentRun:
        hypothesis = self._repository.hypothesis_version(hypothesis_version_id)
        snapshot = self._repository.snapshot(snapshot_id)
        if hypothesis is None:
            raise ResearchEvidenceNotFound("HYPOTHESIS_VERSION_NOT_FOUND")
        if snapshot is None:
            raise ResearchEvidenceNotFound("SNAPSHOT_NOT_FOUND")
        configuration: tuple[tuple[str, object], ...] = (
            ("minimum_eligible_count", minimum_eligible_count),
            ("universe_policy_hash", snapshot.universe_policy_hash),
            ("universe_policy_id", snapshot.universe_policy_id),
        )
        if executor_version != BROAD_UNIVERSE_ELIGIBILITY_EXPERIMENT_V1:
            candidate = ResearchExperimentRun.create(
                run_id=run_id,
                hypothesis_version_id=hypothesis.hypothesis_version_id,
                snapshot_id=snapshot.snapshot_id,
                executor_version=executor_version,
                configuration=configuration,
                feature_policy_versions=(snapshot.universe_policy_id,),
                code_revision=code_revision,
                deterministic_seed=deterministic_seed,
                executed_at=executed_at,
                state=ExperimentState.FAILED,
                warnings=(RESEARCH_ONLY_WARNING,),
                metrics=(),
                artifacts=(),
                evidence_class=snapshot.evidence_class,
                reproducibility_status=ReproducibilityStatus.NOT_REPRODUCIBLE,
                failure_reason="UNSUPPORTED_EXPERIMENT_EXECUTOR_VERSION",
            )
        elif minimum_eligible_count < 0:
            candidate = ResearchExperimentRun.create(
                run_id=run_id,
                hypothesis_version_id=hypothesis.hypothesis_version_id,
                snapshot_id=snapshot.snapshot_id,
                executor_version=executor_version,
                configuration=configuration,
                feature_policy_versions=(snapshot.universe_policy_id,),
                code_revision=code_revision,
                deterministic_seed=deterministic_seed,
                executed_at=executed_at,
                state=ExperimentState.FAILED,
                warnings=(RESEARCH_ONLY_WARNING,),
                metrics=(),
                artifacts=(),
                evidence_class=snapshot.evidence_class,
                reproducibility_status=ReproducibilityStatus.NOT_REPRODUCIBLE,
                failure_reason="MINIMUM_ELIGIBLE_COUNT_INVALID",
            )
        else:
            supported = snapshot.eligible_count >= minimum_eligible_count
            artifact_payload: tuple[tuple[str, object], ...] = (
                ("decisions", snapshot.decisions),
                ("manifest_hash", snapshot.manifest_hash),
            )
            warnings = [RESEARCH_ONLY_WARNING, CURRENT_LISTED_WARNING]
            if snapshot.evidence_class is EvidenceClass.FIXTURE:
                warnings.append("FIXTURE EVIDENCE — NOT GENUINE MARKET OBSERVATION")
            candidate = ResearchExperimentRun.create(
                run_id=run_id,
                hypothesis_version_id=hypothesis.hypothesis_version_id,
                snapshot_id=snapshot.snapshot_id,
                executor_version=executor_version,
                configuration=configuration,
                feature_policy_versions=(snapshot.universe_policy_id,),
                code_revision=code_revision,
                deterministic_seed=deterministic_seed,
                executed_at=executed_at,
                state=ExperimentState.COMPLETED,
                warnings=tuple(warnings),
                metrics=(
                    ExperimentMetric("total_considered", str(snapshot.total_considered)),
                    ExperimentMetric("eligible_count", str(snapshot.eligible_count)),
                    ExperimentMetric("minimum_eligible_count", str(minimum_eligible_count)),
                    ExperimentMetric("hypothesis_supported", str(supported).lower()),
                ),
                artifacts=(
                    ExperimentResultArtifact(
                        "BROAD_UNIVERSE_MEMBER_DECISIONS",
                        content_digest(artifact_payload),
                        artifact_payload,
                    ),
                ),
                evidence_class=snapshot.evidence_class,
                reproducibility_status=ReproducibilityStatus.REPRODUCIBLE,
            )
        prior = self._repository.run(run_id)
        if prior is not None:
            if prior.canonical_hash != candidate.canonical_hash:
                raise ResearchEvidenceConflict("EXPERIMENT_RUN_IDENTITY_CONFLICT")
            return prior
        return self._repository.save_run(candidate)

    def replay(self, run_id: str) -> ResearchExperimentRun:
        prior = self._repository.run(run_id)
        if prior is None:
            raise ResearchEvidenceNotFound("EXPERIMENT_RUN_NOT_FOUND")
        config = dict(prior.configuration)
        minimum_eligible_count = config["minimum_eligible_count"]
        if isinstance(minimum_eligible_count, bool) or not isinstance(minimum_eligible_count, int):
            raise ResearchEvidenceConflict("EXPERIMENT_CONFIGURATION_INVALID")
        replay = self.execute(
            run_id=prior.run_id,
            hypothesis_version_id=prior.hypothesis_version_id,
            snapshot_id=prior.snapshot_id,
            minimum_eligible_count=minimum_eligible_count,
            code_revision=prior.code_revision,
            executed_at=prior.executed_at,
            deterministic_seed=prior.deterministic_seed,
            executor_version=prior.executor_version,
        )
        if replay.result_hash != prior.result_hash:
            raise ResearchEvidenceConflict("EXPERIMENT_REPLAY_RESULT_CONFLICT")
        return replay


class RecordResearchExperimentOutcome:
    def __init__(self, repository: ResearchEvidenceRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        outcome_id: str,
        run_id: str,
        decision: ExperimentOutcomeDecision | str,
        decided_at: datetime,
        actor: str,
        reason: str,
    ) -> ResearchExperimentOutcome:
        run = self._repository.run(run_id)
        if run is None:
            raise ResearchEvidenceNotFound("EXPERIMENT_RUN_NOT_FOUND")
        candidate = ResearchExperimentOutcome.create(
            outcome_id=outcome_id,
            run=run,
            decision=decision,
            decided_at=decided_at,
            evidence_hashes=(run.result_hash, run.canonical_hash),
            threshold_rules=(
                f"configuration_hash={run.configuration_hash}",
                "PROMOTION_CANDIDATE_MEANS_FURTHER_VALIDATION_ONLY",
                "PAPER_ELIGIBLE=false",
                "LIVE_ELIGIBLE=false",
            ),
            warnings=(*run.warnings, RESEARCH_ONLY_WARNING),
            actor=actor,
            reason=reason,
        )
        prior = self._repository.outcome(outcome_id)
        prior_for_run = self._repository.outcome_for_run(run_id)
        if prior is not None:
            if prior.canonical_hash != candidate.canonical_hash:
                raise ResearchEvidenceConflict("OUTCOME_IDENTITY_CONFLICT")
            return prior
        if prior_for_run is not None:
            if prior_for_run.canonical_hash != candidate.canonical_hash:
                raise ResearchEvidenceConflict("RUN_OUTCOME_ALREADY_RECORDED")
            return prior_for_run
        return self._repository.save_outcome(candidate)


def research_workspace(repository: ResearchEvidenceRepository) -> dict[str, object]:
    captures = repository.captures()
    snapshots = repository.snapshots()
    runs = repository.runs()
    outcomes = repository.outcomes()
    return {
        "hypotheses": repository.hypotheses(),
        "captures": captures,
        "capture_checkpoints": tuple(
            checkpoint
            for capture in captures
            for checkpoint in repository.checkpoints(capture.capture_id)
        ),
        "observation_count": sum(
            len(repository.observations(capture.capture_id)) for capture in captures
        ),
        "snapshots": snapshots,
        "runs": runs,
        "outcomes": outcomes,
        "warnings": (
            RESEARCH_ONLY_WARNING,
            CURRENT_LISTED_WARNING,
            "OBSERVATION IS NOT STRATEGY OR OPPORTUNITY AUTHORITY",
        ),
        "authority": "RESEARCH_ONLY",
        "financial_authority": "NONE",
        "orders": 0,
        "positions": 0,
        "reservations": 0,
        "journals": 0,
        "broker_writes": 0,
    }
