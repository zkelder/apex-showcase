"""PostgreSQL repository for Research Signal Calibration and
Negative-Control Hardening v1 (migration ``0082``).

Mirrors the ``payload``-column pattern ``adapters.cross_market_research_
postgres`` already established: a handful of indexed top-level columns for
querying, and the full canonical dataclass retained as JSON so
``rehydrate`` restores an exact original instance.
``research_calibration_run`` is the one deliberately mutable table (no
immutable trigger, exactly mirroring ``cross_market_agenda_run``) -- every
other table here is append-only and immutable.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.research_calibration import (
    CalibratedPromotionEvaluation,
    CalibrationBreakdown,
    CalibrationPlan,
    CalibrationReport,
    CalibrationRun,
    FollowUpPriorityAssessment,
    NullControlOutcome,
    NullControlSpec,
    PriorHypothesisRecalibration,
    StatisticalMethodComparison,
)
from apex.domain.values import canonical_json_bytes

RESEARCH_CALIBRATION_IMMUTABLE_TABLES = (
    "research_calibration_plan",
    "research_calibration_null_control",
    "research_calibration_null_outcome",
    "research_calibration_breakdown",
    "research_calibration_method_comparison",
    "research_calibration_report",
    "research_calibration_prior_recalibration",
    "research_calibration_follow_up_priority",
    "research_calibration_promotion_evaluation",
)
RESEARCH_CALIBRATION_MUTABLE_TABLES = ("research_calibration_run",)
RESEARCH_CALIBRATION_TABLES = (
    RESEARCH_CALIBRATION_IMMUTABLE_TABLES + RESEARCH_CALIBRATION_MUTABLE_TABLES
)

metadata = sa.MetaData()

research_calibration_plan = sa.Table(
    "research_calibration_plan",
    metadata,
    sa.Column("calibration_id", sa.String(160), primary_key=True),
    sa.Column("version", sa.String(80), nullable=False),
    sa.Column("dataset_snapshot_id", sa.String(160), nullable=False),
    sa.Column("replay_run_id", sa.String(160), nullable=False),
    sa.Column("cross_market_dataset_id", sa.String(160), nullable=False),
    sa.Column("source_agenda_id", sa.String(160), nullable=False),
    sa.Column("negative_control_budget", sa.Integer(), nullable=False),
    sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_calibration_null_control = sa.Table(
    "research_calibration_null_control",
    metadata,
    sa.Column("spec_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("family", sa.String(40), nullable=False),
    sa.Column("nature", sa.String(40), nullable=False),
    sa.Column("design_family", sa.String(40), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_calibration_run = sa.Table(
    "research_calibration_run",
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

research_calibration_null_outcome = sa.Table(
    "research_calibration_null_outcome",
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

research_calibration_breakdown = sa.Table(
    "research_calibration_breakdown",
    metadata,
    sa.Column("breakdown_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("dimension_kind", sa.String(40), nullable=False),
    sa.Column("dimension_label", sa.String(80), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_calibration_method_comparison = sa.Table(
    "research_calibration_method_comparison",
    metadata,
    sa.Column("comparison_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("method_id", sa.String(80), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

research_calibration_report = sa.Table(
    "research_calibration_report",
    metadata,
    sa.Column("report_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("calibration_run_id", sa.String(160), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("calibration_id", name="uq_rcr_calibration"),
)

research_calibration_prior_recalibration = sa.Table(
    "research_calibration_prior_recalibration",
    metadata,
    sa.Column("recalibration_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("hypothesis_id", sa.String(160), nullable=False),
    sa.Column("calibrated_evidence_grade", sa.String(20), nullable=False),
    sa.Column("follow_up_priority", sa.String(40), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("calibration_id", "hypothesis_id", name="uq_rcpr_calibration_hypothesis"),
)

research_calibration_follow_up_priority = sa.Table(
    "research_calibration_follow_up_priority",
    metadata,
    sa.Column("assessment_id", sa.String(160), primary_key=True),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("follow_up_id", sa.String(160), nullable=False),
    sa.Column("classification", sa.String(40), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("calibration_id", "follow_up_id", name="uq_rcfup_calibration_follow_up"),
)

research_calibration_promotion_evaluation = sa.Table(
    "research_calibration_promotion_evaluation",
    metadata,
    sa.Column("calibrated_promotion_evaluation_id", sa.String(160), primary_key=True),
    sa.Column("promotion_evaluation_id", sa.String(160), nullable=False),
    sa.Column("calibration_id", sa.String(160), nullable=False),
    sa.Column("eligible", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class ResearchCalibrationPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise ResearchCalibrationPersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresCalibrationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- calibration plan (immutable) ---------------------------------

    def plan_by_id(self, calibration_id: str) -> CalibrationPlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_calibration_plan).where(
                        research_calibration_plan.c.calibration_id == calibration_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CalibrationPlan, _decoded(row))

    def save_plan(self, plan: CalibrationPlan) -> CalibrationPlan:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_plan.c.calibration_id).where(
                    research_calibration_plan.c.calibration_id == plan.calibration_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return plan
            connection.execute(
                research_calibration_plan.insert().values(
                    calibration_id=plan.calibration_id,
                    version=plan.version,
                    dataset_snapshot_id=plan.dataset_snapshot_id,
                    replay_run_id=plan.replay_run_id,
                    cross_market_dataset_id=plan.cross_market_dataset_id,
                    source_agenda_id=plan.source_agenda_id,
                    negative_control_budget=plan.negative_control_budget,
                    registered_at=plan.registered_at,
                    payload=_payload(plan),
                    authority="NONE",
                )
            )
        return plan

    def list_plans(self) -> tuple[CalibrationPlan, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_plan).order_by(
                    research_calibration_plan.c.registered_at
                )
            ).mappings()
            return tuple(rehydrate(CalibrationPlan, _decoded(row)) for row in rows)

    # --- null control specs (immutable) -------------------------------

    def spec_by_id(self, spec_id: str) -> NullControlSpec | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_calibration_null_control).where(
                        research_calibration_null_control.c.spec_id == spec_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(NullControlSpec, _decoded(row))

    def save_spec(self, spec: NullControlSpec) -> NullControlSpec:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_null_control.c.spec_id).where(
                    research_calibration_null_control.c.spec_id == spec.spec_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return spec
            connection.execute(
                research_calibration_null_control.insert().values(
                    spec_id=spec.spec_id,
                    calibration_id=spec.calibration_id,
                    family=spec.family.value,
                    nature=spec.nature.value,
                    design_family=spec.design_family.value,
                    ordinal=spec.ordinal,
                    created_at=spec.created_at,
                    payload=_payload(spec),
                    authority="NONE",
                )
            )
        return spec

    def specs_for_calibration(self, calibration_id: str) -> tuple[NullControlSpec, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_null_control)
                .where(research_calibration_null_control.c.calibration_id == calibration_id)
                .order_by(research_calibration_null_control.c.ordinal)
            ).mappings()
            return tuple(rehydrate(NullControlSpec, _decoded(row)) for row in rows)

    # --- calibration run (the one mutable cursor) ---------------------

    def run_by_calibration(self, calibration_id: str) -> CalibrationRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_calibration_run).where(
                        research_calibration_run.c.calibration_id == calibration_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CalibrationRun, _decoded(row))

    def run_by_id(self, calibration_run_id: str) -> CalibrationRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_calibration_run).where(
                        research_calibration_run.c.calibration_run_id == calibration_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CalibrationRun, _decoded(row))

    def save_run(self, run: CalibrationRun) -> CalibrationRun:
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
                sa.select(research_calibration_run.c.calibration_run_id).where(
                    research_calibration_run.c.calibration_run_id == run.calibration_run_id
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    research_calibration_run.insert().values(
                        calibration_run_id=run.calibration_run_id, **values
                    )
                )
            else:
                connection.execute(
                    research_calibration_run.update()
                    .where(research_calibration_run.c.calibration_run_id == run.calibration_run_id)
                    .values(**values)
                )
        return run

    # --- null control outcomes (immutable) ----------------------------

    def outcome_by_spec(self, spec_id: str) -> NullControlOutcome | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_calibration_null_outcome)
                    .where(research_calibration_null_outcome.c.spec_id == spec_id)
                    .order_by(research_calibration_null_outcome.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(NullControlOutcome, _decoded(row))

    def save_outcome(self, outcome: NullControlOutcome) -> NullControlOutcome:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_null_outcome.c.outcome_id).where(
                    research_calibration_null_outcome.c.outcome_id == outcome.outcome_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return outcome
            connection.execute(
                research_calibration_null_outcome.insert().values(
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
                sa.select(research_calibration_null_outcome).where(
                    research_calibration_null_outcome.c.calibration_run_id == calibration_run_id
                )
            ).mappings()
            return tuple(rehydrate(NullControlOutcome, _decoded(row)) for row in rows)

    # --- breakdowns (immutable) ----------------------------------------

    def save_breakdown(self, breakdown: CalibrationBreakdown) -> CalibrationBreakdown:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_breakdown.c.breakdown_id).where(
                    research_calibration_breakdown.c.breakdown_id == breakdown.breakdown_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return breakdown
            connection.execute(
                research_calibration_breakdown.insert().values(
                    breakdown_id=breakdown.breakdown_id,
                    calibration_id=breakdown.calibration_id,
                    dimension_kind=breakdown.dimension_kind,
                    dimension_label=breakdown.dimension_label,
                    created_at=breakdown.created_at,
                    payload=_payload(breakdown),
                    authority="NONE",
                )
            )
        return breakdown

    def breakdowns_for_calibration(self, calibration_id: str) -> tuple[CalibrationBreakdown, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_breakdown).where(
                    research_calibration_breakdown.c.calibration_id == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(CalibrationBreakdown, _decoded(row)) for row in rows)

    # --- statistical-method comparisons (immutable) ---------------------

    def save_method_comparison(
        self, comparison: StatisticalMethodComparison
    ) -> StatisticalMethodComparison:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_method_comparison.c.comparison_id).where(
                    research_calibration_method_comparison.c.comparison_id
                    == comparison.comparison_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return comparison
            connection.execute(
                research_calibration_method_comparison.insert().values(
                    comparison_id=comparison.comparison_id,
                    calibration_id=comparison.calibration_id,
                    method_id=comparison.method_id,
                    created_at=comparison.created_at,
                    payload=_payload(comparison),
                    authority="NONE",
                )
            )
        return comparison

    def method_comparisons_for_calibration(
        self, calibration_id: str
    ) -> tuple[StatisticalMethodComparison, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_method_comparison).where(
                    research_calibration_method_comparison.c.calibration_id == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(StatisticalMethodComparison, _decoded(row)) for row in rows)

    # --- calibration report (immutable, one per calibration) -----------

    def report_by_calibration(self, calibration_id: str) -> CalibrationReport | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(research_calibration_report).where(
                        research_calibration_report.c.calibration_id == calibration_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CalibrationReport, _decoded(row))

    def save_report(self, report: CalibrationReport) -> CalibrationReport:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_report.c.report_id).where(
                    research_calibration_report.c.calibration_id == report.calibration_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return report
            connection.execute(
                research_calibration_report.insert().values(
                    report_id=report.report_id,
                    calibration_id=report.calibration_id,
                    calibration_run_id=report.calibration_run_id,
                    created_at=report.created_at,
                    payload=_payload(report),
                    authority="NONE",
                )
            )
        return report

    # --- prior-agenda recalibration (immutable) -------------------------

    def save_recalibration(
        self, item: PriorHypothesisRecalibration
    ) -> PriorHypothesisRecalibration:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_prior_recalibration.c.recalibration_id).where(
                    research_calibration_prior_recalibration.c.recalibration_id
                    == item.recalibration_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_calibration_prior_recalibration.insert().values(
                    recalibration_id=item.recalibration_id,
                    calibration_id=item.calibration_id,
                    hypothesis_id=item.hypothesis_id,
                    calibrated_evidence_grade=item.calibrated_evidence_grade.value,
                    follow_up_priority=item.follow_up_priority.value,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def recalibrations_for_calibration(
        self, calibration_id: str
    ) -> tuple[PriorHypothesisRecalibration, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_prior_recalibration).where(
                    research_calibration_prior_recalibration.c.calibration_id == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(PriorHypothesisRecalibration, _decoded(row)) for row in rows)

    # --- follow-up priority (immutable) ---------------------------------

    def save_follow_up_priority(
        self, item: FollowUpPriorityAssessment
    ) -> FollowUpPriorityAssessment:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(research_calibration_follow_up_priority.c.assessment_id).where(
                    research_calibration_follow_up_priority.c.assessment_id == item.assessment_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_calibration_follow_up_priority.insert().values(
                    assessment_id=item.assessment_id,
                    calibration_id=item.calibration_id,
                    follow_up_id=item.follow_up_id,
                    classification=item.classification.value,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def follow_up_priorities_for_calibration(
        self, calibration_id: str
    ) -> tuple[FollowUpPriorityAssessment, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_follow_up_priority).where(
                    research_calibration_follow_up_priority.c.calibration_id == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(FollowUpPriorityAssessment, _decoded(row)) for row in rows)

    # --- calibrated promotion evaluations (immutable) --------------------

    def save_calibrated_promotion(
        self, item: CalibratedPromotionEvaluation
    ) -> CalibratedPromotionEvaluation:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(
                    research_calibration_promotion_evaluation.c.calibrated_promotion_evaluation_id
                ).where(
                    research_calibration_promotion_evaluation.c.calibrated_promotion_evaluation_id
                    == item.calibrated_promotion_evaluation_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return item
            connection.execute(
                research_calibration_promotion_evaluation.insert().values(
                    calibrated_promotion_evaluation_id=item.calibrated_promotion_evaluation_id,
                    promotion_evaluation_id=item.promotion_evaluation_id,
                    calibration_id=item.calibration_id,
                    eligible=item.eligible,
                    created_at=item.created_at,
                    payload=_payload(item),
                    authority="NONE",
                )
            )
        return item

    def calibrated_promotions_for_calibration(
        self, calibration_id: str
    ) -> tuple[CalibratedPromotionEvaluation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(research_calibration_promotion_evaluation).where(
                    research_calibration_promotion_evaluation.c.calibration_id == calibration_id
                )
            ).mappings()
            return tuple(rehydrate(CalibratedPromotionEvaluation, _decoded(row)) for row in rows)
