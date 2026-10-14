"""Immutable PostgreSQL persistence for governed research operations."""

from __future__ import annotations

import json
import types
from dataclasses import MISSING, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast, get_args, get_origin, get_type_hints

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.domain.first_generated_research_method import (
    FirstGeneratedResearchMethodImplementation,
    FirstGeneratedResearchMethodRun,
    FirstGeneratedResearchMethodSelection,
)
from apex.domain.research_operations import (
    PriorResearchKnowledge,
    ResearchAgendaItem,
    ResearchBudgetEvent,
    ResearchExperimentApproval,
    ResearchExperimentQueueEvent,
    ResearchExperimentQueueItem,
    ResearchOperationsError,
    ResearchProposal,
    ResearchProposalFeasibility,
    ResearchProposalGeneratorRun,
    ResearchProposalPriority,
    ResearchProposalReview,
    ResearchRecommendation,
    ResearchVenue,
)
from apex.domain.values import canonical_json_bytes, content_digest, parse_decimal

metadata = sa.MetaData()


def _table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
        sa.Column("live_authority", sa.String(16), nullable=False),
    )


agenda_table = _table(
    "research_agenda_item",
    "agenda_item_id",
    sa.Column("source_fact_id", sa.String(300), nullable=False),
    sa.Column("venue", sa.String(30), nullable=False),
    sa.Column("priority", sa.Integer(), nullable=False),
    sa.Column("status", sa.String(30), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
proposal_table = _table(
    "governed_research_proposal",
    "proposal_id",
    sa.Column("agenda_item_id", sa.String(300), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("venue", sa.String(30), nullable=False),
    sa.Column("origin", sa.String(30), nullable=False),
    sa.Column("semantic_hash", sa.String(64), nullable=False),
    sa.Column("configuration_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
review_table = _table(
    "governed_research_proposal_review",
    "review_id",
    sa.Column("proposal_id", sa.String(300), nullable=False, unique=True),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("to_state", sa.String(40), nullable=False),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
)
budget_table = _table(
    "governed_research_budget_event",
    "event_id",
    sa.Column("proposal_id", sa.String(300), nullable=False),
    sa.Column("run_id", sa.String(300), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("event_type", sa.String(40), nullable=False),
    sa.Column("consumed_runs", sa.Integer(), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
)
recommendation_table = _table(
    "governed_research_recommendation",
    "recommendation_id",
    sa.Column("proposal_id", sa.String(300), nullable=False),
    sa.Column("run_id", sa.String(300), nullable=False, unique=True),
    sa.Column("state", sa.String(40), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)
generator_run_table = _table(
    "research_proposal_generator_run",
    "generator_run_id",
    sa.Column("generator_id", sa.String(100), nullable=False),
    sa.Column("policy_id", sa.String(100), nullable=False),
    sa.Column("origin", sa.String(30), nullable=False),
    sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
)
priority_table = _table(
    "research_proposal_priority",
    "priority_id",
    sa.Column("generator_run_id", sa.String(300), nullable=False),
    sa.Column("proposal_id", sa.String(300), nullable=False),
    sa.Column("proposal_class", sa.String(40), nullable=False),
    sa.Column("total_score", sa.Integer(), nullable=False),
    sa.Column("deterministic_rank", sa.Integer(), nullable=False),
)
feasibility_table = _table(
    "research_proposal_feasibility",
    "feasibility_id",
    sa.Column("generator_run_id", sa.String(300), nullable=False),
    sa.Column("proposal_id", sa.String(300), nullable=False),
    sa.Column("status", sa.String(40), nullable=False),
    sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
)
approval_table = _table(
    "research_experiment_approval",
    "approval_id",
    sa.Column("proposal_id", sa.String(300), nullable=False, unique=True),
    sa.Column("review_id", sa.String(300), nullable=False, unique=True),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
)
queue_item_table = _table(
    "research_experiment_queue_item",
    "queue_id",
    sa.Column("proposal_id", sa.String(300), nullable=False, unique=True),
    sa.Column("approval_id", sa.String(300), nullable=False, unique=True),
    sa.Column("run_id", sa.String(300), nullable=False, unique=True),
    sa.Column("execution_order", sa.Integer(), nullable=False),
    sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
)
queue_event_table = _table(
    "research_experiment_queue_event",
    "event_id",
    sa.Column("queue_id", sa.String(300), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("to_status", sa.String(30), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
)
first_method_selection_table = _table(
    "first_generated_research_method_selection",
    "selection_id",
    sa.Column("generator_run_id", sa.String(300), nullable=False, unique=True),
    sa.Column("selected_proposal_id", sa.String(300), nullable=False, unique=True),
    sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
)
first_method_implementation_table = _table(
    "first_generated_research_method_implementation",
    "implementation_id",
    sa.Column("selection_id", sa.String(300), nullable=False, unique=True),
    sa.Column("proposal_id", sa.String(300), nullable=False, unique=True),
    sa.Column("method_id", sa.String(300), nullable=False, unique=True),
    sa.Column("implemented_at", sa.DateTime(timezone=True), nullable=False),
)
first_method_run_table = _table(
    "first_generated_research_method_run",
    "method_run_id",
    sa.Column("implementation_id", sa.String(300), nullable=False),
    sa.Column("proposal_id", sa.String(300), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
)

TABLE_NAMES = tuple(metadata.tables)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise ResearchOperationsError("RESEARCH_OPERATIONS_RETAINED_PAYLOAD_INVALID")
    return value


def _restore(type_hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(type_hint)
    arguments = get_args(type_hint)
    if origin is tuple:
        item_type = arguments[0] if arguments else Any
        return tuple(_restore(item_type, item) for item in value)
    if origin in {types.UnionType, getattr(types, "UnionType", object)}:
        possible = tuple(item for item in arguments if item is not type(None))
        return _restore(possible[0], value) if possible else None
    if type_hint is Decimal:
        return parse_decimal(str(value), allow_negative=True)
    if type_hint is datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    if isinstance(type_hint, type) and issubclass(type_hint, Enum):
        return type_hint(value)
    if isinstance(type_hint, type) and is_dataclass(type_hint):
        hints = get_type_hints(type_hint)
        return type_hint(
            **{
                field.name: _restore(hints[field.name], value[field.name])
                for field in fields(type_hint)
                if field.name in value
                or (field.default is MISSING and field.default_factory is MISSING)
            }
        )
    return value


def _object[T](row: RowMapping, result_type: type[T]) -> T:
    return cast(T, _restore(result_type, _data(row)))


class PostgresResearchOperationsRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def _rows(self, statement: sa.Select[Any]) -> tuple[RowMapping, ...]:
        with self._engine.connect() as connection:
            return tuple(connection.execute(statement).mappings())

    def _save(
        self,
        *,
        table: sa.Table,
        identity_name: str,
        identity: str,
        value: object,
        extra: dict[str, object],
    ) -> bool:
        canonical_hash = str(cast(Any, value).canonical_hash)
        retained = self._one(sa.select(table).where(table.c[identity_name] == identity))
        if retained is not None:
            if str(retained["canonical_hash"]) != canonical_hash:
                raise ResearchOperationsError("RESEARCH_OPERATIONS_IDENTITY_CONFLICT")
            return False
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    sa.insert(table).values(
                        **{identity_name: identity},
                        **extra,
                        payload=_payload(value),
                        canonical_hash=canonical_hash,
                        authority=str(getattr(value, "authority", "NONE")),
                        live_authority=str(getattr(value, "live_authority", "NONE")),
                    )
                )
        except IntegrityError as error:
            raise ResearchOperationsError("RESEARCH_OPERATIONS_PERSISTENCE_CONFLICT") from error
        return True

    def agenda_item(self, agenda_item_id: str) -> ResearchAgendaItem | None:
        row = self._one(
            sa.select(agenda_table).where(agenda_table.c.agenda_item_id == agenda_item_id)
        )
        return None if row is None else _object(row, ResearchAgendaItem)

    def agenda_items(self) -> tuple[ResearchAgendaItem, ...]:
        return tuple(
            _object(row, ResearchAgendaItem)
            for row in self._rows(
                sa.select(agenda_table).order_by(
                    agenda_table.c.priority.desc(), agenda_table.c.agenda_item_id
                )
            )
        )

    def save_agenda_item(self, value: ResearchAgendaItem) -> bool:
        return self._save(
            table=agenda_table,
            identity_name="agenda_item_id",
            identity=value.agenda_item_id,
            value=value,
            extra={
                "source_fact_id": value.source_fact_id,
                "venue": value.venue.value,
                "priority": value.priority,
                "status": value.status.value,
                "created_at": value.created_at,
            },
        )

    def proposal(self, proposal_id: str) -> ResearchProposal | None:
        row = self._one(
            sa.select(proposal_table).where(proposal_table.c.proposal_id == proposal_id)
        )
        return None if row is None else _object(row, ResearchProposal)

    def proposals(self) -> tuple[ResearchProposal, ...]:
        return tuple(
            _object(row, ResearchProposal)
            for row in self._rows(
                sa.select(proposal_table).order_by(
                    proposal_table.c.created_at, proposal_table.c.proposal_id
                )
            )
        )

    def save_proposal(self, value: ResearchProposal) -> bool:
        if self.agenda_item(value.agenda_item_id) is None:
            raise ResearchOperationsError("RESEARCH_PROPOSAL_AGENDA_ITEM_NOT_FOUND")
        return self._save(
            table=proposal_table,
            identity_name="proposal_id",
            identity=value.proposal_id,
            value=value,
            extra={
                "agenda_item_id": value.agenda_item_id,
                "version": value.version,
                "venue": value.venue.value,
                "origin": value.origin.value,
                "semantic_hash": value.semantic_hash,
                "configuration_hash": value.experiment_configuration_hash,
                "created_at": value.created_at,
            },
        )

    def reviews(self, proposal_id: str) -> tuple[ResearchProposalReview, ...]:
        return tuple(
            _object(row, ResearchProposalReview)
            for row in self._rows(
                sa.select(review_table)
                .where(review_table.c.proposal_id == proposal_id)
                .order_by(review_table.c.ordinal)
            )
        )

    def save_review(self, value: ResearchProposalReview) -> bool:
        return self._save(
            table=review_table,
            identity_name="review_id",
            identity=value.review_id,
            value=value,
            extra={
                "proposal_id": value.proposal_id,
                "ordinal": value.ordinal,
                "to_state": value.to_state.value,
                "reviewed_at": value.reviewed_at,
            },
        )

    def budget_events(self, proposal_id: str) -> tuple[ResearchBudgetEvent, ...]:
        return tuple(
            _object(row, ResearchBudgetEvent)
            for row in self._rows(
                sa.select(budget_table)
                .where(budget_table.c.proposal_id == proposal_id)
                .order_by(budget_table.c.ordinal)
            )
        )

    def save_budget_event(self, value: ResearchBudgetEvent) -> bool:
        return self._save(
            table=budget_table,
            identity_name="event_id",
            identity=value.event_id,
            value=value,
            extra={
                "proposal_id": value.proposal_id,
                "run_id": value.run_id,
                "ordinal": value.ordinal,
                "event_type": value.event_type.value,
                "consumed_runs": value.consumed_runs,
                "occurred_at": value.occurred_at,
            },
        )

    def recommendation_for_run(self, run_id: str) -> ResearchRecommendation | None:
        row = self._one(
            sa.select(recommendation_table).where(recommendation_table.c.run_id == run_id)
        )
        return None if row is None else _object(row, ResearchRecommendation)

    def recommendations(self) -> tuple[ResearchRecommendation, ...]:
        return tuple(
            _object(row, ResearchRecommendation)
            for row in self._rows(
                sa.select(recommendation_table).order_by(
                    recommendation_table.c.created_at, recommendation_table.c.recommendation_id
                )
            )
        )

    def save_recommendation(self, value: ResearchRecommendation) -> bool:
        return self._save(
            table=recommendation_table,
            identity_name="recommendation_id",
            identity=value.recommendation_id,
            value=value,
            extra={
                "proposal_id": value.proposal_id,
                "run_id": value.run_id,
                "state": value.state.value,
                "created_at": value.created_at,
            },
        )

    def generator_run(self, generator_run_id: str) -> ResearchProposalGeneratorRun | None:
        row = self._one(
            sa.select(generator_run_table).where(
                generator_run_table.c.generator_run_id == generator_run_id
            )
        )
        return None if row is None else _object(row, ResearchProposalGeneratorRun)

    def generator_runs(self) -> tuple[ResearchProposalGeneratorRun, ...]:
        return tuple(
            _object(row, ResearchProposalGeneratorRun)
            for row in self._rows(
                sa.select(generator_run_table).order_by(
                    generator_run_table.c.generated_at,
                    generator_run_table.c.generator_run_id,
                )
            )
        )

    def save_generator_run(self, value: ResearchProposalGeneratorRun) -> bool:
        return self._save(
            table=generator_run_table,
            identity_name="generator_run_id",
            identity=value.generator_run_id,
            value=value,
            extra={
                "generator_id": value.generator_id,
                "policy_id": value.policy_id,
                "origin": value.origin.value,
                "generated_at": value.generated_at,
            },
        )

    def proposal_priority(self, proposal_id: str) -> ResearchProposalPriority | None:
        row = self._one(
            sa.select(priority_table).where(priority_table.c.proposal_id == proposal_id)
        )
        return None if row is None else _object(row, ResearchProposalPriority)

    def proposal_priorities(self) -> tuple[ResearchProposalPriority, ...]:
        return tuple(
            _object(row, ResearchProposalPriority)
            for row in self._rows(
                sa.select(priority_table).order_by(
                    priority_table.c.total_score.desc(),
                    priority_table.c.deterministic_rank,
                    priority_table.c.proposal_id,
                )
            )
        )

    def save_proposal_priority(self, value: ResearchProposalPriority) -> bool:
        return self._save(
            table=priority_table,
            identity_name="priority_id",
            identity=value.priority_id,
            value=value,
            extra={
                "generator_run_id": value.generator_run_id,
                "proposal_id": value.proposal_id,
                "proposal_class": value.proposal_class.value,
                "total_score": value.total_score,
                "deterministic_rank": value.deterministic_rank,
            },
        )

    def proposal_feasibility(self, proposal_id: str) -> ResearchProposalFeasibility | None:
        row = self._one(
            sa.select(feasibility_table)
            .where(feasibility_table.c.proposal_id == proposal_id)
            .order_by(
                feasibility_table.c.evaluated_at.desc(),
                feasibility_table.c.feasibility_id.desc(),
            )
            .limit(1)
        )
        return None if row is None else _object(row, ResearchProposalFeasibility)

    def proposal_feasibilities(self) -> tuple[ResearchProposalFeasibility, ...]:
        return tuple(
            _object(row, ResearchProposalFeasibility)
            for row in self._rows(
                sa.select(feasibility_table).order_by(
                    feasibility_table.c.proposal_id,
                    feasibility_table.c.evaluated_at,
                    feasibility_table.c.feasibility_id,
                )
            )
        )

    def save_proposal_feasibility(self, value: ResearchProposalFeasibility) -> bool:
        return self._save(
            table=feasibility_table,
            identity_name="feasibility_id",
            identity=value.feasibility_id,
            value=value,
            extra={
                "generator_run_id": value.generator_run_id,
                "proposal_id": value.proposal_id,
                "status": value.status.value,
                "evaluated_at": value.evaluated_at,
            },
        )

    def experiment_approval(self, proposal_id: str) -> ResearchExperimentApproval | None:
        row = self._one(
            sa.select(approval_table).where(approval_table.c.proposal_id == proposal_id)
        )
        return None if row is None else _object(row, ResearchExperimentApproval)

    def save_experiment_approval(self, value: ResearchExperimentApproval) -> bool:
        return self._save(
            table=approval_table,
            identity_name="approval_id",
            identity=value.approval_id,
            value=value,
            extra={
                "proposal_id": value.proposal_id,
                "review_id": value.review_id,
                "idempotency_key": value.idempotency_key,
                "approved_at": value.approved_at,
            },
        )

    def queue_item(self, queue_id: str) -> ResearchExperimentQueueItem | None:
        row = self._one(sa.select(queue_item_table).where(queue_item_table.c.queue_id == queue_id))
        return None if row is None else _object(row, ResearchExperimentQueueItem)

    def queue_items(self) -> tuple[ResearchExperimentQueueItem, ...]:
        return tuple(
            _object(row, ResearchExperimentQueueItem)
            for row in self._rows(
                sa.select(queue_item_table).order_by(
                    queue_item_table.c.execution_order,
                    queue_item_table.c.queue_id,
                )
            )
        )

    def save_queue_item(self, value: ResearchExperimentQueueItem) -> bool:
        return self._save(
            table=queue_item_table,
            identity_name="queue_id",
            identity=value.queue_id,
            value=value,
            extra={
                "proposal_id": value.proposal_id,
                "approval_id": value.approval_id,
                "run_id": value.run_id,
                "execution_order": value.execution_order,
                "enqueued_at": value.enqueued_at,
            },
        )

    def queue_events(self, queue_id: str) -> tuple[ResearchExperimentQueueEvent, ...]:
        return tuple(
            _object(row, ResearchExperimentQueueEvent)
            for row in self._rows(
                sa.select(queue_event_table)
                .where(queue_event_table.c.queue_id == queue_id)
                .order_by(queue_event_table.c.ordinal)
            )
        )

    def save_queue_event(self, value: ResearchExperimentQueueEvent) -> bool:
        return self._save(
            table=queue_event_table,
            identity_name="event_id",
            identity=value.event_id,
            value=value,
            extra={
                "queue_id": value.queue_id,
                "ordinal": value.ordinal,
                "to_status": value.to_status.value,
                "occurred_at": value.occurred_at,
            },
        )

    def first_method_selection(self) -> FirstGeneratedResearchMethodSelection | None:
        row = self._one(
            sa.select(first_method_selection_table)
            .order_by(first_method_selection_table.c.selected_at.desc())
            .limit(1)
        )
        return None if row is None else _object(row, FirstGeneratedResearchMethodSelection)

    def save_first_method_selection(self, value: FirstGeneratedResearchMethodSelection) -> bool:
        return self._save(
            table=first_method_selection_table,
            identity_name="selection_id",
            identity=value.selection_id,
            value=value,
            extra={
                "generator_run_id": value.generator_run_id,
                "selected_proposal_id": value.selected_proposal_id,
                "selected_at": value.selected_at,
            },
        )

    def first_method_implementation(
        self,
    ) -> FirstGeneratedResearchMethodImplementation | None:
        row = self._one(
            sa.select(first_method_implementation_table)
            .order_by(first_method_implementation_table.c.implemented_at.desc())
            .limit(1)
        )
        return None if row is None else _object(row, FirstGeneratedResearchMethodImplementation)

    def save_first_method_implementation(
        self, value: FirstGeneratedResearchMethodImplementation
    ) -> bool:
        return self._save(
            table=first_method_implementation_table,
            identity_name="implementation_id",
            identity=value.implementation_id,
            value=value,
            extra={
                "selection_id": value.selection_id,
                "proposal_id": value.proposal_id,
                "method_id": value.method_id,
                "implemented_at": value.implemented_at,
            },
        )

    def first_method_run(self, method_run_id: str) -> FirstGeneratedResearchMethodRun | None:
        row = self._one(
            sa.select(first_method_run_table).where(
                first_method_run_table.c.method_run_id == method_run_id
            )
        )
        return None if row is None else _object(row, FirstGeneratedResearchMethodRun)

    def first_method_runs(self) -> tuple[FirstGeneratedResearchMethodRun, ...]:
        return tuple(
            _object(row, FirstGeneratedResearchMethodRun)
            for row in self._rows(
                sa.select(first_method_run_table).order_by(
                    first_method_run_table.c.completed_at,
                    first_method_run_table.c.method_run_id,
                )
            )
        )

    def save_first_method_run(self, value: FirstGeneratedResearchMethodRun) -> bool:
        return self._save(
            table=first_method_run_table,
            identity_name="method_run_id",
            identity=value.method_run_id,
            value=value,
            extra={
                "implementation_id": value.implementation_id,
                "proposal_id": value.proposal_id,
                "completed_at": value.completed_at,
            },
        )

    def workspace(self) -> dict[str, object]:
        from apex.application.research_operations import research_operations_workspace

        return research_operations_workspace(self)


class PostgresRetainedResearchKnowledgeSource:
    """Read only stable semantic identities already retained by Apex."""

    _TABLES = (
        ("research_experiment_family", "experiment_id"),
        ("crypto_strategy_specification", "strategy_id"),
        ("crypto_challenger_hypothesis", "hypothesis_id"),
        ("crypto_campaign_candidate_hypothesis", "hypothesis_id"),
    )

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def knowledge(self) -> tuple[PriorResearchKnowledge, ...]:
        inspector = sa.inspect(self._engine)
        available = set(inspector.get_table_names())
        values: list[PriorResearchKnowledge] = []
        with self._engine.connect() as connection:
            for table_name, identity_name in self._TABLES:
                if table_name not in available:
                    continue
                rows = connection.execute(sa.text(f"SELECT * FROM {table_name}"))
                for row in rows.mappings():
                    identity = str(row.get(identity_name) or row.get("id"))
                    payload_text = row.get("payload")
                    payload = json.loads(str(payload_text)) if payload_text else {}
                    if not isinstance(payload, dict):
                        payload = {}
                    venue = (
                        ResearchVenue.KALSHI
                        if "kalshi" in json.dumps(payload).lower()
                        else ResearchVenue.CRYPTO
                    )
                    family = str(
                        payload.get("research_family")
                        or payload.get("strategy_id")
                        or payload.get("family")
                        or identity
                    )
                    semantic_hash = str(
                        payload.get("semantic_hash")
                        or payload.get("canonical_input_hash")
                        or row.get("canonical_hash")
                        or content_digest((table_name, identity, payload))
                    )
                    if len(semantic_hash) != 64:
                        semantic_hash = content_digest((table_name, identity, payload))
                    configuration_hash = str(
                        payload.get("parameter_space_hash")
                        or payload.get("configuration_hash")
                        or semantic_hash
                    )
                    if len(configuration_hash) != 64:
                        configuration_hash = content_digest((semantic_hash, configuration_hash))
                    values.append(
                        PriorResearchKnowledge(
                            research_id=identity,
                            venue=venue,
                            proposed_family=family,
                            semantic_hash=semantic_hash,
                            experiment_configuration_hash=configuration_hash,
                            validation_windows=(),
                            rejected=str(payload.get("state", "")).upper()
                            in {"REJECTED", "INVALIDATED", "CANCELED"},
                            canonical_hash=str(
                                row.get("canonical_hash") or content_digest(payload)
                            ),
                        )
                    )
        return tuple(sorted(values, key=lambda value: value.research_id))
