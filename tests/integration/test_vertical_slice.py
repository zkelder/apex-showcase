from __future__ import annotations

import inspect
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

import pytest

import apex.application.run_simulation as run_module
from apex import ENGINE_VERSION
from apex.adapters.canonical_report import (
    CanonicalReportRenderer,
    report_document,
)
from apex.adapters.fixtures import FixtureReader
from apex.adapters.simulator import DeterministicSimulator, SimulationError
from apex.application.interfaces import (
    InvariantRejectedResult,
    NoProposalResult,
    PartialFillCanceledExecutionScript,
    PartialFillCanceledResult,
    PartialFillCanceledSettledNoResult,
    PartialFillCanceledSettledVoidResult,
    PolicyRejectedResult,
    RunMetadata,
    RunOutcome,
    SettlementInputs,
    UnfilledExpiredResult,
    UnfilledExpiryExecutionScript,
)
from apex.application.run_simulation import (
    AtomicOrderBook,
    GoldenSimulation,
    SimulationConflictError,
    run_simulation,
)
from apex.cli import _decision_inputs, _execution_script, _settlement_inputs, demo_core
from apex.domain.campaign import (
    OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS,
    Campaign,
    CampaignPolicy,
    CampaignSnapshot,
    OperationalInvariants,
    active_campaign_risk,
)
from apex.domain.decisions import (
    INVARIANT_MARKET_DATA_STALE,
    POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED,
    CapitalAllocator,
    ReferenceEdgeStrategy,
    RestingLimitProposal,
    RiskKernel,
    Side,
    StaleMarketRiskDecision,
    StrategyEvaluation,
    StrategyOutcome,
    market_data_stale_explanation,
)
from apex.domain.execution import (
    AppliedPartialFillCanceledEvent,
    AppliedUnfilledOrderEvent,
    ClassifiedRuleBoundSettlementFact,
    ClassifiedSettledFilledPositionState,
    ExecutionEvent,
    ExecutionEventType,
    ExpiringLimitOrder,
    FilledPositionState,
    OrderState,
    ReservationState,
    RiskReservation,
    RuleBoundSettlementFact,
    SettledFilledPositionState,
    SettlementFact,
    SettlementOutcome,
    SettlementState,
    apply_partial_fill_canceled_event,
    apply_unfilled_order_event,
    create_reserved_order,
    settle_canceled_filled_void_position,
    settle_position,
)
from apex.domain.ledger import AccountCode, JournalType, PostingSide
from apex.domain.market import VoidContractRuleVersion
from apex.domain.values import (
    ContentHash,
    Money,
    Price,
    Quantity,
    ValueValidationError,
    canonical_json_bytes,
    content_digest,
)

EXPECTED_REPORT_SHA256 = "4894ae9e6de37af8190b36689faff639f7efafc26afba51901b19c5bb41c7ee2"
EXPECTED_NO_PROPOSAL_REPORT_SHA256 = (
    "6b50ba28cdab4630e84f83d8f5f1117668eb25a8d60f314275a5aadc73547e1f"
)
EXPECTED_POLICY_REJECTED_REPORT_SHA256 = (
    "0342bb592cf5158e7d8d7920aa91d9e5bd4b760f7538c3c18e0ab25275f0ed73"
)
EXPECTED_INVARIANT_REJECTED_REPORT_SHA256 = (
    "38af1172ec7db536da5e98ea56eb7849c110f56738186022433362408ab3e341"
)
EXPECTED_UNFILLED_EXPIRED_REPORT_SHA256 = (
    "6c3449f12080127227ad585f48e8738e4f97be7794f880cd33a154ef9e27017f"
)
EXPECTED_PARTIAL_FILL_CANCELED_REPORT_SHA256 = (
    "b9522c05abee6268bd36145e1c1696c94df033f2a6430791e6be34bc02663c21"
)
EXPECTED_PARTIAL_FILL_CANCELED_SETTLED_NO_REPORT_SHA256 = (
    "d236abfe89353b9f64af5472a56cf3c656f7e0803b31bd388e5f5fd773f98bef"
)
EXPECTED_PARTIAL_FILL_CANCELED_SETTLED_VOID_REPORT_SHA256 = (
    "7da55882273dd37cca512b176d82d9f03284aff9d8949206e8f48cfba6cd0c12"
)


class MustNotSimulate:
    def simulate(self, *_: object) -> tuple[()]:
        raise AssertionError("simulator must not run for NO_PROPOSAL")


def _must_not_load_settlement() -> SettlementInputs:
    raise AssertionError("settlement must not load for NO_PROPOSAL")


def _simulation() -> tuple[GoldenSimulation, FixtureReader]:
    reader = FixtureReader()
    fixture = reader.load_decision("golden")
    simulation = GoldenSimulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("golden")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000.00"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )
    return simulation, reader


def _render(simulation: GoldenSimulation) -> bytes:
    return CanonicalReportRenderer().render(report_document(simulation.finalize()))


def _no_proposal_simulation() -> GoldenSimulation:
    reader = FixtureReader()
    fixture = reader.load_decision("no-proposal")
    return GoldenSimulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("no-proposal")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000.00"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=MustNotSimulate(),
    )


def _policy_rejected_simulation() -> GoldenSimulation:
    reader = FixtureReader()
    fixture = reader.load_decision("policy-rejected")
    return GoldenSimulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("policy-rejected")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000.00"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=MustNotSimulate(),
    )


def _invariant_rejected_simulation() -> GoldenSimulation:
    reader = FixtureReader()
    fixture = reader.load_decision("invariant-rejected")
    return GoldenSimulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("invariant-rejected")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000.00"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=MustNotSimulate(),
    )


def _unfilled_expired_simulation() -> GoldenSimulation:
    reader = FixtureReader()
    fixture = reader.load_decision("unfilled-expired")
    return GoldenSimulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("unfilled-expired")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000.00"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )


def _partial_fill_canceled_simulation() -> GoldenSimulation:
    reader = FixtureReader()
    fixture = reader.load_decision("partial-fill-canceled")
    return GoldenSimulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("partial-fill-canceled")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        capital=Money("1000.00"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )


def _partial_fill_canceled_settled_no_simulation() -> tuple[GoldenSimulation, FixtureReader]:
    reader = FixtureReader()
    fixture = reader.load_decision("partial-fill-canceled-settled-no")
    return (
        GoldenSimulation(
            decision_inputs=_decision_inputs(fixture),
            execution_script=_execution_script(fixture),
            run_metadata=RunMetadata(
                fixture_manifest_hash=ContentHash(
                    reader.manifest_hash("partial-fill-canceled-settled-no")
                ),
                decision_file_hash=ContentHash(fixture.file_hash),
                logical_time=fixture.logical_time,
            ),
            capital=Money("1000.00"),
            seed=20260716,
            engine_version=ENGINE_VERSION,
            simulator=DeterministicSimulator(),
        ),
        reader,
    )


def _partial_fill_canceled_settled_void_simulation() -> tuple[GoldenSimulation, FixtureReader]:
    fixture_id = "partial-fill-canceled-settled-void"
    reader = FixtureReader()
    fixture = reader.load_decision(fixture_id)
    return (
        GoldenSimulation(
            decision_inputs=_decision_inputs(fixture),
            execution_script=_execution_script(fixture),
            run_metadata=RunMetadata(
                fixture_manifest_hash=ContentHash(reader.manifest_hash(fixture_id)),
                decision_file_hash=ContentHash(fixture.file_hash),
                logical_time=fixture.logical_time,
            ),
            capital=Money("1000.00"),
            seed=20260716,
            engine_version=ENGINE_VERSION,
            simulator=DeterministicSimulator(),
        ),
        reader,
    )


def _rehashed_rule_bound_fact(
    fact: RuleBoundSettlementFact,
    **changes: object,
) -> RuleBoundSettlementFact:
    values = {
        "fixture_id": changes.get("fixture_id", fact.fixture_id),
        "schema_version": changes.get("schema_version", fact.schema_version),
        "source_id": changes.get("source_id", fact.source_id),
        "instrument_id": changes.get("instrument_id", fact.instrument_id),
        "rule_version_id": changes.get("rule_version_id", fact.rule_version_id),
        "rule_content_hash": changes.get("rule_content_hash", fact.rule_content_hash),
        "resolution_source": changes.get("resolution_source", fact.resolution_source),
        "source_event_at": changes.get("source_event_at", fact.source_event_at),
        "available_at": changes.get("available_at", fact.available_at),
        "settlement_value": changes.get("settlement_value", fact.settlement_value),
        "settlement_value_text": changes.get(
            "settlement_value_text",
            fact.settlement_value_text,
        ),
        "raw_payload": changes.get("raw_payload", fact.raw_payload),
        "raw_payload_hash": changes.get("raw_payload_hash", fact.raw_payload_hash),
    }
    evidence = {
        "fixture_id": values["fixture_id"],
        "schema_version": values["schema_version"],
        "source_id": values["source_id"],
        "instrument_id": values["instrument_id"],
        "rule_version_id": values["rule_version_id"],
        "rule_content_hash": values["rule_content_hash"].value,
        "resolution_source": values["resolution_source"],
        "source_event_at": values["source_event_at"],
        "available_at": values["available_at"],
        "settlement_value": values["settlement_value_text"],
        "raw_payload": values["raw_payload"],
        "raw_payload_hash": values["raw_payload_hash"].value,
    }
    return replace(
        fact,
        **values,
        content_hash=ContentHash(content_digest(evidence)),
    )


def _rehashed_classified_fact(
    fact: ClassifiedRuleBoundSettlementFact,
    **changes: object,
) -> ClassifiedRuleBoundSettlementFact:
    values = {
        "fixture_id": changes.get("fixture_id", fact.fixture_id),
        "schema_version": changes.get("schema_version", fact.schema_version),
        "source_id": changes.get("source_id", fact.source_id),
        "instrument_id": changes.get("instrument_id", fact.instrument_id),
        "rule_version_id": changes.get("rule_version_id", fact.rule_version_id),
        "rule_content_hash": changes.get("rule_content_hash", fact.rule_content_hash),
        "resolution_source": changes.get("resolution_source", fact.resolution_source),
        "outcome_classification": changes.get(
            "outcome_classification",
            fact.outcome_classification,
        ),
        "source_event_at": changes.get("source_event_at", fact.source_event_at),
        "available_at": changes.get("available_at", fact.available_at),
        "settlement_value": changes.get("settlement_value", fact.settlement_value),
        "settlement_value_text": changes.get(
            "settlement_value_text",
            fact.settlement_value_text,
        ),
    }
    outcome = values["outcome_classification"]
    rule_hash = values["rule_content_hash"]
    assert isinstance(outcome, SettlementOutcome)
    assert isinstance(rule_hash, ContentHash)
    raw_payload = changes.get(
        "raw_payload",
        (
            f"resolution:{outcome.value};"
            f"settlement_value={values['settlement_value_text']};"
            f"instrument={values['instrument_id']};"
            f"rule={values['rule_version_id']};"
            f"rule_hash={rule_hash.value};"
            f"source={values['resolution_source']}"
        ),
    )
    assert isinstance(raw_payload, str)
    raw_payload_hash = ContentHash(sha256(raw_payload.encode("utf-8")).hexdigest())
    evidence = {
        "fixture_id": values["fixture_id"],
        "schema_version": values["schema_version"],
        "source_id": values["source_id"],
        "instrument_id": values["instrument_id"],
        "rule_version_id": values["rule_version_id"],
        "rule_content_hash": rule_hash.value,
        "resolution_source": values["resolution_source"],
        "outcome_classification": outcome,
        "source_event_at": values["source_event_at"],
        "available_at": values["available_at"],
        "settlement_value": values["settlement_value_text"],
        "raw_payload": raw_payload,
        "raw_payload_hash": raw_payload_hash.value,
    }
    return replace(
        fact,
        **values,
        raw_payload=raw_payload,
        raw_payload_hash=raw_payload_hash,
        content_hash=ContentHash(content_digest(evidence)),
    )


def test_complete_golden_vertical_slice_and_accounting() -> None:
    simulation, reader = _simulation()
    checkpoint = simulation.run_decision_and_execution()
    decision = checkpoint.decision
    proposal = decision.strategy_evaluation.proposal
    assert proposal is not None

    assert tuple(member.source_id for member in decision.information_set.ordered_members) == (
        "source-book-1",
        "source-estimate-2",
    )
    assert "source-late-revision-3" not in {
        member.source_id for member in decision.information_set.ordered_members
    }
    assert proposal.side.value == "YES"
    assert proposal.executable_ask.value == Decimal("0.40")
    assert proposal.expected_net_value_per_unit == Decimal("0.31")
    assert proposal.maximum_loss_per_unit.amount == Decimal("0.41")
    assert proposal.requested_risk_fraction.value == Decimal("0.10")
    assert decision.allocation.quantity.value == Decimal("243")
    assert decision.allocation.maximum_loss.amount == Decimal("99.63")
    assert decision.allocation.maximum_loss.amount <= decision.campaign.authorized_loss.amount
    assert decision.risk_decision.accepted
    assert checkpoint.decision.order.filled_quantity.value == Decimal("243")
    assert checkpoint.decision.reservation.maximum_loss.amount == Decimal("97.20")
    assert len(checkpoint.events) == 2
    assert checkpoint.events[1].fee.amount == Decimal("2.43")

    settlement = _settlement_inputs(reader.load_settlement("golden"))
    result = simulation.apply_settlement(settlement)
    assert result.settlement_cash.amount == Decimal("243")
    assert result.realized_pnl == Decimal("145.8")
    final = simulation.finalize()
    assert final.reservation.state is ReservationState.RELEASED
    assert final.reservation.maximum_loss.amount == Decimal("0")
    assert final.order.reserved_maximum_loss.amount == Decimal("0")
    assert final.metrics.contributions.amount == Decimal("1000")
    assert final.metrics.realized_trading_pnl_before_fees == Decimal("145.8")
    assert final.metrics.fees.amount == Decimal("2.43")
    assert final.metrics.net_trading_pnl == Decimal("143.37")
    assert final.metrics.ledger_equity.amount == Decimal("1143.37")
    assert final.metrics.ledger_equity.amount == (
        final.metrics.contributions.amount + final.metrics.net_trading_pnl
    )
    assert len(final.journals) == 4
    assert all(journal.balanced for journal in final.journals)


def test_execution_and_settlement_idempotency_conflicts_fail_before_mutation() -> None:
    simulation, reader = _simulation()
    checkpoint = simulation.run_decision_and_execution()
    fill = checkpoint.events[-1]
    before = canonical_json_bytes(checkpoint)
    assert canonical_json_bytes(simulation.apply_execution(fill)) == before

    conflicting_fill = replace(fill, fee=Money("2.44"))
    with pytest.raises(SimulationConflictError):
        simulation.apply_execution(conflicting_fill)
    assert canonical_json_bytes(simulation.run_decision_and_execution()) == before

    settlement = _settlement_inputs(reader.load_settlement("golden"))
    original_result = simulation.apply_settlement(settlement)
    original_report = _render(simulation)
    assert simulation.apply_settlement(settlement) == original_result
    assert _render(simulation) == original_report

    conflicting_fact = replace(
        settlement.fact,
        settlement_value=Decimal("0"),
        content_hash=ContentHash("f" * 64),
    )
    with pytest.raises(SimulationConflictError):
        simulation.apply_settlement(replace(settlement, fact=conflicting_fact))
    assert _render(simulation) == original_report


def test_accepted_risk_decision_cannot_create_a_different_second_order() -> None:
    simulation, _ = _simulation()
    checkpoint = simulation.run_decision_and_execution()
    decision = checkpoint.decision
    proposal = decision.strategy_evaluation.proposal
    assert proposal is not None
    order_book = AtomicOrderBook()
    original = order_book.authorize_and_reserve(
        campaign_id=decision.campaign.campaign_id,
        proposal=proposal,
        allocation=decision.allocation,
        risk_decision=decision.risk_decision,
    )
    assert (
        order_book.authorize_and_reserve(
            campaign_id=decision.campaign.campaign_id,
            proposal=proposal,
            allocation=decision.allocation,
            risk_decision=decision.risk_decision,
        )
        == original
    )
    changed_quantity = Quantity(
        decision.allocation.quantity.value - Decimal(1),
        decision.allocation.quantity.increment,
    )
    conflicting_allocation = replace(decision.allocation, quantity=changed_quantity)
    with pytest.raises(ValueValidationError):
        order_book.authorize_and_reserve(
            campaign_id=decision.campaign.campaign_id,
            proposal=proposal,
            allocation=conflicting_allocation,
            risk_decision=decision.risk_decision,
        )


def test_post_cutoff_settlement_mutation_cannot_change_decision_artifacts() -> None:
    yes_simulation, reader = _simulation()
    no_simulation, _ = _simulation()
    yes_checkpoint = yes_simulation.run_decision_and_execution()
    no_checkpoint = no_simulation.run_decision_and_execution()
    assert canonical_json_bytes(yes_checkpoint) == canonical_json_bytes(no_checkpoint)

    yes_settlement = _settlement_inputs(reader.load_settlement("golden"))
    no_fact = SettlementFact(
        source_id=yes_settlement.fact.source_id,
        instrument_id=yes_settlement.fact.instrument_id,
        rule_version_id=yes_settlement.fact.rule_version_id,
        source_event_at=yes_settlement.fact.source_event_at,
        available_at=yes_settlement.fact.available_at,
        settlement_value=Decimal("0"),
        content_hash=ContentHash("f" * 64),
    )
    no_settlement = SettlementInputs(
        fixture_id=yes_settlement.fixture_id,
        settlement_file_hash=ContentHash("e" * 64),
        fact=no_fact,
    )
    yes_simulation.apply_settlement(yes_settlement)
    no_simulation.apply_settlement(no_settlement)
    assert _render(yes_simulation) != _render(no_simulation)
    assert yes_simulation.finalize().metrics.ledger_equity.amount == Decimal("1143.37")
    assert no_simulation.finalize().metrics.ledger_equity.amount == Decimal("900.37")


def test_strategy_surface_has_no_fixture_or_settlement_access() -> None:
    parameters = inspect.signature(ReferenceEdgeStrategy.evaluate).parameters
    assert "settlement" not in parameters
    assert "fixture" not in parameters
    assert "repository" not in parameters


def test_no_proposal_is_terminal_without_downstream_financial_artifacts() -> None:
    simulation = _no_proposal_simulation()
    first = simulation.run_decision_and_execution()
    second = simulation.run_decision_and_execution()
    assert isinstance(first, NoProposalResult)
    assert second == first
    assert simulation.finalize() == first
    assert first.run_outcome is RunOutcome.NO_PROPOSAL
    assert first.strategy_evaluation.outcome is StrategyOutcome.NO_PROPOSAL
    assert first.strategy_evaluation.reason_codes == ("SYNTHETIC_EDGE_BELOW_THRESHOLD",)
    assert first.strategy_evaluation.proposal is None
    for absent in (
        "allocation",
        "risk_decision",
        "reservation",
        "order",
        "execution_events",
        "position",
        "settlement",
    ):
        assert not hasattr(first, absent)

    assert len(first.journals) == 1
    contribution = first.journals[0]
    assert contribution.journal_type is JournalType.CONTRIBUTION
    assert contribution.balanced
    assert {posting.account_code for posting in contribution.postings} == {
        AccountCode.CASH,
        AccountCode.CONTRIBUTED_CAPITAL,
    }
    assert first.metrics.contributions.amount == Decimal("1000")
    assert first.metrics.cash.amount == Decimal("1000")
    assert first.metrics.position_cost.amount == Decimal("0")
    assert first.metrics.ledger_equity.amount == Decimal("1000")
    assert first.metrics.realized_trading_pnl_before_fees == Decimal("0")
    assert first.metrics.fees.amount == Decimal("0")
    assert first.metrics.net_trading_pnl == Decimal("0")


def test_run_use_case_never_loads_settlement_for_no_proposal() -> None:
    reader = FixtureReader()
    fixture = reader.load_decision("no-proposal")
    result = run_simulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("no-proposal")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        settlement_loader=_must_not_load_settlement,
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=MustNotSimulate(),
    )
    assert isinstance(result, NoProposalResult)


def test_post_cutoff_revision_cannot_change_no_proposal_evaluation_or_report() -> None:
    reader = FixtureReader()
    fixture = reader.load_decision("no-proposal")
    inputs = _decision_inputs(fixture)
    late_record = inputs.source_records[-1]
    assert late_record.available_at > inputs.cutoff.value
    changed_late_record = replace(late_record, content_hash=ContentHash("f" * 64))
    changed_inputs = replace(
        inputs,
        source_records=(*inputs.source_records[:-1], changed_late_record),
    )
    metadata = RunMetadata(
        fixture_manifest_hash=ContentHash(reader.manifest_hash("no-proposal")),
        decision_file_hash=ContentHash(fixture.file_hash),
        logical_time=fixture.logical_time,
    )

    def run(decision_inputs: object) -> NoProposalResult:
        result = run_simulation(
            decision_inputs=decision_inputs,  # type: ignore[arg-type]
            execution_script=_execution_script(fixture),
            run_metadata=metadata,
            settlement_loader=_must_not_load_settlement,
            capital=Money("1000"),
            seed=20260716,
            engine_version=ENGINE_VERSION,
            simulator=MustNotSimulate(),
        )
        assert isinstance(result, NoProposalResult)
        return result

    original = run(inputs)
    changed = run(changed_inputs)
    assert original.information_set == changed.information_set
    assert original.strategy_evaluation == changed.strategy_evaluation
    renderer = CanonicalReportRenderer()
    assert renderer.render(report_document(original)) == renderer.render(report_document(changed))


def test_policy_rejection_stops_before_risk_execution_and_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("post-policy component must not be invoked")

    monkeypatch.setattr(RiskKernel, "evaluate", forbidden)
    monkeypatch.setattr(run_module, "create_reserved_order", forbidden)
    monkeypatch.setattr(run_module, "initial_position", forbidden)
    monkeypatch.setattr(run_module, "settle_position", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "purchase", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "fee", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "settlement", forbidden)

    simulation = _policy_rejected_simulation()
    first = simulation.run_decision_and_execution()
    second = simulation.run_decision_and_execution()
    assert isinstance(first, PolicyRejectedResult)
    assert second == first
    assert simulation.finalize() == first
    assert first.run_outcome is RunOutcome.POLICY_REJECTED
    assert first.strategy_evaluation.outcome is StrategyOutcome.PROPOSAL
    assert first.strategy_evaluation.proposal == first.proposal
    assert first.proposal.expected_net_value_per_unit == Decimal("0.31")
    assert not first.allocation.accepted
    assert first.allocation.reason_codes == (POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED,)
    assert first.policy_reason_codes == first.allocation.reason_codes
    assert first.policy_explanation == (
        "Expected capital lockup of 7499 seconds exceeds campaign policy maximum of 3600 seconds."
    )
    assert all(reason.startswith("POLICY_") for reason in first.policy_reason_codes)
    for absent in (
        "risk_decision",
        "reservation",
        "order",
        "execution_events",
        "position",
        "settlement",
    ):
        assert not hasattr(first, absent)

    assert len(first.journals) == 1
    contribution = first.journals[0]
    assert contribution.journal_type is JournalType.CONTRIBUTION
    assert contribution.balanced
    assert {posting.account_code for posting in contribution.postings} == {
        AccountCode.CASH,
        AccountCode.CONTRIBUTED_CAPITAL,
    }
    assert first.metrics.contributions.amount == Decimal("1000")
    assert first.metrics.cash.amount == Decimal("1000")
    assert first.metrics.position_cost.amount == Decimal("0")
    assert first.metrics.ledger_equity.amount == Decimal("1000")
    assert first.metrics.realized_trading_pnl_before_fees == Decimal("0")
    assert first.metrics.fees.amount == Decimal("0")
    assert first.metrics.net_trading_pnl == Decimal("0")


def test_run_use_case_never_loads_settlement_for_policy_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_authorize(*_: object, **__: object) -> object:
        raise AssertionError("RiskKernel must not authorize a policy-rejected proposal")

    monkeypatch.setattr(RiskKernel, "evaluate", must_not_authorize)
    reader = FixtureReader()
    fixture = reader.load_decision("policy-rejected")
    result = run_simulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("policy-rejected")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        settlement_loader=_must_not_load_settlement,
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=MustNotSimulate(),
    )
    assert isinstance(result, PolicyRejectedResult)


def test_policy_rejected_cli_composition_never_reaches_post_policy_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("CLI crossed the terminal policy-rejection boundary")

    monkeypatch.setattr(RiskKernel, "evaluate", forbidden)
    monkeypatch.setattr(AtomicOrderBook, "authorize_and_reserve", forbidden)
    monkeypatch.setattr(DeterministicSimulator, "simulate", forbidden)
    monkeypatch.setattr(FixtureReader, "load_settlement", forbidden)
    monkeypatch.setattr(run_module, "initial_position", forbidden)

    report = json.loads(demo_core("policy-rejected", Money("1000"), 20260716))
    assert report["run_outcome"] == "POLICY_REJECTED"


@pytest.mark.parametrize("capital", ("100", "1000", "10000"))
def test_policy_rejection_is_capital_scale_invariant(capital: str) -> None:
    reader = FixtureReader()
    fixture = reader.load_decision("policy-rejected")
    result = run_simulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("policy-rejected")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        settlement_loader=_must_not_load_settlement,
        capital=Money(capital),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=MustNotSimulate(),
    )
    assert isinstance(result, PolicyRejectedResult)
    assert result.policy_reason_codes == (POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED,)
    assert result.metrics.ledger_equity.amount == Decimal(capital)
    assert result.metrics.net_trading_pnl == Decimal(0)


def test_policy_rejected_fixture_changes_only_policy_and_run_identity() -> None:
    reader = FixtureReader()
    golden = reader.load_decision("golden")
    rejected = reader.load_decision("policy-rejected")
    assert rejected.instrument == golden.instrument
    assert rejected.contract_rule == golden.contract_rule
    assert rejected.source_records == golden.source_records
    assert rejected.reference_estimate == golden.reference_estimate
    assert rejected.strategy_version == golden.strategy_version
    assert rejected.fee_model_version == golden.fee_model_version
    assert rejected.execution.opened_at == golden.execution.opened_at
    assert rejected.execution.filled_at == golden.execution.filled_at
    assert rejected.execution.fill_price == golden.execution.fill_price
    assert rejected.execution.fee_per_contract == golden.execution.fee_per_contract
    assert rejected.policy.max_capital_lockup_seconds == 3600
    assert golden.policy.max_capital_lockup_seconds == 10_800


def test_loosened_campaign_policy_accepts_the_same_proposal_and_lockup() -> None:
    result = _policy_rejected_simulation().run_decision_and_execution()
    assert isinstance(result, PolicyRejectedResult)
    decision_at = FixtureReader().load_decision("policy-rejected").execution.opened_at
    tight = result.campaign.policy
    loosened = CampaignPolicy(
        policy_version_id="policy-lockup-loosened-v1",
        profile=tight.profile,
        requested_risk_fraction=tight.requested_risk_fraction,
        max_trade_loss_fraction=tight.max_trade_loss_fraction,
        max_open_loss_fraction=tight.max_open_loss_fraction,
        min_synthetic_edge=tight.min_synthetic_edge,
        max_spread=tight.max_spread,
        max_capital_lockup_seconds=10_800,
        content_hash=ContentHash("a" * 64),
    )
    loosened_campaign = Campaign(
        campaign_id=result.campaign.campaign_id,
        contribution=result.campaign.contribution,
        authorized_loss=result.campaign.authorized_loss,
        policy=loosened,
    )
    snapshot = CampaignSnapshot(
        campaign=loosened_campaign,
        ledger_equity=result.metrics.ledger_equity,
        settled_cash=result.metrics.cash,
        active_reservations=Money(0),
    )
    allocation = CapitalAllocator.allocate(
        result.proposal,
        snapshot,
        result.information_set.market.visible_yes_quantity,
        loosened,
        decision_at=decision_at,
        expected_settlement_at=result.information_set.rule.expected_settlement_at,
    )
    assert allocation.accepted
    assert allocation.reason_codes == ("ALLOCATED",)
    assert allocation.quantity.value == Decimal("243")


def test_post_cutoff_revision_cannot_change_policy_rejection_or_report() -> None:
    reader = FixtureReader()
    fixture = reader.load_decision("policy-rejected")
    inputs = _decision_inputs(fixture)
    late_record = inputs.source_records[-1]
    assert late_record.available_at > inputs.cutoff.value
    changed_inputs = replace(
        inputs,
        source_records=(
            *inputs.source_records[:-1],
            replace(late_record, content_hash=ContentHash("e" * 64)),
        ),
    )
    metadata = RunMetadata(
        fixture_manifest_hash=ContentHash(reader.manifest_hash("policy-rejected")),
        decision_file_hash=ContentHash(fixture.file_hash),
        logical_time=fixture.logical_time,
    )

    def run(decision_inputs: object) -> PolicyRejectedResult:
        result = run_simulation(
            decision_inputs=decision_inputs,  # type: ignore[arg-type]
            execution_script=_execution_script(fixture),
            run_metadata=metadata,
            settlement_loader=_must_not_load_settlement,
            capital=Money("1000"),
            seed=20260716,
            engine_version=ENGINE_VERSION,
            simulator=MustNotSimulate(),
        )
        assert isinstance(result, PolicyRejectedResult)
        return result

    original = run(inputs)
    changed = run(changed_inputs)
    assert original.information_set == changed.information_set
    assert original.strategy_evaluation == changed.strategy_evaluation
    assert original.proposal == changed.proposal
    assert original.allocation == changed.allocation
    renderer = CanonicalReportRenderer()
    assert renderer.render(report_document(original)) == renderer.render(report_document(changed))


def test_invariant_rejection_is_terminal_idempotent_and_financially_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    risk_calls: list[datetime] = []
    real_risk_evaluate = RiskKernel.evaluate

    def counted_risk(*args: object, **kwargs: object) -> object:
        risk_calls.append(kwargs["authorization_at"])  # type: ignore[arg-type]
        return real_risk_evaluate(*args, **kwargs)  # type: ignore[arg-type]

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("post-invariant component must not be invoked")

    monkeypatch.setattr(RiskKernel, "evaluate", staticmethod(counted_risk))
    monkeypatch.setattr(AtomicOrderBook, "authorize_and_reserve", forbidden)
    monkeypatch.setattr(run_module, "initial_position", forbidden)
    monkeypatch.setattr(run_module, "apply_execution_event", forbidden)
    monkeypatch.setattr(run_module, "settle_position", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "purchase", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "fee", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "settlement", forbidden)

    simulation = _invariant_rejected_simulation()
    first = simulation.run_decision_and_execution()
    second = simulation.run_decision_and_execution()
    assert isinstance(first, InvariantRejectedResult)
    assert second == first
    assert simulation.finalize() == first
    assert risk_calls == [datetime(2026, 7, 15, 15, 3, 2, tzinfo=UTC)]

    assert first.run_outcome is RunOutcome.INVARIANT_REJECTED
    assert first.information_set.valid
    assert first.strategy_evaluation.outcome is StrategyOutcome.PROPOSAL
    assert first.strategy_evaluation.proposal == first.proposal
    assert first.proposal.side is Side.YES
    assert first.proposal.expected_net_value_per_unit == Decimal("0.31")
    assert first.allocation.accepted
    assert first.allocation.reason_codes == ("ALLOCATED",)
    assert first.allocation.quantity.value == Decimal("243")
    assert first.allocation.maximum_loss.amount == Decimal("99.63")

    risk = first.risk_decision
    assert isinstance(risk, StaleMarketRiskDecision)
    assert not risk.accepted
    assert risk.allocation_id == first.allocation.allocation_id
    assert risk.reason_codes == (INVARIANT_MARKET_DATA_STALE,)
    assert first.invariant_reason_codes == risk.reason_codes
    assert risk.observation_source_id == "source-book-1"
    assert risk.observation_available_at == datetime(2026, 7, 15, 14, 58, 1, tzinfo=UTC)
    assert risk.authorization_at == datetime(2026, 7, 15, 15, 3, 2, tzinfo=UTC)
    assert risk.observed_age_seconds == 301
    assert risk.maximum_age_seconds == OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
    assert risk.information_set_id == first.information_set.information_set_id
    assert risk.information_set_hash == first.information_set.content_hash
    for expected_fragment in (
        "2026-07-15T14:58:01Z",
        "2026-07-15T15:03:02Z",
        "301",
        "300",
        first.information_set.information_set_id,
    ):
        assert expected_fragment in risk.invariant_explanation

    estimate_member = next(
        member
        for member in first.information_set.ordered_members
        if member.source_id == "source-estimate-2"
    )
    assert estimate_member.available_at > risk.observation_available_at
    assert (risk.authorization_at - estimate_member.available_at).seconds == 240

    for absent in (
        "reservation",
        "order",
        "execution_events",
        "position",
        "settlement",
    ):
        assert not hasattr(first, absent)
    assert len(first.journals) == 1
    contribution = first.journals[0]
    assert contribution.journal_type is JournalType.CONTRIBUTION
    assert contribution.balanced
    assert {posting.account_code for posting in contribution.postings} == {
        AccountCode.CASH,
        AccountCode.CONTRIBUTED_CAPITAL,
    }
    assert first.metrics.contributions.amount == Decimal("1000")
    assert first.metrics.cash.amount == Decimal("1000")
    assert first.metrics.position_cost.amount == Decimal("0")
    assert first.metrics.ledger_equity.amount == Decimal("1000")
    assert first.metrics.realized_trading_pnl_before_fees == Decimal("0")
    assert first.metrics.fees.amount == Decimal("0")
    assert first.metrics.net_trading_pnl == Decimal("0")


def test_invariant_rejected_cli_stops_before_order_execution_and_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    risk_calls = 0
    real_risk_evaluate = RiskKernel.evaluate

    def counted_risk(*args: object, **kwargs: object) -> object:
        nonlocal risk_calls
        risk_calls += 1
        return real_risk_evaluate(*args, **kwargs)  # type: ignore[arg-type]

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("CLI crossed the terminal invariant-rejection boundary")

    monkeypatch.setattr(RiskKernel, "evaluate", staticmethod(counted_risk))
    monkeypatch.setattr(AtomicOrderBook, "authorize_and_reserve", forbidden)
    monkeypatch.setattr(DeterministicSimulator, "simulate", forbidden)
    monkeypatch.setattr(FixtureReader, "load_settlement", forbidden)
    monkeypatch.setattr(run_module, "initial_position", forbidden)
    monkeypatch.setattr(run_module, "apply_execution_event", forbidden)
    monkeypatch.setattr(run_module, "settle_position", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "purchase", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "fee", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "settlement", forbidden)

    report = json.loads(demo_core("invariant-rejected", Money("1000"), 20260716))
    assert risk_calls == 1
    assert report["run_outcome"] == "INVARIANT_REJECTED"


def test_same_proposal_and_allocation_pass_at_exact_freshness_boundary() -> None:
    result = _invariant_rejected_simulation().run_decision_and_execution()
    assert isinstance(result, InvariantRejectedResult)
    snapshot = CampaignSnapshot(
        campaign=result.campaign,
        ledger_equity=result.metrics.ledger_equity,
        settled_cash=result.metrics.cash,
        active_reservations=Money(0),
    )
    early_authorization = datetime(2026, 7, 15, 15, 3, 1, tzinfo=UTC)
    accepted = RiskKernel.evaluate(
        result.proposal,
        result.allocation,
        snapshot,
        result.information_set,
        result.campaign.policy,
        OperationalInvariants(),
        authorization_at=early_authorization,
        market_observation_source_id="source-book-1",
    )
    assert accepted.accepted
    assert accepted.reason_codes == ("ALL_INVARIANTS_PASSED",)
    assert not isinstance(accepted, StaleMarketRiskDecision)
    assert (
        early_authorization - result.risk_decision.observation_available_at
    ).seconds == OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
    assert result.allocation.quantity.value == Decimal("243")
    assert result.allocation.maximum_loss.amount == Decimal("99.63")
    assert accepted.context_hash != result.risk_decision.context_hash
    assert accepted.exact_order_fingerprint != result.risk_decision.exact_order_fingerprint
    assert accepted.risk_decision_id != result.risk_decision.risk_decision_id


@pytest.mark.parametrize(
    ("profile", "policy_hash"),
    (("CONSERVATIVE", "a" * 64), ("MOONSHOT", "b" * 64)),
)
def test_campaign_policy_changes_cannot_suppress_stale_market_invariant(
    profile: str,
    policy_hash: str,
) -> None:
    result = _invariant_rejected_simulation().run_decision_and_execution()
    assert isinstance(result, InvariantRejectedResult)
    policy = CampaignPolicy(
        policy_version_id=f"policy-{profile.lower()}-freshness-control-v1",
        profile=profile,
        requested_risk_fraction="0.10",
        max_trade_loss_fraction="0.20",
        max_open_loss_fraction="1",
        min_synthetic_edge="0",
        max_spread="1",
        max_capital_lockup_seconds=20_000,
        content_hash=ContentHash(policy_hash),
    )
    campaign = Campaign(
        campaign_id=result.campaign.campaign_id,
        contribution=result.campaign.contribution,
        authorized_loss=result.campaign.authorized_loss,
        policy=policy,
    )
    snapshot = CampaignSnapshot(
        campaign=campaign,
        ledger_equity=result.metrics.ledger_equity,
        settled_cash=result.metrics.cash,
        active_reservations=Money(0),
    )
    allocation = CapitalAllocator.allocate(
        result.proposal,
        snapshot,
        result.information_set.market.visible_yes_quantity,
        policy,
        decision_at=datetime(2026, 7, 15, 15, 0, 1, tzinfo=UTC),
        expected_settlement_at=result.information_set.rule.expected_settlement_at,
    )
    assert allocation.accepted
    rejected = RiskKernel.evaluate(
        result.proposal,
        allocation,
        snapshot,
        result.information_set,
        policy,
        OperationalInvariants(),
        authorization_at=result.risk_decision.authorization_at,
        market_observation_source_id=result.risk_decision.observation_source_id,
    )
    assert not rejected.accepted
    assert rejected.reason_codes == (INVARIANT_MARKET_DATA_STALE,)


def test_operational_freshness_limit_is_sealed_outside_campaign_policy() -> None:
    assert not hasattr(CampaignPolicy, "max_data_age_seconds")
    assert (
        OperationalInvariants().maximum_market_data_age_seconds
        == OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
        == 300
    )
    with pytest.raises(TypeError):
        OperationalInvariants(maximum_market_data_age_seconds=301)  # type: ignore[call-arg]


def test_rejected_risk_decision_cannot_authorize_or_accept_conflicting_evidence() -> None:
    result = _invariant_rejected_simulation().run_decision_and_execution()
    assert isinstance(result, InvariantRejectedResult)
    with pytest.raises(ValueValidationError, match="rejected risk decision"):
        create_reserved_order(
            campaign_id=result.campaign.campaign_id,
            proposal=result.proposal,
            allocation=result.allocation,
            risk_decision=result.risk_decision,
        )
    with pytest.raises(ValueValidationError, match="cannot be accepted"):
        replace(result.risk_decision, accepted=True)
    with pytest.raises(ValueValidationError, match="does not match"):
        replace(result.risk_decision, observed_age_seconds=302)
    with pytest.raises(ValueValidationError, match="sealed operational maximum"):
        replace(result.risk_decision, maximum_age_seconds=301)
    with pytest.raises(ValueValidationError, match="deterministic evidence"):
        replace(result.risk_decision, invariant_explanation="conflicting explanation")
    forged_explanation = market_data_stale_explanation(
        observation_source_id="forged-source",
        observation_available_at=result.risk_decision.observation_available_at,
        authorization_at=result.risk_decision.authorization_at,
        observed_age_seconds=result.risk_decision.observed_age_seconds,
        maximum_age_seconds=result.risk_decision.maximum_age_seconds,
        information_set_id=result.risk_decision.information_set_id,
    )
    forged_risk = replace(
        result.risk_decision,
        observation_source_id="forged-source",
        invariant_explanation=forged_explanation,
    )
    with pytest.raises(ValueError, match="exact information-set member"):
        replace(
            result,
            risk_decision=forged_risk,
            invariant_explanation=forged_explanation,
        )
    with pytest.raises(ValueError, match="accepted allocation"):
        replace(result, allocation=replace(result.allocation, accepted=False))
    with pytest.raises(ValueError, match="contribution journal"):
        replace(result, journals=())
    with pytest.raises(ValueError, match="trading financial state"):
        replace(result, metrics=replace(result.metrics, fees=Money("1")))


def test_post_cutoff_revision_cannot_change_invariant_rejection_or_report() -> None:
    reader = FixtureReader()
    fixture = reader.load_decision("invariant-rejected")
    inputs = _decision_inputs(fixture)
    late_record = inputs.source_records[-1]
    assert late_record.available_at > inputs.cutoff.value
    changed_inputs = replace(
        inputs,
        source_records=(
            *inputs.source_records[:-1],
            replace(late_record, content_hash=ContentHash("d" * 64)),
        ),
    )
    metadata = RunMetadata(
        fixture_manifest_hash=ContentHash(reader.manifest_hash("invariant-rejected")),
        decision_file_hash=ContentHash(fixture.file_hash),
        logical_time=fixture.logical_time,
    )

    def run(decision_inputs: object) -> InvariantRejectedResult:
        result = run_simulation(
            decision_inputs=decision_inputs,  # type: ignore[arg-type]
            execution_script=_execution_script(fixture),
            run_metadata=metadata,
            settlement_loader=_must_not_load_settlement,
            capital=Money("1000"),
            seed=20260716,
            engine_version=ENGINE_VERSION,
            simulator=MustNotSimulate(),
        )
        assert isinstance(result, InvariantRejectedResult)
        return result

    original = run(inputs)
    changed = run(changed_inputs)
    assert original.information_set == changed.information_set
    assert original.strategy_evaluation == changed.strategy_evaluation
    assert original.proposal == changed.proposal
    assert original.allocation == changed.allocation
    assert original.risk_decision == changed.risk_decision
    renderer = CanonicalReportRenderer()
    assert renderer.render(report_document(original)) == renderer.render(report_document(changed))


def test_invariant_rejected_fixture_preserves_golden_decision_economics() -> None:
    reader = FixtureReader()
    golden = reader.load_decision("golden")
    rejected = reader.load_decision("invariant-rejected")
    assert rejected.policy == golden.policy
    assert rejected.instrument == golden.instrument
    assert rejected.contract_rule == golden.contract_rule
    assert rejected.source_records == golden.source_records
    assert rejected.reference_estimate == golden.reference_estimate
    assert rejected.strategy_version == golden.strategy_version
    assert rejected.fee_model_version == golden.fee_model_version
    assert rejected.execution.opened_at == golden.execution.opened_at
    assert rejected.execution.filled_at == golden.execution.filled_at
    assert rejected.authorization_at == datetime(2026, 7, 15, 15, 3, 2, tzinfo=UTC)
    assert rejected.market_observation_source_id == "source-book-1"


def test_unfilled_expiration_is_terminal_and_financially_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"risk": 0, "authorize": 0, "simulate": 0}
    real_risk = RiskKernel.evaluate
    real_authorize = AtomicOrderBook.authorize_and_reserve
    real_simulate = DeterministicSimulator.simulate

    def counted_risk(*args: object, **kwargs: object) -> object:
        calls["risk"] += 1
        return real_risk(*args, **kwargs)  # type: ignore[arg-type]

    def counted_authorize(self: AtomicOrderBook, **kwargs: object) -> object:
        calls["authorize"] += 1
        return real_authorize(self, **kwargs)  # type: ignore[arg-type]

    def counted_simulate(
        self: DeterministicSimulator,
        *args: object,
        **kwargs: object,
    ) -> object:
        calls["simulate"] += 1
        return real_simulate(self, *args, **kwargs)  # type: ignore[arg-type]

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("unfilled expiration reached a fill or settlement component")

    monkeypatch.setattr(RiskKernel, "evaluate", staticmethod(counted_risk))
    monkeypatch.setattr(AtomicOrderBook, "authorize_and_reserve", counted_authorize)
    monkeypatch.setattr(DeterministicSimulator, "simulate", counted_simulate)
    monkeypatch.setattr(run_module, "initial_position", forbidden)
    monkeypatch.setattr(run_module, "apply_execution_event", forbidden)
    monkeypatch.setattr(run_module, "settle_position", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "purchase", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "fee", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "settlement", forbidden)

    simulation = _unfilled_expired_simulation()
    first = simulation.run_decision_and_execution()
    second = simulation.run_decision_and_execution()
    assert isinstance(first, UnfilledExpiredResult)
    assert second == first
    assert simulation.finalize() == first
    assert calls == {"risk": 1, "authorize": 1, "simulate": 1}

    assert first.run_outcome is RunOutcome.UNFILLED_EXPIRED
    assert first.information_set.valid
    assert first.strategy_evaluation.outcome is StrategyOutcome.PROPOSAL
    assert first.strategy_evaluation.proposal == first.proposal
    assert isinstance(first.proposal, RestingLimitProposal)
    assert first.proposal.side is Side.YES
    assert first.proposal.executable_ask.value == Decimal("0.40")
    assert first.proposal.limit_price.value == Decimal("0.39")
    assert first.proposal.expected_net_value_per_unit == Decimal("0.32")
    assert first.proposal.maximum_loss_per_unit.amount == Decimal("0.40")
    assert first.allocation.accepted
    assert first.allocation.quantity.value == Decimal("250")
    assert first.allocation.maximum_loss.amount == Decimal("100")
    assert first.risk_decision.accepted
    assert first.risk_decision.reason_codes == ("ALL_INVARIANTS_PASSED",)

    initial, opened, released = first.reservation_lifecycle
    assert tuple(item.state for item in first.reservation_lifecycle) == (
        ReservationState.HELD,
        ReservationState.OPEN,
        ReservationState.RELEASED,
    )
    assert initial.maximum_loss.amount == Decimal("100")
    assert opened.maximum_loss == initial.maximum_loss
    assert released.maximum_loss.amount == Decimal("0")
    assert first.released_reservation_amount.amount == Decimal("100")
    assert first.risk_capacity_before_order.amount == Decimal("1000")
    assert first.risk_capacity_while_open.amount == Decimal("900")
    assert first.risk_capacity_after_expiration.amount == Decimal("1000")

    assert isinstance(first.order, ExpiringLimitOrder)
    assert first.order.state is OrderState.EXPIRED
    assert first.order.quantity.value == Decimal("250")
    assert first.order.filled_quantity.value == Decimal("0")
    assert first.order.remaining_quantity == first.order.quantity
    assert first.order.reserved_maximum_loss.amount == Decimal("0")
    assert first.order.limit_price.value < first.order.executable_ask.value
    assert first.order.expires_at == datetime(2026, 7, 15, 15, 4, tzinfo=UTC)
    assert tuple(event.event_type for event in first.execution_events) == (
        ExecutionEventType.OPENED,
        ExecutionEventType.EXPIRED,
    )
    assert tuple(event.sequence for event in first.execution_events) == (1, 2)
    assert all(event.quantity.value == 0 for event in first.execution_events)
    assert all(event.price is None for event in first.execution_events)
    assert all(event.fee.amount == 0 for event in first.execution_events)
    assert len({event.event_id for event in first.execution_events}) == 2

    for absent in ("position", "settlement", "fills"):
        assert not hasattr(first, absent)
    assert len(first.journals) == 1
    assert first.journals[0].journal_type is JournalType.CONTRIBUTION
    assert first.journals[0].balanced
    assert first.metrics.contributions.amount == Decimal("1000")
    assert first.metrics.cash.amount == Decimal("1000")
    assert first.metrics.ledger_equity.amount == Decimal("1000")
    assert first.metrics.position_cost.amount == Decimal("0")
    assert first.metrics.realized_trading_pnl_before_fees == Decimal("0")
    assert first.metrics.fees.amount == Decimal("0")
    assert first.metrics.net_trading_pnl == Decimal("0")


def test_late_estimate_source_cannot_be_laundered_into_the_information_set() -> None:
    reader = FixtureReader()
    fixture = reader.load_decision("unfilled-expired")
    inputs = _decision_inputs(fixture)
    late_inputs = replace(
        inputs,
        reference_estimate=replace(
            inputs.reference_estimate,
            source_id="source-late-revision-3",
        ),
    )

    result = run_simulation(
        decision_inputs=late_inputs,
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("unfilled-expired")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        settlement_loader=_must_not_load_settlement,
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=MustNotSimulate(),
    )

    assert isinstance(result, NoProposalResult)
    assert result.strategy_evaluation.reason_codes == ("ESTIMATE_SOURCE_NOT_IN_INFORMATION_SET",)


def test_unfilled_expiration_domain_transitions_and_guards_are_exact() -> None:
    result = _unfilled_expired_simulation().run_decision_and_execution()
    assert isinstance(result, UnfilledExpiredResult)
    held, _, _ = result.reservation_lifecycle
    reservation, order = create_reserved_order(
        campaign_id=result.campaign.campaign_id,
        proposal=result.proposal,
        allocation=result.allocation,
        risk_decision=result.risk_decision,
    )
    assert reservation == held
    assert isinstance(order, ExpiringLimitOrder)
    script = _execution_script(FixtureReader().load_decision("unfilled-expired"))
    assert isinstance(script, UnfilledExpiryExecutionScript)
    events = DeterministicSimulator().simulate(order, script, result.information_set.market)
    assert events == DeterministicSimulator().simulate(
        order,
        script,
        result.information_set.market,
    )

    initial_bytes = canonical_json_bytes({"order": order, "reservation": reservation})
    with pytest.raises(ValueValidationError, match="out of sequence"):
        apply_unfilled_order_event(order, reservation, events[1])
    assert canonical_json_bytes({"order": order, "reservation": reservation}) == initial_bytes

    opened = apply_unfilled_order_event(order, reservation, events[0])
    assert opened.order.state is OrderState.OPEN
    assert opened.reservation.state is ReservationState.OPEN
    assert opened.reservation.maximum_loss.amount == Decimal("100")
    assert opened.released_maximum_loss.amount == Decimal("0")
    open_bytes = canonical_json_bytes(opened)

    gap_expiry = replace(
        events[1],
        event_id="execution_event_gap",
        sequence=3,
        payload_hash=ContentHash("a" * 64),
    )
    with pytest.raises(ValueValidationError, match="out of sequence"):
        apply_unfilled_order_event(opened.order, opened.reservation, gap_expiry)
    assert canonical_json_bytes(opened) == open_bytes

    expired = apply_unfilled_order_event(opened.order, opened.reservation, events[1])
    assert expired.order.state is OrderState.EXPIRED
    assert expired.reservation.state is ReservationState.RELEASED
    assert expired.reservation.maximum_loss.amount == Decimal("0")
    assert expired.released_maximum_loss.amount == Decimal("100")

    new_fill = replace(
        events[1],
        event_id="execution_event_fill_after_expiry",
        sequence=3,
        event_type=ExecutionEventType.FILL,
        quantity=Quantity("1", "1"),
        price=Price("0.39", "0.01"),
        fee=Money("0.01"),
        payload_hash=ContentHash("b" * 64),
    )
    terminal_bytes = canonical_json_bytes(expired)
    with pytest.raises(ValueValidationError):
        apply_unfilled_order_event(expired.order, expired.reservation, new_fill)
    with pytest.raises(ValueValidationError):
        apply_unfilled_order_event(
            expired.order,
            expired.reservation,
            replace(
                events[0],
                event_id="execution_event_open_after_expiry",
                sequence=3,
                payload_hash=ContentHash("c" * 64),
            ),
        )
    assert canonical_json_bytes(expired) == terminal_bytes


def test_unfilled_application_event_idempotency_and_conflicts_fail_closed() -> None:
    simulation = _unfilled_expired_simulation()
    result = simulation.run_decision_and_execution()
    assert isinstance(result, UnfilledExpiredResult)
    original_report = _render(simulation)
    opened, expired = result.execution_events

    opened_retry = simulation.apply_unfilled_execution(opened)
    expired_retry = simulation.apply_unfilled_execution(expired)
    assert opened_retry.decision.order == result.order
    assert expired_retry.decision.order == result.order
    assert tuple(expired_retry.events) == result.execution_events
    assert _render(simulation) == original_report

    with pytest.raises(SimulationConflictError):
        simulation.apply_unfilled_execution(replace(expired, fee=Money("0.01")))
    assert _render(simulation) == original_report

    new_fill = replace(
        expired,
        event_id="execution_event_new_fill",
        sequence=3,
        event_type=ExecutionEventType.FILL,
        quantity=Quantity("1", "1"),
        price=Price("0.39", "0.01"),
        fee=Money("0.01"),
        payload_hash=ContentHash("d" * 64),
    )
    with pytest.raises(ValueValidationError):
        simulation.apply_unfilled_execution(new_fill)
    new_open = replace(
        opened,
        event_id="execution_event_new_open",
        sequence=3,
        payload_hash=ContentHash("e" * 64),
    )
    with pytest.raises(ValueValidationError):
        simulation.apply_unfilled_execution(new_open)
    assert _render(simulation) == original_report


def test_unfilled_order_terms_are_bound_by_accepted_authority() -> None:
    result = _unfilled_expired_simulation().run_decision_and_execution()
    assert isinstance(result, UnfilledExpiredResult)
    exact = create_reserved_order(
        campaign_id=result.campaign.campaign_id,
        proposal=result.proposal,
        allocation=result.allocation,
        risk_decision=result.risk_decision,
    )
    assert exact[0] == result.initial_reservation

    with pytest.raises(ValueValidationError, match="fingerprint"):
        create_reserved_order(
            campaign_id=result.campaign.campaign_id,
            proposal=replace(result.proposal, limit_price=Price("0.38", "0.01")),
            allocation=result.allocation,
            risk_decision=result.risk_decision,
        )
    with pytest.raises(ValueValidationError, match="fingerprint"):
        create_reserved_order(
            campaign_id=result.campaign.campaign_id,
            proposal=replace(
                result.proposal,
                order_expires_at=datetime(2026, 7, 15, 15, 3, 59, tzinfo=UTC),
            ),
            allocation=result.allocation,
            risk_decision=result.risk_decision,
        )
    with pytest.raises(ValueValidationError, match="allocation identity"):
        create_reserved_order(
            campaign_id=result.campaign.campaign_id,
            proposal=result.proposal,
            allocation=replace(
                result.allocation,
                quantity=Quantity("249", "1"),
                maximum_loss=Money("99.60"),
            ),
            risk_decision=result.risk_decision,
        )
    with pytest.raises(ValueValidationError, match="maximum loss"):
        create_reserved_order(
            campaign_id=result.campaign.campaign_id,
            proposal=result.proposal,
            allocation=replace(result.allocation, maximum_loss=Money("1")),
            risk_decision=result.risk_decision,
        )
    with pytest.raises(ValueValidationError, match="accepted positive allocation"):
        create_reserved_order(
            campaign_id=result.campaign.campaign_id,
            proposal=result.proposal,
            allocation=replace(result.allocation, accepted=False),
            risk_decision=result.risk_decision,
        )


def test_risk_kernel_rejects_a_resting_order_expired_at_authorization() -> None:
    result = _unfilled_expired_simulation().run_decision_and_execution()
    assert isinstance(result, UnfilledExpiredResult)
    inputs = _decision_inputs(FixtureReader().load_decision("unfilled-expired"))
    proposal = replace(
        result.proposal,
        order_expires_at=inputs.risk_authorization_at,
    )
    snapshot = CampaignSnapshot(
        campaign=result.campaign,
        ledger_equity=result.metrics.ledger_equity,
        settled_cash=result.metrics.cash,
        active_reservations=Money(0),
    )

    rejected = RiskKernel.evaluate(
        proposal,
        result.allocation,
        snapshot,
        result.information_set,
        result.campaign.policy,
        OperationalInvariants(),
        authorization_at=inputs.risk_authorization_at,
        market_observation_source_id=inputs.market_observation_source_id,
    )

    assert not rejected.accepted
    assert rejected.reason_codes == ("ORDER_EXPIRED",)


@pytest.mark.parametrize("tamper", ("event_id", "payload_hash"))
def test_unfilled_first_seen_forged_expiration_cannot_release_reservation(
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_simulate = DeterministicSimulator.simulate
    real_apply = run_module.apply_unfilled_order_event
    applied: list[AppliedUnfilledOrderEvent] = []

    def forged_simulate(
        self: DeterministicSimulator,
        *args: object,
        **kwargs: object,
    ) -> object:
        opened, expired = real_simulate(self, *args, **kwargs)  # type: ignore[arg-type]
        if tamper == "event_id":
            expired = replace(expired, event_id="execution_event_forged")
        else:
            expired = replace(expired, payload_hash=ContentHash("f" * 64))
        return opened, expired

    def tracked_apply(
        order: ExpiringLimitOrder,
        reservation: RiskReservation,
        event: ExecutionEvent,
    ) -> AppliedUnfilledOrderEvent:
        application = real_apply(order, reservation, event)
        applied.append(application)
        return application

    monkeypatch.setattr(DeterministicSimulator, "simulate", forged_simulate)
    monkeypatch.setattr(run_module, "apply_unfilled_order_event", tracked_apply)
    simulation = _unfilled_expired_simulation()

    with pytest.raises(ValueValidationError, match="canonical"):
        simulation.run_decision_and_execution()

    assert len(applied) == 1
    opened_application = applied[0]
    assert opened_application.order.state is OrderState.OPEN
    assert opened_application.reservation.state is ReservationState.OPEN
    assert opened_application.reservation.maximum_loss == Money("100")


def test_unfilled_simulator_rejects_invalid_script_or_marketable_terms() -> None:
    result = _unfilled_expired_simulation().run_decision_and_execution()
    assert isinstance(result, UnfilledExpiredResult)
    _, order = create_reserved_order(
        campaign_id=result.campaign.campaign_id,
        proposal=result.proposal,
        allocation=result.allocation,
        risk_decision=result.risk_decision,
    )
    assert isinstance(order, ExpiringLimitOrder)
    script = _execution_script(FixtureReader().load_decision("unfilled-expired"))
    assert isinstance(script, UnfilledExpiryExecutionScript)
    simulator = DeterministicSimulator()

    with pytest.raises(SimulationError, match="not contiguous"):
        simulator.simulate(
            order,
            replace(script, expired_sequence=3),
            result.information_set.market,
        )
    with pytest.raises(SimulationError, match="after opening"):
        simulator.simulate(
            order,
            replace(script, expires_at=script.opened_at),
            result.information_set.market,
        )
    with pytest.raises(SimulationError, match="does not match"):
        simulator.simulate(
            order,
            replace(script, executable_yes_ask="0.41"),
            result.information_set.market,
        )
    with pytest.raises(ValueValidationError, match="nonmarketable"):
        replace(order, limit_price=order.executable_ask)


def test_unfilled_expiration_never_loads_settlement_or_creates_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("unfilled expiration crossed into position or settlement")

    monkeypatch.setattr(FixtureReader, "load_settlement", forbidden)
    monkeypatch.setattr(run_module, "initial_position", forbidden)
    monkeypatch.setattr(run_module, "settle_position", forbidden)
    report = json.loads(demo_core("unfilled-expired", Money("1000"), 20260716))
    assert report["run_outcome"] == "UNFILLED_EXPIRED"
    assert "position" not in report
    assert "settlement" not in report


def test_unfilled_result_rejects_inconsistent_terminal_state() -> None:
    result = _unfilled_expired_simulation().run_decision_and_execution()
    assert isinstance(result, UnfilledExpiredResult)
    with pytest.raises(ValueError, match="EXPIRED"):
        replace(result, order=replace(result.order, state=OrderState.OPEN))
    with pytest.raises(ValueError, match="fill or reservation"):
        replace(
            result,
            order=replace(result.order, filled_quantity=Quantity("1", "1")),
        )
    with pytest.raises(ValueError, match="quantity must match"):
        replace(
            result,
            order=replace(result.order, quantity=Quantity("249", "1")),
        )
    with pytest.raises(ValueError, match="contiguous OPENED then EXPIRED"):
        replace(
            result,
            order=replace(result.order, last_execution_sequence=1),
        )
    with pytest.raises(ValueError, match="HELD, OPEN, RELEASED"):
        replace(
            result,
            reservation_lifecycle=(
                result.initial_reservation,
                result.initial_reservation,
                result.final_reservation,
            ),
        )
    with pytest.raises(ValueError, match="restore campaign risk capacity"):
        replace(result, risk_capacity_after_expiration=Money("999"))
    with pytest.raises(ValueError, match="contribution journal"):
        replace(result, journals=())
    with pytest.raises(ValueError, match="trading financial state"):
        replace(result, metrics=replace(result.metrics, fees=Money("1")))


def test_partial_fill_canceled_terminal_risk_and_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"risk": 0, "authorize": 0, "simulate": 0}
    real_risk = RiskKernel.evaluate
    real_authorize = AtomicOrderBook.authorize_and_reserve
    real_simulate = DeterministicSimulator.simulate

    def counted_risk(*args: object, **kwargs: object) -> object:
        calls["risk"] += 1
        return real_risk(*args, **kwargs)  # type: ignore[arg-type]

    def counted_authorize(self: AtomicOrderBook, **kwargs: object) -> object:
        calls["authorize"] += 1
        return real_authorize(self, **kwargs)  # type: ignore[arg-type]

    def counted_simulate(
        self: DeterministicSimulator,
        *args: object,
        **kwargs: object,
    ) -> object:
        calls["simulate"] += 1
        return real_simulate(self, *args, **kwargs)  # type: ignore[arg-type]

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("partial-fill cancellation reached settlement scaffolding")

    monkeypatch.setattr(RiskKernel, "evaluate", staticmethod(counted_risk))
    monkeypatch.setattr(AtomicOrderBook, "authorize_and_reserve", counted_authorize)
    monkeypatch.setattr(DeterministicSimulator, "simulate", counted_simulate)
    monkeypatch.setattr(run_module, "initial_position", forbidden)
    monkeypatch.setattr(run_module, "settle_position", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "settlement", forbidden)

    simulation = _partial_fill_canceled_simulation()
    result = simulation.run_decision_and_execution()
    assert isinstance(result, PartialFillCanceledResult)
    assert simulation.run_decision_and_execution() == result
    assert simulation.finalize() == result
    assert calls == {"risk": 1, "authorize": 1, "simulate": 1}

    assert result.run_outcome is RunOutcome.PARTIAL_FILL_CANCELED
    assert result.information_set.valid
    assert result.strategy_evaluation.outcome is StrategyOutcome.PROPOSAL
    assert result.strategy_evaluation.proposal == result.proposal
    assert result.proposal.side is Side.YES
    assert result.proposal.limit_price == Price("0.39", "0.01")
    assert result.proposal.executable_ask == Price("0.40", "0.01")
    assert result.proposal.maximum_loss_per_unit == Money("0.40")
    assert result.allocation.accepted
    assert result.allocation.quantity == Quantity("250", "1")
    assert result.allocation.maximum_loss == Money("100")
    assert result.risk_decision.accepted

    assert tuple(item.state for item in result.reservation_lifecycle) == (
        ReservationState.HELD,
        ReservationState.OPEN,
        ReservationState.OPEN,
        ReservationState.RELEASED,
    )
    assert tuple(item.maximum_loss.amount for item in result.reservation_lifecycle) == (
        Decimal("100"),
        Decimal("100"),
        Decimal("60"),
        Decimal("0"),
    )
    assert result.released_at_cancellation == Money("60")

    assert result.order.state is OrderState.CANCELED
    assert result.order.quantity == Quantity("250", "1")
    assert result.order.filled_quantity == Quantity("100", "1")
    assert result.order.remaining_quantity == Quantity("150", "1")
    assert result.order.reserved_maximum_loss == Money(0)
    assert tuple(event.event_type for event in result.execution_events) == (
        ExecutionEventType.OPENED,
        ExecutionEventType.FILL,
        ExecutionEventType.CANCELED,
    )
    assert tuple(event.sequence for event in result.execution_events) == (1, 2, 3)
    fill = result.execution_events[1]
    assert fill.quantity == Quantity("100", "1")
    assert fill.price == Price("0.39", "0.01")
    assert fill.fee == Money("1")

    assert isinstance(result.position, FilledPositionState)
    assert result.position.open_quantity == fill.quantity
    assert result.position.cost_basis == Money("39")
    assert result.position.maximum_remaining_loss == Money("40")
    assert result.position.settlement_state is SettlementState.OPEN
    assert result.position.originating_order_id == result.order.order_id
    assert result.position.originating_fill_event_id == fill.event_id
    assert result.active_risk_after_partial_fill == Money("100")
    assert (
        active_campaign_risk(
            result.position.maximum_remaining_loss,
            result.reservation_after_partial_fill.maximum_loss,
        )
        == result.initial_reservation.maximum_loss
    )
    assert result.final_active_campaign_risk == Money("40")
    assert result.risk_capacity_before_order == Money("1000")
    assert result.risk_capacity_while_fully_reserved == Money("900")
    assert result.risk_capacity_after_partial_fill == Money("900")
    assert result.risk_capacity_after_cancellation == Money("960")

    assert len(result.journals) == 3
    by_type = {journal.journal_type: journal for journal in result.journals}
    assert frozenset(by_type) == frozenset(
        (JournalType.CONTRIBUTION, JournalType.PURCHASE, JournalType.FEE)
    )
    assert all(journal.balanced for journal in result.journals)
    assert by_type[JournalType.PURCHASE].source_id == fill.event_id
    assert by_type[JournalType.FEE].source_id == fill.event_id
    purchase_amounts = {posting.amount.amount for posting in by_type[JournalType.PURCHASE].postings}
    fee_amounts = {posting.amount.amount for posting in by_type[JournalType.FEE].postings}
    assert purchase_amounts == {Decimal("39")}
    assert fee_amounts == {Decimal("1")}
    assert result.metrics.contributions == Money("1000")
    assert result.metrics.cash == Money("960")
    assert result.metrics.position_cost == Money("39")
    assert result.metrics.ledger_equity == Money("999")
    assert result.metrics.realized_trading_pnl_before_fees == Decimal(0)
    assert result.metrics.fees == Money("1")
    assert result.metrics.net_trading_pnl == Decimal("-1")
    assert not hasattr(result, "settlement")


def test_partial_fill_canceled_never_loads_or_applies_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("partial-fill canceled run crossed into settlement")

    reader = FixtureReader()
    fixture = reader.load_decision("partial-fill-canceled")
    monkeypatch.setattr(run_module, "settle_position", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "settlement", forbidden)
    result = run_simulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("partial-fill-canceled")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        settlement_loader=forbidden,  # type: ignore[arg-type]
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )
    assert isinstance(result, PartialFillCanceledResult)

    completed = _partial_fill_canceled_simulation()
    assert isinstance(
        completed.run_decision_and_execution(),
        PartialFillCanceledResult,
    )
    golden_settlement = _settlement_inputs(FixtureReader().load_settlement("golden"))
    with pytest.raises(RuntimeError, match="cannot load settlement"):
        completed.apply_settlement(golden_settlement)

    monkeypatch.setattr(FixtureReader, "load_settlement", forbidden)
    report = json.loads(demo_core("partial-fill-canceled", Money("1000"), 20260716))
    assert report["run_outcome"] == "PARTIAL_FILL_CANCELED"
    assert "settlement" not in report


def test_interrupted_partial_fill_fails_closed_and_cannot_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_simulate = DeterministicSimulator.simulate

    def interrupted_simulate(
        self: DeterministicSimulator,
        *args: object,
        **kwargs: object,
    ) -> tuple[ExecutionEvent, ...]:
        events = real_simulate(self, *args, **kwargs)  # type: ignore[arg-type]
        return events[:2]

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("interrupted partial-fill state reached settlement")

    monkeypatch.setattr(DeterministicSimulator, "simulate", interrupted_simulate)
    monkeypatch.setattr(run_module, "settle_position", forbidden)
    monkeypatch.setattr(run_module.JournalFactory, "settlement", forbidden)
    simulation = _partial_fill_canceled_simulation()

    with pytest.raises(RuntimeError, match="did not produce complete state"):
        simulation.run_decision_and_execution()
    with pytest.raises(RuntimeError, match="state is incomplete"):
        simulation.run_decision_and_execution()
    with pytest.raises(RuntimeError, match="cannot finalize while incomplete"):
        simulation.finalize()

    reader = FixtureReader()
    settlement = _settlement_inputs(reader.load_settlement("golden"))
    with pytest.raises(RuntimeError, match="cannot load settlement"):
        simulation.apply_settlement(settlement)


def test_partial_fill_cancel_domain_transitions_fail_without_mutation() -> None:
    result = _partial_fill_canceled_simulation().run_decision_and_execution()
    assert isinstance(result, PartialFillCanceledResult)
    reservation, order = create_reserved_order(
        campaign_id=result.campaign.campaign_id,
        proposal=result.proposal,
        allocation=result.allocation,
        risk_decision=result.risk_decision,
    )
    assert isinstance(order, ExpiringLimitOrder)
    opened_event, fill_event, canceled_event = result.execution_events

    def apply(
        current_order: ExpiringLimitOrder,
        current_reservation: RiskReservation,
        current_position: FilledPositionState | None,
        event: ExecutionEvent,
    ) -> AppliedPartialFillCanceledEvent:
        return apply_partial_fill_canceled_event(
            current_order,
            current_reservation,
            current_position,
            event,
            instrument_id=result.proposal.instrument_id,
            rule_version_id=result.proposal.rule_version_id,
            maximum_loss_per_unit=result.proposal.maximum_loss_per_unit,
            fee_per_unit=result.proposal.fee_per_unit,
        )

    ready_bytes = canonical_json_bytes({"order": order, "reservation": reservation})
    foreign_open = replace(
        opened_event,
        event_id="execution_event_foreign_order",
        order_id="order-foreign",
        payload_hash=ContentHash("0" * 64),
    )
    with pytest.raises(ValueValidationError, match="another order"):
        apply(order, reservation, None, foreign_open)
    with pytest.raises(ValueValidationError, match="out of sequence"):
        apply(order, reservation, None, fill_event)
    with pytest.raises(ValueValidationError, match="out of sequence"):
        apply(order, reservation, None, canceled_event)
    assert canonical_json_bytes({"order": order, "reservation": reservation}) == ready_bytes

    opened = apply(order, reservation, None, opened_event)
    assert opened.order.state is OrderState.OPEN
    assert opened.reservation.maximum_loss == Money("100")
    assert opened.position is None
    open_bytes = canonical_json_bytes(opened)
    with pytest.raises(ValueValidationError, match="out of sequence"):
        apply(opened.order, opened.reservation, None, canceled_event)
    with pytest.raises(ValueValidationError, match="out of sequence"):
        apply(
            opened.order,
            opened.reservation,
            None,
            replace(
                fill_event,
                event_id="execution_event_gap",
                sequence=4,
                payload_hash=ContentHash("a" * 64),
            ),
        )
    assert canonical_json_bytes(opened) == open_bytes

    filled = apply(opened.order, opened.reservation, None, fill_event)
    assert filled.order.state is OrderState.PARTIALLY_FILLED
    assert filled.order.filled_quantity == Quantity("100", "1")
    assert filled.reservation.maximum_loss == Money("60")
    assert isinstance(filled.position, FilledPositionState)
    assert filled.position.maximum_remaining_loss == Money("40")
    assert active_campaign_risk(
        filled.position.maximum_remaining_loss,
        filled.reservation.maximum_loss,
    ) == Money("100")
    filled_bytes = canonical_json_bytes(filled)
    with pytest.raises(ValueValidationError, match="risk decomposition"):
        apply(
            filled.order,
            filled.reservation,
            replace(filled.position, originating_order_id="order-foreign"),
            canceled_event,
        )
    assert canonical_json_bytes(filled) == filled_bytes

    canceled = apply(
        filled.order,
        filled.reservation,
        filled.position,
        canceled_event,
    )
    assert canceled.order.state is OrderState.CANCELED
    assert canceled.reservation.state is ReservationState.RELEASED
    assert canceled.released_maximum_loss == Money("60")
    assert canceled.position == filled.position
    terminal_bytes = canonical_json_bytes(canceled)
    for invalid_type in (
        ExecutionEventType.OPENED,
        ExecutionEventType.FILL,
        ExecutionEventType.EXPIRED,
    ):
        invalid = replace(
            canceled_event,
            event_id=f"execution_event_after_cancel_{invalid_type.value.lower()}",
            sequence=4,
            event_type=invalid_type,
            payload_hash=ContentHash("b" * 64),
        )
        with pytest.raises(ValueValidationError):
            apply(canceled.order, canceled.reservation, canceled.position, invalid)
    assert canonical_json_bytes(canceled) == terminal_bytes


def test_partial_fill_application_idempotency_and_conflicts_fail_closed() -> None:
    simulation = _partial_fill_canceled_simulation()
    result = simulation.run_decision_and_execution()
    assert isinstance(result, PartialFillCanceledResult)
    original_report = _render(simulation)
    for event in result.execution_events:
        duplicate = simulation.apply_partial_execution(event)
        assert duplicate.decision.order == result.order
        assert duplicate.position == result.position
        assert duplicate.events == result.execution_events
        assert duplicate.metrics == result.metrics
    assert _render(simulation) == original_report

    fill = result.execution_events[1]
    canceled = result.execution_events[2]
    with pytest.raises(SimulationConflictError):
        simulation.apply_partial_execution(replace(fill, fee=Money("2")))
    with pytest.raises(SimulationConflictError):
        simulation.apply_partial_execution(replace(canceled, payload_hash=ContentHash("c" * 64)))
    new_fill = replace(
        fill,
        event_id="execution_event_second_fill",
        sequence=4,
        payload_hash=ContentHash("d" * 64),
    )
    with pytest.raises(ValueValidationError):
        simulation.apply_partial_execution(new_fill)
    assert _render(simulation) == original_report


@pytest.mark.parametrize(
    ("event_index", "tamper"),
    ((1, "event_id"), (1, "payload_hash"), (2, "event_id"), (2, "payload_hash")),
)
def test_partial_first_seen_forged_events_fail_before_risk_or_money_mutation(
    event_index: int,
    tamper: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_simulate = DeterministicSimulator.simulate
    real_apply = run_module.apply_partial_fill_canceled_event
    applications: list[AppliedPartialFillCanceledEvent] = []

    def forged_simulate(
        self: DeterministicSimulator,
        *args: object,
        **kwargs: object,
    ) -> tuple[ExecutionEvent, ...]:
        events = list(real_simulate(self, *args, **kwargs))  # type: ignore[arg-type]
        target = events[event_index]
        if tamper == "event_id":
            target = replace(target, event_id="execution_event_forged")
        else:
            target = replace(target, payload_hash=ContentHash("f" * 64))
        events[event_index] = target
        return tuple(events)

    def tracked_apply(
        order: ExpiringLimitOrder,
        reservation: RiskReservation,
        position: FilledPositionState | None,
        event: ExecutionEvent,
        **kwargs: object,
    ) -> AppliedPartialFillCanceledEvent:
        applied = real_apply(
            order,
            reservation,
            position,
            event,
            **kwargs,  # type: ignore[arg-type]
        )
        applications.append(applied)
        return applied

    monkeypatch.setattr(DeterministicSimulator, "simulate", forged_simulate)
    monkeypatch.setattr(run_module, "apply_partial_fill_canceled_event", tracked_apply)
    simulation = _partial_fill_canceled_simulation()

    with pytest.raises(ValueValidationError, match="canonical"):
        simulation.run_decision_and_execution()

    expected_applied = event_index
    assert len(applications) == expected_applied
    if event_index == 1:
        assert applications[-1].order.state is OrderState.OPEN
        assert applications[-1].reservation.maximum_loss == Money("100")
        assert applications[-1].position is None
    else:
        assert applications[-1].order.state is OrderState.PARTIALLY_FILLED
        assert applications[-1].reservation.maximum_loss == Money("60")
        assert applications[-1].position is not None
        assert applications[-1].position.maximum_remaining_loss == Money("40")


def test_partial_fill_simulator_rejects_invalid_script() -> None:
    result = _partial_fill_canceled_simulation().run_decision_and_execution()
    assert isinstance(result, PartialFillCanceledResult)
    _, order = create_reserved_order(
        campaign_id=result.campaign.campaign_id,
        proposal=result.proposal,
        allocation=result.allocation,
        risk_decision=result.risk_decision,
    )
    assert isinstance(order, ExpiringLimitOrder)
    script = _execution_script(FixtureReader().load_decision("partial-fill-canceled"))
    assert isinstance(script, PartialFillCanceledExecutionScript)
    simulator = DeterministicSimulator()

    invalid_scripts = (
        replace(script, fill_quantity="0"),
        replace(script, fill_quantity="250", remaining_quantity="0"),
        replace(script, remaining_quantity="149"),
        replace(script, fill_price="0.40"),
        replace(script, fee_per_contract="0"),
        replace(script, fill_sequence=3),
        replace(script, canceled_at=script.filled_at),
        replace(script, executable_yes_ask="0.41"),
    )
    for invalid_script in invalid_scripts:
        with pytest.raises(SimulationError):
            simulator.simulate(order, invalid_script, result.information_set.market)


def test_partial_fill_canceled_result_rejects_inconsistent_terminal_state() -> None:
    result = _partial_fill_canceled_simulation().run_decision_and_execution()
    assert isinstance(result, PartialFillCanceledResult)
    with pytest.raises(ValueError, match="order state"):
        replace(result, order=replace(result.order, state=OrderState.PARTIALLY_FILLED))
    with pytest.raises(ValueError, match="gap or double count"):
        replace(
            result,
            position=replace(result.position, maximum_remaining_loss=Money("39")),
        )
    with pytest.raises(ValueError, match="risk capacity"):
        replace(result, risk_capacity_after_cancellation=Money("1000"))
    with pytest.raises(ValueError, match="journals"):
        replace(result, journals=result.journals[:2])
    with pytest.raises(ValueError, match="metrics"):
        replace(result, metrics=replace(result.metrics, fees=Money("2")))


def test_partial_fill_canceled_settled_no_terminal_economics_and_provenance() -> None:
    simulation, reader = _partial_fill_canceled_settled_no_simulation()
    checkpoint = simulation.run_decision_and_execution()
    settlement_inputs = _settlement_inputs(
        reader.load_settlement("partial-fill-canceled-settled-no")
    )
    assert isinstance(settlement_inputs.fact, RuleBoundSettlementFact)

    assert checkpoint.decision.information_set.valid
    assert checkpoint.decision.strategy_evaluation.outcome is StrategyOutcome.PROPOSAL
    assert checkpoint.decision.strategy_evaluation.proposal is not None
    assert checkpoint.decision.allocation.accepted
    assert checkpoint.decision.risk_decision.accepted
    assert checkpoint.decision.order.state is OrderState.CANCELED
    assert tuple(event.event_type for event in checkpoint.events) == (
        ExecutionEventType.OPENED,
        ExecutionEventType.FILL,
        ExecutionEventType.CANCELED,
    )
    assert checkpoint.position.settlement_state is SettlementState.OPEN

    settlement = simulation.apply_settlement(settlement_inputs)
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledNoResult)
    assert settlement == result.settlement
    assert result.run_outcome is RunOutcome.PARTIAL_FILL_CANCELED_SETTLED_NO
    assert result.logical_time == datetime(2026, 7, 15, 18, 0, tzinfo=UTC)

    assert result.strategy_evaluation.proposal == result.proposal
    assert result.proposal.side is Side.YES
    assert result.proposal.limit_price == Price("0.39", "0.01")
    assert result.allocation.quantity == Quantity("250", "1")
    assert result.allocation.maximum_loss == Money("100")
    assert tuple(item.state for item in result.reservation_lifecycle) == (
        ReservationState.HELD,
        ReservationState.OPEN,
        ReservationState.OPEN,
        ReservationState.RELEASED,
    )
    assert tuple(item.maximum_loss for item in result.reservation_lifecycle) == (
        Money("100"),
        Money("100"),
        Money("60"),
        Money(0),
    )
    assert result.released_at_cancellation == Money("60")

    assert result.order == checkpoint.decision.order
    assert result.order.state is OrderState.CANCELED
    assert result.order.quantity == Quantity("250", "1")
    assert result.order.filled_quantity == Quantity("100", "1")
    assert result.order.remaining_quantity == Quantity("150", "1")
    assert result.order.reserved_maximum_loss == Money(0)
    assert result.order.last_execution_sequence == 3
    assert result.execution_events == checkpoint.events
    assert tuple(event.sequence for event in result.execution_events) == (1, 2, 3)
    assert (
        sum(event.event_type is ExecutionEventType.FILL for event in result.execution_events) == 1
    )
    assert {event.order_id for event in result.execution_events} == {result.order.order_id}

    fill = result.execution_events[1]
    assert fill.occurred_at == datetime(2026, 7, 15, 15, 0, 4, tzinfo=UTC)
    assert fill.quantity == Quantity("100", "1")
    assert fill.price == Price("0.39", "0.01")
    assert fill.fee == Money("1")
    assert result.execution_events[0].occurred_at == datetime(2026, 7, 15, 15, 0, 3, tzinfo=UTC)
    assert result.execution_events[2].occurred_at == datetime(2026, 7, 15, 15, 0, 5, tzinfo=UTC)

    assert isinstance(result.pre_settlement_position, FilledPositionState)
    assert result.pre_settlement_position.open_quantity == fill.quantity
    assert result.pre_settlement_position.cost_basis == Money("39")
    assert result.pre_settlement_position.maximum_remaining_loss == Money("40")
    assert result.pre_settlement_position.settlement_state is SettlementState.OPEN
    assert result.pre_settlement_position.originating_order_id == result.order.order_id
    assert result.pre_settlement_position.originating_fill_event_id == fill.event_id
    assert result.active_risk_after_partial_fill == Money("100")
    assert result.active_risk_after_cancellation == Money("40")

    fact = result.settlement_fact
    assert result.contract_rule == result.information_set.rule
    assert fact.instrument_id == result.information_set.instrument.instrument_id
    assert fact.rule_version_id == result.contract_rule.rule_version_id
    assert fact.rule_content_hash == result.contract_rule.content_hash
    assert fact.resolution_source == result.contract_rule.resolution_source
    assert fact.source_event_at == datetime(2026, 7, 15, 17, 0, tzinfo=UTC)
    assert fact.available_at == datetime(2026, 7, 15, 17, 0, 3, tzinfo=UTC)
    assert fact.available_at > result.execution_events[-1].occurred_at
    assert fact.available_at > result.information_set.cutoff.value
    assert fact.available_at > result.contract_rule.determination_at
    assert fact.settlement_value == Decimal(0)

    assert result.settlement.filled_quantity == fill.quantity
    assert result.settlement.settlement_cash == Money(0)
    assert result.settlement.realized_pnl == Decimal("-39")
    assert result.settlement.reservation_release == Money(0)
    assert result.settlement.position_risk_release == Money("40")
    assert result.position_risk_released_at_settlement == Money("40")

    assert isinstance(result.settled_position, SettledFilledPositionState)
    assert result.settled_position.open_quantity == Quantity(0, "1")
    assert result.settled_position.settled_quantity == fill.quantity
    assert result.settled_position.cost_basis == Money(0)
    assert result.settled_position.maximum_remaining_loss == Money(0)
    assert result.settled_position.settlement_state is SettlementState.SETTLED
    assert result.settled_position.settlement_value == Decimal(0)
    assert result.settled_position.settlement_id == result.settlement.settlement_id
    assert result.settled_position.originating_order_id == result.order.order_id
    assert result.settled_position.originating_fill_event_id == fill.event_id
    assert result.final_reservation.maximum_loss == Money(0)
    assert result.final_active_campaign_risk == Money(0)

    assert result.risk_capacity_before_order == Money("1000")
    assert result.risk_capacity_while_fully_reserved == Money("900")
    assert result.risk_capacity_after_partial_fill == Money("900")
    assert result.risk_capacity_after_cancellation == Money("960")
    assert result.risk_capacity_after_settlement == Money("960")

    by_type = {journal.journal_type: journal for journal in result.journals}
    assert frozenset(by_type) == frozenset(
        (
            JournalType.CONTRIBUTION,
            JournalType.PURCHASE,
            JournalType.FEE,
            JournalType.SETTLEMENT,
        )
    )
    assert len(result.journals) == 4
    assert all(journal.balanced for journal in result.journals)
    assert by_type[JournalType.PURCHASE].source_id == fill.event_id
    assert by_type[JournalType.FEE].source_id == fill.event_id
    settlement_journal = by_type[JournalType.SETTLEMENT]
    assert settlement_journal.source_id == fact.source_id
    assert settlement_journal.occurred_at == fact.available_at
    assert {
        (posting.account_code, posting.side, posting.amount.amount)
        for posting in settlement_journal.postings
    } == {
        (AccountCode.REALIZED_PNL, PostingSide.DEBIT, Decimal("39")),
        (AccountCode.POSITION_COST, PostingSide.CREDIT, Decimal("39")),
    }
    assert all(
        posting.account_code is not AccountCode.CASH for posting in settlement_journal.postings
    )

    assert result.pre_settlement_metrics.cash == Money("960")
    assert result.pre_settlement_metrics.position_cost == Money("39")
    assert result.pre_settlement_metrics.ledger_equity == Money("999")
    assert result.pre_settlement_metrics.fees == Money("1")
    assert result.metrics.contributions == Money("1000")
    assert result.metrics.cash == Money("960")
    assert result.metrics.position_cost == Money(0)
    assert result.metrics.ledger_equity == Money("960")
    assert result.metrics.realized_trading_pnl_before_fees == Decimal("-39")
    assert result.metrics.fees == Money("1")
    assert result.metrics.net_trading_pnl == Decimal("-40")
    assert result.metrics.ledger_equity.amount == (
        result.metrics.contributions.amount + result.metrics.net_trading_pnl
    )


def test_partial_canceled_no_loads_settlement_only_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FixtureReader()
    fixture = reader.load_decision("partial-fill-canceled-settled-no")
    settlement = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-no"))
    applied_events: list[ExecutionEventType] = []
    latest_order_states: list[OrderState] = []
    calls = {"authorize": 0, "simulate": 0, "settlement_loader": 0}
    real_apply = GoldenSimulation.apply_partial_execution
    real_authorize = AtomicOrderBook.authorize_and_reserve
    real_simulate = DeterministicSimulator.simulate

    def tracked_apply(
        self: GoldenSimulation,
        event: ExecutionEvent,
    ) -> object:
        checkpoint = real_apply(self, event)
        applied_events.append(event.event_type)
        latest_order_states.append(checkpoint.decision.order.state)
        return checkpoint

    def tracked_authorize(self: AtomicOrderBook, **kwargs: object) -> object:
        calls["authorize"] += 1
        return real_authorize(self, **kwargs)  # type: ignore[arg-type]

    def tracked_simulate(
        self: DeterministicSimulator,
        *args: object,
        **kwargs: object,
    ) -> tuple[ExecutionEvent, ...]:
        calls["simulate"] += 1
        return real_simulate(self, *args, **kwargs)  # type: ignore[arg-type]

    def load_settlement() -> SettlementInputs:
        calls["settlement_loader"] += 1
        assert applied_events == [
            ExecutionEventType.OPENED,
            ExecutionEventType.FILL,
            ExecutionEventType.CANCELED,
        ]
        assert latest_order_states[-1] is OrderState.CANCELED
        return settlement

    monkeypatch.setattr(GoldenSimulation, "apply_partial_execution", tracked_apply)
    monkeypatch.setattr(AtomicOrderBook, "authorize_and_reserve", tracked_authorize)
    monkeypatch.setattr(DeterministicSimulator, "simulate", tracked_simulate)
    result = run_simulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(
                reader.manifest_hash("partial-fill-canceled-settled-no")
            ),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        settlement_loader=load_settlement,
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )
    assert isinstance(result, PartialFillCanceledSettledNoResult)
    assert calls == {"authorize": 1, "simulate": 1, "settlement_loader": 1}


def test_partial_canceled_no_settlement_cannot_mutate_order_or_execute_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulation, reader = _partial_fill_canceled_settled_no_simulation()
    checkpoint = simulation.run_decision_and_execution()
    settlement = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-no"))
    order_before = canonical_json_bytes(checkpoint.decision.order)
    events_before = canonical_json_bytes(checkpoint.events)
    reservation_before = canonical_json_bytes(checkpoint.reservation_lifecycle[-1])

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("settlement attempted a second authorization or execution")

    monkeypatch.setattr(DeterministicSimulator, "simulate", forbidden)
    monkeypatch.setattr(AtomicOrderBook, "authorize_and_reserve", forbidden)
    monkeypatch.setattr(GoldenSimulation, "apply_partial_execution", forbidden)

    first = simulation.apply_settlement(settlement)
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledNoResult)
    assert simulation.apply_settlement(settlement) == first
    assert simulation.finalize() == result
    assert canonical_json_bytes(result.order) == order_before
    assert canonical_json_bytes(result.execution_events) == events_before
    assert canonical_json_bytes(result.final_reservation) == reservation_before
    assert result.order is checkpoint.decision.order
    assert all(
        retained is original
        for retained, original in zip(
            result.execution_events,
            checkpoint.events,
            strict=True,
        )
    )
    assert len(result.execution_events) == 3
    assert (
        sum(event.event_type is ExecutionEventType.FILL for event in result.execution_events) == 1
    )


def test_partial_canceled_no_duplicate_and_conflicting_settlement_are_atomic() -> None:
    simulation, reader = _partial_fill_canceled_settled_no_simulation()
    simulation.run_decision_and_execution()
    exact = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-no"))
    original_settlement = simulation.apply_settlement(exact)
    original_result = simulation.finalize()
    assert isinstance(original_result, PartialFillCanceledSettledNoResult)
    original_report = _render(simulation)

    assert simulation.apply_settlement(exact) == original_settlement
    assert _render(simulation) == original_report
    assert len(simulation.finalize().journals) == 4

    fact = exact.fact
    assert isinstance(fact, RuleBoundSettlementFact)
    conflicts = (
        replace(exact, settlement_file_hash=ContentHash("0" * 64)),
        replace(
            exact,
            fact=_rehashed_rule_bound_fact(
                fact,
                source_id="settlement-conflicting-source",
            ),
        ),
        replace(
            exact,
            fact=_rehashed_rule_bound_fact(
                fact,
                instrument_id="instrument-conflicting",
            ),
        ),
        replace(
            exact,
            fact=_rehashed_rule_bound_fact(
                fact,
                rule_version_id="rule-conflicting",
            ),
        ),
        replace(
            exact,
            fact=_rehashed_rule_bound_fact(
                fact,
                rule_content_hash=ContentHash("0" * 64),
            ),
        ),
        replace(
            exact,
            fact=_rehashed_rule_bound_fact(
                fact,
                resolution_source="Conflicting source",
            ),
        ),
        replace(
            exact,
            fact=_rehashed_rule_bound_fact(
                fact,
                settlement_value=Decimal("1"),
                settlement_value_text="1",
            ),
        ),
        replace(
            exact,
            fact=_rehashed_rule_bound_fact(
                fact,
                available_at=datetime(2026, 7, 15, 17, 0, 4, tzinfo=UTC),
            ),
        ),
    )
    for conflicting in conflicts:
        with pytest.raises(SimulationConflictError, match="conflicting content"):
            simulation.apply_settlement(conflicting)
        assert _render(simulation) == original_report
        retained = simulation.finalize()
        assert retained.order == original_result.order
        assert retained.execution_events == original_result.execution_events
        assert retained.journals == original_result.journals
        assert retained.metrics == original_result.metrics


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("instrument", "instrument mismatch"),
        ("rule_version", "rule version mismatch"),
        ("rule_hash", "rule content hash mismatch"),
        ("resolution_source", "resolution source mismatch"),
        ("before_execution", "before execution completed"),
        ("before_determination", "precedes contract determination"),
        ("not_no", "exact NO rule value"),
    ),
)
def test_partial_canceled_no_settlement_mismatch_fails_before_mutation(
    case: str,
    message: str,
) -> None:
    simulation, reader = _partial_fill_canceled_settled_no_simulation()
    checkpoint = simulation.run_decision_and_execution()
    exact = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-no"))
    fact = exact.fact
    assert isinstance(fact, RuleBoundSettlementFact)
    if case == "instrument":
        changed = _rehashed_rule_bound_fact(
            fact,
            instrument_id="instrument-conflicting",
        )
    elif case == "rule_version":
        changed = _rehashed_rule_bound_fact(
            fact,
            rule_version_id="rule-conflicting",
        )
    elif case == "rule_hash":
        changed = _rehashed_rule_bound_fact(
            fact,
            rule_content_hash=ContentHash("0" * 64),
        )
    elif case == "resolution_source":
        changed = _rehashed_rule_bound_fact(
            fact,
            resolution_source="Conflicting source",
        )
    elif case == "before_execution":
        changed = _rehashed_rule_bound_fact(
            fact,
            source_event_at=datetime(2026, 7, 15, 15, 0, 5, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 15, 0, 5, tzinfo=UTC),
        )
    elif case == "before_determination":
        changed = _rehashed_rule_bound_fact(
            fact,
            source_event_at=datetime(2026, 7, 15, 16, 59, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 16, 59, tzinfo=UTC),
        )
    else:
        changed = _rehashed_rule_bound_fact(
            fact,
            settlement_value=Decimal("2"),
            settlement_value_text="2",
        )

    before = canonical_json_bytes(checkpoint)
    with pytest.raises(ValueValidationError, match=message):
        simulation.apply_settlement(replace(exact, fact=changed))
    assert canonical_json_bytes(simulation.run_decision_and_execution()) == before
    with pytest.raises(RuntimeError, match="before exact NO settlement"):
        simulation.finalize()

    simulation.apply_settlement(exact)
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledNoResult)
    assert result.metrics.net_trading_pnl == Decimal("-40")


def test_partial_canceled_no_invalid_raw_payload_hash_fails_without_state_change() -> None:
    simulation, reader = _partial_fill_canceled_settled_no_simulation()
    checkpoint = simulation.run_decision_and_execution()
    exact = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-no"))
    assert isinstance(exact.fact, RuleBoundSettlementFact)
    before = canonical_json_bytes(checkpoint)
    with pytest.raises(ValueValidationError, match="raw payload hash mismatch"):
        replace(exact.fact, raw_payload_hash=ContentHash("0" * 64))
    assert canonical_json_bytes(simulation.run_decision_and_execution()) == before
    simulation.apply_settlement(exact)
    assert isinstance(simulation.finalize(), PartialFillCanceledSettledNoResult)


def test_partial_canceled_no_result_rejects_inconsistent_terminal_state() -> None:
    simulation, reader = _partial_fill_canceled_settled_no_simulation()
    simulation.run_decision_and_execution()
    simulation.apply_settlement(
        _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-no"))
    )
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledNoResult)

    with pytest.raises(ValueError):
        replace(result, order=replace(result.order, state=OrderState.OPEN))
    with pytest.raises(ValueError):
        replace(
            result,
            execution_events=(*result.execution_events, result.execution_events[1]),
        )
    with pytest.raises(ValueError):
        replace(
            result,
            pre_settlement_position=replace(
                result.pre_settlement_position,
                originating_order_id="order-conflicting",
            ),
        )
    altered_lifecycle = (
        *result.reservation_lifecycle[:3],
        replace(
            result.final_reservation,
            state=ReservationState.OPEN,
            maximum_loss=Money("1"),
        ),
    )
    with pytest.raises(ValueError):
        replace(result, reservation_lifecycle=altered_lifecycle)
    with pytest.raises(ValueError, match="derecognize"):
        replace(
            result,
            settlement=replace(result.settlement, settlement_cash=Money("1")),
        )
    with pytest.raises(ValueError, match="derecognize"):
        replace(
            result,
            settlement=replace(result.settlement, position_risk_release=Money("39")),
        )
    with pytest.raises(ValueError, match="derecognize"):
        replace(
            result,
            settlement=replace(
                result.settlement,
                settlement_id="settlement_forged",
            ),
            settled_position=replace(
                result.settled_position,
                settlement_id="settlement_forged",
            ),
        )
    with pytest.raises(ValueError, match="close exact fill provenance"):
        replace(
            result,
            settled_position=replace(
                result.settled_position,
                settled_quantity=Quantity("99", "1"),
            ),
        )
    with pytest.raises(ValueError, match="four exact balanced journals"):
        replace(
            result,
            journals=tuple(
                journal
                for journal in result.journals
                if journal.journal_type is not JournalType.SETTLEMENT
            ),
        )
    with pytest.raises(ValueError, match="derived from retained journals"):
        replace(result, metrics=replace(result.metrics, fees=Money("2")))
    with pytest.raises(ValueError, match="contract rule"):
        replace(
            result,
            contract_rule=replace(
                result.contract_rule,
                content_hash=ContentHash("0" * 64),
            ),
        )
    with pytest.raises(ValueError, match="risk capacity"):
        replace(result, risk_capacity_after_settlement=Money("1000"))


def test_only_no_proposal_may_finalize_without_settlement() -> None:
    simulation, _ = _simulation()
    simulation.run_decision_and_execution()
    with pytest.raises(RuntimeError, match="settlement"):
        simulation.finalize()


def test_strategy_evaluation_rejects_inconsistent_outcome_and_proposal() -> None:
    with pytest.raises(ValueValidationError, match="requires a proposal"):
        StrategyEvaluation(
            evaluation_id="evaluation-invalid",
            outcome=StrategyOutcome.PROPOSAL,
            information_set_id="information-invalid",
            strategy_version="strategy-invalid",
            reason_codes=("INVALID",),
            proposal=None,
        )
    simulation, _ = _simulation()
    checkpoint = simulation.run_decision_and_execution()
    with pytest.raises(ValueValidationError, match="cannot contain a proposal"):
        replace(
            checkpoint.decision.strategy_evaluation,
            outcome=StrategyOutcome.NO_PROPOSAL,
        )


def test_canonical_report_is_byte_identical_and_hash_pinned() -> None:
    first = demo_core("golden", Money("1000.00"), 20260716)
    second = demo_core("golden", Money("1000.00"), 20260716)
    assert first == second
    report = json.loads(first)
    assert report["runtime_capability"] == "SIMULATION_ONLY"
    assert report["execution_evidence"] == "SYNTHETIC"
    assert report["edge_evidence"] == "NONE"
    assert report["calibration_evidence"] == "NONE"
    assert report["schema_version"] == "2"
    assert report["run_outcome"] == "SETTLED_TRADE"
    assert sha256(first).hexdigest() == EXPECTED_REPORT_SHA256


def test_no_proposal_report_is_canonical_complete_and_hash_pinned() -> None:
    first = demo_core("no-proposal", Money("1000.00"), 20260716)
    second = demo_core("no-proposal", Money("1000.00"), 20260716)
    assert first == second
    report = json.loads(first)
    assert report["schema_version"] == "2"
    assert report["run_outcome"] == "NO_PROPOSAL"
    assert report["strategy_evaluation"]["outcome"] == "NO_PROPOSAL"
    assert report["strategy_evaluation"]["reason_codes"] == ["SYNTHETIC_EDGE_BELOW_THRESHOLD"]
    for absent in (
        "proposal",
        "allocation",
        "risk_decision",
        "reservation",
        "order",
        "execution_events",
        "position",
        "settlement",
    ):
        assert absent not in report
    assert len(report["ledger_journals"]) == 1
    assert report["final_ledger_equity"] == "1000"
    assert report["realized_trading_pnl_before_fees"] == "0"
    assert report["fees"] == "0"
    assert report["net_trading_pnl"] == "0"
    assert report["runtime_capability"] == "SIMULATION_ONLY"
    assert report["execution_evidence"] == "SYNTHETIC"
    assert report["edge_evidence"] == "NONE"
    assert report["calibration_evidence"] == "NONE"
    assert sha256(first).hexdigest() == EXPECTED_NO_PROPOSAL_REPORT_SHA256


def test_policy_rejected_report_is_canonical_complete_and_hash_pinned() -> None:
    first = demo_core("policy-rejected", Money("1000.00"), 20260716)
    second = demo_core("policy-rejected", Money("1000.00"), 20260716)
    assert first == second
    report = json.loads(first)
    assert report["schema_version"] == "2"
    assert report["run_outcome"] == "POLICY_REJECTED"
    assert report["strategy_evaluation"]["outcome"] == "PROPOSAL"
    assert report["proposal"]["expected_net_value_per_unit"] == "0.31"
    assert report["allocation"]["accepted"] is False
    assert report["allocation"]["reason_codes"] == [POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED]
    assert report["policy_reason_codes"] == [POLICY_MAX_CAPITAL_LOCKUP_EXCEEDED]
    assert report["policy_explanation"] == (
        "Expected capital lockup of 7499 seconds exceeds campaign policy maximum of 3600 seconds."
    )
    for absent in (
        "risk_decision",
        "reservation",
        "order",
        "execution_events",
        "position",
        "settlement",
    ):
        assert absent not in report
    assert len(report["ledger_journals"]) == 1
    assert report["final_ledger_equity"] == "1000"
    assert report["realized_trading_pnl_before_fees"] == "0"
    assert report["fees"] == "0"
    assert report["net_trading_pnl"] == "0"
    assert report["runtime_capability"] == "SIMULATION_ONLY"
    assert report["execution_evidence"] == "SYNTHETIC"
    assert report["edge_evidence"] == "NONE"
    assert report["calibration_evidence"] == "NONE"
    assert sha256(first).hexdigest() == EXPECTED_POLICY_REJECTED_REPORT_SHA256


def test_invariant_rejected_report_is_canonical_complete_and_hash_pinned() -> None:
    first = demo_core("invariant-rejected", Money("1000.00"), 20260716)
    second = demo_core("invariant-rejected", Money("1000.00"), 20260716)
    assert first == second
    report = json.loads(first)
    assert report["schema_version"] == "2"
    assert report["run_outcome"] == "INVARIANT_REJECTED"
    assert report["strategy_evaluation"]["outcome"] == "PROPOSAL"
    assert report["proposal"]["side"] == "YES"
    assert report["proposal"]["expected_net_value_per_unit"] == "0.31"
    assert report["allocation"]["accepted"] is True
    assert report["allocation"]["reason_codes"] == ["ALLOCATED"]
    assert report["risk_decision"]["accepted"] is False
    assert report["risk_decision"]["allocation_id"] == report["allocation"]["allocation_id"]
    assert report["risk_decision"]["reason_codes"] == [INVARIANT_MARKET_DATA_STALE]
    assert report["invariant_reason_codes"] == [INVARIANT_MARKET_DATA_STALE]
    evidence = report["freshness_evidence"]
    assert evidence["observation_source_id"] == "source-book-1"
    assert evidence["observation_available_at"] == "2026-07-15T14:58:01Z"
    assert evidence["authorization_at"] == "2026-07-15T15:03:02Z"
    assert evidence["observed_age_seconds"] == 301
    assert evidence["maximum_age_seconds"] == 300
    assert evidence["information_set_id"] == report["information_set"]["information_set_id"]
    for expected_fragment in ("14:58:01Z", "15:03:02Z", "301", "300"):
        assert expected_fragment in report["invariant_explanation"]
    for absent in (
        "reservation",
        "order",
        "execution_events",
        "position",
        "settlement",
    ):
        assert absent not in report
    assert len(report["ledger_journals"]) == 1
    assert report["ledger_journals"][0]["journal_type"] == "CONTRIBUTION"
    assert report["final_ledger_equity"] == "1000"
    assert report["realized_trading_pnl_before_fees"] == "0"
    assert report["fees"] == "0"
    assert report["net_trading_pnl"] == "0"
    assert report["runtime_capability"] == "SIMULATION_ONLY"
    assert report["execution_evidence"] == "SYNTHETIC"
    assert report["edge_evidence"] == "NONE"
    assert report["calibration_evidence"] == "NONE"
    assert sha256(first).hexdigest() == EXPECTED_INVARIANT_REJECTED_REPORT_SHA256


def test_unfilled_expired_report_is_canonical_complete_and_hash_pinned() -> None:
    first = demo_core("unfilled-expired", Money("1000.00"), 20260716)
    second = demo_core("unfilled-expired", Money("1000.00"), 20260716)
    assert first == second
    report = json.loads(first)
    assert report["schema_version"] == "2"
    assert report["run_outcome"] == "UNFILLED_EXPIRED"
    assert report["strategy_evaluation"]["outcome"] == "PROPOSAL"
    assert report["proposal"]["side"] == "YES"
    assert report["proposal"]["expected_net_value_per_unit"] == "0.32"
    assert report["proposal"]["maximum_loss_per_unit"]["amount"] == "0.4"
    assert report["allocation"]["accepted"] is True
    assert report["allocation"]["quantity"]["value"] == "250"
    assert report["allocation"]["maximum_loss"]["amount"] == "100"
    assert report["risk_decision"]["accepted"] is True
    assert report["risk_decision"]["reason_codes"] == ["ALL_INVARIANTS_PASSED"]
    assert report["order"]["state"] == "EXPIRED"
    assert report["order_limit_price"]["value"] == "0.39"
    assert report["synthetic_executable_ask"]["value"] == "0.4"
    assert report["ordered_quantity"]["value"] == "250"
    assert report["filled_quantity"]["value"] == "0"
    assert report["remaining_quantity"]["value"] == "250"
    assert [item["state"] for item in report["reservation_lifecycle"]] == [
        "HELD",
        "OPEN",
        "RELEASED",
    ]
    assert report["initial_reserved_maximum_loss"]["amount"] == "100"
    assert report["final_reservation"]["maximum_loss"]["amount"] == "0"
    assert report["released_reservation_amount"]["amount"] == "100"
    assert report["risk_capacity_before_order"]["amount"] == "1000"
    assert report["risk_capacity_while_open"]["amount"] == "900"
    assert report["risk_capacity_after_expiration"]["amount"] == "1000"
    assert [event["event_type"] for event in report["execution_events"]] == [
        "OPENED",
        "EXPIRED",
    ]
    assert all(event["quantity"]["value"] == "0" for event in report["execution_events"])
    assert all(event["price"] is None for event in report["execution_events"])
    assert all(event["fee"]["amount"] == "0" for event in report["execution_events"])
    for absent in ("fills", "position", "settlement"):
        assert absent not in report
    assert len(report["ledger_journals"]) == 1
    assert report["ledger_journals"][0]["journal_type"] == "CONTRIBUTION"
    assert report["final_cash"]["amount"] == "1000"
    assert report["final_ledger_equity"] == "1000"
    assert report["realized_trading_pnl_before_fees"] == "0"
    assert report["fees"] == "0"
    assert report["net_trading_pnl"] == "0"
    assert report["runtime_capability"] == "SIMULATION_ONLY"
    assert report["execution_evidence"] == "SYNTHETIC"
    assert report["edge_evidence"] == "NONE"
    assert report["calibration_evidence"] == "NONE"
    assert sha256(first).hexdigest() == EXPECTED_UNFILLED_EXPIRED_REPORT_SHA256


def test_partial_fill_canceled_report_is_canonical_complete_and_hash_pinned() -> None:
    first = demo_core("partial-fill-canceled", Money("1000.00"), 20260716)
    second = demo_core("partial-fill-canceled", Money("1000.00"), 20260716)
    assert first == second
    report = json.loads(first)
    assert report["schema_version"] == "2"
    assert report["run_outcome"] == "PARTIAL_FILL_CANCELED"
    assert report["runtime_capability"] == "SIMULATION_ONLY"
    assert report["execution_evidence"] == "SYNTHETIC"
    assert report["edge_evidence"] == "NONE"
    assert report["calibration_evidence"] == "NONE"
    assert report["strategy_evaluation"]["outcome"] == "PROPOSAL"
    assert report["proposal"]["side"] == "YES"
    assert report["proposal"]["limit_price"]["value"] == "0.39"
    assert report["allocation"]["accepted"] is True
    assert report["allocation"]["quantity"]["value"] == "250"
    assert report["allocation"]["maximum_loss"]["amount"] == "100"
    assert report["risk_decision"]["accepted"] is True
    assert report["order"]["state"] == "CANCELED"
    assert report["ordered_quantity"]["value"] == "250"
    assert report["filled_quantity"]["value"] == "100"
    assert report["remaining_quantity"]["value"] == "150"
    assert [event["event_type"] for event in report["execution_events"]] == [
        "OPENED",
        "FILL",
        "CANCELED",
    ]
    assert [event["sequence"] for event in report["execution_events"]] == [1, 2, 3]
    assert [item["maximum_loss"]["amount"] for item in report["reservation_lifecycle"]] == [
        "100",
        "100",
        "60",
        "0",
    ]
    assert report["released_at_cancellation"]["amount"] == "60"
    assert report["position"]["open_quantity"]["value"] == "100"
    assert report["position"]["cost_basis"]["amount"] == "39"
    assert report["position_maximum_remaining_loss"]["amount"] == "40"
    assert report["active_risk_after_partial_fill"]["amount"] == "100"
    assert report["total_active_campaign_risk"]["amount"] == "40"
    assert report["risk_capacity_before_order"]["amount"] == "1000"
    assert report["risk_capacity_while_fully_reserved"]["amount"] == "900"
    assert report["risk_capacity_after_partial_fill"]["amount"] == "900"
    assert report["risk_capacity_after_cancellation"]["amount"] == "960"
    assert report["position_valuation_basis"] == "CARRIED_AT_COST"
    assert report["fee_accounting_basis"] == "EXPENSED_SEPARATELY"
    assert report["mark_evidence"] == "NONE"
    assert report["final_cash"]["amount"] == "960"
    assert report["position_cost"]["amount"] == "39"
    assert report["final_ledger_equity"] == "999"
    assert report["realized_settlement_pnl"] == "0"
    assert report["fees"] == "1"
    assert report["net_trading_pnl"] == "-1"
    assert {journal["journal_type"] for journal in report["ledger_journals"]} == {
        "CONTRIBUTION",
        "PURCHASE",
        "FEE",
    }
    for absent in (
        "settlement",
        "settlement_journal",
        "position_close",
        "second_fill",
    ):
        assert absent not in report
    assert sha256(first).hexdigest() == EXPECTED_PARTIAL_FILL_CANCELED_REPORT_SHA256


def test_partial_canceled_settled_no_report_is_explicit_canonical_and_hash_pinned() -> None:
    first = demo_core(
        "partial-fill-canceled-settled-no",
        Money("1000.00"),
        20260716,
    )
    second = demo_core(
        "partial-fill-canceled-settled-no",
        Money("1000.00"),
        20260716,
    )
    assert first == second
    report = json.loads(first)
    assert report["schema_version"] == "2"
    assert report["run_outcome"] == "PARTIAL_FILL_CANCELED_SETTLED_NO"
    assert report["runtime_capability"] == "SIMULATION_ONLY"
    assert report["execution_evidence"] == "SYNTHETIC"
    assert report["edge_evidence"] == "NONE"
    assert report["calibration_evidence"] == "NONE"
    assert report["strategy_evaluation"]["outcome"] == "PROPOSAL"
    assert report["proposal"]["side"] == "YES"
    assert report["allocation"]["accepted"] is True
    assert report["allocation"]["quantity"]["value"] == "250"
    assert report["allocation"]["maximum_loss"]["amount"] == "100"
    assert report["risk_decision"]["accepted"] is True

    order = report["order"]
    assert order["state"] == "CANCELED"
    assert order["quantity"]["value"] == "250"
    assert order["filled_quantity"]["value"] == "100"
    assert report["remaining_quantity"]["value"] == "150"
    assert order["reserved_maximum_loss"]["amount"] == "0"
    assert order["last_execution_sequence"] == 3
    events = report["execution_events"]
    assert len(events) == 3
    assert [event["event_type"] for event in events] == [
        "OPENED",
        "FILL",
        "CANCELED",
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert sum(event["event_type"] == "FILL" for event in events) == 1
    assert {event["order_id"] for event in events} == {order["order_id"]}

    assert [item["maximum_loss"]["amount"] for item in report["reservation_lifecycle"]] == [
        "100",
        "100",
        "60",
        "0",
    ]
    assert report["released_at_cancellation"]["amount"] == "60"
    assert report["final_reservation"]["state"] == "RELEASED"
    assert report["final_reservation"]["maximum_loss"]["amount"] == "0"
    assert report["risk_capacity_before_order"]["amount"] == "1000"
    assert report["risk_capacity_while_fully_reserved"]["amount"] == "900"
    assert report["risk_capacity_after_partial_fill"]["amount"] == "900"
    assert report["risk_capacity_after_cancellation"]["amount"] == "960"
    assert report["risk_capacity_after_settlement"]["amount"] == "960"

    pre_position = report["pre_settlement_position"]
    assert pre_position["side"] == "YES"
    assert pre_position["open_quantity"]["value"] == "100"
    assert pre_position["cost_basis"]["amount"] == "39"
    assert pre_position["maximum_remaining_loss"]["amount"] == "40"
    assert pre_position["settlement_state"] == "OPEN"
    assert pre_position["originating_order_id"] == order["order_id"]
    assert pre_position["originating_fill_event_id"] == events[1]["event_id"]
    assert report["position_risk_before_settlement"]["amount"] == "40"
    assert report["active_risk_after_partial_fill"]["amount"] == "100"
    assert report["active_risk_after_cancellation"]["amount"] == "40"

    rule = report["contract_rule_version"]
    fact = report["settlement_fact"]
    settlement = report["settlement_result"]
    assert fact["instrument_id"] == report["selected_instrument"]
    assert fact["rule_version_id"] == rule["rule_version_id"]
    assert fact["rule_content_hash"] == rule["content_hash"]
    assert fact["resolution_source"] == rule["resolution_source"]
    assert fact["source_event_at"] == "2026-07-15T17:00:00Z"
    assert fact["available_at"] == "2026-07-15T17:00:03Z"
    assert fact["settlement_value"] == "0"
    assert report["settlement_value"] == "0"
    assert report["settlement_cash"]["amount"] == "0"
    assert settlement["settlement_cash"]["amount"] == "0"
    assert settlement["realized_pnl"] == "-39"
    assert settlement["reservation_release"]["amount"] == "0"
    assert settlement["position_risk_release"]["amount"] == "40"
    assert report["realized_settlement_pnl"] == "-39"
    assert report["position_risk_released_at_settlement"]["amount"] == "40"

    settled_position = report["settled_position"]
    assert settled_position["settlement_state"] == "SETTLED"
    assert settled_position["open_quantity"]["value"] == "0"
    assert settled_position["settled_quantity"]["value"] == "100"
    assert settled_position["cost_basis"]["amount"] == "0"
    assert settled_position["maximum_remaining_loss"]["amount"] == "0"
    assert settled_position["settlement_value"] == "0"
    assert settled_position["settlement_id"] == settlement["settlement_id"]
    assert settled_position["originating_order_id"] == order["order_id"]
    assert settled_position["originating_fill_event_id"] == events[1]["event_id"]
    assert report["final_position_risk"]["amount"] == "0"
    assert report["total_active_campaign_risk"]["amount"] == "0"

    assert report["final_cash"]["amount"] == "960"
    assert report["position_cost"]["amount"] == "0"
    assert report["final_ledger_equity"] == "960"
    assert report["realized_trading_pnl_before_fees"] == "-39"
    assert report["fees"] == "1"
    assert report["net_trading_pnl"] == "-40"
    assert report["position_valuation_basis"] == "FINAL_RULE_SETTLEMENT"
    assert report["fee_accounting_basis"] == "EXPENSED_SEPARATELY"
    assert report["mark_evidence"] == "NONE"
    journals = report["ledger_journals"]
    assert len(journals) == 4
    assert {journal["journal_type"] for journal in journals} == {
        "CONTRIBUTION",
        "PURCHASE",
        "FEE",
        "SETTLEMENT",
    }
    settlement_journal = next(
        journal for journal in journals if journal["journal_type"] == "SETTLEMENT"
    )
    assert {
        (
            posting["account_code"],
            posting["side"],
            posting["amount"]["amount"],
        )
        for posting in settlement_journal["postings"]
    } == {
        ("REALIZED_PNL", "DEBIT", "39"),
        ("POSITION_COST", "CREDIT", "39"),
    }

    def all_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(
                *(all_keys(member) for member in value.values()),
            )
        if isinstance(value, list):
            return set().union(*(all_keys(member) for member in value))
        return set()

    forbidden = {
        "orders",
        "replacement_order",
        "closing_order",
        "second_order",
        "closing_trade",
        "synthetic_sale",
        "full_fill_event",
        "second_fill",
        "void_result",
        "fills",
    }
    assert forbidden.isdisjoint(all_keys(report))
    assert sha256(first).hexdigest() == (EXPECTED_PARTIAL_FILL_CANCELED_SETTLED_NO_REPORT_SHA256)


def test_partial_fill_canceled_settled_void_terminal_economics_and_provenance() -> None:
    simulation, reader = _partial_fill_canceled_settled_void_simulation()
    checkpoint = simulation.run_decision_and_execution()
    settlement_inputs = _settlement_inputs(
        reader.load_settlement("partial-fill-canceled-settled-void")
    )
    assert isinstance(settlement_inputs.fact, ClassifiedRuleBoundSettlementFact)
    assert checkpoint.decision.information_set.valid
    assert checkpoint.decision.strategy_evaluation.outcome is StrategyOutcome.PROPOSAL
    assert checkpoint.decision.allocation.accepted
    assert checkpoint.decision.risk_decision.accepted
    assert checkpoint.decision.order.state is OrderState.CANCELED
    assert checkpoint.position.settlement_state is SettlementState.OPEN
    order_before = canonical_json_bytes(checkpoint.decision.order)
    events_before = canonical_json_bytes(checkpoint.events)

    settlement = simulation.apply_settlement(settlement_inputs)
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledVoidResult)
    assert settlement == result.settlement
    assert result.run_outcome is RunOutcome.PARTIAL_FILL_CANCELED_SETTLED_VOID
    assert result.logical_time == datetime(2026, 7, 15, 18, 0, tzinfo=UTC)

    assert result.proposal.side is Side.YES
    assert result.proposal.limit_price == Price("0.39", "0.01")
    assert result.allocation.quantity == Quantity("250", "1")
    assert result.allocation.maximum_loss == Money("100")
    assert tuple(item.state for item in result.reservation_lifecycle) == (
        ReservationState.HELD,
        ReservationState.OPEN,
        ReservationState.OPEN,
        ReservationState.RELEASED,
    )
    assert tuple(item.maximum_loss for item in result.reservation_lifecycle) == (
        Money("100"),
        Money("100"),
        Money("60"),
        Money(0),
    )
    assert result.released_at_cancellation == Money("60")

    assert canonical_json_bytes(result.order) == order_before
    assert canonical_json_bytes(result.execution_events) == events_before
    assert result.order is checkpoint.decision.order
    assert result.order.state is OrderState.CANCELED
    assert result.order.quantity == Quantity("250", "1")
    assert result.order.filled_quantity == Quantity("100", "1")
    assert result.order.remaining_quantity == Quantity("150", "1")
    assert result.order.reserved_maximum_loss == Money(0)
    assert result.order.last_execution_sequence == 3
    assert tuple(event.event_type for event in result.execution_events) == (
        ExecutionEventType.OPENED,
        ExecutionEventType.FILL,
        ExecutionEventType.CANCELED,
    )
    assert tuple(event.sequence for event in result.execution_events) == (1, 2, 3)
    assert (
        sum(event.event_type is ExecutionEventType.FILL for event in result.execution_events) == 1
    )
    fill = result.execution_events[1]
    assert fill.quantity == Quantity("100", "1")
    assert fill.price == Price("0.39", "0.01")
    assert fill.fee == Money("1")
    assert result.execution_events[0].occurred_at == datetime(2026, 7, 15, 15, 0, 3, tzinfo=UTC)
    assert fill.occurred_at == datetime(2026, 7, 15, 15, 0, 4, tzinfo=UTC)
    assert result.execution_events[2].occurred_at == datetime(2026, 7, 15, 15, 0, 5, tzinfo=UTC)

    assert result.pre_settlement_position.open_quantity == fill.quantity
    assert result.pre_settlement_position.cost_basis == Money("39")
    assert result.pre_settlement_position.maximum_remaining_loss == Money("40")
    assert result.pre_settlement_position.originating_order_id == result.order.order_id
    assert result.pre_settlement_position.originating_fill_event_id == fill.event_id
    assert result.active_risk_after_partial_fill == Money("100")
    assert result.active_risk_after_cancellation == Money("40")

    fact = result.settlement_fact
    assert result.contract_rule == result.information_set.rule
    assert fact.source_id == "settlement-partial-fill-canceled-void"
    assert fact.instrument_id == result.information_set.instrument.instrument_id
    assert fact.rule_version_id == result.contract_rule.rule_version_id
    assert fact.rule_content_hash == result.contract_rule.content_hash
    assert fact.resolution_source == result.contract_rule.resolution_source
    assert fact.outcome_classification is SettlementOutcome.VOID
    assert fact.settlement_value_text == "0.40"
    assert fact.settlement_value == Decimal("0.40")
    assert Decimal(0) <= fact.settlement_value <= Decimal(1)
    assert fact.source_event_at == datetime(2026, 7, 15, 17, 0, tzinfo=UTC)
    assert fact.available_at == datetime(2026, 7, 15, 17, 0, 3, tzinfo=UTC)
    assert fact.available_at > max(
        result.information_set.cutoff.value,
        result.execution_events[-1].occurred_at,
        result.order.expires_at,
    )

    assert result.settlement.outcome_classification is SettlementOutcome.VOID
    assert result.settlement.filled_quantity == fill.quantity
    assert result.settlement.filled_quantity != result.order.quantity
    assert result.settlement.settlement_cash == Money("40")
    assert result.settlement.realized_pnl == Decimal("1")
    assert result.settlement.reservation_release == Money(0)
    assert result.settlement.position_risk_release == Money("40")
    assert result.position_risk_released_at_settlement == Money("40")

    assert isinstance(result.settled_position, ClassifiedSettledFilledPositionState)
    assert result.settled_position.outcome_classification is SettlementOutcome.VOID
    assert result.settled_position.open_quantity == Quantity(0, "1")
    assert result.settled_position.settled_quantity == fill.quantity
    assert result.settled_position.cost_basis == Money(0)
    assert result.settled_position.maximum_remaining_loss == Money(0)
    assert result.settled_position.settlement_cash == Money("40")
    assert result.settled_position.realized_contract_pnl == Decimal("1")
    assert result.settled_position.originating_order_id == result.order.order_id
    assert result.settled_position.originating_fill_event_id == fill.event_id
    assert result.final_reservation.maximum_loss == Money(0)
    assert result.final_active_campaign_risk == Money(0)

    assert result.risk_capacity_before_order == Money("1000")
    assert result.risk_capacity_while_fully_reserved == Money("900")
    assert result.risk_capacity_after_partial_fill == Money("900")
    assert result.risk_capacity_after_cancellation == Money("960")
    assert result.risk_capacity_after_settlement == Money("1000")

    by_type = {journal.journal_type: journal for journal in result.journals}
    assert len(result.journals) == 4
    assert all(journal.balanced for journal in result.journals)
    assert by_type[JournalType.PURCHASE].source_id == fill.event_id
    assert by_type[JournalType.FEE].source_id == fill.event_id
    settlement_journal = by_type[JournalType.SETTLEMENT]
    assert settlement_journal.source_id == fact.source_id
    assert {
        (posting.account_code, posting.side, posting.amount.amount)
        for posting in settlement_journal.postings
    } == {
        (AccountCode.CASH, PostingSide.DEBIT, Decimal("40")),
        (AccountCode.POSITION_COST, PostingSide.CREDIT, Decimal("39")),
        (AccountCode.REALIZED_PNL, PostingSide.CREDIT, Decimal("1")),
    }
    assert result.pre_settlement_metrics.cash == Money("960")
    assert result.pre_settlement_metrics.position_cost == Money("39")
    assert result.pre_settlement_metrics.ledger_equity == Money("999")
    assert result.pre_settlement_metrics.fees == Money("1")
    assert result.metrics.contributions == Money("1000")
    assert result.metrics.cash == Money("1000")
    assert result.metrics.position_cost == Money(0)
    assert result.metrics.ledger_equity == Money("1000")
    assert result.metrics.realized_trading_pnl_before_fees == Decimal("1")
    assert result.metrics.fees == Money("1")
    assert result.metrics.net_trading_pnl == Decimal(0)


def test_partial_canceled_void_loads_only_after_cancellation_and_executes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_id = "partial-fill-canceled-settled-void"
    reader = FixtureReader()
    fixture = reader.load_decision(fixture_id)
    settlement = _settlement_inputs(reader.load_settlement(fixture_id))
    applied_events: list[ExecutionEventType] = []
    states: list[OrderState] = []
    calls = {"authorize": 0, "simulate": 0, "settlement": 0}
    real_apply = GoldenSimulation.apply_partial_execution
    real_authorize = AtomicOrderBook.authorize_and_reserve
    real_simulate = DeterministicSimulator.simulate

    def tracked_apply(self: GoldenSimulation, event: ExecutionEvent) -> object:
        checkpoint = real_apply(self, event)
        applied_events.append(event.event_type)
        states.append(checkpoint.decision.order.state)
        return checkpoint

    def tracked_authorize(self: AtomicOrderBook, **kwargs: object) -> object:
        calls["authorize"] += 1
        return real_authorize(self, **kwargs)  # type: ignore[arg-type]

    def tracked_simulate(
        self: DeterministicSimulator,
        *args: object,
        **kwargs: object,
    ) -> tuple[ExecutionEvent, ...]:
        calls["simulate"] += 1
        return real_simulate(self, *args, **kwargs)  # type: ignore[arg-type]

    def load_settlement() -> SettlementInputs:
        calls["settlement"] += 1
        assert applied_events == [
            ExecutionEventType.OPENED,
            ExecutionEventType.FILL,
            ExecutionEventType.CANCELED,
        ]
        assert states[-1] is OrderState.CANCELED
        return settlement

    monkeypatch.setattr(GoldenSimulation, "apply_partial_execution", tracked_apply)
    monkeypatch.setattr(AtomicOrderBook, "authorize_and_reserve", tracked_authorize)
    monkeypatch.setattr(DeterministicSimulator, "simulate", tracked_simulate)
    result = run_simulation(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash(fixture_id)),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
        settlement_loader=load_settlement,
        capital=Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )
    assert isinstance(result, PartialFillCanceledSettledVoidResult)
    assert calls == {"authorize": 1, "simulate": 1, "settlement": 1}
    assert len(result.execution_events) == 3


def test_partial_canceled_void_duplicate_is_idempotent_and_order_is_immutable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulation, reader = _partial_fill_canceled_settled_void_simulation()
    checkpoint = simulation.run_decision_and_execution()
    exact = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-void"))
    order_before = canonical_json_bytes(checkpoint.decision.order)
    events_before = canonical_json_bytes(checkpoint.events)

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("VOID settlement attempted another authorization or execution")

    monkeypatch.setattr(DeterministicSimulator, "simulate", forbidden)
    monkeypatch.setattr(AtomicOrderBook, "authorize_and_reserve", forbidden)
    monkeypatch.setattr(GoldenSimulation, "apply_partial_execution", forbidden)
    first = simulation.apply_settlement(exact)
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledVoidResult)
    report = _render(simulation)
    assert simulation.apply_settlement(exact) == first
    assert simulation.finalize() == result
    assert _render(simulation) == report
    assert canonical_json_bytes(result.order) == order_before
    assert canonical_json_bytes(result.execution_events) == events_before
    assert result.order is checkpoint.decision.order
    assert all(
        retained is original
        for retained, original in zip(result.execution_events, checkpoint.events, strict=True)
    )
    assert len(result.journals) == 4
    assert (
        sum(event.event_type is ExecutionEventType.FILL for event in result.execution_events) == 1
    )


@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("source", "source identity mismatch"),
        ("instrument", "instrument mismatch"),
        ("rule_version", "rule version mismatch"),
        ("rule_hash", "rule content hash mismatch"),
        ("resolution_source", "resolution source mismatch"),
        ("classification", "classification must be VOID"),
        ("value", "exact VOID rule value"),
        ("before_execution", "before execution completed"),
        ("before_determination", "precedes contract determination"),
    ),
)
def test_partial_canceled_void_mismatch_fails_before_mutation(
    case: str,
    message: str,
) -> None:
    simulation, reader = _partial_fill_canceled_settled_void_simulation()
    checkpoint = simulation.run_decision_and_execution()
    exact = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-void"))
    fact = exact.fact
    assert isinstance(fact, ClassifiedRuleBoundSettlementFact)
    if case == "source":
        changed = _rehashed_classified_fact(fact, source_id="settlement-conflicting-source")
    elif case == "instrument":
        changed = _rehashed_classified_fact(fact, instrument_id="instrument-conflicting")
    elif case == "rule_version":
        changed = _rehashed_classified_fact(fact, rule_version_id="rule-conflicting")
    elif case == "rule_hash":
        changed = _rehashed_classified_fact(
            fact,
            rule_content_hash=ContentHash("0" * 64),
        )
    elif case == "resolution_source":
        changed = _rehashed_classified_fact(fact, resolution_source="Conflicting source")
    elif case == "classification":
        changed = _rehashed_classified_fact(
            fact,
            outcome_classification=SettlementOutcome.NO,
        )
    elif case == "value":
        changed = _rehashed_classified_fact(
            fact,
            settlement_value=Decimal("0.39"),
            settlement_value_text="0.39",
        )
    elif case == "before_execution":
        changed = _rehashed_classified_fact(
            fact,
            source_event_at=datetime(2026, 7, 15, 15, 0, 5, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 15, 0, 6, tzinfo=UTC),
        )
    else:
        changed = _rehashed_classified_fact(
            fact,
            source_event_at=datetime(2026, 7, 15, 16, 59, tzinfo=UTC),
            available_at=datetime(2026, 7, 15, 16, 59, 1, tzinfo=UTC),
        )
    before = canonical_json_bytes(checkpoint)
    with pytest.raises(ValueValidationError, match=message):
        simulation.apply_settlement(replace(exact, fact=changed))
    assert canonical_json_bytes(simulation.run_decision_and_execution()) == before
    with pytest.raises(RuntimeError, match="before exact VOID settlement"):
        simulation.finalize()


def test_void_value_cannot_follow_a_mutated_instrument_without_matching_rule_terms() -> None:
    simulation, reader = _partial_fill_canceled_settled_void_simulation()
    simulation.run_decision_and_execution()
    exact = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-void"))
    fact = exact.fact
    assert isinstance(fact, ClassifiedRuleBoundSettlementFact)
    checkpoint = simulation.run_decision_and_execution()
    rule = checkpoint.decision.information_set.rule
    assert isinstance(rule, VoidContractRuleVersion)
    assert isinstance(checkpoint.position, FilledPositionState)
    assert isinstance(checkpoint.decision.order, ExpiringLimitOrder)
    instrument = replace(
        checkpoint.decision.information_set.instrument,
        void_settlement_value=Decimal("0.39"),
    )
    changed = _rehashed_classified_fact(
        fact,
        settlement_value=Decimal("0.39"),
        settlement_value_text="0.39",
    )
    before = canonical_json_bytes(checkpoint)
    with pytest.raises(ValueValidationError, match="exact VOID rule value"):
        settle_canceled_filled_void_position(
            checkpoint.position,
            checkpoint.decision.reservation,
            checkpoint.decision.order,
            instrument,
            rule,
            changed,
            execution_completed_at=checkpoint.events[-1].occurred_at,
        )
    assert canonical_json_bytes(simulation.run_decision_and_execution()) == before


def test_classified_void_fact_cannot_enter_the_legacy_golden_settlement_path() -> None:
    simulation, reader = _simulation()
    checkpoint = simulation.run_decision_and_execution()
    golden = _settlement_inputs(reader.load_settlement("golden"))
    golden_fact = golden.fact
    void_fact = _settlement_inputs(
        reader.load_settlement("partial-fill-canceled-settled-void")
    ).fact
    assert isinstance(void_fact, ClassifiedRuleBoundSettlementFact)
    rule = checkpoint.decision.information_set.rule
    classified = _rehashed_classified_fact(
        void_fact,
        fixture_id="golden",
        source_id=golden_fact.source_id,
        instrument_id=golden_fact.instrument_id,
        rule_version_id=golden_fact.rule_version_id,
        rule_content_hash=rule.content_hash,
        resolution_source=rule.resolution_source,
        outcome_classification=SettlementOutcome.VOID,
        source_event_at=golden_fact.source_event_at,
        available_at=golden_fact.available_at,
        settlement_value=golden_fact.settlement_value,
        settlement_value_text="1.00",
    )
    before = canonical_json_bytes(checkpoint)
    with pytest.raises(ValueValidationError, match="explicit fixture path"):
        simulation.apply_settlement(replace(golden, fact=classified))
    assert canonical_json_bytes(simulation.run_decision_and_execution()) == before


def test_plain_settlement_fact_cannot_bypass_a_void_contract_rule() -> None:
    simulation, reader = _partial_fill_canceled_settled_void_simulation()
    checkpoint = simulation.run_decision_and_execution()
    exact = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-void"))
    classified = exact.fact
    assert isinstance(classified, ClassifiedRuleBoundSettlementFact)
    assert isinstance(checkpoint.position, FilledPositionState)
    rule = checkpoint.decision.information_set.rule
    assert isinstance(rule, VoidContractRuleVersion)
    plain = SettlementFact(
        source_id=classified.source_id,
        instrument_id=classified.instrument_id,
        rule_version_id=classified.rule_version_id,
        source_event_at=classified.source_event_at,
        available_at=classified.available_at,
        settlement_value=classified.settlement_value,
        content_hash=classified.content_hash,
    )
    before = canonical_json_bytes(checkpoint)
    with pytest.raises(ValueValidationError, match="classified settlement path"):
        settle_position(
            checkpoint.position,
            checkpoint.decision.reservation,
            checkpoint.decision.information_set.instrument,
            rule,
            plain,
        )
    assert canonical_json_bytes(simulation.run_decision_and_execution()) == before


def test_partial_canceled_void_duplicate_conflicts_and_invalid_evidence_are_atomic() -> None:
    simulation, reader = _partial_fill_canceled_settled_void_simulation()
    checkpoint = simulation.run_decision_and_execution()
    exact = _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-void"))
    fact = exact.fact
    assert isinstance(fact, ClassifiedRuleBoundSettlementFact)
    first = simulation.apply_settlement(exact)
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledVoidResult)
    before = canonical_json_bytes(result)
    report = _render(simulation)
    assert simulation.apply_settlement(exact) == first
    conflicts = (
        replace(exact, settlement_file_hash=ContentHash("0" * 64)),
        replace(
            exact,
            fact=_rehashed_classified_fact(
                fact,
                outcome_classification=SettlementOutcome.NO,
            ),
        ),
        replace(
            exact,
            fact=_rehashed_classified_fact(
                fact,
                settlement_value=Decimal("0.41"),
                settlement_value_text="0.41",
            ),
        ),
        replace(
            exact,
            fact=_rehashed_classified_fact(
                fact,
                available_at=datetime(2026, 7, 15, 17, 0, 4, tzinfo=UTC),
            ),
        ),
        replace(
            exact,
            fact=_rehashed_classified_fact(fact, source_id="settlement-conflicting-source"),
        ),
    )
    for conflicting in conflicts:
        with pytest.raises(SimulationConflictError, match="conflicting content"):
            simulation.apply_settlement(conflicting)
        assert canonical_json_bytes(simulation.finalize()) == before
        assert _render(simulation) == report
        retained = simulation.finalize()
        assert retained.order == checkpoint.decision.order  # type: ignore[union-attr]
        assert retained.execution_events == checkpoint.events  # type: ignore[union-attr]
        assert len(retained.journals) == 4
    with pytest.raises(ValueValidationError, match="between zero and one"):
        replace(
            fact,
            settlement_value=Decimal("1.01"),
            settlement_value_text="1.01",
        )
    with pytest.raises(ValueValidationError, match="raw payload hash mismatch"):
        replace(fact, raw_payload_hash=ContentHash("0" * 64))
    with pytest.raises(ValueValidationError, match="evidence content hash mismatch"):
        replace(fact, content_hash=ContentHash("0" * 64))
    assert canonical_json_bytes(simulation.finalize()) == before


def test_partial_canceled_void_result_rejects_inconsistent_terminal_state() -> None:
    simulation, reader = _partial_fill_canceled_settled_void_simulation()
    simulation.run_decision_and_execution()
    simulation.apply_settlement(
        _settlement_inputs(reader.load_settlement("partial-fill-canceled-settled-void"))
    )
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledVoidResult)
    with pytest.raises(ValueError):
        replace(result, order=replace(result.order, state=OrderState.OPEN))
    with pytest.raises(ValueError):
        replace(result, execution_events=(*result.execution_events, result.execution_events[1]))
    with pytest.raises(ValueError, match="derecognize"):
        replace(
            result,
            settlement=replace(result.settlement, filled_quantity=result.order.quantity),
        )
    with pytest.raises(ValueError, match="derecognize"):
        replace(
            result,
            settlement=replace(
                result.settlement,
                settlement_id="settlement_forged",
            ),
            settled_position=replace(
                result.settled_position,
                settlement_id="settlement_forged",
            ),
        )
    with pytest.raises(ValueError, match="derecognize"):
        replace(
            result,
            settlement=replace(
                result.settlement,
                outcome_classification=SettlementOutcome.NO,
            ),
        )
    with pytest.raises(ValueError, match="preserve exact fill provenance"):
        replace(
            result,
            settled_position=replace(
                result.settled_position,
                outcome_classification=SettlementOutcome.NO,
            ),
        )
    with pytest.raises(ValueError, match="four balanced journals"):
        replace(
            result,
            journals=tuple(
                journal
                for journal in result.journals
                if journal.journal_type is not JournalType.SETTLEMENT
            ),
        )
    with pytest.raises(ValueError, match="derived from retained journals"):
        replace(result, metrics=replace(result.metrics, fees=Money("2")))
    with pytest.raises(ValueError, match="risk capacity"):
        replace(result, risk_capacity_after_settlement=Money("999"))


def test_partial_canceled_settled_void_report_is_explicit_canonical_and_hash_pinned() -> None:
    fixture_id = "partial-fill-canceled-settled-void"
    first = demo_core(fixture_id, Money("1000.00"), 20260716)
    second = demo_core(fixture_id, Money("1000.00"), 20260716)
    assert first == second
    report = json.loads(first)
    assert report["schema_version"] == "2"
    assert report["run_outcome"] == "PARTIAL_FILL_CANCELED_SETTLED_VOID"
    assert report["runtime_capability"] == "SIMULATION_ONLY"
    assert report["execution_evidence"] == "SYNTHETIC"
    assert report["edge_evidence"] == "NONE"
    assert report["calibration_evidence"] == "NONE"
    assert report["outcome_classification"] == "VOID"
    assert report["settlement_fact"]["outcome_classification"] == "VOID"
    assert report["contract_rule_version"]["void_settlement_term"] == {
        "content_hash": {
            "value": "2d00611ca057d33025a8875b307955f95fa0501b25b768bc072ad300dfbb81d4"
        },
        "outcome_classification": "VOID",
        "settlement_value": "0.4",
        "settlement_value_text": "0.40",
    }
    assert report["settlement_fact"]["settlement_value_text"] == "0.40"
    assert report["settlement_value"] == "0.4"
    assert report["settlement_cash"]["amount"] == "40"
    assert report["realized_contract_pnl"] == "1"
    assert report["fees"] == "1"
    assert report["net_trading_pnl"] == "0"
    assert report["final_cash"]["amount"] == "1000"
    assert report["final_ledger_equity"] == "1000"
    assert report["final_position_risk"]["amount"] == "0"
    assert report["total_active_campaign_risk"]["amount"] == "0"
    order = report["order"]
    assert order["state"] == "CANCELED"
    assert order["quantity"]["value"] == "250"
    assert order["filled_quantity"]["value"] == "100"
    assert report["canceled_quantity"]["value"] == "150"
    assert order["last_execution_sequence"] == 3
    assert [event["event_type"] for event in report["execution_events"]] == [
        "OPENED",
        "FILL",
        "CANCELED",
    ]
    assert sum(event["event_type"] == "FILL" for event in report["execution_events"]) == 1
    assert report["settlement_result"]["filled_quantity"]["value"] == "100"
    assert report["settled_position"]["settled_quantity"]["value"] == "100"
    assert report["settled_position"]["open_quantity"]["value"] == "0"
    settlement_journal = next(
        journal for journal in report["ledger_journals"] if journal["journal_type"] == "SETTLEMENT"
    )
    assert {
        (posting["account_code"], posting["side"], posting["amount"]["amount"])
        for posting in settlement_journal["postings"]
    } == {
        ("CASH", "DEBIT", "40"),
        ("POSITION_COST", "CREDIT", "39"),
        ("REALIZED_PNL", "CREDIT", "1"),
    }

    def all_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(all_keys(member) for member in value.values()))
        if isinstance(value, list):
            return set().union(*(all_keys(member) for member in value))
        return set()

    forbidden = {
        "orders",
        "replacement_order",
        "closing_order",
        "second_order",
        "closing_trade",
        "synthetic_sale",
        "full_fill_event",
        "second_fill",
        "refund_transaction",
        "fills",
    }
    assert forbidden.isdisjoint(all_keys(report))
    assert sha256(first).hexdigest() == EXPECTED_PARTIAL_FILL_CANCELED_SETTLED_VOID_REPORT_SHA256


@pytest.mark.parametrize(
    ("fixture_id", "expected_hash"),
    (
        ("golden", EXPECTED_REPORT_SHA256),
        ("no-proposal", EXPECTED_NO_PROPOSAL_REPORT_SHA256),
        ("policy-rejected", EXPECTED_POLICY_REJECTED_REPORT_SHA256),
        ("invariant-rejected", EXPECTED_INVARIANT_REJECTED_REPORT_SHA256),
        ("unfilled-expired", EXPECTED_UNFILLED_EXPIRED_REPORT_SHA256),
        ("partial-fill-canceled", EXPECTED_PARTIAL_FILL_CANCELED_REPORT_SHA256),
        (
            "partial-fill-canceled-settled-no",
            EXPECTED_PARTIAL_FILL_CANCELED_SETTLED_NO_REPORT_SHA256,
        ),
    ),
)
def test_void_extension_preserves_every_legacy_report_hash(
    fixture_id: str,
    expected_hash: str,
) -> None:
    assert sha256(demo_core(fixture_id, Money("1000.00"), 20260716)).hexdigest() == expected_hash
