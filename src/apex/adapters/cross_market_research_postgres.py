"""PostgreSQL repository for controlled cross-market hypothesis research
(migration ``0081``).

Mirrors the ``payload``-column pattern ``adapters.genuine_multi_asset_
history_postgres`` already established: a handful of indexed top-level
columns for querying, and the full canonical dataclass retained as JSON
so ``rehydrate`` restores an exact original instance. ``cross_market_
agenda_run`` is the one deliberately mutable table (no immutable
trigger) -- every other table here is append-only and immutable.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.cross_market_research import (
    AgendaQualityReport,
    AgendaRun,
    FollowUpProposal,
    GeneratedHypothesis,
    HypothesisExecutionRecord,
    ResearchAgenda,
    ResearchContradiction,
    ResearchKnowledgeEntry,
)
from apex.domain.values import canonical_json_bytes

CROSS_MARKET_RESEARCH_IMMUTABLE_TABLES = (
    "cross_market_research_agenda",
    "cross_market_hypothesis",
    "cross_market_hypothesis_execution",
    "cross_market_knowledge_entry",
    "cross_market_contradiction",
    "cross_market_follow_up_proposal",
    "cross_market_agenda_quality_report",
)
CROSS_MARKET_RESEARCH_MUTABLE_TABLES = ("cross_market_agenda_run",)
CROSS_MARKET_RESEARCH_TABLES = (
    CROSS_MARKET_RESEARCH_IMMUTABLE_TABLES + CROSS_MARKET_RESEARCH_MUTABLE_TABLES
)

metadata = sa.MetaData()

cross_market_research_agenda = sa.Table(
    "cross_market_research_agenda",
    metadata,
    sa.Column("agenda_id", sa.String(160), primary_key=True),
    sa.Column("version", sa.String(80), nullable=False),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column("replay_run_id", sa.String(160), nullable=False),
    sa.Column("cross_market_dataset_id", sa.String(160), nullable=False),
    sa.Column("cohort_policy_id", sa.String(160), nullable=False),
    sa.Column("maximum_hypothesis_count", sa.Integer(), nullable=False),
    sa.Column("maximum_total_trials", sa.Integer(), nullable=False),
    sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

cross_market_hypothesis = sa.Table(
    "cross_market_hypothesis",
    metadata,
    sa.Column("hypothesis_id", sa.String(160), primary_key=True),
    sa.Column("agenda_id", sa.String(160), nullable=False),
    sa.Column("template_id", sa.String(80), nullable=False),
    sa.Column("family", sa.String(40), nullable=False),
    sa.Column("is_negative_control", sa.Boolean(), nullable=False),
    sa.Column("discovery_status", sa.String(40), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

cross_market_agenda_run = sa.Table(
    "cross_market_agenda_run",
    metadata,
    sa.Column("agenda_run_id", sa.String(160), primary_key=True),
    sa.Column("agenda_id", sa.String(160), nullable=False),
    sa.Column("cursor_ordinal", sa.Integer(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("failure_reason", sa.Text(), nullable=True),
    sa.Column("trials_executed", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

cross_market_hypothesis_execution = sa.Table(
    "cross_market_hypothesis_execution",
    metadata,
    sa.Column("execution_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_id", sa.String(160), nullable=False),
    sa.Column("agenda_id", sa.String(160), nullable=False),
    sa.Column("governance_policy_id", sa.String(160), nullable=False),
    sa.Column("conclusion_id", sa.String(160), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

cross_market_knowledge_entry = sa.Table(
    "cross_market_knowledge_entry",
    metadata,
    sa.Column("entry_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_id", sa.String(160), nullable=False),
    sa.Column("conclusion_id", sa.String(160), nullable=False),
    sa.Column("status", sa.String(40), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("supersedes_entry_id", sa.String(160), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

cross_market_contradiction = sa.Table(
    "cross_market_contradiction",
    metadata,
    sa.Column("contradiction_id", sa.String(160), primary_key=True),
    sa.Column("first_entry_id", sa.String(160), nullable=False),
    sa.Column("second_entry_id", sa.String(160), nullable=False),
    sa.Column("resolved", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

cross_market_follow_up_proposal = sa.Table(
    "cross_market_follow_up_proposal",
    metadata,
    sa.Column("follow_up_id", sa.String(160), primary_key=True),
    sa.Column("parent_hypothesis_id", sa.String(160), nullable=False),
    sa.Column("parent_conclusion_id", sa.String(160), nullable=False),
    sa.Column("kind", sa.String(40), nullable=False),
    sa.Column("requires_new_agenda", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

cross_market_agenda_quality_report = sa.Table(
    "cross_market_agenda_quality_report",
    metadata,
    sa.Column("report_id", sa.String(160), primary_key=True),
    sa.Column("agenda_id", sa.String(160), nullable=False),
    sa.Column("hypotheses_proposed", sa.Integer(), nullable=False),
    sa.Column("trials_run", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class CrossMarketResearchPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise CrossMarketResearchPersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresCrossMarketResearchRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- research agenda (immutable) ----------------------------------

    def agenda_by_id(self, agenda_id: str) -> ResearchAgenda | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(cross_market_research_agenda).where(
                        cross_market_research_agenda.c.agenda_id == agenda_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ResearchAgenda, _decoded(row))

    def save_agenda(self, agenda: ResearchAgenda) -> ResearchAgenda:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(cross_market_research_agenda.c.agenda_id).where(
                    cross_market_research_agenda.c.agenda_id == agenda.agenda_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return agenda
            connection.execute(
                cross_market_research_agenda.insert().values(
                    agenda_id=agenda.agenda_id,
                    version=agenda.version,
                    dataset_snapshot_id=agenda.dataset_snapshot_id,
                    replay_run_id=agenda.replay_run_id,
                    cross_market_dataset_id=agenda.cross_market_dataset_id,
                    cohort_policy_id=agenda.cohort_policy_id,
                    maximum_hypothesis_count=agenda.maximum_hypothesis_count,
                    maximum_total_trials=agenda.maximum_total_trials,
                    registered_at=agenda.registered_at,
                    payload=_payload(agenda),
                    authority="NONE",
                )
            )
        return agenda

    def list_agendas(self) -> tuple[ResearchAgenda, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(cross_market_research_agenda).order_by(
                    cross_market_research_agenda.c.registered_at
                )
            ).mappings()
            return tuple(rehydrate(ResearchAgenda, _decoded(row)) for row in rows)

    # --- generated hypotheses (immutable) -----------------------------

    def hypothesis_by_id(self, hypothesis_id: str) -> GeneratedHypothesis | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(cross_market_hypothesis).where(
                        cross_market_hypothesis.c.hypothesis_id == hypothesis_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(GeneratedHypothesis, _decoded(row))

    def save_hypothesis(self, hypothesis: GeneratedHypothesis) -> GeneratedHypothesis:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(cross_market_hypothesis.c.hypothesis_id).where(
                    cross_market_hypothesis.c.hypothesis_id == hypothesis.hypothesis_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return hypothesis
            connection.execute(
                cross_market_hypothesis.insert().values(
                    hypothesis_id=hypothesis.hypothesis_id,
                    agenda_id=hypothesis.agenda_id,
                    template_id=hypothesis.template_id.value,
                    family=hypothesis.family.value,
                    is_negative_control=hypothesis.is_negative_control,
                    discovery_status=hypothesis.discovery_status.value,
                    ordinal=hypothesis.ordinal,
                    created_at=hypothesis.created_at,
                    payload=_payload(hypothesis),
                    authority="NONE",
                )
            )
        return hypothesis

    def hypotheses_for_agenda(self, agenda_id: str) -> tuple[GeneratedHypothesis, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(cross_market_hypothesis)
                .where(cross_market_hypothesis.c.agenda_id == agenda_id)
                .order_by(cross_market_hypothesis.c.ordinal)
            ).mappings()
            return tuple(rehydrate(GeneratedHypothesis, _decoded(row)) for row in rows)

    # --- agenda run (the one mutable cursor) --------------------------

    def agenda_run_by_agenda(self, agenda_id: str) -> AgendaRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(cross_market_agenda_run).where(
                        cross_market_agenda_run.c.agenda_id == agenda_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(AgendaRun, _decoded(row))

    def agenda_run_by_id(self, agenda_run_id: str) -> AgendaRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(cross_market_agenda_run).where(
                        cross_market_agenda_run.c.agenda_run_id == agenda_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(AgendaRun, _decoded(row))

    def save_agenda_run(self, run: AgendaRun) -> AgendaRun:
        values = {
            "agenda_id": run.agenda_id,
            "cursor_ordinal": run.cursor_ordinal,
            "status": run.status.value,
            "failure_reason": run.failure_reason,
            "trials_executed": run.trials_executed,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "payload": _payload(run),
            "authority": "NONE",
        }
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(cross_market_agenda_run.c.agenda_run_id).where(
                    cross_market_agenda_run.c.agenda_run_id == run.agenda_run_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    cross_market_agenda_run.insert().values(
                        agenda_run_id=run.agenda_run_id, **values
                    )
                )
            else:
                connection.execute(
                    cross_market_agenda_run.update()
                    .where(cross_market_agenda_run.c.agenda_run_id == run.agenda_run_id)
                    .values(**values)
                )
        return run

    # --- hypothesis execution evidence (immutable) --------------------

    def execution_by_hypothesis(self, hypothesis_id: str) -> HypothesisExecutionRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(cross_market_hypothesis_execution).where(
                        cross_market_hypothesis_execution.c.hypothesis_id == hypothesis_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(HypothesisExecutionRecord, _decoded(row))

    def save_execution(self, record: HypothesisExecutionRecord) -> HypothesisExecutionRecord:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(cross_market_hypothesis_execution.c.execution_id).where(
                    cross_market_hypothesis_execution.c.hypothesis_id == record.hypothesis_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return record
            connection.execute(
                cross_market_hypothesis_execution.insert().values(
                    execution_id=record.execution_id,
                    hypothesis_id=record.hypothesis_id,
                    agenda_id=record.agenda_id,
                    governance_policy_id=record.governance_policy_id,
                    conclusion_id=record.conclusion_id,
                    created_at=record.created_at,
                    payload=_payload(record),
                    authority="NONE",
                )
            )
        return record

    def executions_for_agenda(self, agenda_id: str) -> tuple[HypothesisExecutionRecord, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(cross_market_hypothesis_execution)
                .where(cross_market_hypothesis_execution.c.agenda_id == agenda_id)
                .order_by(cross_market_hypothesis_execution.c.created_at)
            ).mappings()
            return tuple(rehydrate(HypothesisExecutionRecord, _decoded(row)) for row in rows)

    # --- research knowledge base (append-only revisions) --------------

    def knowledge_entries_for_hypothesis(
        self, hypothesis_id: str
    ) -> tuple[ResearchKnowledgeEntry, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(cross_market_knowledge_entry)
                .where(cross_market_knowledge_entry.c.hypothesis_id == hypothesis_id)
                .order_by(cross_market_knowledge_entry.c.revision)
            ).mappings()
            return tuple(rehydrate(ResearchKnowledgeEntry, _decoded(row)) for row in rows)

    def save_knowledge_entry(self, entry: ResearchKnowledgeEntry) -> ResearchKnowledgeEntry:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(cross_market_knowledge_entry.c.entry_id).where(
                    cross_market_knowledge_entry.c.entry_id == entry.entry_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return entry
            connection.execute(
                cross_market_knowledge_entry.insert().values(
                    entry_id=entry.entry_id,
                    hypothesis_id=entry.hypothesis_id,
                    conclusion_id=entry.conclusion_id,
                    status=entry.status.value,
                    revision=entry.revision,
                    supersedes_entry_id=entry.supersedes_entry_id,
                    created_at=entry.created_at,
                    payload=_payload(entry),
                    authority="NONE",
                )
            )
        return entry

    def list_knowledge_entries(self) -> tuple[ResearchKnowledgeEntry, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(cross_market_knowledge_entry).order_by(
                    cross_market_knowledge_entry.c.created_at
                )
            ).mappings()
            return tuple(rehydrate(ResearchKnowledgeEntry, _decoded(row)) for row in rows)

    # --- contradictions (immutable) -----------------------------------

    def save_contradiction(self, item: ResearchContradiction) -> ResearchContradiction:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(cross_market_contradiction.c.contradiction_id).where(
                    cross_market_contradiction.c.contradiction_id == item.contradiction_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                cross_market_contradiction.insert().values(
                    contradiction_id=item.contradiction_id,
                    first_entry_id=item.first_entry_id,
                    second_entry_id=item.second_entry_id,
                    resolved=item.resolved,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def list_contradictions(self) -> tuple[ResearchContradiction, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(cross_market_contradiction).order_by(
                    cross_market_contradiction.c.created_at
                )
            ).mappings()
            return tuple(rehydrate(ResearchContradiction, _decoded(row)) for row in rows)

    # --- follow-up proposals (immutable) ------------------------------

    def save_follow_up(self, item: FollowUpProposal) -> FollowUpProposal:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(cross_market_follow_up_proposal.c.follow_up_id).where(
                    cross_market_follow_up_proposal.c.follow_up_id == item.follow_up_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                cross_market_follow_up_proposal.insert().values(
                    follow_up_id=item.follow_up_id,
                    parent_hypothesis_id=item.parent_hypothesis_id,
                    parent_conclusion_id=item.parent_conclusion_id,
                    kind=item.kind.value,
                    requires_new_agenda=item.requires_new_agenda,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def list_follow_ups(self) -> tuple[FollowUpProposal, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(cross_market_follow_up_proposal).order_by(
                    cross_market_follow_up_proposal.c.created_at
                )
            ).mappings()
            return tuple(rehydrate(FollowUpProposal, _decoded(row)) for row in rows)

    # --- agenda-level research-quality report (immutable) -------------

    def quality_report_by_agenda(self, agenda_id: str) -> AgendaQualityReport | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(cross_market_agenda_quality_report).where(
                        cross_market_agenda_quality_report.c.agenda_id == agenda_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(AgendaQualityReport, _decoded(row))

    def save_quality_report(self, report: AgendaQualityReport) -> AgendaQualityReport:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(cross_market_agenda_quality_report.c.report_id).where(
                    cross_market_agenda_quality_report.c.agenda_id == report.agenda_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return report
            connection.execute(
                cross_market_agenda_quality_report.insert().values(
                    report_id=report.report_id,
                    agenda_id=report.agenda_id,
                    hypotheses_proposed=report.hypotheses_proposed,
                    trials_run=report.trials_run,
                    created_at=report.created_at,
                    payload=_payload(report),
                    authority="NONE",
                )
            )
        return report
