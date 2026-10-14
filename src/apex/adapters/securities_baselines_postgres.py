"""PostgreSQL repository for immutable canonical Securities baseline evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import (
    canonical_securities_baseline,
    securities_baseline_scorecard,
    securities_simulation_run,
    securities_simulator_conformance_report,
    strategy_lab_baseline_comparison,
)
from apex.application.securities_baselines import (
    SecuritiesBaselineApplicationError,
    SecuritiesBaselineIdempotencyConflict,
    SecuritiesBaselineRepository,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarInterval,
    SecuritiesSignalDirection,
)
from apex.domain.securities_baselines import (
    BaselineEquityCheckpoint,
    BaselineSignalEvidence,
    BaselineTransactionEvidence,
    CanonicalBaselineFamily,
    CanonicalBaselineState,
    CanonicalSecuritiesBaseline,
    CanonicalSecuritiesSimulationRequest,
    MultiSimulatorConformanceClassification,
    PaperCompatibilityState,
    SecuritiesBaselineScorecard,
    SecuritiesBenchmarkIdentity,
    SecuritiesSimulationMode,
    SecuritiesSimulationResult,
    SecuritiesSimulationRun,
    SecuritiesSimulationState,
    SecuritiesSimulatorConformanceReport,
    SecuritiesSimulatorIdentity,
    StrategyLabBaselineComparison,
)
from apex.domain.values import canonical_json_bytes


class PostgresSecuritiesBaselineRepository(SecuritiesBaselineRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save_baseline(self, baseline: CanonicalSecuritiesBaseline) -> None:
        values = {
            "baseline_id": baseline.baseline_id,
            "family": baseline.family.value,
            "specification_version": baseline.specification_version,
            "display_name": baseline.display_name,
            "research_description": baseline.research_description,
            "source_reference_identity": baseline.source_reference_identity,
            "exact_feature_rules": list(baseline.exact_feature_rules),
            "exact_decision_rules": list(baseline.exact_decision_rules),
            "universe_requirements": list(baseline.universe_requirements),
            "required_interval": baseline.required_interval.value,
            "required_adjustment_state": baseline.required_adjustment_state.value,
            "rebalance_policy": baseline.rebalance_policy,
            "holding_policy": baseline.holding_policy,
            "analytical_short_behavior": baseline.analytical_short_behavior,
            "paper_compatibility_state": baseline.paper_compatibility_state.value,
            "benchmark_identity": baseline.benchmark_identity.value,
            "state": baseline.state.value,
            "canonical_payload": canonical_json_bytes(baseline.canonical_payload).decode(),
            "canonical_hash": baseline.canonical_hash,
            "created_at": baseline.created_at,
            "superseded_at": baseline.superseded_at,
        }
        with self._engine.begin() as connection:
            existing = connection.execute(
                sa.select(
                    canonical_securities_baseline.c.canonical_hash,
                ).where(canonical_securities_baseline.c.baseline_id == baseline.baseline_id)
            ).scalar_one_or_none()
            if existing is not None:
                if existing != baseline.canonical_hash:
                    raise SecuritiesBaselineIdempotencyConflict(
                        "canonical baseline conflicts with retained specification"
                    )
                return
            connection.execute(canonical_securities_baseline.insert().values(**values))

    def baseline(self, baseline_id: str) -> CanonicalSecuritiesBaseline:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(canonical_securities_baseline).where(
                        canonical_securities_baseline.c.baseline_id == baseline_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise SecuritiesBaselineApplicationError("canonical baseline was not found")
        return _restore_baseline(row)

    def baselines(self) -> tuple[CanonicalSecuritiesBaseline, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(canonical_securities_baseline).order_by(
                        canonical_securities_baseline.c.family
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_restore_baseline(row) for row in rows)

    def find_run_by_idempotency_key(self, idempotency_key: str) -> SecuritiesSimulationRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(securities_simulation_run).where(
                        securities_simulation_run.c.idempotency_key == idempotency_key
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _restore_run(row)

    def save_run(
        self,
        *,
        request: CanonicalSecuritiesSimulationRequest,
        run: SecuritiesSimulationRun,
    ) -> None:
        if request.canonical_hash != run.canonical_input_hash:
            raise SecuritiesBaselineIdempotencyConflict(
                "simulation request differs from retained run binding"
            )
        existing = self.find_run_by_idempotency_key(run.idempotency_key)
        if existing is not None:
            if (
                existing.canonical_input_hash != run.canonical_input_hash
                or existing.canonical_result_hash != run.canonical_result_hash
                or existing.simulator_identity is not run.simulator_identity
            ):
                raise SecuritiesBaselineIdempotencyConflict(
                    "simulation idempotency key conflicts with retained evidence"
                )
            return
        values = {
            "simulation_run_id": run.simulation_run_id,
            "simulator_identity": run.simulator_identity.value,
            "simulator_version": run.simulator_version,
            "adapter_version": run.adapter_version,
            "mode": run.mode.value,
            "baseline_id": run.baseline_id,
            "baseline_hash": run.baseline_hash,
            "data_batch_id": run.data_batch_id,
            "data_batch_hash": run.data_batch_hash,
            "universe": list(request.universe),
            "universe_hash": run.universe_hash,
            "start_date": run.start_date,
            "end_date": run.end_date,
            "starting_capital": run.starting_capital,
            "execution_policy_hash": run.execution_policy_hash,
            "spread_bps": request.spread_bps,
            "slippage_bps": request.slippage_bps,
            "commission_bps": request.commission_bps,
            "state": run.state.value,
            "signal_count": run.signal_count,
            "order_count": run.order_count,
            "fill_count": run.fill_count,
            "position_count": run.position_count,
            "gross_return": run.gross_return,
            "net_return": run.net_return,
            "maximum_drawdown": run.maximum_drawdown,
            "turnover": run.turnover,
            "managed_result_artifact": run.managed_result_artifact,
            "managed_result_hash": run.managed_result_hash,
            "started_at": run.started_at,
            "completed_at": run.completed_at,
            "failure_code": run.failure_code,
            "idempotency_key": run.idempotency_key,
            "canonical_request_payload": canonical_json_bytes(request).decode(),
            "canonical_result_payload": canonical_json_bytes(run.result).decode(),
            "canonical_input_hash": run.canonical_input_hash,
            "canonical_result_hash": run.canonical_result_hash,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(securities_simulation_run.insert().values(**values))
        except sa.exc.IntegrityError as error:
            raise SecuritiesBaselineIdempotencyConflict(
                "simulation run conflicts with retained evidence"
            ) from error

    def runs(self) -> tuple[SecuritiesSimulationRun, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(securities_simulation_run).order_by(
                        securities_simulation_run.c.started_at,
                        securities_simulation_run.c.simulation_run_id,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_restore_run(row) for row in rows)

    def save_report(self, report: SecuritiesSimulatorConformanceReport) -> None:
        values = {
            "report_id": report.report_id,
            "baseline_id": report.baseline_id,
            "baseline_hash": report.baseline_hash,
            "data_batch_hash": report.data_batch_hash,
            "universe_hash": report.universe_hash,
            "date_scope_hash": report.date_scope_hash,
            "reference_run_id": report.reference_run_id,
            "comparison_run_id": report.comparison_run_id,
            "comparison_mode": report.comparison_mode.value,
            "signal_agreement": report.signal_agreement,
            "decision_time_disagreements": report.decision_time_disagreements,
            "direction_disagreements": report.direction_disagreements,
            "target_weight_disagreements": report.target_weight_disagreements,
            "order_state_disagreements": report.transaction_disagreements,
            "fill_time_disagreements": report.fill_time_disagreements,
            "fill_price_disagreements": report.fill_price_disagreements,
            "corporate_action_disagreements": report.corporate_action_disagreements,
            "position_disagreements": report.position_disagreements,
            "cash_difference": report.cash_difference,
            "realized_pnl_difference": report.realized_pnl_difference,
            "unrealized_pnl_difference": report.unrealized_pnl_difference,
            "return_difference": report.return_difference,
            "drawdown_difference": report.drawdown_difference,
            "turnover_difference": report.turnover_difference,
            "materiality_policy_id": report.materiality_policy_id,
            "materiality_policy_hash": report.materiality_policy_hash,
            "classification": report.classification.value,
            "reason_codes": list(report.reason_codes),
            "canonical_input_hash": report.canonical_input_hash,
            "canonical_result_hash": report.canonical_result_hash,
        }
        self._save_by_hash(
            table=securities_simulator_conformance_report,
            identity_column=securities_simulator_conformance_report.c.report_id,
            identity=report.report_id,
            hash_column=securities_simulator_conformance_report.c.canonical_result_hash,
            expected_hash=report.canonical_result_hash,
            values=values,
        )

    def reports(self) -> tuple[SecuritiesSimulatorConformanceReport, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(securities_simulator_conformance_report).order_by(
                        securities_simulator_conformance_report.c.report_id
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_restore_report(row) for row in rows)

    def conformance_detail(self, report_id: str) -> dict[str, object]:
        """Load exact request and three-layer output evidence for one report."""

        with self._engine.connect() as connection:
            report_row = (
                connection.execute(
                    sa.select(securities_simulator_conformance_report).where(
                        securities_simulator_conformance_report.c.report_id == report_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if report_row is None:
                raise SecuritiesBaselineApplicationError(
                    "simulator conformance report was not found"
                )
            run_rows = (
                connection.execute(
                    sa.select(securities_simulation_run).where(
                        securities_simulation_run.c.simulation_run_id.in_(
                            (
                                report_row["reference_run_id"],
                                report_row["comparison_run_id"],
                            )
                        )
                    )
                )
                .mappings()
                .all()
            )
        runs = {
            cast(str, row["simulation_run_id"]): {
                "request": json.loads(cast(str, row["canonical_request_payload"])),
                "result": json.loads(cast(str, row["canonical_result_payload"])),
            }
            for row in run_rows
        }
        reference_run_id = cast(str, report_row["reference_run_id"])
        comparison_run_id = cast(str, report_row["comparison_run_id"])
        return {
            "report": _restore_report(report_row),
            "reference": runs[reference_run_id],
            "comparison": runs[comparison_run_id],
        }

    def retained_request(
        self, report_id: str
    ) -> tuple[SecuritiesSimulatorConformanceReport, CanonicalSecuritiesSimulationRequest]:
        """Restore and verify one immutable canonical request payload."""
        detail = self.conformance_detail(report_id)
        report = cast(SecuritiesSimulatorConformanceReport, detail["report"])
        reference = cast(dict[str, object], detail["reference"])
        payload = cast(dict[str, object], reference["request"])
        request = CanonicalSecuritiesSimulationRequest.create(
            baseline=self.baseline(report.baseline_id),
            data_batch_id=cast(str, payload["data_batch_id"]),
            data_batch_hash=cast(str, payload["data_batch_hash"]),
            universe=tuple(cast(list[str], payload["universe"])),
            start_date=cast(str, payload["start_date"]),
            end_date=cast(str, payload["end_date"]),
            cutoff=datetime.fromisoformat(cast(str, payload["cutoff"])),
            starting_capital=cast(str, payload["starting_capital"]),
            execution_policy_hash=cast(str | None, payload["execution_policy_hash"]),
            spread_bps=cast(str, payload["spread_bps"]),
            slippage_bps=cast(str, payload["slippage_bps"]),
            commission_bps=cast(str, payload["commission_bps"]),
        )
        if request.canonical_hash != cast(str, payload["canonical_hash"]):
            raise SecuritiesBaselineApplicationError("RETAINED_REQUEST_HASH_DIVERGENCE")
        return report, request

    def save_scorecard(self, scorecard: SecuritiesBaselineScorecard) -> None:
        values = {
            "scorecard_id": scorecard.scorecard_id,
            "baseline_id": scorecard.baseline_id,
            "baseline_hash": scorecard.baseline_hash,
            "data_batch_id": scorecard.data_batch_id,
            "data_batch_hash": scorecard.data_batch_hash,
            "universe_hash": scorecard.universe_hash,
            "start_date": scorecard.start_date,
            "end_date": scorecard.end_date,
            "simulator_identity": scorecard.simulator_identity.value,
            "benchmark_identity": scorecard.benchmark_identity.value,
            "signal_count": scorecard.signal_count,
            "trade_count": scorecard.trade_count,
            "gross_return": scorecard.gross_return,
            "net_return": scorecard.net_return,
            "annualized_return": scorecard.annualized_return,
            "volatility": scorecard.volatility,
            "maximum_drawdown": scorecard.maximum_drawdown,
            "turnover": scorecard.turnover,
            "hit_rate": scorecard.hit_rate,
            "average_holding_period": scorecard.average_holding_period,
            "exposure": scorecard.exposure,
            "cost_total": scorecard.cost_total,
            "corporate_action_count": scorecard.corporate_action_count,
            "conformance_state": scorecard.conformance_state.value,
            "integrity_warnings": list(scorecard.integrity_warnings),
            "canonical_input_hash": scorecard.canonical_input_hash,
            "canonical_result_hash": scorecard.canonical_result_hash,
        }
        self._save_by_hash(
            table=securities_baseline_scorecard,
            identity_column=securities_baseline_scorecard.c.scorecard_id,
            identity=scorecard.scorecard_id,
            hash_column=securities_baseline_scorecard.c.canonical_result_hash,
            expected_hash=scorecard.canonical_result_hash,
            values=values,
        )

    def scorecards(self) -> tuple[SecuritiesBaselineScorecard, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    sa.select(securities_baseline_scorecard).order_by(
                        securities_baseline_scorecard.c.baseline_id,
                        securities_baseline_scorecard.c.simulator_identity,
                    )
                )
                .mappings()
                .all()
            )
        return tuple(_restore_scorecard(row) for row in rows)

    def save_strategy_lab_comparison(self, comparison: StrategyLabBaselineComparison) -> None:
        values = {
            "comparison_id": comparison.comparison_id,
            "candidate_id": comparison.candidate_id,
            "candidate_result_hash": comparison.candidate_result_hash,
            "baseline_scorecard_id": comparison.baseline_scorecard_id,
            "baseline_id": comparison.baseline_id,
            "baseline_hash": comparison.baseline_hash,
            "data_batch_hash": comparison.data_batch_hash,
            "universe_hash": comparison.universe_hash,
            "date_scope_hash": comparison.date_scope_hash,
            "aligned": comparison.aligned,
            "integrity_blocked": comparison.integrity_blocked,
            "candidate_net_return": comparison.candidate_net_return,
            "baseline_net_return": comparison.baseline_net_return,
            "net_return_difference": comparison.net_return_difference,
            "reason_codes": list(comparison.reason_codes),
            "canonical_hash": comparison.canonical_hash,
        }
        self._save_by_hash(
            table=strategy_lab_baseline_comparison,
            identity_column=strategy_lab_baseline_comparison.c.comparison_id,
            identity=comparison.comparison_id,
            hash_column=strategy_lab_baseline_comparison.c.canonical_hash,
            expected_hash=comparison.canonical_hash,
            values=values,
        )

    def _save_by_hash(
        self,
        *,
        table: sa.Table,
        identity_column: sa.Column[Any],
        identity: str,
        hash_column: sa.Column[Any],
        expected_hash: str,
        values: Mapping[str, object],
    ) -> None:
        with self._engine.begin() as connection:
            existing = connection.scalar(sa.select(hash_column).where(identity_column == identity))
            if existing is not None:
                if existing != expected_hash:
                    raise SecuritiesBaselineIdempotencyConflict(
                        "immutable baseline evidence conflicts"
                    )
                return
            connection.execute(table.insert().values(**values))


def _restore_baseline(row: RowMapping) -> CanonicalSecuritiesBaseline:
    family = CanonicalBaselineFamily(cast(str, row["family"]))
    feature_rules = tuple(cast(list[str], row["exact_feature_rules"]))
    decision_rules = tuple(cast(list[str], row["exact_decision_rules"]))
    universe_requirements = tuple(cast(list[str], row["universe_requirements"]))
    required_interval = SecuritiesBarInterval(cast(str, row["required_interval"]))
    required_adjustment_state = SecuritiesAdjustmentState(
        cast(str, row["required_adjustment_state"])
    )
    paper_compatibility_state = PaperCompatibilityState(cast(str, row["paper_compatibility_state"]))
    benchmark_identity = SecuritiesBenchmarkIdentity(cast(str, row["benchmark_identity"]))
    canonical_payload: dict[str, object] = {
        "family": family,
        "specification_version": cast(str, row["specification_version"]),
        "display_name": cast(str, row["display_name"]),
        "research_description": cast(str, row["research_description"]),
        "source_reference_identity": cast(str, row["source_reference_identity"]),
        "exact_feature_rules": feature_rules,
        "exact_decision_rules": decision_rules,
        "universe_requirements": universe_requirements,
        "required_interval": required_interval,
        "required_adjustment_state": required_adjustment_state,
        "rebalance_policy": cast(str, row["rebalance_policy"]),
        "holding_policy": cast(str, row["holding_policy"]),
        "analytical_short_behavior": cast(str, row["analytical_short_behavior"]),
        "paper_compatibility_state": paper_compatibility_state,
        "benchmark_identity": benchmark_identity,
    }
    return CanonicalSecuritiesBaseline(
        baseline_id=cast(str, row["baseline_id"]),
        family=family,
        specification_version=cast(str, row["specification_version"]),
        display_name=cast(str, row["display_name"]),
        research_description=cast(str, row["research_description"]),
        source_reference_identity=cast(str, row["source_reference_identity"]),
        exact_feature_rules=feature_rules,
        exact_decision_rules=decision_rules,
        universe_requirements=universe_requirements,
        required_interval=required_interval,
        required_adjustment_state=required_adjustment_state,
        rebalance_policy=cast(str, row["rebalance_policy"]),
        holding_policy=cast(str, row["holding_policy"]),
        analytical_short_behavior=cast(str, row["analytical_short_behavior"]),
        paper_compatibility_state=paper_compatibility_state,
        benchmark_identity=benchmark_identity,
        state=CanonicalBaselineState(cast(str, row["state"])),
        canonical_payload=canonical_payload,
        canonical_hash=cast(str, row["canonical_hash"]),
        created_at=cast(datetime, row["created_at"]),
        superseded_at=cast(datetime | None, row["superseded_at"]),
    )


def _restore_run(row: RowMapping) -> SecuritiesSimulationRun:
    result = _restore_result(
        cast(dict[str, object], json.loads(cast(str, row["canonical_result_payload"])))
    )
    return SecuritiesSimulationRun(
        simulation_run_id=cast(str, row["simulation_run_id"]),
        simulator_identity=SecuritiesSimulatorIdentity(cast(str, row["simulator_identity"])),
        simulator_version=cast(str, row["simulator_version"]),
        adapter_version=cast(str, row["adapter_version"]),
        mode=SecuritiesSimulationMode(cast(str, row["mode"])),
        baseline_id=cast(str, row["baseline_id"]),
        baseline_hash=cast(str, row["baseline_hash"]),
        data_batch_id=cast(str, row["data_batch_id"]),
        data_batch_hash=cast(str, row["data_batch_hash"]),
        universe_hash=cast(str, row["universe_hash"]),
        start_date=cast(str, row["start_date"]),
        end_date=cast(str, row["end_date"]),
        starting_capital=cast(Decimal, row["starting_capital"]),
        execution_policy_hash=cast(str | None, row["execution_policy_hash"]),
        state=SecuritiesSimulationState(cast(str, row["state"])),
        signal_count=cast(int, row["signal_count"]),
        order_count=cast(int, row["order_count"]),
        fill_count=cast(int, row["fill_count"]),
        position_count=cast(int, row["position_count"]),
        gross_return=cast(Decimal | None, row["gross_return"]),
        net_return=cast(Decimal | None, row["net_return"]),
        maximum_drawdown=cast(Decimal | None, row["maximum_drawdown"]),
        turnover=cast(Decimal | None, row["turnover"]),
        managed_result_artifact=cast(str | None, row["managed_result_artifact"]),
        managed_result_hash=cast(str | None, row["managed_result_hash"]),
        started_at=cast(datetime, row["started_at"]),
        completed_at=cast(datetime, row["completed_at"]),
        failure_code=cast(str | None, row["failure_code"]),
        idempotency_key=cast(str, row["idempotency_key"]),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str | None, row["canonical_result_hash"]),
        result=result,
    )


def _restore_result(payload: dict[str, object]) -> SecuritiesSimulationResult:
    signals = tuple(
        BaselineSignalEvidence(
            instrument_id=cast(str, item["instrument_id"]),
            decision_at=datetime.fromisoformat(cast(str, item["decision_at"])),
            direction=SecuritiesSignalDirection(cast(str, item["direction"])),
            score=Decimal(cast(str, item["score"])),
            target_weight=Decimal(cast(str, item["target_weight"])),
            source_bar_ids=tuple(cast(list[str], item["source_bar_ids"])),
            reason_codes=tuple(cast(list[str], item["reason_codes"])),
            canonical_hash=cast(str, item["canonical_hash"]),
        )
        for item in _object_rows(payload["signals"])
    )
    transactions = tuple(
        BaselineTransactionEvidence(
            instrument_id=cast(str, item["instrument_id"]),
            decision_at=datetime.fromisoformat(cast(str, item["decision_at"])),
            transaction_at=datetime.fromisoformat(cast(str, item["transaction_at"])),
            prior_weight=Decimal(cast(str, item["prior_weight"])),
            target_weight=Decimal(cast(str, item["target_weight"])),
            modeled_cost=Decimal(cast(str, item["modeled_cost"])),
            execution_price=Decimal(cast(str, item["execution_price"])),
            canonical_hash=cast(str, item["canonical_hash"]),
        )
        for item in _object_rows(payload["transactions"])
    )
    equity_curve = tuple(
        BaselineEquityCheckpoint(
            session_at=datetime.fromisoformat(cast(str, item["session_at"])),
            gross_equity=Decimal(cast(str, item["gross_equity"])),
            net_equity=Decimal(cast(str, item["net_equity"])),
            cash_balance=Decimal(cast(str, item["cash_balance"])),
            gross_exposure=Decimal(cast(str, item["gross_exposure"])),
            drawdown=Decimal(cast(str, item["drawdown"])),
            canonical_hash=cast(str, item["canonical_hash"]),
        )
        for item in _object_rows(payload["equity_curve"])
    )
    return SecuritiesSimulationResult(
        simulator_identity=SecuritiesSimulatorIdentity(cast(str, payload["simulator_identity"])),
        simulator_version=cast(str, payload["simulator_version"]),
        adapter_version=cast(str, payload["adapter_version"]),
        mode=SecuritiesSimulationMode(cast(str, payload["mode"])),
        request_hash=cast(str, payload["request_hash"]),
        state=SecuritiesSimulationState(cast(str, payload["state"])),
        signals=signals,
        transactions=transactions,
        equity_curve=equity_curve,
        gross_return=_decimal_or_none(payload["gross_return"]),
        net_return=_decimal_or_none(payload["net_return"]),
        annualized_return=_decimal_or_none(payload["annualized_return"]),
        volatility=_decimal_or_none(payload["volatility"]),
        maximum_drawdown=_decimal_or_none(payload["maximum_drawdown"]),
        turnover=_decimal_or_none(payload["turnover"]),
        hit_rate=_decimal_or_none(payload["hit_rate"]),
        average_holding_period=_decimal_or_none(payload["average_holding_period"]),
        average_exposure=_decimal_or_none(payload["average_exposure"]),
        cost_total=_decimal_or_none(payload["cost_total"]),
        corporate_action_count=cast(int, payload["corporate_action_count"]),
        position_count=cast(int, payload["position_count"]),
        reason_codes=tuple(cast(list[str], payload["reason_codes"])),
        managed_result_artifact=cast(str | None, payload["managed_result_artifact"]),
        managed_result_hash=cast(str | None, payload["managed_result_hash"]),
        canonical_result_hash=cast(str | None, payload["canonical_result_hash"]),
    )


def _restore_report(row: RowMapping) -> SecuritiesSimulatorConformanceReport:
    return SecuritiesSimulatorConformanceReport(
        report_id=cast(str, row["report_id"]),
        baseline_id=cast(str, row["baseline_id"]),
        baseline_hash=cast(str, row["baseline_hash"]),
        data_batch_hash=cast(str, row["data_batch_hash"]),
        universe_hash=cast(str, row["universe_hash"]),
        date_scope_hash=cast(str, row["date_scope_hash"]),
        reference_run_id=cast(str, row["reference_run_id"]),
        comparison_run_id=cast(str, row["comparison_run_id"]),
        comparison_mode=SecuritiesSimulationMode(cast(str, row["comparison_mode"])),
        signal_agreement=cast(Decimal, row["signal_agreement"]),
        decision_time_disagreements=cast(int, row["decision_time_disagreements"]),
        direction_disagreements=cast(int, row["direction_disagreements"]),
        target_weight_disagreements=cast(int, row["target_weight_disagreements"]),
        transaction_disagreements=cast(int, row["order_state_disagreements"]),
        fill_time_disagreements=cast(int, row["fill_time_disagreements"]),
        fill_price_disagreements=cast(int, row["fill_price_disagreements"]),
        corporate_action_disagreements=cast(int, row["corporate_action_disagreements"]),
        position_disagreements=cast(int, row["position_disagreements"]),
        cash_difference=cast(Decimal | None, row["cash_difference"]),
        realized_pnl_difference=cast(Decimal | None, row["realized_pnl_difference"]),
        unrealized_pnl_difference=cast(Decimal | None, row["unrealized_pnl_difference"]),
        return_difference=cast(Decimal | None, row["return_difference"]),
        drawdown_difference=cast(Decimal | None, row["drawdown_difference"]),
        turnover_difference=cast(Decimal | None, row["turnover_difference"]),
        materiality_policy_id=cast(str, row["materiality_policy_id"]),
        materiality_policy_hash=cast(str, row["materiality_policy_hash"]),
        classification=MultiSimulatorConformanceClassification(cast(str, row["classification"])),
        reason_codes=tuple(cast(list[str], row["reason_codes"])),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _restore_scorecard(row: RowMapping) -> SecuritiesBaselineScorecard:
    return SecuritiesBaselineScorecard(
        scorecard_id=cast(str, row["scorecard_id"]),
        baseline_id=cast(str, row["baseline_id"]),
        baseline_hash=cast(str, row["baseline_hash"]),
        data_batch_id=cast(str, row["data_batch_id"]),
        data_batch_hash=cast(str, row["data_batch_hash"]),
        universe_hash=cast(str, row["universe_hash"]),
        start_date=cast(str, row["start_date"]),
        end_date=cast(str, row["end_date"]),
        simulator_identity=SecuritiesSimulatorIdentity(cast(str, row["simulator_identity"])),
        benchmark_identity=SecuritiesBenchmarkIdentity(cast(str, row["benchmark_identity"])),
        signal_count=cast(int, row["signal_count"]),
        trade_count=cast(int, row["trade_count"]),
        gross_return=cast(Decimal, row["gross_return"]),
        net_return=cast(Decimal, row["net_return"]),
        annualized_return=cast(Decimal | None, row["annualized_return"]),
        volatility=cast(Decimal | None, row["volatility"]),
        maximum_drawdown=cast(Decimal, row["maximum_drawdown"]),
        turnover=cast(Decimal, row["turnover"]),
        hit_rate=cast(Decimal, row["hit_rate"]),
        average_holding_period=cast(Decimal, row["average_holding_period"]),
        exposure=cast(Decimal, row["exposure"]),
        cost_total=cast(Decimal, row["cost_total"]),
        corporate_action_count=cast(int, row["corporate_action_count"]),
        conformance_state=MultiSimulatorConformanceClassification(
            cast(str, row["conformance_state"])
        ),
        integrity_warnings=tuple(cast(list[str], row["integrity_warnings"])),
        canonical_input_hash=cast(str, row["canonical_input_hash"]),
        canonical_result_hash=cast(str, row["canonical_result_hash"]),
    )


def _object_rows(value: object) -> tuple[dict[str, object], ...]:
    return tuple(cast(dict[str, object], item) for item in cast(list[object], value))


def _decimal_or_none(value: object) -> Decimal | None:
    return None if value is None else Decimal(cast(str, value))
