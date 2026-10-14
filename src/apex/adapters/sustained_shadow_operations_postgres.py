"""PostgreSQL retention for durable local sustained-SHADOW operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    securities_sustained_shadow_operation_job,
    securities_sustained_shadow_schedule_policy,
)
from apex.application.sustained_shadow_operations import SustainedShadowOperationsError
from apex.domain.sustained_shadow_operations import (
    SecuritiesShadowOperationJob,
    SecuritiesShadowSchedulePolicy,
    ShadowOperationState,
)
from apex.domain.values import canonical_json_bytes


class PostgresSustainedShadowOperationsRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def policy(self, plan_id: str) -> SecuritiesShadowSchedulePolicy | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_sustained_shadow_schedule_policy).where(
                        securities_sustained_shadow_schedule_policy.c.plan_id == plan_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _policy(json.loads(cast(str, row["payload"])))

    def save_policy(self, policy: SecuritiesShadowSchedulePolicy) -> SecuritiesShadowSchedulePolicy:
        old = self.policy(policy.plan_id)
        if old is not None:
            if old.canonical_hash != policy.canonical_hash:
                raise SustainedShadowOperationsError("SCHEDULE_POLICY_CONFLICT")
            return old
        with self.engine.begin() as connection:
            connection.execute(
                securities_sustained_shadow_schedule_policy.insert().values(
                    policy_id=policy.policy_id,
                    plan_id=policy.plan_id,
                    payload=canonical_json_bytes(policy).decode(),
                    canonical_hash=policy.canonical_hash,
                    created_at=datetime.now(tz=UTC),
                )
            )
        return policy

    def job(self, policy_id: str, session_id: str) -> SecuritiesShadowOperationJob | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_sustained_shadow_operation_job).where(
                        securities_sustained_shadow_operation_job.c.policy_id == policy_id,
                        securities_sustained_shadow_operation_job.c.session_id == session_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _job(json.loads(cast(str, row["payload"])))

    def jobs(self, policy_id: str) -> tuple[SecuritiesShadowOperationJob, ...]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(securities_sustained_shadow_operation_job.c.payload)
                    .where(securities_sustained_shadow_operation_job.c.policy_id == policy_id)
                    .order_by(securities_sustained_shadow_operation_job.c.updated_at.desc())
                )
                .scalars()
                .all()
            )
        return tuple(_job(json.loads(cast(str, row))) for row in rows)

    def save_job(self, job: SecuritiesShadowOperationJob) -> SecuritiesShadowOperationJob:
        old = self.job(job.policy_id, job.session_id)
        if old is not None and old.job_id != job.job_id:
            raise SustainedShadowOperationsError("SCHEDULE_JOB_CONFLICT")
        values = dict(
            state=job.state.value,
            payload=canonical_json_bytes(job).decode(),
            canonical_hash=job.canonical_hash,
            updated_at=datetime.now(tz=UTC),
        )
        with self.engine.begin() as connection:
            if old is None:
                connection.execute(
                    securities_sustained_shadow_operation_job.insert().values(
                        job_id=job.job_id,
                        policy_id=job.policy_id,
                        session_id=job.session_id,
                        **values,
                    )
                )
            else:
                connection.execute(
                    securities_sustained_shadow_operation_job.update()
                    .where(securities_sustained_shadow_operation_job.c.job_id == job.job_id)
                    .values(**values)
                )
        return job


def _policy(value: dict[str, object]) -> SecuritiesShadowSchedulePolicy:
    return SecuritiesShadowSchedulePolicy(
        policy_id=cast(str, value["policy_id"]),
        plan_id=cast(str, value["plan_id"]),
        calendar_identity=cast(str, value["calendar_identity"]),
        availability_delay_seconds=int(cast(str | int, value["availability_delay_seconds"])),
        maximum_attempts=int(cast(str | int, value["maximum_attempts"])),
        lease_seconds=int(cast(str | int, value["lease_seconds"])),
        paused=bool(value["paused"]),
        canonical_hash=cast(str, value["canonical_hash"]),
    )


def _job(value: dict[str, object]) -> SecuritiesShadowOperationJob:
    return SecuritiesShadowOperationJob(
        job_id=cast(str, value["job_id"]),
        policy_id=cast(str, value["policy_id"]),
        plan_id=cast(str, value["plan_id"]),
        session_id=cast(str, value["session_id"]),
        due_at=datetime.fromisoformat(cast(str, value["due_at"])),
        attempt=int(cast(str | int, value["attempt"])),
        state=ShadowOperationState(cast(str, value["state"])),
        lease_owner=cast(str | None, value["lease_owner"]),
        lease_token=cast(str | None, value["lease_token"]),
        lease_expires_at=(
            datetime.fromisoformat(cast(str, value["lease_expires_at"]))
            if value["lease_expires_at"] is not None
            else None
        ),
        result_code=cast(str | None, value["result_code"]),
        canonical_hash=cast(str, value["canonical_hash"]),
    )
