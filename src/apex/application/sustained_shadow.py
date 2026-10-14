"""One-pass observation-only sustained Securities SHADOW sequencing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from apex.domain.sustained_shadow import (
    ShadowCaptureState,
    ShadowOutcome,
    ShadowPrediction,
    SustainedShadowPlan,
    SustainedShadowState,
)


class SustainedShadowError(RuntimeError):
    pass


class SustainedShadowRepository(Protocol):
    def plan_by_key(self, key: str) -> SustainedShadowPlan | None: ...
    def save_plan(self, plan: SustainedShadowPlan) -> SustainedShadowPlan: ...
    def prediction(
        self, plan_id: str, session_id: str, instrument_id: str
    ) -> ShadowPrediction | None: ...
    def save_prediction(self, prediction: ShadowPrediction) -> ShadowPrediction: ...
    def save_outcome(self, outcome: ShadowOutcome) -> ShadowOutcome: ...
    def outcome(self, prediction_id: str) -> ShadowOutcome | None: ...
    def predictions(self, plan_id: str) -> tuple[ShadowPrediction, ...]: ...


@dataclass(frozen=True, slots=True)
class ShadowWorkerResult:
    plan_id: str
    state: ShadowCaptureState
    intended_session: str | None
    prediction_count: int
    watermark: str | None
    reason: str


class InMemorySustainedShadowRepository:
    def __init__(self) -> None:
        self.plans: dict[str, SustainedShadowPlan] = {}
        self.items: dict[tuple[str, str, str], ShadowPrediction] = {}
        self.outcomes: dict[str, ShadowOutcome] = {}

    def plan_by_key(self, key: str) -> SustainedShadowPlan | None:
        return next((x for x in self.plans.values() if x.idempotency_key == key), None)

    def save_plan(self, plan: SustainedShadowPlan) -> SustainedShadowPlan:
        self.plans[plan.plan_id] = plan
        return plan

    def prediction(
        self, plan_id: str, session_id: str, instrument_id: str
    ) -> ShadowPrediction | None:
        return self.items.get((plan_id, session_id, instrument_id))

    def save_prediction(self, p: ShadowPrediction) -> ShadowPrediction:
        key = (p.plan_id, p.session_id, p.instrument_id)
        old = self.items.get(key)
        if old and old.canonical_hash != p.canonical_hash:
            raise SustainedShadowError("SUSTAINED_SHADOW_PREDICTION_CONFLICT")
        self.items[key] = p
        return p

    def save_outcome(self, o: ShadowOutcome) -> ShadowOutcome:
        old = self.outcomes.get(o.prediction_id)
        if old and old.canonical_hash != o.canonical_hash:
            raise SustainedShadowError("SUSTAINED_SHADOW_OUTCOME_CONFLICT")
        self.outcomes[o.prediction_id] = o
        return o

    def outcome(self, prediction_id: str) -> ShadowOutcome | None:
        return self.outcomes.get(prediction_id)

    def predictions(self, plan_id: str) -> tuple[ShadowPrediction, ...]:
        return tuple(x for x in self.items.values() if x.plan_id == plan_id)


class SustainedShadowService:
    def __init__(self, repo: SustainedShadowRepository) -> None:
        self.repo = repo

    def create(self, plan: SustainedShadowPlan) -> SustainedShadowPlan:
        old = self.repo.plan_by_key(plan.idempotency_key)
        if old:
            if old.canonical_hash != plan.canonical_hash:
                raise SustainedShadowError("SUSTAINED_SHADOW_PLAN_CONFLICT")
            return old
        return self.repo.save_plan(plan)

    def activate(self, plan: SustainedShadowPlan, confirmation: str) -> SustainedShadowPlan:
        return self.repo.save_plan(plan.activate(confirmation))

    def pause(self, plan: SustainedShadowPlan, reason: str) -> SustainedShadowPlan:
        return self.repo.save_plan(plan.pause(reason))

    def resume(self, plan: SustainedShadowPlan, confirmation: str) -> SustainedShadowPlan:
        return self.repo.save_plan(plan.resume(confirmation))

    def revoke(self, plan: SustainedShadowPlan, confirmation: str) -> SustainedShadowPlan:
        return self.repo.save_plan(plan.revoke(confirmation))

    def capture(
        self,
        plan: SustainedShadowPlan,
        *,
        session_id: str,
        received_at: datetime,
        capture_hash: str,
        rows: tuple[tuple[str, Decimal, Decimal, Decimal, str], ...],
    ) -> ShadowWorkerResult:
        if plan.state is not SustainedShadowState.ACTIVE:
            return ShadowWorkerResult(
                plan.plan_id,
                ShadowCaptureState.REJECTED_INTEGRITY,
                None,
                0,
                plan.watermark,
                "PLAN_NOT_ACTIVE",
            )
        if plan.watermark == session_id:
            return ShadowWorkerResult(
                plan.plan_id,
                ShadowCaptureState.NO_NEW_SESSION,
                session_id,
                0,
                plan.watermark,
                "NO_NEW_SESSION",
            )
        if len(rows) != 16:
            return ShadowWorkerResult(
                plan.plan_id,
                ShadowCaptureState.PARTIAL,
                session_id,
                0,
                plan.watermark,
                "UNIVERSE_COVERAGE_INCOMPLETE",
            )
        for instrument, probability, net, uncertainty, feature_hash in rows:
            self.repo.save_prediction(
                ShadowPrediction.create(
                    plan=plan,
                    session_id=session_id,
                    instrument_id=instrument,
                    predicted_at=received_at,
                    outcome_session_id=f"{session_id}:PLUS_FIVE",
                    probability=probability,
                    expected_net_return=net,
                    uncertainty=uncertainty,
                    action="ABSTAIN" if net <= 0 or uncertainty > Decimal("0.35") else "OPEN_LONG",
                    desired_exposure=Decimal()
                    if net <= 0 or uncertainty > Decimal("0.35")
                    else Decimal("0.10"),
                    capture_hash=capture_hash,
                    feature_hash=feature_hash,
                )
            )
        updated = plan._replace(watermark=session_id)
        self.repo.save_plan(updated)
        return ShadowWorkerResult(
            plan.plan_id, ShadowCaptureState.CAPTURED, session_id, 16, session_id, "CAPTURED"
        )

    def resolve(
        self,
        plan: SustainedShadowPlan,
        *,
        cutoff: datetime,
        realized_returns: dict[str, Decimal],
    ) -> tuple[ShadowOutcome, ...]:
        """Resolve only pre-existing predictions; this never creates or rewrites one."""
        if cutoff.tzinfo is None:
            raise SustainedShadowError("SUSTAINED_SHADOW_TIMESTAMP_REQUIRED")
        results: list[ShadowOutcome] = []
        for prediction in self.repo.predictions(plan.plan_id):
            existing = self.repo.outcome(prediction.prediction_id)
            if existing is not None:
                results.append(existing)
                continue
            if prediction.outcome_session_id not in realized_returns:
                # Horizon/missing-evidence is a current read-model state, not a final
                # immutable outcome.  Persisting it would prevent the later genuine
                # resolution from being appended without rewriting evidence.
                results.append(ShadowOutcome.unresolved(prediction.prediction_id))
                continue
            if prediction.predicted_at >= cutoff:
                raise SustainedShadowError("SUSTAINED_SHADOW_LATE_OUTCOME_CUTOFF")
            results.append(
                self.repo.save_outcome(
                    ShadowOutcome.resolved(
                        prediction.prediction_id,
                        realized_returns[prediction.outcome_session_id],
                        cutoff,
                    )
                )
            )
        return tuple(results)

    def report(self, plan: SustainedShadowPlan) -> dict[str, object]:
        predictions = self.repo.predictions(plan.plan_id)
        outcomes = tuple(self.repo.outcome(x.prediction_id) for x in predictions)
        resolved = tuple(x for x in outcomes if x and x.state.value == "RESOLVED")
        acted = tuple(x for x in predictions if x.action != "ABSTAIN")
        brier = (
            sum(
                (
                    prediction.probability
                    - (Decimal("1") if outcome.realized_return > 0 else Decimal())
                )
                ** 2
                for prediction, outcome in zip(predictions, outcomes, strict=True)
                if outcome
                and outcome.state.value == "RESOLVED"
                and outcome.realized_return is not None
            )
            / Decimal(len(resolved))
            if resolved
            else None
        )
        return {
            "plan_id": plan.plan_id,
            "state": plan.state.value,
            "watermark": plan.watermark,
            "blockers": plan.blockers,
            "prediction_count": len(predictions),
            "resolved_outcome_count": len(resolved),
            "unresolved_outcome_count": len(predictions) - len(resolved),
            "action_coverage": Decimal(len(acted)) / Decimal(len(predictions))
            if predictions
            else Decimal(),
            "brier_score": brier,
            "recommendation": "BLOCKED_INTEGRITY"
            if plan.blockers
            else "REQUIRE_MORE_FORWARD_EVIDENCE",
            "authority": "OBSERVATION_ONLY",
        }
