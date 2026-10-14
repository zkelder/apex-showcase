"""Adapter-free orchestration for historical market-data capture, dataset
snapshots, data-quality reporting, and point-in-time replay.

This module composes the *existing* ``application.real_securities_data``
provider-fetch lifecycle (one bounded, checkpointed HTTP operation per chunk)
rather than reimplementing it; it adds only the layer above one bounded fetch:
a multi-chunk plan, deterministic dataset-snapshot materialization, a
data-quality report, and the minimal point-in-time replay seam the next
milestone (a historical replay engine) can consume unchanged.

No service here has a Strategy, Opportunity, PAPER, LIVE, or provider-write
dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apex.application.real_securities_data import (
    FetchSecuritiesProviderOperation,
    FetchSecuritiesProviderOperationCommand,
    SecuritiesEvidencePort,
)
from apex.domain.historical_market_data import (
    HISTORICAL_DATA_WARNING,
    SURVIVORSHIP_WARNING,
    HistoricalCaptureChunk,
    HistoricalCaptureChunkState,
    HistoricalCapturePlan,
    HistoricalCapturePlanState,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalClock,
    HistoricalDataQualityReport,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
    SecurityCaptureOutcome,
    derive_plan_state,
    log_return,
    max_drawdown,
    point_in_time_slice,
    rolling_correlation,
    rolling_liquidity,
    rolling_volatility,
    simple_return,
)
from apex.domain.real_securities_data import (
    DERIVED_EOD_AVAILABLE_V1,
    SecuritiesProviderFetchOperation,
    SecuritiesProviderFetchOperationState,
    SecuritiesProviderFetchOperationType,
    SecuritiesUniverseSnapshot,
    evaluate_securities_data_quality,
)
from apex.domain.securities import (
    SecuritiesAssetClass,
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
    SecuritiesCorporateActionEvidence,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
)
from apex.domain.security_identity import StableSecurityIdentity


class HistoricalMarketDataApplicationError(RuntimeError):
    """A historical-data operation is blocked without creating financial state."""


class HistoricalMarketDataConflict(HistoricalMarketDataApplicationError):
    """An idempotent historical-data operation was replayed with different input."""


class HistoricalMarketDataNotFound(HistoricalMarketDataApplicationError):
    """A referenced plan/chunk/snapshot/report does not exist."""


# --------------------------------------------------------------------------
# Collaborator protocols
# --------------------------------------------------------------------------


class StableSecurityIdentitySource(Protocol):
    """Satisfied directly by ``PostgresSecurityIdentityRepository``."""

    def latest_stable_identities(self) -> tuple[StableSecurityIdentity, ...]: ...


@dataclass(frozen=True, slots=True)
class HistoricalChunkCanonicalizationResult:
    batch: SecuritiesMarketDataBatch
    instrument_bar_counts: tuple[tuple[str, int], ...]


class HistoricalCanonicalizerPort(Protocol):
    """Adapter-layer seam: turns one completed provider fetch operation into
    retained canonical Securities evidence (reusing ``ImportSecuritiesData``
    under the hood -- see ``adapters.alpaca_canonicalization``)."""

    def canonicalize_chunk(
        self,
        *,
        historical_operation: SecuritiesProviderFetchOperation,
        instruments: tuple[SecuritiesInstrument, ...],
        idempotency_key: str,
        imported_at: datetime,
    ) -> HistoricalChunkCanonicalizationResult: ...


class HistoricalMarketDataRepository(Protocol):
    def plan_by_idempotency(self, key: str) -> HistoricalCapturePlan | None: ...

    def save_plan(self, plan: HistoricalCapturePlan) -> HistoricalCapturePlan: ...

    def get_plan(self, plan_id: str) -> HistoricalCapturePlan | None: ...

    def list_plans(self) -> tuple[HistoricalCapturePlan, ...]: ...

    def list_chunks(self, plan_id: str) -> tuple[HistoricalCaptureChunk, ...]: ...

    def save_chunk(self, chunk: HistoricalCaptureChunk) -> HistoricalCaptureChunk: ...

    def snapshot_by_id(self, snapshot_id: str) -> HistoricalDatasetSnapshot | None: ...

    def save_snapshot(self, snapshot: HistoricalDatasetSnapshot) -> HistoricalDatasetSnapshot: ...

    def list_snapshots(self) -> tuple[HistoricalDatasetSnapshot, ...]: ...

    def quality_report_by_id(self, report_id: str) -> HistoricalDataQualityReport | None: ...

    def save_quality_report(
        self, report: HistoricalDataQualityReport
    ) -> HistoricalDataQualityReport: ...

    def list_quality_reports(self) -> tuple[HistoricalDataQualityReport, ...]: ...


# --------------------------------------------------------------------------
# Create a bounded, chunkable capture plan over a stable-security cohort
# --------------------------------------------------------------------------


class CreateHistoricalCapturePlanCommand:
    __slots__ = (
        "chunk_size",
        "created_at",
        "end_date",
        "etf_symbols",
        "evidence_class",
        "idempotency_key",
        "maximum_bytes",
        "maximum_instruments",
        "maximum_requests",
        "operator_action_id",
        "provider",
        "start_date",
        "symbols",
    )

    def __init__(
        self,
        *,
        symbols: tuple[str, ...],
        provider: str,
        start_date: str,
        end_date: str,
        chunk_size: int,
        maximum_instruments: int,
        maximum_requests: int,
        maximum_bytes: int,
        evidence_class: str,
        operator_action_id: str,
        idempotency_key: str,
        created_at: datetime,
        etf_symbols: frozenset[str] = frozenset(),
    ) -> None:
        self.symbols = symbols
        self.etf_symbols = etf_symbols
        self.provider = provider
        self.start_date = start_date
        self.end_date = end_date
        self.chunk_size = chunk_size
        self.maximum_instruments = maximum_instruments
        self.maximum_requests = maximum_requests
        self.maximum_bytes = maximum_bytes
        self.evidence_class = evidence_class
        self.operator_action_id = operator_action_id
        self.idempotency_key = idempotency_key
        self.created_at = created_at


class CreateHistoricalCapturePlan:
    """Resolve an explicit operator-declared symbol list against the current
    stable-security universe, then freeze a bounded capture plan.

    Symbols are resolved, never ranked or discovered -- the same
    "operator/system declares an exact fixed list up front" ergonomics
    ``SecuritiesUniverseSnapshot.fixed`` already uses, so this milestone adds
    no second universe-selection policy to review.
    """

    def __init__(
        self,
        *,
        repository: HistoricalMarketDataRepository,
        identity_source: StableSecurityIdentitySource,
    ) -> None:
        self._repository = repository
        self._identity_source = identity_source

    def execute(self, command: CreateHistoricalCapturePlanCommand) -> HistoricalCapturePlan:
        by_symbol: dict[str, list[StableSecurityIdentity]] = {}
        for identity in self._identity_source.latest_stable_identities():
            by_symbol.setdefault(identity.current_symbol, []).append(identity)
        unresolved: list[str] = []
        ambiguous: list[str] = []
        members: list[HistoricalCaptureUniverseMember] = []
        for raw_symbol in command.symbols:
            symbol = raw_symbol.strip().upper()
            candidates = by_symbol.get(symbol, [])
            if not candidates:
                unresolved.append(symbol)
                continue
            if len(candidates) > 1:
                ambiguous.append(symbol)
                continue
            asset_class = (
                SecuritiesAssetClass.ETF.value
                if symbol in command.etf_symbols
                else SecuritiesAssetClass.COMMON_STOCK.value
            )
            members.append(
                HistoricalCaptureUniverseMember.create(
                    stable_security_id=candidates[0].stable_security_id,
                    symbol_at_capture=symbol,
                    asset_class=asset_class,
                )
            )
        if unresolved or ambiguous:
            raise HistoricalMarketDataApplicationError(
                "HISTORICAL_CAPTURE_PLAN_SYMBOLS_UNRESOLVED: "
                f"unresolved={sorted(unresolved)} ambiguous={sorted(ambiguous)}"
            )
        policy = HistoricalCapturePolicy.create(
            provider=command.provider,
            start_date=command.start_date,
            end_date=command.end_date,
            chunk_size=command.chunk_size,
            maximum_instruments=command.maximum_instruments,
            maximum_requests=command.maximum_requests,
            maximum_bytes=command.maximum_bytes,
        )
        plan = HistoricalCapturePlan.draft(
            policy=policy,
            universe=members,
            evidence_class=command.evidence_class,
            operator_action_id=command.operator_action_id,
            idempotency_key=command.idempotency_key,
            created_at=command.created_at,
        )
        existing = self._repository.plan_by_idempotency(command.idempotency_key)
        if existing is not None:
            if existing.canonical_input_hash != plan.canonical_input_hash:
                raise HistoricalMarketDataConflict("HISTORICAL_CAPTURE_PLAN_IDEMPOTENCY_CONFLICT")
            return existing
        return self._repository.save_plan(plan)


# --------------------------------------------------------------------------
# Run (or resume) exactly one bounded, checkpointed chunk
# --------------------------------------------------------------------------


class RunHistoricalCaptureChunkCommand:
    __slots__ = ("ordinal", "plan_id", "requested_at")

    def __init__(self, *, plan_id: str, requested_at: datetime, ordinal: int | None = None) -> None:
        self.plan_id = plan_id
        self.ordinal = ordinal
        self.requested_at = requested_at


class RunHistoricalCaptureChunk:
    """Execute (or resume) exactly one chunk of a capture plan.

    Resuming without an explicit ``ordinal`` always advances to the next
    never-attempted chunk; retrying a specific failed/credentials-required
    ordinal requires supplying it explicitly. A completed chunk is idempotent
    and returned unchanged. Zero unauthorized requests are ever made: the
    underlying ``FetchSecuritiesProviderOperation`` checks credential
    availability before any network I/O.
    """

    def __init__(
        self,
        *,
        repository: HistoricalMarketDataRepository,
        fetch_operation: FetchSecuritiesProviderOperation,
        canonicalizer: HistoricalCanonicalizerPort,
        configuration_id: str,
    ) -> None:
        self._repository = repository
        self._fetch_operation = fetch_operation
        self._canonicalizer = canonicalizer
        self._configuration_id = configuration_id

    def execute(self, command: RunHistoricalCaptureChunkCommand) -> HistoricalCaptureChunk:
        plan = self._repository.get_plan(command.plan_id)
        if plan is None:
            raise HistoricalMarketDataNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")
        persisted = {item.ordinal: item for item in self._repository.list_chunks(command.plan_id)}
        total_chunks = len(plan.chunk_plan)
        ordinal = command.ordinal
        if ordinal is None:
            ordinal = len(persisted)
            if ordinal >= total_chunks:
                raise HistoricalMarketDataApplicationError(
                    "HISTORICAL_CAPTURE_PLAN_ALREADY_COMPLETE"
                )
        if not (0 <= ordinal < total_chunks):
            raise HistoricalMarketDataApplicationError(
                "HISTORICAL_CAPTURE_CHUNK_ORDINAL_OUT_OF_RANGE"
            )
        chunk = persisted.get(ordinal)
        if chunk is None:
            if ordinal != len(persisted):
                raise HistoricalMarketDataApplicationError(
                    "HISTORICAL_CAPTURE_CHUNK_OUT_OF_SEQUENCE"
                )
            previous_hash = None if ordinal == 0 else persisted[ordinal - 1].canonical_result_hash
            chunk = self._repository.save_chunk(
                HistoricalCaptureChunk.pending(
                    plan=plan, ordinal=ordinal, previous_chunk_hash=previous_hash
                )
            )
        if chunk.state is HistoricalCaptureChunkState.COMPLETED:
            return chunk

        running = self._repository.save_chunk(chunk.start(started_at=command.requested_at))
        members_by_instrument = {item.instrument_id: item for item in plan.universe}

        fetch_command = FetchSecuritiesProviderOperationCommand(
            configuration_id=self._configuration_id,
            operation_type=SecuritiesProviderFetchOperationType.HISTORICAL_DAILY_BARS,
            symbols=running.symbols,
            universe_id=None,
            start_at=_day_start(plan.policy.start_date),
            end_at=_day_end(plan.policy.end_date),
            idempotency_key=running.attempt_idempotency_key,
            requested_at=command.requested_at,
        )
        operation = self._fetch_operation.execute(fetch_command)
        if operation.state is SecuritiesProviderFetchOperationState.CREDENTIALS_REQUIRED:
            failed = running.fail(
                state=HistoricalCaptureChunkState.CREDENTIALS_REQUIRED,
                failure_code=operation.failure_code or "ALPACA_CREDENTIALS_REQUIRED",
                completed_at=command.requested_at,
                operation_id=operation.operation_id,
            )
            return self._repository.save_chunk(failed)
        if operation.state is not SecuritiesProviderFetchOperationState.COMPLETED:
            failed = running.fail(
                state=HistoricalCaptureChunkState.FAILED,
                failure_code=operation.failure_code or "HISTORICAL_CHUNK_FETCH_FAILED",
                completed_at=command.requested_at,
                operation_id=operation.operation_id,
            )
            return self._repository.save_chunk(failed)

        instruments = tuple(
            SecuritiesInstrument(
                instrument_id=instrument_id,
                symbol=members_by_instrument[instrument_id].symbol_at_capture,
                asset_class=members_by_instrument[instrument_id].asset_class,
                exchange_mic=plan.policy.calendar_identity,
                currency="USD",
                timezone="America/New_York",
                valid_from="0001-01-01",
                valid_to=None,
                source_identity=(
                    f"STABLE_SECURITY_ID:{members_by_instrument[instrument_id].stable_security_id}"
                    "+LISTING_VALIDITY_UNKNOWN"
                ),
            )
            for instrument_id in running.instrument_ids
        )
        try:
            result = self._canonicalizer.canonicalize_chunk(
                historical_operation=operation,
                instruments=instruments,
                idempotency_key=f"historical-canonicalize:{running.chunk_id}:attempt:{running.retry_count}",
                imported_at=command.requested_at,
            )
        except Exception as error:
            failed = running.fail(
                state=HistoricalCaptureChunkState.FAILED,
                failure_code=f"HISTORICAL_CHUNK_CANONICALIZATION_FAILED:{error}"[:500],
                completed_at=command.requested_at,
                operation_id=operation.operation_id,
            )
            return self._repository.save_chunk(failed)

        captured = tuple(iid for iid, count in result.instrument_bar_counts if count > 0)
        failed_symbols = tuple(
            sorted(
                members_by_instrument[iid].symbol_at_capture
                for iid, count in result.instrument_bar_counts
                if count == 0
            )
        )
        completed = running.complete(
            operation_id=operation.operation_id,
            raw_artifact_set_hash=operation.raw_artifact_set_hash or "",
            batch_id=result.batch.batch_id,
            captured_instrument_count=len(captured),
            failed_symbols=failed_symbols,
            completed_at=command.requested_at,
        )
        return self._repository.save_chunk(completed)


def _day_start(value: str) -> datetime:
    return datetime.fromisoformat(f"{value}T00:00:00+00:00")


def _day_end(value: str) -> datetime:
    return datetime.fromisoformat(f"{value}T23:59:59+00:00")


# --------------------------------------------------------------------------
# Materialize a deterministic dataset snapshot from a plan's chunks
# --------------------------------------------------------------------------


class MaterializeHistoricalDatasetSnapshotCommand:
    __slots__ = ("created_at", "plan_id")

    def __init__(self, *, plan_id: str, created_at: datetime) -> None:
        self.plan_id = plan_id
        self.created_at = created_at


class MaterializeHistoricalDatasetSnapshot:
    def __init__(
        self, *, repository: HistoricalMarketDataRepository, evidence: SecuritiesEvidencePort
    ) -> None:
        self._repository = repository
        self._evidence = evidence

    def execute(
        self, command: MaterializeHistoricalDatasetSnapshotCommand
    ) -> HistoricalDatasetSnapshot:
        plan = self._repository.get_plan(command.plan_id)
        if plan is None:
            raise HistoricalMarketDataNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")
        chunks = self._repository.list_chunks(command.plan_id)
        if not chunks:
            raise HistoricalMarketDataApplicationError(
                "HISTORICAL_SNAPSHOT_REQUIRES_ATTEMPTED_CHUNKS"
            )
        attempted = len(chunks)
        all_completed = attempted == len(plan.chunk_plan) and all(
            item.state is HistoricalCaptureChunkState.COMPLETED for item in chunks
        )
        completeness = (
            HistoricalDatasetCompletenessState.COMPLETE
            if all_completed
            else HistoricalDatasetCompletenessState.PARTIAL
        )
        batch_ids = sorted({item.batch_id for item in chunks if item.batch_id})
        operation_ids = sorted({item.operation_id for item in chunks if item.operation_id})
        bars: list[SecuritiesBarEvidence] = []
        for batch_id in batch_ids:
            _instruments, _sessions, batch_bars, _actions = self._evidence.get_dataset(batch_id)
            bars.extend(batch_bars)
        earliest = min((bar.period_start.date().isoformat() for bar in bars), default=None)
        latest = max((bar.period_start.date().isoformat() for bar in bars), default=None)
        snapshot = HistoricalDatasetSnapshot.create(
            plan=plan,
            source_batch_ids=batch_ids,
            source_operation_ids=operation_ids,
            observation_count=len(bars),
            completeness_state=completeness,
            earliest_observation_date=earliest,
            latest_observation_date=latest,
            created_at=command.created_at,
        )
        existing = self._repository.snapshot_by_id(snapshot.snapshot_id)
        if existing is not None:
            return existing
        return self._repository.save_snapshot(snapshot)


# --------------------------------------------------------------------------
# Historical data-quality report -- retained evidence, not log text
# --------------------------------------------------------------------------


class EvaluateHistoricalDataQualityCommand:
    __slots__ = ("created_at", "snapshot_id")

    def __init__(self, *, snapshot_id: str, created_at: datetime) -> None:
        self.snapshot_id = snapshot_id
        self.created_at = created_at


class EvaluateHistoricalDataQuality:
    def __init__(
        self, *, repository: HistoricalMarketDataRepository, evidence: SecuritiesEvidencePort
    ) -> None:
        self._repository = repository
        self._evidence = evidence

    def execute(self, command: EvaluateHistoricalDataQualityCommand) -> HistoricalDataQualityReport:
        snapshot = self._repository.snapshot_by_id(command.snapshot_id)
        if snapshot is None:
            raise HistoricalMarketDataNotFound("HISTORICAL_DATASET_SNAPSHOT_NOT_FOUND")
        plan = self._repository.get_plan(snapshot.plan_id)
        if plan is None:
            raise HistoricalMarketDataNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")
        chunks = self._repository.list_chunks(plan.plan_id)

        instruments_by_id: dict[str, SecuritiesInstrument] = {}
        sessions_by_id: dict[str, SecuritiesCalendarSession] = {}
        bars: list[SecuritiesBarEvidence] = []
        actions: list[SecuritiesCorporateActionEvidence] = []
        for batch_id in snapshot.source_batch_ids:
            batch_instruments, batch_sessions, batch_bars, batch_actions = (
                self._evidence.get_dataset(batch_id)
            )
            for instrument in batch_instruments:
                instruments_by_id.setdefault(instrument.instrument_id, instrument)
            for session in batch_sessions:
                sessions_by_id.setdefault(session.session_id, session)
            bars.extend(batch_bars)
            actions.extend(batch_actions)

        duplicate = missing = invalid_ohlc = nonpositive = volume_anomaly = 0
        if instruments_by_id and sessions_by_id:
            plumbing_universe = SecuritiesUniverseSnapshot.fixed(
                display_name=f"historical-quality-plumbing:{plan.plan_id}"[:120],
                symbols=tuple(sorted({item.symbol for item in instruments_by_id.values()})),
                reason="INTERNAL_REUSE_OF_EVALUATE_SECURITIES_DATA_QUALITY_V1",
                frozen_at=command.created_at,
                operator_action_id="historical-market-data-quality-v1",
                idempotency_key=f"historical-quality-universe:{snapshot.snapshot_id}",
            )
            evaluation = evaluate_securities_data_quality(
                market_data_batch_id=snapshot.snapshot_id,
                market_data_batch_hash=snapshot.canonical_hash,
                universe=plumbing_universe,
                instruments=tuple(instruments_by_id.values()),
                sessions=tuple(sessions_by_id.values()),
                bars=tuple(bars),
                corporate_actions=tuple(actions),
                availability_policy_id=DERIVED_EOD_AVAILABLE_V1,
                availability_policy={"delay_minutes": 30, "derived_not_provider_timestamp": True},
                revision_count=0,
                created_at=command.created_at,
            )
            duplicate = evaluation.duplicate_count
            missing = evaluation.missing_bar_session_count
            invalid_ohlc = evaluation.invalid_ohlc_count
            nonpositive = evaluation.zero_or_negative_price_count
            volume_anomaly = evaluation.zero_volume_count + evaluation.extreme_return_count

        captured_instrument_ids = {bar.instrument_id for bar in bars}
        chunk_by_instrument = {
            instrument_id: chunk for chunk in chunks for instrument_id in chunk.instrument_ids
        }
        security_outcomes: list[tuple[str, SecurityCaptureOutcome, str]] = []
        for member in plan.universe:
            if member.instrument_id in captured_instrument_ids:
                security_outcomes.append(
                    (member.stable_security_id, SecurityCaptureOutcome.CAPTURED, "bars retained")
                )
                continue
            chunk = chunk_by_instrument.get(member.instrument_id)
            if chunk is None:
                security_outcomes.append(
                    (
                        member.stable_security_id,
                        SecurityCaptureOutcome.UNIVERSE_MEMBERSHIP_UNKNOWN,
                        "chunk not yet attempted",
                    )
                )
            elif chunk.state is HistoricalCaptureChunkState.CREDENTIALS_REQUIRED:
                security_outcomes.append(
                    (
                        member.stable_security_id,
                        SecurityCaptureOutcome.REQUEST_FAILED,
                        chunk.failure_code or "credentials required",
                    )
                )
            elif chunk.state is HistoricalCaptureChunkState.FAILED:
                security_outcomes.append(
                    (
                        member.stable_security_id,
                        SecurityCaptureOutcome.REQUEST_FAILED,
                        chunk.failure_code or "request failed",
                    )
                )
            elif chunk.state is HistoricalCaptureChunkState.COMPLETED:
                security_outcomes.append(
                    (
                        member.stable_security_id,
                        SecurityCaptureOutcome.PROVIDER_NO_DATA,
                        "provider returned no bars for this symbol/date range",
                    )
                )
            else:
                security_outcomes.append(
                    (
                        member.stable_security_id,
                        SecurityCaptureOutcome.UNIVERSE_MEMBERSHIP_UNKNOWN,
                        "chunk not completed",
                    )
                )

        raw_hashes = tuple(
            sorted({item.raw_artifact_set_hash for item in chunks if item.raw_artifact_set_hash})
        )
        report = HistoricalDataQualityReport.create(
            snapshot=snapshot,
            requested_security_count=len(plan.universe),
            security_outcomes=security_outcomes,
            duplicate_timestamp_count=duplicate,
            missing_session_count=missing,
            invalid_ohlc_count=invalid_ohlc,
            nonpositive_price_count=nonpositive,
            volume_anomaly_count=volume_anomaly,
            raw_capture_hashes=raw_hashes,
            created_at=command.created_at,
        )
        existing = self._repository.quality_report_by_id(report.report_id)
        if existing is not None:
            return existing
        return self._repository.save_quality_report(report)


# --------------------------------------------------------------------------
# Minimal point-in-time replay seam (section 13)
# --------------------------------------------------------------------------


class HistoricalPointInTimeReplay:
    """The minimal interface the next milestone (a historical replay engine)
    can consume without redesigning this foundation: a historical clock, a
    point-in-time universe/dataset/evidence lookup, and feature calculation --
    every read routed through the same ``as_of``-gated domain guards used
    everywhere else in this module.
    """

    def __init__(
        self, *, repository: HistoricalMarketDataRepository, evidence: SecuritiesEvidencePort
    ) -> None:
        self._repository = repository
        self._evidence = evidence

    def universe_as_of(self, snapshot_id: str) -> tuple[HistoricalCaptureUniverseMember, ...]:
        """Universe lookup. Always pair with the snapshot's own
        ``point_in_time_universe_state``/``survivorship_state`` -- this is
        today's stable-identity cohort applied retroactively, not a verified
        point-in-time historical membership list."""

        plan = self._require_plan(self._require_snapshot(snapshot_id).plan_id)
        return plan.universe

    def evidence_as_of(
        self, *, snapshot_id: str, stable_security_id: str, clock: HistoricalClock
    ) -> tuple[SecuritiesBarEvidence, ...]:
        """Point-in-time dataset/evidence lookup: every bar returned has
        ``available_at <= clock.as_of``; nothing later can leak in."""

        snapshot = self._require_snapshot(snapshot_id)
        plan = self._require_plan(snapshot.plan_id)
        member = next(
            (item for item in plan.universe if item.stable_security_id == stable_security_id), None
        )
        if member is None:
            raise HistoricalMarketDataApplicationError("HISTORICAL_REPLAY_SECURITY_NOT_IN_UNIVERSE")
        bars: list[SecuritiesBarEvidence] = []
        for batch_id in snapshot.source_batch_ids:
            _instruments, _sessions, batch_bars, _actions = self._evidence.get_dataset(batch_id)
            bars.extend(bar for bar in batch_bars if bar.instrument_id == member.instrument_id)
        return point_in_time_slice(bars, as_of=clock.as_of)

    def calculate(
        self,
        *,
        snapshot_id: str,
        stable_security_id: str,
        clock: HistoricalClock,
        calculation: str,
        window: int | None = None,
    ) -> Decimal | None:
        """Feature calculation, dispatched only to the shared, as_of-gated
        primitives in ``domain.historical_market_data`` -- never a bespoke
        per-caller reimplementation."""

        bars = self.evidence_as_of(
            snapshot_id=snapshot_id, stable_security_id=stable_security_id, clock=clock
        )
        if calculation == "simple_return":
            return simple_return(bars, as_of=clock.as_of)
        if calculation == "log_return":
            return log_return(bars, as_of=clock.as_of)
        if calculation == "max_drawdown":
            return max_drawdown(bars, as_of=clock.as_of)
        if calculation == "rolling_volatility":
            if window is None:
                raise HistoricalMarketDataApplicationError("HISTORICAL_REPLAY_WINDOW_REQUIRED")
            return rolling_volatility(bars, as_of=clock.as_of, window=window)
        if calculation == "rolling_liquidity":
            if window is None:
                raise HistoricalMarketDataApplicationError("HISTORICAL_REPLAY_WINDOW_REQUIRED")
            return rolling_liquidity(bars, as_of=clock.as_of, window=window)
        raise HistoricalMarketDataApplicationError(
            f"HISTORICAL_REPLAY_UNKNOWN_CALCULATION:{calculation}"
        )

    def correlation_as_of(
        self,
        *,
        snapshot_id: str,
        stable_security_id_a: str,
        stable_security_id_b: str,
        clock: HistoricalClock,
        window: int,
    ) -> Decimal | None:
        bars_a = self.evidence_as_of(
            snapshot_id=snapshot_id, stable_security_id=stable_security_id_a, clock=clock
        )
        bars_b = self.evidence_as_of(
            snapshot_id=snapshot_id, stable_security_id=stable_security_id_b, clock=clock
        )
        return rolling_correlation(bars_a, bars_b, as_of=clock.as_of, window=window)

    def _require_snapshot(self, snapshot_id: str) -> HistoricalDatasetSnapshot:
        snapshot = self._repository.snapshot_by_id(snapshot_id)
        if snapshot is None:
            raise HistoricalMarketDataNotFound("HISTORICAL_DATASET_SNAPSHOT_NOT_FOUND")
        return snapshot

    def _require_plan(self, plan_id: str) -> HistoricalCapturePlan:
        plan = self._repository.get_plan(plan_id)
        if plan is None:
            raise HistoricalMarketDataNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")
        return plan


def historical_market_data_warnings() -> tuple[str, ...]:
    return (HISTORICAL_DATA_WARNING, SURVIVORSHIP_WARNING)


def plan_summary_state(
    *, repository: HistoricalMarketDataRepository, plan: HistoricalCapturePlan
) -> HistoricalCapturePlanState:
    chunks = repository.list_chunks(plan.plan_id)
    if not chunks:
        return HistoricalCapturePlanState.DRAFT
    return derive_plan_state(tuple(item.state for item in chunks))


class InMemoryHistoricalMarketDataRepository:
    """Reference implementation of ``HistoricalMarketDataRepository`` for
    tests and any future in-process caller -- mirrors the role
    ``InMemorySecuritiesRepository``/``InMemoryResearchEvidenceRepository``
    already play for their own domains."""

    def __init__(self) -> None:
        self._plans: dict[str, HistoricalCapturePlan] = {}
        self._plans_by_idempotency: dict[str, str] = {}
        self._chunks: dict[str, HistoricalCaptureChunk] = {}
        self._snapshots: dict[str, HistoricalDatasetSnapshot] = {}
        self._reports: dict[str, HistoricalDataQualityReport] = {}

    def plan_by_idempotency(self, key: str) -> HistoricalCapturePlan | None:
        plan_id = self._plans_by_idempotency.get(key)
        return None if plan_id is None else self._plans[plan_id]

    def save_plan(self, plan: HistoricalCapturePlan) -> HistoricalCapturePlan:
        self._plans[plan.plan_id] = plan
        self._plans_by_idempotency[plan.idempotency_key] = plan.plan_id
        return plan

    def get_plan(self, plan_id: str) -> HistoricalCapturePlan | None:
        return self._plans.get(plan_id)

    def list_plans(self) -> tuple[HistoricalCapturePlan, ...]:
        return tuple(sorted(self._plans.values(), key=lambda item: item.created_at))

    def list_chunks(self, plan_id: str) -> tuple[HistoricalCaptureChunk, ...]:
        return tuple(
            sorted(
                (item for item in self._chunks.values() if item.plan_id == plan_id),
                key=lambda item: item.ordinal,
            )
        )

    def save_chunk(self, chunk: HistoricalCaptureChunk) -> HistoricalCaptureChunk:
        self._chunks[chunk.chunk_id] = chunk
        return chunk

    def snapshot_by_id(self, snapshot_id: str) -> HistoricalDatasetSnapshot | None:
        return self._snapshots.get(snapshot_id)

    def save_snapshot(self, snapshot: HistoricalDatasetSnapshot) -> HistoricalDatasetSnapshot:
        self._snapshots.setdefault(snapshot.snapshot_id, snapshot)
        return self._snapshots[snapshot.snapshot_id]

    def list_snapshots(self) -> tuple[HistoricalDatasetSnapshot, ...]:
        return tuple(sorted(self._snapshots.values(), key=lambda item: item.created_at))

    def quality_report_by_id(self, report_id: str) -> HistoricalDataQualityReport | None:
        return self._reports.get(report_id)

    def save_quality_report(
        self, report: HistoricalDataQualityReport
    ) -> HistoricalDataQualityReport:
        self._reports.setdefault(report.report_id, report)
        return self._reports[report.report_id]

    def list_quality_reports(self) -> tuple[HistoricalDataQualityReport, ...]:
        return tuple(sorted(self._reports.values(), key=lambda item: item.created_at))
