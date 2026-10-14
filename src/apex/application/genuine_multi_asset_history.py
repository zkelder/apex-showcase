"""Adapter-free orchestration for the genuine multi-asset cohort quality
gate and the cross-market dataset binding (milestone sections 9-10).

Composes existing repositories/ports unmodified: ``HistoricalMarketData
Repository`` and ``SecuritiesEvidencePort`` (already used by ``application.
historical_market_data``), plus a narrow calendar port. Adds no new bar
store, no new capture mechanism, and no financial/Strategy/PAPER authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.application.historical_market_data import HistoricalMarketDataRepository
from apex.application.real_securities_data import SecuritiesEvidencePort
from apex.application.securities import SecuritiesCalendarPort
from apex.domain.cross_market_dataset import CrossMarketDataset
from apex.domain.genuine_data_quality_gate import (
    GenuineCohortQualityGateReport,
    assess_instrument_quality,
)
from apex.domain.research_cohort import ResearchCohortPolicy
from apex.domain.securities import SecuritiesBarEvidence


class GenuineMultiAssetHistoryApplicationError(RuntimeError):
    pass


class GenuineMultiAssetHistoryNotFound(GenuineMultiAssetHistoryApplicationError):
    pass


class GenuineMultiAssetHistoryRepository(Protocol):
    def quality_gate_report_by_id(
        self, report_id: str
    ) -> GenuineCohortQualityGateReport | None: ...
    def save_quality_gate_report(
        self, report: GenuineCohortQualityGateReport
    ) -> GenuineCohortQualityGateReport: ...
    def list_quality_gate_reports(self) -> tuple[GenuineCohortQualityGateReport, ...]: ...

    def cross_market_dataset_by_id(self, dataset_id: str) -> CrossMarketDataset | None: ...
    def save_cross_market_dataset(self, dataset: CrossMarketDataset) -> CrossMarketDataset: ...
    def list_cross_market_datasets(self) -> tuple[CrossMarketDataset, ...]: ...


@dataclass(frozen=True, slots=True)
class BuildQualityGateReportCommand:
    snapshot_id: str
    cohort_policy: ResearchCohortPolicy
    created_at: datetime


class BuildQualityGateReport:
    """Assesses every retained instrument in one equity dataset snapshot,
    grouped by its own observed coverage window (never the whole cohort's
    declared policy window -- see ``domain.genuine_data_quality_gate``),
    and retains one immutable, content-addressed report."""

    def __init__(
        self,
        *,
        repository: GenuineMultiAssetHistoryRepository,
        historical_repository: HistoricalMarketDataRepository,
        evidence: SecuritiesEvidencePort,
        calendar: SecuritiesCalendarPort,
    ) -> None:
        self._repository = repository
        self._historical_repository = historical_repository
        self._evidence = evidence
        self._calendar = calendar

    def execute(self, command: BuildQualityGateReportCommand) -> GenuineCohortQualityGateReport:
        snapshot = self._historical_repository.snapshot_by_id(command.snapshot_id)
        if snapshot is None:
            raise GenuineMultiAssetHistoryNotFound("HISTORICAL_DATASET_SNAPSHOT_NOT_FOUND")
        plan = self._historical_repository.get_plan(snapshot.plan_id)
        if plan is None:
            raise GenuineMultiAssetHistoryNotFound("HISTORICAL_CAPTURE_PLAN_NOT_FOUND")

        bars_by_instrument: dict[str, list[SecuritiesBarEvidence]] = {}
        for batch_id in snapshot.source_batch_ids:
            _instruments, _sessions, bars, _actions = self._evidence.get_dataset(batch_id)
            for bar in bars:
                bars_by_instrument.setdefault(bar.instrument_id, []).append(bar)

        calendar_sessions = self._calendar.sessions_between(
            calendar_identity=plan.policy.calendar_identity,
            start_date=plan.policy.start_date,
            end_date=plan.policy.end_date,
        )

        assessments = tuple(
            assess_instrument_quality(
                stable_security_id=member.stable_security_id,
                symbol=member.symbol_at_capture,
                instrument_id=member.instrument_id,
                bars=bars_by_instrument.get(member.instrument_id, ()),
                calendar_sessions=calendar_sessions,
                source_batch_ids=snapshot.source_batch_ids,
            )
            for member in plan.universe
        )
        report = GenuineCohortQualityGateReport.build(
            snapshot_id=snapshot.snapshot_id,
            cohort_policy_id=command.cohort_policy.policy_id,
            requested_symbols=tuple(member.symbol_at_capture for member in plan.universe),
            assessments=assessments,
            point_in_time_limitations=(
                snapshot.point_in_time_universe_state.value,
                snapshot.survivorship_state.value,
            ),
            source_hashes=snapshot.source_batch_ids,
            created_at=command.created_at,
        )
        existing = self._repository.quality_gate_report_by_id(report.report_id)
        if existing is not None:
            return existing
        return self._repository.save_quality_gate_report(report)


@dataclass(frozen=True, slots=True)
class BuildCrossMarketDatasetCommand:
    equity_snapshot_id: str
    cohort_policy: ResearchCohortPolicy
    quality_gate_report_id: str
    crypto_provider: str
    crypto_product_id: str
    crypto_capture_ids: tuple[str, ...]
    crypto_coverage_start: str
    crypto_coverage_end: str
    tmc_component_symbols: tuple[str, ...]
    tmc_source_identity: str
    alignment_policy_id: str
    created_at: datetime


class BuildCrossMarketDataset:
    def __init__(
        self,
        *,
        repository: GenuineMultiAssetHistoryRepository,
        historical_repository: HistoricalMarketDataRepository,
    ) -> None:
        self._repository = repository
        self._historical_repository = historical_repository

    def execute(self, command: BuildCrossMarketDatasetCommand) -> CrossMarketDataset:
        snapshot = self._historical_repository.snapshot_by_id(command.equity_snapshot_id)
        if snapshot is None:
            raise GenuineMultiAssetHistoryNotFound("HISTORICAL_DATASET_SNAPSHOT_NOT_FOUND")
        quality_gate_report = self._repository.quality_gate_report_by_id(
            command.quality_gate_report_id
        )
        if quality_gate_report is None:
            raise GenuineMultiAssetHistoryNotFound("QUALITY_GATE_REPORT_NOT_FOUND")
        if snapshot.earliest_observation_date is None or snapshot.latest_observation_date is None:
            raise GenuineMultiAssetHistoryApplicationError(
                "CROSS_MARKET_DATASET_REQUIRES_A_NON_EMPTY_EQUITY_SNAPSHOT"
            )
        dataset = CrossMarketDataset.create(
            equity_dataset_snapshot_id=snapshot.snapshot_id,
            equity_dataset_snapshot_hash=snapshot.canonical_hash,
            equity_coverage_start=snapshot.earliest_observation_date,
            equity_coverage_end=snapshot.latest_observation_date,
            cohort_policy_id=command.cohort_policy.policy_id,
            cohort_policy_hash=command.cohort_policy.canonical_hash,
            quality_gate_report_id=quality_gate_report.report_id,
            quality_gate_report_hash=quality_gate_report.canonical_hash,
            crypto_provider=command.crypto_provider,
            crypto_product_id=command.crypto_product_id,
            crypto_capture_ids=command.crypto_capture_ids,
            crypto_coverage_start=command.crypto_coverage_start,
            crypto_coverage_end=command.crypto_coverage_end,
            tmc_component_symbols=command.tmc_component_symbols,
            tmc_source_identity=command.tmc_source_identity,
            adjustment_policy_id=snapshot.adjustment_policy_id,
            missing_data_policy_id=snapshot.missing_data_policy_id,
            alignment_policy_id=command.alignment_policy_id,
            point_in_time_limitations=(
                snapshot.point_in_time_universe_state.value,
                snapshot.survivorship_state.value,
                "HISTORICAL_OPTIONS_PRICING_UNAVAILABLE",
            ),
            created_at=command.created_at,
        )
        existing = self._repository.cross_market_dataset_by_id(dataset.cross_market_dataset_id)
        if existing is not None:
            return existing
        return self._repository.save_cross_market_dataset(dataset)


@dataclass(frozen=True, slots=True)
class GenuineMultiAssetRegistry:
    """The canonical, addressable-by-ID summary milestone section 14 asks
    for -- every id here is resolved live from retained evidence (never
    hardcoded), so a later research milestone can reuse the exact artifacts
    without rerunning any network capture."""

    cohort_policy_id: str
    cohort_policy_version: str
    cohort_member_count: int
    historical_capture_plan_id: str | None
    equity_dataset_snapshot_id: str | None
    equity_coverage_start: str | None
    equity_coverage_end: str | None
    historical_data_quality_report_id: str | None
    quality_gate_report_id: str | None
    quality_gate_usable_count: int | None
    quality_gate_usable_with_warnings_count: int | None
    quality_gate_insufficient_count: int | None
    quality_gate_rejected_count: int | None
    cross_market_dataset_id: str | None
    crypto_capture_ids: tuple[str, ...]
    common_overlap_start: str | None
    common_overlap_end: str | None
    tmc_component_symbols: tuple[str, ...]
    replay_run_ids: tuple[str, ...]
    governance_policy_ids: tuple[str, ...]
    conclusion_ids: tuple[str, ...]
    promotion_evaluation_ids: tuple[str, ...]
    walk_forward_run_ids: tuple[str, ...]
    robustness_run_ids: tuple[str, ...]
    trial_family_ids: tuple[str, ...]
    authority: str = "NONE"


class ResearchMethodologyRegistryPort(Protocol):
    def governance_policies_for_replay_run(self, replay_run_id: str) -> tuple[object, ...]: ...
    def conclusions_for_hypothesis(self, hypothesis_version_id: str) -> tuple[object, ...]: ...
    def trial_families_for_hypothesis(self, hypothesis_id: str) -> tuple[object, ...]: ...
    def promotion_evaluations_for_conclusion(self, conclusion_id: str) -> tuple[object, ...]: ...
    def list_walk_forward_runs(self) -> tuple[object, ...]: ...


def resolve_genuine_multi_asset_registry(
    *,
    cohort_policy: ResearchCohortPolicy,
    historical_repository: HistoricalMarketDataRepository,
    genuine_repository: GenuineMultiAssetHistoryRepository,
    replay_repository: object | None = None,
    methodology_repository: ResearchMethodologyRegistryPort | None = None,
    hypothesis_version_id: str | None = None,
    hypothesis_id: str | None = None,
) -> GenuineMultiAssetRegistry:
    plan_idempotency_key = f"genuine-multi-asset-cohort-v1:{cohort_policy.policy_id}"
    plan = historical_repository.plan_by_idempotency(plan_idempotency_key)

    snapshot = None
    if plan is not None:
        matching_snapshots = tuple(
            item for item in historical_repository.list_snapshots() if item.plan_id == plan.plan_id
        )
        snapshot = matching_snapshots[-1] if matching_snapshots else None

    quality_report = None
    if snapshot is not None:
        matching_reports = tuple(
            item
            for item in historical_repository.list_quality_reports()
            if item.snapshot_id == snapshot.snapshot_id
        )
        quality_report = matching_reports[-1] if matching_reports else None

    quality_gate_reports = tuple(
        item
        for item in genuine_repository.list_quality_gate_reports()
        if item.cohort_policy_id == cohort_policy.policy_id
    )
    quality_gate_report = quality_gate_reports[-1] if quality_gate_reports else None

    cross_market_datasets = tuple(
        item
        for item in genuine_repository.list_cross_market_datasets()
        if item.cohort_policy_id == cohort_policy.policy_id
    )
    cross_market_dataset = cross_market_datasets[-1] if cross_market_datasets else None

    replay_run_ids: tuple[str, ...] = ()
    if replay_repository is not None and snapshot is not None:
        replay_run_ids = tuple(
            item.replay_run_id
            for item in replay_repository.list_runs()  # type: ignore[attr-defined]
            if item.dataset_snapshot_id == snapshot.snapshot_id
        )

    governance_policy_ids: tuple[str, ...] = ()
    walk_forward_run_ids: tuple[str, ...] = ()
    conclusion_ids: tuple[str, ...] = ()
    promotion_evaluation_ids: tuple[str, ...] = ()
    trial_family_ids: tuple[str, ...] = ()
    robustness_run_ids: tuple[str, ...] = ()
    if methodology_repository is not None:
        for replay_run_id in replay_run_ids:
            governance_policy_ids += tuple(
                item.governance_policy_id  # type: ignore[attr-defined]
                for item in methodology_repository.governance_policies_for_replay_run(replay_run_id)
            )
        walk_forward_run_ids = tuple(
            item.walk_forward_run_id  # type: ignore[attr-defined]
            for item in methodology_repository.list_walk_forward_runs()
            if item.replay_run_id in replay_run_ids  # type: ignore[attr-defined]
        )
        if hypothesis_version_id is not None:
            conclusions = methodology_repository.conclusions_for_hypothesis(hypothesis_version_id)
            conclusion_ids = tuple(item.conclusion_id for item in conclusions)  # type: ignore[attr-defined]
            robustness_run_ids = tuple(
                sorted(
                    {
                        item.robustness_run_id  # type: ignore[attr-defined]
                        for item in conclusions
                        if item.robustness_run_id is not None  # type: ignore[attr-defined]
                    }
                )
            )
            for conclusion_id in conclusion_ids:
                promotion_evaluation_ids += tuple(
                    item.promotion_evaluation_id  # type: ignore[attr-defined]
                    for item in methodology_repository.promotion_evaluations_for_conclusion(
                        conclusion_id
                    )
                )
        if hypothesis_id is not None:
            trial_family_ids = tuple(
                item.trial_family_id  # type: ignore[attr-defined]
                for item in methodology_repository.trial_families_for_hypothesis(hypothesis_id)
            )

    return GenuineMultiAssetRegistry(
        cohort_policy_id=cohort_policy.policy_id,
        cohort_policy_version=cohort_policy.version,
        cohort_member_count=len(cohort_policy.members),
        historical_capture_plan_id=None if plan is None else plan.plan_id,
        equity_dataset_snapshot_id=None if snapshot is None else snapshot.snapshot_id,
        equity_coverage_start=None if snapshot is None else snapshot.earliest_observation_date,
        equity_coverage_end=None if snapshot is None else snapshot.latest_observation_date,
        historical_data_quality_report_id=(
            None if quality_report is None else quality_report.report_id
        ),
        quality_gate_report_id=None
        if quality_gate_report is None
        else quality_gate_report.report_id,
        quality_gate_usable_count=(
            None if quality_gate_report is None else quality_gate_report.usable_count
        ),
        quality_gate_usable_with_warnings_count=(
            None if quality_gate_report is None else quality_gate_report.usable_with_warnings_count
        ),
        quality_gate_insufficient_count=(
            None if quality_gate_report is None else quality_gate_report.insufficient_count
        ),
        quality_gate_rejected_count=(
            None if quality_gate_report is None else quality_gate_report.rejected_count
        ),
        cross_market_dataset_id=(
            None if cross_market_dataset is None else cross_market_dataset.cross_market_dataset_id
        ),
        crypto_capture_ids=(
            () if cross_market_dataset is None else cross_market_dataset.crypto_capture_ids
        ),
        common_overlap_start=(
            None if cross_market_dataset is None else cross_market_dataset.common_overlap_start
        ),
        common_overlap_end=(
            None if cross_market_dataset is None else cross_market_dataset.common_overlap_end
        ),
        tmc_component_symbols=(
            () if cross_market_dataset is None else cross_market_dataset.tmc_component_symbols
        ),
        replay_run_ids=replay_run_ids,
        governance_policy_ids=governance_policy_ids,
        conclusion_ids=conclusion_ids,
        promotion_evaluation_ids=promotion_evaluation_ids,
        walk_forward_run_ids=walk_forward_run_ids,
        robustness_run_ids=robustness_run_ids,
        trial_family_ids=trial_family_ids,
    )


class InMemoryGenuineMultiAssetHistoryRepository:
    def __init__(self) -> None:
        self._reports: dict[str, GenuineCohortQualityGateReport] = {}
        self._datasets: dict[str, CrossMarketDataset] = {}

    def quality_gate_report_by_id(self, report_id: str) -> GenuineCohortQualityGateReport | None:
        return self._reports.get(report_id)

    def save_quality_gate_report(
        self, report: GenuineCohortQualityGateReport
    ) -> GenuineCohortQualityGateReport:
        self._reports.setdefault(report.report_id, report)
        return self._reports[report.report_id]

    def list_quality_gate_reports(self) -> tuple[GenuineCohortQualityGateReport, ...]:
        return tuple(sorted(self._reports.values(), key=lambda item: item.created_at))

    def cross_market_dataset_by_id(self, dataset_id: str) -> CrossMarketDataset | None:
        return self._datasets.get(dataset_id)

    def save_cross_market_dataset(self, dataset: CrossMarketDataset) -> CrossMarketDataset:
        self._datasets.setdefault(dataset.cross_market_dataset_id, dataset)
        return self._datasets[dataset.cross_market_dataset_id]

    def list_cross_market_datasets(self) -> tuple[CrossMarketDataset, ...]:
        return tuple(sorted(self._datasets.values(), key=lambda item: item.created_at))
