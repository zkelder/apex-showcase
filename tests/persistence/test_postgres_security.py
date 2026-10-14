from __future__ import annotations

from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from apex.adapters.broad_equity_postgres import metadata as broad_equity_metadata
from apex.adapters.coinbase_read_only_postgres import coinbase_read_only_metadata
from apex.adapters.corporate_calibration_depth_postgres import (
    metadata as corporate_calibration_depth_metadata,
)
from apex.adapters.corporate_fundamentals_postgres import (
    metadata as corporate_fundamentals_metadata,
)
from apex.adapters.corporate_market_research_postgres import (
    metadata as corporate_market_research_metadata,
)
from apex.adapters.corporate_regime_followup_postgres import (
    metadata as corporate_regime_followup_metadata,
)
from apex.adapters.counterfactual_followup_postgres import (
    metadata as counterfactual_followup_metadata,
)
from apex.adapters.counterfactual_ledger_postgres import (
    metadata as counterfactual_ledger_metadata,
)
from apex.adapters.counterfactual_outcome_postgres import (
    metadata as counterfactual_outcome_metadata,
)
from apex.adapters.cross_market_research_postgres import (
    metadata as cross_market_research_metadata,
)
from apex.adapters.cross_venue_allocation_postgres import (
    metadata as cross_venue_allocation_metadata,
)
from apex.adapters.crypto_campaign_candidate_postgres import (
    metadata as crypto_campaign_candidate_metadata,
)
from apex.adapters.crypto_campaign_generic_postgres import (
    metadata as crypto_campaign_generic_metadata,
)
from apex.adapters.crypto_campaign_postgres import metadata as crypto_campaign_metadata
from apex.adapters.crypto_challenger_postgres import metadata as crypto_challenger_metadata
from apex.adapters.crypto_dossier_postgres import metadata as crypto_dossier_metadata
from apex.adapters.crypto_market_pulse_postgres import (
    metadata as crypto_market_pulse_metadata,
)
from apex.adapters.crypto_paper_postgres import metadata as crypto_paper_metadata
from apex.adapters.crypto_research_postgres import metadata as crypto_research_metadata
from apex.adapters.crypto_stateful_research_postgres import (
    metadata as crypto_stateful_research_metadata,
)
from apex.adapters.crypto_universe_postgres import metadata as crypto_universe_metadata
from apex.adapters.evidence_recovery_postgres import metadata as evidence_recovery_metadata
from apex.adapters.external_strategy_research_postgres import (
    metadata as external_strategy_research_metadata,
)
from apex.adapters.genuine_multi_asset_history_postgres import (
    metadata as genuine_multi_asset_history_metadata,
)
from apex.adapters.historical_market_data_postgres import (
    metadata as historical_market_data_metadata,
)
from apex.adapters.historical_replay_postgres import (
    metadata as historical_replay_metadata,
)
from apex.adapters.investment_mandate_postgres import (
    metadata as investment_mandate_metadata,
)
from apex.adapters.kalshi_baseline_quote_postgres import (
    metadata as kalshi_baseline_quote_metadata,
)
from apex.adapters.kalshi_operating_postgres import (
    metadata as kalshi_operating_metadata,
)
from apex.adapters.kalshi_read_only_postgres import kalshi_read_only_metadata
from apex.adapters.kalshi_resolution_postgres import metadata as kalshi_resolution_metadata
from apex.adapters.live_market_observation_postgres import (
    metadata as live_market_observation_metadata,
)
from apex.adapters.live_preflight_postgres import live_preflight_metadata
from apex.adapters.live_readiness_postgres import live_readiness_metadata
from apex.adapters.operating_cycle_postgres import (
    metadata as operating_cycle_metadata,
)
from apex.adapters.operational_reliability_postgres import (
    operational_reliability_metadata,
)
from apex.adapters.options_intelligence_postgres import (
    metadata as options_intelligence_metadata,
)
from apex.adapters.options_quote_intelligence_postgres import (
    metadata as options_quote_intelligence_metadata,
)
from apex.adapters.options_research_operations_postgres import (
    metadata as options_research_operations_metadata,
)
from apex.adapters.options_structures_postgres import (
    metadata as options_structures_metadata,
)
from apex.adapters.performance_attribution_postgres import (
    metadata as performance_attribution_metadata,
)
from apex.adapters.portfolio_state_postgres import (
    metadata as portfolio_state_metadata,
)
from apex.adapters.postgres import (
    EVIDENCE_REVIEW_TABLES,
    PERSISTENCE_TABLE_NAMES,
    PostgresSettings,
    autopilot_metadata,
    batch_learning_metadata,
    candidate_generation_metadata,
    capture_metadata,
    comparison_metadata,
    complement_metadata,
    durable_metadata,
    dynamics_metadata,
    event_discovery_metadata,
    external_paper_metadata,
    history_metadata,
    market_discovery_metadata,
    metadata,
    microstructure_metadata,
    multi_strategy_metadata,
    operator_metadata,
    orderbook_history_metadata,
    paper_metadata,
    portfolio_intelligence_metadata,
    real_securities_data_metadata,
    research_integrity_metadata,
    securities_baseline_metadata,
    securities_daily_direction_dataset_metadata,
    securities_ensemble_metadata,
    securities_metadata,
    securities_paper_metadata,
    securities_strategy_intelligence_metadata,
    shadow_operations_metadata,
    strategy_lab_metadata,
    strategy_shadow_metadata,
    sustained_shadow_metadata,
    threshold_ladder_metadata,
)
from apex.adapters.research_calibration_postgres import (
    metadata as research_calibration_metadata,
)
from apex.adapters.research_calibration_v2_postgres import (
    metadata as research_calibration_v2_metadata,
)
from apex.adapters.research_methodology_postgres import (
    metadata as research_methodology_metadata,
)
from apex.adapters.research_operations_postgres import (
    metadata as research_operations_metadata,
)
from apex.adapters.research_replication_postgres import (
    metadata as research_replication_metadata,
)
from apex.adapters.securities_structure_outcome_postgres import (
    metadata as securities_structure_outcome_metadata,
)
from apex.adapters.security_identity_postgres import (
    metadata as security_identity_metadata,
)
from apex.adapters.security_reference_postgres import (
    metadata as security_reference_metadata,
)
from apex.adapters.shared_opportunity_postgres import (
    metadata as shared_opportunity_metadata,
)
from apex.adapters.strategy_evaluation_history_postgres import (
    metadata as strategy_evaluation_history_metadata,
)
from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
    metadata as strategy_paper_trial_lifecycle_metadata,
)
from apex.adapters.strategy_trial_outcome_assessment_postgres import (
    metadata as strategy_trial_outcome_assessment_metadata,
)
from apex.adapters.strategy_version_provenance_postgres import (
    metadata as strategy_version_provenance_metadata,
)
from apex.adapters.total_market_context_postgres import (
    metadata as total_market_context_metadata,
)
from apex.application.authorize_order import (
    AuthorizeAndReserveCommand,
    PersistedRiskRejection,
)
from apex.domain.decisions import OpportunityProposal, RestingLimitProposal
from apex.domain.values import Price

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]


def _command(artifacts, client_order_id: str) -> AuthorizeAndReserveCommand:
    return AuthorizeAndReserveCommand(
        campaign_id=artifacts.inputs.campaign_id,
        proposal_id=artifacts.inputs.proposal.proposal_id,
        allocation_id=artifacts.inputs.allocation.allocation_id,
        client_order_id=client_order_id,
        authorization_at=artifacts.authorization_at,
    )


EVENT_INSERT = sa.text(
    "INSERT INTO execution_event "
    "(event_id, campaign_id, order_id, sequence, event_type, occurred_at, "
    "canonical_payload_hash, created_at) VALUES "
    "(:event_id, :campaign_id, :order_id, :sequence, :event_type, :occurred_at, "
    ":canonical_payload_hash, :created_at)"
)


def _event_values(
    *,
    event_id: str,
    campaign_id: str,
    order_id: str,
    sequence: int,
    event_type: str,
    occurred_at,
    canonical_payload_hash: str = "a" * 64,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "campaign_id": campaign_id,
        "order_id": order_id,
        "sequence": sequence,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "canonical_payload_hash": canonical_payload_hash,
        "created_at": occurred_at,
    }


def _assert_write_rejected(engine: Engine, statement, parameters: dict[str, object]) -> None:
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(statement, parameters)
        transaction.rollback()


def _resting_artifacts(artifacts):
    proposal = artifacts.inputs.proposal
    proposal_fields = {
        field.name: getattr(proposal, field.name) for field in fields(OpportunityProposal)
    }
    resting_proposal = RestingLimitProposal(
        **proposal_fields,
        limit_price=Price("0.39", proposal.executable_ask.tick),
        order_expires_at=artifacts.authorization_at + timedelta(minutes=5),
    )
    return replace(
        artifacts,
        inputs=replace(artifacts.inputs, proposal=resting_proposal),
    )


def _open_resting_order(
    engine: Engine,
    artifacts,
    result,
    *,
    event_id: str,
) -> None:
    opened_at = artifacts.authorization_at + timedelta(seconds=1)
    with engine.begin() as connection:
        connection.execute(
            EVENT_INSERT,
            _event_values(
                event_id=event_id,
                campaign_id=artifacts.inputs.campaign_id,
                order_id=result.order.order_id,
                sequence=1,
                event_type="OPENED",
                occurred_at=opened_at,
            ),
        )
        connection.execute(
            sa.text(
                "UPDATE \"order\" SET state = 'OPEN', last_execution_sequence = 1, "
                "row_version = 2 WHERE order_id = :order_id"
            ),
            {"order_id": result.order.order_id},
        )
        connection.execute(
            sa.text("UPDATE risk_reservation SET state = 'OPEN' WHERE order_id = :order_id"),
            {"order_id": result.order.order_id},
        )


def test_schema_has_no_approximate_authoritative_numeric_or_scope_expansion() -> None:
    for schema_metadata in (
        metadata,
        durable_metadata,
        strategy_lab_metadata,
        securities_baseline_metadata,
        securities_metadata,
        securities_paper_metadata,
        research_integrity_metadata,
        candidate_generation_metadata,
        batch_learning_metadata,
        crypto_research_metadata,
        crypto_dossier_metadata,
        crypto_challenger_metadata,
        crypto_campaign_metadata,
        crypto_campaign_candidate_metadata,
        crypto_campaign_generic_metadata,
        crypto_universe_metadata,
        crypto_market_pulse_metadata,
        crypto_paper_metadata,
        kalshi_operating_metadata,
        cross_venue_allocation_metadata,
        performance_attribution_metadata,
        research_operations_metadata,
        crypto_stateful_research_metadata,
        live_readiness_metadata,
        operational_reliability_metadata,
        coinbase_read_only_metadata,
        kalshi_read_only_metadata,
        live_preflight_metadata,
        security_reference_metadata,
        broad_equity_metadata,
        options_intelligence_metadata,
        security_identity_metadata,
        historical_market_data_metadata,
        historical_replay_metadata,
        research_methodology_metadata,
        genuine_multi_asset_history_metadata,
        cross_market_research_metadata,
        research_calibration_metadata,
        research_calibration_v2_metadata,
        research_replication_metadata,
        options_quote_intelligence_metadata,
        options_structures_metadata,
        options_research_operations_metadata,
        corporate_fundamentals_metadata,
        corporate_market_research_metadata,
        corporate_calibration_depth_metadata,
        corporate_regime_followup_metadata,
        portfolio_state_metadata,
        investment_mandate_metadata,
        counterfactual_ledger_metadata,
        counterfactual_followup_metadata,
        counterfactual_outcome_metadata,
        kalshi_baseline_quote_metadata,
        securities_structure_outcome_metadata,
        live_market_observation_metadata,
        kalshi_resolution_metadata,
        external_strategy_research_metadata,
        strategy_evaluation_history_metadata,
        strategy_paper_trial_lifecycle_metadata,
        strategy_version_provenance_metadata,
        strategy_trial_outcome_assessment_metadata,
        evidence_recovery_metadata,
    ):
        for table in schema_metadata.tables.values():
            for column in table.columns:
                assert not isinstance(column.type, (sa.Float, sa.REAL))

    base_names = set(metadata.tables)
    durable_overlay_names = set(durable_metadata.tables)
    discovery_names = set(market_discovery_metadata.tables)
    event_discovery_names = set(event_discovery_metadata.tables)
    comparison_names = set(comparison_metadata.tables)
    history_names = set(history_metadata.tables)
    complement_names = set(complement_metadata.tables)
    orderbook_history_names = set(orderbook_history_metadata.tables)
    capture_names = set(capture_metadata.tables)
    threshold_names = set(threshold_ladder_metadata.tables)
    autopilot_names = set(autopilot_metadata.tables)
    paper_names = set(paper_metadata.tables)
    operator_names = set(operator_metadata.tables)
    strategy_shadow_names = set(strategy_shadow_metadata.tables)
    microstructure_names = set(microstructure_metadata.tables)
    dynamics_names = set(dynamics_metadata.tables)
    multi_strategy_names = set(multi_strategy_metadata.tables)
    shadow_operations_names = set(shadow_operations_metadata.tables)
    evidence_review_names = {
        "strategy_evidence_review_policy",
        "strategy_evidence_snapshot",
        "evidence_criterion_result",
        "strategy_evidence_review",
        "paper_trial_authorization",
        "paper_trial_lineage",
    }
    strategy_lab_names = {
        "strategy_lab_feature_schema",
        "strategy_lab_label_definition",
        "strategy_lab_dataset_snapshot",
        "strategy_lab_training_run",
        "strategy_lab_walk_forward_evaluation",
        "strategy_lab_model_candidate",
        "strategy_lab_operator_action",
        "strategy_lab_shadow_review",
        "strategy_lab_model_deployment",
        "strategy_lab_inference_batch",
        "strategy_lab_forward_prediction",
        "strategy_lab_forward_evaluation",
        "strategy_lab_champion_assignment",
        "strategy_lab_challenger_evaluation",
    }
    securities_names = {
        "campaign",
        "securities_instrument",
        "securities_market_data_batch",
        "securities_calendar_session",
        "securities_bar_evidence",
        "securities_corporate_action_evidence",
        "securities_strategy_specification",
        "securities_research_run",
        "securities_signal",
        "external_securities_engine_run",
        "securities_research_conformance_report",
    }
    securities_paper_names = {
        "campaign",
        "paper_trial_authorization",
        "securities_market_data_batch",
        "securities_strategy_specification",
        "securities_signal",
        "securities_instrument",
        "securities_bar_evidence",
        "securities_corporate_action_evidence",
        "ledger_journal",
        "securities_execution_policy",
        "securities_paper_run",
        "securities_paper_opportunity",
        "securities_paper_authorization",
        "securities_paper_order",
        "securities_fill_evidence",
        "securities_position_state",
        "securities_mark_evidence",
        "securities_corporate_action_application",
        "securities_exit_decision",
        "securities_execution_lineage",
        "external_securities_paper_run",
        "securities_paper_conformance_report",
    }
    securities_baseline_names = {
        "canonical_securities_baseline",
        "securities_simulation_run",
        "securities_simulator_conformance_report",
        "securities_baseline_scorecard",
        "strategy_lab_baseline_comparison",
    }
    research_integrity_names = {
        "research_experiment_family",
        "research_trial_attempt",
        "research_return_series_artifact",
        "research_metric_snapshot",
        "research_selection_event",
        "research_cross_validation_partition",
        "research_multiple_testing_evaluation",
    }
    candidate_generation_names = {
        "automated_candidate_search_plan",
        "automated_candidate_search_run",
        "automated_candidate_trial_binding",
        "mlflow_mirror_workspace",
        "mlflow_mirror_record",
        "candidate_generation_decision",
    }
    batch_learning_names = {
        "batch_learning_plan",
        "batch_learning_evidence_window",
        "batch_learning_cycle",
        "batch_learning_cycle_binding",
        "forward_shadow_evaluation_window",
        "batch_learning_decision",
    }
    real_securities_names = set(real_securities_data_metadata.tables)
    post_0030_names = {
        *securities_daily_direction_dataset_metadata.tables,
        *securities_strategy_intelligence_metadata.tables,
        *securities_ensemble_metadata.tables,
        *sustained_shadow_metadata.tables,
        *portfolio_intelligence_metadata.tables,
        *external_paper_metadata.tables,
        "research_hypothesis_version",
        "research_observation_capture",
        "research_observation",
        "research_capture_checkpoint",
        "research_dataset_snapshot",
        "research_experiment_run",
        "research_experiment_outcome",
        *crypto_research_metadata.tables,
        *crypto_dossier_metadata.tables,
        *crypto_challenger_metadata.tables,
        *crypto_campaign_metadata.tables,
        *crypto_campaign_candidate_metadata.tables,
        *crypto_campaign_generic_metadata.tables,
        *crypto_universe_metadata.tables,
        *crypto_market_pulse_metadata.tables,
        *shared_opportunity_metadata.tables,
        *total_market_context_metadata.tables,
        *operating_cycle_metadata.tables,
        *crypto_paper_metadata.tables,
        *kalshi_operating_metadata.tables,
        *cross_venue_allocation_metadata.tables,
        *performance_attribution_metadata.tables,
        *research_operations_metadata.tables,
        *crypto_stateful_research_metadata.tables,
        *live_readiness_metadata.tables,
        *operational_reliability_metadata.tables,
        *coinbase_read_only_metadata.tables,
        *kalshi_read_only_metadata.tables,
        *live_preflight_metadata.tables,
        *security_reference_metadata.tables,
        *broad_equity_metadata.tables,
        *options_intelligence_metadata.tables,
        *security_identity_metadata.tables,
        *historical_market_data_metadata.tables,
        *historical_replay_metadata.tables,
        *research_methodology_metadata.tables,
        *genuine_multi_asset_history_metadata.tables,
        *cross_market_research_metadata.tables,
        *research_calibration_metadata.tables,
        *research_calibration_v2_metadata.tables,
        *research_replication_metadata.tables,
        *options_quote_intelligence_metadata.tables,
        *options_structures_metadata.tables,
        *options_research_operations_metadata.tables,
        *corporate_fundamentals_metadata.tables,
        *corporate_market_research_metadata.tables,
        *corporate_calibration_depth_metadata.tables,
        *corporate_regime_followup_metadata.tables,
        *portfolio_state_metadata.tables,
        *investment_mandate_metadata.tables,
        *counterfactual_ledger_metadata.tables,
        *counterfactual_followup_metadata.tables,
        *counterfactual_outcome_metadata.tables,
        *kalshi_baseline_quote_metadata.tables,
        *securities_structure_outcome_metadata.tables,
        *live_market_observation_metadata.tables,
        *kalshi_resolution_metadata.tables,
        *external_strategy_research_metadata.tables,
        *strategy_evaluation_history_metadata.tables,
        *strategy_paper_trial_lifecycle_metadata.tables,
        *strategy_version_provenance_metadata.tables,
        *strategy_trial_outcome_assessment_metadata.tables,
        *evidence_recovery_metadata.tables,
    }
    declared_names = set(PERSISTENCE_TABLE_NAMES)
    assert "execution_event" not in base_names
    assert durable_overlay_names == {
        "opportunity_proposal",
        "order",
        "risk_reservation",
        "execution_event",
        "position",
        "settlement_fact",
        "position_settlement",
    }
    assert discovery_names == {
        "provider_ingestion_run",
        "raw_provider_response",
        "normalized_market_snapshot",
        "shadow_market_evaluation",
    }
    assert event_discovery_names == {
        "normalized_event_snapshot",
        "event_market_relationship",
        "consistency_evaluation",
    }
    assert comparison_names == {
        "scan_comparison",
        "market_snapshot_delta",
        "event_consistency_transition",
    }
    assert history_names == {"event_history_analysis", "event_history_observation"}
    assert complement_names == {
        "series_fee_snapshot",
        "market_orderbook_snapshot",
        "market_orderbook_level",
        "binary_complement_evaluation",
        "binary_complement_leg",
    }
    assert orderbook_history_names == {
        "orderbook_history_analysis",
        "orderbook_history_observation",
    }
    assert capture_names == {"orderbook_capture_session", "orderbook_capture_observation"}
    assert threshold_names == {
        "threshold_ladder_scan",
        "threshold_market_relationship",
        "threshold_bundle_evaluation",
        "threshold_matched_slice",
    }
    assert autopilot_names == {
        "campaign",
        "campaign_operating_state",
        "campaign_control_event",
        "campaign_portfolio_policy",
        "strategy_governance",
        "autopilot_cycle",
        "autopilot_strategy_invocation",
        "opportunity_economics",
        "autopilot_opportunity_assessment",
        "shadow_portfolio_decision",
    }
    assert paper_names == {
        "campaign",
        "autopilot_cycle",
        "autopilot_opportunity_assessment",
        "order",
        "position",
        "paper_cycle_authority",
        "paper_execution_policy",
        "paper_execution_evidence",
        "paper_execution_level",
        "paper_execution",
        "paper_position_lineage",
        "paper_position_mark",
    }
    assert operator_names == {"campaign", "campaign_operator_metadata", "operator_action"}
    assert strategy_shadow_names == {
        "campaign",
        "strategy_governance",
        "operator_action",
        "autopilot_cycle",
        "autopilot_strategy_invocation",
        "normalized_market_snapshot",
        "settlement_fact",
        "provider_ingestion_run",
        "strategy_authority_decision",
        "shadow_prediction",
        "shadow_outcome_evaluation",
        "shadow_observation_session",
        "shadow_observation_iteration",
    }
    assert microstructure_names == {
        "market_orderbook_snapshot",
        "microstructure_feature_snapshot",
        "microstructure_prediction_feature",
        "normalized_market_snapshot",
        "shadow_prediction",
        "strategy_governance",
    }
    assert dynamics_names == {
        "market_dynamics_feature_snapshot",
        "market_dynamics_prediction_feature",
        "normalized_market_snapshot",
        "shadow_prediction",
        "strategy_governance",
    }
    assert multi_strategy_names == {
        "campaign",
        "normalized_market_snapshot",
        "multi_strategy_shadow_cycle",
        "multi_strategy_shadow_session",
        "multi_strategy_shadow_session_iteration",
        "multi_strategy_shadow_invocation",
        "multi_strategy_shadow_cohort_member",
        "cross_strategy_prediction_comparison",
        "strategy_conflict",
        "shadow_capital_allocation",
    }
    assert shadow_operations_names == {
        "campaign",
        "shadow_prediction",
        "shadow_evidence_plan",
        "shadow_evidence_cohort_member",
        "shadow_evidence_iteration",
        "shadow_worker_lease",
        "public_outcome_fact",
        "shadow_forward_evaluation",
        "performance_evaluation_run",
    }
    assert {table.name for table in EVIDENCE_REVIEW_TABLES} == evidence_review_names
    assert set(strategy_lab_metadata.tables) == strategy_lab_names
    assert set(securities_metadata.tables) == securities_names
    assert set(securities_paper_metadata.tables) == securities_paper_names
    assert set(securities_baseline_metadata.tables) == securities_baseline_names
    assert set(research_integrity_metadata.tables) == research_integrity_names
    assert candidate_generation_names.issubset(candidate_generation_metadata.tables)
    assert batch_learning_names.issubset(batch_learning_metadata.tables)
    assert declared_names == {
        *base_names,
        "execution_event",
        "position",
        "settlement_fact",
        "position_settlement",
        *discovery_names,
        *event_discovery_names,
        *comparison_names,
        *history_names,
        *complement_names,
        *orderbook_history_names,
        *capture_names,
        *threshold_names,
        *autopilot_names,
        *paper_names,
        *operator_names,
        *strategy_shadow_names,
        *microstructure_names,
        *dynamics_names,
        *multi_strategy_names,
        *shadow_operations_names,
        *evidence_review_names,
        *strategy_lab_names,
        *securities_names,
        *securities_paper_names,
        *securities_baseline_names,
        *research_integrity_names,
        *candidate_generation_names,
        *batch_learning_names,
        *real_securities_names,
        *post_0030_names,
    }
    assert {name for name in declared_names if "execution" in name} == {
        "execution_event",
        "paper_execution_policy",
        "paper_execution_evidence",
        "paper_execution_level",
        "paper_execution",
        "securities_execution_policy",
        "securities_execution_lineage",
        "external_paper_internal_execution_binding",
        "crypto_execution_history",
        "execution_control_event",
        "cross_market_hypothesis_execution",
        "corporate_market_hypothesis_execution",
    }
    assert {name for name in declared_names if "settlement" in name} == {
        "settlement_fact",
        "position_settlement",
    }
    assert {name for name in declared_names if "provider" in name} == {
        "apex_crypto_paper_provider_contract",
        "provider_account_capability_snapshot",
        "provider_credential_reference",
        "provider_ingestion_run",
        "raw_provider_response",
        "securities_provider_capability_snapshot",
        "securities_provider_configuration",
        "securities_provider_fetch_operation",
        "provider_preview_approval",
        "provider_preview_capture",
        "provider_preview_attempt",
    }
    assert {name for name in declared_names if "broker" in name} == {
        "external_paper_broker_evidence"
    }
    assert {
        name
        for name in declared_names
        if any(forbidden in name for forbidden in ("secret", "wallet", "credential"))
    } == {"provider_credential_reference"}


def test_settings_and_url_string_hide_password() -> None:
    settings = PostgresSettings(password="not-for-output")
    assert "not-for-output" not in repr(settings)
    assert "not-for-output" not in str(settings.sqlalchemy_url())
    assert "***" in str(settings.sqlalchemy_url())


def test_rejected_risk_cannot_be_given_a_reservation(
    postgres_engine: Engine,
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    initialize_campaign(campaign_id="campaign-rejected-fk")
    first = make_artifacts(
        "rejected-fk-first",
        campaign_id="campaign-rejected-fk",
        maximum_loss="600",
    )
    second = make_artifacts(
        "rejected-fk-second",
        campaign_id="campaign-rejected-fk",
        maximum_loss="600",
        authorization_at=first.authorization_at + timedelta(seconds=1),
    )
    service.persist_authorization_inputs(first.inputs)
    service.persist_authorization_inputs(second.inputs)
    service.authorize_and_reserve(_command(first, "client-rejected-fk-first"))
    rejected = service.authorize_and_reserve(_command(second, "client-rejected-fk-second"))
    assert isinstance(rejected, PersistedRiskRejection)

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO risk_reservation "
                    "(reservation_id, campaign_id, risk_decision_id, "
                    "risk_decision_accepted, order_id, exact_order_fingerprint, "
                    "maximum_loss, current_amount, state, canonical_payload_hash, created_at) "
                    "VALUES ('forbidden-reservation', 'campaign-rejected-fk', :risk_id, true, "
                    "'missing-order', :fingerprint, 1, 1, 'HELD', repeat('b', 64), now())"
                ),
                {
                    "risk_id": rejected.risk_decision.risk_decision_id,
                    "fingerprint": rejected.risk_decision.exact_order_fingerprint,
                },
            )
        transaction.rollback()


def test_authorization_rows_are_immutable_after_commit(
    postgres_engine: Engine,
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    initialize_campaign(campaign_id="campaign-immutable")
    artifacts = make_artifacts("immutable", campaign_id="campaign-immutable")
    service.persist_authorization_inputs(artifacts.inputs)
    result = service.authorize_and_reserve(_command(artifacts, "client-immutable"))
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(
                sa.text('UPDATE "order" SET row_version = 2 WHERE order_id = :order_id'),
                {"order_id": result.order.order_id},
            )
        transaction.rollback()


@pytest.mark.parametrize(
    ("case_name", "overrides"),
    (
        ("sequence-zero", {"sequence": 0}),
        ("unsupported-fill", {"event_type": "FILL"}),
        ("short-hash", {"canonical_payload_hash": "too-short"}),
        ("missing-campaign", {"campaign_id": "campaign-does-not-exist"}),
        ("missing-order", {"order_id": "order-does-not-exist"}),
    ),
)
def test_execution_event_constraints_reject_invalid_rows(
    case_name: str,
    overrides: dict[str, object],
    postgres_engine: Engine,
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    campaign_id = f"campaign-event-constraint-{case_name}"
    initialize_campaign(campaign_id=campaign_id)
    artifacts = make_artifacts(case_name, campaign_id=campaign_id)
    service.persist_authorization_inputs(artifacts.inputs)
    result = service.authorize_and_reserve(_command(artifacts, f"client-{case_name}"))
    values = _event_values(
        event_id=f"event-{case_name}",
        campaign_id=campaign_id,
        order_id=result.order.order_id,
        sequence=1,
        event_type="OPENED",
        occurred_at=artifacts.authorization_at + timedelta(seconds=1),
    )
    values.update(overrides)

    _assert_write_rejected(postgres_engine, EVENT_INSERT, values)
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM execution_event WHERE event_id = :event_id"),
                {"event_id": values["event_id"]},
            )
            == 0
        )


def test_execution_event_primary_and_order_sequence_uniqueness_are_enforced(
    postgres_engine: Engine,
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    campaign_id = "campaign-event-uniqueness"
    initialize_campaign(campaign_id=campaign_id)
    artifacts = make_artifacts("event-uniqueness", campaign_id=campaign_id)
    service.persist_authorization_inputs(artifacts.inputs)
    result = service.authorize_and_reserve(_command(artifacts, "client-event-uniqueness"))
    occurred_at = artifacts.authorization_at + timedelta(seconds=1)
    original = _event_values(
        event_id="event-unique-original",
        campaign_id=campaign_id,
        order_id=result.order.order_id,
        sequence=1,
        event_type="OPENED",
        occurred_at=occurred_at,
    )
    with postgres_engine.begin() as connection:
        connection.execute(EVENT_INSERT, original)

    duplicate_id = _event_values(
        event_id="event-unique-original",
        campaign_id=campaign_id,
        order_id=result.order.order_id,
        sequence=2,
        event_type="EXPIRED",
        occurred_at=occurred_at + timedelta(seconds=1),
    )
    _assert_write_rejected(postgres_engine, EVENT_INSERT, duplicate_id)

    duplicate_order_sequence = _event_values(
        event_id="event-unique-sequence-conflict",
        campaign_id=campaign_id,
        order_id=result.order.order_id,
        sequence=1,
        event_type="OPENED",
        occurred_at=occurred_at,
    )
    _assert_write_rejected(
        postgres_engine,
        EVENT_INSERT,
        duplicate_order_sequence,
    )
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM execution_event WHERE order_id = :order_id"),
                {"order_id": result.order.order_id},
            )
            == 1
        )


@pytest.mark.parametrize(
    "invalid_write",
    (
        "order-lifecycle",
        "reservation-current-bound",
        "reservation-lifecycle",
        "reservation-release-time",
    ),
)
def test_order_and_reservation_constraints_reject_invalid_lifecycle_values(
    invalid_write: str,
    postgres_engine: Engine,
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    campaign_id = f"campaign-invalid-{invalid_write}"
    initialize_campaign(campaign_id=campaign_id)
    artifacts = make_artifacts(invalid_write, campaign_id=campaign_id)
    service.persist_authorization_inputs(artifacts.inputs)
    result = service.authorize_and_reserve(_command(artifacts, f"client-invalid-{invalid_write}"))
    order_id = result.order.order_id

    if invalid_write == "order-lifecycle":
        statement = sa.text(
            "UPDATE \"order\" SET state = 'EXPIRED', "
            "last_execution_sequence = 2, row_version = 3, "
            "reserved_maximum_loss = 0 WHERE order_id = :order_id"
        )
        parameters: dict[str, object] = {"order_id": order_id}
    elif invalid_write == "reservation-current-bound":
        statement = sa.text(
            "UPDATE risk_reservation SET current_amount = maximum_loss + 1 "
            "WHERE order_id = :order_id"
        )
        parameters = {"order_id": order_id}
    elif invalid_write == "reservation-lifecycle":
        statement = sa.text(
            "UPDATE risk_reservation SET current_amount = 0 WHERE order_id = :order_id"
        )
        parameters = {"order_id": order_id}
    else:
        statement = sa.text(
            "UPDATE risk_reservation SET released_at = :released_at WHERE order_id = :order_id"
        )
        parameters = {
            "order_id": order_id,
            "released_at": artifacts.authorization_at,
        }

    _assert_write_rejected(postgres_engine, statement, parameters)


def test_non_expiring_marketable_order_cannot_transition_to_open(
    postgres_engine: Engine,
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    campaign_id = "campaign-marketable-cannot-open"
    initialize_campaign(campaign_id=campaign_id)
    artifacts = make_artifacts("marketable-cannot-open", campaign_id=campaign_id)
    service.persist_authorization_inputs(artifacts.inputs)
    result = service.authorize_and_reserve(_command(artifacts, "client-marketable-cannot-open"))
    with postgres_engine.begin() as connection:
        connection.execute(
            EVENT_INSERT,
            _event_values(
                event_id="event-marketable-open",
                campaign_id=campaign_id,
                order_id=result.order.order_id,
                sequence=1,
                event_type="OPENED",
                occurred_at=artifacts.authorization_at + timedelta(seconds=1),
            ),
        )

    _assert_write_rejected(
        postgres_engine,
        sa.text(
            "UPDATE \"order\" SET state = 'OPEN', "
            "last_execution_sequence = 1, row_version = 2 "
            "WHERE order_id = :order_id"
        ),
        {"order_id": result.order.order_id},
    )
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text('SELECT state FROM "order" WHERE order_id = :order_id'),
                {"order_id": result.order.order_id},
            )
            == "READY"
        )


def test_wrong_expiration_and_release_times_cannot_advance_lifecycle(
    postgres_engine: Engine,
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    wrong_campaign_id = "campaign-wrong-expiration-time"
    initialize_campaign(campaign_id=wrong_campaign_id)
    wrong_artifacts = _resting_artifacts(
        make_artifacts("wrong-expiration-time", campaign_id=wrong_campaign_id)
    )
    service.persist_authorization_inputs(wrong_artifacts.inputs)
    wrong_result = service.authorize_and_reserve(
        _command(wrong_artifacts, "client-wrong-expiration-time")
    )
    _open_resting_order(
        postgres_engine,
        wrong_artifacts,
        wrong_result,
        event_id="event-wrong-time-opened",
    )
    wrong_expired_at = wrong_result.order.expires_at - timedelta(seconds=1)
    with postgres_engine.begin() as connection:
        connection.execute(
            EVENT_INSERT,
            _event_values(
                event_id="event-wrong-time-expired",
                campaign_id=wrong_campaign_id,
                order_id=wrong_result.order.order_id,
                sequence=2,
                event_type="EXPIRED",
                occurred_at=wrong_expired_at,
            ),
        )

    _assert_write_rejected(
        postgres_engine,
        sa.text(
            "UPDATE \"order\" SET state = 'EXPIRED', "
            "last_execution_sequence = 2, row_version = 3, "
            "reserved_maximum_loss = 0 WHERE order_id = :order_id"
        ),
        {"order_id": wrong_result.order.order_id},
    )
    _assert_write_rejected(
        postgres_engine,
        sa.text(
            "UPDATE risk_reservation SET state = 'RELEASED', current_amount = 0, "
            "released_at = :released_at WHERE order_id = :order_id"
        ),
        {
            "order_id": wrong_result.order.order_id,
            "released_at": wrong_expired_at,
        },
    )

    release_campaign_id = "campaign-wrong-release-time"
    initialize_campaign(campaign_id=release_campaign_id)
    release_artifacts = _resting_artifacts(
        make_artifacts("wrong-release-time", campaign_id=release_campaign_id)
    )
    service.persist_authorization_inputs(release_artifacts.inputs)
    release_result = service.authorize_and_reserve(
        _command(release_artifacts, "client-wrong-release-time")
    )
    _open_resting_order(
        postgres_engine,
        release_artifacts,
        release_result,
        event_id="event-release-time-opened",
    )
    correct_expired_at = release_result.order.expires_at
    with postgres_engine.begin() as connection:
        connection.execute(
            EVENT_INSERT,
            _event_values(
                event_id="event-release-time-expired",
                campaign_id=release_campaign_id,
                order_id=release_result.order.order_id,
                sequence=2,
                event_type="EXPIRED",
                occurred_at=correct_expired_at,
            ),
        )
        connection.execute(
            sa.text(
                "UPDATE \"order\" SET state = 'EXPIRED', "
                "last_execution_sequence = 2, row_version = 3, "
                "reserved_maximum_loss = 0 WHERE order_id = :order_id"
            ),
            {"order_id": release_result.order.order_id},
        )

    _assert_write_rejected(
        postgres_engine,
        sa.text(
            "UPDATE risk_reservation SET state = 'RELEASED', current_amount = 0, "
            "released_at = :released_at WHERE order_id = :order_id"
        ),
        {
            "order_id": release_result.order.order_id,
            "released_at": correct_expired_at + timedelta(seconds=1),
        },
    )


def test_execution_events_are_immutable_at_the_database_boundary(
    postgres_engine: Engine,
    service,
    initialize_campaign,
    make_artifacts,
) -> None:
    initialize_campaign(campaign_id="campaign-event-immutable")
    artifacts = make_artifacts("event-immutable", campaign_id="campaign-event-immutable")
    service.persist_authorization_inputs(artifacts.inputs)
    result = service.authorize_and_reserve(_command(artifacts, "client-event-immutable"))
    with postgres_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO execution_event "
                "(event_id, campaign_id, order_id, sequence, event_type, "
                "occurred_at, canonical_payload_hash, created_at) VALUES "
                "('event-immutable', :campaign_id, :order_id, 1, 'OPENED', "
                "now(), repeat('a', 64), now())"
            ),
            {
                "campaign_id": artifacts.inputs.campaign_id,
                "order_id": result.order.order_id,
            },
        )

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(
                sa.text(
                    "UPDATE execution_event SET event_type = 'EXPIRED' "
                    "WHERE event_id = 'event-immutable'"
                )
            )
        transaction.rollback()

    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(sa.exc.DBAPIError):
            connection.execute(
                sa.text("DELETE FROM execution_event WHERE event_id = 'event-immutable'")
            )
        transaction.rollback()


def test_new_persistence_code_contains_no_remote_or_live_execution_capability() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "src/apex/adapters/postgres.py",
            ROOT / "src/apex/adapters/postgres_unfilled.py",
            ROOT / "src/apex/application/authorize_order.py",
            ROOT / "src/apex/application/execute_unfilled_order.py",
            ROOT / "src/apex/application/execute_partial_fill_canceled_order.py",
            ROOT / "src/apex/application/settle_partial_fill_canceled_no.py",
            ROOT / "migrations/versions/0002_durable_execution_events.py",
            ROOT / "migrations/versions/0003_partial_fill_and_position.py",
            ROOT / "migrations/versions/0004_durable_no_settlement.py",
            ROOT / "migrations/versions/0005_durable_void_settlement.py",
        )
    ).lower()
    for forbidden in (
        "import requests",
        "import httpx",
        "import socket",
        "import subprocess",
        "urllib.request",
        "api_key",
        "private_key",
        "transaction_sign",
        'runtime_capability="live',
    ):
        assert forbidden not in combined


def test_database_contains_only_the_declared_persistence_tables(
    postgres_engine: Engine,
) -> None:
    tables = set(inspect(postgres_engine).get_table_names()) - {"alembic_version"}
    assert tables == set(PERSISTENCE_TABLE_NAMES)
