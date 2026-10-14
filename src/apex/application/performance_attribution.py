"""Exact-ID orchestration for shared PAPER attribution and strategy health."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.performance_attribution import (
    AttributionComponent,
    AttributionSourceSnapshot,
    PaperEvidenceClass,
    PaperPerformanceSummary,
    PaperVenue,
    PerformanceAttributionError,
    SharedPerformanceAttribution,
    StrategyHealthAssessment,
    assess_strategy_health,
    materialize_attribution,
    summarize_performance,
)


class PerformanceAttributionSource(Protocol):
    def snapshots(
        self, evidence_class: PaperEvidenceClass
    ) -> tuple[AttributionSourceSnapshot, ...]: ...

    def snapshot(
        self, source_id: str, evidence_class: PaperEvidenceClass
    ) -> AttributionSourceSnapshot | None: ...


class PerformanceAttributionRepository(Protocol):
    def attribution(self, attribution_id: str) -> SharedPerformanceAttribution | None: ...
    def attribution_for_source(
        self, source_id: str, source_hash: str, evidence_class: PaperEvidenceClass
    ) -> SharedPerformanceAttribution | None: ...
    def attributions(
        self, evidence_class: PaperEvidenceClass | None = None
    ) -> tuple[SharedPerformanceAttribution, ...]: ...
    def components(self, attribution_id: str) -> tuple[AttributionComponent, ...]: ...
    def health(self, health_id: str) -> StrategyHealthAssessment | None: ...
    def health_assessments(
        self, evidence_class: PaperEvidenceClass | None = None
    ) -> tuple[StrategyHealthAssessment, ...]: ...
    def venue_summary(
        self, venue: PaperVenue, evidence_class: PaperEvidenceClass
    ) -> PaperPerformanceSummary | None: ...
    def portfolio_summary(
        self, evidence_class: PaperEvidenceClass
    ) -> PaperPerformanceSummary | None: ...
    def method_summaries(
        self, evidence_class: PaperEvidenceClass
    ) -> tuple[PaperPerformanceSummary, ...]: ...
    def save_attribution(
        self,
        *,
        idempotency_key: str,
        attribution: SharedPerformanceAttribution,
        components: tuple[AttributionComponent, ...],
    ) -> bool: ...
    def save_health(
        self, *, idempotency_key: str, assessment: StrategyHealthAssessment
    ) -> bool: ...
    def save_summary(self, *, idempotency_key: str, summary: PaperPerformanceSummary) -> bool: ...


@dataclass(frozen=True, slots=True)
class AttributionMaterializationResult:
    attributions: tuple[SharedPerformanceAttribution, ...]
    components: tuple[AttributionComponent, ...]
    replayed_attribution_ids: tuple[str, ...]
    provider_requests: int = 0
    broker_requests: int = 0
    financial_mutations: int = 0
    live_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class HealthMaterializationResult:
    assessments: tuple[StrategyHealthAssessment, ...]
    method_summaries: tuple[PaperPerformanceSummary, ...]
    venue_summaries: tuple[PaperPerformanceSummary, ...]
    portfolio_summary: PaperPerformanceSummary
    replayed_ids: tuple[str, ...]
    operator_attention_items: tuple[str, ...]
    provider_requests: int = 0
    broker_requests: int = 0
    financial_mutations: int = 0
    live_authority: str = "NONE"


class MaterializePerformanceAttribution:
    def __init__(
        self,
        repository: PerformanceAttributionRepository,
        source: PerformanceAttributionSource,
    ) -> None:
        self._repository = repository
        self._source = source

    def execute(
        self,
        *,
        run_id: str,
        evidence_class: PaperEvidenceClass,
        materialized_at: datetime,
        idempotency_key: str,
        source_ids: tuple[str, ...] | None = None,
    ) -> AttributionMaterializationResult:
        if not run_id.strip() or not idempotency_key.strip():
            raise PerformanceAttributionError("ATTRIBUTION_MATERIALIZATION_IDENTITY_INVALID")
        snapshots = (
            self._source.snapshots(evidence_class)
            if source_ids is None
            else tuple(
                snapshot
                for source_id in source_ids
                if (snapshot := self._source.snapshot(source_id, evidence_class)) is not None
            )
        )
        if source_ids is not None and len(snapshots) != len(source_ids):
            raise PerformanceAttributionError("ATTRIBUTION_SOURCE_NOT_FOUND")
        records: list[SharedPerformanceAttribution] = []
        all_components: list[AttributionComponent] = []
        replays: list[str] = []
        for snapshot in sorted(snapshots, key=lambda item: item.source_id):
            retained = self._repository.attribution_for_source(
                snapshot.source_id, snapshot.source_hash, evidence_class
            )
            if retained is not None:
                records.append(retained)
                retained_components = self._repository.components(retained.attribution_id)
                all_components.extend(retained_components)
                replays.append(retained.attribution_id)
                continue
            record, components = materialize_attribution(
                attribution_id=f"{run_id}-{snapshot.source_id}-attribution",
                source=snapshot,
                materialized_at=materialized_at,
            )
            replay = self._repository.save_attribution(
                idempotency_key=f"{idempotency_key}:{snapshot.source_id}",
                attribution=record,
                components=components,
            )
            records.append(record)
            all_components.extend(components)
            if replay:
                replays.append(record.attribution_id)
        return AttributionMaterializationResult(
            tuple(records), tuple(all_components), tuple(replays)
        )


class MaterializeStrategyHealth:
    def __init__(self, repository: PerformanceAttributionRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        run_id: str,
        evidence_class: PaperEvidenceClass,
        as_of: datetime,
        idempotency_key: str,
        strategy_or_method_id: str | None = None,
    ) -> HealthMaterializationResult:
        retained = self._repository.attributions(evidence_class)
        current_by_source: dict[str, SharedPerformanceAttribution] = {}
        for item in sorted(
            retained, key=lambda value: (value.materialized_at, value.attribution_id)
        ):
            current_by_source[item.source_id] = item
        attributions = tuple(current_by_source[key] for key in sorted(current_by_source))
        grouped: dict[tuple[str, str], list[SharedPerformanceAttribution]] = {}
        for item in attributions:
            if strategy_or_method_id is None or item.strategy_or_method_id == strategy_or_method_id:
                grouped.setdefault(
                    (item.strategy_or_method_id, item.strategy_or_method_version), []
                ).append(item)
        assessments: list[StrategyHealthAssessment] = []
        method_summaries: list[PaperPerformanceSummary] = []
        replays: list[str] = []
        for (method_id, method_version), values in sorted(grouped.items()):
            assessment = assess_strategy_health(
                health_id=f"{run_id}-{method_id}-{method_version}-health",
                attributions=tuple(values),
                as_of=as_of,
            )
            if self._repository.save_health(
                idempotency_key=f"{idempotency_key}:health:{method_id}:{method_version}",
                assessment=assessment,
            ):
                replays.append(assessment.health_id)
            assessments.append(assessment)
            method_summary = summarize_performance(
                summary_id=f"{run_id}-{method_id}-{method_version}-summary",
                scope_type="STRATEGY_OR_METHOD",
                scope_id=f"{method_id}:{method_version}",
                attributions=tuple(values),
                evidence_class=evidence_class,
                as_of=as_of,
            )
            if self._repository.save_summary(
                idempotency_key=(f"{idempotency_key}:summary:method:{method_id}:{method_version}"),
                summary=method_summary,
            ):
                replays.append(method_summary.summary_id)
            method_summaries.append(method_summary)

        venue_summaries: list[PaperPerformanceSummary] = []
        for venue in PaperVenue:
            venue_values = tuple(item for item in attributions if item.venue is venue)
            summary = summarize_performance(
                summary_id=f"{run_id}-{venue.value.lower()}-summary",
                scope_type="VENUE",
                scope_id=venue.value,
                attributions=venue_values,
                evidence_class=evidence_class,
                as_of=as_of,
            )
            if self._repository.save_summary(
                idempotency_key=f"{idempotency_key}:summary:{venue.value}", summary=summary
            ):
                replays.append(summary.summary_id)
            venue_summaries.append(summary)
        portfolio = summarize_performance(
            summary_id=f"{run_id}-combined-paper-portfolio-summary",
            scope_type="PORTFOLIO",
            scope_id="COMBINED_PAPER_RESEARCH_PORTFOLIO",
            attributions=attributions,
            evidence_class=evidence_class,
            as_of=as_of,
        )
        if self._repository.save_summary(
            idempotency_key=f"{idempotency_key}:summary:portfolio", summary=portfolio
        ):
            replays.append(portfolio.summary_id)
        attention = tuple(
            f"{item.strategy_or_method_id}:{item.state.value}:{item.operator_recommendation}"
            for item in assessments
            if item.state.value not in {"HEALTHY", "NO_OPERATIONAL_EVIDENCE"}
        )
        return HealthMaterializationResult(
            tuple(assessments),
            tuple(method_summaries),
            tuple(venue_summaries),
            portfolio,
            tuple(replays),
            attention,
        )
