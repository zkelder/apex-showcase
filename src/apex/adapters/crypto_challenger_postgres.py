"""PostgreSQL persistence for immutable crypto challenger evidence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.crypto_dossier_postgres import PostgresCryptoDossierRepository
from apex.adapters.crypto_research_postgres import _data, _payload, _strings, _time
from apex.adapters.research_evidence_postgres import research_hypothesis_version
from apex.domain.crypto_challenger import (
    ChallengerConclusionType,
    ChallengerMetrics,
    ChallengerPortfolio,
    ChallengerWindow,
    CryptoChallengerConclusion,
    CryptoChallengerDossier,
    CryptoChallengerOutcome,
    CryptoChallengerProtocol,
    CryptoChallengerRun,
    MonthlyMomentumDecision,
    MonthlyMomentumHypothesisSpecification,
    RetainedPriorComparison,
)
from apex.domain.crypto_research import CostScenarioName
from apex.domain.research_evidence import (
    EvidenceClass,
    ExperimentOutcomeDecision,
    ExperimentState,
    ReproducibilityStatus,
    ResearchHypothesisVersion,
)

metadata = sa.MetaData()


def _table(name: str, *columns: Any, unique: str | None = None) -> sa.Table:
    items: list[Any] = list(columns)
    if unique is not None:
        items.append(sa.UniqueConstraint(unique))
    return sa.Table(name, metadata, *items)


crypto_challenger_hypothesis = _table(
    "crypto_challenger_hypothesis",
    sa.Column("hypothesis_version_id", sa.String(160), primary_key=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
crypto_challenger_protocol = _table(
    "crypto_challenger_protocol",
    sa.Column("protocol_id", sa.String(160), primary_key=True),
    sa.Column("hypothesis_version_id", sa.String(160), nullable=False),
    sa.Column("strategy_specification_id", sa.String(160), nullable=False),
    sa.Column("protocol_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    unique="protocol_hash",
)
crypto_challenger_run = _table(
    "crypto_challenger_run",
    sa.Column("run_id", sa.String(160), primary_key=True),
    sa.Column("protocol_id", sa.String(160), nullable=False),
    sa.Column("snapshot_id", sa.String(160), nullable=False),
    sa.Column("result_hash", sa.String(64), nullable=False),
    sa.Column("configuration_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("protocol_id", "snapshot_id"),
)
crypto_challenger_outcome = _table(
    "crypto_challenger_outcome",
    sa.Column("outcome_id", sa.String(160), primary_key=True),
    sa.Column("run_id", sa.String(160), nullable=False),
    sa.Column("decision", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("paper_eligible", sa.Boolean(), nullable=False),
    sa.Column("live_eligible", sa.Boolean(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    unique="run_id",
)
crypto_challenger_dossier = _table(
    "crypto_challenger_dossier",
    sa.Column("dossier_id", sa.String(160), primary_key=True),
    sa.Column("protocol_id", sa.String(160), nullable=False),
    sa.Column("strategy_specification_id", sa.String(160), nullable=False),
    sa.Column("earlier_run_id", sa.String(160), nullable=False),
    sa.Column("later_run_id", sa.String(160), nullable=False),
    sa.Column("membership_hash", sa.String(64), nullable=False),
    sa.Column("complete_windows", sa.Integer(), nullable=False),
    sa.Column("passing_windows", sa.Integer(), nullable=False),
    sa.Column("failing_windows", sa.Integer(), nullable=False),
    sa.Column("inconclusive_windows", sa.Integer(), nullable=False),
    sa.Column("compounded_conservative_return", sa.Numeric(28, 8), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    unique="membership_hash",
)
crypto_challenger_conclusion = _table(
    "crypto_challenger_conclusion",
    sa.Column("conclusion_id", sa.String(160), primary_key=True),
    sa.Column("dossier_id", sa.String(160), nullable=False),
    sa.Column("conclusion", sa.String(40), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("paper_eligible", sa.Boolean(), nullable=False),
    sa.Column("live_eligible", sa.Boolean(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    unique="dossier_id",
)


def _windows(value: object) -> tuple[ChallengerWindow, ChallengerWindow]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("CRYPTO_CHALLENGER_WINDOWS_INVALID")
    result = tuple(
        ChallengerWindow(
            label=str(item["label"]),
            snapshot_id=str(item["snapshot_id"]),
            validation_start=_time(item["validation_start"]),
            validation_end=_time(item["validation_end"]),
            prior_run_id=str(item["prior_run_id"]),
        )
        for item in value
        if isinstance(item, dict)
    )
    if len(result) != 2:
        raise ValueError("CRYPTO_CHALLENGER_WINDOWS_INVALID")
    return (result[0], result[1])


def _hypothesis(row: RowMapping) -> MonthlyMomentumHypothesisSpecification:
    value = _data(row)
    snapshots = _strings(value["snapshot_ids"])
    windows = value["validation_windows"]
    if len(snapshots) != 2 or not isinstance(windows, list) or len(windows) != 2:
        raise ValueError("CRYPTO_CHALLENGER_HYPOTHESIS_INVALID")
    parsed_windows = tuple(
        (_time(item[0]), _time(item[1]))
        for item in windows
        if isinstance(item, list) and len(item) == 2
    )
    if len(parsed_windows) != 2:
        raise ValueError("CRYPTO_CHALLENGER_HYPOTHESIS_INVALID")
    return MonthlyMomentumHypothesisSpecification(
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        product_id=str(value["product_id"]),
        granularity=str(value["granularity"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        snapshot_ids=(snapshots[0], snapshots[1]),
        validation_windows=(parsed_windows[0], parsed_windows[1]),
        exact_rule=str(value["exact_rule"]),
        execution_timing=str(value["execution_timing"]),
        cost_model=str(value["cost_model"]),
        baselines=_strings(value["baselines"]),
        success_criteria=_strings(value["success_criteria"]),
        failure_criteria=_strings(value["failure_criteria"]),
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _protocol(row: RowMapping) -> CryptoChallengerProtocol:
    value = _data(row)
    return CryptoChallengerProtocol(
        protocol_id=str(value["protocol_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        windows=_windows(value["windows"]),
        evaluator_identity=str(value["evaluator_identity"]),
        feature_semantics=str(value["feature_semantics"]),
        execution_semantics=str(value["execution_semantics"]),
        cost_model=str(value["cost_model"]),
        baseline_definitions=_strings(value["baseline_definitions"]),
        per_window_policy=str(value["per_window_policy"]),
        aggregate_policy=str(value["aggregate_policy"]),
        deterministic_seed=int(value["deterministic_seed"]),
        code_revision=str(value["code_revision"]),
        protocol_hash=str(value["protocol_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _decision(value: object) -> MonthlyMomentumDecision:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CHALLENGER_DECISION_INVALID")
    return MonthlyMomentumDecision(
        decision_month=_time(value["decision_month"]),
        information_through=(
            _time(value["information_through"])
            if value["information_through"] is not None
            else None
        ),
        information_available_at=(
            _time(value["information_available_at"])
            if value["information_available_at"] is not None
            else None
        ),
        trailing_bucket_start=(
            _time(value["trailing_bucket_start"])
            if value["trailing_bucket_start"] is not None
            else None
        ),
        execution_at=_time(value["execution_at"]) if value["execution_at"] is not None else None,
        recent_close=(
            Decimal(str(value["recent_close"])) if value["recent_close"] is not None else None
        ),
        trailing_close=(
            Decimal(str(value["trailing_close"])) if value["trailing_close"] is not None else None
        ),
        momentum=Decimal(str(value["momentum"])) if value["momentum"] is not None else None,
        desired_long=bool(value["desired_long"]),
        eligible=bool(value["eligible"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        canonical_hash=str(value["canonical_hash"]),
    )


def _metrics(value: object) -> ChallengerMetrics:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CHALLENGER_METRIC_INVALID")
    return ChallengerMetrics(
        portfolio=ChallengerPortfolio(value["portfolio"]),
        scenario=CostScenarioName(value["scenario"]),
        validation_start=_time(value["validation_start"]),
        validation_end=_time(value["validation_end"]),
        starting_notional=Decimal(str(value["starting_notional"])),
        ending_notional=Decimal(str(value["ending_notional"])),
        gross_return=Decimal(str(value["gross_return"])),
        net_return=Decimal(str(value["net_return"])),
        maximum_drawdown=Decimal(str(value["maximum_drawdown"])),
        realized_volatility=Decimal(str(value["realized_volatility"])),
        entries=int(value["entries"]),
        exits=int(value["exits"]),
        completed_round_trips=int(value["completed_round_trips"]),
        turnover=Decimal(str(value["turnover"])),
        exposure_percentage=Decimal(str(value["exposure_percentage"])),
        days_long=int(value["days_long"]),
        days_in_cash=int(value["days_in_cash"]),
        rebalance_decisions=int(value["rebalance_decisions"]),
        failed_or_skipped_rebalances=int(value["failed_or_skipped_rebalances"]),
        modeled_fees=Decimal(str(value["modeled_fees"])),
        modeled_slippage=Decimal(str(value["modeled_slippage"])),
        total_modeled_cost=Decimal(str(value["total_modeled_cost"])),
        missing_or_excluded_observations=int(value["missing_or_excluded_observations"]),
        complete=bool(value["complete"]),
        reproducibility_status=ReproducibilityStatus(value["reproducibility_status"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _comparison(value: object) -> RetainedPriorComparison:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CHALLENGER_COMPARISON_INVALID")
    return RetainedPriorComparison(
        prior_run_id=str(value["prior_run_id"]),
        prior_result_hash=str(value["prior_result_hash"]),
        prior_canonical_run_hash=str(value["prior_canonical_run_hash"]),
        challenger_net_return_delta=Decimal(str(value["challenger_net_return_delta"])),
        challenger_drawdown_delta=Decimal(str(value["challenger_drawdown_delta"])),
        challenger_turnover_delta=Decimal(str(value["challenger_turnover_delta"])),
        challenger_cost_delta=Decimal(str(value["challenger_cost_delta"])),
        canonical_hash=str(value["canonical_hash"]),
    )


def _run(row: RowMapping) -> CryptoChallengerRun:
    value = _data(row)
    decisions = value["decisions"]
    metrics = value["metrics"]
    if not isinstance(decisions, list) or not isinstance(metrics, list):
        raise ValueError("CRYPTO_CHALLENGER_RESULT_INVALID")
    return CryptoChallengerRun(
        run_id=str(value["run_id"]),
        protocol_id=str(value["protocol_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        snapshot_id=str(value["snapshot_id"]),
        snapshot_manifest_hash=str(value["snapshot_manifest_hash"]),
        validation_start=_time(value["validation_start"]),
        validation_end=_time(value["validation_end"]),
        code_revision=str(value["code_revision"]),
        deterministic_seed=int(value["deterministic_seed"]),
        executed_at=_time(value["executed_at"]),
        state=ExperimentState(value["state"]),
        complete=bool(value["complete"]),
        reproducibility_status=ReproducibilityStatus(value["reproducibility_status"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        decisions=tuple(_decision(item) for item in decisions),
        metrics=tuple(_metrics(item) for item in metrics),
        prior_comparison=_comparison(value["prior_comparison"]),
        warnings=_strings(value["warnings"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        configuration_hash=str(value["configuration_hash"]),
        result_hash=str(value["result_hash"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _outcome(row: RowMapping) -> CryptoChallengerOutcome:
    value = _data(row)
    return CryptoChallengerOutcome(
        outcome_id=str(value["outcome_id"]),
        run_id=str(value["run_id"]),
        decision=ExperimentOutcomeDecision(value["decision"]),
        reason=str(value["reason"]),
        decided_at=_time(value["decided_at"]),
        actor=str(value["actor"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _dossier(row: RowMapping) -> CryptoChallengerDossier:
    value = _data(row)
    run_ids = _strings(value["window_run_ids"])
    result_hashes = _strings(value["window_result_hashes"])
    outcome_ids = _strings(value["window_outcome_ids"])
    prior_ids = _strings(value["prior_run_ids"])
    if any(len(items) != 2 for items in (run_ids, result_hashes, outcome_ids, prior_ids)):
        raise ValueError("CRYPTO_CHALLENGER_DOSSIER_MEMBERSHIP_INVALID")
    return CryptoChallengerDossier(
        dossier_id=str(value["dossier_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        semantic_comparison_hash=str(value["semantic_comparison_hash"]),
        material_change_rationale=str(value["material_change_rationale"]),
        protocol_id=str(value["protocol_id"]),
        protocol_hash=str(value["protocol_hash"]),
        window_run_ids=(run_ids[0], run_ids[1]),
        window_result_hashes=(result_hashes[0], result_hashes[1]),
        window_outcome_ids=(outcome_ids[0], outcome_ids[1]),
        prior_run_ids=(prior_ids[0], prior_ids[1]),
        complete_windows=int(value["complete_windows"]),
        passing_windows=int(value["passing_windows"]),
        failing_windows=int(value["failing_windows"]),
        inconclusive_windows=int(value["inconclusive_windows"]),
        compounded_conservative_return=Decimal(str(value["compounded_conservative_return"])),
        median_conservative_return=Decimal(str(value["median_conservative_return"])),
        worst_window_return=Decimal(str(value["worst_window_return"])),
        best_window_return=Decimal(str(value["best_window_return"])),
        worst_maximum_drawdown=Decimal(str(value["worst_maximum_drawdown"])),
        median_turnover=Decimal(str(value["median_turnover"])),
        total_modeled_costs=Decimal(str(value["total_modeled_costs"])),
        total_entries=int(value["total_entries"]),
        total_exits=int(value["total_exits"]),
        percentage_windows_beating_cash=Decimal(str(value["percentage_windows_beating_cash"])),
        percentage_windows_reducing_drawdown=Decimal(
            str(value["percentage_windows_reducing_drawdown"])
        ),
        consistency_status=str(value["consistency_status"]),
        warnings=_strings(value["warnings"]),
        membership_hash=str(value["membership_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _conclusion(row: RowMapping) -> CryptoChallengerConclusion:
    value = _data(row)
    return CryptoChallengerConclusion(
        conclusion_id=str(value["conclusion_id"]),
        dossier_id=str(value["dossier_id"]),
        conclusion=ChallengerConclusionType(value["conclusion"]),
        reason=str(value["reason"]),
        leading_challenger_status=str(value["leading_challenger_status"]),
        approved_champion_status=str(value["approved_champion_status"]),
        decided_at=_time(value["decided_at"]),
        actor=str(value["actor"]),
        canonical_hash=str(value["canonical_hash"]),
    )


class PostgresCryptoChallengerRepository(PostgresCryptoDossierRepository):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

    def challenger_hypothesis(
        self, hypothesis_version_id: str
    ) -> MonthlyMomentumHypothesisSpecification | None:
        row = self._one(
            sa.select(crypto_challenger_hypothesis).where(
                crypto_challenger_hypothesis.c.hypothesis_version_id == hypothesis_version_id
            )
        )
        return _hypothesis(row) if row is not None else None

    def save_challenger_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: MonthlyMomentumHypothesisSpecification,
    ) -> MonthlyMomentumHypothesisSpecification:
        self._insert(
            research_hypothesis_version,
            {
                "hypothesis_version_id": hypothesis.hypothesis_version_id,
                "hypothesis_id": hypothesis.hypothesis_id,
                "revision": hypothesis.revision,
                "status": hypothesis.status.value,
                "supersedes_version_id": hypothesis.supersedes_version_id,
                "payload": _payload(hypothesis),
                "canonical_hash": hypothesis.canonical_hash,
                "created_at": hypothesis.created_at,
            },
            "HYPOTHESIS_PERSISTENCE_CONFLICT",
        )
        self._insert(
            crypto_challenger_hypothesis,
            {
                "hypothesis_version_id": hypothesis.hypothesis_version_id,
                "payload": _payload(specification),
                "canonical_hash": specification.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CHALLENGER_HYPOTHESIS_PERSISTENCE_CONFLICT",
        )
        return specification

    def challenger_hypotheses(self) -> tuple[MonthlyMomentumHypothesisSpecification, ...]:
        return tuple(
            _hypothesis(row)
            for row in self._rows(
                sa.select(crypto_challenger_hypothesis).order_by(
                    crypto_challenger_hypothesis.c.hypothesis_version_id
                )
            )
        )

    def challenger_protocol(self, protocol_id: str) -> CryptoChallengerProtocol | None:
        row = self._one(
            sa.select(crypto_challenger_protocol).where(
                crypto_challenger_protocol.c.protocol_id == protocol_id
            )
        )
        return _protocol(row) if row is not None else None

    def save_challenger_protocol(self, value: CryptoChallengerProtocol) -> CryptoChallengerProtocol:
        self._insert(
            crypto_challenger_protocol,
            {
                "protocol_id": value.protocol_id,
                "hypothesis_version_id": value.hypothesis_version_id,
                "strategy_specification_id": value.strategy_specification_id,
                "protocol_hash": value.protocol_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CHALLENGER_PROTOCOL_PERSISTENCE_CONFLICT",
        )
        return value

    def challenger_protocols(self) -> tuple[CryptoChallengerProtocol, ...]:
        return tuple(
            _protocol(row)
            for row in self._rows(
                sa.select(crypto_challenger_protocol).order_by(
                    crypto_challenger_protocol.c.protocol_id
                )
            )
        )

    def challenger_run(self, run_id: str) -> CryptoChallengerRun | None:
        row = self._one(
            sa.select(crypto_challenger_run).where(crypto_challenger_run.c.run_id == run_id)
        )
        return _run(row) if row is not None else None

    def save_challenger_run(self, value: CryptoChallengerRun) -> CryptoChallengerRun:
        self._insert(
            crypto_challenger_run,
            {
                "run_id": value.run_id,
                "protocol_id": value.protocol_id,
                "snapshot_id": value.snapshot_id,
                "result_hash": value.result_hash,
                "configuration_hash": value.configuration_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CHALLENGER_RUN_PERSISTENCE_CONFLICT",
        )
        return value

    def challenger_runs(self) -> tuple[CryptoChallengerRun, ...]:
        return tuple(
            _run(row)
            for row in self._rows(
                sa.select(crypto_challenger_run).order_by(crypto_challenger_run.c.run_id)
            )
        )

    def challenger_outcome(self, outcome_id: str) -> CryptoChallengerOutcome | None:
        row = self._one(
            sa.select(crypto_challenger_outcome).where(
                crypto_challenger_outcome.c.outcome_id == outcome_id
            )
        )
        return _outcome(row) if row is not None else None

    def challenger_outcome_for_run(self, run_id: str) -> CryptoChallengerOutcome | None:
        row = self._one(
            sa.select(crypto_challenger_outcome).where(crypto_challenger_outcome.c.run_id == run_id)
        )
        return _outcome(row) if row is not None else None

    def save_challenger_outcome(self, value: CryptoChallengerOutcome) -> CryptoChallengerOutcome:
        self._insert(
            crypto_challenger_outcome,
            {
                "outcome_id": value.outcome_id,
                "run_id": value.run_id,
                "decision": value.decision.value,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "paper_eligible": False,
                "live_eligible": False,
                "authority": "NONE",
            },
            "CRYPTO_CHALLENGER_OUTCOME_PERSISTENCE_CONFLICT",
        )
        return value

    def challenger_outcomes(self) -> tuple[CryptoChallengerOutcome, ...]:
        return tuple(
            _outcome(row)
            for row in self._rows(
                sa.select(crypto_challenger_outcome).order_by(
                    crypto_challenger_outcome.c.outcome_id
                )
            )
        )

    def challenger_dossier(self, dossier_id: str) -> CryptoChallengerDossier | None:
        row = self._one(
            sa.select(crypto_challenger_dossier).where(
                crypto_challenger_dossier.c.dossier_id == dossier_id
            )
        )
        return _dossier(row) if row is not None else None

    def save_challenger_dossier(self, value: CryptoChallengerDossier) -> CryptoChallengerDossier:
        self._insert(
            crypto_challenger_dossier,
            {
                "dossier_id": value.dossier_id,
                "protocol_id": value.protocol_id,
                "strategy_specification_id": value.strategy_specification_id,
                "earlier_run_id": value.window_run_ids[0],
                "later_run_id": value.window_run_ids[1],
                "membership_hash": value.membership_hash,
                "complete_windows": value.complete_windows,
                "passing_windows": value.passing_windows,
                "failing_windows": value.failing_windows,
                "inconclusive_windows": value.inconclusive_windows,
                "compounded_conservative_return": value.compounded_conservative_return,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CHALLENGER_DOSSIER_PERSISTENCE_CONFLICT",
        )
        return value

    def challenger_dossiers(self) -> tuple[CryptoChallengerDossier, ...]:
        return tuple(
            _dossier(row)
            for row in self._rows(
                sa.select(crypto_challenger_dossier).order_by(
                    crypto_challenger_dossier.c.dossier_id
                )
            )
        )

    def challenger_conclusion(self, conclusion_id: str) -> CryptoChallengerConclusion | None:
        row = self._one(
            sa.select(crypto_challenger_conclusion).where(
                crypto_challenger_conclusion.c.conclusion_id == conclusion_id
            )
        )
        return _conclusion(row) if row is not None else None

    def challenger_conclusion_for_dossier(
        self, dossier_id: str
    ) -> CryptoChallengerConclusion | None:
        row = self._one(
            sa.select(crypto_challenger_conclusion).where(
                crypto_challenger_conclusion.c.dossier_id == dossier_id
            )
        )
        return _conclusion(row) if row is not None else None

    def save_challenger_conclusion(
        self, value: CryptoChallengerConclusion
    ) -> CryptoChallengerConclusion:
        self._insert(
            crypto_challenger_conclusion,
            {
                "conclusion_id": value.conclusion_id,
                "dossier_id": value.dossier_id,
                "conclusion": value.conclusion.value,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "paper_eligible": False,
                "live_eligible": False,
                "authority": "NONE",
            },
            "CRYPTO_CHALLENGER_CONCLUSION_PERSISTENCE_CONFLICT",
        )
        return value

    def challenger_conclusions(self) -> tuple[CryptoChallengerConclusion, ...]:
        return tuple(
            _conclusion(row)
            for row in self._rows(
                sa.select(crypto_challenger_conclusion).order_by(
                    crypto_challenger_conclusion.c.conclusion_id
                )
            )
        )


__all__ = ["PostgresCryptoChallengerRepository", "metadata"]
