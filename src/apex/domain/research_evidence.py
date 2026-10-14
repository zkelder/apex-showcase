"""Durable, authority-free research evidence and experiment lifecycle values.

Observation, experiment, strategy, and opportunity are deliberately distinct.
This module owns retained observations and reproducible research decisions only;
it cannot create a Strategy, Opportunity, Order, Position, reservation, journal,
broker request, PAPER authority, or LIVE authority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from apex.domain.values import content_digest, deterministic_id

DURABLE_RESEARCH_EVIDENCE_V1 = "DURABLE_RESEARCH_EVIDENCE_V1"
NASDAQ_DIRECTORY_CAPTURE_V1 = "NASDAQ_DIRECTORY_CAPTURE_V1"
BROAD_UNIVERSE_ELIGIBILITY_EXPERIMENT_V1 = "BROAD_UNIVERSE_ELIGIBILITY_EXPERIMENT_V1"
RESEARCH_ONLY_WARNING = "RESEARCH EVIDENCE ONLY — NO CAPITAL OR EXECUTION AUTHORITY"

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class ResearchEvidenceError(ValueError):
    """A durable research evidence invariant was violated."""


class EvidenceClass(StrEnum):
    FIXTURE = "FIXTURE"
    GENUINE_PUBLIC = "GENUINE_PUBLIC"
    HISTORICAL_PUBLIC = "HISTORICAL_PUBLIC"
    SANDBOX = "SANDBOX"
    PAPER = "PAPER"
    LIVE = "LIVE"


class HypothesisStatus(StrEnum):
    REGISTERED = "REGISTERED"
    SUPERSEDED = "SUPERSEDED"
    RETIRED = "RETIRED"


class CaptureStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    FAILED = "FAILED"


class QualityStatus(StrEnum):
    VALID = "VALID"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"


class CompletenessStatus(StrEnum):
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class StalenessStatus(StrEnum):
    CURRENT_REFERENCE = "CURRENT_REFERENCE"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class ExperimentState(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ReproducibilityStatus(StrEnum):
    REPRODUCIBLE = "REPRODUCIBLE"
    NOT_REPRODUCIBLE = "NOT_REPRODUCIBLE"


class ExperimentOutcomeDecision(StrEnum):
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    PROMOTION_CANDIDATE = "PROMOTION_CANDIDATE"


def _identity(value: str, code: str) -> str:
    normalized = value.strip()
    if _IDENTITY.fullmatch(normalized) is None:
        raise ResearchEvidenceError(code)
    return normalized


def _aware(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResearchEvidenceError(code)
    return value


def _hash(value: str, code: str) -> str:
    if _HASH.fullmatch(value) is None:
        raise ResearchEvidenceError(code)
    return value


def _text(value: str, code: str, *, maximum: int = 2_000) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ResearchEvidenceError(code)
    return normalized


@dataclass(frozen=True, slots=True)
class ResearchHypothesisVersion:
    hypothesis_id: str
    hypothesis_version_id: str
    revision: int
    question: str
    motivation: str
    actor: str
    market_scope: tuple[str, ...]
    created_at: datetime
    status: HypothesisStatus
    supersedes_version_id: str | None
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
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
        root_id = _identity(hypothesis_id, "HYPOTHESIS_ID_INVALID")
        if revision < 1:
            raise ResearchEvidenceError("HYPOTHESIS_REVISION_INVALID")
        parent = (
            _identity(supersedes_version_id, "HYPOTHESIS_PARENT_ID_INVALID")
            if supersedes_version_id is not None
            else None
        )
        if (revision == 1) != (parent is None):
            raise ResearchEvidenceError("HYPOTHESIS_REVISION_LINEAGE_INVALID")
        scope = tuple(
            sorted({_text(item, "HYPOTHESIS_SCOPE_INVALID", maximum=160) for item in market_scope})
        )
        if not scope:
            raise ResearchEvidenceError("HYPOTHESIS_SCOPE_REQUIRED")
        payload = (
            DURABLE_RESEARCH_EVIDENCE_V1,
            root_id,
            revision,
            _text(question, "HYPOTHESIS_QUESTION_INVALID"),
            _text(motivation, "HYPOTHESIS_MOTIVATION_INVALID"),
            _text(actor, "HYPOTHESIS_ACTOR_INVALID", maximum=160),
            scope,
            _aware(created_at, "HYPOTHESIS_TIMESTAMP_INVALID"),
            HypothesisStatus(status),
            parent,
        )
        return cls(
            root_id,
            deterministic_id("research_hypothesis_version", payload),
            revision,
            payload[3],
            payload[4],
            payload[5],
            scope,
            payload[7],
            payload[8],
            parent,
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ObservationCapture:
    capture_id: str
    provider: str
    source_contract_version: str
    parser_version: str
    evidence_class: EvidenceClass
    source_locator: str
    requested_at: datetime
    observation_at: datetime | None
    ingestion_at: datetime
    completed_at: datetime | None
    status: CaptureStatus
    raw_sha256: str | None
    byte_count: int
    expected_observations: int
    retained_observations: int
    next_ordinal: int
    quality_status: QualityStatus
    completeness_status: CompletenessStatus
    staleness_status: StalenessStatus
    limitations: tuple[str, ...]
    failure_reason: str | None
    request_hash: str
    state_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        provider: str,
        source_contract_version: str,
        parser_version: str,
        evidence_class: EvidenceClass | str,
        source_locator: str,
        requested_at: datetime,
        observation_at: datetime | None,
        ingestion_at: datetime,
        completed_at: datetime | None,
        status: CaptureStatus | str,
        raw_sha256: str | None,
        byte_count: int,
        expected_observations: int,
        retained_observations: int,
        next_ordinal: int,
        quality_status: QualityStatus | str,
        completeness_status: CompletenessStatus | str,
        staleness_status: StalenessStatus | str,
        limitations: tuple[str, ...],
        failure_reason: str | None = None,
    ) -> ObservationCapture:
        identity = _identity(capture_id, "CAPTURE_ID_INVALID")
        source = _text(provider, "CAPTURE_PROVIDER_INVALID", maximum=80)
        contract = _text(source_contract_version, "CAPTURE_SOURCE_CONTRACT_INVALID", maximum=160)
        parser = _text(parser_version, "CAPTURE_PARSER_VERSION_INVALID", maximum=160)
        locator = _text(source_locator, "CAPTURE_SOURCE_LOCATOR_INVALID", maximum=500)
        requested = _aware(requested_at, "CAPTURE_REQUEST_TIMESTAMP_INVALID")
        ingested = _aware(ingestion_at, "CAPTURE_INGESTION_TIMESTAMP_INVALID")
        observed = (
            _aware(observation_at, "CAPTURE_OBSERVATION_TIMESTAMP_INVALID")
            if observation_at is not None
            else None
        )
        completed = (
            _aware(completed_at, "CAPTURE_COMPLETION_TIMESTAMP_INVALID")
            if completed_at is not None
            else None
        )
        raw_hash = _hash(raw_sha256, "CAPTURE_RAW_HASH_INVALID") if raw_sha256 else None
        if (
            byte_count < 0
            or expected_observations < 0
            or retained_observations < 0
            or retained_observations > expected_observations
            or next_ordinal != retained_observations
        ):
            raise ResearchEvidenceError("CAPTURE_CHECKPOINT_INVALID")
        state = CaptureStatus(status)
        completeness = CompletenessStatus(completeness_status)
        quality = QualityStatus(quality_status)
        if state is CaptureStatus.COMPLETED and (
            completed is None
            or raw_hash is None
            or retained_observations != expected_observations
            or completeness is not CompletenessStatus.COMPLETE
            or quality is not QualityStatus.VALID
        ):
            raise ResearchEvidenceError("CAPTURE_COMPLETION_INVALID")
        if state is CaptureStatus.RUNNING and (
            raw_hash is None or completeness is not CompletenessStatus.PARTIAL
        ):
            raise ResearchEvidenceError("CAPTURE_RUNNING_STATE_INVALID")
        if state in {CaptureStatus.CAPABILITY_UNAVAILABLE, CaptureStatus.FAILED} and (
            completed is None or failure_reason is None
        ):
            raise ResearchEvidenceError("CAPTURE_FAILURE_STATE_INVALID")
        limit_values = tuple(
            sorted({_text(item, "CAPTURE_LIMITATION_INVALID", maximum=500) for item in limitations})
        )
        reason = (
            _text(failure_reason, "CAPTURE_FAILURE_REASON_INVALID", maximum=1_000)
            if failure_reason
            else None
        )
        request_payload = (
            NASDAQ_DIRECTORY_CAPTURE_V1,
            identity,
            source,
            contract,
            parser,
            EvidenceClass(evidence_class),
            locator,
            requested,
        )
        request_hash = content_digest(request_payload)
        state_payload = (
            request_hash,
            observed,
            ingested,
            completed,
            state,
            raw_hash,
            byte_count,
            expected_observations,
            retained_observations,
            next_ordinal,
            quality,
            completeness,
            StalenessStatus(staleness_status),
            limit_values,
            reason,
        )
        return cls(
            identity,
            source,
            contract,
            parser,
            EvidenceClass(evidence_class),
            locator,
            requested,
            observed,
            ingested,
            completed,
            state,
            raw_hash,
            byte_count,
            expected_observations,
            retained_observations,
            next_ordinal,
            quality,
            completeness,
            StalenessStatus(staleness_status),
            limit_values,
            reason,
            request_hash,
            content_digest(state_payload),
        )


@dataclass(frozen=True, slots=True)
class RetainedObservation:
    observation_id: str
    capture_id: str
    ordinal: int
    provider: str
    source_contract_version: str
    parser_version: str
    evidence_class: EvidenceClass
    subject_id: str
    observed_at: datetime
    ingested_at: datetime
    normalized_values: tuple[tuple[str, object], ...]
    raw_sha256: str
    quality_status: QualityStatus
    completeness_status: CompletenessStatus
    staleness_status: StalenessStatus
    limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        capture: ObservationCapture,
        ordinal: int,
        subject_id: str,
        observed_at: datetime,
        ingested_at: datetime,
        normalized_values: tuple[tuple[str, object], ...],
        limitations: tuple[str, ...],
    ) -> RetainedObservation:
        if capture.raw_sha256 is None or ordinal < 0 or ordinal >= capture.expected_observations:
            raise ResearchEvidenceError("OBSERVATION_ORDINAL_INVALID")
        normalized = tuple(sorted(normalized_values, key=lambda item: item[0]))
        if not normalized or len({name for name, _ in normalized}) != len(normalized):
            raise ResearchEvidenceError("OBSERVATION_NORMALIZED_VALUES_INVALID")
        payload = (
            DURABLE_RESEARCH_EVIDENCE_V1,
            capture.capture_id,
            ordinal,
            capture.provider,
            capture.source_contract_version,
            capture.parser_version,
            capture.evidence_class,
            _identity(subject_id, "OBSERVATION_SUBJECT_ID_INVALID"),
            _aware(observed_at, "OBSERVATION_TIMESTAMP_INVALID"),
            _aware(ingested_at, "OBSERVATION_INGESTION_TIMESTAMP_INVALID"),
            normalized,
            capture.raw_sha256,
            QualityStatus.VALID,
            CompletenessStatus.COMPLETE,
            capture.staleness_status,
            tuple(sorted(limitations)),
        )
        digest = content_digest(payload)
        return cls(
            deterministic_id("research_observation", payload),
            capture.capture_id,
            ordinal,
            capture.provider,
            capture.source_contract_version,
            capture.parser_version,
            capture.evidence_class,
            payload[7],
            payload[8],
            payload[9],
            normalized,
            capture.raw_sha256,
            QualityStatus.VALID,
            CompletenessStatus.COMPLETE,
            capture.staleness_status,
            payload[15],
            digest,
        )


@dataclass(frozen=True, slots=True)
class CaptureCheckpoint:
    checkpoint_id: str
    capture_id: str
    next_ordinal: int
    retained_observations: int
    raw_sha256: str
    created_at: datetime
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        next_ordinal: int,
        retained_observations: int,
        raw_sha256: str,
        created_at: datetime,
    ) -> CaptureCheckpoint:
        if next_ordinal < 0 or next_ordinal != retained_observations:
            raise ResearchEvidenceError("CHECKPOINT_ORDINAL_INVALID")
        payload = (
            _identity(capture_id, "CHECKPOINT_CAPTURE_ID_INVALID"),
            next_ordinal,
            retained_observations,
            _hash(raw_sha256, "CHECKPOINT_RAW_HASH_INVALID"),
            _aware(created_at, "CHECKPOINT_TIMESTAMP_INVALID"),
        )
        return cls(
            deterministic_id("research_capture_checkpoint", payload),
            payload[0],
            next_ordinal,
            retained_observations,
            payload[3],
            payload[4],
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class DatasetMemberDecision:
    """``eligible``/``reasons`` here has always meant research-universe
    eligibility (see ``apex.domain.security_reference.UniverseMemberDecision``,
    the source this is mapped from); ``execution_capability``/
    ``execution_reasons`` carries that source's independent provider
    execution-capability decision alongside it, never feeding back into
    ``eligible``.
    """

    canonical_security_id: str
    current_symbol: str
    eligible: bool
    reasons: tuple[str, ...]
    execution_capability: str
    execution_reasons: tuple[str, ...]
    security_hash: str


@dataclass(frozen=True, slots=True)
class ResearchDatasetSnapshot:
    snapshot_id: str
    component_capture_ids: tuple[str, ...]
    evidence_class: EvidenceClass
    normalized_security_ids: tuple[str, ...]
    source_receipt_ids: tuple[str, ...]
    source_versions: tuple[str, ...]
    parser_versions: tuple[str, ...]
    universe_id: str
    universe_policy_id: str
    universe_policy_hash: str
    total_considered: int
    eligible_count: int
    decisions: tuple[DatasetMemberDecision, ...]
    quality_status: QualityStatus
    completeness_status: CompletenessStatus
    manifest_hash: str
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        snapshot_id: str,
        component_capture_ids: tuple[str, ...],
        evidence_class: EvidenceClass | str,
        normalized_security_ids: tuple[str, ...],
        source_receipt_ids: tuple[str, ...],
        source_versions: tuple[str, ...],
        parser_versions: tuple[str, ...],
        universe_id: str,
        universe_policy_id: str,
        universe_policy_hash: str,
        total_considered: int,
        eligible_count: int,
        decisions: tuple[DatasetMemberDecision, ...],
        created_at: datetime,
    ) -> ResearchDatasetSnapshot:
        captures = tuple(
            sorted(
                {_identity(item, "SNAPSHOT_CAPTURE_ID_INVALID") for item in component_capture_ids}
            )
        )
        if not captures:
            raise ResearchEvidenceError("SNAPSHOT_CAPTURE_REQUIRED")
        securities = tuple(sorted(set(normalized_security_ids)))
        if (
            not securities
            or total_considered != len(decisions)
            or eligible_count != sum(item.eligible for item in decisions)
            or total_considered != len(securities)
        ):
            raise ResearchEvidenceError("SNAPSHOT_MEMBERSHIP_INVALID")
        manifest = (
            DURABLE_RESEARCH_EVIDENCE_V1,
            captures,
            EvidenceClass(evidence_class),
            securities,
            tuple(sorted(source_receipt_ids)),
            tuple(sorted(source_versions)),
            tuple(sorted(parser_versions)),
            _identity(universe_id, "SNAPSHOT_UNIVERSE_ID_INVALID"),
            _identity(universe_policy_id, "SNAPSHOT_POLICY_ID_INVALID"),
            _hash(universe_policy_hash, "SNAPSHOT_POLICY_HASH_INVALID"),
            tuple(decisions),
            QualityStatus.VALID,
            CompletenessStatus.COMPLETE,
        )
        manifest_hash = content_digest(manifest)
        created = _aware(created_at, "SNAPSHOT_TIMESTAMP_INVALID")
        return cls(
            _identity(snapshot_id, "SNAPSHOT_ID_INVALID"),
            captures,
            EvidenceClass(evidence_class),
            securities,
            manifest[4],
            manifest[5],
            manifest[6],
            manifest[7],
            manifest[8],
            manifest[9],
            total_considered,
            eligible_count,
            tuple(decisions),
            QualityStatus.VALID,
            CompletenessStatus.COMPLETE,
            manifest_hash,
            created,
            content_digest((manifest_hash, created)),
        )


@dataclass(frozen=True, slots=True)
class ExperimentMetric:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class ExperimentResultArtifact:
    artifact_type: str
    content_hash: str
    payload: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class ResearchExperimentRun:
    run_id: str
    hypothesis_version_id: str
    snapshot_id: str
    executor_version: str
    configuration: tuple[tuple[str, object], ...]
    configuration_hash: str
    feature_policy_versions: tuple[str, ...]
    code_revision: str
    deterministic_seed: int | None
    executed_at: datetime
    state: ExperimentState
    warnings: tuple[str, ...]
    metrics: tuple[ExperimentMetric, ...]
    artifacts: tuple[ExperimentResultArtifact, ...]
    evidence_class: EvidenceClass
    reproducibility_status: ReproducibilityStatus
    failure_reason: str | None
    result_hash: str
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        hypothesis_version_id: str,
        snapshot_id: str,
        executor_version: str,
        configuration: tuple[tuple[str, object], ...],
        feature_policy_versions: tuple[str, ...],
        code_revision: str,
        deterministic_seed: int | None,
        executed_at: datetime,
        state: ExperimentState | str,
        warnings: tuple[str, ...],
        metrics: tuple[ExperimentMetric, ...],
        artifacts: tuple[ExperimentResultArtifact, ...],
        evidence_class: EvidenceClass | str,
        reproducibility_status: ReproducibilityStatus | str,
        failure_reason: str | None = None,
    ) -> ResearchExperimentRun:
        config = tuple(sorted(configuration, key=lambda item: item[0]))
        if not config or len({name for name, _ in config}) != len(config):
            raise ResearchEvidenceError("EXPERIMENT_CONFIGURATION_INVALID")
        state_value = ExperimentState(state)
        reason = (
            _text(failure_reason, "EXPERIMENT_FAILURE_REASON_INVALID", maximum=1_000)
            if failure_reason
            else None
        )
        if state_value is ExperimentState.COMPLETED and (reason is not None or not metrics):
            raise ResearchEvidenceError("EXPERIMENT_COMPLETION_INVALID")
        if state_value is ExperimentState.FAILED and reason is None:
            raise ResearchEvidenceError("EXPERIMENT_FAILURE_REQUIRED")
        configuration_hash = content_digest(config)
        result_payload = (
            state_value,
            tuple(metrics),
            tuple(artifacts),
            tuple(sorted(warnings)),
            reason,
        )
        result_hash = content_digest(result_payload)
        payload = (
            BROAD_UNIVERSE_ELIGIBILITY_EXPERIMENT_V1,
            _identity(run_id, "EXPERIMENT_RUN_ID_INVALID"),
            _identity(hypothesis_version_id, "EXPERIMENT_HYPOTHESIS_ID_INVALID"),
            _identity(snapshot_id, "EXPERIMENT_SNAPSHOT_ID_INVALID"),
            _text(executor_version, "EXPERIMENT_EXECUTOR_INVALID", maximum=160),
            configuration_hash,
            tuple(sorted(feature_policy_versions)),
            _text(code_revision, "EXPERIMENT_CODE_REVISION_INVALID", maximum=160),
            deterministic_seed,
            _aware(executed_at, "EXPERIMENT_TIMESTAMP_INVALID"),
            EvidenceClass(evidence_class),
            ReproducibilityStatus(reproducibility_status),
            result_hash,
        )
        return cls(
            payload[1],
            payload[2],
            payload[3],
            payload[4],
            config,
            configuration_hash,
            payload[6],
            payload[7],
            deterministic_seed,
            payload[9],
            state_value,
            tuple(sorted(warnings)),
            tuple(metrics),
            tuple(artifacts),
            payload[10],
            payload[11],
            reason,
            result_hash,
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ResearchExperimentOutcome:
    outcome_id: str
    run_id: str
    decision: ExperimentOutcomeDecision
    decided_at: datetime
    evidence_hashes: tuple[str, ...]
    threshold_rules: tuple[str, ...]
    warnings: tuple[str, ...]
    actor: str
    reason: str
    canonical_hash: str
    paper_eligible: bool = False
    live_eligible: bool = False
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        outcome_id: str,
        run: ResearchExperimentRun,
        decision: ExperimentOutcomeDecision | str,
        decided_at: datetime,
        evidence_hashes: tuple[str, ...],
        threshold_rules: tuple[str, ...],
        warnings: tuple[str, ...],
        actor: str,
        reason: str,
    ) -> ResearchExperimentOutcome:
        decision_value = ExperimentOutcomeDecision(decision)
        if run.state is not ExperimentState.COMPLETED:
            raise ResearchEvidenceError("OUTCOME_REQUIRES_COMPLETED_RUN")
        if (
            decision_value is ExperimentOutcomeDecision.PROMOTION_CANDIDATE
            and run.evidence_class is not EvidenceClass.GENUINE_PUBLIC
        ):
            raise ResearchEvidenceError("FIXTURE_CANNOT_BECOME_PROMOTION_CANDIDATE")
        hashes = tuple(
            sorted({_hash(item, "OUTCOME_EVIDENCE_HASH_INVALID") for item in evidence_hashes})
        )
        if not hashes:
            raise ResearchEvidenceError("OUTCOME_EVIDENCE_REQUIRED")
        payload = (
            DURABLE_RESEARCH_EVIDENCE_V1,
            _identity(outcome_id, "OUTCOME_ID_INVALID"),
            run.run_id,
            decision_value,
            _aware(decided_at, "OUTCOME_TIMESTAMP_INVALID"),
            hashes,
            tuple(
                sorted(
                    {
                        _text(item, "OUTCOME_THRESHOLD_INVALID", maximum=500)
                        for item in threshold_rules
                    }
                )
            ),
            tuple(
                sorted({_text(item, "OUTCOME_WARNING_INVALID", maximum=500) for item in warnings})
            ),
            _text(actor, "OUTCOME_ACTOR_INVALID", maximum=160),
            _text(reason, "OUTCOME_REASON_INVALID", maximum=1_000),
            False,
            False,
            "NONE",
        )
        return cls(
            payload[1],
            payload[2],
            decision_value,
            payload[4],
            hashes,
            payload[6],
            payload[7],
            payload[8],
            payload[9],
            content_digest(payload),
        )
