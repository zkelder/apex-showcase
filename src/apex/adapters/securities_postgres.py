"""PostgreSQL repository for immutable Securities Stage 1 evidence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import (
    campaign,
    campaign_operating_state,
    external_securities_engine_run,
    securities_bar_evidence,
    securities_calendar_session,
    securities_corporate_action_evidence,
    securities_instrument,
    securities_market_data_batch,
    securities_research_conformance_report,
    securities_research_run,
    securities_signal,
    securities_strategy_specification,
)
from apex.application.securities import (
    SecuritiesApplicationError,
    SecuritiesCampaignBoundary,
    SecuritiesIdempotencyConflictError,
    SecuritiesRepository,
)
from apex.domain.securities import (
    ExternalSecuritiesEngineRun,
    ExternalSecuritiesEngineState,
    ExternalSecuritiesSignal,
    SecuritiesAdjustmentState,
    SecuritiesAssetClass,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
    SecuritiesConformanceClassification,
    SecuritiesCorporateActionEvidence,
    SecuritiesDataBatchState,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
    SecuritiesResearchConformanceReport,
    SecuritiesResearchMetrics,
    SecuritiesResearchRun,
    SecuritiesResearchRunState,
    SecuritiesSignal,
    SecuritiesSignalDirection,
    SecuritiesStrategyKind,
    SecuritiesStrategySpecification,
)
from apex.domain.values import canonical_json_bytes


class PostgresSecuritiesCampaignBoundary(SecuritiesCampaignBoundary):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def assert_research_allowed(self, campaign_id: str) -> None:
        with self._engine.connect() as connection:
            exists = connection.scalar(
                sa.select(sa.func.count())
                .select_from(campaign)
                .where(campaign.c.campaign_id == campaign_id)
            )
            state = connection.scalar(
                sa.select(campaign_operating_state.c.state).where(
                    campaign_operating_state.c.campaign_id == campaign_id
                )
            )
        if exists != 1 or state in {"KILLED", "EXPIRED"}:
            raise SecuritiesApplicationError("campaign is absent, killed, or not research-enabled")


class PostgresSecuritiesRepository(SecuritiesRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find_batch_by_idempotency_key(
        self, idempotency_key: str
    ) -> SecuritiesMarketDataBatch | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_market_data_batch).where(
                        securities_market_data_batch.c.idempotency_key == idempotency_key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _restore_batch(row)

    def save_dataset(
        self,
        *,
        batch: SecuritiesMarketDataBatch,
        instruments: Sequence[SecuritiesInstrument],
        sessions: Sequence[SecuritiesCalendarSession],
        bars: Sequence[SecuritiesBarEvidence],
        corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
    ) -> None:
        try:
            with self._engine.begin() as connection:
                for instrument in instruments:
                    existing = connection.execute(
                        sa.select(securities_instrument.c.content_hash).where(
                            securities_instrument.c.instrument_id == instrument.instrument_id
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        if existing != instrument.content_hash:
                            raise SecuritiesIdempotencyConflictError(
                                "instrument identity conflicts with retained evidence"
                            )
                        continue
                    connection.execute(
                        securities_instrument.insert().values(
                            instrument_id=instrument.instrument_id,
                            symbol=instrument.symbol,
                            asset_class=instrument.asset_class.value,
                            exchange_mic=instrument.exchange_mic,
                            currency=instrument.currency,
                            timezone=instrument.timezone,
                            valid_from=instrument.valid_from,
                            valid_to=instrument.valid_to,
                            source_identity=instrument.source_identity,
                            content_hash=instrument.content_hash,
                        )
                    )
                connection.execute(
                    securities_market_data_batch.insert().values(
                        batch_id=batch.batch_id,
                        dataset_name=batch.dataset_name,
                        state=batch.state.value,
                        imported_at=batch.imported_at,
                        source_artifact=batch.source_artifact,
                        source_sha256=batch.source_sha256,
                        source_byte_size=batch.source_byte_size,
                        canonical_artifact=batch.canonical_artifact,
                        canonical_sha256=batch.canonical_sha256,
                        canonical_byte_size=batch.canonical_byte_size,
                        instrument_count=batch.instrument_count,
                        session_count=batch.session_count,
                        bar_count=batch.bar_count,
                        corporate_action_count=batch.corporate_action_count,
                        first_period_start=batch.first_period_start,
                        last_period_end=batch.last_period_end,
                        idempotency_key=batch.idempotency_key,
                        warnings=list(batch.warnings),
                        canonical_hash=batch.canonical_hash,
                    )
                )
                connection.execute(
                    securities_calendar_session.insert(),
                    [
                        {
                            "batch_id": batch.batch_id,
                            "session_id": item.session_id,
                            "calendar_identity": item.calendar_identity,
                            "calendar_version": item.calendar_version,
                            "timezone": item.timezone,
                            "session_date": item.session_date,
                            "opens_at": item.opens_at,
                            "closes_at": item.closes_at,
                            "is_early_close": item.is_early_close,
                            "source_identity": item.source_identity,
                            "content_hash": item.content_hash,
                        }
                        for item in sessions
                    ],
                )
                connection.execute(
                    securities_bar_evidence.insert(),
                    [
                        {
                            "batch_id": batch.batch_id,
                            "bar_id": item.bar_id,
                            "instrument_id": item.instrument_id,
                            "interval": item.interval.value,
                            "adjustment_state": item.adjustment_state.value,
                            "period_start": item.period_start,
                            "period_end": item.period_end,
                            "available_at": item.available_at,
                            "open": item.open,
                            "high": item.high,
                            "low": item.low,
                            "close": item.close,
                            "volume": item.volume,
                            "source_identity": item.source_identity,
                            "content_hash": item.content_hash,
                        }
                        for item in bars
                    ],
                )
                if corporate_actions:
                    connection.execute(
                        securities_corporate_action_evidence.insert(),
                        [
                            {
                                "batch_id": batch.batch_id,
                                "action_id": item.action_id,
                                "instrument_id": item.instrument_id,
                                "action_type": item.action_type.value,
                                "effective_date": item.effective_date,
                                "available_at": item.available_at,
                                "split_ratio": item.split_ratio,
                                "cash_amount": item.cash_amount,
                                "successor_symbol": item.successor_symbol,
                                "source_identity": item.source_identity,
                                "content_hash": item.content_hash,
                            }
                            for item in corporate_actions
                        ],
                    )
        except SecuritiesIdempotencyConflictError:
            raise
        except sa.exc.IntegrityError as error:
            raise SecuritiesIdempotencyConflictError(
                "securities dataset conflicts with retained evidence"
            ) from error

    def get_batch(self, batch_id: str) -> SecuritiesMarketDataBatch:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_market_data_batch).where(
                        securities_market_data_batch.c.batch_id == batch_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesApplicationError("securities data batch was not found")
        return _restore_batch(row)

    def list_batches(self) -> tuple[SecuritiesMarketDataBatch, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(securities_market_data_batch).order_by(
                    securities_market_data_batch.c.imported_at
                )
            ).mappings()
            return tuple(_restore_batch(row) for row in rows)

    def list_instruments(self) -> tuple[SecuritiesInstrument, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(securities_instrument).order_by(securities_instrument.c.instrument_id)
            ).mappings()
            return tuple(_restore_instrument(row) for row in rows)

    def get_instrument(self, instrument_id: str) -> SecuritiesInstrument:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_instrument).where(
                        securities_instrument.c.instrument_id == instrument_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesApplicationError("securities instrument was not found")
        return _restore_instrument(row)

    def get_dataset(
        self, batch_id: str
    ) -> tuple[
        tuple[SecuritiesInstrument, ...],
        tuple[SecuritiesCalendarSession, ...],
        tuple[SecuritiesBarEvidence, ...],
        tuple[SecuritiesCorporateActionEvidence, ...],
    ]:
        self.get_batch(batch_id)
        with self._engine.connect() as connection:
            instrument_rows = connection.execute(
                sa.select(securities_instrument)
                .join(
                    securities_bar_evidence,
                    securities_bar_evidence.c.instrument_id
                    == securities_instrument.c.instrument_id,
                )
                .where(securities_bar_evidence.c.batch_id == batch_id)
                .distinct()
                .order_by(securities_instrument.c.instrument_id)
            ).mappings()
            session_rows = connection.execute(
                sa.select(securities_calendar_session)
                .where(securities_calendar_session.c.batch_id == batch_id)
                .order_by(securities_calendar_session.c.opens_at)
            ).mappings()
            bar_rows = connection.execute(
                sa.select(securities_bar_evidence)
                .where(securities_bar_evidence.c.batch_id == batch_id)
                .order_by(
                    securities_bar_evidence.c.instrument_id,
                    securities_bar_evidence.c.period_start,
                    securities_bar_evidence.c.adjustment_state,
                )
            ).mappings()
            action_rows = connection.execute(
                sa.select(securities_corporate_action_evidence)
                .where(securities_corporate_action_evidence.c.batch_id == batch_id)
                .order_by(
                    securities_corporate_action_evidence.c.instrument_id,
                    securities_corporate_action_evidence.c.effective_date,
                )
            ).mappings()
            return (
                tuple(_restore_instrument(row) for row in instrument_rows),
                tuple(_restore_session(row) for row in session_rows),
                tuple(_restore_bar(row) for row in bar_rows),
                tuple(_restore_action(row) for row in action_rows),
            )

    def save_specification(self, specification: SecuritiesStrategySpecification) -> None:
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(securities_strategy_specification.c.canonical_hash).where(
                    securities_strategy_specification.c.specification_id
                    == specification.specification_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing != specification.canonical_hash:
                    raise SecuritiesIdempotencyConflictError(
                        "securities specification identity conflicts"
                    )
                return
            connection.execute(
                securities_strategy_specification.insert().values(
                    specification_id=specification.specification_id,
                    strategy_id=specification.strategy_id,
                    kind=specification.kind.value,
                    version=specification.version,
                    interval=specification.interval.value,
                    adjustment_state=specification.adjustment_state.value,
                    lookback_sessions=specification.lookback_sessions,
                    rebalance_every_sessions=specification.rebalance_every_sessions,
                    top_quantile=specification.top_quantile,
                    allow_research_short=specification.allow_research_short,
                    estimated_cost_bps=specification.estimated_cost_bps,
                    strategy_lab_deployment_id=specification.strategy_lab_deployment_id,
                    created_at=specification.created_at,
                    canonical_payload=canonical_json_bytes(specification.canonical_payload).decode(
                        "utf-8"
                    ),
                    canonical_hash=specification.canonical_hash,
                )
            )

    def get_specification(self, specification_id: str) -> SecuritiesStrategySpecification:
        return self._one_specification(
            securities_strategy_specification.c.specification_id == specification_id
        )

    def get_specification_by_strategy_id(self, strategy_id: str) -> SecuritiesStrategySpecification:
        return self._one_specification(
            securities_strategy_specification.c.strategy_id == strategy_id
        )

    def list_specifications(self) -> tuple[SecuritiesStrategySpecification, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(securities_strategy_specification).order_by(
                    securities_strategy_specification.c.strategy_id
                )
            ).mappings()
            return tuple(_restore_specification(row) for row in rows)

    def _one_specification(self, condition: object) -> SecuritiesStrategySpecification:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_strategy_specification).where(cast(Any, condition))
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesApplicationError("securities strategy specification was not found")
        return _restore_specification(row)

    def find_research_run_by_idempotency_key(
        self, *, campaign_id: str, idempotency_key: str
    ) -> SecuritiesResearchRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_research_run).where(
                        securities_research_run.c.campaign_id == campaign_id,
                        securities_research_run.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _restore_research_run(row)

    def save_research_run(
        self, *, run: SecuritiesResearchRun, signals: Sequence[SecuritiesSignal]
    ) -> None:
        metrics = run.metrics
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    securities_research_run.insert().values(
                        research_run_id=run.research_run_id,
                        campaign_id=run.campaign_id,
                        specification_id=run.specification_id,
                        data_batch_id=run.data_batch_id,
                        universe=list(run.universe),
                        start_date=run.start_date,
                        end_date=run.end_date,
                        decision_cutoff=run.decision_cutoff,
                        state=run.state.value,
                        benchmark_instrument_id=run.benchmark_instrument_id,
                        session_count=metrics.session_count,
                        instrument_count=metrics.instrument_count,
                        signal_count=metrics.signal_count,
                        long_count=metrics.long_count,
                        short_count=metrics.short_count,
                        flat_count=metrics.flat_count,
                        missing_observation_count=metrics.missing_observation_count,
                        rejected_observation_count=metrics.rejected_observation_count,
                        coverage_ratio=metrics.coverage_ratio,
                        benchmark_return=metrics.benchmark_return,
                        hypothetical_gross_return=metrics.hypothetical_gross_return,
                        hypothetical_cost=metrics.hypothetical_cost,
                        hypothetical_net_return=metrics.hypothetical_net_return,
                        maximum_drawdown=metrics.maximum_drawdown,
                        turnover=metrics.turnover,
                        hit_rate=metrics.hit_rate,
                        average_gross_exposure=metrics.average_gross_exposure,
                        warnings=list(metrics.warnings),
                        idempotency_key=run.idempotency_key,
                        started_at=run.started_at,
                        completed_at=run.completed_at,
                        canonical_input_hash=run.canonical_input_hash,
                        canonical_result_hash=run.canonical_result_hash,
                    )
                )
                if signals:
                    connection.execute(
                        securities_signal.insert(),
                        [
                            {
                                "signal_id": item.signal_id,
                                "research_run_id": item.research_run_id,
                                "specification_id": item.specification_id,
                                "instrument_id": item.instrument_id,
                                "signal_at": item.signal_at,
                                "direction": item.direction.value,
                                "target_weight": item.target_weight,
                                "reference_close": item.reference_close,
                                "feature_start": item.feature_start,
                                "feature_end": item.feature_end,
                                "source_bar_ids": list(item.source_bar_ids),
                                "reason_codes": list(item.reason_codes),
                                "model_deployment_id": item.model_deployment_id,
                                "model_artifact_hash": item.model_artifact_hash,
                                "canonical_hash": item.canonical_hash,
                            }
                            for item in signals
                        ],
                    )
        except sa.exc.IntegrityError as error:
            raise SecuritiesIdempotencyConflictError(
                "securities research run conflicts with retained evidence"
            ) from error

    def get_research_run(self, research_run_id: str) -> SecuritiesResearchRun:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_research_run).where(
                        securities_research_run.c.research_run_id == research_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesApplicationError("securities research run was not found")
        return _restore_research_run(row)

    def list_research_runs(
        self, *, campaign_id: str | None = None
    ) -> tuple[SecuritiesResearchRun, ...]:
        statement = sa.select(securities_research_run)
        if campaign_id is not None:
            statement = statement.where(securities_research_run.c.campaign_id == campaign_id)
        with self._engine.connect() as connection:
            rows = connection.execute(
                statement.order_by(securities_research_run.c.completed_at)
            ).mappings()
            return tuple(_restore_research_run(row) for row in rows)

    def list_signals(self, research_run_id: str) -> tuple[SecuritiesSignal, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(securities_signal)
                .where(securities_signal.c.research_run_id == research_run_id)
                .order_by(
                    securities_signal.c.signal_at,
                    securities_signal.c.instrument_id,
                )
            ).mappings()
            return tuple(_restore_signal(row) for row in rows)

    def save_external_run(self, run: ExternalSecuritiesEngineRun) -> None:
        with self._engine.begin() as connection:
            if connection.scalar(
                sa.select(sa.func.count())
                .select_from(external_securities_engine_run)
                .where(external_securities_engine_run.c.external_run_id == run.external_run_id)
            ):
                return
            connection.execute(
                external_securities_engine_run.insert().values(
                    external_run_id=run.external_run_id,
                    research_run_id=run.research_run_id,
                    engine_identity=run.engine_identity,
                    engine_version=run.engine_version,
                    state=run.state.value,
                    request_hash=run.request_hash,
                    result_hash=run.result_hash,
                    signals_payload=canonical_json_bytes(run.signals).decode("utf-8"),
                    benchmark_return=run.benchmark_return,
                    hypothetical_gross_return=run.hypothetical_gross_return,
                    hypothetical_cost=run.hypothetical_cost,
                    warnings=list(run.warnings),
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                )
            )

    def get_external_run(self, external_run_id: str) -> ExternalSecuritiesEngineRun:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(external_securities_engine_run).where(
                        external_securities_engine_run.c.external_run_id == external_run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesApplicationError("external securities run was not found")
        return _restore_external_run(row)

    def save_conformance_report(self, report: SecuritiesResearchConformanceReport) -> None:
        with self._engine.begin() as connection:
            if connection.scalar(
                sa.select(sa.func.count())
                .select_from(securities_research_conformance_report)
                .where(securities_research_conformance_report.c.report_id == report.report_id)
            ):
                return
            connection.execute(
                securities_research_conformance_report.insert().values(
                    report_id=report.report_id,
                    research_run_id=report.research_run_id,
                    external_run_id=report.external_run_id,
                    classification=report.classification.value,
                    compared_signal_count=report.compared_signal_count,
                    matching_signal_count=report.matching_signal_count,
                    differing_signal_count=report.differing_signal_count,
                    missing_signal_count=report.missing_signal_count,
                    benchmark_difference=report.benchmark_difference,
                    gross_return_difference=report.gross_return_difference,
                    cost_difference=report.cost_difference,
                    reason_codes=list(report.reason_codes),
                    created_at=report.created_at,
                    canonical_input_hash=report.canonical_input_hash,
                    canonical_result_hash=report.canonical_result_hash,
                )
            )

    def get_conformance_report(self, report_id: str) -> SecuritiesResearchConformanceReport:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_research_conformance_report).where(
                        securities_research_conformance_report.c.report_id == report_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesApplicationError("securities conformance report was not found")
        return _restore_report(row)

    def list_conformance_reports(
        self, *, research_run_id: str | None = None
    ) -> tuple[SecuritiesResearchConformanceReport, ...]:
        statement = sa.select(securities_research_conformance_report)
        if research_run_id is not None:
            statement = statement.where(
                securities_research_conformance_report.c.research_run_id == research_run_id
            )
        with self._engine.connect() as connection:
            rows = connection.execute(
                statement.order_by(securities_research_conformance_report.c.created_at)
            ).mappings()
            return tuple(_restore_report(row) for row in rows)


def _restore_batch(row: RowMapping) -> SecuritiesMarketDataBatch:
    return SecuritiesMarketDataBatch(
        batch_id=cast(str, row["batch_id"]),
        dataset_name=cast(str, row["dataset_name"]),
        state=SecuritiesDataBatchState(cast(str, row["state"])),
        imported_at=cast(datetime, row["imported_at"]),
        source_artifact=cast(str, row["source_artifact"]),
        source_sha256=cast(str, row["source_sha256"]),
        source_byte_size=cast(int, row["source_byte_size"]),
        canonical_artifact=cast(str, row["canonical_artifact"]),
        canonical_sha256=cast(str, row["canonical_sha256"]),
        canonical_byte_size=cast(int, row["canonical_byte_size"]),
        instrument_count=cast(int, row["instrument_count"]),
        session_count=cast(int, row["session_count"]),
        bar_count=cast(int, row["bar_count"]),
        corporate_action_count=cast(int, row["corporate_action_count"]),
        first_period_start=cast(datetime, row["first_period_start"]),
        last_period_end=cast(datetime, row["last_period_end"]),
        idempotency_key=cast(str, row["idempotency_key"]),
        warnings=tuple(cast(Sequence[str], row["warnings"])),
        canonical_hash=cast(str, row["canonical_hash"]),
    )


def _restore_instrument(row: RowMapping) -> SecuritiesInstrument:
    return SecuritiesInstrument(
        instrument_id=cast(str, row["instrument_id"]),
        symbol=cast(str, row["symbol"]),
        asset_class=SecuritiesAssetClass(cast(str, row["asset_class"])),
        exchange_mic=cast(str, row["exchange_mic"]),
        currency=cast(str, row["currency"]),
        timezone=cast(str, row["timezone"]),
        valid_from=cast(str, row["valid_from"]),
        valid_to=cast(str | None, row["valid_to"]),
        source_identity=cast(str, row["source_identity"]),
        content_hash=cast(str, row["content_hash"]),
    )


def _restore_session(row: RowMapping) -> SecuritiesCalendarSession:
    return SecuritiesCalendarSession(
        session_id=cast(str, row["session_id"]),
        calendar_identity=cast(str, row["calendar_identity"]),
        calendar_version=cast(str, row["calendar_version"]),
        timezone=cast(str, row["timezone"]),
        session_date=cast(str, row["session_date"]),
        opens_at=cast(datetime, row["opens_at"]),
        closes_at=cast(datetime, row["closes_at"]),
        is_early_close=cast(bool, row["is_early_close"]),
        source_identity=cast(str, row["source_identity"]),
        content_hash=cast(str, row["content_hash"]),
    )


def _restore_bar(row: RowMapping) -> SecuritiesBarEvidence:
    return SecuritiesBarEvidence(
        bar_id=cast(str, row["bar_id"]),
        instrument_id=cast(str, row["instrument_id"]),
        interval=SecuritiesBarInterval(cast(str, row["interval"])),
        adjustment_state=SecuritiesAdjustmentState(cast(str, row["adjustment_state"])),
        period_start=cast(datetime, row["period_start"]),
        period_end=cast(datetime, row["period_end"]),
        available_at=cast(datetime, row["available_at"]),
        open=_database_decimal(row["open"]),
        high=_database_decimal(row["high"]),
        low=_database_decimal(row["low"]),
        close=_database_decimal(row["close"]),
        volume=_database_decimal(row["volume"]),
        source_identity=cast(str, row["source_identity"]),
        content_hash=cast(str, row["content_hash"]),
    )


def _restore_action(row: RowMapping) -> SecuritiesCorporateActionEvidence:
    return SecuritiesCorporateActionEvidence(
        action_id=cast(str, row["action_id"]),
        instrument_id=cast(str, row["instrument_id"]),
        action_type=cast(str, row["action_type"]),
        effective_date=cast(str, row["effective_date"]),
        available_at=cast(datetime, row["available_at"]),
        split_ratio=_optional_database_decimal(row["split_ratio"]),
        cash_amount=_optional_database_decimal(row["cash_amount"]),
        successor_symbol=cast(str | None, row["successor_symbol"]),
        source_identity=cast(str, row["source_identity"]),
        content_hash=cast(str, row["content_hash"]),
    )


def _restore_specification(row: RowMapping) -> SecuritiesStrategySpecification:
    payload = cast(dict[str, object], json.loads(cast(str, row["canonical_payload"])))
    return SecuritiesStrategySpecification(
        specification_id=cast(str, row["specification_id"]),
        strategy_id=cast(str, row["strategy_id"]),
        kind=SecuritiesStrategyKind(cast(str, row["kind"])),
        version=cast(str, row["version"]),
        interval=SecuritiesBarInterval(cast(str, row["interval"])),
        adjustment_state=SecuritiesAdjustmentState(cast(str, row["adjustment_state"])),
        lookback_sessions=cast(int, row["lookback_sessions"]),
        rebalance_every_sessions=cast(int, row["rebalance_every_sessions"]),
        top_quantile=_database_decimal(row["top_quantile"]),
        allow_research_short=cast(bool, row["allow_research_short"]),
        estimated_cost_bps=_database_decimal(row["estimated_cost_bps"]),
        strategy_lab_deployment_id=cast(str | None, row["strategy_lab_deployment_id"]),
        created_at=cast(datetime, row["created_at"]),
        canonical_payload=payload,
        canonical_hash=cast(str, row["canonical_hash"]),
    )


def _restore_research_run(row: RowMapping) -> SecuritiesResearchRun:
    metrics = SecuritiesResearchMetrics(
        session_count=cast(int, row["session_count"]),
        instrument_count=cast(int, row["instrument_count"]),
        signal_count=cast(int, row["signal_count"]),
        long_count=cast(int, row["long_count"]),
        short_count=cast(int, row["short_count"]),
        flat_count=cast(int, row["flat_count"]),
        missing_observation_count=cast(int, row["missing_observation_count"]),
        rejected_observation_count=cast(int, row["rejected_observation_count"]),
        coverage_ratio=_database_decimal(row["coverage_ratio"]),
        benchmark_return=_database_decimal(row["benchmark_return"]),
        hypothetical_gross_return=_database_decimal(row["hypothetical_gross_return"]),
        hypothetical_cost=_database_decimal(row["hypothetical_cost"]),
        hypothetical_net_return=_database_decimal(row["hypothetical_net_return"]),
        maximum_drawdown=_database_decimal(row["maximum_drawdown"]),
        turnover=_database_decimal(row["turnover"]),
        hit_rate=_database_decimal(row["hit_rate"]),
        average_gross_exposure=_database_decimal(row["average_gross_exposure"]),
        warnings=tuple(cast(Sequence[str], row["warnings"])),
    )
    return SecuritiesResearchRun(
        research_run_id=cast(str, row["research_run_id"]),
        campaign_id=cast(str, row["campaign_id"]),
        specification_id=cast(str, row["specification_id"]),
        data_batch_id=cast(str, row["data_batch_id"]),
        universe=tuple(cast(Sequence[str], row["universe"])),
        start_date=cast(str, row["start_date"]),
        end_date=cast(str, row["end_date"]),
        decision_cutoff=cast(datetime, row["decision_cutoff"]),
        state=SecuritiesResearchRunState(cast(str, row["state"])),
        benchmark_instrument_id=cast(str, row["benchmark_instrument_id"]),
        metrics=metrics,
        idempotency_key=cast(str, row["idempotency_key"]),
        started_at=cast(datetime, row["started_at"]),
        completed_at=cast(datetime, row["completed_at"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _restore_signal(row: RowMapping) -> SecuritiesSignal:
    return SecuritiesSignal(
        signal_id=cast(str, row["signal_id"]),
        research_run_id=cast(str, row["research_run_id"]),
        specification_id=cast(str, row["specification_id"]),
        instrument_id=cast(str, row["instrument_id"]),
        signal_at=cast(datetime, row["signal_at"]),
        direction=SecuritiesSignalDirection(cast(str, row["direction"])),
        target_weight=_database_decimal(row["target_weight"]),
        reference_close=_database_decimal(row["reference_close"]),
        feature_start=cast(datetime, row["feature_start"]),
        feature_end=cast(datetime, row["feature_end"]),
        source_bar_ids=tuple(cast(Sequence[str], row["source_bar_ids"])),
        reason_codes=tuple(cast(Sequence[str], row["reason_codes"])),
        model_deployment_id=cast(str | None, row["model_deployment_id"]),
        model_artifact_hash=cast(str | None, row["model_artifact_hash"]),
        canonical_hash=cast(str, row["canonical_hash"]),
    )


def _restore_external_run(row: RowMapping) -> ExternalSecuritiesEngineRun:
    payload = cast(list[dict[str, object]], json.loads(cast(str, row["signals_payload"])))
    signals = tuple(
        ExternalSecuritiesSignal(
            instrument_id=cast(str, item["instrument_id"]),
            signal_at=datetime.fromisoformat(cast(str, item["signal_at"]).replace("Z", "+00:00")),
            direction=SecuritiesSignalDirection(cast(str, item["direction"])),
            target_weight=Decimal(cast(str, item["target_weight"])),
        )
        for item in payload
    )
    return ExternalSecuritiesEngineRun(
        external_run_id=cast(str, row["external_run_id"]),
        research_run_id=cast(str, row["research_run_id"]),
        engine_identity=cast(str, row["engine_identity"]),
        engine_version=cast(str, row["engine_version"]),
        state=ExternalSecuritiesEngineState(cast(str, row["state"])),
        request_hash=cast(str, row["request_hash"]),
        result_hash=cast(str | None, row["result_hash"]),
        signals=signals,
        benchmark_return=_optional_database_decimal(row["benchmark_return"]),
        hypothetical_gross_return=_optional_database_decimal(row["hypothetical_gross_return"]),
        hypothetical_cost=_optional_database_decimal(row["hypothetical_cost"]),
        warnings=tuple(cast(Sequence[str], row["warnings"])),
        started_at=cast(datetime, row["started_at"]),
        completed_at=cast(datetime, row["completed_at"]),
    )


def _restore_report(row: RowMapping) -> SecuritiesResearchConformanceReport:
    return SecuritiesResearchConformanceReport(
        report_id=cast(str, row["report_id"]),
        research_run_id=cast(str, row["research_run_id"]),
        external_run_id=cast(str, row["external_run_id"]),
        classification=SecuritiesConformanceClassification(cast(str, row["classification"])),
        compared_signal_count=cast(int, row["compared_signal_count"]),
        matching_signal_count=cast(int, row["matching_signal_count"]),
        differing_signal_count=cast(int, row["differing_signal_count"]),
        missing_signal_count=cast(int, row["missing_signal_count"]),
        benchmark_difference=_optional_database_decimal(row["benchmark_difference"]),
        gross_return_difference=_optional_database_decimal(row["gross_return_difference"]),
        cost_difference=_optional_database_decimal(row["cost_difference"]),
        reason_codes=tuple(cast(Sequence[str], row["reason_codes"])),
        created_at=cast(datetime, row["created_at"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _database_decimal(value: object) -> Decimal:
    decimal_value = cast(Decimal, value)
    return Decimal(0) if decimal_value == 0 else decimal_value.normalize()


def _optional_database_decimal(value: object) -> Decimal | None:
    return None if value is None else _database_decimal(value)
