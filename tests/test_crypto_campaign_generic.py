from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from apex.adapters.crypto_campaign_generic_fixtures import (
    generic_fixture_acceptance,
)
from apex.application.crypto_campaign_generic import (
    GenericCampaignConflict,
    GenericCampaignWorkflow,
    InMemoryGenericCampaignRepository,
    campaign_policy_v1_finding,
    retained_strategy_compatibility,
)
from apex.domain.crypto_campaign_generic import (
    CandidateDefinitionSchema,
    ComparisonOperator,
    FeatureKind,
    GenericCandidateDefinition,
    GenericCandidateError,
    GenericPortfolio,
    MissingDataAction,
    PositionSizing,
    PositionState,
    RuleMode,
    candidate_definition_mapping,
    compile_candidate_definition,
    execute_compiled_plan,
    generic_metric,
)
from apex.domain.crypto_research import CostScenarioName


def _compatibility(index: int = 2) -> GenericCandidateDefinition:
    return retained_strategy_compatibility()[index].definition


def test_definition_round_trip_is_strict_and_deterministic() -> None:
    definition = _compatibility()
    mapping = candidate_definition_mapping(definition)

    retained = GenericCandidateDefinition.from_mapping(mapping)

    assert retained == definition
    assert retained.schema_version is CandidateDefinitionSchema.V1
    assert retained.authority == "NONE"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("schema_version", "GENERIC_CRYPTO_CANDIDATE_DEFINITION_V999", "UNSUPPORTED"),
        ("execution_timing", "SAME_CANDLE_CLOSE_TO_OPEN", "UNSUPPORTED"),
        ("position_sizing", "PARTIAL", "UNSUPPORTED"),
        ("product_id", "ETH-USD", "UNAUTHORIZED"),
    ),
)
def test_definition_rejects_unsupported_top_level_values(
    field: str, value: object, error: str
) -> None:
    mapping = candidate_definition_mapping(_compatibility())
    mapping[field] = value

    with pytest.raises(GenericCandidateError, match=error):
        GenericCandidateDefinition.from_mapping(mapping)


def test_definition_rejects_arbitrary_code_and_unknown_fields() -> None:
    mapping = candidate_definition_mapping(_compatibility())
    mapping["script"] = "import os"

    with pytest.raises(GenericCandidateError):
        GenericCandidateDefinition.from_mapping(mapping)


def test_definition_rejects_unsupported_feature_operator_and_lookback() -> None:
    feature_mapping = candidate_definition_mapping(_compatibility())
    feature_mapping["features"][0]["input_field"] = "high"  # type: ignore[index]
    with pytest.raises(GenericCandidateError, match="INPUT_UNSUPPORTED"):
        GenericCandidateDefinition.from_mapping(feature_mapping)

    operator_mapping = candidate_definition_mapping(_compatibility())
    operator_mapping["rules"][0]["operator"] = "EXECUTE_PYTHON"  # type: ignore[index]
    with pytest.raises(GenericCandidateError, match="ENUM_UNSUPPORTED"):
        GenericCandidateDefinition.from_mapping(operator_mapping)

    lookback_mapping = candidate_definition_mapping(_compatibility())
    lookback_mapping["features"][0]["observations"] = 10_000  # type: ignore[index]
    with pytest.raises(GenericCandidateError, match="LOOKBACK_UNBOUNDED"):
        GenericCandidateDefinition.from_mapping(lookback_mapping)


def test_definition_rejects_leverage_shorting_and_partial_sizing_fields() -> None:
    for forbidden in ("leverage", "shorting", "partial_sizing"):
        mapping = candidate_definition_mapping(_compatibility())
        mapping[forbidden] = True
        with pytest.raises(GenericCandidateError):
            GenericCandidateDefinition.from_mapping(mapping)


def test_metadata_does_not_change_semantics_but_economic_change_does() -> None:
    original = _compatibility()
    metadata = candidate_definition_mapping(original)
    metadata["definition_id"] = "metadata-only-definition-v1"
    metadata["candidate_id"] = "metadata-only-candidate-v1"
    metadata["hypothesis_id"] = "metadata-only-hypothesis"
    metadata["hypothesis_version_id"] = "metadata-only-hypothesis-v1"
    metadata["expected_mechanism"] = "Different bounded explanatory metadata."
    metadata_definition = GenericCandidateDefinition.from_mapping(metadata)
    economic = deepcopy(metadata)
    economic["features"][0]["observations"] = 199  # type: ignore[index]
    economic_definition = GenericCandidateDefinition.from_mapping(economic)

    assert metadata_definition.semantics_hash == original.semantics_hash
    assert metadata_definition.canonical_hash != original.canonical_hash
    assert economic_definition.semantics_hash != original.semantics_hash


def test_compiler_is_deterministic_and_metadata_insensitive() -> None:
    original = _compatibility()
    mapping = candidate_definition_mapping(original)
    mapping["definition_id"] = "equivalent-definition-v2"
    mapping["candidate_id"] = "equivalent-candidate-v2"
    equivalent = GenericCandidateDefinition.from_mapping(mapping)

    first = compile_candidate_definition(original)
    second = compile_candidate_definition(original)
    metadata_only = compile_candidate_definition(equivalent)

    assert first == second
    assert first.plan_id == metadata_only.plan_id
    assert first.plan_hash == metadata_only.plan_hash
    assert first.definition_id != metadata_only.definition_id
    assert first.authority == "NONE"


def test_invalid_stateful_transition_and_missing_behavior_are_rejected() -> None:
    stateful = _compatibility(3)
    invalid_rule = replace(
        stateful.rules[0],
        trigger_state=PositionState.CASH,
    )
    with pytest.raises(GenericCandidateError, match="STATEFUL_TRANSITIONS_INVALID"):
        GenericCandidateDefinition.create(
            definition_id=stateful.definition_id,
            campaign_id=stateful.campaign_id,
            candidate_id=stateful.candidate_id,
            hypothesis_id=stateful.hypothesis_id,
            hypothesis_version_id=stateful.hypothesis_version_id,
            strategy_specification_id=stateful.strategy_specification_id,
            strategy_version=stateful.strategy_version,
            family=stateful.family,
            expected_mechanism=stateful.expected_mechanism,
            approved_snapshot_ids=stateful.approved_snapshot_ids,
            validation_windows=stateful.validation_windows,
            cadence=stateful.cadence,
            initial_state=stateful.initial_state,
            rule_mode=RuleMode.STATEFUL_ENTRY_EXIT,
            features=stateful.features,
            rules=(invalid_rule, stateful.rules[1]),
        )

    missing_rule = replace(
        stateful.rules[1],
        missing_data_action=MissingDataAction.MARK_RUN_INCOMPLETE,
    )
    with pytest.raises(GenericCandidateError, match="STATEFUL_TRANSITIONS_INVALID"):
        GenericCandidateDefinition.create(
            definition_id=stateful.definition_id,
            campaign_id=stateful.campaign_id,
            candidate_id=stateful.candidate_id,
            hypothesis_id=stateful.hypothesis_id,
            hypothesis_version_id=stateful.hypothesis_version_id,
            strategy_specification_id=stateful.strategy_specification_id,
            strategy_version=stateful.strategy_version,
            family=stateful.family,
            expected_mechanism=stateful.expected_mechanism,
            approved_snapshot_ids=stateful.approved_snapshot_ids,
            validation_windows=stateful.validation_windows,
            cadence=stateful.cadence,
            initial_state=stateful.initial_state,
            rule_mode=RuleMode.STATEFUL_ENTRY_EXIT,
            features=stateful.features,
            rules=(stateful.rules[0], missing_rule),
        )


def test_supported_primitives_are_bounded() -> None:
    assert set(FeatureKind) == {
        FeatureKind.ROLLING_MEAN,
        FeatureKind.ROLLING_MINIMUM,
        FeatureKind.ROLLING_MAXIMUM,
        FeatureKind.POINT_TO_POINT_RETURN,
    }
    assert set(ComparisonOperator) == {
        ComparisonOperator.STRICTLY_GREATER_THAN,
        ComparisonOperator.STRICTLY_LESS_THAN,
        ComparisonOperator.GREATER_THAN_OR_EQUAL,
        ComparisonOperator.LESS_THAN_OR_EQUAL,
    }
    assert set(PositionState) == {PositionState.CASH, PositionState.LONG}
    assert set(PositionSizing) == {PositionSizing.FULL_LONG_OR_CASH}


def test_isolated_stateless_fixture_is_nomination_eligible() -> None:
    acceptance = generic_fixture_acceptance()
    report = acceptance.stateless

    assert report.nomination.eligible
    assert report.nomination.authority == "NONE"
    assert report.nomination.canonical_hash != "NONE"
    assert report.dossier.passing_windows == 2
    assert all(outcome.decision.value == "PROMOTION_CANDIDATE" for outcome in report.outcomes)
    assert all(run.complete for run in report.runs)
    assert all(
        decision.information_through < decision.execution_at
        for run in report.runs
        for decision in run.decisions
    )
    assert all(generic_metric(run, GenericPortfolio.CANDIDATE).days_long > 0 for run in report.runs)


def test_isolated_stateful_fixture_uses_one_executor_and_is_ineligible() -> None:
    acceptance = generic_fixture_acceptance()
    report = acceptance.stateful

    assert not report.nomination.eligible
    assert report.nomination.authority == "NONE"
    assert report.nomination.canonical_hash != "NONE"
    assert report.dossier.passing_windows == 0
    assert any(
        decision.prior_state is PositionState.CASH and decision.selected_state is PositionState.LONG
        for run in report.runs
        for decision in run.decisions
    )
    assert any(
        decision.prior_state is PositionState.LONG and decision.selected_state is PositionState.CASH
        for run in report.runs
        for decision in run.decisions
    )
    assert report.plan.compiler_id == "GENERIC_CRYPTO_CANDIDATE_COMPILER_V1"


def test_fixture_costs_metrics_and_baselines_are_retained() -> None:
    report = generic_fixture_acceptance().stateless

    for run in report.runs:
        diagnostic = generic_metric(
            run,
            GenericPortfolio.CANDIDATE,
            CostScenarioName.DIAGNOSTIC_ZERO_COST,
        )
        conservative = generic_metric(run, GenericPortfolio.CANDIDATE)
        cash = generic_metric(run, GenericPortfolio.CASH)
        buy_hold = generic_metric(run, GenericPortfolio.BUY_AND_HOLD)
        assert diagnostic.total_modeled_cost == 0
        assert conservative.total_modeled_cost > 0
        assert conservative.modeled_fees > conservative.modeled_slippage
        assert cash.net_return == 0
        assert buy_hold.maximum_drawdown > conservative.maximum_drawdown


def test_fixture_replay_idempotency_and_budget_accounting() -> None:
    acceptance = generic_fixture_acceptance()

    for report, replay_hashes, retry_hash in (
        (
            acceptance.stateless,
            acceptance.stateless_replay_hashes,
            acceptance.stateless_retry_hash,
        ),
        (
            acceptance.stateful,
            acceptance.stateful_replay_hashes,
            acceptance.stateful_retry_hash,
        ),
    ):
        assert replay_hashes == tuple(item.canonical_hash for item in report.runs)
        assert retry_hash == report.canonical_hash
        assert report.budget.consumed_candidates == 1
        assert report.budget.consumed_experiments == 2
        assert not report.holdout_accessed
        assert not report.network_request_performed
        assert report.authority == "NONE"


def test_registration_is_idempotent_and_duplicate_semantics_is_rejected() -> None:
    definition = _compatibility()
    repository = InMemoryGenericCampaignRepository()
    workflow = GenericCampaignWorkflow(repository)
    prior = (("prior-v1", "1" * 64),)

    first = workflow.register(definition=definition, prior_semantics=prior)
    retry = workflow.register(definition=definition, prior_semantics=prior)

    assert first[0] == retry[0]
    assert len(repository.consumed_candidate_ids()) == 1
    with pytest.raises(GenericCampaignConflict, match="DUPLICATE_SEMANTICS"):
        workflow.register(
            definition=definition,
            prior_semantics=(("same-economics-v1", definition.semantics_hash),),
        )
    assert len(repository.consumed_candidate_ids()) == 1


def test_executor_rejects_sealed_holdout_unapproved_snapshot_and_same_candle_use() -> None:
    acceptance = generic_fixture_acceptance()
    report = acceptance.stateless
    run = report.runs[0]
    definition = report.definition
    snapshot = report.protocol.snapshot_ids[0]

    assert all(decision.information_through < decision.execution_at for decision in run.decisions)
    with pytest.raises(GenericCandidateError, match="EXPOSURE_UNAPPROVED"):
        execute_compiled_plan(
            plan=report.plan,
            definition=definition,
            run_id="sealed-holdout-attempt-v1",
            protocol_id=report.protocol.protocol_id,
            snapshot_id=snapshot,
            snapshot_manifest_hash=report.protocol.snapshot_manifest_hashes[0],
            snapshot_exposure_state="SEALED",
            validation_start=run.validation_start,
            validation_end=run.validation_end,
            candles=(),
            executed_at=datetime(2030, 1, 3, tzinfo=UTC),
        )
    with pytest.raises(GenericCandidateError, match="SNAPSHOT_NOT_APPROVED"):
        execute_compiled_plan(
            plan=report.plan,
            definition=definition,
            run_id="unknown-snapshot-attempt-v1",
            protocol_id=report.protocol.protocol_id,
            snapshot_id="unknown-fixture-snapshot-v1",
            snapshot_manifest_hash="1" * 64,
            snapshot_exposure_state="FIXTURE",
            validation_start=run.validation_start,
            validation_end=run.validation_end,
            candles=(),
            executed_at=datetime(2030, 1, 3, tzinfo=UTC),
        )


def test_all_four_retained_families_have_read_only_compatibility_plans() -> None:
    projections = retained_strategy_compatibility()

    assert len(projections) == 4
    assert {item.historical_family for item in projections} == {
        "BTC_USD_DAILY_SMA_TREND",
        "BTC_USD_MONTHLY_252_ABSOLUTE_MOMENTUM",
        "BTC_USD_MONTHLY_200_OBSERVATION_PRICE_TREND",
        "BTC_USD_MONTHLY_STATEFUL_90_45_CLOSE_BREAKOUT",
    }
    assert all(item.economic_rules_representable for item in projections)
    assert all(
        item.historical_hash_claim
        == "ECONOMIC_EQUIVALENCE_ONLY_HISTORICAL_CANONICAL_HASH_NOT_REPLACED"
        for item in projections
    )
    assert all(item.authority == "NONE" for item in projections)


def test_retained_historical_semantics_hashes_are_preserved() -> None:
    assert tuple(item.historical_semantics_hash for item in retained_strategy_compatibility()) == (
        "2e028203df214ac1484dfd567ba9cc85e1cbecd1ba193aeed76700272551da2f",
        "5a98e1444f88fe5be0e0cb3438af3dfbf3f4c35c855479d36b442e7c76e9e6d6",
        "c76c9d1690880803c293d25fd104eebbd9c2bd20d1cf9f60fcbe0a7e9f19dd4f",
        "5b81c588665ba08d3a13e206035d270b729dcda69555576b7fe9380e73291c98",
    )


def test_campaign_policy_review_preserves_v1_and_recommends_future_version() -> None:
    finding = campaign_policy_v1_finding(
        campaign_question=(
            "Can a long-or-cash strategy produce non-negative conservative returns "
            "and lower drawdown than buy-and-hold across both windows?"
        ),
        campaign_canonical_hash="5e8e7df47bd624dace0c3d355b53dee6d1cd9dc801b00516f080a10bd2990902",
        reviewed_at=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert finding.reviewed_condition == "AT_LEAST_ONE_LONG_EXPOSURE_PERIOD_PER_WINDOW"
    assert finding.requires_future_policy_version
    assert "always-cash" in finding.degeneracy_risk
    assert "complete multi-window dossier" in finding.recommended_future_treatment
    assert finding.authority == "NONE"
