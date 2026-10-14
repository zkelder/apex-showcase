"""Thin orchestration over the existing atomic sustained-SHADOW worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.application.sustained_shadow import ShadowWorkerResult
from apex.domain.securities import SecuritiesCalendarSession
from apex.domain.sustained_shadow_operations import (
    SecuritiesShadowOperationJob,
    SecuritiesShadowSchedulePolicy,
    ShadowOperationState,
)


class SustainedShadowOperationsError(RuntimeError):
    pass


class SustainedShadowOperationsRepository(Protocol):
    def policy(self, plan_id: str) -> SecuritiesShadowSchedulePolicy | None: ...
    def save_policy(
        self, policy: SecuritiesShadowSchedulePolicy
    ) -> SecuritiesShadowSchedulePolicy: ...
    def job(self, policy_id: str, session_id: str) -> SecuritiesShadowOperationJob | None: ...
    def jobs(self, policy_id: str) -> tuple[SecuritiesShadowOperationJob, ...]: ...
    def save_job(self, job: SecuritiesShadowOperationJob) -> SecuritiesShadowOperationJob: ...


@dataclass(frozen=True, slots=True)
class DailyShadowOperationResult:
    job: SecuritiesShadowOperationJob
    worker: ShadowWorkerResult | None
    outcomes_resolved: int
    recommendation: str


class RunSecuritiesShadowDailyOperation:
    """Claim one durable job and delegate capture to the pre-existing worker port."""

    def __init__(
        self,
        *,
        repository: SustainedShadowOperationsRepository,
        worker_once: Callable[[], ShadowWorkerResult],
        resolve_once: Callable[[], int],
    ) -> None:
        self._repository = repository
        self._worker_once = worker_once
        self._resolve_once = resolve_once

    def execute(
        self,
        *,
        policy: SecuritiesShadowSchedulePolicy,
        session_id: str,
        due_at: datetime,
        owner: str,
        token: str,
        now: datetime,
    ) -> DailyShadowOperationResult:
        retained = self._repository.policy(policy.plan_id)
        if retained is None:
            retained = self._repository.save_policy(policy)
        elif retained.canonical_hash != policy.canonical_hash:
            raise SustainedShadowOperationsError("SCHEDULE_POLICY_CONFLICT")
        job = self._repository.job(retained.policy_id, session_id)
        if job is None:
            job = self._repository.save_job(
                SecuritiesShadowOperationJob.scheduled(
                    policy=retained, session_id=session_id, due_at=due_at
                )
            )
        if job.state in {
            ShadowOperationState.COMPLETED,
            ShadowOperationState.COMPLETED_NO_NEW_SESSION,
            ShadowOperationState.PERMANENT_FAILURE,
            ShadowOperationState.CANCELED,
            ShadowOperationState.BLOCKED_INTEGRITY,
        }:
            return DailyShadowOperationResult(job, None, 0, "IDEMPOTENT_REPLAY")
        if retained.paused:
            completed = self._repository.save_job(
                job.finish(state=ShadowOperationState.CANCELED, result_code="SCHEDULE_PAUSED")
            )
            return DailyShadowOperationResult(completed, None, 0, "PAUSED")
        if now < due_at:
            return DailyShadowOperationResult(job, None, 0, "NOT_DUE")
        if (
            job.state is ShadowOperationState.RETRYABLE_FAILURE
            and job.attempt >= retained.maximum_attempts
        ):
            exhausted = self._repository.save_job(
                job.finish(
                    state=ShadowOperationState.PERMANENT_FAILURE,
                    result_code="ATTEMPT_BUDGET_EXHAUSTED",
                )
            )
            return DailyShadowOperationResult(exhausted, None, 0, "ATTEMPT_BUDGET_EXHAUSTED")
        claimed = self._repository.save_job(
            job.claim(owner=owner, token=token, now=now, lease_seconds=retained.lease_seconds)
        )
        try:
            worker = self._worker_once()
            outcomes = self._resolve_once()
        except SustainedShadowOperationsError:
            raise
        except Exception as error:
            state = (
                ShadowOperationState.PERMANENT_FAILURE
                if "AUTHENTICATION_REJECTED" in str(error)
                else ShadowOperationState.RETRYABLE_FAILURE
            )
            completed = self._repository.save_job(
                claimed.finish(state=state, result_code=type(error).__name__)
            )
            return DailyShadowOperationResult(
                completed, None, 0, completed.result_code or "FAILURE"
            )
        state = (
            ShadowOperationState.COMPLETED_NO_NEW_SESSION
            if worker.state.value == "NO_NEW_SESSION"
            else ShadowOperationState.COMPLETED
        )
        completed = self._repository.save_job(
            claimed.finish(state=state, result_code=worker.state.value)
        )
        return DailyShadowOperationResult(completed, worker, outcomes, "OBSERVATION_ONLY")


def latest_evidence_eligible_session(
    *,
    sessions: tuple[SecuritiesCalendarSession, ...],
    policy: SecuritiesShadowSchedulePolicy,
    now: datetime,
) -> SecuritiesCalendarSession | None:
    """Use retained official close timestamps, including XNYS early closes."""
    if now.tzinfo is None:
        raise SustainedShadowOperationsError("SCHEDULE_NOW_TIMESTAMP_REQUIRED")
    eligible = tuple(item for item in sessions if policy.due_at(item.closes_at) <= now)
    return max(eligible, key=lambda item: item.closes_at) if eligible else None


class InMemorySustainedShadowOperationsRepository:
    def __init__(self) -> None:
        self.policies: dict[str, SecuritiesShadowSchedulePolicy] = {}
        self.job_items: dict[tuple[str, str], SecuritiesShadowOperationJob] = {}

    def policy(self, plan_id: str) -> SecuritiesShadowSchedulePolicy | None:
        return self.policies.get(plan_id)

    def save_policy(self, policy: SecuritiesShadowSchedulePolicy) -> SecuritiesShadowSchedulePolicy:
        old = self.policies.get(policy.plan_id)
        if old is not None and old.canonical_hash != policy.canonical_hash:
            raise SustainedShadowOperationsError("SCHEDULE_POLICY_CONFLICT")
        self.policies[policy.plan_id] = policy
        return policy

    def job(self, policy_id: str, session_id: str) -> SecuritiesShadowOperationJob | None:
        return self.job_items.get((policy_id, session_id))

    def jobs(self, policy_id: str) -> tuple[SecuritiesShadowOperationJob, ...]:
        return tuple(item for item in self.job_items.values() if item.policy_id == policy_id)

    def save_job(self, job: SecuritiesShadowOperationJob) -> SecuritiesShadowOperationJob:
        key = (job.policy_id, job.session_id)
        old = self.job_items.get(key)
        # State/lease transitions retain one immutable job identity.
        if (
            old is not None
            and old.canonical_hash != job.canonical_hash
            and old.job_id != job.job_id
        ):
            raise SustainedShadowOperationsError("SCHEDULE_JOB_CONFLICT")
        self.job_items[key] = job
        return job
