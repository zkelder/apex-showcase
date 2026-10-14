"""PostgreSQL repository for the genuine multi-asset cohort quality gate and
the cross-market dataset binding (migration ``0080``).

Mirrors the ``payload``-column pattern ``adapters.historical_market_data_
postgres`` already established: a handful of indexed top-level columns for
querying, and the full canonical dataclass retained as JSON so ``rehydrate``
restores an exact original instance.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.cross_market_dataset import CrossMarketDataset
from apex.domain.genuine_data_quality_gate import GenuineCohortQualityGateReport
from apex.domain.values import canonical_json_bytes

GENUINE_MULTI_ASSET_HISTORY_TABLES = (
    "genuine_cohort_quality_gate_report",
    "cross_market_dataset",
)
GENUINE_MULTI_ASSET_HISTORY_IMMUTABLE_TABLES = GENUINE_MULTI_ASSET_HISTORY_TABLES

metadata = sa.MetaData()

genuine_cohort_quality_gate_report = sa.Table(
    "genuine_cohort_quality_gate_report",
    metadata,
    sa.Column("report_id", sa.String(160), primary_key=True),
    sa.Column(
        "snapshot_id",
        sa.String(160),
        sa.ForeignKey("historical_dataset_snapshot.snapshot_id"),
        nullable=False,
    ),
    sa.Column("cohort_policy_id", sa.String(160), nullable=False),
    sa.Column("requested_instrument_count", sa.Integer(), nullable=False),
    sa.Column("successful_instrument_count", sa.Integer(), nullable=False),
    sa.Column("usable_count", sa.Integer(), nullable=False),
    sa.Column("usable_with_warnings_count", sa.Integer(), nullable=False),
    sa.Column("insufficient_count", sa.Integer(), nullable=False),
    sa.Column("rejected_count", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

cross_market_dataset = sa.Table(
    "cross_market_dataset",
    metadata,
    sa.Column("cross_market_dataset_id", sa.String(160), primary_key=True),
    sa.Column(
        "equity_dataset_snapshot_id",
        sa.String(160),
        sa.ForeignKey("historical_dataset_snapshot.snapshot_id"),
        nullable=False,
    ),
    sa.Column("cohort_policy_id", sa.String(160), nullable=False),
    sa.Column(
        "quality_gate_report_id",
        sa.String(160),
        sa.ForeignKey("genuine_cohort_quality_gate_report.report_id"),
        nullable=False,
    ),
    sa.Column("crypto_provider", sa.String(40), nullable=False),
    sa.Column("crypto_product_id", sa.String(40), nullable=False),
    sa.Column("equity_coverage_start", sa.String(10), nullable=False),
    sa.Column("equity_coverage_end", sa.String(10), nullable=False),
    sa.Column("crypto_coverage_start", sa.String(10), nullable=False),
    sa.Column("crypto_coverage_end", sa.String(10), nullable=False),
    sa.Column("common_overlap_start", sa.String(10), nullable=True),
    sa.Column("common_overlap_end", sa.String(10), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class GenuineMultiAssetHistoryPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise GenuineMultiAssetHistoryPersistenceError("STORED_PAYLOAD_INVALID")
    return value


class PostgresGenuineMultiAssetHistoryRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- per-instrument quality gate report (immutable) --------------------

    def quality_gate_report_by_id(self, report_id: str) -> GenuineCohortQualityGateReport | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(genuine_cohort_quality_gate_report).where(
                        genuine_cohort_quality_gate_report.c.report_id == report_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(GenuineCohortQualityGateReport, _decoded(row))

    def save_quality_gate_report(
        self, report: GenuineCohortQualityGateReport
    ) -> GenuineCohortQualityGateReport:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(genuine_cohort_quality_gate_report.c.report_id).where(
                    genuine_cohort_quality_gate_report.c.report_id == report.report_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return report
            connection.execute(
                genuine_cohort_quality_gate_report.insert().values(
                    report_id=report.report_id,
                    snapshot_id=report.snapshot_id,
                    cohort_policy_id=report.cohort_policy_id,
                    requested_instrument_count=report.requested_instrument_count,
                    successful_instrument_count=report.successful_instrument_count,
                    usable_count=report.usable_count,
                    usable_with_warnings_count=report.usable_with_warnings_count,
                    insufficient_count=report.insufficient_count,
                    rejected_count=report.rejected_count,
                    created_at=report.created_at,
                    payload=_payload(report),
                    authority="NONE",
                )
            )
        return report

    def list_quality_gate_reports(self) -> tuple[GenuineCohortQualityGateReport, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(genuine_cohort_quality_gate_report).order_by(
                    genuine_cohort_quality_gate_report.c.created_at
                )
            ).mappings()
            return tuple(rehydrate(GenuineCohortQualityGateReport, _decoded(row)) for row in rows)

    # --- cross-market dataset binding (immutable) ---------------------------

    def cross_market_dataset_by_id(self, dataset_id: str) -> CrossMarketDataset | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(cross_market_dataset).where(
                        cross_market_dataset.c.cross_market_dataset_id == dataset_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else rehydrate(CrossMarketDataset, _decoded(row))

    def save_cross_market_dataset(self, dataset: CrossMarketDataset) -> CrossMarketDataset:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(cross_market_dataset.c.cross_market_dataset_id).where(
                    cross_market_dataset.c.cross_market_dataset_id
                    == dataset.cross_market_dataset_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return dataset
            connection.execute(
                cross_market_dataset.insert().values(
                    cross_market_dataset_id=dataset.cross_market_dataset_id,
                    equity_dataset_snapshot_id=dataset.equity_dataset_snapshot_id,
                    cohort_policy_id=dataset.cohort_policy_id,
                    quality_gate_report_id=dataset.quality_gate_report_id,
                    crypto_provider=dataset.crypto_provider,
                    crypto_product_id=dataset.crypto_product_id,
                    equity_coverage_start=dataset.equity_coverage_start,
                    equity_coverage_end=dataset.equity_coverage_end,
                    crypto_coverage_start=dataset.crypto_coverage_start,
                    crypto_coverage_end=dataset.crypto_coverage_end,
                    common_overlap_start=dataset.common_overlap_start,
                    common_overlap_end=dataset.common_overlap_end,
                    created_at=dataset.created_at,
                    payload=_payload(dataset),
                    authority="NONE",
                )
            )
        return dataset

    def list_cross_market_datasets(self) -> tuple[CrossMarketDataset, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(cross_market_dataset).order_by(cross_market_dataset.c.created_at)
            ).mappings()
            return tuple(rehydrate(CrossMarketDataset, _decoded(row)) for row in rows)
