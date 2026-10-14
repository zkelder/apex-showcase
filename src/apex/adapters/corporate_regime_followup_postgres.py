"""PostgreSQL repository for Corporate Calibration-Qualified Regime
Follow-Up v1 (migration ``0091``).

Mirrors the ``payload``-column pattern ``adapters.corporate_calibration_
depth_postgres``/``adapters.corporate_market_research_postgres`` already
established: a handful of indexed top-level columns for querying, and the
full canonical dataclass retained as JSON so ``rehydrate`` restores an
exact original instance. Every table here is append-only and immutable.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.corporate_regime_followup import (
    CorporateRegimeFollowUpResult,
    CorporateRegimeFollowUpSpecification,
    CorporateRegimeNullControl,
    RegimeSplitRole,
)
from apex.domain.values import canonical_json_bytes

CORPORATE_REGIME_FOLLOWUP_IMMUTABLE_TABLES = (
    "corporate_regime_followup_specification",
    "corporate_regime_followup_null_control",
    "corporate_regime_followup_result",
)
CORPORATE_REGIME_FOLLOWUP_TABLES = CORPORATE_REGIME_FOLLOWUP_IMMUTABLE_TABLES

metadata = sa.MetaData()

corporate_regime_followup_specification = sa.Table(
    "corporate_regime_followup_specification",
    metadata,
    sa.Column("specification_id", sa.String(160), primary_key=True),
    sa.Column("follow_up_id", sa.String(160), nullable=False),
    sa.Column("parent_hypothesis_id", sa.String(160), nullable=False),
    sa.Column("parent_conclusion_id", sa.String(160), nullable=False),
    sa.Column("agenda_id", sa.String(160), nullable=False),
    sa.Column("feature_id", sa.String(48), nullable=False),
    sa.Column("outcome_id", sa.String(48), nullable=False),
    sa.Column("horizon_sessions", sa.Integer(), nullable=False),
    sa.Column("post_hoc_status", sa.String(60), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_regime_followup_null_control = sa.Table(
    "corporate_regime_followup_null_control",
    metadata,
    sa.Column("control_id", sa.String(160), primary_key=True),
    sa.Column("specification_id", sa.String(160), nullable=False),
    sa.Column("split_role", sa.String(16), nullable=False),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("seed", sa.BigInteger(), nullable=False),
    sa.Column("mechanism", sa.String(60), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

corporate_regime_followup_result = sa.Table(
    "corporate_regime_followup_result",
    metadata,
    sa.Column("result_id", sa.String(160), primary_key=True),
    sa.Column("specification_id", sa.String(160), nullable=False),
    sa.Column("conclusion", sa.String(40), nullable=False),
    sa.Column("evidence_grade", sa.String(20), nullable=False),
    sa.Column("independent_confirmation_required", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class CorporateRegimeFollowUpPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise CorporateRegimeFollowUpPersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresCorporateRegimeFollowUpRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- specification (immutable) --------------------------------------

    def specification_by_id(
        self, specification_id: str
    ) -> CorporateRegimeFollowUpSpecification | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_regime_followup_specification).where(
                        corporate_regime_followup_specification.c.specification_id
                        == specification_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return (
            None if row is None else rehydrate(CorporateRegimeFollowUpSpecification, _decoded(row))
        )

    def specification_by_follow_up(
        self, follow_up_id: str
    ) -> CorporateRegimeFollowUpSpecification | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_regime_followup_specification).where(
                        corporate_regime_followup_specification.c.follow_up_id == follow_up_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return (
            None if row is None else rehydrate(CorporateRegimeFollowUpSpecification, _decoded(row))
        )

    def save_specification(
        self, specification: CorporateRegimeFollowUpSpecification
    ) -> CorporateRegimeFollowUpSpecification:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(corporate_regime_followup_specification.c.specification_id).where(
                    corporate_regime_followup_specification.c.specification_id
                    == specification.specification_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return specification
            connection.execute(
                corporate_regime_followup_specification.insert().values(
                    specification_id=specification.specification_id,
                    follow_up_id=specification.follow_up_id,
                    parent_hypothesis_id=specification.parent_hypothesis_id,
                    parent_conclusion_id=specification.parent_conclusion_id,
                    agenda_id=specification.agenda_id,
                    feature_id=specification.feature_id.value,
                    outcome_id=specification.outcome_id.value,
                    horizon_sessions=specification.horizon_sessions,
                    post_hoc_status=specification.post_hoc_status,
                    created_at=specification.created_at,
                    payload=_payload(specification),
                    authority="NONE",
                )
            )
        return specification

    # --- null control (immutable) ----------------------------------------

    def save_null_control(self, control: CorporateRegimeNullControl) -> CorporateRegimeNullControl:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(corporate_regime_followup_null_control.c.control_id).where(
                    corporate_regime_followup_null_control.c.control_id == control.control_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return control
            connection.execute(
                corporate_regime_followup_null_control.insert().values(
                    control_id=control.control_id,
                    specification_id=control.specification_id,
                    split_role=control.split_role.value,
                    ordinal=control.ordinal,
                    seed=control.seed,
                    mechanism=control.mechanism.value,
                    created_at=control.created_at,
                    payload=_payload(control),
                    authority="NONE",
                )
            )
        return control

    def controls_for_specification(
        self, specification_id: str, *, role: RegimeSplitRole | None = None
    ) -> tuple[CorporateRegimeNullControl, ...]:
        statement = sa.select(corporate_regime_followup_null_control).where(
            corporate_regime_followup_null_control.c.specification_id == specification_id
        )
        if role is not None:
            statement = statement.where(
                corporate_regime_followup_null_control.c.split_role == role.value
            )
        statement = statement.order_by(
            corporate_regime_followup_null_control.c.split_role,
            corporate_regime_followup_null_control.c.ordinal,
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            return tuple(rehydrate(CorporateRegimeNullControl, _decoded(row)) for row in rows)

    # --- result (immutable) -----------------------------------------------

    def result_by_specification(
        self, specification_id: str
    ) -> CorporateRegimeFollowUpResult | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(corporate_regime_followup_result).where(
                        corporate_regime_followup_result.c.specification_id == specification_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CorporateRegimeFollowUpResult, _decoded(row))

    def save_result(self, result: CorporateRegimeFollowUpResult) -> CorporateRegimeFollowUpResult:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(corporate_regime_followup_result.c.result_id).where(
                    corporate_regime_followup_result.c.result_id == result.result_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return result
            connection.execute(
                corporate_regime_followup_result.insert().values(
                    result_id=result.result_id,
                    specification_id=result.specification_id,
                    conclusion=result.conclusion.value,
                    evidence_grade=result.evidence_grade.value,
                    independent_confirmation_required=result.independent_confirmation_required,
                    created_at=result.created_at,
                    payload=_payload(result),
                    authority="NONE",
                )
            )
        return result
