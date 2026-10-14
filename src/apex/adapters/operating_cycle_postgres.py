"""PostgreSQL persistence and retained-state reads for Apex operating cycles."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.cross_venue_allocation_postgres import (
    PostgresCrossVenueAllocationRepository,
)
from apex.adapters.crypto_market_pulse_postgres import PostgresCryptoMarketPulseRepository
from apex.adapters.investment_mandate_postgres import PostgresInvestmentMandateRepository
from apex.adapters.kalshi_operating_postgres import PostgresKalshiOperatingRepository
from apex.adapters.options_quote_intelligence_postgres import (
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.adapters.options_research_capture import resolve_options_capture_due_decision
from apex.adapters.options_research_operations_postgres import (
    PostgresOptionsResearchOperationsRepository,
)
from apex.adapters.performance_attribution_postgres import (
    PostgresPerformanceAttributionRepository,
)
from apex.adapters.performance_attribution_sources import (
    RetainedPerformanceAttributionSource,
)
from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
from apex.adapters.postgres import (
    binary_complement_evaluation,
    securities_market_data_batch,
)
from apex.adapters.research_operations_postgres import (
    PostgresResearchOperationsRepository,
)
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.total_market_context_postgres import PostgresTotalMarketContextRepository
from apex.adapters.total_market_context_sources import GENUINE_TOTAL_MARKET_DATASET
from apex.application.operating_cycle import OperatingCycleRepository
from apex.application.research_operations import proposal_state
from apex.domain.investment_mandate import DEFAULT_MANDATE_LINEAGE_ID
from apex.domain.operating_cycle import (
    OPERATING_WARNING,
    DueDisposition,
    DueTask,
    DueWorkPlan,
    OperatingBrief,
    OperatingControlEvent,
    OperatingControlState,
    OperatingCycleError,
    OperatingCycleEvent,
    OperatingCyclePlan,
    OperatingCycleRun,
    OperatingCycleState,
    OperatingHealth,
    OperatingHealthState,
    OperatingTaskResult,
    OperatingTaskStatus,
    OperatingTaskType,
    PaperActivationEvent,
    PaperActivationState,
    RetainedOperatingState,
    effective_paper_activation,
)
from apex.domain.performance_attribution import PaperEvidenceClass
from apex.domain.research_evidence import EvidenceClass
from apex.domain.research_operations import ResearchProposalState
from apex.domain.shared_opportunity import EligibilityState, SharedVenue
from apex.domain.values import canonical_json_bytes, content_digest

metadata = sa.MetaData()

# Crypto Universe Recurring Refresh & Dependency Health v1: genuine, currently
# always-on capability support for ``CRYPTO_UNIVERSE_REFRESH`` -- deliberately
# a plain constant, not a persisted per-cycle probe. Unlike
# ``KalshiCapability.public_refresh_available`` (which genuinely can vary,
# because it also gates private/credentialed Kalshi readiness behind a
# retained inspection event), Coinbase's public product-catalog endpoint this
# task composes (``CapturePublicCryptoProducts`` + ``CaptureCryptoUniverseObservations``
# + ``MaterializeCryptoUniverse``, over ``CoinbasePublicUniverseSource``) is
# unconditionally credential-free and account-independent -- the exact same
# capability ``CRYPTO_MARKET_PULSE`` already relies on with no "supported"
# gate in front of it at all (see ``plan_due_work``). There is nothing
# genuinely variable left to probe: the only real-world failure mode is a
# transient request failing at execution time, which the existing
# DUE -> attempt -> COMPLETED/FAILED task-result lifecycle already reports
# honestly without needing a separate capability flag. Before this milestone
# this was hardcoded ``False`` -- not because the capability was unsafe or
# unimplemented, but because the recurring cycle was never wired to the
# already-existing ``crypto_universe`` capture/materialize services at all
# (no executor branch existed for this task type). That prerequisite is now
# met, so the honest value is ``True``; ``plan_due_work``'s own
# ``DueDisposition.UNAVAILABLE`` branch for this task remains fully
# implemented and covered (see ``tests/test_operating_cycle.py``) for the
# case where a future deployment genuinely lacks this capability.
CRYPTO_UNIVERSE_REFRESH_GENUINELY_SUPPORTED = True


def _table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
    )


plan_table = _table(
    "apex_operating_cycle_plan",
    "plan_id",
    sa.Column("enabled", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
)
control_table = _table(
    "apex_operating_control_event",
    "event_id",
    sa.Column("plan_id", sa.String(300), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("state", sa.String(20), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.UniqueConstraint("plan_id", "ordinal", name="uq_operating_control_plan_ordinal"),
)
paper_activation_table = _table(
    "apex_operating_paper_activation_event",
    "event_id",
    sa.Column("plan_id", sa.String(300), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("state", sa.String(20), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.UniqueConstraint("plan_id", "ordinal", name="uq_operating_paper_activation_plan_ordinal"),
)
cycle_table = _table(
    "apex_operating_cycle_run",
    "cycle_id",
    sa.Column("plan_id", sa.String(300), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("due_work_payload", sa.Text(), nullable=False),
    sa.Column("due_work_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.UniqueConstraint("plan_id", "as_of", name="uq_operating_cycle_plan_as_of"),
)
cycle_event_table = _table(
    "apex_operating_cycle_event",
    "event_id",
    sa.Column("cycle_id", sa.String(300), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("state", sa.String(40), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("cycle_id", "ordinal", name="uq_operating_cycle_event_ordinal"),
)
task_table = _table(
    "apex_operating_task_result",
    "task_result_id",
    sa.Column("cycle_id", sa.String(300), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("task_type", sa.String(60), nullable=False),
    sa.Column("status", sa.String(40), nullable=False),
    sa.UniqueConstraint("cycle_id", "ordinal", name="uq_operating_task_cycle_ordinal"),
)
brief_table = _table(
    "apex_operating_brief",
    "brief_id",
    sa.Column("cycle_id", sa.String(300), nullable=False, unique=True),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
)
health_table = _table(
    "apex_operating_health",
    "health_id",
    sa.Column("cycle_id", sa.String(300), nullable=False, unique=True),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("state", sa.String(30), nullable=False),
)

TABLE_NAMES = tuple(metadata.tables)
T = TypeVar("T")


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise OperatingCycleError("OPERATING_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise OperatingCycleError("OPERATING_RETAINED_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _pairs(value: object) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, list):
        raise OperatingCycleError("OPERATING_RETAINED_PAIRS_INVALID")
    return tuple((str(item[0]), int(item[1])) for item in value)


def _plan(row: RowMapping) -> OperatingCyclePlan:
    value = _data(row)
    return OperatingCyclePlan(
        plan_id=str(value["plan_id"]),
        version=int(value["version"]),
        policy_id=str(value["policy_id"]),
        enabled=bool(value["enabled"]),
        timezone=str(value["timezone"]),
        utc_boundary_hour=int(value["utc_boundary_hour"]),
        crypto_pulse_cadence_hours=int(value["crypto_pulse_cadence_hours"]),
        crypto_universe_cadence_days=int(value["crypto_universe_cadence_days"]),
        total_market_cadence=str(value["total_market_cadence"]),
        kalshi_cadence=str(value["kalshi_cadence"]),
        shared_rebuild_trigger=str(value["shared_rebuild_trigger"]),
        context_rebuild_trigger=str(value["context_rebuild_trigger"]),
        allocation_rebuild_trigger=str(value["allocation_rebuild_trigger"]),
        performance_attribution_trigger=str(
            value.get(
                "performance_attribution_trigger",
                "WHEN_PAPER_ORDER_FILL_RECONCILIATION_OR_SETTLEMENT_EVIDENCE_CHANGES",
            )
        ),
        freshness_policies=_pairs(value["freshness_policies"]),
        request_bounds=_pairs(value["request_bounds"]),
        failure_behavior=_strings(value["failure_behavior"]),
        retry_policy=_strings(value["retry_policy"]),
        crypto_pulse_plan_id=str(value["crypto_pulse_plan_id"]),
        actor=str(value["actor"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        paper_eligible=False,
        live_eligible=False,
        capital_authority="NONE",
        reservation_authority="NONE",
        order_authority="NONE",
        broker_authority="NONE",
        financial_mutation_capability="NONE",
    )


def _control(row: RowMapping) -> OperatingControlEvent:
    value = _data(row)
    return OperatingControlEvent(
        event_id=str(value["event_id"]),
        plan_id=str(value["plan_id"]),
        ordinal=int(value["ordinal"]),
        state=OperatingControlState(value["state"]),
        reason=str(value["reason"]),
        actor=str(value["actor"]),
        occurred_at=_time(value["occurred_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _paper_activation(row: RowMapping) -> PaperActivationEvent:
    value = _data(row)
    return PaperActivationEvent(
        event_id=str(value["event_id"]),
        plan_id=str(value["plan_id"]),
        ordinal=int(value["ordinal"]),
        state=PaperActivationState(value["state"]),
        reason=str(value["reason"]),
        actor=str(value["actor"]),
        occurred_at=_time(value["occurred_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _due_task(value: dict[str, Any]) -> DueTask:
    return DueTask(
        task_type=OperatingTaskType(value["task_type"]),
        ordinal=int(value["ordinal"]),
        disposition=DueDisposition(value["disposition"]),
        reasons=_strings(value["reasons"]),
        dependency_types=tuple(OperatingTaskType(item) for item in value["dependency_types"]),
        source_evidence_ids=_strings(value["source_evidence_ids"]),
        planned_identity=str(value["planned_identity"]),
        network_behavior=str(value["network_behavior"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _due(value: dict[str, Any]) -> DueWorkPlan:
    return DueWorkPlan(
        due_work_id=str(value["due_work_id"]),
        plan_id=str(value["plan_id"]),
        cycle_id=str(value["cycle_id"]),
        as_of=_time(value["as_of"]),
        retained_state_hash=str(value["retained_state_hash"]),
        tasks=tuple(_due_task(item) for item in value["tasks"]),
        dependency_order=tuple(OperatingTaskType(item) for item in value["dependency_order"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _cycle(row: RowMapping) -> OperatingCycleRun:
    value = _data(row)
    return OperatingCycleRun(
        cycle_id=str(value["cycle_id"]),
        plan_id=str(value["plan_id"]),
        as_of=_time(value["as_of"]),
        due_work_id=str(value["due_work_id"]),
        due_work_hash=str(value["due_work_hash"]),
        prior_cycle_id=(None if value["prior_cycle_id"] is None else str(value["prior_cycle_id"])),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _cycle_event(row: RowMapping) -> OperatingCycleEvent:
    value = _data(row)
    return OperatingCycleEvent(
        event_id=str(value["event_id"]),
        cycle_id=str(value["cycle_id"]),
        ordinal=int(value["ordinal"]),
        state=OperatingCycleState(value["state"]),
        occurred_at=_time(value["occurred_at"]),
        warnings=_strings(value["warnings"]),
        failures=_strings(value["failures"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _task(row: RowMapping) -> OperatingTaskResult:
    value = _data(row)
    return OperatingTaskResult(
        task_result_id=str(value["task_result_id"]),
        cycle_id=str(value["cycle_id"]),
        task_type=OperatingTaskType(value["task_type"]),
        ordinal=int(value["ordinal"]),
        planned_identity=str(value["planned_identity"]),
        input_identities=_strings(value["input_identities"]),
        output_identities=_strings(value["output_identities"]),
        reused_identities=_strings(value["reused_identities"]),
        status=OperatingTaskStatus(value["status"]),
        changed=bool(value["changed"]),
        retryable=bool(value["retryable"]),
        network_requests=int(value["network_requests"]),
        warning_codes=_strings(value["warning_codes"]),
        error_code=None if value["error_code"] is None else str(value["error_code"]),
        started_at=_time(value["started_at"]),
        completed_at=_time(value["completed_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _brief(row: RowMapping) -> OperatingBrief:
    value = _data(row)
    return OperatingBrief(
        brief_id=str(value["brief_id"]),
        cycle_id=str(value["cycle_id"]),
        as_of=_time(value["as_of"]),
        overall_system_state=str(value["overall_system_state"]),
        task_counts=_pairs(value["task_counts"]),
        crypto_universe_id=value["crypto_universe_id"],
        crypto_pulse_id=value["crypto_pulse_id"],
        total_market_context_id=value["total_market_context_id"],
        total_market_state=value["total_market_state"],
        kalshi_status=str(value["kalshi_status"]),
        shared_opportunity_set_id=value["shared_opportunity_set_id"],
        opportunity_counts=_pairs(value["opportunity_counts"]),
        allocation_id=value["allocation_id"],
        unallocated_cash=value["unallocated_cash"],
        largest_crypto_score_changes=_strings(value["largest_crypto_score_changes"]),
        stale_or_unavailable=_strings(value["stale_or_unavailable"]),
        warnings=_strings(value["warnings"]),
        failures=_strings(value["failures"]),
        operator_attention=_strings(value["operator_attention"]),
        source_state_hash=str(value["source_state_hash"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _health(row: RowMapping) -> OperatingHealth:
    value = _data(row)
    return OperatingHealth(
        health_id=str(value["health_id"]),
        cycle_id=str(value["cycle_id"]),
        as_of=_time(value["as_of"]),
        state=OperatingHealthState(value["state"]),
        provider_capabilities=tuple(
            (str(item[0]), str(item[1])) for item in value["provider_capabilities"]
        ),
        latest_successful_refresh=(
            None
            if value["latest_successful_refresh"] is None
            else _time(value["latest_successful_refresh"])
        ),
        next_due_at=None if value["next_due_at"] is None else _time(value["next_due_at"]),
        stale_data=_strings(value["stale_data"]),
        incomplete_cycles=int(value["incomplete_cycles"]),
        repeated_failures=int(value["repeated_failures"]),
        blocked_dependencies=_strings(value["blocked_dependencies"]),
        schema_revision=value["schema_revision"],
        operator_control=OperatingControlState(value["operator_control"]),
        warnings=_strings(value["warnings"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
        paper_eligible=False,
        live_eligible=False,
        capital_authority="NONE",
        reservation_authority="NONE",
        order_authority="NONE",
        broker_authority="NONE",
        financial_mutation_capability="NONE",
    )


class PostgresOperatingCycleRepository(OperatingCycleRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def _rows(self, statement: sa.Select[Any]) -> tuple[RowMapping, ...]:
        with self._engine.connect() as connection:
            return tuple(connection.execute(statement).mappings())

    def _get(
        self,
        table: sa.Table,
        column: sa.Column[Any],
        identity: str,
        decode: Callable[[RowMapping], T],
    ) -> T | None:
        row = self._one(sa.select(table).where(column == identity))
        return None if row is None else decode(row)

    def _save_immutable(
        self,
        *,
        table: sa.Table,
        identity_name: str,
        identity: str,
        value: Any,
        extra: dict[str, Any],
    ) -> bool:
        prior = self._one(sa.select(table).where(table.c[identity_name] == identity))
        if prior is not None:
            if str(prior["canonical_hash"]) != value.canonical_hash:
                raise OperatingCycleError("OPERATING_IDENTITY_CONFLICT")
            return True
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    table.insert().values(
                        **{
                            identity_name: identity,
                            **extra,
                            "payload": _payload(value),
                            "canonical_hash": value.canonical_hash,
                            "authority": "NONE",
                        }
                    )
                )
            return False
        except IntegrityError as error:
            raise OperatingCycleError("OPERATING_PERSISTENCE_CONFLICT") from error

    def plan(self, plan_id: str) -> OperatingCyclePlan | None:
        return self._get(plan_table, plan_table.c.plan_id, plan_id, _plan)

    def plans(self) -> tuple[OperatingCyclePlan, ...]:
        return tuple(
            _plan(row)
            for row in self._rows(sa.select(plan_table).order_by(plan_table.c.created_at))
        )

    def save_plan(self, *, idempotency_key: str, plan: OperatingCyclePlan) -> bool:
        prior = self.plan(plan.plan_id)
        if prior is not None:
            row = self._one(sa.select(plan_table).where(plan_table.c.plan_id == plan.plan_id))
            if (
                prior.canonical_hash != plan.canonical_hash
                or row is None
                or row["idempotency_key"] != idempotency_key
            ):
                raise OperatingCycleError("OPERATING_PLAN_IDENTITY_CONFLICT")
            return True
        return self._save_immutable(
            table=plan_table,
            identity_name="plan_id",
            identity=plan.plan_id,
            value=plan,
            extra={
                "enabled": plan.enabled,
                "created_at": plan.created_at,
                "idempotency_key": idempotency_key,
            },
        )

    def control_events(self, plan_id: str) -> tuple[OperatingControlEvent, ...]:
        return tuple(
            _control(row)
            for row in self._rows(
                sa.select(control_table)
                .where(control_table.c.plan_id == plan_id)
                .order_by(control_table.c.ordinal)
            )
        )

    def save_control_event(self, *, idempotency_key: str, event: OperatingControlEvent) -> bool:
        prior = self._get(control_table, control_table.c.event_id, event.event_id, _control)
        if prior is not None:
            row = self._one(
                sa.select(control_table).where(control_table.c.event_id == event.event_id)
            )
            if (
                prior.canonical_hash != event.canonical_hash
                or row is None
                or row["idempotency_key"] != idempotency_key
            ):
                raise OperatingCycleError("OPERATING_CONTROL_IDENTITY_CONFLICT")
            return True
        return self._save_immutable(
            table=control_table,
            identity_name="event_id",
            identity=event.event_id,
            value=event,
            extra={
                "plan_id": event.plan_id,
                "ordinal": event.ordinal,
                "state": event.state.value,
                "occurred_at": event.occurred_at,
                "idempotency_key": idempotency_key,
            },
        )

    def paper_activation_events(self, plan_id: str) -> tuple[PaperActivationEvent, ...]:
        return tuple(
            _paper_activation(row)
            for row in self._rows(
                sa.select(paper_activation_table)
                .where(paper_activation_table.c.plan_id == plan_id)
                .order_by(paper_activation_table.c.ordinal)
            )
        )

    def save_paper_activation_event(
        self, *, idempotency_key: str, event: PaperActivationEvent
    ) -> bool:
        prior = self._get(
            paper_activation_table,
            paper_activation_table.c.event_id,
            event.event_id,
            _paper_activation,
        )
        if prior is not None:
            row = self._one(
                sa.select(paper_activation_table).where(
                    paper_activation_table.c.event_id == event.event_id
                )
            )
            if (
                prior.canonical_hash != event.canonical_hash
                or row is None
                or row["idempotency_key"] != idempotency_key
            ):
                raise OperatingCycleError("OPERATING_PAPER_ACTIVATION_IDENTITY_CONFLICT")
            return True
        return self._save_immutable(
            table=paper_activation_table,
            identity_name="event_id",
            identity=event.event_id,
            value=event,
            extra={
                "plan_id": event.plan_id,
                "ordinal": event.ordinal,
                "state": event.state.value,
                "occurred_at": event.occurred_at,
                "idempotency_key": idempotency_key,
            },
        )

    def cycle(self, cycle_id: str) -> OperatingCycleRun | None:
        return self._get(cycle_table, cycle_table.c.cycle_id, cycle_id, _cycle)

    def cycle_idempotency_key(self, cycle_id: str) -> str | None:
        row = self._one(
            sa.select(cycle_table.c.idempotency_key).where(cycle_table.c.cycle_id == cycle_id)
        )
        return None if row is None else str(row["idempotency_key"])

    def cycle_for_plan_as_of(self, plan_id: str, as_of: datetime) -> OperatingCycleRun | None:
        row = self._one(
            sa.select(cycle_table).where(
                cycle_table.c.plan_id == plan_id, cycle_table.c.as_of == as_of
            )
        )
        return None if row is None else _cycle(row)

    def cycles(self) -> tuple[OperatingCycleRun, ...]:
        return tuple(
            _cycle(row) for row in self._rows(sa.select(cycle_table).order_by(cycle_table.c.as_of))
        )

    def save_cycle(
        self, *, idempotency_key: str, cycle: OperatingCycleRun, due_work: DueWorkPlan
    ) -> bool:
        occupied = self.cycle_for_plan_as_of(cycle.plan_id, cycle.as_of)
        if occupied is not None and occupied.cycle_id != cycle.cycle_id:
            raise OperatingCycleError("OPERATING_PLAN_AS_OF_CONFLICT")
        prior = self.cycle(cycle.cycle_id)
        if prior is not None:
            row = self._one(sa.select(cycle_table).where(cycle_table.c.cycle_id == cycle.cycle_id))
            if (
                prior.canonical_hash != cycle.canonical_hash
                or row is None
                or row["idempotency_key"] != idempotency_key
            ):
                raise OperatingCycleError("OPERATING_CYCLE_IDENTITY_CONFLICT")
            return True
        return self._save_immutable(
            table=cycle_table,
            identity_name="cycle_id",
            identity=cycle.cycle_id,
            value=cycle,
            extra={
                "plan_id": cycle.plan_id,
                "as_of": cycle.as_of,
                "due_work_payload": _payload(due_work),
                "due_work_hash": due_work.canonical_hash,
                "created_at": cycle.created_at,
                "idempotency_key": idempotency_key,
            },
        )

    def due_work(self, cycle_id: str) -> DueWorkPlan | None:
        row = self._one(
            sa.select(cycle_table.c.due_work_payload).where(cycle_table.c.cycle_id == cycle_id)
        )
        if row is None:
            return None
        value = json.loads(str(row["due_work_payload"]))
        return _due(value)

    def cycle_events(self, cycle_id: str) -> tuple[OperatingCycleEvent, ...]:
        return tuple(
            _cycle_event(row)
            for row in self._rows(
                sa.select(cycle_event_table)
                .where(cycle_event_table.c.cycle_id == cycle_id)
                .order_by(cycle_event_table.c.ordinal)
            )
        )

    def save_cycle_event(self, event: OperatingCycleEvent) -> bool:
        return self._save_immutable(
            table=cycle_event_table,
            identity_name="event_id",
            identity=event.event_id,
            value=event,
            extra={
                "cycle_id": event.cycle_id,
                "ordinal": event.ordinal,
                "state": event.state.value,
                "occurred_at": event.occurred_at,
            },
        )

    def task_results(self, cycle_id: str) -> tuple[OperatingTaskResult, ...]:
        return tuple(
            _task(row)
            for row in self._rows(
                sa.select(task_table)
                .where(task_table.c.cycle_id == cycle_id)
                .order_by(task_table.c.ordinal)
            )
        )

    def task_result(self, task_result_id: str) -> OperatingTaskResult | None:
        return self._get(task_table, task_table.c.task_result_id, task_result_id, _task)

    def save_task_result(self, result: OperatingTaskResult) -> bool:
        return self._save_immutable(
            table=task_table,
            identity_name="task_result_id",
            identity=result.task_result_id,
            value=result,
            extra={
                "cycle_id": result.cycle_id,
                "ordinal": result.ordinal,
                "task_type": result.task_type.value,
                "status": result.status.value,
            },
        )

    def brief(self, brief_id: str) -> OperatingBrief | None:
        return self._get(brief_table, brief_table.c.brief_id, brief_id, _brief)

    def briefs(self) -> tuple[OperatingBrief, ...]:
        return tuple(
            _brief(row) for row in self._rows(sa.select(brief_table).order_by(brief_table.c.as_of))
        )

    def save_brief(self, brief: OperatingBrief) -> bool:
        return self._save_immutable(
            table=brief_table,
            identity_name="brief_id",
            identity=brief.brief_id,
            value=brief,
            extra={"cycle_id": brief.cycle_id, "as_of": brief.as_of},
        )

    def health(self, health_id: str) -> OperatingHealth | None:
        return self._get(health_table, health_table.c.health_id, health_id, _health)

    def health_summaries(self) -> tuple[OperatingHealth, ...]:
        return tuple(
            _health(row)
            for row in self._rows(sa.select(health_table).order_by(health_table.c.as_of))
        )

    def save_health(self, health: OperatingHealth) -> bool:
        return self._save_immutable(
            table=health_table,
            identity_name="health_id",
            identity=health.health_id,
            value=health,
            extra={"cycle_id": health.cycle_id, "as_of": health.as_of, "state": health.state.value},
        )

    def workspace(self) -> dict[str, object]:
        plans, cycles, briefs, health = (
            self.plans(),
            self.cycles(),
            self.briefs(),
            self.health_summaries(),
        )
        plan = plans[-1] if plans else None
        cycle = cycles[-1] if cycles else None
        controls = self.control_events(plan.plan_id) if plan else ()
        paper_activations = self.paper_activation_events(plan.plan_id) if plan else ()
        return {
            "plan": plan,
            "operator_control": controls[-1] if controls else None,
            "paper_activation": paper_activations[-1] if paper_activations else None,
            "latest_cycle": cycle,
            "latest_cycle_state": (
                self.cycle_events(cycle.cycle_id)[-1].state
                if cycle and self.cycle_events(cycle.cycle_id)
                else None
            ),
            "due_work": self.due_work(cycle.cycle_id) if cycle else None,
            "task_results": self.task_results(cycle.cycle_id) if cycle else (),
            "operating_brief": briefs[-1] if briefs else None,
            "operating_health": health[-1] if health else None,
            "warnings": (OPERATING_WARNING,) if plan else ("NO_OPERATING_CYCLE_PLAN_RETAINED",),
            **_authority_payload(),
            # Only PAPER activation itself is dynamic here; every other authority
            # field in _authority_payload() stays a conservative fixed "NONE".
            "paper_eligible": effective_paper_activation(paper_activations),
        }


class PostgresOperatingStateSource:
    def __init__(
        self,
        engine: Engine,
        operating_repository: PostgresOperatingCycleRepository,
    ) -> None:
        self._engine = engine
        self._operating = operating_repository
        self._crypto = PostgresCryptoMarketPulseRepository(engine)
        self._kalshi = PostgresKalshiOperatingRepository(engine)
        self._shared = PostgresSharedOpportunityRepository(engine)
        self._context = PostgresTotalMarketContextRepository(engine)
        self._cross_venue = PostgresCrossVenueAllocationRepository(engine)
        self._performance = PostgresPerformanceAttributionRepository(engine)
        self._performance_source = RetainedPerformanceAttributionSource(engine)
        self._research_operations = PostgresResearchOperationsRepository(engine)
        self._options_quote = PostgresOptionsQuoteIntelligenceRepository(engine)
        self._options_checkpoints = PostgresOptionsResearchOperationsRepository(engine)
        self._portfolio = PostgresPortfolioSnapshotRepository(engine)
        self._mandate = PostgresInvestmentMandateRepository(engine)

    def _portfolio_and_mandate_state(
        self, *, evaluated_at: datetime
    ) -> tuple[
        str | None,
        str | None,
        datetime | None,
        bool,
        str | None,
        str | None,
        int | None,
        str | None,
    ]:
        """Genuine, point-in-time normalized-portfolio and investment-mandate
        state, resolved here for the identical reason ``_options_capture_state``
        below is: ``plan_due_work`` must not re-derive "is a mandate active"
        itself, since the exact same point-in-time query
        (``PostgresInvestmentMandateRepository.active_mandate``) is what
        ``EvaluateOpportunityAgainstMandate`` uses when the task actually
        executes -- computing it once here means due-state and execution can
        never silently disagree, exactly like the options due-check. Returns
        ``(portfolio_snapshot_id, portfolio_snapshot_hash,
        portfolio_snapshot_as_of, mandate_lineage_has_any_version,
        active_mandate_id, active_mandate_lineage_id, active_mandate_version,
        active_mandate_hash)`` -- the mandate fields are all ``None``/``False``
        when no version has ever been authored, mirroring how every other
        task here reports ``UNAVAILABLE`` rather than fabricating a decision
        from absent evidence."""

        latest_snapshot = self._portfolio.latest_portfolio_snapshot()
        latest_version = self._mandate.latest_mandate_version(DEFAULT_MANDATE_LINEAGE_ID)
        active = self._mandate.active_mandate(DEFAULT_MANDATE_LINEAGE_ID, as_of=evaluated_at)
        return (
            None if latest_snapshot is None else latest_snapshot.snapshot_id,
            None if latest_snapshot is None else latest_snapshot.canonical_hash,
            None if latest_snapshot is None else latest_snapshot.as_of,
            latest_version is not None,
            None if active is None else active.mandate_id,
            None if active is None else active.lineage_id,
            None if active is None else active.version,
            None if active is None else active.canonical_hash,
        )

    def _options_capture_state(
        self, *, as_of: datetime
    ) -> tuple[
        str | None,
        str | None,
        str | None,
        datetime | None,
        datetime | None,
        tuple[str, ...],
        datetime | None,
    ]:
        """Genuine, calendar-evidence-driven due state for the options
        research-evidence capture task -- computed here (not inline in
        ``plan_due_work``) because it genuinely depends on a bounded window
        of retained XNYS session evidence, not one retained scalar
        timestamp; see the field-level comment on ``RetainedOperatingState.
        options_capture_due_status``. Returns
        ``(plan_id, due_status, session_id, window_open_at, window_close_at,
        reasons, next_capture_window_open_at)`` -- all ``None``/empty when no
        options capture plan has ever been registered, mirroring how every
        other task here reports ``UNAVAILABLE`` rather than fabricating a
        decision from absent evidence."""

        plan, _window_policy, _sessions, decision = resolve_options_capture_due_decision(
            quote_repository=self._options_quote,
            checkpoint_repository=self._options_checkpoints,
            as_of=as_of,
        )
        if plan is None:
            return None, decision.status.value, None, None, None, decision.reasons, None
        return (
            plan.plan_id,
            decision.status.value,
            decision.session_id,
            decision.window_open_at,
            decision.window_close_at,
            decision.reasons,
            decision.next_capture_window_open_at,
        )

    def snapshot(
        self, *, as_of: datetime, evaluated_at: datetime | None = None
    ) -> RetainedOperatingState:
        # ``as_of`` remains the day-scoped evidence cutoff every existing
        # cadence/dedup rule below is built around (and what ``plan_due_work``
        # asserts this state's own ``captured_at`` equals). ``evaluated_at``
        # is the true wall-clock instant the cycle is genuinely being
        # evaluated at -- distinct from ``as_of`` only when a caller
        # deliberately separates them (the supervised worker pins ``as_of``
        # to UTC midnight for one-cycle-per-day identity, but must still
        # resolve time-of-day-sensitive due-ness, like the options capture
        # window, against real current time). Defaulting to ``as_of`` keeps
        # every existing caller/test that only ever passed one timestamp
        # byte-for-byte unchanged.
        resolved_evaluated_at = as_of if evaluated_at is None else evaluated_at
        universes = self._crypto.universe_snapshots()
        pulses = self._crypto.pulse_snapshots()
        contexts = self._context.context_snapshots()
        shared_sets = self._shared.opportunity_sets()
        enriched_sets = self._context.enriched_sets()
        pulse = pulses[-1] if pulses else None
        # Genuinely latest *retained* universe evidence -- deliberately NOT
        # ``pulse.universe_snapshot_id`` (Crypto Universe Recurring Refresh &
        # Dependency Health v1). A ``CryptoRefreshPlan`` pins one exact
        # ``universe_snapshot_id`` forever at plan-registration time (see
        # ``RegisterCryptoRefreshPlan``); resolving *this* field from
        # whichever universe the latest pulse happens to still be pinned to
        # would mean ``crypto_universe_created_at`` could never advance
        # merely by this task genuinely refreshing -- the pinned pulse plan
        # never re-adopts a newer snapshot on its own -- so
        # ``universe_age_due`` below would stay permanently true and
        # ``CRYPTO_UNIVERSE_REFRESH`` would look due again on literally every
        # future calendar day forever, violating its own seven-day cadence.
        # ``crypto_universe_id``/``crypto_universe_hash``/
        # ``crypto_universe_created_at`` instead answer "what is the newest
        # universe evidence Apex genuinely retains" -- independent of which
        # snapshot any one pulse plan is pinned to -- which is what this
        # task's own due-check needs, and a strictly more informative fact
        # for the operating brief/health summaries that also read it.
        # Precise pulse-to-universe lineage (Part 8's point-in-time concern)
        # is unaffected: it already lives on the pulse record itself
        # (``CryptoPulseSnapshot.universe_snapshot_id``), not here. Only
        # genuine, non-fixture evidence counts -- retained fixture universe
        # rows (from offline/demo acceptance) must never leak into the real
        # recurring-cycle due-state.
        genuine_universes = tuple(
            item for item in universes if item.evidence_class is EvidenceClass.HISTORICAL_PUBLIC
        )
        universe = genuine_universes[-1] if genuine_universes else None
        context = contexts[-1] if contexts else None
        shared = shared_sets[-1] if shared_sets else None
        enriched = enriched_sets[-1] if enriched_sets else None
        projections = (
            tuple(
                item
                for identity in shared.projection_ids
                if (item := self._shared.projection(identity)) is not None
            )
            if shared
            else ()
        )
        shared_pulse_ids = tuple(
            value
            for item in projections
            if item.venue is SharedVenue.CRYPTO
            for key, value in item.source_details
            if key == "pulse_id"
        )
        shared_kalshi_ids = tuple(
            sorted(
                item.source_record_id for item in projections if item.venue is SharedVenue.KALSHI
            )
        )
        latest_kalshi_refresh = self._kalshi.latest_refresh()
        kalshi_ids = (
            latest_kalshi_refresh.evaluation_ids
            if latest_kalshi_refresh is not None
            else shared_kalshi_ids
        )
        kalshi_capability = self._kalshi.latest_capability()
        allocation = (
            self._cross_venue.decision_for_enriched_set(enriched.enriched_set_id)
            if enriched
            else None
        )
        paper_sources = self._performance_source.snapshots(PaperEvidenceClass.GENUINE_PAPER)
        research_agenda = self._research_operations.agenda_items()
        research_proposals = self._research_operations.proposals()
        unattributed = tuple(
            item.source_id
            for item in paper_sources
            if self._performance.attribution_for_source(
                item.source_id, item.source_hash, PaperEvidenceClass.GENUINE_PAPER
            )
            is None
        )
        with self._engine.connect() as connection:
            batch_row = (
                connection.execute(
                    sa.select(securities_market_data_batch)
                    .where(
                        securities_market_data_batch.c.state == "READY",
                        securities_market_data_batch.c.dataset_name == GENUINE_TOTAL_MARKET_DATASET,
                        securities_market_data_batch.c.imported_at <= as_of,
                    )
                    .order_by(
                        securities_market_data_batch.c.last_period_end.desc(),
                        securities_market_data_batch.c.batch_id.desc(),
                    )
                )
                .mappings()
                .first()
            )
            kalshi_latest = connection.scalar(
                sa.select(sa.func.max(binary_complement_evaluation.c.evaluated_at))
            )
            revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        briefs = self._operating.briefs()
        cycles = self._operating.cycles()
        incomplete = 0
        repeated = 0
        latest_successful = None
        for cycle_record in cycles:
            events = self._operating.cycle_events(cycle_record.cycle_id)
            if not events or events[-1].state in {
                OperatingCycleState.PLANNED,
                OperatingCycleState.RUNNING,
                OperatingCycleState.PARTIAL,
            }:
                incomplete += 1
            if events and events[-1].state is OperatingCycleState.FAILED:
                repeated += 1
            if events and events[-1].state in {
                OperatingCycleState.COMPLETED,
                OperatingCycleState.COMPLETED_WITH_WARNINGS,
            }:
                latest_successful = cycle_record.cycle_id
        (
            options_plan_id,
            options_due_status,
            options_session_id,
            options_window_open_at,
            options_window_close_at,
            options_reasons,
            options_next_window_open_at,
        ) = self._options_capture_state(as_of=resolved_evaluated_at)
        (
            portfolio_snapshot_id,
            portfolio_snapshot_hash,
            portfolio_snapshot_as_of,
            mandate_lineage_has_any_version,
            active_mandate_id,
            active_mandate_lineage_id,
            active_mandate_version,
            active_mandate_hash,
        ) = self._portfolio_and_mandate_state(evaluated_at=resolved_evaluated_at)
        return RetainedOperatingState.create(
            captured_at=as_of,
            crypto_universe_id=None if universe is None else universe.snapshot_id,
            crypto_universe_hash=None if universe is None else universe.canonical_hash,
            crypto_universe_created_at=None if universe is None else universe.created_at,
            crypto_universe_refresh_supported=CRYPTO_UNIVERSE_REFRESH_GENUINELY_SUPPORTED,
            crypto_pulse_id=None if pulse is None else pulse.pulse_id,
            crypto_pulse_hash=None if pulse is None else pulse.canonical_hash,
            crypto_pulse_as_of=None if pulse is None else pulse.as_of,
            total_market_snapshot_id=None if context is None else context.snapshot_id,
            total_market_hash=None if context is None else context.canonical_hash,
            total_market_source_batch_id=None if context is None else context.source_batch_id,
            securities_batch_id=None if batch_row is None else str(batch_row["batch_id"]),
            securities_batch_hash=None if batch_row is None else str(batch_row["canonical_hash"]),
            securities_last_period_end=None if batch_row is None else batch_row["last_period_end"],
            kalshi_evaluation_ids=kalshi_ids,
            kalshi_evidence_hash=content_digest(kalshi_ids) if kalshi_ids else None,
            kalshi_latest_at=kalshi_latest,
            kalshi_refresh_supported=bool(
                kalshi_capability is not None and kalshi_capability.public_refresh_available
            ),
            options_capture_plan_id=options_plan_id,
            options_capture_due_status=options_due_status,
            options_capture_session_id=options_session_id,
            options_capture_window_open_at=options_window_open_at,
            options_capture_window_close_at=options_window_close_at,
            options_capture_reasons=options_reasons,
            options_capture_next_window_open_at=options_next_window_open_at,
            portfolio_snapshot_id=portfolio_snapshot_id,
            portfolio_snapshot_hash=portfolio_snapshot_hash,
            portfolio_snapshot_as_of=portfolio_snapshot_as_of,
            mandate_lineage_has_any_version=mandate_lineage_has_any_version,
            active_mandate_id=active_mandate_id,
            active_mandate_lineage_id=active_mandate_lineage_id,
            active_mandate_version=active_mandate_version,
            active_mandate_hash=active_mandate_hash,
            shared_set_id=None if shared is None else shared.opportunity_set_id,
            shared_set_hash=None if shared is None else shared.canonical_hash,
            shared_crypto_pulse_id=shared_pulse_ids[0] if len(set(shared_pulse_ids)) == 1 else None,
            shared_kalshi_evaluation_ids=shared_kalshi_ids,
            enriched_set_id=None if enriched is None else enriched.enriched_set_id,
            enriched_set_hash=None if enriched is None else enriched.canonical_hash,
            enriched_shared_set_id=None
            if enriched is None
            else enriched.original_opportunity_set_id,
            enriched_context_snapshot_id=None if enriched is None else enriched.context_snapshot_id,
            allocation_id=None if allocation is None else allocation.decision_id,
            allocation_hash=None if allocation is None else allocation.canonical_hash,
            paper_evidence_ids=tuple(item.source_id for item in paper_sources),
            paper_evidence_hash=(
                content_digest(tuple((item.source_id, item.source_hash) for item in paper_sources))
                if paper_sources
                else None
            ),
            unattributed_paper_evidence_ids=unattributed,
            research_agenda_ids=tuple(value.agenda_item_id for value in research_agenda),
            research_proposal_awaiting_review_ids=tuple(
                value.proposal_id
                for value in research_proposals
                if proposal_state(self._research_operations, value.proposal_id)
                is ResearchProposalState.READY_FOR_REVIEW
            ),
            research_approved_proposal_ids=tuple(
                value.proposal_id
                for value in research_proposals
                if proposal_state(self._research_operations, value.proposal_id)
                is ResearchProposalState.APPROVED_FOR_EXPERIMENT
            ),
            latest_brief_as_of=briefs[-1].as_of if briefs else None,
            latest_successful_cycle_id=latest_successful,
            incomplete_cycle_count=incomplete,
            repeated_failure_count=repeated,
            schema_revision=None if revision is None else str(revision),
        )

    def brief_inputs(
        self,
    ) -> tuple[str | None, tuple[tuple[str, int], ...], str | None, tuple[str, ...]]:
        contexts = self._context.context_snapshots()
        shared_sets = self._shared.opportunity_sets()
        context = contexts[-1] if contexts else None
        shared = shared_sets[-1] if shared_sets else None
        decisions = self._shared.eligibility_decisions(shared.opportunity_set_id) if shared else ()
        enriched_sets = self._context.enriched_sets()
        allocation = (
            self._cross_venue.decision_for_enriched_set(enriched_sets[-1].enriched_set_id)
            if enriched_sets
            else None
        )
        counts = (
            ("ELIGIBLE", sum(item.state is EligibilityState.ELIGIBLE for item in decisions)),
            ("EXPIRED", sum(item.state is EligibilityState.EXPIRED for item in decisions)),
            (
                "BLOCKED",
                sum(item.state is EligibilityState.BLOCKED_MISSING_EVIDENCE for item in decisions),
            ),
            (
                "REJECTED",
                sum(item.state is EligibilityState.REJECTED for item in decisions),
            ),
        )
        summaries = self._crypto.market_change_summaries()
        changes = (
            tuple(
                f"{asset}:{delta}"
                for asset, delta in sorted(
                    (
                        *summaries[-1].largest_score_increases,
                        *summaries[-1].largest_score_decreases,
                    ),
                    key=lambda item: (abs(item[1]), item[0]),
                    reverse=True,
                )[:5]
            )
            if summaries
            else ()
        )
        return (
            None if context is None else context.overall_state.value,
            counts,
            None if allocation is None else str(allocation.retained_cash),
            changes,
        )


def _authority_payload() -> dict[str, object]:
    return {
        "network_behavior": "DUE_TASKS_ONLY_EXACT_BOUNDED_PUBLIC_GET",
        "task_effects": "RESEARCH_EVIDENCE_ONLY",
        "paper_eligible": False,
        "live_eligible": False,
        "capital_authority": "NONE",
        "reservation_authority": "NONE",
        "order_authority": "NONE",
        "broker_authority": "NONE",
        "financial_mutation_capability": "NONE",
    }
