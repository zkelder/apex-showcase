"""Adapter-free Securities Stage 1 import, SHADOW research, and conformance."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from apex.domain.securities import (
    ExternalSecuritiesEngineRequest,
    ExternalSecuritiesEngineRun,
    SecuritiesBarEvidence,
    SecuritiesCalendarSession,
    SecuritiesConformanceClassification,
    SecuritiesCorporateActionEvidence,
    SecuritiesDataBatchState,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
    SecuritiesResearchConformanceReport,
    SecuritiesResearchRun,
    SecuritiesResearchRunState,
    SecuritiesSignal,
    SecuritiesStrategyKind,
    SecuritiesStrategySpecification,
    build_baseline_signals,
    calculate_research_metrics,
    compare_external_evidence,
    validate_dataset_evidence,
)
from apex.domain.values import content_digest, deterministic_id


class SecuritiesApplicationError(RuntimeError):
    """A Stage 1 application boundary rejected an operation."""


class SecuritiesIdempotencyConflictError(SecuritiesApplicationError):
    """A key was replayed with a different canonical request."""


class SecuritiesCampaignBoundary(Protocol):
    def assert_research_allowed(self, campaign_id: str) -> None: ...


class SecuritiesMarketDataPort(Protocol):
    """Managed local-only market-data boundary."""

    def load_managed_source(
        self, *, managed_source: str, dataset_name: str
    ) -> SecuritiesManagedDataset: ...

    def verify_batch_artifact(self, batch: SecuritiesMarketDataBatch) -> None: ...


class SecuritiesCalendarPort(Protocol):
    """Retained calendar evidence query boundary."""

    def sessions_between(
        self,
        *,
        calendar_identity: str,
        start_date: str,
        end_date: str,
    ) -> tuple[SecuritiesCalendarSession, ...]: ...

    def is_session(self, *, calendar_identity: str, session_date: str) -> bool: ...

    def previous_session(
        self, *, calendar_identity: str, session_date: str
    ) -> SecuritiesCalendarSession | None: ...

    def next_session(
        self, *, calendar_identity: str, session_date: str
    ) -> SecuritiesCalendarSession | None: ...


class SecuritiesModelSignalBuilder(Protocol):
    """Exact Strategy Lab deployment adapter; outputs research signals only."""

    def build_signals(
        self,
        *,
        research_run_id: str,
        specification: SecuritiesStrategySpecification,
        bars: Sequence[SecuritiesBarEvidence],
        universe: Sequence[str],
        start_date: str,
        end_date: str,
        decision_cutoff: datetime,
    ) -> tuple[SecuritiesSignal, ...]: ...


class ExternalSecuritiesEnginePort(Protocol):
    """Canonical external research-engine evidence boundary."""

    engine_identity: str

    def availability(self) -> tuple[bool, str]: ...

    def run(
        self,
        *,
        request: ExternalSecuritiesEngineRequest,
        apex_signals: Sequence[SecuritiesSignal],
        apex_run: SecuritiesResearchRun,
    ) -> ExternalSecuritiesEngineRun: ...


class SecuritiesRepository(Protocol):
    def find_batch_by_idempotency_key(
        self, idempotency_key: str
    ) -> SecuritiesMarketDataBatch | None: ...

    def save_dataset(
        self,
        *,
        batch: SecuritiesMarketDataBatch,
        instruments: Sequence[SecuritiesInstrument],
        sessions: Sequence[SecuritiesCalendarSession],
        bars: Sequence[SecuritiesBarEvidence],
        corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    ) -> None: ...

    def get_batch(self, batch_id: str) -> SecuritiesMarketDataBatch: ...

    def list_batches(self) -> tuple[SecuritiesMarketDataBatch, ...]: ...

    def list_instruments(self) -> tuple[SecuritiesInstrument, ...]: ...

    def get_instrument(self, instrument_id: str) -> SecuritiesInstrument: ...

    def get_dataset(
        self, batch_id: str
    ) -> tuple[
        tuple[SecuritiesInstrument, ...],
        tuple[SecuritiesCalendarSession, ...],
        tuple[SecuritiesBarEvidence, ...],
        tuple[SecuritiesCorporateActionEvidence, ...],
    ]: ...

    def save_specification(self, specification: SecuritiesStrategySpecification) -> None: ...

    def get_specification(self, specification_id: str) -> SecuritiesStrategySpecification: ...

    def get_specification_by_strategy_id(
        self, strategy_id: str
    ) -> SecuritiesStrategySpecification: ...

    def list_specifications(self) -> tuple[SecuritiesStrategySpecification, ...]: ...

    def find_research_run_by_idempotency_key(
        self, *, campaign_id: str, idempotency_key: str
    ) -> SecuritiesResearchRun | None: ...

    def save_research_run(
        self, *, run: SecuritiesResearchRun, signals: Sequence[SecuritiesSignal]
    ) -> None: ...

    def get_research_run(self, research_run_id: str) -> SecuritiesResearchRun: ...

    def list_research_runs(
        self, *, campaign_id: str | None = None
    ) -> tuple[SecuritiesResearchRun, ...]: ...

    def list_signals(self, research_run_id: str) -> tuple[SecuritiesSignal, ...]: ...

    def save_external_run(self, run: ExternalSecuritiesEngineRun) -> None: ...

    def get_external_run(self, external_run_id: str) -> ExternalSecuritiesEngineRun: ...

    def save_conformance_report(self, report: SecuritiesResearchConformanceReport) -> None: ...

    def get_conformance_report(self, report_id: str) -> SecuritiesResearchConformanceReport: ...

    def list_conformance_reports(
        self, *, research_run_id: str | None = None
    ) -> tuple[SecuritiesResearchConformanceReport, ...]: ...


@dataclass(frozen=True, slots=True)
class SecuritiesManagedDataset:
    source_artifact: str
    source_sha256: str
    source_byte_size: int
    canonical_artifact: str
    canonical_sha256: str
    canonical_byte_size: int
    instruments: tuple[SecuritiesInstrument, ...]
    sessions: tuple[SecuritiesCalendarSession, ...]
    bars: tuple[SecuritiesBarEvidence, ...]
    corporate_actions: tuple[SecuritiesCorporateActionEvidence, ...]


@dataclass(frozen=True, slots=True)
class ImportSecuritiesDataCommand:
    managed_source: str
    dataset_name: str
    idempotency_key: str
    imported_at: datetime


@dataclass(frozen=True, slots=True)
class RunSecuritiesResearchCommand:
    campaign_id: str
    strategy_id: str
    data_batch_id: str
    universe: tuple[str, ...]
    start_date: str
    end_date: str
    decision_cutoff: datetime
    benchmark_instrument_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class RunSecuritiesConformanceCommand:
    research_run_id: str
    external_engine: str
    created_at: datetime


class ImportSecuritiesData:
    def __init__(
        self,
        *,
        repository: SecuritiesRepository,
        market_data: SecuritiesMarketDataPort,
    ) -> None:
        self._repository = repository
        self._market_data = market_data

    def execute(self, command: ImportSecuritiesDataCommand) -> SecuritiesMarketDataBatch:
        _require_nonempty(command.dataset_name, "dataset name")
        _require_nonempty(command.idempotency_key, "idempotency key")
        _require_aware(command.imported_at, "imported_at")
        canonical_input_hash = content_digest(
            {
                "managed_source": command.managed_source,
                "dataset_name": command.dataset_name,
                "idempotency_key": command.idempotency_key,
            }
        )
        existing = self._repository.find_batch_by_idempotency_key(command.idempotency_key)
        if existing is not None:
            if existing.canonical_hash != canonical_input_hash:
                raise SecuritiesIdempotencyConflictError(
                    "securities import idempotency key conflicts with retained input"
                )
            self._market_data.verify_batch_artifact(existing)
            return existing
        dataset = self._market_data.load_managed_source(
            managed_source=command.managed_source,
            dataset_name=command.dataset_name,
        )
        warnings = validate_dataset_evidence(
            instruments=dataset.instruments,
            sessions=dataset.sessions,
            bars=dataset.bars,
            corporate_actions=dataset.corporate_actions,
        )
        first_start = min(bar.period_start for bar in dataset.bars)
        last_end = max(bar.period_end for bar in dataset.bars)
        batch = SecuritiesMarketDataBatch(
            batch_id=deterministic_id(
                "securities_batch",
                {
                    "canonical_sha256": dataset.canonical_sha256,
                    "dataset_name": command.dataset_name,
                },
            ),
            dataset_name=command.dataset_name,
            state=SecuritiesDataBatchState.READY,
            imported_at=command.imported_at,
            source_artifact=dataset.source_artifact,
            source_sha256=dataset.source_sha256,
            source_byte_size=dataset.source_byte_size,
            canonical_artifact=dataset.canonical_artifact,
            canonical_sha256=dataset.canonical_sha256,
            canonical_byte_size=dataset.canonical_byte_size,
            instrument_count=len(dataset.instruments),
            session_count=len(dataset.sessions),
            bar_count=len(dataset.bars),
            corporate_action_count=len(dataset.corporate_actions),
            first_period_start=first_start,
            last_period_end=last_end,
            idempotency_key=command.idempotency_key,
            warnings=warnings,
            canonical_hash=canonical_input_hash,
        )
        self._repository.save_dataset(
            batch=batch,
            instruments=dataset.instruments,
            sessions=dataset.sessions,
            bars=dataset.bars,
            corporate_actions=dataset.corporate_actions,
        )
        return batch


class RunSecuritiesResearch:
    """Run deterministic SHADOW research without entering financial services."""

    def __init__(
        self,
        *,
        repository: SecuritiesRepository,
        campaigns: SecuritiesCampaignBoundary,
        market_data: SecuritiesMarketDataPort,
        model_signal_builder: SecuritiesModelSignalBuilder | None = None,
    ) -> None:
        self._repository = repository
        self._campaigns = campaigns
        self._market_data = market_data
        self._model_signal_builder = model_signal_builder

    def execute(self, command: RunSecuritiesResearchCommand) -> SecuritiesResearchRun:
        _require_nonempty(command.idempotency_key, "idempotency key")
        _require_aware(command.decision_cutoff, "decision_cutoff")
        if not command.universe or len(set(command.universe)) != len(command.universe):
            raise SecuritiesApplicationError(
                "research universe must be explicit, non-empty, and duplicate-free"
            )
        self._campaigns.assert_research_allowed(command.campaign_id)
        batch = self._repository.get_batch(command.data_batch_id)
        if batch.state is not SecuritiesDataBatchState.READY:
            raise SecuritiesApplicationError("securities data batch is not READY")
        self._market_data.verify_batch_artifact(batch)
        specification = self._repository.get_specification_by_strategy_id(command.strategy_id)
        instruments, _sessions, bars, _actions = self._repository.get_dataset(batch.batch_id)
        known_instruments = {instrument.instrument_id for instrument in instruments}
        if not set(command.universe) <= known_instruments:
            raise SecuritiesApplicationError("research universe contains an unknown instrument")
        if command.benchmark_instrument_id not in command.universe:
            raise SecuritiesApplicationError("benchmark must be in the explicit universe")
        canonical_input_hash = content_digest(
            {
                "campaign_id": command.campaign_id,
                "specification_hash": specification.canonical_hash,
                "data_batch_hash": batch.canonical_hash,
                "data_artifact_hash": batch.canonical_sha256,
                "universe": command.universe,
                "start_date": command.start_date,
                "end_date": command.end_date,
                "decision_cutoff": command.decision_cutoff,
                "benchmark_instrument_id": command.benchmark_instrument_id,
            }
        )
        existing = self._repository.find_research_run_by_idempotency_key(
            campaign_id=command.campaign_id,
            idempotency_key=command.idempotency_key,
        )
        if existing is not None:
            if existing.canonical_input_hash != canonical_input_hash:
                raise SecuritiesIdempotencyConflictError(
                    "securities research idempotency key conflicts with retained input"
                )
            return existing
        research_run_id = deterministic_id(
            "securities_research",
            {
                "canonical_input_hash": canonical_input_hash,
                "idempotency_key": command.idempotency_key,
            },
        )
        if specification.kind is SecuritiesStrategyKind.STRATEGY_LAB_MODEL:
            if self._model_signal_builder is None:
                raise SecuritiesApplicationError(
                    "Strategy Lab securities research adapter is unavailable"
                )
            signals = self._model_signal_builder.build_signals(
                research_run_id=research_run_id,
                specification=specification,
                bars=bars,
                universe=command.universe,
                start_date=command.start_date,
                end_date=command.end_date,
                decision_cutoff=command.decision_cutoff,
            )
        else:
            signals = build_baseline_signals(
                research_run_id=research_run_id,
                specification=specification,
                bars=bars,
                universe=command.universe,
                start_date=command.start_date,
                end_date=command.end_date,
                decision_cutoff=command.decision_cutoff,
            )
        if any(signal.signal_at > command.decision_cutoff for signal in signals):
            raise SecuritiesApplicationError("signal construction used post-cutoff evidence")
        metrics = calculate_research_metrics(
            signals=signals,
            bars=bars,
            universe=command.universe,
            benchmark_instrument_id=command.benchmark_instrument_id,
            estimated_cost_bps=specification.estimated_cost_bps,
        )
        completed_at = datetime.now(UTC)
        result_payload = {
            "research_run_id": research_run_id,
            "signals": tuple(signal.canonical_hash for signal in signals),
            "metrics": metrics,
        }
        run = SecuritiesResearchRun(
            research_run_id=research_run_id,
            campaign_id=command.campaign_id,
            specification_id=specification.specification_id,
            data_batch_id=batch.batch_id,
            universe=command.universe,
            start_date=command.start_date,
            end_date=command.end_date,
            decision_cutoff=command.decision_cutoff,
            state=SecuritiesResearchRunState.COMPLETED,
            benchmark_instrument_id=command.benchmark_instrument_id,
            metrics=metrics,
            idempotency_key=command.idempotency_key,
            started_at=completed_at,
            completed_at=completed_at,
            canonical_input_hash=canonical_input_hash,
            canonical_result_hash=content_digest(result_payload),
        )
        self._repository.save_research_run(run=run, signals=signals)
        return run


class RunSecuritiesConformance:
    def __init__(
        self,
        *,
        repository: SecuritiesRepository,
        engines: Sequence[ExternalSecuritiesEnginePort],
    ) -> None:
        self._repository = repository
        self._engines = {engine.engine_identity: engine for engine in engines}

    def execute(
        self, command: RunSecuritiesConformanceCommand
    ) -> SecuritiesResearchConformanceReport:
        _require_aware(command.created_at, "conformance created_at")
        try:
            engine = self._engines[command.external_engine]
        except KeyError as error:
            raise SecuritiesApplicationError(
                "external securities engine is not allowlisted"
            ) from error
        research_run = self._repository.get_research_run(command.research_run_id)
        specification = self._repository.get_specification(research_run.specification_id)
        batch = self._repository.get_batch(research_run.data_batch_id)
        _instruments, _sessions, _bars, actions = self._repository.get_dataset(batch.batch_id)
        request_payload = {
            "research_run_id": research_run.research_run_id,
            "engine_identity": engine.engine_identity,
            "engine_version": engine.availability()[1],
            "specification_hash": specification.canonical_hash,
            "dataset_hash": batch.canonical_sha256,
            "universe": research_run.universe,
            "start_date": research_run.start_date,
            "end_date": research_run.end_date,
            "benchmark_instrument_id": research_run.benchmark_instrument_id,
            "estimated_cost_bps": specification.estimated_cost_bps,
            "corporate_action_hashes": tuple(action.content_hash for action in actions),
        }
        request = ExternalSecuritiesEngineRequest(
            research_run_id=research_run.research_run_id,
            engine_identity=engine.engine_identity,
            engine_version=engine.availability()[1],
            specification_hash=specification.canonical_hash,
            dataset_hash=batch.canonical_sha256,
            universe=research_run.universe,
            start_date=research_run.start_date,
            end_date=research_run.end_date,
            benchmark_instrument_id=research_run.benchmark_instrument_id,
            estimated_cost_bps=specification.estimated_cost_bps,
            corporate_action_hashes=tuple(action.content_hash for action in actions),
            canonical_hash=content_digest(request_payload),
        )
        apex_signals = self._repository.list_signals(research_run.research_run_id)
        external_run = engine.run(
            request=request,
            apex_signals=apex_signals,
            apex_run=research_run,
        )
        self._repository.save_external_run(external_run)
        report = compare_external_evidence(
            research_run=research_run,
            apex_signals=apex_signals,
            external_run=external_run,
            created_at=command.created_at,
        )
        self._repository.save_conformance_report(report)
        return report


class InMemorySecuritiesCampaignBoundary:
    def __init__(self, *, allowed_campaign_ids: Sequence[str]) -> None:
        self._allowed = frozenset(allowed_campaign_ids)

    def assert_research_allowed(self, campaign_id: str) -> None:
        if campaign_id not in self._allowed:
            raise SecuritiesApplicationError("campaign is absent, killed, or not research-enabled")


class InMemorySecuritiesRepository:
    """Deterministic test/demo repository with the same idempotency boundaries."""

    def __init__(self) -> None:
        self.batches: dict[str, SecuritiesMarketDataBatch] = {}
        self.datasets: dict[
            str,
            tuple[
                tuple[SecuritiesInstrument, ...],
                tuple[SecuritiesCalendarSession, ...],
                tuple[SecuritiesBarEvidence, ...],
                tuple[SecuritiesCorporateActionEvidence, ...],
            ],
        ] = {}
        self.specifications: dict[str, SecuritiesStrategySpecification] = {}
        self.runs: dict[str, SecuritiesResearchRun] = {}
        self.signals: dict[str, tuple[SecuritiesSignal, ...]] = {}
        self.external_runs: dict[str, ExternalSecuritiesEngineRun] = {}
        self.reports: dict[str, SecuritiesResearchConformanceReport] = {}

    def find_batch_by_idempotency_key(
        self, idempotency_key: str
    ) -> SecuritiesMarketDataBatch | None:
        return next(
            (batch for batch in self.batches.values() if batch.idempotency_key == idempotency_key),
            None,
        )

    def save_dataset(
        self,
        *,
        batch: SecuritiesMarketDataBatch,
        instruments: Sequence[SecuritiesInstrument],
        sessions: Sequence[SecuritiesCalendarSession],
        bars: Sequence[SecuritiesBarEvidence],
        corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    ) -> None:
        if batch.batch_id in self.batches:
            raise SecuritiesIdempotencyConflictError("market-data batch already exists")
        self.batches[batch.batch_id] = batch
        self.datasets[batch.batch_id] = (
            tuple(instruments),
            tuple(sessions),
            tuple(bars),
            tuple(corporate_actions),
        )

    def get_batch(self, batch_id: str) -> SecuritiesMarketDataBatch:
        try:
            return self.batches[batch_id]
        except KeyError as error:
            raise SecuritiesApplicationError("securities data batch was not found") from error

    def list_batches(self) -> tuple[SecuritiesMarketDataBatch, ...]:
        return tuple(sorted(self.batches.values(), key=lambda item: item.imported_at))

    def list_instruments(self) -> tuple[SecuritiesInstrument, ...]:
        values = {
            item.instrument_id: item for dataset in self.datasets.values() for item in dataset[0]
        }
        return tuple(sorted(values.values(), key=lambda item: item.instrument_id))

    def get_instrument(self, instrument_id: str) -> SecuritiesInstrument:
        try:
            return next(
                item for item in self.list_instruments() if item.instrument_id == instrument_id
            )
        except StopIteration as error:
            raise SecuritiesApplicationError("securities instrument was not found") from error

    def get_dataset(
        self, batch_id: str
    ) -> tuple[
        tuple[SecuritiesInstrument, ...],
        tuple[SecuritiesCalendarSession, ...],
        tuple[SecuritiesBarEvidence, ...],
        tuple[SecuritiesCorporateActionEvidence, ...],
    ]:
        self.get_batch(batch_id)
        return self.datasets[batch_id]

    def save_specification(self, specification: SecuritiesStrategySpecification) -> None:
        existing = self.specifications.get(specification.specification_id)
        if existing is not None and existing != specification:
            raise SecuritiesIdempotencyConflictError("strategy specification hash collision")
        self.specifications[specification.specification_id] = specification

    def get_specification(self, specification_id: str) -> SecuritiesStrategySpecification:
        try:
            return self.specifications[specification_id]
        except KeyError as error:
            raise SecuritiesApplicationError(
                "securities strategy specification was not found"
            ) from error

    def get_specification_by_strategy_id(self, strategy_id: str) -> SecuritiesStrategySpecification:
        matches = [
            specification
            for specification in self.specifications.values()
            if specification.strategy_id == strategy_id
        ]
        if len(matches) != 1:
            raise SecuritiesApplicationError(
                "strategy identity does not resolve to exactly one specification"
            )
        return matches[0]

    def list_specifications(self) -> tuple[SecuritiesStrategySpecification, ...]:
        return tuple(sorted(self.specifications.values(), key=lambda item: item.specification_id))

    def find_research_run_by_idempotency_key(
        self, *, campaign_id: str, idempotency_key: str
    ) -> SecuritiesResearchRun | None:
        return next(
            (
                run
                for run in self.runs.values()
                if run.campaign_id == campaign_id and run.idempotency_key == idempotency_key
            ),
            None,
        )

    def save_research_run(
        self, *, run: SecuritiesResearchRun, signals: Sequence[SecuritiesSignal]
    ) -> None:
        if run.research_run_id in self.runs:
            raise SecuritiesIdempotencyConflictError("securities research run already exists")
        self.runs[run.research_run_id] = run
        self.signals[run.research_run_id] = tuple(signals)

    def get_research_run(self, research_run_id: str) -> SecuritiesResearchRun:
        try:
            return self.runs[research_run_id]
        except KeyError as error:
            raise SecuritiesApplicationError("securities research run was not found") from error

    def list_research_runs(
        self, *, campaign_id: str | None = None
    ) -> tuple[SecuritiesResearchRun, ...]:
        values = (
            run
            for run in self.runs.values()
            if campaign_id is None or run.campaign_id == campaign_id
        )
        return tuple(sorted(values, key=lambda item: item.completed_at))

    def list_signals(self, research_run_id: str) -> tuple[SecuritiesSignal, ...]:
        self.get_research_run(research_run_id)
        return self.signals[research_run_id]

    def save_external_run(self, run: ExternalSecuritiesEngineRun) -> None:
        self.external_runs.setdefault(run.external_run_id, run)

    def get_external_run(self, external_run_id: str) -> ExternalSecuritiesEngineRun:
        try:
            return self.external_runs[external_run_id]
        except KeyError as error:
            raise SecuritiesApplicationError("external securities run was not found") from error

    def save_conformance_report(self, report: SecuritiesResearchConformanceReport) -> None:
        self.reports.setdefault(report.report_id, report)

    def get_conformance_report(self, report_id: str) -> SecuritiesResearchConformanceReport:
        try:
            return self.reports[report_id]
        except KeyError as error:
            raise SecuritiesApplicationError(
                "securities conformance report was not found"
            ) from error

    def list_conformance_reports(
        self, *, research_run_id: str | None = None
    ) -> tuple[SecuritiesResearchConformanceReport, ...]:
        values = (
            report
            for report in self.reports.values()
            if research_run_id is None or report.research_run_id == research_run_id
        )
        return tuple(sorted(values, key=lambda item: item.created_at))


def conformance_counts(
    reports: Sequence[SecuritiesResearchConformanceReport],
) -> dict[SecuritiesConformanceClassification, int]:
    return {
        classification: sum(report.classification is classification for report in reports)
        for classification in SecuritiesConformanceClassification
    }


def initial_securities_strategy_specifications(
    created_at: datetime,
) -> tuple[SecuritiesStrategySpecification, ...]:
    """Return the four transparent Stage 1 baselines with frozen configurations."""

    return (
        SecuritiesStrategySpecification.create(
            strategy_id="securities-cash-baseline-v1",
            kind=SecuritiesStrategyKind.CASH_BASELINE,
            version="1",
            created_at=created_at,
        ),
        SecuritiesStrategySpecification.create(
            strategy_id="securities-buy-and-hold-v1",
            kind=SecuritiesStrategyKind.BUY_AND_HOLD,
            version="1",
            created_at=created_at,
        ),
        SecuritiesStrategySpecification.create(
            strategy_id="securities-time-series-momentum-v1",
            kind=SecuritiesStrategyKind.TIME_SERIES_MOMENTUM,
            version="1",
            lookback_sessions=20,
            rebalance_every_sessions=5,
            created_at=created_at,
        ),
        SecuritiesStrategySpecification.create(
            strategy_id="securities-cross-sectional-momentum-v1",
            kind=SecuritiesStrategyKind.CROSS_SECTIONAL_MOMENTUM,
            version="1",
            lookback_sessions=20,
            rebalance_every_sessions=5,
            top_quantile="0.25",
            created_at=created_at,
        ),
    )


def _require_nonempty(value: str, label: str) -> None:
    if not value or not value.strip():
        raise SecuritiesApplicationError(f"{label} is required")


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SecuritiesApplicationError(f"{label} must be timezone-aware")
