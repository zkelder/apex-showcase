"""Immutable PostgreSQL evidence for Securities Structure Forward Performance v1.

Persists one ``SecuritiesStructureOutcomeAggregate`` row per genuinely
computed ``(cohort_id, run_id)`` -- append-only, never mutated, never
deleted. No provider client, credential, or network capability lives here,
and no row this module writes ever carries an ``authority`` other than
``'NONE'``. Mirrors ``adapters.counterfactual_outcome_postgres`` exactly in
style; a distinct table because the two aggregate shapes are structurally
different (venue/reason_family/classifier_version vs. structure_type --
see ``domain.securities_structure_outcome``'s own module docstring for why
these are separate, complementary mechanisms rather than one).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.domain.counterfactual_followup import FollowUpBaselineKind, FollowUpHorizonKind
from apex.domain.securities_structure_outcome import (
    SECURITIES_STRUCTURE_OUTCOME_IDENTITY_CONFLICT,
    SecuritiesStructureOutcomeAggregate,
    SecuritiesStructureOutcomeError,
    SecuritiesStructureOutcomeQualificationState,
)
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()

aggregate_table = sa.Table(
    "securities_structure_outcome_aggregate",
    metadata,
    sa.Column("aggregate_id", sa.String(300), primary_key=True),
    sa.Column("cohort_id", sa.String(300), nullable=False),
    sa.Column("outcome_policy_version", sa.String(60), nullable=False),
    sa.Column("run_id", sa.String(300), nullable=False),
    sa.Column("strategy_or_method_id", sa.String(300), nullable=False),
    sa.Column("structure_type", sa.String(40), nullable=False),
    sa.Column("horizon_kind", sa.String(20), nullable=False),
    sa.Column("baseline_kind", sa.String(20), nullable=False),
    sa.Column("followup_policy_version", sa.String(40), nullable=False),
    sa.Column("evidence_quality_policy_version", sa.String(60), nullable=False),
    sa.Column("qualification_state", sa.String(40), nullable=False),
    sa.Column("total_decisions_considered", sa.Integer(), nullable=False),
    sa.Column("mature_observation_count", sa.Integer(), nullable=False),
    sa.Column("eligible_observation_count", sa.Integer(), nullable=False),
    sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

SECURITIES_STRUCTURE_OUTCOME_TABLES = ("securities_structure_outcome_aggregate",)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise SecuritiesStructureOutcomeError(
            "SECURITIES_STRUCTURE_OUTCOME_RETAINED_PAYLOAD_INVALID"
        )
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _decimal_or_none(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SecuritiesStructureOutcomeError(
            "SECURITIES_STRUCTURE_OUTCOME_RETAINED_COLLECTION_INVALID"
        )
    return tuple(str(item) for item in value)


def _aggregate(row: RowMapping) -> SecuritiesStructureOutcomeAggregate:
    value = _data(row)
    return SecuritiesStructureOutcomeAggregate(
        aggregate_id=str(value["aggregate_id"]),
        cohort_id=str(value["cohort_id"]),
        outcome_policy_version=str(value["outcome_policy_version"]),
        run_id=str(value["run_id"]),
        strategy_or_method_id=str(value["strategy_or_method_id"]),
        structure_type=str(value["structure_type"]),
        horizon_kind=FollowUpHorizonKind(value["horizon_kind"]),
        baseline_kind=FollowUpBaselineKind(value["baseline_kind"]),
        followup_policy_version=str(value["followup_policy_version"]),
        evidence_quality_policy_version=str(value["evidence_quality_policy_version"]),
        generated_at=_time(value["generated_at"]),
        total_decisions_considered=int(value["total_decisions_considered"]),
        mature_observation_count=int(value["mature_observation_count"]),
        eligible_observation_count=int(value["eligible_observation_count"]),
        evidence_ineligible_observation_count=int(value["evidence_ineligible_observation_count"]),
        unassessed_observation_count=int(value["unassessed_observation_count"]),
        unavailable_observation_count=int(value["unavailable_observation_count"]),
        positive_outcome_count=int(value["positive_outcome_count"]),
        negative_outcome_count=int(value["negative_outcome_count"]),
        zero_outcome_count=int(value["zero_outcome_count"]),
        mean_subsequent_return=_decimal_or_none(value["mean_subsequent_return"]),
        median_subsequent_return=_decimal_or_none(value["median_subsequent_return"]),
        positive_rate=_decimal_or_none(value["positive_rate"]),
        qualification_state=SecuritiesStructureOutcomeQualificationState(
            value["qualification_state"]
        ),
        contributing_decision_ids=_strings(value["contributing_decision_ids"]),
        explanation=str(value["explanation"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


class PostgresSecuritiesStructureOutcomeRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def aggregate(self, aggregate_id: str) -> SecuritiesStructureOutcomeAggregate | None:
        row = self._one(
            sa.select(aggregate_table).where(aggregate_table.c.aggregate_id == aggregate_id)
        )
        return None if row is None else _aggregate(row)

    def save_aggregate(self, *, aggregate: SecuritiesStructureOutcomeAggregate) -> bool:
        """Persist one aggregate. Idempotent: replaying the identical
        ``aggregate_id``/``canonical_hash`` returns ``True`` and writes
        nothing; a conflicting payload under the same ``aggregate_id``
        raises rather than silently overwriting retained truth -- mirrors
        ``PostgresCounterfactualOutcomeRepository.save_aggregate`` exactly."""

        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(aggregate_table.c.canonical_hash).where(
                        aggregate_table.c.aggregate_id == aggregate.aggregate_id
                    )
                )
                if existing is not None:
                    if existing != aggregate.canonical_hash:
                        raise SecuritiesStructureOutcomeError(
                            SECURITIES_STRUCTURE_OUTCOME_IDENTITY_CONFLICT
                        )
                    return True
                connection.execute(
                    aggregate_table.insert().values(
                        aggregate_id=aggregate.aggregate_id,
                        cohort_id=aggregate.cohort_id,
                        outcome_policy_version=aggregate.outcome_policy_version,
                        run_id=aggregate.run_id,
                        strategy_or_method_id=aggregate.strategy_or_method_id,
                        structure_type=aggregate.structure_type,
                        horizon_kind=aggregate.horizon_kind.value,
                        baseline_kind=aggregate.baseline_kind.value,
                        followup_policy_version=aggregate.followup_policy_version,
                        evidence_quality_policy_version=aggregate.evidence_quality_policy_version,
                        qualification_state=aggregate.qualification_state.value,
                        total_decisions_considered=aggregate.total_decisions_considered,
                        mature_observation_count=aggregate.mature_observation_count,
                        eligible_observation_count=aggregate.eligible_observation_count,
                        generated_at=aggregate.generated_at,
                        payload=_payload(aggregate),
                        canonical_hash=aggregate.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise SecuritiesStructureOutcomeError(
                "SECURITIES_STRUCTURE_OUTCOME_PERSISTENCE_CONFLICT"
            ) from error

    def aggregates_for_cohort(
        self, cohort_id: str
    ) -> tuple[SecuritiesStructureOutcomeAggregate, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(aggregate_table)
                .where(aggregate_table.c.cohort_id == cohort_id)
                .order_by(aggregate_table.c.generated_at, aggregate_table.c.aggregate_id)
            ).mappings()
            return tuple(_aggregate(row) for row in rows)

    def aggregates_for_run(self, run_id: str) -> tuple[SecuritiesStructureOutcomeAggregate, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(aggregate_table)
                .where(aggregate_table.c.run_id == run_id)
                .order_by(aggregate_table.c.aggregate_id)
            ).mappings()
            return tuple(_aggregate(row) for row in rows)

    def earliest_qualified_per_cohort(self) -> tuple[SecuritiesStructureOutcomeAggregate, ...]:
        """For every distinct ``cohort_id`` that has ever reached
        ``QUALIFIED``, the single earliest such row (by ``generated_at``,
        then ``aggregate_id``) -- mirrors ``PostgresCounterfactualOutcome
        Repository.earliest_qualified_per_cohort`` exactly, including the
        reason this exists: the stable evidence a Research Operations fact
        anchors to, so the same cohort continuing to qualify on later
        cycles never re-notifies a human every cycle."""

        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(aggregate_table)
                .where(
                    aggregate_table.c.qualification_state
                    == SecuritiesStructureOutcomeQualificationState.QUALIFIED.value
                )
                .order_by(
                    aggregate_table.c.cohort_id,
                    aggregate_table.c.generated_at,
                    aggregate_table.c.aggregate_id,
                )
            ).mappings()
            earliest_by_cohort: dict[str, RowMapping] = {}
            for row in rows:
                cohort_id = str(row["cohort_id"])
                if cohort_id not in earliest_by_cohort:
                    earliest_by_cohort[cohort_id] = row
            return tuple(_aggregate(row) for row in earliest_by_cohort.values())

    def census(self) -> dict[str, object]:
        """Bounded operator diagnostic: retained aggregate counts by
        qualification state and by structure type -- never a substitute for
        reading the aggregates themselves. Mirrors ``PostgresCounterfactual
        OutcomeRepository.census`` exactly."""

        with self._engine.connect() as connection:
            total_count = connection.scalar(sa.select(sa.func.count()).select_from(aggregate_table))
            total = int(total_count or 0)
            by_state: dict[str, int] = {
                str(state): int(count)
                for state, count in connection.execute(
                    sa.select(aggregate_table.c.qualification_state, sa.func.count()).group_by(
                        aggregate_table.c.qualification_state
                    )
                )
            }
            by_structure_type: dict[str, int] = {
                str(structure_type): int(count)
                for structure_type, count in connection.execute(
                    sa.select(aggregate_table.c.structure_type, sa.func.count()).group_by(
                        aggregate_table.c.structure_type
                    )
                )
            }
        return {
            "total_aggregates": total,
            "by_qualification_state": by_state,
            "by_structure_type": by_structure_type,
        }
