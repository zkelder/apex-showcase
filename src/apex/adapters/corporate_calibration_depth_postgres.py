"""PostgreSQL repository for Corporate Research Calibration Depth v1
(migration ``0090``).

Mirrors the ``payload``-column pattern ``adapters.research_calibration_v2_
postgres``/``adapters.corporate_market_research_postgres`` already
established: a handful of indexed top-level columns for querying, and the
full canonical dataclass retained as JSON so ``rehydrate`` restores an
exact original instance. ``corporate_calibration_run`` is the one
deliberately mutable table (upsert, no immutable trigger) -- every other
table here is append-only and immutable.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.corporate_calibration_depth import (
    CorporateCalibrationPlan,
    CorporateCalibrationReport,
    CorporateCalibrationRun,
    CorporateNullControlOutcome,
    CorporateNullControlSpec,
    CorporateReassessment,
)
from apex.domain.research_calibration_v2 import FollowUpQualification
from apex.domain.values import canonical_json_bytes

CORPORATE_CALIBRATION_DEPTH_IMMUTABLE_TABLES = (
    "corporate_calibration_plan",
    "corporate_calibration_null_control",
    "corporate_calibration_null_outcome",
    "corporate_calibration_reassessment",
    "corporate_calibration_follow_up_qualification",
    "corporate_calibration_report",
)
CORPORATE_CALIBRATION_DEPTH_MUTABLE_TABLES = ("corporate_calibration_run",)
CORPORATE_CALIBRATION_DEPTH_TABLES = (
    CORPORATE_CALIBRATION_DEPTH_IMMUTABLE_TABLES + CORPORATE_CALIBRATION_DEPTH_MUTABLE_TABLES
)

metadata = sa.MetaData()

corporate_calibration_plan = sa.Table(
    "corporate_calibration_plan",
    metadata,
    sa.Column("calibration_id", sa.String(160), primary_key=True),
    sa.Column("version", sa.String(80), nullable=False),
    sa.Column("source_agenda_id", sa.String(160), nullable=False),
    sa.Column("corporate_dataset_id", sa.String(160), nullable=False),
    sa.Column("equity_snapshot_id", sa.String(160), nullable=False),
    sa.Column("replay_run_id", sa.String(160), nullable=False),
    sa.Column("cross_market_dataset_id", sa.String(160), nullable=False),
    sa.Column("controls_per_stratum", sa.Integer(), nullable=False),
    sa.Column("total_new_control_budget", sa.Integer(), nullable=False),
    sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_calibration_run = sa.Table(
    "corporate_calibration_run",
    metadata,
    sa.Column("calibration_run_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("cursor_ordinal", sa.Integer(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("failure_reason", sa.Text(), nullable=True),
    sa.Column("trials_executed", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_calibration_null_control = sa.Table(
    "corporate_calibration_null_control",
    metadata,
    sa.Column("spec_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("matched_stratum", sa.String(200), nullable=False),
    sa.Column("research_design", sa.String(40), nullable=False),
    sa.Column("conditioning_shape", sa.String(40), nullable=False),
    sa.Column("mechanism", sa.String(60), nullable=False),
    sa.Column("feature_id", sa.String(48), nullable=False),
    sa.Column("outcome_id", sa.String(48), nullable=False),
    sa.Column("horizon_sessions", sa.Integer(), nullable=False),
    sa.Column("seed", sa.BigInteger(), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_calibration_null_outcome = sa.Table(
    "corporate_calibration_null_outcome",
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

corporate_calibration_reassessment = sa.Table(
    "corporate_calibration_reassessment",
    metadata,
    sa.Column("reassessment_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("subject_id", sa.String(160), nullable=False),
    sa.Column("subject_kind", sa.String(20), nullable=False),
    sa.Column("new_evidence_grade", sa.String(20), nullable=False),
    sa.Column("assessment_changed", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_calibration_follow_up_qualification = sa.Table(
    "corporate_calibration_follow_up_qualification",
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

corporate_calibration_report = sa.Table(
    "corporate_calibration_report",
    metadata,
    sa.Column("report_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("calibration_run_id", sa.String(160), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class CorporateCalibrationDepthPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise CorporateCalibrationDepthPersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresCorporateCalibrationDepthRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- calibration plan (immutable) ---------------------------------

    def plan_by_id(self, calibration_id: str) -> CorporateCalibrationPlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_calibration_plan).where(
                        corporate_calibration_plan.c.calibration_id == calibration_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CorporateCalibrationPlan, _decoded(row))

    def save_plan(self, plan: CorporateCalibrationPlan) -> CorporateCalibrationPlan:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(corporate_calibration_plan.c.calibration_id).where(
                    corporate_calibration_plan.c.calibration_id == plan.calibration_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return plan
            connection.execute(
                corporate_calibration_plan.insert().values(
                    calibration_id=plan.calibration_id,
                    version=plan.version,
                    source_agenda_id=plan.source_agenda_id,
                    corporate_dataset_id=plan.corporate_dataset_id,
                    equity_snapshot_id=plan.equity_snapshot_id,
                    replay_run_id=plan.replay_run_id,
                    cross_market_dataset_id=plan.cross_market_dataset_id,
                    controls_per_stratum=plan.controls_per_stratum,
                    total_new_control_budget=plan.total_new_control_budget,
                    registered_at=plan.registered_at,
                    payload=_payload(plan),
                    authority="NONE",
                )
            )
        return plan

    def list_plans(self) -> tuple[CorporateCalibrationPlan, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_calibration_plan).order_by(
                    corporate_calibration_plan.c.registered_at
                )
            ).mappings()
            return tuple(rehydrate(CorporateCalibrationPlan, _decoded(row)) for row in rows)

    # --- calibration run (mutable) -------------------------------------

    def run_by_id(self, calibration_run_id: str) -> CorporateCalibrationRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_calibration_run).where(
                        corporate_calibration_run.c.calibration_run_id == calibration_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CorporateCalibrationRun, _decoded(row))

    def run_by_calibration(self, calibration_id: str) -> CorporateCalibrationRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_calibration_run).where(
                        corporate_calibration_run.c.calibration_id == calibration_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CorporateCalibrationRun, _decoded(row))

    def save_run(self, run: CorporateCalibrationRun) -> CorporateCalibrationRun:
        values = {
            "calibration_id": run.calibration_id,
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
                sa.select(corporate_calibration_run.c.calibration_run_id).where(
                    corporate_calibration_run.c.calibration_run_id == run.calibration_run_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    corporate_calibration_run.insert().values(
                        calibration_run_id=run.calibration_run_id, **values
                    )
                )
            else:
                connection.execute(
                    corporate_calibration_run.update()
                    .where(corporate_calibration_run.c.calibration_run_id == run.calibration_run_id)
                    .values(**values)
                )
        return run

    # --- null control provenance (immutable) ----------------------------

    def spec_by_id(self, spec_id: str) -> CorporateNullControlSpec | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_calibration_null_control).where(
                        corporate_calibration_null_control.c.spec_id == spec_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CorporateNullControlSpec, _decoded(row))

    def save_spec(self, spec: CorporateNullControlSpec) -> CorporateNullControlSpec:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(corporate_calibration_null_control.c.spec_id).where(
                    corporate_calibration_null_control.c.spec_id == spec.spec_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return spec
            connection.execute(
                corporate_calibration_null_control.insert().values(
                    spec_id=spec.spec_id,
                    calibration_id=spec.calibration_id,
                    matched_stratum=spec.matched_stratum,
                    research_design=spec.research_design.value,
                    conditioning_shape=spec.conditioning_shape.value,
                    mechanism=spec.mechanism.value,
                    feature_id=spec.feature_id.value,
                    outcome_id=spec.outcome_id.value,
                    horizon_sessions=spec.horizon_sessions,
                    seed=spec.seed,
                    ordinal=spec.ordinal,
                    created_at=spec.created_at,
                    payload=_payload(spec),
                    authority="NONE",
                )
            )
        return spec

    def specs_for_calibration(self, calibration_id: str) -> tuple[CorporateNullControlSpec, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_calibration_null_control)
                .where(corporate_calibration_null_control.c.calibration_id == calibration_id)
                .order_by(corporate_calibration_null_control.c.ordinal)
            ).mappings()
            return tuple(rehydrate(CorporateNullControlSpec, _decoded(row)) for row in rows)

    def specs_for_stratum(self, matched_stratum: str) -> tuple[CorporateNullControlSpec, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_calibration_null_control).where(
                    corporate_calibration_null_control.c.matched_stratum == matched_stratum
                )
            ).mappings()
            return tuple(rehydrate(CorporateNullControlSpec, _decoded(row)) for row in rows)

    # --- null control outcomes (immutable) ------------------------------

    def outcome_by_spec(self, spec_id: str) -> CorporateNullControlOutcome | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_calibration_null_outcome)
                    .where(corporate_calibration_null_outcome.c.spec_id == spec_id)
                    .order_by(corporate_calibration_null_outcome.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CorporateNullControlOutcome, _decoded(row))

    def save_outcome(self, outcome: CorporateNullControlOutcome) -> CorporateNullControlOutcome:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(corporate_calibration_null_outcome.c.outcome_id).where(
                    corporate_calibration_null_outcome.c.outcome_id == outcome.outcome_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return outcome
            connection.execute(
                corporate_calibration_null_outcome.insert().values(
                    outcome_id=outcome.outcome_id,
                    spec_id=outcome.spec_id,
                    calibration_run_id=outcome.calibration_run_id,
                    conclusion_id=outcome.conclusion_id,
                    knowledge_status=outcome.knowledge_status,
                    created_at=outcome.created_at,
                    payload=_payload(outcome),
                    authority="NONE",
                )
            )
        return outcome

    def outcomes_for_run(self, calibration_run_id: str) -> tuple[CorporateNullControlOutcome, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_calibration_null_outcome).where(
                    corporate_calibration_null_outcome.c.calibration_run_id == calibration_run_id
                )
            ).mappings()
            return tuple(rehydrate(CorporateNullControlOutcome, _decoded(row)) for row in rows)

    # --- reassessment (immutable) ---------------------------------------

    def save_reassessment(self, item: CorporateReassessment) -> CorporateReassessment:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(corporate_calibration_reassessment.c.reassessment_id).where(
                    corporate_calibration_reassessment.c.reassessment_id == item.reassessment_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                corporate_calibration_reassessment.insert().values(
                    reassessment_id=item.reassessment_id,
                    calibration_id=item.calibration_id,
                    subject_id=item.subject_id,
                    subject_kind=item.subject_kind.value,
                    new_evidence_grade=item.new_evidence_grade.value,
                    assessment_changed=item.assessment_changed,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def reassessments_for_calibration(
        self, calibration_id: str
    ) -> tuple[CorporateReassessment, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(corporate_calibration_reassessment).where(
                    corporate_calibration_reassessment.c.calibration_id == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(CorporateReassessment, _decoded(row)) for row in rows)

    def reassessment_for_subject(
        self, *, calibration_id: str, subject_id: str
    ) -> CorporateReassessment | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_calibration_reassessment).where(
                        corporate_calibration_reassessment.c.calibration_id == calibration_id,
                        corporate_calibration_reassessment.c.subject_id == subject_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CorporateReassessment, _decoded(row))

    # --- follow-up qualification (immutable) ----------------------------

    def save_follow_up_qualification(self, item: FollowUpQualification) -> FollowUpQualification:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(corporate_calibration_follow_up_qualification.c.assessment_id).where(
                    corporate_calibration_follow_up_qualification.c.assessment_id
                    == item.assessment_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                corporate_calibration_follow_up_qualification.insert().values(
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
                sa.select(corporate_calibration_follow_up_qualification).where(
                    corporate_calibration_follow_up_qualification.c.calibration_id == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(FollowUpQualification, _decoded(row)) for row in rows)

    # --- calibration report (immutable) ---------------------------------

    def report_by_calibration(self, calibration_id: str) -> CorporateCalibrationReport | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_calibration_report).where(
                        corporate_calibration_report.c.calibration_id == calibration_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CorporateCalibrationReport, _decoded(row))

    def save_report(self, report: CorporateCalibrationReport) -> CorporateCalibrationReport:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(corporate_calibration_report.c.report_id).where(
                    corporate_calibration_report.c.report_id == report.report_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return report
            connection.execute(
                corporate_calibration_report.insert().values(
                    report_id=report.report_id,
                    calibration_id=report.calibration_id,
                    calibration_run_id=report.calibration_run_id,
                    created_at=report.created_at,
                    payload=_payload(report),
                    authority="NONE",
                )
            )
        return report
