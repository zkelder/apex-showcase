"""Application services for canonical Securities baseline research evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.securities import (
    SecuritiesBarEvidence,
    SecuritiesCorporateActionEvidence,
    SecuritiesMarketDataBatch,
)
from apex.domain.securities_baselines import (
    CanonicalSecuritiesBaseline,
    CanonicalSecuritiesSimulationRequest,
    MultiSimulatorConformanceClassification,
    SecuritiesBaselineScorecard,
    SecuritiesConformanceMaterialityPolicy,
    SecuritiesSimulationResult,
    SecuritiesSimulationRun,
    SecuritiesSimulatorConformanceReport,
    SecuritiesSimulatorIdentity,
    StrategyLabBaselineComparison,
    build_baseline_scorecard,
    canonical_securities_baselines,
    compare_simulator_runs,
    compare_strategy_lab_candidate,
    simulation_run,
)


class SecuritiesBaselineApplicationError(RuntimeError):
    """A canonical baseline application boundary rejected an operation."""


class SecuritiesBaselineIdempotencyConflict(SecuritiesBaselineApplicationError):
    """A retained idempotency key was replayed with different canonical input."""


@dataclass(frozen=True, slots=True)
class CurrentSecuritiesConformanceSelection:
    """One visible historical/current choice for an exact canonical scope."""

    baseline_id: str
    canonical_request_hash: str
    historical_report_ids: tuple[str, ...]
    current_report_id: str
    current_adapter_version: str
    replacement_reason: str


class SecuritiesBaselineDataPort(Protocol):
    def get_batch(self, batch_id: str) -> SecuritiesMarketDataBatch: ...

    def get_dataset(
        self, batch_id: str
    ) -> tuple[
        tuple[object, ...],
        tuple[object, ...],
        tuple[SecuritiesBarEvidence, ...],
        tuple[SecuritiesCorporateActionEvidence, ...],
    ]: ...


class SecuritiesBaselineSimulator(Protocol):
    simulator_identity: SecuritiesSimulatorIdentity

    def availability(self) -> tuple[bool, str]: ...

    def run(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        baseline: CanonicalSecuritiesBaseline,
        bars: Sequence[SecuritiesBarEvidence],
        corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    ) -> SecuritiesSimulationResult: ...


class SecuritiesBaselineRepository(Protocol):
    def save_baseline(self, baseline: CanonicalSecuritiesBaseline) -> None: ...

    def baseline(self, baseline_id: str) -> CanonicalSecuritiesBaseline: ...

    def baselines(self) -> tuple[CanonicalSecuritiesBaseline, ...]: ...

    def find_run_by_idempotency_key(
        self, idempotency_key: str
    ) -> SecuritiesSimulationRun | None: ...

    def save_run(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        run: SecuritiesSimulationRun,
    ) -> None: ...

    def runs(self) -> tuple[SecuritiesSimulationRun, ...]: ...

    def save_report(self, report: SecuritiesSimulatorConformanceReport) -> None: ...

    def reports(self) -> tuple[SecuritiesSimulatorConformanceReport, ...]: ...

    def retained_request(
        self, report_id: str
    ) -> tuple[SecuritiesSimulatorConformanceReport, CanonicalSecuritiesSimulationRequest]: ...

    def save_scorecard(self, scorecard: SecuritiesBaselineScorecard) -> None: ...

    def scorecards(self) -> tuple[SecuritiesBaselineScorecard, ...]: ...

    def save_strategy_lab_comparison(self, comparison: StrategyLabBaselineComparison) -> None: ...


@dataclass(frozen=True, slots=True)
class RunCanonicalSecuritiesBaselineCommand:
    campaign_id: str
    baseline_id: str
    data_batch_id: str
    universe: tuple[str, ...]
    start_date: str
    end_date: str
    cutoff: datetime
    simulator: SecuritiesSimulatorIdentity
    idempotency_key: str
    started_at: datetime
    completed_at: datetime


class RegisterCanonicalSecuritiesBaselines:
    def __init__(self, repository: SecuritiesBaselineRepository) -> None:
        self._repository = repository

    def execute(self, *, created_at: datetime) -> tuple[CanonicalSecuritiesBaseline, ...]:
        baselines = canonical_securities_baselines(created_at)
        for baseline in baselines:
            self._repository.save_baseline(baseline)
        return self._repository.baselines()


class RunCanonicalSecuritiesBaseline:
    def __init__(
        self,
        *,
        repository: SecuritiesBaselineRepository,
        data: SecuritiesBaselineDataPort,
        simulators: Sequence[SecuritiesBaselineSimulator],
    ) -> None:
        self._repository = repository
        self._data = data
        self._simulators = {item.simulator_identity: item for item in simulators}

    def execute(self, command: RunCanonicalSecuritiesBaselineCommand) -> SecuritiesSimulationRun:
        if not command.campaign_id or not command.idempotency_key:
            raise SecuritiesBaselineApplicationError(
                "campaign and idempotency identities are required"
            )
        baseline = self._repository.baseline(command.baseline_id)
        batch = self._data.get_batch(command.data_batch_id)
        _instruments, _sessions, bars, corporate_actions = self._data.get_dataset(
            command.data_batch_id
        )
        request = CanonicalSecuritiesSimulationRequest.create(
            baseline=baseline,
            data_batch_id=batch.batch_id,
            data_batch_hash=batch.canonical_hash,
            universe=command.universe,
            start_date=command.start_date,
            end_date=command.end_date,
            cutoff=command.cutoff,
        )
        simulator = self._simulators.get(command.simulator)
        if simulator is None:
            raise SecuritiesBaselineApplicationError("requested simulator is not configured")
        existing = self._repository.find_run_by_idempotency_key(command.idempotency_key)
        if existing is not None:
            if (
                existing.canonical_input_hash != request.canonical_hash
                or existing.simulator_identity is not command.simulator
            ):
                raise SecuritiesBaselineIdempotencyConflict(
                    "baseline simulation idempotency key conflicts with retained input"
                )
            return existing
        result = simulator.run(
            request=request,
            baseline=baseline,
            bars=bars,
            corporate_actions=corporate_actions,
        )
        retained = simulation_run(
            request=request,
            result=result,
            idempotency_key=command.idempotency_key,
            started_at=command.started_at,
            completed_at=command.completed_at,
        )
        self._repository.save_run(request=request, run=retained)
        return retained


class CompareCanonicalSecuritiesSimulators:
    def __init__(self, repository: SecuritiesBaselineRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        reference: SecuritiesSimulationRun,
        comparison: SecuritiesSimulationRun,
        explanation_codes: Sequence[str] = (),
    ) -> SecuritiesSimulatorConformanceReport:
        report = compare_simulator_runs(
            reference=reference,
            comparison=comparison,
            policy=SecuritiesConformanceMaterialityPolicy.v1(),
            explanation_codes=explanation_codes,
        )
        self._repository.save_report(report)
        return report


class ReconcileSecuritiesSimulationFromRetainedRequest:
    """Create a replacement vectorbt run from one verified stored request."""

    def __init__(
        self,
        *,
        repository: SecuritiesBaselineRepository,
        data: SecuritiesBaselineDataPort,
        simulator: SecuritiesBaselineSimulator,
    ) -> None:
        self._repository = repository
        self._data = data
        self._simulator = simulator

    def execute(
        self, *, report_id: str, idempotency_key: str, now: datetime
    ) -> tuple[SecuritiesSimulationRun, SecuritiesSimulatorConformanceReport]:
        if self._simulator.simulator_identity is not SecuritiesSimulatorIdentity.VECTORBT:
            raise SecuritiesBaselineApplicationError("RETAINED_RECONCILIATION_VECTORBT_REQUIRED")
        original, request = self._repository.retained_request(report_id)
        reference = next(
            (
                item
                for item in self._repository.runs()
                if item.simulation_run_id == original.reference_run_id
            ),
            None,
        )
        if reference is None or reference.canonical_input_hash != request.canonical_hash:
            raise SecuritiesBaselineApplicationError("RETAINED_REFERENCE_REQUEST_DIVERGENCE")
        batch = self._data.get_batch(request.data_batch_id)
        if batch.canonical_hash != request.data_batch_hash:
            raise SecuritiesBaselineApplicationError("RETAINED_DATA_BATCH_HASH_DIVERGENCE")
        existing = self._repository.find_run_by_idempotency_key(idempotency_key)
        if existing is not None:
            if (
                existing.canonical_input_hash != request.canonical_hash
                or existing.simulator_identity is not SecuritiesSimulatorIdentity.VECTORBT
            ):
                raise SecuritiesBaselineIdempotencyConflict("RETAINED_RECONCILIATION_KEY_CONFLICT")
            retained_report = next(
                (
                    item
                    for item in self._repository.reports()
                    if item.reference_run_id == reference.simulation_run_id
                    and item.comparison_run_id == existing.simulation_run_id
                ),
                None,
            )
            if retained_report is None:
                raise SecuritiesBaselineApplicationError("RETAINED_REPLACEMENT_REPORT_UNAVAILABLE")
            return existing, retained_report
        _instruments, _sessions, bars, actions = self._data.get_dataset(request.data_batch_id)
        result = self._simulator.run(
            request=request,
            baseline=self._repository.baseline(request.baseline_id),
            bars=bars,
            corporate_actions=actions,
        )
        replacement = simulation_run(
            request=request,
            result=result,
            idempotency_key=idempotency_key,
            started_at=now,
            completed_at=now,
        )
        self._repository.save_run(request=request, run=replacement)
        report = compare_simulator_runs(
            reference=reference,
            comparison=replacement,
            policy=SecuritiesConformanceMaterialityPolicy.v1(),
        )
        self._repository.save_report(report)
        return replacement, report


def select_current_securities_conformance_reports(
    *,
    reports: Sequence[SecuritiesSimulatorConformanceReport],
    runs: Sequence[SecuritiesSimulationRun],
) -> tuple[CurrentSecuritiesConformanceSelection, ...]:
    """Select V2 retained-request replacements without altering history."""

    by_run_id = {item.simulation_run_id: item for item in runs}
    grouped: dict[tuple[str, str], list[SecuritiesSimulatorConformanceReport]] = {}
    for report in reports:
        reference = by_run_id.get(report.reference_run_id)
        comparison = by_run_id.get(report.comparison_run_id)
        if reference is None or comparison is None:
            continue
        if (
            reference.canonical_input_hash != comparison.canonical_input_hash
            or reference.simulator_identity is not SecuritiesSimulatorIdentity.APEX_RESEARCH
            or comparison.simulator_identity is not SecuritiesSimulatorIdentity.VECTORBT
        ):
            continue
        grouped.setdefault((report.baseline_id, reference.canonical_input_hash), []).append(report)
    selections = []
    for (baseline_id, request_hash), scope_reports in sorted(grouped.items()):
        replacements = tuple(
            item
            for item in scope_reports
            if by_run_id[item.comparison_run_id].adapter_version == "VECTORBT_CANONICAL_BASELINE_V2"
        )
        current = sorted(replacements or tuple(scope_reports), key=lambda item: item.report_id)[-1]
        selections.append(
            CurrentSecuritiesConformanceSelection(
                baseline_id=baseline_id,
                canonical_request_hash=request_hash,
                historical_report_ids=tuple(sorted(item.report_id for item in scope_reports)),
                current_report_id=current.report_id,
                current_adapter_version=by_run_id[current.comparison_run_id].adapter_version,
                replacement_reason=(
                    "RETAINED_REQUEST_VECTORBT_CASH_COST_RECONCILIATION"
                    if replacements
                    else "NO_VALID_RETAINED_REQUEST_REPLACEMENT"
                ),
            )
        )
    return tuple(selections)


class BuildCanonicalSecuritiesScorecard:
    def __init__(self, repository: SecuritiesBaselineRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        run: SecuritiesSimulationRun,
        conformance_state: MultiSimulatorConformanceClassification,
        integrity_warnings: Sequence[str] = (),
    ) -> SecuritiesBaselineScorecard:
        scorecard = build_baseline_scorecard(
            baseline=self._repository.baseline(run.baseline_id),
            request=request,
            run=run,
            conformance_state=conformance_state,
            integrity_warnings=integrity_warnings,
        )
        self._repository.save_scorecard(scorecard)
        return scorecard


class CompareStrategyLabWithCanonicalBaseline:
    def __init__(self, repository: SecuritiesBaselineRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        candidate_id: str,
        candidate_result_hash: str,
        candidate_data_batch_hash: str,
        candidate_universe_hash: str,
        candidate_start_date: str,
        candidate_end_date: str,
        candidate_net_return: str,
        scorecard: SecuritiesBaselineScorecard,
    ) -> StrategyLabBaselineComparison:
        comparison = compare_strategy_lab_candidate(
            candidate_id=candidate_id,
            candidate_result_hash=candidate_result_hash,
            candidate_data_batch_hash=candidate_data_batch_hash,
            candidate_universe_hash=candidate_universe_hash,
            candidate_start_date=candidate_start_date,
            candidate_end_date=candidate_end_date,
            candidate_net_return=candidate_net_return,
            scorecard=scorecard,
        )
        self._repository.save_strategy_lab_comparison(comparison)
        return comparison


class InMemorySecuritiesBaselineRepository(SecuritiesBaselineRepository):
    """Deterministic repository for focused tests and the provider-free demo."""

    def __init__(self) -> None:
        self._baselines: dict[str, CanonicalSecuritiesBaseline] = {}
        self._runs: dict[str, SecuritiesSimulationRun] = {}
        self._run_keys: dict[str, str] = {}
        self._reports: dict[str, SecuritiesSimulatorConformanceReport] = {}
        self._scorecards: dict[str, SecuritiesBaselineScorecard] = {}
        self._comparisons: dict[str, StrategyLabBaselineComparison] = {}
        self._requests: dict[str, CanonicalSecuritiesSimulationRequest] = {}

    def save_baseline(self, baseline: CanonicalSecuritiesBaseline) -> None:
        existing = self._baselines.get(baseline.baseline_id)
        if existing is not None and existing.canonical_hash != baseline.canonical_hash:
            raise SecuritiesBaselineIdempotencyConflict(
                "canonical baseline identity conflicts with retained specification"
            )
        self._baselines[baseline.baseline_id] = baseline

    def baseline(self, baseline_id: str) -> CanonicalSecuritiesBaseline:
        try:
            return self._baselines[baseline_id]
        except KeyError as error:
            raise SecuritiesBaselineApplicationError("canonical baseline was not found") from error

    def baselines(self) -> tuple[CanonicalSecuritiesBaseline, ...]:
        return tuple(sorted(self._baselines.values(), key=lambda item: item.family.value))

    def find_run_by_idempotency_key(self, idempotency_key: str) -> SecuritiesSimulationRun | None:
        run_id = self._run_keys.get(idempotency_key)
        return None if run_id is None else self._runs[run_id]

    def save_run(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        run: SecuritiesSimulationRun,
    ) -> None:
        if run.canonical_input_hash != request.canonical_hash:
            raise SecuritiesBaselineIdempotencyConflict("retained run request hash differs")
        existing = self.find_run_by_idempotency_key(run.idempotency_key)
        if existing is not None and existing != run:
            raise SecuritiesBaselineIdempotencyConflict(
                "simulation idempotency key conflicts with retained result"
            )
        self._runs[run.simulation_run_id] = run
        self._run_keys[run.idempotency_key] = run.simulation_run_id
        self._requests[run.simulation_run_id] = request

    def runs(self) -> tuple[SecuritiesSimulationRun, ...]:
        return tuple(sorted(self._runs.values(), key=lambda item: item.simulation_run_id))

    def save_report(self, report: SecuritiesSimulatorConformanceReport) -> None:
        self._save_immutable(self._reports, report.report_id, report)

    def reports(self) -> tuple[SecuritiesSimulatorConformanceReport, ...]:
        return tuple(sorted(self._reports.values(), key=lambda item: item.report_id))

    def retained_request(
        self, report_id: str
    ) -> tuple[SecuritiesSimulatorConformanceReport, CanonicalSecuritiesSimulationRequest]:
        report = self._reports.get(report_id)
        if report is None or report.reference_run_id not in self._requests:
            raise SecuritiesBaselineApplicationError("RETAINED_REQUEST_UNAVAILABLE")
        return report, self._requests[report.reference_run_id]

    def save_scorecard(self, scorecard: SecuritiesBaselineScorecard) -> None:
        self._save_immutable(self._scorecards, scorecard.scorecard_id, scorecard)

    def scorecards(self) -> tuple[SecuritiesBaselineScorecard, ...]:
        return tuple(sorted(self._scorecards.values(), key=lambda item: item.scorecard_id))

    def save_strategy_lab_comparison(self, comparison: StrategyLabBaselineComparison) -> None:
        self._save_immutable(self._comparisons, comparison.comparison_id, comparison)

    @staticmethod
    def _save_immutable[T](target: dict[str, T], identity: str, value: T) -> None:
        existing = target.get(identity)
        if existing is not None and existing != value:
            raise SecuritiesBaselineIdempotencyConflict(
                "immutable canonical baseline evidence conflicts"
            )
        target[identity] = value


def simulator_catalog(
    simulators: Sequence[SecuritiesBaselineSimulator],
) -> Mapping[str, tuple[bool, str]]:
    return {
        simulator.simulator_identity.value: simulator.availability() for simulator in simulators
    }
