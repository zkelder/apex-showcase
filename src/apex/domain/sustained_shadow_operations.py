"""Immutable scheduling contracts for observation-only sustained SHADOW work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from apex.domain.values import content_digest, deterministic_id

XNYS_DAILY_SHADOW_SCHEDULE_V1 = "XNYS_DAILY_SHADOW_SCHEDULE_V1"
SECURITIES_FORWARD_EVIDENCE_GATE_V1 = "SECURITIES_FORWARD_EVIDENCE_GATE_V1"


class ShadowOperationState(StrEnum):
    SCHEDULED = "SCHEDULED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    COMPLETED_NO_NEW_SESSION = "COMPLETED_NO_NEW_SESSION"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    CANCELED = "CANCELED"
    BLOCKED_INTEGRITY = "BLOCKED_INTEGRITY"


TERMINAL_STATES = frozenset(
    {
        ShadowOperationState.COMPLETED,
        ShadowOperationState.COMPLETED_NO_NEW_SESSION,
        ShadowOperationState.PERMANENT_FAILURE,
        ShadowOperationState.CANCELED,
        ShadowOperationState.BLOCKED_INTEGRITY,
    }
)


@dataclass(frozen=True, slots=True)
class SecuritiesShadowSchedulePolicy:
    policy_id: str
    plan_id: str
    calendar_identity: str
    availability_delay_seconds: int
    maximum_attempts: int
    lease_seconds: int
    paused: bool
    canonical_hash: str

    @classmethod
    def create(cls, *, plan_id: str, calendar_identity: str) -> SecuritiesShadowSchedulePolicy:
        payload = (XNYS_DAILY_SHADOW_SCHEDULE_V1, plan_id, calendar_identity, 900, 3, 300)
        digest = content_digest(payload)
        return cls(
            policy_id=deterministic_id("securities_shadow_schedule_policy", payload),
            plan_id=plan_id,
            calendar_identity=calendar_identity,
            availability_delay_seconds=900,
            maximum_attempts=3,
            lease_seconds=300,
            paused=False,
            canonical_hash=digest,
        )

    def due_at(self, official_close: datetime) -> datetime:
        if official_close.tzinfo is None:
            raise ValueError("SCHEDULE_CLOSE_TIMESTAMP_REQUIRED")
        return official_close + timedelta(seconds=self.availability_delay_seconds)


@dataclass(frozen=True, slots=True)
class SecuritiesShadowOperationJob:
    job_id: str
    policy_id: str
    plan_id: str
    session_id: str
    due_at: datetime
    attempt: int
    state: ShadowOperationState
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    result_code: str | None
    canonical_hash: str

    @classmethod
    def scheduled(
        cls, *, policy: SecuritiesShadowSchedulePolicy, session_id: str, due_at: datetime
    ) -> SecuritiesShadowOperationJob:
        if due_at.tzinfo is None:
            raise ValueError("SCHEDULE_DUE_TIMESTAMP_REQUIRED")
        payload = (policy.policy_id, policy.plan_id, session_id, due_at)
        return cls(
            deterministic_id("securities_shadow_operation_job", payload),
            policy.policy_id,
            policy.plan_id,
            session_id,
            due_at,
            0,
            ShadowOperationState.SCHEDULED,
            None,
            None,
            None,
            None,
            content_digest(payload),
        )

    def claim(
        self, *, owner: str, token: str, now: datetime, lease_seconds: int
    ) -> SecuritiesShadowOperationJob:
        if now.tzinfo is None or not owner or not token:
            raise ValueError("SCHEDULE_LEASE_IDENTITY_REQUIRED")
        if self.state in TERMINAL_STATES:
            return self
        if (
            self.lease_expires_at is not None
            and self.lease_expires_at > now
            and self.lease_owner != owner
        ):
            raise ValueError("SCHEDULE_LEASE_HELD")
        return self._replace(
            attempt=self.attempt + 1,
            state=ShadowOperationState.CLAIMED,
            lease_owner=owner,
            lease_token=token,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )

    def finish(
        self, *, state: ShadowOperationState, result_code: str
    ) -> SecuritiesShadowOperationJob:
        if state not in TERMINAL_STATES | {ShadowOperationState.RETRYABLE_FAILURE}:
            raise ValueError("SCHEDULE_RESULT_STATE_INVALID")
        return self._replace(
            state=state,
            result_code=result_code,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
        )

    def _replace(self, **values: object) -> SecuritiesShadowOperationJob:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data.update(values)
        return SecuritiesShadowOperationJob(**data)


def forward_evidence_recommendation(
    *,
    resolved_predictions: int,
    independent_sessions: int,
    severe_drift: bool,
    integrity_blocked: bool,
) -> str:
    """Predeclared conservative gate; it never authorizes PAPER or LIVE."""
    if integrity_blocked or severe_drift:
        return "PAUSE"
    if resolved_predictions < 60 or independent_sessions < 20:
        return "REQUIRE_MORE_EVIDENCE"
    return "ELIGIBLE_FOR_FUTURE_PAPER_REVIEW_REQUIRES_OPERATOR_REVIEW"
