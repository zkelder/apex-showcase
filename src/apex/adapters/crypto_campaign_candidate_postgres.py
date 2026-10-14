"""PostgreSQL persistence for crypto campaign Candidate 1 evidence."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.crypto_campaign_postgres import PostgresCryptoCampaignRepository
from apex.adapters.crypto_research_postgres import _data, _payload, _strings, _time
from apex.adapters.research_evidence_postgres import research_hypothesis_version
from apex.domain.crypto_campaign_candidate import (
    CandidateDossier,
    CandidateHolding,
    CandidateHypothesisSpecification,
    CandidateMetrics,
    CandidateNominationEvaluation,
    CandidateOutcome,
    CandidatePortfolio,
    CandidateProtocol,
    CandidateRun,
    CandidateSemanticComparisons,
    CandidateState,
    CandidateWindow,
    MaterialDifference,
    Monthly200Decision,
    PriorStrategyComparison,
)
from apex.domain.crypto_campaign_candidate_2 import (
    BreakoutDecision,
    BreakoutDossier,
    BreakoutHolding,
    BreakoutHypothesisSpecification,
    BreakoutMaterialDifference,
    BreakoutMetrics,
    BreakoutPortfolio,
    BreakoutPositionState,
    BreakoutPriorStrategyComparison,
    BreakoutProtocol,
    BreakoutRule,
    BreakoutRun,
    BreakoutSemanticComparisons,
    BreakoutTransitionReason,
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


def _table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(160), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
    )


candidate_hypothesis_table = _table(
    "crypto_campaign_candidate_hypothesis",
    "hypothesis_version_id",
    sa.Column("campaign_id", sa.String(160), nullable=False),
)
candidate_comparison_table = _table(
    "crypto_campaign_candidate_comparison",
    "candidate_id",
    sa.Column("strategy_specification_id", sa.String(160), nullable=False),
)
candidate_protocol_table = _table(
    "crypto_campaign_candidate_protocol",
    "protocol_id",
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("candidate_id", sa.String(160), nullable=False),
    sa.Column("protocol_hash", sa.String(64), nullable=False),
)
candidate_run_table = _table(
    "crypto_campaign_candidate_run",
    "run_id",
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("candidate_id", sa.String(160), nullable=False),
    sa.Column("protocol_id", sa.String(160), nullable=False),
    sa.Column("snapshot_id", sa.String(160), nullable=False),
    sa.Column("result_hash", sa.String(64), nullable=False),
    sa.Column("configuration_hash", sa.String(64), nullable=False),
)
candidate_outcome_table = _table(
    "crypto_campaign_candidate_outcome",
    "outcome_id",
    sa.Column("run_id", sa.String(160), nullable=False),
    sa.Column("decision", sa.String(40), nullable=False),
    sa.Column("paper_eligible", sa.Boolean(), nullable=False),
    sa.Column("live_eligible", sa.Boolean(), nullable=False),
)
candidate_dossier_table = _table(
    "crypto_campaign_candidate_dossier",
    "dossier_id",
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("candidate_id", sa.String(160), nullable=False),
    sa.Column("protocol_id", sa.String(160), nullable=False),
    sa.Column("membership_hash", sa.String(64), nullable=False),
    sa.Column("passing_windows", sa.Integer(), nullable=False),
)
candidate_nomination_table = _table(
    "crypto_campaign_candidate_nomination",
    "evaluation_id",
    sa.Column("dossier_id", sa.String(160), nullable=False),
    sa.Column("campaign_id", sa.String(160), nullable=False),
    sa.Column("candidate_id", sa.String(160), nullable=False),
    sa.Column("eligible", sa.Boolean(), nullable=False),
    sa.Column("paper_eligible", sa.Boolean(), nullable=False),
    sa.Column("live_eligible", sa.Boolean(), nullable=False),
)


def _has_field(row: RowMapping, name: str) -> bool:
    return name in _data(row)


def _material(value: object) -> MaterialDifference:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_COMPARISON_INVALID")
    changed = value["changed"]
    preserved = value["preserved"]
    if not isinstance(changed, list) or not isinstance(preserved, list):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_COMPARISON_INVALID")
    return MaterialDifference(
        prior_specification_id=str(value["prior_specification_id"]),
        prior_family=str(value["prior_family"]),
        changed=tuple((str(a), str(b), str(c)) for a, b, c in changed),
        preserved=tuple((str(a), str(b)) for a, b in preserved),
        materially_distinct=bool(value["materially_distinct"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _hypothesis(row: RowMapping) -> CandidateHypothesisSpecification:
    value = _data(row)
    snapshots = _strings(value["snapshot_ids"])
    windows = value["validation_windows"]
    if len(snapshots) != 2 or not isinstance(windows, list) or len(windows) != 2:
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_HYPOTHESIS_INVALID")
    return CandidateHypothesisSpecification(
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        campaign_id=str(value["campaign_id"]),
        product_id=str(value["product_id"]),
        granularity=str(value["granularity"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        snapshot_ids=(snapshots[0], snapshots[1]),
        validation_windows=(
            (_time(windows[0][0]), _time(windows[0][1])),
            (_time(windows[1][0]), _time(windows[1][1])),
        ),
        signal_definition=str(value["signal_definition"]),
        execution_timing=str(value["execution_timing"]),
        position_sizing=str(value["position_sizing"]),
        cost_model=str(value["cost_model"]),
        per_window_policy=_strings(value["per_window_policy"]),
        aggregate_policy=_strings(value["aggregate_policy"]),
        failure_conditions=_strings(value["failure_conditions"]),
        expected_mechanism=str(value["expected_mechanism"]),
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _comparisons(row: RowMapping) -> CandidateSemanticComparisons:
    value = _data(row)
    return CandidateSemanticComparisons(
        candidate_specification_id=str(value["candidate_specification_id"]),
        candidate_semantics_hash=str(value["candidate_semantics_hash"]),
        against_sma_20_100=_material(value["against_sma_20_100"]),
        against_monthly_252=_material(value["against_monthly_252"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _window(value: object) -> CandidateWindow:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_WINDOW_INVALID")
    return CandidateWindow(
        label=str(value["label"]),
        snapshot_id=str(value["snapshot_id"]),
        validation_start=_time(value["validation_start"]),
        validation_end=_time(value["validation_end"]),
    )


def _protocol(row: RowMapping) -> CandidateProtocol:
    value = _data(row)
    windows = value["windows"]
    if not isinstance(windows, list) or len(windows) != 2:
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_INVALID")
    return CandidateProtocol(
        protocol_id=str(value["protocol_id"]),
        campaign_id=str(value["campaign_id"]),
        candidate_id=str(value["candidate_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        windows=(_window(windows[0]), _window(windows[1])),
        evaluator_identity=str(value["evaluator_identity"]),
        feature_semantics=str(value["feature_semantics"]),
        execution_semantics=str(value["execution_semantics"]),
        cost_model=str(value["cost_model"]),
        baseline_definitions=_strings(value["baseline_definitions"]),
        per_window_policy=str(value["per_window_policy"]),
        aggregate_policy=str(value["aggregate_policy"]),
        deterministic_seed=int(value["deterministic_seed"]),
        code_revision=str(value["code_revision"]),
        evidence_classification=str(value["evidence_classification"]),
        protocol_hash=str(value["protocol_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _decision(value: object) -> Monthly200Decision:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_DECISION_INVALID")

    def maybe_time(name: str) -> datetime | None:
        return _time(value[name]) if value[name] is not None else None

    def maybe_decimal(name: str) -> Decimal | None:
        return Decimal(str(value[name])) if value[name] is not None else None

    return Monthly200Decision(
        decision_month=_time(value["decision_month"]),
        information_through=maybe_time("information_through"),
        information_available_at=maybe_time("information_available_at"),
        observation_start=maybe_time("observation_start"),
        observation_count=int(value["observation_count"]),
        completed_close=maybe_decimal("completed_close"),
        arithmetic_mean=maybe_decimal("arithmetic_mean"),
        selected_state=str(value["selected_state"]),
        desired_long=bool(value["desired_long"]),
        execution_at=maybe_time("execution_at"),
        execution_open=maybe_decimal("execution_open"),
        eligible=bool(value["eligible"]),
        ineligibility_reason=(
            str(value["ineligibility_reason"])
            if value["ineligibility_reason"] is not None
            else None
        ),
        canonical_hash=str(value["canonical_hash"]),
    )


def _holding(value: object) -> CandidateHolding:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_HOLDING_INVALID")
    return CandidateHolding(
        bucket_start=_time(value["bucket_start"]),
        portfolio=CandidatePortfolio(value["portfolio"]),
        scenario=CostScenarioName(value["scenario"]),
        state=str(value["state"]),
        cash=Decimal(str(value["cash"])),
        units=Decimal(str(value["units"])),
        marked_value=Decimal(str(value["marked_value"])),
        canonical_hash=str(value["canonical_hash"]),
    )


def _metric(value: object) -> CandidateMetrics:
    if not isinstance(value, dict) or not isinstance(value["holdings"], list):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_METRIC_INVALID")
    return CandidateMetrics(
        portfolio=CandidatePortfolio(value["portfolio"]),
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
        monthly_decisions=int(value["monthly_decisions"]),
        eligible_decisions=int(value["eligible_decisions"]),
        ineligible_decisions=int(value["ineligible_decisions"]),
        modeled_fees=Decimal(str(value["modeled_fees"])),
        modeled_slippage=Decimal(str(value["modeled_slippage"])),
        total_modeled_cost=Decimal(str(value["total_modeled_cost"])),
        missing_or_excluded_observations=int(value["missing_or_excluded_observations"]),
        complete=bool(value["complete"]),
        reproducibility_status=ReproducibilityStatus(value["reproducibility_status"]),
        holdings=tuple(_holding(item) for item in value["holdings"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _prior(value: object) -> PriorStrategyComparison:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_PRIOR_COMPARISON_INVALID")
    return PriorStrategyComparison(
        strategy_family=str(value["strategy_family"]),
        prior_run_id=str(value["prior_run_id"]),
        prior_result_hash=str(value["prior_result_hash"]),
        prior_canonical_run_hash=str(value["prior_canonical_run_hash"]),
        candidate_net_return_delta=Decimal(str(value["candidate_net_return_delta"])),
        candidate_drawdown_delta=Decimal(str(value["candidate_drawdown_delta"])),
        candidate_turnover_delta=Decimal(str(value["candidate_turnover_delta"])),
        candidate_cost_delta=Decimal(str(value["candidate_cost_delta"])),
        canonical_hash=str(value["canonical_hash"]),
    )


def _run(row: RowMapping) -> CandidateRun:
    value = _data(row)
    decisions = value["decisions"]
    metrics = value["metrics"]
    comparisons = value["prior_comparisons"]
    if (
        not isinstance(decisions, list)
        or not isinstance(metrics, list)
        or not isinstance(comparisons, list)
        or len(comparisons) != 2
    ):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_RUN_INVALID")
    return CandidateRun(
        run_id=str(value["run_id"]),
        campaign_id=str(value["campaign_id"]),
        candidate_id=str(value["candidate_id"]),
        protocol_id=str(value["protocol_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        snapshot_id=str(value["snapshot_id"]),
        snapshot_manifest_hash=str(value["snapshot_manifest_hash"]),
        snapshot_exposure_state=str(value["snapshot_exposure_state"]),
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
        metrics=tuple(_metric(item) for item in metrics),
        prior_comparisons=(_prior(comparisons[0]), _prior(comparisons[1])),
        warnings=_strings(value["warnings"]),
        disqualifying_warnings=_strings(value["disqualifying_warnings"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        configuration_hash=str(value["configuration_hash"]),
        result_hash=str(value["result_hash"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _outcome(row: RowMapping) -> CandidateOutcome:
    value = _data(row)
    return CandidateOutcome(
        outcome_id=str(value["outcome_id"]),
        run_id=str(value["run_id"]),
        decision=ExperimentOutcomeDecision(value["decision"]),
        reason=str(value["reason"]),
        passed_conditions=_strings(value["passed_conditions"]),
        failed_conditions=_strings(value["failed_conditions"]),
        decided_at=_time(value["decided_at"]),
        actor=str(value["actor"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _dossier(row: RowMapping) -> CandidateDossier:
    value = _data(row)
    pairs = (
        _strings(value["snapshot_ids"]),
        _strings(value["snapshot_manifest_hashes"]),
        _strings(value["snapshot_exposure_states"]),
        _strings(value["window_run_ids"]),
        _strings(value["window_result_hashes"]),
        _strings(value["window_outcome_ids"]),
    )
    if any(len(item) != 2 for item in pairs):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_INVALID")
    return CandidateDossier(
        dossier_id=str(value["dossier_id"]),
        campaign_id=str(value["campaign_id"]),
        candidate_id=str(value["candidate_id"]),
        candidate_registration_hash=str(value["candidate_registration_hash"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        semantic_comparisons_hash=str(value["semantic_comparisons_hash"]),
        protocol_id=str(value["protocol_id"]),
        protocol_hash=str(value["protocol_hash"]),
        snapshot_ids=(pairs[0][0], pairs[0][1]),
        snapshot_manifest_hashes=(pairs[1][0], pairs[1][1]),
        snapshot_exposure_states=(pairs[2][0], pairs[2][1]),
        window_run_ids=(pairs[3][0], pairs[3][1]),
        window_result_hashes=(pairs[4][0], pairs[4][1]),
        window_outcome_ids=(pairs[5][0], pairs[5][1]),
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
        total_modeled_fees=Decimal(str(value["total_modeled_fees"])),
        total_modeled_slippage=Decimal(str(value["total_modeled_slippage"])),
        total_modeled_costs=Decimal(str(value["total_modeled_costs"])),
        total_entries=int(value["total_entries"]),
        total_exits=int(value["total_exits"]),
        percentage_windows_beating_cash=Decimal(str(value["percentage_windows_beating_cash"])),
        percentage_windows_reducing_drawdown=Decimal(
            str(value["percentage_windows_reducing_drawdown"])
        ),
        percentage_windows_improving_sma=Decimal(str(value["percentage_windows_improving_sma"])),
        percentage_windows_improving_monthly_252=Decimal(
            str(value["percentage_windows_improving_monthly_252"])
        ),
        consistency_status=str(value["consistency_status"]),
        warnings=_strings(value["warnings"]),
        holdout_accessed=bool(value["holdout_accessed"]),
        independent_validation=bool(value["independent_validation"]),
        membership_hash=str(value["membership_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_material(value: object) -> BreakoutMaterialDifference:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_COMPARISON_INVALID")
    changed = value["changed"]
    preserved = value["preserved"]
    if not isinstance(changed, list) or not isinstance(preserved, list):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_COMPARISON_INVALID")
    return BreakoutMaterialDifference(
        prior_specification_id=str(value["prior_specification_id"]),
        prior_family=str(value["prior_family"]),
        changed=tuple((str(a), str(b), str(c)) for a, b, c in changed),
        preserved=tuple((str(a), str(b)) for a, b in preserved),
        materially_distinct=bool(value["materially_distinct"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_hypothesis(row: RowMapping) -> BreakoutHypothesisSpecification:
    value = _data(row)
    snapshots = _strings(value["snapshot_ids"])
    windows = value["validation_windows"]
    if len(snapshots) != 2 or not isinstance(windows, list) or len(windows) != 2:
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_HYPOTHESIS_INVALID")
    return BreakoutHypothesisSpecification(
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        campaign_id=str(value["campaign_id"]),
        product_id=str(value["product_id"]),
        granularity=str(value["granularity"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        snapshot_ids=(snapshots[0], snapshots[1]),
        validation_windows=(
            (_time(windows[0][0]), _time(windows[0][1])),
            (_time(windows[1][0]), _time(windows[1][1])),
        ),
        initial_state=BreakoutPositionState(value["initial_state"]),
        entry_definition=str(value["entry_definition"]),
        exit_definition=str(value["exit_definition"]),
        execution_timing=str(value["execution_timing"]),
        position_sizing=str(value["position_sizing"]),
        cost_model=str(value["cost_model"]),
        per_window_policy=_strings(value["per_window_policy"]),
        aggregate_policy=_strings(value["aggregate_policy"]),
        failure_conditions=_strings(value["failure_conditions"]),
        expected_mechanism=str(value["expected_mechanism"]),
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_comparisons(row: RowMapping) -> BreakoutSemanticComparisons:
    value = _data(row)
    return BreakoutSemanticComparisons(
        candidate_specification_id=str(value["candidate_specification_id"]),
        candidate_semantics_hash=str(value["candidate_semantics_hash"]),
        against_sma_20_100=_breakout_material(value["against_sma_20_100"]),
        against_monthly_252=_breakout_material(value["against_monthly_252"]),
        against_monthly_200=_breakout_material(value["against_monthly_200"]),
        actor=str(value["actor"]),
        compared_at=_time(value["compared_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_protocol(row: RowMapping) -> BreakoutProtocol:
    value = _data(row)
    windows = value["windows"]
    manifests = _strings(value["snapshot_manifest_hashes"])
    if not isinstance(windows, list) or len(windows) != 2 or len(manifests) != 2:
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_PROTOCOL_INVALID")
    return BreakoutProtocol(
        protocol_id=str(value["protocol_id"]),
        campaign_id=str(value["campaign_id"]),
        candidate_id=str(value["candidate_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_version=int(value["strategy_version"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        semantic_comparisons_hash=str(value["semantic_comparisons_hash"]),
        windows=(_window(windows[0]), _window(windows[1])),
        snapshot_manifest_hashes=(manifests[0], manifests[1]),
        initial_state=BreakoutPositionState(value["initial_state"]),
        evaluator_identity=str(value["evaluator_identity"]),
        feature_semantics=str(value["feature_semantics"]),
        entry_semantics=str(value["entry_semantics"]),
        exit_semantics=str(value["exit_semantics"]),
        state_transition_semantics=str(value["state_transition_semantics"]),
        protective_exit_semantics=str(value["protective_exit_semantics"]),
        execution_semantics=str(value["execution_semantics"]),
        cost_model=str(value["cost_model"]),
        baseline_definitions=_strings(value["baseline_definitions"]),
        per_window_policy=str(value["per_window_policy"]),
        aggregate_policy=str(value["aggregate_policy"]),
        deterministic_seed=int(value["deterministic_seed"]),
        code_revision=str(value["code_revision"]),
        evidence_classification=str(value["evidence_classification"]),
        protocol_hash=str(value["protocol_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_decision(value: object) -> BreakoutDecision:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_DECISION_INVALID")

    def maybe_time(name: str) -> datetime | None:
        return _time(value[name]) if value[name] is not None else None

    def maybe_decimal(name: str) -> Decimal | None:
        return Decimal(str(value[name])) if value[name] is not None else None

    return BreakoutDecision(
        decision_month=_time(value["decision_month"]),
        prior_state=BreakoutPositionState(value["prior_state"]),
        information_through=maybe_time("information_through"),
        information_available_at=maybe_time("information_available_at"),
        decision_close=maybe_decimal("decision_close"),
        evaluated_rule=BreakoutRule(value["evaluated_rule"]),
        reference_start=maybe_time("reference_start"),
        reference_end=maybe_time("reference_end"),
        reference_count=int(value["reference_count"]),
        entry_maximum=maybe_decimal("entry_maximum"),
        exit_minimum=maybe_decimal("exit_minimum"),
        trigger=bool(value["trigger"]),
        resulting_state=BreakoutPositionState(value["resulting_state"]),
        execution_at=maybe_time("execution_at"),
        execution_open=maybe_decimal("execution_open"),
        transition_reason=BreakoutTransitionReason(value["transition_reason"]),
        data_quality_status=str(value["data_quality_status"]),
        eligible=bool(value["eligible"]),
        ineligibility_reason=(
            str(value["ineligibility_reason"])
            if value["ineligibility_reason"] is not None
            else None
        ),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_holding(value: object) -> BreakoutHolding:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_HOLDING_INVALID")
    return BreakoutHolding(
        bucket_start=_time(value["bucket_start"]),
        portfolio=BreakoutPortfolio(value["portfolio"]),
        scenario=CostScenarioName(value["scenario"]),
        state=str(value["state"]),
        cash=Decimal(str(value["cash"])),
        units=Decimal(str(value["units"])),
        marked_value=Decimal(str(value["marked_value"])),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_metric(value: object) -> BreakoutMetrics:
    if not isinstance(value, dict) or not isinstance(value["holdings"], list):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_METRIC_INVALID")
    return BreakoutMetrics(
        portfolio=BreakoutPortfolio(value["portfolio"]),
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
        strategy_triggered_transitions=int(value["strategy_triggered_transitions"]),
        protective_data_quality_transitions=int(value["protective_data_quality_transitions"]),
        turnover=Decimal(str(value["turnover"])),
        exposure_percentage=Decimal(str(value["exposure_percentage"])),
        days_long=int(value["days_long"]),
        days_in_cash=int(value["days_in_cash"]),
        monthly_decisions=int(value["monthly_decisions"]),
        eligible_decisions=int(value["eligible_decisions"]),
        ineligible_decisions=int(value["ineligible_decisions"]),
        entry_rule_evaluations=int(value["entry_rule_evaluations"]),
        exit_rule_evaluations=int(value["exit_rule_evaluations"]),
        entry_triggers=int(value["entry_triggers"]),
        exit_triggers=int(value["exit_triggers"]),
        modeled_fees=Decimal(str(value["modeled_fees"])),
        modeled_slippage=Decimal(str(value["modeled_slippage"])),
        total_modeled_cost=Decimal(str(value["total_modeled_cost"])),
        missing_or_excluded_observations=int(value["missing_or_excluded_observations"]),
        complete=bool(value["complete"]),
        reproducibility_status=ReproducibilityStatus(value["reproducibility_status"]),
        holdings=tuple(_breakout_holding(item) for item in value["holdings"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_prior(value: object) -> BreakoutPriorStrategyComparison:
    if not isinstance(value, dict):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_PRIOR_COMPARISON_INVALID")
    return BreakoutPriorStrategyComparison(
        strategy_family=str(value["strategy_family"]),
        prior_run_id=str(value["prior_run_id"]),
        prior_result_hash=str(value["prior_result_hash"]),
        prior_canonical_run_hash=str(value["prior_canonical_run_hash"]),
        candidate_net_return_delta=Decimal(str(value["candidate_net_return_delta"])),
        candidate_drawdown_delta=Decimal(str(value["candidate_drawdown_delta"])),
        candidate_turnover_delta=Decimal(str(value["candidate_turnover_delta"])),
        candidate_cost_delta=Decimal(str(value["candidate_cost_delta"])),
        candidate_exposure_delta=Decimal(str(value["candidate_exposure_delta"])),
        candidate_trade_count_delta=int(value["candidate_trade_count_delta"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_run(row: RowMapping) -> BreakoutRun:
    value = _data(row)
    decisions = value["decisions"]
    metrics = value["metrics"]
    comparisons = value["prior_comparisons"]
    if (
        not isinstance(decisions, list)
        or not isinstance(metrics, list)
        or not isinstance(comparisons, list)
        or len(comparisons) != 3
    ):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_RUN_INVALID")
    return BreakoutRun(
        run_id=str(value["run_id"]),
        campaign_id=str(value["campaign_id"]),
        candidate_id=str(value["candidate_id"]),
        protocol_id=str(value["protocol_id"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_version=int(value["strategy_version"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        snapshot_id=str(value["snapshot_id"]),
        snapshot_manifest_hash=str(value["snapshot_manifest_hash"]),
        snapshot_exposure_state=str(value["snapshot_exposure_state"]),
        validation_start=_time(value["validation_start"]),
        validation_end=_time(value["validation_end"]),
        initial_state=BreakoutPositionState(value["initial_state"]),
        code_revision=str(value["code_revision"]),
        deterministic_seed=int(value["deterministic_seed"]),
        executed_at=_time(value["executed_at"]),
        state=ExperimentState(value["state"]),
        complete=bool(value["complete"]),
        reproducibility_status=ReproducibilityStatus(value["reproducibility_status"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        decisions=tuple(_breakout_decision(item) for item in decisions),
        metrics=tuple(_breakout_metric(item) for item in metrics),
        prior_comparisons=(
            _breakout_prior(comparisons[0]),
            _breakout_prior(comparisons[1]),
            _breakout_prior(comparisons[2]),
        ),
        warnings=_strings(value["warnings"]),
        disqualifying_warnings=_strings(value["disqualifying_warnings"]),
        failure_reason=(
            str(value["failure_reason"]) if value["failure_reason"] is not None else None
        ),
        configuration_hash=str(value["configuration_hash"]),
        result_hash=str(value["result_hash"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _breakout_dossier(row: RowMapping) -> BreakoutDossier:
    value = _data(row)
    pairs = (
        _strings(value["snapshot_ids"]),
        _strings(value["snapshot_manifest_hashes"]),
        _strings(value["snapshot_exposure_states"]),
        _strings(value["window_run_ids"]),
        _strings(value["window_result_hashes"]),
        _strings(value["window_outcome_ids"]),
    )
    if any(len(item) != 2 for item in pairs):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_2_DOSSIER_INVALID")
    return BreakoutDossier(
        dossier_id=str(value["dossier_id"]),
        campaign_id=str(value["campaign_id"]),
        candidate_id=str(value["candidate_id"]),
        candidate_registration_hash=str(value["candidate_registration_hash"]),
        hypothesis_version_id=str(value["hypothesis_version_id"]),
        strategy_specification_id=str(value["strategy_specification_id"]),
        strategy_semantics_hash=str(value["strategy_semantics_hash"]),
        semantic_comparisons_hash=str(value["semantic_comparisons_hash"]),
        protocol_id=str(value["protocol_id"]),
        protocol_hash=str(value["protocol_hash"]),
        snapshot_ids=(pairs[0][0], pairs[0][1]),
        snapshot_manifest_hashes=(pairs[1][0], pairs[1][1]),
        snapshot_exposure_states=(pairs[2][0], pairs[2][1]),
        window_run_ids=(pairs[3][0], pairs[3][1]),
        window_result_hashes=(pairs[4][0], pairs[4][1]),
        window_outcome_ids=(pairs[5][0], pairs[5][1]),
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
        total_modeled_fees=Decimal(str(value["total_modeled_fees"])),
        total_modeled_slippage=Decimal(str(value["total_modeled_slippage"])),
        total_modeled_costs=Decimal(str(value["total_modeled_costs"])),
        total_entries=int(value["total_entries"]),
        total_exits=int(value["total_exits"]),
        total_strategy_triggered_transitions=int(value["total_strategy_triggered_transitions"]),
        total_protective_data_quality_transitions=int(
            value["total_protective_data_quality_transitions"]
        ),
        percentage_windows_beating_cash=Decimal(str(value["percentage_windows_beating_cash"])),
        percentage_windows_reducing_drawdown=Decimal(
            str(value["percentage_windows_reducing_drawdown"])
        ),
        percentage_windows_improving_sma=Decimal(str(value["percentage_windows_improving_sma"])),
        percentage_windows_improving_monthly_252=Decimal(
            str(value["percentage_windows_improving_monthly_252"])
        ),
        percentage_windows_improving_monthly_200=Decimal(
            str(value["percentage_windows_improving_monthly_200"])
        ),
        consistency_status=str(value["consistency_status"]),
        warnings=_strings(value["warnings"]),
        holdout_accessed=bool(value["holdout_accessed"]),
        independent_validation=bool(value["independent_validation"]),
        network_request_performed=bool(value["network_request_performed"]),
        membership_hash=str(value["membership_hash"]),
        created_at=_time(value["created_at"]),
        canonical_hash=str(value["canonical_hash"]),
    )


def _nomination(row: RowMapping) -> CandidateNominationEvaluation:
    value = _data(row)
    conditions = value["conditions"]
    if not isinstance(conditions, list):
        raise ValueError("CRYPTO_CAMPAIGN_CANDIDATE_NOMINATION_INVALID")
    return CandidateNominationEvaluation(
        evaluation_id=str(value["evaluation_id"]),
        dossier_id=str(value["dossier_id"]),
        campaign_id=str(value["campaign_id"]),
        candidate_id=str(value["candidate_id"]),
        conditions=tuple((str(name), bool(result)) for name, result in conditions),
        passed_conditions=_strings(value["passed_conditions"]),
        failed_conditions=_strings(value["failed_conditions"]),
        eligible=bool(value["eligible"]),
        candidate_state=CandidateState(value["candidate_state"]),
        evaluated_at=_time(value["evaluated_at"]),
        actor=str(value["actor"]),
        reason=str(value["reason"]),
        canonical_hash=str(value["canonical_hash"]),
    )


class PostgresCryptoCampaignCandidateRepository(PostgresCryptoCampaignRepository):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

    def candidate_hypothesis(
        self, hypothesis_version_id: str
    ) -> CandidateHypothesisSpecification | None:
        row = self._one(
            sa.select(candidate_hypothesis_table).where(
                candidate_hypothesis_table.c.hypothesis_version_id == hypothesis_version_id
            )
        )
        if row is None or _has_field(row, "entry_definition"):
            return None
        return _hypothesis(row)

    def save_candidate_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: CandidateHypothesisSpecification,
    ) -> CandidateHypothesisSpecification:
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
            candidate_hypothesis_table,
            {
                "hypothesis_version_id": hypothesis.hypothesis_version_id,
                "campaign_id": specification.campaign_id,
                "payload": _payload(specification),
                "canonical_hash": specification.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_HYPOTHESIS_PERSISTENCE_CONFLICT",
        )
        return specification

    def candidate_hypotheses(self) -> tuple[CandidateHypothesisSpecification, ...]:
        return tuple(
            _hypothesis(row)
            for row in self._rows(
                sa.select(candidate_hypothesis_table).order_by(
                    candidate_hypothesis_table.c.hypothesis_version_id
                )
            )
            if not _has_field(row, "entry_definition")
        )

    def candidate_comparisons(self, candidate_id: str) -> CandidateSemanticComparisons | None:
        row = self._one(
            sa.select(candidate_comparison_table).where(
                candidate_comparison_table.c.candidate_id == candidate_id
            )
        )
        if row is None or _has_field(row, "against_monthly_200"):
            return None
        return _comparisons(row)

    def save_candidate_comparisons(
        self, candidate_id: str, value: CandidateSemanticComparisons
    ) -> CandidateSemanticComparisons:
        self._insert(
            candidate_comparison_table,
            {
                "candidate_id": candidate_id,
                "strategy_specification_id": value.candidate_specification_id,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_COMPARISON_PERSISTENCE_CONFLICT",
        )
        return value

    def candidate_protocol(self, protocol_id: str) -> CandidateProtocol | None:
        row = self._one(
            sa.select(candidate_protocol_table).where(
                candidate_protocol_table.c.protocol_id == protocol_id
            )
        )
        if row is None or _has_field(row, "initial_state"):
            return None
        return _protocol(row)

    def save_candidate_protocol(self, value: CandidateProtocol) -> CandidateProtocol:
        self._insert(
            candidate_protocol_table,
            {
                "protocol_id": value.protocol_id,
                "campaign_id": value.campaign_id,
                "candidate_id": value.candidate_id,
                "protocol_hash": value.protocol_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_PROTOCOL_PERSISTENCE_CONFLICT",
        )
        return value

    def candidate_protocols(self) -> tuple[CandidateProtocol, ...]:
        return tuple(
            _protocol(row)
            for row in self._rows(
                sa.select(candidate_protocol_table).order_by(candidate_protocol_table.c.protocol_id)
            )
            if not _has_field(row, "initial_state")
        )

    def candidate_run(self, run_id: str) -> CandidateRun | None:
        row = self._one(
            sa.select(candidate_run_table).where(candidate_run_table.c.run_id == run_id)
        )
        if row is None or _has_field(row, "initial_state"):
            return None
        return _run(row)

    def save_candidate_run(self, value: CandidateRun) -> CandidateRun:
        self._insert(
            candidate_run_table,
            {
                "run_id": value.run_id,
                "campaign_id": value.campaign_id,
                "candidate_id": value.candidate_id,
                "protocol_id": value.protocol_id,
                "snapshot_id": value.snapshot_id,
                "result_hash": value.result_hash,
                "configuration_hash": value.configuration_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_RUN_PERSISTENCE_CONFLICT",
        )
        return value

    def candidate_runs(self) -> tuple[CandidateRun, ...]:
        return tuple(
            _run(row)
            for row in self._rows(
                sa.select(candidate_run_table).order_by(candidate_run_table.c.run_id)
            )
            if not _has_field(row, "initial_state")
        )

    def candidate_outcome(self, outcome_id: str) -> CandidateOutcome | None:
        row = self._one(
            sa.select(candidate_outcome_table).where(
                candidate_outcome_table.c.outcome_id == outcome_id
            )
        )
        return _outcome(row) if row is not None else None

    def candidate_outcome_for_run(self, run_id: str) -> CandidateOutcome | None:
        row = self._one(
            sa.select(candidate_outcome_table).where(candidate_outcome_table.c.run_id == run_id)
        )
        return _outcome(row) if row is not None else None

    def save_candidate_outcome(self, value: CandidateOutcome) -> CandidateOutcome:
        self._insert(
            candidate_outcome_table,
            {
                "outcome_id": value.outcome_id,
                "run_id": value.run_id,
                "decision": value.decision.value,
                "paper_eligible": False,
                "live_eligible": False,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_OUTCOME_PERSISTENCE_CONFLICT",
        )
        return value

    def candidate_outcomes(self) -> tuple[CandidateOutcome, ...]:
        candidate_1_run_ids = {
            row["run_id"]
            for row in self._rows(sa.select(candidate_run_table))
            if not _has_field(row, "initial_state")
        }
        return tuple(
            _outcome(row)
            for row in self._rows(
                sa.select(candidate_outcome_table).order_by(candidate_outcome_table.c.outcome_id)
            )
            if row["run_id"] in candidate_1_run_ids
        )

    def candidate_dossier(self, dossier_id: str) -> CandidateDossier | None:
        row = self._one(
            sa.select(candidate_dossier_table).where(
                candidate_dossier_table.c.dossier_id == dossier_id
            )
        )
        if row is None or _has_field(row, "percentage_windows_improving_monthly_200"):
            return None
        return _dossier(row)

    def save_candidate_dossier(self, value: CandidateDossier) -> CandidateDossier:
        self._insert(
            candidate_dossier_table,
            {
                "dossier_id": value.dossier_id,
                "campaign_id": value.campaign_id,
                "candidate_id": value.candidate_id,
                "protocol_id": value.protocol_id,
                "membership_hash": value.membership_hash,
                "passing_windows": value.passing_windows,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_DOSSIER_PERSISTENCE_CONFLICT",
        )
        return value

    def candidate_dossiers(self) -> tuple[CandidateDossier, ...]:
        return tuple(
            _dossier(row)
            for row in self._rows(
                sa.select(candidate_dossier_table).order_by(candidate_dossier_table.c.dossier_id)
            )
            if not _has_field(row, "percentage_windows_improving_monthly_200")
        )

    def candidate_nomination(self, evaluation_id: str) -> CandidateNominationEvaluation | None:
        row = self._one(
            sa.select(candidate_nomination_table).where(
                candidate_nomination_table.c.evaluation_id == evaluation_id
            )
        )
        return _nomination(row) if row is not None else None

    def candidate_nomination_for_dossier(
        self, dossier_id: str
    ) -> CandidateNominationEvaluation | None:
        row = self._one(
            sa.select(candidate_nomination_table).where(
                candidate_nomination_table.c.dossier_id == dossier_id
            )
        )
        return _nomination(row) if row is not None else None

    def save_candidate_nomination(
        self, value: CandidateNominationEvaluation
    ) -> CandidateNominationEvaluation:
        self._insert(
            candidate_nomination_table,
            {
                "evaluation_id": value.evaluation_id,
                "dossier_id": value.dossier_id,
                "campaign_id": value.campaign_id,
                "candidate_id": value.candidate_id,
                "eligible": value.eligible,
                "paper_eligible": False,
                "live_eligible": False,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_NOMINATION_PERSISTENCE_CONFLICT",
        )
        return value

    def candidate_nominations(self) -> tuple[CandidateNominationEvaluation, ...]:
        candidate_1_dossier_ids = {
            row["dossier_id"]
            for row in self._rows(sa.select(candidate_dossier_table))
            if not _has_field(row, "percentage_windows_improving_monthly_200")
        }
        return tuple(
            _nomination(row)
            for row in self._rows(
                sa.select(candidate_nomination_table).order_by(
                    candidate_nomination_table.c.evaluation_id
                )
            )
            if row["dossier_id"] in candidate_1_dossier_ids
        )

    def breakout_hypothesis(
        self, hypothesis_version_id: str
    ) -> BreakoutHypothesisSpecification | None:
        row = self._one(
            sa.select(candidate_hypothesis_table).where(
                candidate_hypothesis_table.c.hypothesis_version_id == hypothesis_version_id
            )
        )
        if row is None or not _has_field(row, "entry_definition"):
            return None
        return _breakout_hypothesis(row)

    def save_breakout_hypothesis(
        self,
        hypothesis: ResearchHypothesisVersion,
        specification: BreakoutHypothesisSpecification,
    ) -> BreakoutHypothesisSpecification:
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
            candidate_hypothesis_table,
            {
                "hypothesis_version_id": hypothesis.hypothesis_version_id,
                "campaign_id": specification.campaign_id,
                "payload": _payload(specification),
                "canonical_hash": specification.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_2_HYPOTHESIS_PERSISTENCE_CONFLICT",
        )
        return specification

    def breakout_hypotheses(
        self,
    ) -> tuple[BreakoutHypothesisSpecification, ...]:
        return tuple(
            _breakout_hypothesis(row)
            for row in self._rows(
                sa.select(candidate_hypothesis_table).order_by(
                    candidate_hypothesis_table.c.hypothesis_version_id
                )
            )
            if _has_field(row, "entry_definition")
        )

    def breakout_comparisons(self, candidate_id: str) -> BreakoutSemanticComparisons | None:
        row = self._one(
            sa.select(candidate_comparison_table).where(
                candidate_comparison_table.c.candidate_id == candidate_id
            )
        )
        if row is None or not _has_field(row, "against_monthly_200"):
            return None
        return _breakout_comparisons(row)

    def save_breakout_comparisons(
        self, candidate_id: str, value: BreakoutSemanticComparisons
    ) -> BreakoutSemanticComparisons:
        self._insert(
            candidate_comparison_table,
            {
                "candidate_id": candidate_id,
                "strategy_specification_id": (value.candidate_specification_id),
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_2_COMPARISON_PERSISTENCE_CONFLICT",
        )
        return value

    def breakout_protocol(self, protocol_id: str) -> BreakoutProtocol | None:
        row = self._one(
            sa.select(candidate_protocol_table).where(
                candidate_protocol_table.c.protocol_id == protocol_id
            )
        )
        if row is None or not _has_field(row, "initial_state"):
            return None
        return _breakout_protocol(row)

    def save_breakout_protocol(self, value: BreakoutProtocol) -> BreakoutProtocol:
        self._insert(
            candidate_protocol_table,
            {
                "protocol_id": value.protocol_id,
                "campaign_id": value.campaign_id,
                "candidate_id": value.candidate_id,
                "protocol_hash": value.protocol_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_2_PROTOCOL_PERSISTENCE_CONFLICT",
        )
        return value

    def breakout_protocols(self) -> tuple[BreakoutProtocol, ...]:
        return tuple(
            _breakout_protocol(row)
            for row in self._rows(
                sa.select(candidate_protocol_table).order_by(candidate_protocol_table.c.protocol_id)
            )
            if _has_field(row, "initial_state")
        )

    def breakout_run(self, run_id: str) -> BreakoutRun | None:
        row = self._one(
            sa.select(candidate_run_table).where(candidate_run_table.c.run_id == run_id)
        )
        if row is None or not _has_field(row, "initial_state"):
            return None
        return _breakout_run(row)

    def save_breakout_run(self, value: BreakoutRun) -> BreakoutRun:
        self._insert(
            candidate_run_table,
            {
                "run_id": value.run_id,
                "campaign_id": value.campaign_id,
                "candidate_id": value.candidate_id,
                "protocol_id": value.protocol_id,
                "snapshot_id": value.snapshot_id,
                "result_hash": value.result_hash,
                "configuration_hash": value.configuration_hash,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_2_RUN_PERSISTENCE_CONFLICT",
        )
        return value

    def breakout_runs(self) -> tuple[BreakoutRun, ...]:
        return tuple(
            _breakout_run(row)
            for row in self._rows(
                sa.select(candidate_run_table).order_by(candidate_run_table.c.run_id)
            )
            if _has_field(row, "initial_state")
        )

    def breakout_outcome(self, outcome_id: str) -> CandidateOutcome | None:
        row = self._one(
            sa.select(candidate_outcome_table).where(
                candidate_outcome_table.c.outcome_id == outcome_id
            )
        )
        if row is None:
            return None
        return _outcome(row) if self.breakout_run(str(row["run_id"])) is not None else None

    def breakout_outcome_for_run(self, run_id: str) -> CandidateOutcome | None:
        if self.breakout_run(run_id) is None:
            return None
        row = self._one(
            sa.select(candidate_outcome_table).where(candidate_outcome_table.c.run_id == run_id)
        )
        return _outcome(row) if row is not None else None

    def save_breakout_outcome(self, value: CandidateOutcome) -> CandidateOutcome:
        self._insert(
            candidate_outcome_table,
            {
                "outcome_id": value.outcome_id,
                "run_id": value.run_id,
                "decision": value.decision.value,
                "paper_eligible": False,
                "live_eligible": False,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_2_OUTCOME_PERSISTENCE_CONFLICT",
        )
        return value

    def breakout_outcomes(self) -> tuple[CandidateOutcome, ...]:
        run_ids = {item.run_id for item in self.breakout_runs()}
        return tuple(
            _outcome(row)
            for row in self._rows(
                sa.select(candidate_outcome_table).order_by(candidate_outcome_table.c.outcome_id)
            )
            if row["run_id"] in run_ids
        )

    def breakout_dossier(self, dossier_id: str) -> BreakoutDossier | None:
        row = self._one(
            sa.select(candidate_dossier_table).where(
                candidate_dossier_table.c.dossier_id == dossier_id
            )
        )
        if row is None or not _has_field(row, "percentage_windows_improving_monthly_200"):
            return None
        return _breakout_dossier(row)

    def save_breakout_dossier(self, value: BreakoutDossier) -> BreakoutDossier:
        self._insert(
            candidate_dossier_table,
            {
                "dossier_id": value.dossier_id,
                "campaign_id": value.campaign_id,
                "candidate_id": value.candidate_id,
                "protocol_id": value.protocol_id,
                "membership_hash": value.membership_hash,
                "passing_windows": value.passing_windows,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_2_DOSSIER_PERSISTENCE_CONFLICT",
        )
        return value

    def breakout_dossiers(self) -> tuple[BreakoutDossier, ...]:
        return tuple(
            _breakout_dossier(row)
            for row in self._rows(
                sa.select(candidate_dossier_table).order_by(candidate_dossier_table.c.dossier_id)
            )
            if _has_field(row, "percentage_windows_improving_monthly_200")
        )

    def breakout_nomination(self, evaluation_id: str) -> CandidateNominationEvaluation | None:
        row = self._one(
            sa.select(candidate_nomination_table).where(
                candidate_nomination_table.c.evaluation_id == evaluation_id
            )
        )
        if row is None:
            return None
        return (
            _nomination(row) if self.breakout_dossier(str(row["dossier_id"])) is not None else None
        )

    def breakout_nomination_for_dossier(
        self, dossier_id: str
    ) -> CandidateNominationEvaluation | None:
        if self.breakout_dossier(dossier_id) is None:
            return None
        row = self._one(
            sa.select(candidate_nomination_table).where(
                candidate_nomination_table.c.dossier_id == dossier_id
            )
        )
        return _nomination(row) if row is not None else None

    def save_breakout_nomination(
        self, value: CandidateNominationEvaluation
    ) -> CandidateNominationEvaluation:
        self._insert(
            candidate_nomination_table,
            {
                "evaluation_id": value.evaluation_id,
                "dossier_id": value.dossier_id,
                "campaign_id": value.campaign_id,
                "candidate_id": value.candidate_id,
                "eligible": value.eligible,
                "paper_eligible": False,
                "live_eligible": False,
                "payload": _payload(value),
                "canonical_hash": value.canonical_hash,
                "authority": "NONE",
            },
            "CRYPTO_CAMPAIGN_CANDIDATE_2_NOMINATION_PERSISTENCE_CONFLICT",
        )
        return value

    def breakout_nominations(
        self,
    ) -> tuple[CandidateNominationEvaluation, ...]:
        dossier_ids = {item.dossier_id for item in self.breakout_dossiers()}
        return tuple(
            _nomination(row)
            for row in self._rows(
                sa.select(candidate_nomination_table).order_by(
                    candidate_nomination_table.c.evaluation_id
                )
            )
            if row["dossier_id"] in dossier_ids
        )


__all__ = ["PostgresCryptoCampaignCandidateRepository", "metadata"]
