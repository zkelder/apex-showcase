"""Immutable PostgreSQL evidence for Counterfactual Outcome Feedback v1.

Persists one ``CounterfactualOutcomeAggregate`` row per genuinely computed
``(cohort_id, run_id)`` -- append-only, never mutated, never deleted. No
provider client, credential, or network capability lives here, and no row
this module writes ever carries an ``authority`` other than ``'NONE'``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.domain.counterfactual_classification import ReasonFamily
from apex.domain.counterfactual_followup import FollowUpBaselineKind, FollowUpHorizonKind
from apex.domain.counterfactual_outcome import (
    COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT,
    CounterfactualOutcomeAggregate,
    CounterfactualOutcomeError,
    CounterfactualOutcomeQualificationState,
    CounterfactualOutcomeSpecificity,
)
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()

aggregate_table = sa.Table(
    "counterfactual_outcome_aggregate",
    metadata,
    sa.Column("aggregate_id", sa.String(300), primary_key=True),
    sa.Column("cohort_id", sa.String(300), nullable=False),
    sa.Column("outcome_policy_version", sa.String(60), nullable=False),
    sa.Column("run_id", sa.String(300), nullable=False),
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("strategy_or_method_id", sa.String(300), nullable=False),
    sa.Column("reason_family", sa.String(40), nullable=False),
    sa.Column("horizon_kind", sa.String(20), nullable=False),
    sa.Column("baseline_kind", sa.String(20), nullable=False),
    sa.Column("followup_policy_version", sa.String(40), nullable=False),
    sa.Column("classifier_version", sa.String(40), nullable=False),
    sa.Column("evidence_quality_policy_version", sa.String(60), nullable=False),
    sa.Column("qualification_state", sa.String(40), nullable=False),
    sa.Column("total_observations_considered", sa.Integer(), nullable=False),
    sa.Column("mature_observation_count", sa.Integer(), nullable=False),
    sa.Column("eligible_observation_count", sa.Integer(), nullable=False),
    sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

COUNTERFACTUAL_OUTCOME_TABLES = ("counterfactual_outcome_aggregate",)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _decimal_or_none(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_RETAINED_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _time_or_none(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _specificity(value: object) -> CounterfactualOutcomeSpecificity | None:
    """Counterfactual Outcome Evidence Specificity v3: the nested V3 block,
    read back from the retained ``payload``.  Backward compatible by
    construction -- every V1/V2 row retained before this milestone simply
    lacks the key and reads as ``None``, exactly as ``adapters.
    counterfactual_followup_postgres._observation`` treats the six
    FOLLOWUP_POLICY_V3 facts on a legacy row."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise CounterfactualOutcomeError("COUNTERFACTUAL_OUTCOME_RETAINED_PAYLOAD_INVALID")
    return CounterfactualOutcomeSpecificity(
        attribution_policy_version=str(value["attribution_policy_version"]),
        peer_reference_policy_version=str(value["peer_reference_policy_version"]),
        independent_window_policy_version=str(value["independent_window_policy_version"]),
        policy_hash=str(value["policy_hash"]),
        multi_family_decision_count=int(value["multi_family_decision_count"]),
        method_unattributed_decision_count=int(value["method_unattributed_decision_count"]),
        raw_return_observation_count=int(value["raw_return_observation_count"]),
        anchor_mismatch_observation_count=int(value["anchor_mismatch_observation_count"]),
        raw_window_count=int(value["raw_window_count"]),
        inconsistent_window_count=int(value["inconsistent_window_count"]),
        mixed_origin_window_count=int(value["mixed_origin_window_count"]),
        membership_conflict_product_count=int(value["membership_conflict_product_count"]),
        degenerate_window_count=int(value["degenerate_window_count"]),
        products_with_decision_but_no_return_count=int(
            value["products_with_decision_but_no_return_count"]
        ),
        awaiting_peer_completion_window_count=int(value["awaiting_peer_completion_window_count"]),
        off_grid_window_count=int(value["off_grid_window_count"]),
        window_horizon_days=(
            None if value["window_horizon_days"] is None else int(value["window_horizon_days"])
        ),
        horizon_days_heterogeneous=bool(value["horizon_days_heterogeneous"]),
        independent_window_count=int(value["independent_window_count"]),
        independent_window_keys=_strings(value["independent_window_keys"]),
        first_window_start=_time_or_none(value["first_window_start"]),
        last_window_end=_time_or_none(value["last_window_end"]),
        descriptive_mean_raw_return=_decimal_or_none(value["descriptive_mean_raw_return"]),
        descriptive_mean_cohort_window_return=_decimal_or_none(
            value["descriptive_mean_cohort_window_return"]
        ),
        descriptive_mean_complement_return=_decimal_or_none(
            value["descriptive_mean_complement_return"]
        ),
        descriptive_mean_window_spread=_decimal_or_none(value["descriptive_mean_window_spread"]),
        descriptive_informative_window_count=int(value["descriptive_informative_window_count"]),
        relative_positive_window_count=int(value["relative_positive_window_count"]),
        relative_negative_window_count=int(value["relative_negative_window_count"]),
        relative_zero_window_count=int(value["relative_zero_window_count"]),
        mean_relative_window_return=_decimal_or_none(value["mean_relative_window_return"]),
        median_relative_window_return=_decimal_or_none(value["median_relative_window_return"]),
        relative_positive_window_rate=_decimal_or_none(value["relative_positive_window_rate"]),
        relative_window_return_standard_deviation=_decimal_or_none(
            value["relative_window_return_standard_deviation"]
        ),
    )


def _aggregate(row: RowMapping) -> CounterfactualOutcomeAggregate:
    value = _data(row)
    return CounterfactualOutcomeAggregate(
        aggregate_id=str(value["aggregate_id"]),
        cohort_id=str(value["cohort_id"]),
        outcome_policy_version=str(value["outcome_policy_version"]),
        run_id=str(value["run_id"]),
        venue=SharedVenue(value["venue"]),
        strategy_or_method_id=str(value["strategy_or_method_id"]),
        reason_family=ReasonFamily(value["reason_family"]),
        horizon_kind=FollowUpHorizonKind(value["horizon_kind"]),
        baseline_kind=FollowUpBaselineKind(value["baseline_kind"]),
        followup_policy_version=str(value["followup_policy_version"]),
        classifier_version=str(value["classifier_version"]),
        evidence_quality_policy_version=str(value["evidence_quality_policy_version"]),
        generated_at=_time(value["generated_at"]),
        total_observations_considered=int(value["total_observations_considered"]),
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
        qualification_state=CounterfactualOutcomeQualificationState(value["qualification_state"]),
        contributing_decision_ids=_strings(value["contributing_decision_ids"]),
        explanation=str(value["explanation"]),
        canonical_hash=str(value["canonical_hash"]),
        specificity=_specificity(value.get("specificity")),
        authority="NONE",
    )


class PostgresCounterfactualOutcomeRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def aggregate(self, aggregate_id: str) -> CounterfactualOutcomeAggregate | None:
        row = self._one(
            sa.select(aggregate_table).where(aggregate_table.c.aggregate_id == aggregate_id)
        )
        return None if row is None else _aggregate(row)

    def save_aggregate(self, *, aggregate: CounterfactualOutcomeAggregate) -> bool:
        """Persist one aggregate. Idempotent: replaying the identical
        ``aggregate_id``/``canonical_hash`` returns ``True`` and writes
        nothing; a conflicting payload under the same ``aggregate_id``
        raises rather than silently overwriting retained truth -- mirrors
        ``PostgresCounterfactualLedgerRepository.save_decision`` exactly."""

        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(aggregate_table.c.canonical_hash).where(
                        aggregate_table.c.aggregate_id == aggregate.aggregate_id
                    )
                )
                if existing is not None:
                    if existing != aggregate.canonical_hash:
                        raise CounterfactualOutcomeError(COUNTERFACTUAL_OUTCOME_IDENTITY_CONFLICT)
                    return True
                connection.execute(
                    aggregate_table.insert().values(
                        aggregate_id=aggregate.aggregate_id,
                        cohort_id=aggregate.cohort_id,
                        outcome_policy_version=aggregate.outcome_policy_version,
                        run_id=aggregate.run_id,
                        venue=aggregate.venue.value,
                        strategy_or_method_id=aggregate.strategy_or_method_id,
                        reason_family=aggregate.reason_family.value,
                        horizon_kind=aggregate.horizon_kind.value,
                        baseline_kind=aggregate.baseline_kind.value,
                        followup_policy_version=aggregate.followup_policy_version,
                        classifier_version=aggregate.classifier_version,
                        evidence_quality_policy_version=aggregate.evidence_quality_policy_version,
                        qualification_state=aggregate.qualification_state.value,
                        total_observations_considered=aggregate.total_observations_considered,
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
            raise CounterfactualOutcomeError(
                "COUNTERFACTUAL_OUTCOME_PERSISTENCE_CONFLICT"
            ) from error

    def aggregates_for_cohort(self, cohort_id: str) -> tuple[CounterfactualOutcomeAggregate, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(aggregate_table)
                .where(aggregate_table.c.cohort_id == cohort_id)
                .order_by(aggregate_table.c.generated_at, aggregate_table.c.aggregate_id)
            ).mappings()
            return tuple(_aggregate(row) for row in rows)

    def aggregates_for_run(self, run_id: str) -> tuple[CounterfactualOutcomeAggregate, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(aggregate_table)
                .where(aggregate_table.c.run_id == run_id)
                .order_by(aggregate_table.c.aggregate_id)
            ).mappings()
            return tuple(_aggregate(row) for row in rows)

    def earliest_qualified_per_cohort(self) -> tuple[CounterfactualOutcomeAggregate, ...]:
        """For every distinct ``cohort_id`` that has ever reached
        ``QUALIFIED``, the single earliest such row (by ``generated_at``,
        then ``aggregate_id``) -- the stable evidence Research Operations
        fact derivation anchors to, so the same cohort continuing to
        qualify on later cycles never re-notifies a human every cycle (see
        ``domain.counterfactual_outcome``'s own module docstring and
        ``adapters.research_operations_sources``'s counterfactual-outcome
        fact source)."""

        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(aggregate_table)
                .where(
                    aggregate_table.c.qualification_state
                    == CounterfactualOutcomeQualificationState.QUALIFIED.value
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
        qualification state and by venue -- never a substitute for reading
        a specific aggregate's own explanation/provenance."""

        with self._engine.connect() as connection:
            by_state: dict[str, int] = {
                str(state): int(count)
                for state, count in connection.execute(
                    sa.select(aggregate_table.c.qualification_state, sa.func.count()).group_by(
                        aggregate_table.c.qualification_state
                    )
                ).all()
            }
            by_venue: dict[str, int] = {
                str(venue): int(count)
                for venue, count in connection.execute(
                    sa.select(aggregate_table.c.venue, sa.func.count()).group_by(
                        aggregate_table.c.venue
                    )
                ).all()
            }
            total = connection.scalar(sa.select(sa.func.count()).select_from(aggregate_table))
            distinct_cohorts = connection.scalar(
                sa.select(sa.func.count(sa.distinct(aggregate_table.c.cohort_id)))
            )
        return {
            "total_aggregate_rows": int(total or 0),
            "distinct_cohort_count": int(distinct_cohorts or 0),
            "by_qualification_state": {str(k): int(v) for k, v in by_state.items()},
            "by_venue": {str(k): int(v) for k, v in by_venue.items()},
        }
