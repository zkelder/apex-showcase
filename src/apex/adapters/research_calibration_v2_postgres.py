"""PostgreSQL repository for Research Calibration v2: Template-Matched
Nulls and Stability (migration ``0083``).

Mirrors the ``payload``-column pattern ``adapters.research_calibration_
postgres`` already established. The plan/run/report/breakdown/method-
comparison/prior-recalibration/calibrated-promotion tables from ``0082``
are reused unmodified for v2 evidence (a v2 ``CalibrationPlan`` is simply
another row with a distinct ``calibration_id``); only the six genuinely
new v2 fact types declared in ``domain.research_calibration_v2`` get new
tables here.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.research_calibration import NullControlOutcome
from apex.domain.research_calibration_v2 import (
    DesignClassification,
    FollowUpQualification,
    NullControlSpecV2,
    StabilitySlice,
    WalkForwardNullSummary,
)
from apex.domain.values import canonical_json_bytes

RESEARCH_CALIBRATION_V2_IMMUTABLE_TABLES = (
    "research_calibration_v2_null_control",
    "research_calibration_v2_null_outcome",
    "research_calibration_v2_design_classification",
    "research_calibration_v2_stability_slice",
    "research_calibration_v2_walk_forward_null_summary",
    "research_calibration_v2_follow_up_qualification",
)
RESEARCH_CALIBRATION_V2_TABLES = RESEARCH_CALIBRATION_V2_IMMUTABLE_TABLES

metadata = sa.MetaData()

research_calibration_v2_null_control = sa.Table(
    "research_calibration_v2_null_control",
    metadata,
    sa.Column("spec_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("research_design", sa.String(40), nullable=False),
    sa.Column("conditioning_shape", sa.String(40), nullable=False),
    sa.Column("matched_stratum", sa.String(200), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_calibration_v2_null_outcome = sa.Table(
    "research_calibration_v2_null_outcome",
    metadata,
    sa.Column("outcome_id", sa.String(160), primary_key=True),
    sa.Column("spec_id", sa.String(160), nullable=False),
    sa.Column("calibration_run_id", sa.String(160), nullable=False),
    sa.Column("conclusion_id", sa.String(160), nullable=True),
    sa.Column("knowledge_status", sa.String(40), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_calibration_v2_design_classification = sa.Table(
    "research_calibration_v2_design_classification",
    metadata,
    sa.Column("classification_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("subject_id", sa.String(160), nullable=False),
    sa.Column("subject_kind", sa.String(20), nullable=False),
    sa.Column("research_design", sa.String(40), nullable=False),
    sa.Column("matched_null_count", sa.Integer(), nullable=False),
    sa.Column("calibration_sufficient", sa.Boolean(), nullable=False),
    sa.Column("evidence_grade_v2", sa.String(20), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_calibration_v2_stability_slice = sa.Table(
    "research_calibration_v2_stability_slice",
    metadata,
    sa.Column("slice_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("dimension_kind", sa.String(40), nullable=False),
    sa.Column("dimension_label", sa.String(80), nullable=False),
    sa.Column("control_count", sa.Integer(), nullable=False),
    sa.Column("sample_adequate", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_calibration_v2_walk_forward_null_summary = sa.Table(
    "research_calibration_v2_walk_forward_null_summary",
    metadata,
    sa.Column("summary_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("spec_id", sa.String(160), nullable=False),
    sa.Column("fold_count", sa.Integer(), nullable=False),
    sa.Column("folds_beating_benchmark", sa.Integer(), nullable=False),
    sa.Column("final_evidence_grade", sa.String(20), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_calibration_v2_follow_up_qualification = sa.Table(
    "research_calibration_v2_follow_up_qualification",
    metadata,
    sa.Column("assessment_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("follow_up_id", sa.String(160), nullable=False),
    sa.Column("parent_hypothesis_id", sa.String(160), nullable=False),
    sa.Column("qualification", sa.String(40), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class ResearchCalibrationV2PersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise ResearchCalibrationV2PersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresCalibrationV2Repository:
    """Covers only the six new v2-only tables. Callers reuse the existing
    ``PostgresCalibrationRepository`` (unmodified) for plan/run/report/
    breakdown/method-comparison/prior-recalibration/calibrated-promotion
    reads and writes -- both repositories operate on the same underlying
    engine/schema and are meant to be used side by side."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- v2 null control specs (immutable) ----------------------------

    def spec_by_id(self, spec_id: str) -> NullControlSpecV2 | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_calibration_v2_null_control).where(
                        research_calibration_v2_null_control.c.spec_id == spec_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(NullControlSpecV2, _decoded(row))

    def save_spec(self, spec: NullControlSpecV2) -> NullControlSpecV2:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_v2_null_control.c.spec_id).where(
                    research_calibration_v2_null_control.c.spec_id == spec.spec_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return spec
            connection.execute(
                research_calibration_v2_null_control.insert().values(
                    spec_id=spec.spec_id,
                    calibration_id=spec.calibration_id,
                    research_design=spec.research_design.value,
                    conditioning_shape=spec.conditioning_shape.value,
                    matched_stratum=spec.matched_stratum,
                    ordinal=spec.ordinal,
                    created_at=spec.created_at,
                    payload=_payload(spec),
                    authority="NONE",
                )
            )
        return spec

    def specs_for_calibration(self, calibration_id: str) -> tuple[NullControlSpecV2, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_v2_null_control)
                .where(research_calibration_v2_null_control.c.calibration_id == calibration_id)
                .order_by(research_calibration_v2_null_control.c.ordinal)
            ).mappings()
            return tuple(rehydrate(NullControlSpecV2, _decoded(row)) for row in rows)

    def specs_for_stratum(self, matched_stratum: str) -> tuple[NullControlSpecV2, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_v2_null_control).where(
                    research_calibration_v2_null_control.c.matched_stratum == matched_stratum
                )
            ).mappings()
            return tuple(rehydrate(NullControlSpecV2, _decoded(row)) for row in rows)

    # --- v2 null control outcomes (immutable) -------------------------

    def outcome_by_spec(self, spec_id: str) -> NullControlOutcome | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_calibration_v2_null_outcome)
                    .where(research_calibration_v2_null_outcome.c.spec_id == spec_id)
                    .order_by(research_calibration_v2_null_outcome.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(NullControlOutcome, _decoded(row))

    def save_outcome(self, outcome: NullControlOutcome) -> NullControlOutcome:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_v2_null_outcome.c.outcome_id).where(
                    research_calibration_v2_null_outcome.c.outcome_id == outcome.outcome_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return outcome
            connection.execute(
                research_calibration_v2_null_outcome.insert().values(
                    outcome_id=outcome.outcome_id,
                    spec_id=outcome.spec_id,
                    calibration_run_id=outcome.calibration_run_id,
                    conclusion_id=outcome.conclusion_id,
                    knowledge_status=outcome.knowledge_status.value,
                    created_at=outcome.created_at,
                    payload=_payload(outcome),
                    authority="NONE",
                )
            )
        return outcome

    def outcomes_for_run(self, calibration_run_id: str) -> tuple[NullControlOutcome, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_v2_null_outcome).where(
                    research_calibration_v2_null_outcome.c.calibration_run_id == calibration_run_id
                )
            ).mappings()
            return tuple(rehydrate(NullControlOutcome, _decoded(row)) for row in rows)

    # --- design classifications (immutable) ----------------------------

    def save_classification(self, item: DesignClassification) -> DesignClassification:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_v2_design_classification.c.classification_id).where(
                    research_calibration_v2_design_classification.c.classification_id
                    == item.classification_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_calibration_v2_design_classification.insert().values(
                    classification_id=item.classification_id,
                    calibration_id=item.calibration_id,
                    subject_id=item.subject_id,
                    subject_kind=item.subject_kind,
                    research_design=item.research_design.value,
                    matched_null_count=item.matched.matched_null_count,
                    calibration_sufficient=item.matched.sufficient,
                    evidence_grade_v2=item.evidence_grade_v2.value,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def classifications_for_calibration(
        self, calibration_id: str
    ) -> tuple[DesignClassification, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_v2_design_classification).where(
                    research_calibration_v2_design_classification.c.calibration_id == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(DesignClassification, _decoded(row)) for row in rows)

    def classification_for_subject(
        self, *, calibration_id: str, subject_id: str
    ) -> DesignClassification | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_calibration_v2_design_classification).where(
                        research_calibration_v2_design_classification.c.calibration_id
                        == calibration_id,
                        research_calibration_v2_design_classification.c.subject_id == subject_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(DesignClassification, _decoded(row))

    # --- stability slices (immutable) -----------------------------------

    def save_stability_slice(self, item: StabilitySlice) -> StabilitySlice:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_v2_stability_slice.c.slice_id).where(
                    research_calibration_v2_stability_slice.c.slice_id == item.slice_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_calibration_v2_stability_slice.insert().values(
                    slice_id=item.slice_id,
                    calibration_id=item.calibration_id,
                    dimension_kind=item.dimension_kind,
                    dimension_label=item.dimension_label,
                    control_count=item.control_count,
                    sample_adequate=item.sample_adequate,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def stability_slices_for_calibration(self, calibration_id: str) -> tuple[StabilitySlice, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_v2_stability_slice).where(
                    research_calibration_v2_stability_slice.c.calibration_id == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(StabilitySlice, _decoded(row)) for row in rows)

    # --- walk-forward null summaries (immutable) -------------------------

    def save_walk_forward_summary(self, item: WalkForwardNullSummary) -> WalkForwardNullSummary:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_v2_walk_forward_null_summary.c.summary_id).where(
                    research_calibration_v2_walk_forward_null_summary.c.summary_id
                    == item.summary_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_calibration_v2_walk_forward_null_summary.insert().values(
                    summary_id=item.summary_id,
                    calibration_id=item.calibration_id,
                    spec_id=item.spec_id,
                    fold_count=item.fold_count,
                    folds_beating_benchmark=item.folds_beating_benchmark,
                    final_evidence_grade=item.final_evidence_grade.value,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def walk_forward_summaries_for_calibration(
        self, calibration_id: str
    ) -> tuple[WalkForwardNullSummary, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_v2_walk_forward_null_summary).where(
                    research_calibration_v2_walk_forward_null_summary.c.calibration_id
                    == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(WalkForwardNullSummary, _decoded(row)) for row in rows)

    # --- follow-up qualifications (immutable) -----------------------------

    def save_follow_up_qualification(self, item: FollowUpQualification) -> FollowUpQualification:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_v2_follow_up_qualification.c.assessment_id).where(
                    research_calibration_v2_follow_up_qualification.c.assessment_id
                    == item.assessment_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_calibration_v2_follow_up_qualification.insert().values(
                    assessment_id=item.assessment_id,
                    calibration_id=item.calibration_id,
                    follow_up_id=item.follow_up_id,
                    parent_hypothesis_id=item.parent_hypothesis_id,
                    qualification=item.qualification.value,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def follow_up_qualifications_for_calibration(
        self, calibration_id: str
    ) -> tuple[FollowUpQualification, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_v2_follow_up_qualification).where(
                    research_calibration_v2_follow_up_qualification.c.calibration_id
                    == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(FollowUpQualification, _decoded(row)) for row in rows)
