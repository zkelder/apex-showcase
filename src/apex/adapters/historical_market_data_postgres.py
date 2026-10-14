"""PostgreSQL repository for historical market-data capture plans, chunks,
dataset snapshots, and data-quality reports (migration ``0077``).

This module owns only what migration ``0077`` adds on top of the unmodified
``securities_instrument``/``securities_bar_evidence``/``securities_market_data_batch``
tables (migration ``0025``, queried through the existing
``adapters.securities_postgres.PostgresSecuritiesRepository``) and the
unmodified provider-fetch lifecycle (migration ``0031``,
``adapters.real_securities_postgres.PostgresRealSecuritiesDataRepository``):
the capture-plan/chunk checkpoint lifecycle, the deterministic dataset-
snapshot manifest, the data-quality report, and a narrow stable-security-id
lineage table for the instrument identities this milestone mints.

``historical_instrument_identity.stable_security_id`` is a plain indexed
column, not a hard foreign key -- matching migration ``0076``'s own
``security_identity_resolution.resolved_stable_security_id``, for the
identical reason: ``security_stable_identity.stable_security_id`` is not
itself unique-constrained (append-only evidence may retain more than one
historical row for a distinct stable identity), so Postgres cannot reference
it directly. The lineage remains fully retained and queryable; it is just not
a database-enforced foreign key.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.historical_market_data import (
    HistoricalCaptureChunk,
    HistoricalCapturePlan,
    HistoricalDataQualityReport,
    HistoricalDatasetSnapshot,
)
from apex.domain.values import canonical_json_bytes

HISTORICAL_MARKET_DATA_TABLES = (
    "historical_instrument_identity",
    "historical_capture_plan",
    "historical_capture_chunk",
    "historical_dataset_snapshot",
    "historical_data_quality_report",
)
HISTORICAL_MARKET_DATA_IMMUTABLE_TABLES = (
    "historical_instrument_identity",
    "historical_capture_plan",
    "historical_dataset_snapshot",
    "historical_data_quality_report",
)

metadata = sa.MetaData()

historical_instrument_identity = sa.Table(
    "historical_instrument_identity",
    metadata,
    sa.Column("instrument_id", sa.String(160), primary_key=True),
    sa.Column("stable_security_id", sa.String(160), nullable=False),
    sa.Column("symbol_at_capture", sa.String(32), nullable=False),
    sa.Column("asset_class", sa.String(24), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
historical_capture_plan = sa.Table(
    "historical_capture_plan",
    metadata,
    sa.Column("plan_id", sa.String(160), primary_key=True),
    sa.Column("policy_id", sa.String(160), nullable=False),
    sa.Column("provider", sa.String(80), nullable=False),
    sa.Column("start_date", sa.String(10), nullable=False),
    sa.Column("end_date", sa.String(10), nullable=False),
    sa.Column("evidence_class", sa.String(24), nullable=False),
    sa.Column("universe_count", sa.Integer(), nullable=False),
    sa.Column("chunk_count", sa.Integer(), nullable=False),
    sa.Column("operator_action_id", sa.String(160), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
historical_capture_chunk = sa.Table(
    "historical_capture_chunk",
    metadata,
    sa.Column("chunk_id", sa.String(160), primary_key=True),
    sa.Column(
        "plan_id", sa.String(160), sa.ForeignKey("historical_capture_plan.plan_id"), nullable=False
    ),
    sa.Column("ordinal", sa.Integer(), nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("operation_id", sa.String(160), nullable=True),
    sa.Column(
        "batch_id",
        sa.String(160),
        sa.ForeignKey("securities_market_data_batch.batch_id"),
        nullable=True,
    ),
    sa.Column("retry_count", sa.Integer(), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("plan_id", "ordinal"),
)
historical_dataset_snapshot = sa.Table(
    "historical_dataset_snapshot",
    metadata,
    sa.Column("snapshot_id", sa.String(160), primary_key=True),
    sa.Column(
        "plan_id", sa.String(160), sa.ForeignKey("historical_capture_plan.plan_id"), nullable=False
    ),
    sa.Column("evidence_class", sa.String(24), nullable=False),
    sa.Column("start_date", sa.String(10), nullable=False),
    sa.Column("end_date", sa.String(10), nullable=False),
    sa.Column("completeness_state", sa.String(16), nullable=False),
    sa.Column("point_in_time_universe_state", sa.String(48), nullable=False),
    sa.Column("survivorship_state", sa.String(32), nullable=False),
    sa.Column("observation_count", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
historical_data_quality_report = sa.Table(
    "historical_data_quality_report",
    metadata,
    sa.Column("report_id", sa.String(160), primary_key=True),
    sa.Column(
        "snapshot_id",
        sa.String(160),
        sa.ForeignKey("historical_dataset_snapshot.snapshot_id"),
        nullable=False,
    ),
    sa.Column("requested_security_count", sa.Integer(), nullable=False),
    sa.Column("captured_security_count", sa.Integer(), nullable=False),
    sa.Column("observation_count", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class HistoricalMarketDataPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise HistoricalMarketDataPersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresHistoricalMarketDataRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- capture plan + stable-instrument lineage -----------------------

    def plan_by_idempotency(self, key: str) -> HistoricalCapturePlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_capture_plan).where(
                        historical_capture_plan.c.idempotency_key == key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(HistoricalCapturePlan, _decoded(row))

    def save_plan(self, plan: HistoricalCapturePlan) -> HistoricalCapturePlan:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_capture_plan.c.plan_id).where(
                    historical_capture_plan.c.plan_id == plan.plan_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return plan
            for member in plan.universe:
                already = connection.execute(
                    sa.select(historical_instrument_identity.c.instrument_id).where(
                        historical_instrument_identity.c.instrument_id == member.instrument_id
                    )
                ).scalar_one_or_none()
                if already is not None:
                    continue
                connection.execute(
                    historical_instrument_identity.insert().values(
                        instrument_id=member.instrument_id,
                        stable_security_id=member.stable_security_id,
                        symbol_at_capture=member.symbol_at_capture,
                        asset_class=member.asset_class,
                        recorded_at=plan.created_at,
                        authority="NONE",
                    )
                )
            connection.execute(
                historical_capture_plan.insert().values(
                    plan_id=plan.plan_id,
                    policy_id=plan.policy.policy_id,
                    provider=plan.policy.provider,
                    start_date=plan.policy.start_date,
                    end_date=plan.policy.end_date,
                    evidence_class=plan.evidence_class.value,
                    universe_count=len(plan.universe),
                    chunk_count=len(plan.chunk_plan),
                    operator_action_id=plan.operator_action_id,
                    idempotency_key=plan.idempotency_key,
                    created_at=plan.created_at,
                    payload=_payload(plan),
                    authority="NONE",
                )
            )
        return plan

    def get_plan(self, plan_id: str) -> HistoricalCapturePlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_capture_plan).where(
                        historical_capture_plan.c.plan_id == plan_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(HistoricalCapturePlan, _decoded(row))

    def list_plans(self) -> tuple[HistoricalCapturePlan, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(historical_capture_plan).order_by(historical_capture_plan.c.created_at)
            ).mappings()
            return tuple(rehydrate(HistoricalCapturePlan, _decoded(row)) for row in rows)

    def instrument_identity(self, instrument_id: str) -> tuple[str, str, str] | None:
        """``(stable_security_id, symbol_at_capture, asset_class)`` lineage
        lookup -- the queryable projection of what
        ``historical_instrument_id`` already lets any caller recompute."""

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(
                        historical_instrument_identity.c.stable_security_id,
                        historical_instrument_identity.c.symbol_at_capture,
                        historical_instrument_identity.c.asset_class,
                    ).where(historical_instrument_identity.c.instrument_id == instrument_id)
                )
                .mappings()
                .one_or_none()
            )
        return (
            None
            if row is None
            else (
                str(row["stable_security_id"]),
                str(row["symbol_at_capture"]),
                str(row["asset_class"]),
            )
        )

    # --- capture chunks (mutable checkpoint state) -----------------------

    def list_chunks(self, plan_id: str) -> tuple[HistoricalCaptureChunk, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(historical_capture_chunk)
                .where(historical_capture_chunk.c.plan_id == plan_id)
                .order_by(historical_capture_chunk.c.ordinal)
            ).mappings()
            return tuple(rehydrate(HistoricalCaptureChunk, _decoded(row)) for row in rows)

    def save_chunk(self, chunk: HistoricalCaptureChunk) -> HistoricalCaptureChunk:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_capture_chunk.c.chunk_id).where(
                    historical_capture_chunk.c.chunk_id == chunk.chunk_id
                )
            ).scalar_one_or_none()
            values = {
                "plan_id": chunk.plan_id,
                "ordinal": chunk.ordinal,
                "state": chunk.state.value,
                "operation_id": chunk.operation_id,
                "batch_id": chunk.batch_id,
                "retry_count": chunk.retry_count,
                "started_at": chunk.started_at,
                "completed_at": chunk.completed_at,
                "payload": _payload(chunk),
                "authority": "NONE",
            }
            if existing is None:
                connection.execute(
                    historical_capture_chunk.insert().values(chunk_id=chunk.chunk_id, **values)
                )
            else:
                connection.execute(
                    historical_capture_chunk.update()
                    .where(historical_capture_chunk.c.chunk_id == chunk.chunk_id)
                    .values(**values)
                )
        return chunk

    # --- dataset snapshots (immutable, content-addressed) -----------------

    def snapshot_by_id(self, snapshot_id: str) -> HistoricalDatasetSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_dataset_snapshot).where(
                        historical_dataset_snapshot.c.snapshot_id == snapshot_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(HistoricalDatasetSnapshot, _decoded(row))

    def save_snapshot(self, snapshot: HistoricalDatasetSnapshot) -> HistoricalDatasetSnapshot:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_dataset_snapshot.c.snapshot_id).where(
                    historical_dataset_snapshot.c.snapshot_id == snapshot.snapshot_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return snapshot
            connection.execute(
                historical_dataset_snapshot.insert().values(
                    snapshot_id=snapshot.snapshot_id,
                    plan_id=snapshot.plan_id,
                    evidence_class=snapshot.evidence_class.value,
                    start_date=snapshot.start_date,
                    end_date=snapshot.end_date,
                    completeness_state=snapshot.completeness_state.value,
                    point_in_time_universe_state=snapshot.point_in_time_universe_state.value,
                    survivorship_state=snapshot.survivorship_state.value,
                    observation_count=snapshot.observation_count,
                    created_at=snapshot.created_at,
                    payload=_payload(snapshot),
                    authority="NONE",
                )
            )
        return snapshot

    def list_snapshots(self) -> tuple[HistoricalDatasetSnapshot, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(historical_dataset_snapshot).order_by(
                    historical_dataset_snapshot.c.created_at
                )
            ).mappings()
            return tuple(rehydrate(HistoricalDatasetSnapshot, _decoded(row)) for row in rows)

    # --- data-quality reports (immutable, content-addressed) --------------

    def quality_report_by_id(self, report_id: str) -> HistoricalDataQualityReport | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(historical_data_quality_report).where(
                        historical_data_quality_report.c.report_id == report_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(HistoricalDataQualityReport, _decoded(row))

    def save_quality_report(
        self, report: HistoricalDataQualityReport
    ) -> HistoricalDataQualityReport:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(historical_data_quality_report.c.report_id).where(
                    historical_data_quality_report.c.report_id == report.report_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return report
            connection.execute(
                historical_data_quality_report.insert().values(
                    report_id=report.report_id,
                    snapshot_id=report.snapshot_id,
                    requested_security_count=report.requested_security_count,
                    captured_security_count=report.captured_security_count,
                    observation_count=report.observation_count,
                    created_at=report.created_at,
                    payload=_payload(report),
                    authority="NONE",
                )
            )
        return report

    def list_quality_reports(self) -> tuple[HistoricalDataQualityReport, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(historical_data_quality_report).order_by(
                    historical_data_quality_report.c.created_at
                )
            ).mappings()
            return tuple(rehydrate(HistoricalDataQualityReport, _decoded(row)) for row in rows)
