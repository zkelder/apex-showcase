"""Pure, observation-only Securities sustained-SHADOW contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import content_digest, deterministic_id

SECURITIES_SUSTAINED_SHADOW_V1 = "SECURITIES_SUSTAINED_SHADOW_V1"


class SustainedShadowState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    REVOKED = "REVOKED"
    COMPLETED = "COMPLETED"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


class ShadowCaptureState(StrEnum):
    CAPTURED = "CAPTURED"
    PARTIAL = "PARTIAL"
    NO_NEW_SESSION = "NO_NEW_SESSION"
    CREDENTIALS_REQUIRED = "CREDENTIALS_REQUIRED"
    AUTHENTICATION_REJECTED = "AUTHENTICATION_REJECTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    REJECTED_INTEGRITY = "REJECTED_INTEGRITY"


class ShadowOutcomeState(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED_HORIZON = "UNRESOLVED_HORIZON"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    INVALID_LINEAGE = "INVALID_LINEAGE"
    DELISTING_UNRESOLVED = "DELISTING_UNRESOLVED"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


@dataclass(frozen=True, slots=True)
class SustainedShadowPlan:
    plan_id: str
    universe_id: str
    universe_hash: str
    policy_hash: str
    feature_schema_hash: str
    intelligence_run_id: str
    ensemble_run_id: str
    blockers: tuple[str, ...]
    state: SustainedShadowState
    watermark: str | None
    horizon_sessions: int
    operator_action_id: str
    idempotency_key: str
    canonical_hash: str
    parent_plan_id: str | None = None

    @classmethod
    def draft(
        cls,
        *,
        universe_id: str,
        universe_hash: str,
        feature_schema_hash: str,
        intelligence_run_id: str,
        ensemble_run_id: str,
        blockers: tuple[str, ...],
        operator_action_id: str,
        idempotency_key: str,
        parent_plan_id: str | None = None,
    ) -> SustainedShadowPlan:
        payload = (
            SECURITIES_SUSTAINED_SHADOW_V1,
            universe_id,
            universe_hash,
            feature_schema_hash,
            intelligence_run_id,
            ensemble_run_id,
            tuple(sorted(blockers)),
            operator_action_id,
            idempotency_key,
        ) + ((parent_plan_id,) if parent_plan_id is not None else ())
        digest = content_digest(payload)
        return cls(
            deterministic_id("securities_sustained_shadow_plan", payload),
            universe_id,
            universe_hash,
            digest,
            feature_schema_hash,
            intelligence_run_id,
            ensemble_run_id,
            tuple(sorted(blockers)),
            SustainedShadowState.DRAFT,
            None,
            5,
            operator_action_id,
            idempotency_key,
            digest,
            parent_plan_id,
        )

    def activate(self, confirmation: str) -> SustainedShadowPlan:
        if confirmation != self.plan_id:
            raise ValueError("SUSTAINED_SHADOW_EXACT_CONFIRMATION_REQUIRED")
        if self.state is not SustainedShadowState.DRAFT:
            raise ValueError("SUSTAINED_SHADOW_ACTIVATION_INVALID")
        return self._replace(state=SustainedShadowState.ACTIVE)

    def pause(self, reason: str) -> SustainedShadowPlan:
        if not reason or self.state is not SustainedShadowState.ACTIVE:
            raise ValueError("SUSTAINED_SHADOW_PAUSE_INVALID")
        return self._replace(state=SustainedShadowState.PAUSED)

    def resume(self, confirmation: str) -> SustainedShadowPlan:
        if confirmation != self.plan_id:
            raise ValueError("SUSTAINED_SHADOW_EXACT_CONFIRMATION_REQUIRED")
        if self.state is not SustainedShadowState.PAUSED:
            raise ValueError("SUSTAINED_SHADOW_RESUME_INVALID")
        return self._replace(state=SustainedShadowState.ACTIVE)

    def revoke(self, confirmation: str) -> SustainedShadowPlan:
        if confirmation != self.plan_id:
            raise ValueError("SUSTAINED_SHADOW_EXACT_CONFIRMATION_REQUIRED")
        if self.state in (SustainedShadowState.REVOKED, SustainedShadowState.COMPLETED):
            raise ValueError("SUSTAINED_SHADOW_REVOKE_INVALID")
        return self._replace(state=SustainedShadowState.REVOKED)

    def _replace(self, **values: object) -> SustainedShadowPlan:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data.update(values)
        return SustainedShadowPlan(**data)


@dataclass(frozen=True, slots=True)
class ShadowPrediction:
    prediction_id: str
    plan_id: str
    session_id: str
    instrument_id: str
    predicted_at: datetime
    outcome_session_id: str
    probability: Decimal
    expected_net_return: Decimal
    uncertainty: Decimal
    action: str
    desired_exposure: Decimal
    capture_hash: str
    feature_hash: str
    policy_hash: str
    blockers: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        plan: SustainedShadowPlan,
        session_id: str,
        instrument_id: str,
        predicted_at: datetime,
        outcome_session_id: str,
        probability: Decimal,
        expected_net_return: Decimal,
        uncertainty: Decimal,
        action: str,
        desired_exposure: Decimal,
        capture_hash: str,
        feature_hash: str,
    ) -> ShadowPrediction:
        if plan.state is not SustainedShadowState.ACTIVE:
            raise ValueError("SUSTAINED_SHADOW_PLAN_NOT_ACTIVE")
        if predicted_at.tzinfo is None:
            raise ValueError("SUSTAINED_SHADOW_TIMESTAMP_REQUIRED")
        if probability < Decimal() or probability > Decimal("1"):
            raise ValueError("SUSTAINED_SHADOW_PROBABILITY_INVALID")
        if uncertainty < Decimal():
            raise ValueError("SUSTAINED_SHADOW_UNCERTAINTY_INVALID")
        payload = (
            plan.plan_id,
            session_id,
            instrument_id,
            predicted_at,
            outcome_session_id,
            probability,
            expected_net_return,
            uncertainty,
            action,
            desired_exposure,
            capture_hash,
            feature_hash,
            plan.policy_hash,
        )
        digest = content_digest(payload)
        return cls(
            deterministic_id("securities_shadow_prediction", payload),
            plan.plan_id,
            session_id,
            instrument_id,
            predicted_at,
            outcome_session_id,
            probability,
            expected_net_return,
            uncertainty,
            action,
            desired_exposure,
            capture_hash,
            feature_hash,
            plan.policy_hash,
            plan.blockers,
            digest,
        )


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    prediction_id: str
    state: ShadowOutcomeState
    realized_return: Decimal | None
    resolved_at: datetime | None
    canonical_hash: str

    @classmethod
    def unresolved(cls, prediction_id: str) -> ShadowOutcome:
        return cls(
            prediction_id,
            ShadowOutcomeState.UNRESOLVED_HORIZON,
            None,
            None,
            content_digest((prediction_id, "UNRESOLVED_HORIZON")),
        )

    @classmethod
    def resolved(
        cls, prediction_id: str, realized_return: Decimal, resolved_at: datetime
    ) -> ShadowOutcome:
        return cls(
            prediction_id,
            ShadowOutcomeState.RESOLVED,
            realized_return,
            resolved_at,
            content_digest((prediction_id, realized_return, resolved_at)),
        )
