"""Durable observation-only sustained-SHADOW repository."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import (
    securities_sustained_shadow_outcome,
    securities_sustained_shadow_plan,
    securities_sustained_shadow_prediction,
)
from apex.application.sustained_shadow import SustainedShadowError
from apex.domain.sustained_shadow import (
    ShadowOutcome,
    ShadowPrediction,
    SustainedShadowPlan,
    SustainedShadowState,
)
from apex.domain.values import canonical_json_bytes


class PostgresSustainedShadowRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def plan_by_key(self, key: str) -> SustainedShadowPlan | None:
        with self.engine.connect() as c:
            row = (
                c.execute(
                    sa.select(securities_sustained_shadow_plan).where(
                        securities_sustained_shadow_plan.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _plan(row)

    def plan(self, plan_id: str) -> SustainedShadowPlan | None:
        with self.engine.connect() as c:
            row = (
                c.execute(
                    sa.select(securities_sustained_shadow_plan).where(
                        securities_sustained_shadow_plan.c.plan_id == plan_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _plan(row)

    def plans(self) -> tuple[SustainedShadowPlan, ...]:
        with self.engine.connect() as c:
            rows = (
                c.execute(
                    sa.select(securities_sustained_shadow_plan).order_by(
                        securities_sustained_shadow_plan.c.created_at
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_plan(x) for x in rows)

    def save_plan(self, plan: SustainedShadowPlan) -> SustainedShadowPlan:
        old = self.plan(plan.plan_id)
        if old:
            if old.canonical_hash != plan.canonical_hash and old.state == plan.state:
                raise SustainedShadowError("SUSTAINED_SHADOW_PLAN_CONFLICT")
            with self.engine.begin() as c:
                c.execute(
                    securities_sustained_shadow_plan.update()
                    .where(securities_sustained_shadow_plan.c.plan_id == plan.plan_id)
                    .values(
                        state=plan.state.value,
                        watermark=plan.watermark,
                        payload=canonical_json_bytes(plan).decode(),
                    )
                )
            return plan
        with self.engine.begin() as c:
            c.execute(
                securities_sustained_shadow_plan.insert().values(
                    plan_id=plan.plan_id,
                    universe_id=plan.universe_id,
                    parent_plan_id=plan.parent_plan_id,
                    universe_hash=plan.universe_hash,
                    feature_schema_hash=plan.feature_schema_hash,
                    intelligence_run_id=plan.intelligence_run_id,
                    ensemble_run_id=plan.ensemble_run_id,
                    blockers=list(plan.blockers),
                    state=plan.state.value,
                    watermark=plan.watermark,
                    payload=canonical_json_bytes(plan).decode(),
                    idempotency_key=plan.idempotency_key,
                    canonical_hash=plan.canonical_hash,
                    created_at=datetime.now(tz=UTC),
                )
            )
        return plan

    def prediction(
        self, plan_id: str, session_id: str, instrument_id: str
    ) -> ShadowPrediction | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_sustained_shadow_prediction).where(
                        securities_sustained_shadow_prediction.c.plan_id == plan_id,
                        securities_sustained_shadow_prediction.c.session_id == session_id,
                        securities_sustained_shadow_prediction.c.instrument_id == instrument_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _prediction(row)

    def save_prediction(self, prediction: ShadowPrediction) -> ShadowPrediction:
        old = self.prediction(prediction.plan_id, prediction.session_id, prediction.instrument_id)
        if old is not None:
            if old.canonical_hash != prediction.canonical_hash:
                raise SustainedShadowError("SUSTAINED_SHADOW_PREDICTION_CONFLICT")
            return old
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    securities_sustained_shadow_prediction.insert().values(
                        prediction_id=prediction.prediction_id,
                        plan_id=prediction.plan_id,
                        session_id=prediction.session_id,
                        instrument_id=prediction.instrument_id,
                        payload=canonical_json_bytes(prediction).decode(),
                        canonical_hash=prediction.canonical_hash,
                    )
                )
        except sa.exc.IntegrityError as error:
            raise SustainedShadowError("SUSTAINED_SHADOW_PREDICTION_CONFLICT") from error
        return prediction

    def save_outcome(self, outcome: ShadowOutcome) -> ShadowOutcome:
        with self.engine.connect() as connection:
            old = (
                connection.execute(
                    sa.select(securities_sustained_shadow_outcome).where(
                        securities_sustained_shadow_outcome.c.prediction_id == outcome.prediction_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if old is not None:
            if str(old["canonical_hash"]) != outcome.canonical_hash:
                raise SustainedShadowError("SUSTAINED_SHADOW_OUTCOME_CONFLICT")
            return outcome
        with self.engine.begin() as connection:
            connection.execute(
                securities_sustained_shadow_outcome.insert().values(
                    prediction_id=outcome.prediction_id,
                    payload=canonical_json_bytes(outcome).decode(),
                    canonical_hash=outcome.canonical_hash,
                )
            )
        return outcome

    def outcome(self, prediction_id: str) -> ShadowOutcome | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_sustained_shadow_outcome).where(
                        securities_sustained_shadow_outcome.c.prediction_id == prediction_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _outcome(row)

    def predictions(self, plan_id: str) -> tuple[ShadowPrediction, ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(securities_sustained_shadow_prediction)
                    .where(securities_sustained_shadow_prediction.c.plan_id == plan_id)
                    .order_by(
                        securities_sustained_shadow_prediction.c.session_id,
                        securities_sustained_shadow_prediction.c.instrument_id,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_prediction(row) for row in rows)


def _plan(row: RowMapping) -> SustainedShadowPlan:
    value = cast(dict[str, object], json.loads(cast(str, row["payload"])))
    return SustainedShadowPlan(
        plan_id=cast(str, value["plan_id"]),
        universe_id=cast(str, value["universe_id"]),
        universe_hash=cast(str, value["universe_hash"]),
        policy_hash=cast(str, value["policy_hash"]),
        feature_schema_hash=cast(str, value["feature_schema_hash"]),
        intelligence_run_id=cast(str, value["intelligence_run_id"]),
        ensemble_run_id=cast(str, value["ensemble_run_id"]),
        blockers=tuple(cast(list[str], value["blockers"])),
        state=SustainedShadowState(cast(str, value["state"])),
        watermark=cast(str | None, value["watermark"]),
        horizon_sessions=int(cast(str | int, value["horizon_sessions"])),
        operator_action_id=cast(str, value["operator_action_id"]),
        idempotency_key=cast(str, value["idempotency_key"]),
        canonical_hash=cast(str, value["canonical_hash"]),
        parent_plan_id=cast(str | None, value.get("parent_plan_id")),
    )


def _prediction(row: RowMapping) -> ShadowPrediction:
    value = cast(dict[str, object], json.loads(cast(str, row["payload"])))
    from apex.domain.values import parse_decimal

    return ShadowPrediction(
        prediction_id=cast(str, value["prediction_id"]),
        plan_id=cast(str, value["plan_id"]),
        session_id=cast(str, value["session_id"]),
        instrument_id=cast(str, value["instrument_id"]),
        predicted_at=datetime.fromisoformat(cast(str, value["predicted_at"])),
        outcome_session_id=cast(str, value["outcome_session_id"]),
        probability=parse_decimal(cast(str, value["probability"])),
        expected_net_return=parse_decimal(
            cast(str, value["expected_net_return"]), allow_negative=True
        ),
        uncertainty=parse_decimal(cast(str, value["uncertainty"])),
        action=cast(str, value["action"]),
        desired_exposure=parse_decimal(cast(str, value["desired_exposure"])),
        capture_hash=cast(str, value["capture_hash"]),
        feature_hash=cast(str, value["feature_hash"]),
        policy_hash=cast(str, value["policy_hash"]),
        blockers=tuple(cast(list[str], value["blockers"])),
        canonical_hash=cast(str, value["canonical_hash"]),
    )


def _outcome(row: RowMapping) -> ShadowOutcome:
    from apex.domain.sustained_shadow import ShadowOutcomeState
    from apex.domain.values import parse_decimal

    value = cast(dict[str, object], json.loads(cast(str, row["payload"])))
    return ShadowOutcome(
        prediction_id=cast(str, value["prediction_id"]),
        state=ShadowOutcomeState(cast(str, value["state"])),
        realized_return=(
            parse_decimal(cast(str, value["realized_return"]), allow_negative=True)
            if value["realized_return"] is not None
            else None
        ),
        resolved_at=(
            datetime.fromisoformat(cast(str, value["resolved_at"]))
            if value["resolved_at"] is not None
            else None
        ),
        canonical_hash=cast(str, value["canonical_hash"]),
    )
