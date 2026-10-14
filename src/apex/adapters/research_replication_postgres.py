"""PostgreSQL repository for Calibration-Qualified Interaction Replication
v1 (migration ``0084``).

Mirrors the ``payload``-column pattern ``adapters.research_calibration_v2_
postgres`` already established: one narrow table per genuinely new fact
type, a thin indexed-column subset for querying, and the full canonical
JSON payload for exact rehydration.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.research_replication import (
    ReplicationConclusion,
    ReplicationConfoundAssessment,
    ReplicationEffectProfile,
    ReplicationPartitionResult,
    ReplicationSpecification,
    TimingAlignmentResult,
)
from apex.domain.values import canonical_json_bytes

RESEARCH_REPLICATION_IMMUTABLE_TABLES = (
    "research_replication_specification",
    "research_replication_timing",
    "research_replication_partition",
    "research_replication_effect_profile",
    "research_replication_confound_assessment",
    "research_replication_conclusion",
)
RESEARCH_REPLICATION_TABLES = RESEARCH_REPLICATION_IMMUTABLE_TABLES

metadata = sa.MetaData()

research_replication_specification = sa.Table(
    "research_replication_specification",
    metadata,
    sa.Column("replication_id", sa.String(160), primary_key=True),
    sa.Column("follow_up_id", sa.String(160), nullable=False),
    sa.Column("parent_hypothesis_id", sa.String(160), nullable=False),
    sa.Column("parent_conclusion_id", sa.String(160), nullable=False),
    sa.Column("parent_agenda_id", sa.String(160), nullable=False),
    sa.Column("v1_calibration_id", sa.String(160), nullable=False),
    sa.Column("v1_priority_assessment_id", sa.String(160), nullable=False),
    sa.Column("v2_calibration_id", sa.String(160), nullable=False),
    sa.Column("v2_classification_id", sa.String(160), nullable=False),
    sa.Column("research_design", sa.String(40), nullable=False),
    sa.Column("conditioning_shape", sa.String(40), nullable=False),
    sa.Column("horizon_sessions", sa.Integer(), nullable=False),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column("replay_run_id", sa.String(160), nullable=False),
    sa.Column("cross_market_dataset_id", sa.String(160), nullable=False),
    sa.Column("is_in_sample_replication", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_replication_timing = sa.Table(
    "research_replication_timing",
    metadata,
    sa.Column("alignment_id", sa.String(160), primary_key=True),
    sa.Column("replication_id", sa.String(160), nullable=False),
    sa.Column("convention", sa.String(40), nullable=False),
    sa.Column("ordering_valid", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_replication_partition = sa.Table(
    "research_replication_partition",
    metadata,
    sa.Column("partition_id", sa.String(160), primary_key=True),
    sa.Column("replication_id", sa.String(160), nullable=False),
    sa.Column("partition_kind", sa.String(20), nullable=False),
    sa.Column("label", sa.String(40), nullable=False),
    sa.Column("sample_count", sa.Integer(), nullable=False),
    sa.Column("matched_percentile", sa.Numeric(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_replication_effect_profile = sa.Table(
    "research_replication_effect_profile",
    metadata,
    sa.Column("profile_id", sa.String(160), primary_key=True),
    sa.Column("replication_id", sa.String(160), nullable=False),
    sa.Column("meets_practical_floor", sa.Boolean(), nullable=False),
    sa.Column("outlier_robust", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_replication_confound_assessment = sa.Table(
    "research_replication_confound_assessment",
    metadata,
    sa.Column("assessment_id", sa.String(160), primary_key=True),
    sa.Column("replication_id", sa.String(160), nullable=False),
    sa.Column("overall_confound_resolved", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_replication_conclusion = sa.Table(
    "research_replication_conclusion",
    metadata,
    sa.Column("conclusion_id", sa.String(160), primary_key=True),
    sa.Column("replication_id", sa.String(160), nullable=False),
    sa.Column("follow_up_id", sa.String(160), nullable=False),
    sa.Column("conclusion_state", sa.String(40), nullable=False),
    sa.Column("evidence_grade", sa.String(20), nullable=False),
    sa.Column("evidence_grade_upgraded", sa.Boolean(), nullable=False),
    sa.Column("transaction_cost_relevance", sa.String(40), nullable=False),
    sa.Column("forward_confirmation_required", sa.Boolean(), nullable=False),
    sa.Column("qualification", sa.String(40), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class ResearchReplicationPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise ResearchReplicationPersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresResearchReplicationRepository:
    """Idempotent insert-if-absent persistence for every genuinely new
    Calibration-Qualified Interaction Replication v1 fact type. Nothing
    here ever updates or deletes a row; a caller that observes an existing
    row simply reuses it (exact replay), and a mismatched re-declaration
    under the same deterministic id is a domain-level content mismatch the
    caller's own hash comparison would already reject before reaching
    here."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- specification (immutable) -------------------------------------

    def specification_by_id(self, replication_id: str) -> ReplicationSpecification | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_replication_specification).where(
                        research_replication_specification.c.replication_id == replication_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ReplicationSpecification, _decoded(row))

    def specification_by_follow_up(self, follow_up_id: str) -> ReplicationSpecification | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_replication_specification).where(
                        research_replication_specification.c.follow_up_id == follow_up_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ReplicationSpecification, _decoded(row))

    def save_specification(self, item: ReplicationSpecification) -> ReplicationSpecification:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_replication_specification.c.replication_id).where(
                    research_replication_specification.c.replication_id == item.replication_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_replication_specification.insert().values(
                    replication_id=item.replication_id,
                    follow_up_id=item.follow_up_id,
                    parent_hypothesis_id=item.parent_hypothesis_id,
                    parent_conclusion_id=item.parent_conclusion_id,
                    parent_agenda_id=item.parent_agenda_id,
                    v1_calibration_id=item.v1_calibration_id,
                    v1_priority_assessment_id=item.v1_priority_assessment_id,
                    v2_calibration_id=item.v2_calibration_id,
                    v2_classification_id=item.v2_classification_id,
                    research_design=item.research_design.value,
                    conditioning_shape=item.conditioning_shape.value,
                    horizon_sessions=item.horizon_sessions,
                    dataset_snapshot_id=item.dataset_snapshot_id,
                    replay_run_id=item.replay_run_id,
                    cross_market_dataset_id=item.cross_market_dataset_id,
                    is_in_sample_replication=item.is_in_sample_replication,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    # --- timing (immutable) ---------------------------------------------

    def save_timing(self, item: TimingAlignmentResult) -> TimingAlignmentResult:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_replication_timing.c.alignment_id).where(
                    research_replication_timing.c.alignment_id == item.alignment_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_replication_timing.insert().values(
                    alignment_id=item.alignment_id,
                    replication_id=item.replication_id,
                    convention=item.convention.value,
                    ordering_valid=item.ordering_valid,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def timings_for_replication(self, replication_id: str) -> tuple[TimingAlignmentResult, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_replication_timing).where(
                    research_replication_timing.c.replication_id == replication_id
                )
            ).mappings()
            return tuple(rehydrate(TimingAlignmentResult, _decoded(row)) for row in rows)

    # --- partitions (immutable) ------------------------------------------

    def save_partition(self, item: ReplicationPartitionResult) -> ReplicationPartitionResult:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_replication_partition.c.partition_id).where(
                    research_replication_partition.c.partition_id == item.partition_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_replication_partition.insert().values(
                    partition_id=item.partition_id,
                    replication_id=item.replication_id,
                    partition_kind=item.partition_kind.value,
                    label=item.label,
                    sample_count=item.sample_count,
                    matched_percentile=item.matched_percentile,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def partitions_for_replication(
        self, replication_id: str
    ) -> tuple[ReplicationPartitionResult, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_replication_partition).where(
                    research_replication_partition.c.replication_id == replication_id
                )
            ).mappings()
            return tuple(rehydrate(ReplicationPartitionResult, _decoded(row)) for row in rows)

    # --- effect profile (immutable) --------------------------------------

    def save_effect_profile(self, item: ReplicationEffectProfile) -> ReplicationEffectProfile:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_replication_effect_profile.c.profile_id).where(
                    research_replication_effect_profile.c.profile_id == item.profile_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_replication_effect_profile.insert().values(
                    profile_id=item.profile_id,
                    replication_id=item.replication_id,
                    meets_practical_floor=item.meets_practical_floor,
                    outlier_robust=item.outlier_robust,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def effect_profile_for_replication(
        self, replication_id: str
    ) -> ReplicationEffectProfile | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_replication_effect_profile).where(
                        research_replication_effect_profile.c.replication_id == replication_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ReplicationEffectProfile, _decoded(row))

    # --- confound assessment (immutable) ----------------------------------

    def save_confound_assessment(
        self, item: ReplicationConfoundAssessment
    ) -> ReplicationConfoundAssessment:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_replication_confound_assessment.c.assessment_id).where(
                    research_replication_confound_assessment.c.assessment_id == item.assessment_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_replication_confound_assessment.insert().values(
                    assessment_id=item.assessment_id,
                    replication_id=item.replication_id,
                    overall_confound_resolved=item.overall_confound_resolved,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def confound_assessment_for_replication(
        self, replication_id: str
    ) -> ReplicationConfoundAssessment | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_replication_confound_assessment).where(
                        research_replication_confound_assessment.c.replication_id == replication_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ReplicationConfoundAssessment, _decoded(row))

    # --- conclusion (immutable) -------------------------------------------

    def save_conclusion(self, item: ReplicationConclusion) -> ReplicationConclusion:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_replication_conclusion.c.conclusion_id).where(
                    research_replication_conclusion.c.conclusion_id == item.conclusion_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_replication_conclusion.insert().values(
                    conclusion_id=item.conclusion_id,
                    replication_id=item.replication_id,
                    follow_up_id=item.follow_up_id,
                    conclusion_state=item.conclusion_state.value,
                    evidence_grade=item.evidence_grade.value,
                    evidence_grade_upgraded=item.evidence_grade_upgraded,
                    transaction_cost_relevance=item.transaction_cost_relevance.value,
                    forward_confirmation_required=item.forward_confirmation_required,
                    qualification=item.qualification.value,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def conclusion_for_replication(self, replication_id: str) -> ReplicationConclusion | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_replication_conclusion).where(
                        research_replication_conclusion.c.replication_id == replication_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(ReplicationConclusion, _decoded(row))
