"""PostgreSQL repository for durable broad-equity research evidence.

This is the durable boundary for what ``broad_equity`` owns: canonical-bar
data-quality inclusion/exclusion decisions, discovery runs, and draft SHADOW
plans.  Resolved security identity and ambiguity live in
``security_reference``; optionable-underlying and contract-reference evidence
lives in ``options_intelligence``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.broad_equity import (
    BroadEquityDiscoveryRun,
    BroadEquityShadowPlan,
    DataReadiness,
    SecurityDataQuality,
)
from apex.domain.values import canonical_json_bytes

BROAD_EQUITY_TABLES = (
    "broad_equity_security_quality",
    "broad_equity_discovery_run",
    "broad_equity_shadow_plan",
)

_INCLUDED_READINESS = frozenset({DataReadiness.READY, DataReadiness.PARTIAL})

metadata = sa.MetaData()

broad_equity_security_quality = sa.Table(
    "broad_equity_security_quality",
    metadata,
    sa.Column("canonical_hash", sa.String(64), primary_key=True),
    sa.Column("capture_id", sa.String(160), nullable=False),
    sa.Column("canonical_security_id", sa.String(160), nullable=False),
    sa.Column("readiness", sa.String(32), nullable=False),
    sa.Column("included", sa.Boolean(), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
broad_equity_discovery_run = sa.Table(
    "broad_equity_discovery_run",
    metadata,
    sa.Column("idempotency_key", sa.String(160), primary_key=True),
    sa.Column("run_id", sa.String(160), nullable=False),
    sa.Column("universe_id", sa.String(160), nullable=False),
    sa.Column("data_capture_id", sa.String(160), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
broad_equity_shadow_plan = sa.Table(
    "broad_equity_shadow_plan",
    metadata,
    sa.Column("idempotency_key", sa.String(160), primary_key=True),
    sa.Column("plan_id", sa.String(160), nullable=False),
    sa.Column("discovery_run_id", sa.String(160), nullable=False),
    sa.Column("state", sa.String(20), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class BroadEquityPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise BroadEquityPersistenceError("STORED_PAYLOAD_INVALID")
    return value


def _quality(row: RowMapping) -> SecurityDataQuality:
    return rehydrate(SecurityDataQuality, _decoded(row))


def _discovery(row: RowMapping) -> BroadEquityDiscoveryRun:
    return rehydrate(BroadEquityDiscoveryRun, _decoded(row))


def _shadow(row: RowMapping) -> BroadEquityShadowPlan:
    return rehydrate(BroadEquityShadowPlan, _decoded(row))


class PostgresBroadEquityRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def discovery_by_key(self, key: str) -> BroadEquityDiscoveryRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(broad_equity_discovery_run).where(
                        broad_equity_discovery_run.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _discovery(row) if row is not None else None

    def save_discovery(self, key: str, run: BroadEquityDiscoveryRun) -> BroadEquityDiscoveryRun:
        # BroadEquityDiscoveryRun carries no timestamp of its own (it is a pure
        # content-addressed research artifact); recorded_at is adapter-level
        # bookkeeping for when this exact immutable evidence was retained.
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    broad_equity_discovery_run.insert().values(
                        idempotency_key=key,
                        run_id=run.run_id,
                        universe_id=run.universe_id,
                        data_capture_id=run.data_capture_id,
                        recorded_at=datetime.now(tz=UTC),
                        canonical_hash=run.canonical_hash,
                        payload=_payload(run),
                        authority="NONE",
                    )
                )
        except IntegrityError as error:
            raise BroadEquityPersistenceError("DISCOVERY_PERSISTENCE_CONFLICT") from error
        return run

    def discovery(self, run_id: str) -> BroadEquityDiscoveryRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(broad_equity_discovery_run).where(
                        broad_equity_discovery_run.c.run_id == run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _discovery(row) if row is not None else None

    def shadow_by_key(self, key: str) -> BroadEquityShadowPlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(broad_equity_shadow_plan).where(
                        broad_equity_shadow_plan.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _shadow(row) if row is not None else None

    def save_shadow(self, key: str, plan: BroadEquityShadowPlan) -> BroadEquityShadowPlan:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    broad_equity_shadow_plan.insert().values(
                        idempotency_key=key,
                        plan_id=plan.plan_id,
                        discovery_run_id=plan.discovery_run_id,
                        state=plan.state,
                        recorded_at=plan.decision_at,
                        canonical_hash=plan.canonical_hash,
                        payload=_payload(plan),
                        authority="NONE",
                    )
                )
        except IntegrityError as error:
            raise BroadEquityPersistenceError("SHADOW_PERSISTENCE_CONFLICT") from error
        return plan

    def shadow(self, plan_id: str) -> BroadEquityShadowPlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(broad_equity_shadow_plan).where(
                        broad_equity_shadow_plan.c.plan_id == plan_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _shadow(row) if row is not None else None

    def save_quality(
        self,
        capture_id: str,
        qualities: tuple[SecurityDataQuality, ...],
        *,
        recorded_at: datetime,
    ) -> tuple[SecurityDataQuality, ...]:
        if not qualities:
            return qualities
        try:
            with self._engine.begin() as connection:
                for quality in qualities:
                    connection.execute(
                        broad_equity_security_quality.insert().values(
                            canonical_hash=quality.canonical_hash,
                            capture_id=capture_id,
                            canonical_security_id=quality.canonical_security_id,
                            readiness=quality.readiness.value,
                            included=quality.readiness in _INCLUDED_READINESS,
                            recorded_at=recorded_at,
                            payload=_payload(quality),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise BroadEquityPersistenceError("QUALITY_PERSISTENCE_CONFLICT") from error
        return qualities

    def qualities_for_capture(self, capture_id: str) -> tuple[SecurityDataQuality, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(broad_equity_security_quality)
                .where(broad_equity_security_quality.c.capture_id == capture_id)
                .order_by(broad_equity_security_quality.c.canonical_security_id)
            ).mappings()
            return tuple(_quality(row) for row in rows)
