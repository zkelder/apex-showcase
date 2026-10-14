"""Application services for bounded, authority-free historical crypto research."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol

from apex.domain.crypto_research import (
    AUTHORITY_WARNING,
    BTC_USD_DAILY_TREND_VALIDATION_V1,
    BTC_USD_PRIOR_DAY_SMA_FEATURES_V1,
    COINBASE_GRANULARITY,
    COINBASE_PRODUCT,
    CRYPTO_RESEARCH_COST_MODEL_V1,
    CRYPTO_VALIDATION_POLICY_V1,
    FIXED_DATA_END,
    FIXED_DATA_START,
    FIXED_VALIDATION_END,
    FIXED_VALIDATION_START,
    HYPOTHETICAL_WARNING,
    MAX_NETWORK_REQUESTS,
    MAX_TOTAL_RUNTIME_MILLISECONDS,
    PRIOR_DAY_WARNING,
    RETROSPECTIVE_WARNING,
    CostScenarioName,
    CryptoAttemptStatus,
    CryptoCaptureCheckpoint,
    CryptoCapturePartition,
    CryptoCaptureQuality,
    CryptoDailyCandle,
    CryptoExperimentOutcome,
    CryptoExperimentRun,
    CryptoHistoricalCapture,
    CryptoHistoricalSnapshot,
    CryptoHypothesisSpecification,
    CryptoPerformanceMetrics,
    CryptoRequestAttempt,
    CryptoResearchError,
    CryptoSignalDecision,
    PortfolioName,
    build_prior_day_signals,
    calculate_validation_metrics,
    capture_partitions,
)
from apex.domain.research_evidence import (
    CaptureStatus,
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    HypothesisStatus,
    ReproducibilityStatus,
    ResearchHypothesisVersion,
)
from apex.domain.values import content_digest


class CryptoResearchApplicationError(RuntimeError):
    pass


class CryptoResearchConflict(CryptoResearchApplicationError):
    pass


class CryptoResearchNotFound(CryptoResearchApplicationError):
    pass


@dataclass(frozen=True, slots=True)
class CoinbaseCandleInput:
    bucket_start: datetime
    open: object
    high: object
    low: object
    close: object
    volume: object


@dataclass(frozen=True, slots=True)
class CapturedCoinbasePartition:
    requested_at: datetime
    completed_at: datetime
    response_status: int
    raw_payload: bytes
    candles: tuple[CoinbaseCandleInput, ...]
    missing_bucket_starts: tuple[datetime, ...]
    rejected_records: int
    duplicate_records: int
    runtime_milliseconds: int
    source_locator: str


class CryptoSourceFailure(CryptoResearchApplicationError):
    def __init__(
        self,
        reason: str,
        *,
        capability_unavailable: bool,
        requested_at: datetime,
        completed_at: datetime,
        runtime_milliseconds: int,
        source_locator: str,
        response_status: int | None = None,
        raw_payload: bytes | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.capability_unavailable = capability_unavailable
        self.requested_at = requested_at
        self.completed_at = completed_at
        self.runtime_milliseconds = runtime_milliseconds
        self.source_locator = source_locator
        self.response_status = response_status
        self.raw_payload = raw_payload


class CoinbaseHistoricalSource(Protocol):
    evidence_class: EvidenceClass
    network_access: bool

    def capture_partition(
        self,
        *,
        capture: CryptoHistoricalCapture,
        partition: CryptoCapturePartition,
        requested_at: datetime,
    ) -> CapturedCoinbasePartition: ...


class CryptoResearchRepository(Protocol):
    def hypothesis_version(self, version_id: str) -> ResearchHypothesisVersion | None: ...
    def hypothesis_revision(
        self, hypothesis_id: str, revision: int
    ) -> ResearchHypothesisVersion | None: ...
    def hypothesis_specification(self, version_id: str) -> CryptoHypothesisSpecification | None: ...
    def save_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: CryptoHypothesisSpecification,
    ) -> CryptoHypothesisSpecification: ...
    def hypotheses(
        self,
    ) -> tuple[tuple[ResearchHypothesisVersion, CryptoHypothesisSpecification], ...]: ...

    def capture(self, capture_id: str) -> CryptoHistoricalCapture | None: ...
    def save_capture(self, capture: CryptoHistoricalCapture) -> CryptoHistoricalCapture: ...
    def append_attempt(
        self,
        *,
        expected_state_hash: str,
        capture: CryptoHistoricalCapture,
        attempt: CryptoRequestAttempt,
        raw_payload: bytes | None,
        candles: tuple[CryptoDailyCandle, ...],
        checkpoint: CryptoCaptureCheckpoint | None,
    ) -> CryptoHistoricalCapture: ...
    def captures(self) -> tuple[CryptoHistoricalCapture, ...]: ...
    def attempts(self, capture_id: str) -> tuple[CryptoRequestAttempt, ...]: ...
    def raw_response(self, attempt_id: str) -> bytes | None: ...
    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]: ...
    def checkpoints(self, capture_id: str) -> tuple[CryptoCaptureCheckpoint, ...]: ...

    def snapshot(self, snapshot_id: str) -> CryptoHistoricalSnapshot | None: ...
    def save_snapshot(self, snapshot: CryptoHistoricalSnapshot) -> CryptoHistoricalSnapshot: ...
    def snapshots(self) -> tuple[CryptoHistoricalSnapshot, ...]: ...

    def run(self, run_id: str) -> CryptoExperimentRun | None: ...
    def save_run(self, run: CryptoExperimentRun) -> CryptoExperimentRun: ...
    def runs(self) -> tuple[CryptoExperimentRun, ...]: ...

    def outcome(self, outcome_id: str) -> CryptoExperimentOutcome | None: ...
    def outcome_for_run(self, run_id: str) -> CryptoExperimentOutcome | None: ...
    def save_outcome(self, outcome: CryptoExperimentOutcome) -> CryptoExperimentOutcome: ...
    def outcomes(self) -> tuple[CryptoExperimentOutcome, ...]: ...


class InMemoryCryptoResearchRepository:
    def __init__(self) -> None:
        self._hypotheses: dict[str, ResearchHypothesisVersion] = {}
        self._specifications: dict[str, CryptoHypothesisSpecification] = {}
        self._captures: dict[str, CryptoHistoricalCapture] = {}
        self._attempts: dict[str, list[CryptoRequestAttempt]] = {}
        self._raw: dict[str, bytes] = {}
        self._candles: dict[str, list[CryptoDailyCandle]] = {}
        self._checkpoints: dict[str, list[CryptoCaptureCheckpoint]] = {}
        self._snapshots: dict[str, CryptoHistoricalSnapshot] = {}
        self._runs: dict[str, CryptoExperimentRun] = {}
        self._outcomes: dict[str, CryptoExperimentOutcome] = {}

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

    def hypothesis_specification(self, version_id: str) -> CryptoHypothesisSpecification | None:
        return self._specifications.get(version_id)

    def save_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: CryptoHypothesisSpecification,
    ) -> CryptoHypothesisSpecification:
        self._hypotheses[hypothesis.hypothesis_version_id] = hypothesis
        self._specifications[hypothesis.hypothesis_version_id] = specification
        return specification

    def hypotheses(
        self,
    ) -> tuple[tuple[ResearchHypothesisVersion, CryptoHypothesisSpecification], ...]:
        return tuple(
            (item, self._specifications[item.hypothesis_version_id])
            for item in sorted(
                self._hypotheses.values(),
                key=lambda value: (value.hypothesis_id, value.revision),
            )
        )

    def capture(self, capture_id: str) -> CryptoHistoricalCapture | None:
        return self._captures.get(capture_id)

    def save_capture(self, capture: CryptoHistoricalCapture) -> CryptoHistoricalCapture:
        prior = self._captures.get(capture.capture_id)
        if prior is not None and prior.state_hash != capture.state_hash:
            raise CryptoResearchConflict("CRYPTO_CAPTURE_IDENTITY_CONFLICT")
        self._captures[capture.capture_id] = prior or capture
        return prior or capture

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
        prior = self._captures.get(capture.capture_id)
        if prior is None or prior.state_hash != expected_state_hash:
            raise CryptoResearchConflict("CRYPTO_CAPTURE_CHECKPOINT_CONFLICT")
        attempts = self._attempts.setdefault(capture.capture_id, [])
        if attempt.request_ordinal != len(attempts):
            raise CryptoResearchConflict("CRYPTO_CAPTURE_REQUEST_ORDINAL_CONFLICT")
        completed_for_partition = {
            item.partition_ordinal
            for item in attempts
            if item.status is CryptoAttemptStatus.COMPLETED
        }
        if (
            attempt.status is CryptoAttemptStatus.COMPLETED
            and attempt.partition_ordinal in completed_for_partition
        ):
            raise CryptoResearchConflict("CRYPTO_CAPTURE_PARTITION_ALREADY_COMPLETED")
        existing = self._candles.setdefault(capture.capture_id, [])
        existing_buckets = {item.bucket_start for item in existing}
        if any(item.bucket_start in existing_buckets for item in candles):
            raise CryptoResearchConflict("CRYPTO_CAPTURE_DUPLICATE_BUCKET_CONFLICT")
        attempts.append(attempt)
        existing.extend(candles)
        if raw_payload is not None:
            self._raw[attempt.attempt_id] = raw_payload
        if checkpoint is not None:
            self._checkpoints.setdefault(capture.capture_id, []).append(checkpoint)
        self._captures[capture.capture_id] = capture
        return capture

    def captures(self) -> tuple[CryptoHistoricalCapture, ...]:
        return tuple(sorted(self._captures.values(), key=lambda item: item.capture_id))

    def attempts(self, capture_id: str) -> tuple[CryptoRequestAttempt, ...]:
        return tuple(self._attempts.get(capture_id, ()))

    def raw_response(self, attempt_id: str) -> bytes | None:
        return self._raw.get(attempt_id)

    def candles(self, capture_id: str) -> tuple[CryptoDailyCandle, ...]:
        return tuple(sorted(self._candles.get(capture_id, ()), key=lambda item: item.bucket_start))

    def checkpoints(self, capture_id: str) -> tuple[CryptoCaptureCheckpoint, ...]:
        return tuple(self._checkpoints.get(capture_id, ()))

    def snapshot(self, snapshot_id: str) -> CryptoHistoricalSnapshot | None:
        return self._snapshots.get(snapshot_id)

    def save_snapshot(self, snapshot: CryptoHistoricalSnapshot) -> CryptoHistoricalSnapshot:
        prior = self._snapshots.get(snapshot.snapshot_id)
        if prior is not None and prior.canonical_hash != snapshot.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_SNAPSHOT_IDENTITY_CONFLICT")
        self._snapshots[snapshot.snapshot_id] = prior or snapshot
        return prior or snapshot

    def snapshots(self) -> tuple[CryptoHistoricalSnapshot, ...]:
        return tuple(sorted(self._snapshots.values(), key=lambda item: item.snapshot_id))

    def run(self, run_id: str) -> CryptoExperimentRun | None:
        return self._runs.get(run_id)

    def save_run(self, run: CryptoExperimentRun) -> CryptoExperimentRun:
        prior = self._runs.get(run.run_id)
        if prior is not None and prior.canonical_hash != run.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_RUN_IDENTITY_CONFLICT")
        self._runs[run.run_id] = prior or run
        return prior or run

    def runs(self) -> tuple[CryptoExperimentRun, ...]:
        return tuple(sorted(self._runs.values(), key=lambda item: item.run_id))

    def outcome(self, outcome_id: str) -> CryptoExperimentOutcome | None:
        return self._outcomes.get(outcome_id)

    def outcome_for_run(self, run_id: str) -> CryptoExperimentOutcome | None:
        return next((item for item in self._outcomes.values() if item.run_id == run_id), None)

    def save_outcome(self, outcome: CryptoExperimentOutcome) -> CryptoExperimentOutcome:
        prior = self._outcomes.get(outcome.outcome_id)
        run_prior = self.outcome_for_run(outcome.run_id)
        if (prior is not None and prior.canonical_hash != outcome.canonical_hash) or (
            run_prior is not None and run_prior.canonical_hash != outcome.canonical_hash
        ):
            raise CryptoResearchConflict("CRYPTO_OUTCOME_IDENTITY_CONFLICT")
        self._outcomes[outcome.outcome_id] = prior or outcome
        return prior or outcome

    def outcomes(self) -> tuple[CryptoExperimentOutcome, ...]:
        return tuple(sorted(self._outcomes.values(), key=lambda item: item.outcome_id))


class RegisterCryptoHypothesis:
    def __init__(self, repository: CryptoResearchRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        hypothesis_id: str,
        revision: int,
        actor: str,
        created_at: datetime,
        supersedes_version_id: str | None = None,
    ) -> tuple[ResearchHypothesisVersion, CryptoHypothesisSpecification]:
        question = (
            "A long-or-cash BTC-USD daily 20/100 prior-close SMA trend rule will "
            "produce a non-negative net return and lower maximum drawdown than "
            "buy-and-hold from 2025-07-01 through 2026-06-30 under 60 bps fees "
            "and 10 bps slippage per side."
        )
        motivation = (
            "Test one predeclared out-of-sample rule over retained Coinbase daily "
            "candles from 2024-07-01T00:00:00Z through 2026-07-01T00:00:00Z. "
            "Signals use only prior completed candles; the comparison baselines are "
            "cash and buy-and-hold; success also requires complete evidence, exact "
            "replay, and at least one completed round trip. Authority remains NONE."
        )
        candidate = ResearchHypothesisVersion.create(
            hypothesis_id=hypothesis_id,
            revision=revision,
            question=question,
            motivation=motivation,
            actor=actor,
            market_scope=(
                "COINBASE_PUBLIC_SPOT",
                "BTC-USD",
                "DAILY_CANDLES",
                "2024-07-01/2026-07-01",
                "VALIDATION_2025-07-01/2026-07-01",
                "NO_PAPER_OR_LIVE_AUTHORITY",
            ),
            created_at=created_at,
            status=HypothesisStatus.REGISTERED,
            supersedes_version_id=supersedes_version_id,
        )
        prior = self._repository.hypothesis_revision(hypothesis_id, revision)
        if prior is not None:
            specification = self._repository.hypothesis_specification(prior.hypothesis_version_id)
            if prior.canonical_hash != candidate.canonical_hash or specification is None:
                raise CryptoResearchConflict("CRYPTO_HYPOTHESIS_REVISION_CONFLICT")
            return prior, specification
        if revision > 1:
            parent = self._repository.hypothesis_version(supersedes_version_id or "")
            if (
                parent is None
                or parent.hypothesis_id != hypothesis_id
                or parent.revision != revision - 1
            ):
                raise CryptoResearchConflict("CRYPTO_HYPOTHESIS_PARENT_CONFLICT")
        specification = CryptoHypothesisSpecification.fixed(candidate)
        self._repository.save_hypothesis(candidate, specification)
        return candidate, specification


def _capture_state_hash(capture: CryptoHistoricalCapture) -> str:
    return content_digest(
        (
            capture.request_hash,
            capture.status,
            capture.captured_at,
            capture.completed_at,
            capture.completed_partitions,
            capture.request_count,
            capture.accepted_buckets,
            capture.missing_bucket_starts,
            capture.rejected_records,
            capture.duplicate_records,
            capture.total_raw_bytes,
            capture.total_runtime_milliseconds,
            capture.quality,
            capture.limitations,
            capture.failure_reason,
        )
    )


class CaptureCoinbaseHistory:
    def __init__(
        self,
        repository: CryptoResearchRepository,
        source: CoinbaseHistoricalSource,
    ) -> None:
        self._repository = repository
        self._source = source

    def start(
        self,
        *,
        capture_id: str,
        start: datetime,
        end: datetime,
        requested_at: datetime,
    ) -> CryptoHistoricalCapture:
        candidate = CryptoHistoricalCapture.initial(
            capture_id=capture_id,
            start=start,
            end=end,
            evidence_class=self._source.evidence_class,
            requested_at=requested_at,
        )
        prior = self._repository.capture(capture_id)
        if prior is not None:
            if prior.request_hash != candidate.request_hash:
                raise CryptoResearchConflict("CRYPTO_CAPTURE_IDENTITY_CONFLICT")
            return prior
        self._repository.save_capture(candidate)
        return self.resume(capture_id=capture_id, requested_at=requested_at)

    def resume(self, *, capture_id: str, requested_at: datetime) -> CryptoHistoricalCapture:
        capture = self._repository.capture(capture_id)
        if capture is None:
            raise CryptoResearchNotFound("CRYPTO_CAPTURE_NOT_FOUND")
        if capture.evidence_class is not self._source.evidence_class:
            raise CryptoResearchConflict("CRYPTO_CAPTURE_EVIDENCE_CLASS_CONFLICT")
        if capture.status is not CaptureStatus.RUNNING:
            return capture
        if (
            capture.request_count >= MAX_NETWORK_REQUESTS
            or capture.total_runtime_milliseconds >= MAX_TOTAL_RUNTIME_MILLISECONDS
        ):
            failed = replace(
                capture,
                status=CaptureStatus.FAILED,
                completed_at=requested_at,
                quality=CryptoCaptureQuality.FAILED,
                failure_reason="CRYPTO_CAPTURE_REQUEST_OR_RUNTIME_BUDGET_EXHAUSTED",
            )
            failed = replace(failed, state_hash=_capture_state_hash(failed))
            return failed
        partition = capture_partitions(capture.start, capture.end)[capture.completed_partitions]
        try:
            page = self._source.capture_partition(
                capture=capture,
                partition=partition,
                requested_at=requested_at,
            )
        except CryptoSourceFailure as error:
            return self._record_failure(capture, partition, error)
        return self._record_success(capture, partition, page)

    def _record_success(
        self,
        capture: CryptoHistoricalCapture,
        partition: CryptoCapturePartition,
        page: CapturedCoinbasePartition,
    ) -> CryptoHistoricalCapture:
        raw_hash = hashlib.sha256(page.raw_payload).hexdigest()
        attempt = CryptoRequestAttempt.create(
            capture_id=capture.capture_id,
            request_ordinal=capture.request_count,
            partition=partition,
            requested_at=page.requested_at,
            completed_at=page.completed_at,
            status=CryptoAttemptStatus.COMPLETED,
            response_status=page.response_status,
            raw_response_hash=raw_hash,
            raw_bytes=len(page.raw_payload),
            accepted_buckets=len(page.candles),
            missing_bucket_starts=page.missing_bucket_starts,
            rejected_records=page.rejected_records,
            duplicate_records=page.duplicate_records,
            runtime_milliseconds=page.runtime_milliseconds,
            source_locator=page.source_locator,
        )
        candles = tuple(
            CryptoDailyCandle.create(
                capture_id=capture.capture_id,
                attempt_id=attempt.attempt_id,
                evidence_class=capture.evidence_class,
                bucket_start=item.bucket_start,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
                ingested_at=page.completed_at,
                raw_response_hash=raw_hash,
            )
            for item in page.candles
        )
        if any(not partition.start <= candle.bucket_start < partition.end for candle in candles):
            raise CryptoResearchConflict("CRYPTO_CAPTURE_CANDLE_OUTSIDE_PARTITION")
        completed_partitions = capture.completed_partitions + 1
        is_complete = completed_partitions == capture.expected_partitions
        missing = tuple(
            sorted(set(capture.missing_bucket_starts) | set(page.missing_bucket_starts))
        )
        rejected = capture.rejected_records + page.rejected_records
        duplicates = capture.duplicate_records + page.duplicate_records
        quality = (
            CryptoCaptureQuality.VALID
            if is_complete and not missing and rejected == 0 and duplicates == 0
            else CryptoCaptureQuality.INCOMPLETE
        )
        updated = replace(
            capture,
            status=CaptureStatus.COMPLETED if is_complete else CaptureStatus.RUNNING,
            captured_at=capture.captured_at or page.completed_at,
            completed_at=page.completed_at if is_complete else None,
            completed_partitions=completed_partitions,
            request_count=capture.request_count + 1,
            accepted_buckets=capture.accepted_buckets + len(candles),
            missing_bucket_starts=missing,
            rejected_records=rejected,
            duplicate_records=duplicates,
            total_raw_bytes=capture.total_raw_bytes + len(page.raw_payload),
            total_runtime_milliseconds=(
                capture.total_runtime_milliseconds + page.runtime_milliseconds
            ),
            quality=quality,
            failure_reason=None,
        )
        updated = replace(updated, state_hash=_capture_state_hash(updated))
        checkpoint = CryptoCaptureCheckpoint.create(capture=updated, created_at=page.completed_at)
        return self._repository.append_attempt(
            expected_state_hash=capture.state_hash,
            capture=updated,
            attempt=attempt,
            raw_payload=page.raw_payload,
            candles=candles,
            checkpoint=checkpoint,
        )

    def _record_failure(
        self,
        capture: CryptoHistoricalCapture,
        partition: CryptoCapturePartition,
        failure: CryptoSourceFailure,
    ) -> CryptoHistoricalCapture:
        raw = failure.raw_payload
        raw_hash = hashlib.sha256(raw).hexdigest() if raw else None
        attempt_status = (
            CryptoAttemptStatus.CAPABILITY_UNAVAILABLE
            if failure.capability_unavailable
            else CryptoAttemptStatus.FAILED
        )
        attempt = CryptoRequestAttempt.create(
            capture_id=capture.capture_id,
            request_ordinal=capture.request_count,
            partition=partition,
            requested_at=failure.requested_at,
            completed_at=failure.completed_at,
            status=attempt_status,
            response_status=failure.response_status,
            raw_response_hash=raw_hash,
            raw_bytes=len(raw or b""),
            accepted_buckets=0,
            missing_bucket_starts=(),
            rejected_records=0,
            duplicate_records=0,
            runtime_milliseconds=failure.runtime_milliseconds,
            source_locator=failure.source_locator,
            failure_reason=failure.reason,
        )
        terminal = (
            not failure.capability_unavailable
            or capture.completed_partitions == 0
            or capture.request_count + 1 >= MAX_NETWORK_REQUESTS
        )
        status = (
            CaptureStatus.CAPABILITY_UNAVAILABLE
            if terminal and failure.capability_unavailable and capture.completed_partitions == 0
            else CaptureStatus.FAILED
            if terminal
            else CaptureStatus.RUNNING
        )
        quality = (
            CryptoCaptureQuality.UNAVAILABLE
            if status is CaptureStatus.CAPABILITY_UNAVAILABLE
            else CryptoCaptureQuality.FAILED
            if status is CaptureStatus.FAILED
            else CryptoCaptureQuality.INCOMPLETE
        )
        updated = replace(
            capture,
            status=status,
            completed_at=failure.completed_at if terminal else None,
            request_count=capture.request_count + 1,
            total_raw_bytes=capture.total_raw_bytes + len(raw or b""),
            total_runtime_milliseconds=(
                capture.total_runtime_milliseconds + failure.runtime_milliseconds
            ),
            quality=quality,
            failure_reason=failure.reason,
        )
        updated = replace(updated, state_hash=_capture_state_hash(updated))
        return self._repository.append_attempt(
            expected_state_hash=capture.state_hash,
            capture=updated,
            attempt=attempt,
            raw_payload=raw,
            candles=(),
            checkpoint=None,
        )


class MaterializeCryptoHistoricalSnapshot:
    def __init__(self, repository: CryptoResearchRepository) -> None:
        self._repository = repository

    def execute(
        self, *, snapshot_id: str, capture_id: str, created_at: datetime
    ) -> CryptoHistoricalSnapshot:
        capture = self._repository.capture(capture_id)
        if capture is None:
            raise CryptoResearchNotFound("CRYPTO_CAPTURE_NOT_FOUND")
        candidate = CryptoHistoricalSnapshot.create(
            snapshot_id=snapshot_id,
            capture=capture,
            candles=self._repository.candles(capture_id),
            attempts=self._repository.attempts(capture_id),
            created_at=created_at,
        )
        prior = self._repository.snapshot(snapshot_id)
        if prior is not None:
            if prior.manifest_hash != candidate.manifest_hash:
                raise CryptoResearchConflict("CRYPTO_SNAPSHOT_IDENTITY_CONFLICT")
            return prior
        return self._repository.save_snapshot(candidate)


def _configuration(
    specification: CryptoHypothesisSpecification,
) -> tuple[tuple[str, object], ...]:
    return (
        ("product_id", COINBASE_PRODUCT),
        ("granularity", COINBASE_GRANULARITY),
        ("data_start", FIXED_DATA_START),
        ("data_end", FIXED_DATA_END),
        ("validation_start", FIXED_VALIDATION_START),
        ("validation_end", FIXED_VALIDATION_END),
        ("fast_sma_days", specification.fast_sma_days),
        ("slow_sma_days", specification.slow_sma_days),
        ("starting_notional", "10000"),
        ("conservative_fee_bps_per_side", specification.conservative_fee_bps),
        (
            "conservative_slippage_bps_per_side",
            specification.conservative_slippage_bps,
        ),
        ("annualization_days", 365),
        ("terminal_research_liquidation", True),
    )


class ExecuteCryptoTrendValidation:
    def __init__(self, repository: CryptoResearchRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        run_id: str,
        hypothesis_version_id: str,
        snapshot_id: str,
        code_revision: str,
        executed_at: datetime,
    ) -> CryptoExperimentRun:
        candidate = self._build(
            run_id=run_id,
            hypothesis_version_id=hypothesis_version_id,
            snapshot_id=snapshot_id,
            code_revision=code_revision,
            executed_at=executed_at,
        )
        prior = self._repository.run(run_id)
        if prior is not None:
            if prior.canonical_hash != candidate.canonical_hash:
                raise CryptoResearchConflict("CRYPTO_EXPERIMENT_RUN_CONFLICT")
            return prior
        return self._repository.save_run(candidate)

    def replay(self, run_id: str) -> CryptoExperimentRun:
        prior = self._repository.run(run_id)
        if prior is None:
            raise CryptoResearchNotFound("CRYPTO_EXPERIMENT_RUN_NOT_FOUND")
        replay = self._build(
            run_id=prior.run_id,
            hypothesis_version_id=prior.hypothesis_version_id,
            snapshot_id=prior.snapshot_id,
            code_revision=prior.code_revision,
            executed_at=prior.executed_at,
        )
        if replay.result_hash != prior.result_hash or replay.canonical_hash != prior.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_EXPERIMENT_REPLAY_MISMATCH")
        return prior

    def _build(
        self,
        *,
        run_id: str,
        hypothesis_version_id: str,
        snapshot_id: str,
        code_revision: str,
        executed_at: datetime,
    ) -> CryptoExperimentRun:
        hypothesis = self._repository.hypothesis_version(hypothesis_version_id)
        specification = self._repository.hypothesis_specification(hypothesis_version_id)
        snapshot = self._repository.snapshot(snapshot_id)
        if hypothesis is None or specification is None:
            raise CryptoResearchNotFound("CRYPTO_HYPOTHESIS_NOT_FOUND")
        if snapshot is None:
            raise CryptoResearchNotFound("CRYPTO_SNAPSHOT_NOT_FOUND")
        candles = self._repository.candles(snapshot.capture_id)
        fixed_specification = CryptoHypothesisSpecification.fixed(hypothesis)
        fixed_latest_bucket = FIXED_DATA_END - timedelta(days=1)
        fixed_bucket_count = (FIXED_DATA_END - FIXED_DATA_START).days
        if specification.canonical_hash != fixed_specification.canonical_hash:
            raise CryptoResearchConflict("CRYPTO_HYPOTHESIS_SPECIFICATION_NOT_FIXED")
        if (
            snapshot.product_id != COINBASE_PRODUCT
            or snapshot.start != FIXED_DATA_START
            or snapshot.end != FIXED_DATA_END
            or snapshot.earliest_bucket != FIXED_DATA_START
            or snapshot.latest_bucket != fixed_latest_bucket
            or snapshot.expected_buckets != fixed_bucket_count
        ):
            raise CryptoResearchConflict("CRYPTO_SNAPSHOT_FIXED_WINDOW_MISMATCH")
        configuration = _configuration(specification)
        configuration_hash = content_digest(configuration)
        warnings = (
            RETROSPECTIVE_WARNING,
            HYPOTHETICAL_WARNING,
            PRIOR_DAY_WARNING,
            AUTHORITY_WARNING,
        )
        complete = (
            snapshot.quality is CryptoCaptureQuality.VALID
            and snapshot.accepted_buckets == snapshot.expected_buckets
            and not snapshot.missing_bucket_starts
            and snapshot.rejected_records == 0
            and snapshot.duplicate_records == 0
        )
        signals: tuple[CryptoSignalDecision, ...] = ()
        metrics: tuple[CryptoPerformanceMetrics, ...] = ()
        failure_reason = None
        state = ExperimentState.COMPLETED
        if complete:
            try:
                signals = build_prior_day_signals(
                    candles,
                    validation_start=FIXED_VALIDATION_START,
                    validation_end=FIXED_VALIDATION_END,
                )
                metrics = calculate_validation_metrics(candles, signals)
            except CryptoResearchError as error:
                complete = False
                state = ExperimentState.FAILED
                failure_reason = str(error)
        else:
            failure_reason = "CRYPTO_EXPERIMENT_REQUIRED_EVIDENCE_INCOMPLETE"
        result_payload = (
            BTC_USD_DAILY_TREND_VALIDATION_V1,
            snapshot.manifest_hash,
            hypothesis.canonical_hash,
            specification.canonical_hash,
            configuration_hash,
            BTC_USD_PRIOR_DAY_SMA_FEATURES_V1,
            CRYPTO_RESEARCH_COST_MODEL_V1,
            CRYPTO_VALIDATION_POLICY_V1,
            signals,
            metrics,
            warnings,
            complete,
            state,
            failure_reason,
        )
        result_hash = content_digest(result_payload)
        canonical_payload = (
            run_id,
            hypothesis_version_id,
            snapshot_id,
            code_revision,
            executed_at,
            result_hash,
        )
        return CryptoExperimentRun(
            run_id=run_id,
            hypothesis_version_id=hypothesis_version_id,
            snapshot_id=snapshot_id,
            executor_version=BTC_USD_DAILY_TREND_VALIDATION_V1,
            configuration=configuration,
            configuration_hash=configuration_hash,
            feature_definition_version=BTC_USD_PRIOR_DAY_SMA_FEATURES_V1,
            cost_model_version=CRYPTO_RESEARCH_COST_MODEL_V1,
            validation_policy_version=CRYPTO_VALIDATION_POLICY_V1,
            code_revision=code_revision,
            deterministic_seed=0,
            executed_at=executed_at,
            state=state,
            complete=complete,
            reproducibility_status=ReproducibilityStatus.REPRODUCIBLE,
            evidence_class=snapshot.evidence_class,
            signals=signals,
            metrics=metrics,
            warnings=warnings,
            failure_reason=failure_reason,
            result_hash=result_hash,
            canonical_hash=content_digest(canonical_payload),
        )


def _metric(
    run: CryptoExperimentRun,
    portfolio: PortfolioName,
    scenario: CostScenarioName,
) -> CryptoPerformanceMetrics:
    return next(
        item for item in run.metrics if item.portfolio is portfolio and item.scenario is scenario
    )


class RecordCryptoExperimentOutcome:
    def __init__(self, repository: CryptoResearchRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        outcome_id: str,
        run_id: str,
        actor: str,
        decided_at: datetime,
    ) -> CryptoExperimentOutcome:
        run = self._repository.run(run_id)
        if run is None:
            raise CryptoResearchNotFound("CRYPTO_EXPERIMENT_RUN_NOT_FOUND")
        prior = self._repository.outcome_for_run(run_id)
        if prior is not None:
            if prior.outcome_id != outcome_id:
                raise CryptoResearchConflict("CRYPTO_OUTCOME_RUN_ALREADY_DECIDED")
            return prior
        if not run.complete or run.state is not ExperimentState.COMPLETED:
            decision = ExperimentOutcomeDecision.DEFERRED
            reason = (
                "Required daily coverage or experiment integrity was incomplete; "
                "the predeclared validation policy cannot decide the hypothesis."
            )
        else:
            trend = _metric(
                run,
                PortfolioName.TREND_RULE,
                CostScenarioName.CONSERVATIVE_RESEARCH,
            )
            buy_hold = _metric(
                run,
                PortfolioName.BUY_AND_HOLD,
                CostScenarioName.CONSERVATIVE_RESEARCH,
            )
            passed = (
                trend.net_return >= 0
                and trend.maximum_drawdown < buy_hold.maximum_drawdown
                and trend.completed_round_trips >= 1
                and run.reproducibility_status is ReproducibilityStatus.REPRODUCIBLE
            )
            if passed and run.evidence_class is EvidenceClass.HISTORICAL_PUBLIC:
                decision = ExperimentOutcomeDecision.PROMOTION_CANDIDATE
                reason = (
                    "The conservative historical validation met the predeclared "
                    "net-return, drawdown, completeness, replay, and round-trip rules; "
                    "only another validation phase is justified."
                )
            elif passed:
                decision = ExperimentOutcomeDecision.DEFERRED
                reason = (
                    "The deterministic fixture met diagnostic thresholds, but fixture "
                    "evidence cannot create a promotion candidate."
                )
            else:
                decision = ExperimentOutcomeDecision.REJECTED
                reason = (
                    "Complete evidence contradicted at least one predeclared conservative "
                    "net-return, drawdown, reproducibility, or round-trip threshold."
                )
        threshold_rules = (
            "conservative_net_return>=0",
            "trend_maximum_drawdown<buy_and_hold_maximum_drawdown",
            "complete_required_evidence=true",
            "reproducibility_status=REPRODUCIBLE",
            "completed_round_trips>=1",
            "PAPER_ELIGIBLE=false",
            "LIVE_ELIGIBLE=false",
        )
        payload = (
            outcome_id,
            run_id,
            decision,
            decided_at,
            actor,
            reason,
            (run.canonical_hash, run.result_hash),
            threshold_rules,
            run.warnings,
            False,
            False,
        )
        candidate = CryptoExperimentOutcome(
            outcome_id=outcome_id,
            run_id=run_id,
            decision=decision,
            decided_at=decided_at,
            actor=actor,
            reason=reason,
            evidence_hashes=(run.canonical_hash, run.result_hash),
            threshold_rules=threshold_rules,
            warnings=run.warnings,
            paper_eligible=False,
            live_eligible=False,
            canonical_hash=content_digest(payload),
        )
        return self._repository.save_outcome(candidate)


def crypto_research_workspace(repository: CryptoResearchRepository) -> dict[str, object]:
    captures = repository.captures()
    return {
        "provider_capability": {
            "provider": "COINBASE",
            "endpoint_class": "ADVANCED_TRADE_PUBLIC_MARKET_CANDLES_GET_ONLY",
            "product": COINBASE_PRODUCT,
            "granularity": COINBASE_GRANULARITY,
            "authentication": "NONE",
            "network_writes": 0,
        },
        "hypotheses": repository.hypotheses(),
        "captures": captures,
        "attempts": tuple(
            item for capture in captures for item in repository.attempts(capture.capture_id)
        ),
        "checkpoints": tuple(
            item for capture in captures for item in repository.checkpoints(capture.capture_id)
        ),
        "candle_count": sum(item.accepted_buckets for item in captures),
        "snapshots": repository.snapshots(),
        "runs": repository.runs(),
        "outcomes": repository.outcomes(),
        "warnings": (
            RETROSPECTIVE_WARNING,
            HYPOTHETICAL_WARNING,
            AUTHORITY_WARNING,
        ),
        "orders": 0,
        "positions": 0,
        "reservations": 0,
        "broker_writes": 0,
        "journals": 0,
        "paper_eligible": False,
        "live_eligible": False,
        "authority": "NONE",
    }
