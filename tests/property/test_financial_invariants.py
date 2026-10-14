from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from apex import ENGINE_VERSION
from apex.adapters.fixtures import FixtureReader
from apex.adapters.simulator import DeterministicSimulator
from apex.application.interfaces import (
    DecisionInputs,
    ExecutionScript,
    InvariantRejectedResult,
    PartialFillCanceledExecutionScript,
    PartialFillCanceledResult,
    PartialFillCanceledSettledNoResult,
    PartialFillCanceledSettledVoidResult,
    RunMetadata,
    SettlementInputs,
    SyntheticExecutionScript,
    UnfilledExpiredResult,
)
from apex.application.run_simulation import (
    GoldenSimulation,
    PartialExecutionCheckpoint,
    SimulationConflictError,
    run_simulation,
)
from apex.cli import _decision_inputs, _execution_script, _settlement_inputs
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
    AllocationDecision,
    CapitalAllocator,
    OpportunityProposal,
    ReferenceEdgeStrategy,
    RiskKernel,
    Side,
    StaleMarketRiskDecision,
)
from apex.domain.execution import (
    ClassifiedRuleBoundSettlementFact,
    ExecutionEventType,
    ExpiringLimitOrder,
    FilledPositionState,
    OrderState,
    ReservationState,
    SettlementOutcome,
    SettlementState,
    apply_partial_fill_canceled_event,
    apply_unfilled_order_event,
    create_reserved_order,
    settle_canceled_filled_position,
    settle_canceled_filled_void_position,
)
from apex.domain.ledger import AccountCode, JournalFactory, JournalType, Ledger, PostingSide
from apex.domain.market import (
    InformationSet,
    InformationSetBuilder,
    ReferenceEstimate,
    VoidContractRuleVersion,
    VoidSettlementRuleTerm,
    void_settlement_term_content_hash,
)
from apex.domain.values import (
    ContentHash,
    Money,
    Price,
    ProbabilityVector,
    Quantity,
    RiskFraction,
    ValueValidationError,
    canonical_decimal,
    canonical_json_bytes,
    content_digest,
    floor_to_increment,
)

NOW = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
HASH = ContentHash("0" * 64)


def _policy() -> CampaignPolicy:
    return CampaignPolicy(
        policy_version_id="property-policy-v1",
        profile="MOONSHOT",
        requested_risk_fraction="0.10",
        max_trade_loss_fraction="0.10",
        max_open_loss_fraction="1",
        min_synthetic_edge="0",
        max_spread="1",
        max_capital_lockup_seconds=10800,
        content_hash=HASH,
    )


def _proposal() -> OpportunityProposal:
    return OpportunityProposal(
        proposal_id="proposal-property",
        instrument_id="instrument-property",
        rule_version_id="rule-property",
        information_set_id="information-property",
        strategy_version="strategy-property",
        side=Side.YES,
        executable_ask=Price("0.40", "0.01"),
        fee_model_version="fee-property",
        fee_per_unit=Money("0.01"),
        expected_net_value_per_unit=Decimal("0.31"),
        maximum_loss_per_unit=Money("0.41"),
        requested_risk_fraction=RiskFraction("0.10"),
        expires_at=datetime(2026, 7, 15, 15, 5, tzinfo=UTC),
        fingerprint="1" * 64,
    )


@dataclass(frozen=True, slots=True)
class _FreshnessArtifacts:
    proposal: OpportunityProposal
    allocation: AllocationDecision
    snapshot: CampaignSnapshot
    information_set: InformationSet
    policy: CampaignPolicy
    market_observation_source_id: str
    market_observation_available_at: datetime


@dataclass(frozen=True, slots=True)
class _InvariantApplicationInputs:
    decision_inputs: DecisionInputs
    execution_script: SyntheticExecutionScript
    run_metadata: RunMetadata


@dataclass(frozen=True, slots=True)
class _UnfilledApplicationInputs:
    decision_inputs: DecisionInputs
    execution_script: ExecutionScript
    run_metadata: RunMetadata


@dataclass(frozen=True, slots=True)
class _PartialFillApplicationInputs:
    decision_inputs: DecisionInputs
    execution_script: PartialFillCanceledExecutionScript
    run_metadata: RunMetadata


@dataclass(frozen=True, slots=True)
class _SettledNoApplicationInputs:
    decision_inputs: DecisionInputs
    execution_script: PartialFillCanceledExecutionScript
    settlement_inputs: SettlementInputs
    run_metadata: RunMetadata


@dataclass(frozen=True, slots=True)
class _SettledVoidApplicationInputs:
    decision_inputs: DecisionInputs
    execution_script: PartialFillCanceledExecutionScript
    settlement_inputs: SettlementInputs
    run_metadata: RunMetadata


class _MustNotSimulate:
    def simulate(self, *_: object) -> tuple[()]:
        raise AssertionError("invariant-rejected property path must not execute")


def _must_not_load_settlement() -> SettlementInputs:
    raise AssertionError("invariant-rejected property path must not load settlement")


@lru_cache(maxsize=1)
def _freshness_artifacts() -> _FreshnessArtifacts:
    fixture = FixtureReader().load_decision("invariant-rejected")
    inputs = _decision_inputs(fixture)
    capital = Money("1000")
    campaign = Campaign(
        campaign_id=inputs.campaign_id,
        contribution=capital,
        authorized_loss=capital,
        policy=inputs.policy,
    )
    snapshot = CampaignSnapshot(
        campaign=campaign,
        ledger_equity=capital,
        settled_cash=capital,
        active_reservations=Money(0),
    )
    information_set = InformationSetBuilder.build(
        inputs.source_records,
        inputs.instrument,
        inputs.cutoff,
        inputs.rule,
        inputs.market,
    )
    estimate_input = inputs.reference_estimate
    estimate = ReferenceEstimate(
        estimate_id=estimate_input.estimate_id,
        source_id=estimate_input.source_id,
        probability_vector=estimate_input.probability_vector,
        published_at=estimate_input.published_at,
        expires_at=estimate_input.expires_at,
        method_version=estimate_input.method_version,
        information_set_id=information_set.information_set_id,
    )
    evaluation = ReferenceEdgeStrategy.evaluate(
        information_set,
        estimate,
        inputs.policy,
        strategy_version=inputs.strategy_version,
        fee_model_version=inputs.fee_model_version,
        fee_per_unit=Money(fixture.execution.fee_per_contract),
        decision_at=fixture.execution.opened_at,
    )
    proposal = evaluation.proposal
    assert proposal is not None
    allocation = CapitalAllocator.allocate(
        proposal,
        snapshot,
        information_set.market.visible_yes_quantity,
        inputs.policy,
        decision_at=fixture.execution.opened_at,
        expected_settlement_at=information_set.rule.expected_settlement_at,
    )
    assert allocation.accepted
    market_members = tuple(
        member
        for member in information_set.ordered_members
        if member.source_id == inputs.market_observation_source_id
    )
    assert len(market_members) == 1
    return _FreshnessArtifacts(
        proposal=proposal,
        allocation=allocation,
        snapshot=snapshot,
        information_set=information_set,
        policy=inputs.policy,
        market_observation_source_id=inputs.market_observation_source_id,
        market_observation_available_at=market_members[0].available_at,
    )


@lru_cache(maxsize=1)
def _invariant_application_inputs() -> _InvariantApplicationInputs:
    reader = FixtureReader()
    fixture = reader.load_decision("invariant-rejected")
    return _InvariantApplicationInputs(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("invariant-rejected")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
    )


@lru_cache(maxsize=1)
def _unfilled_application_inputs() -> _UnfilledApplicationInputs:
    reader = FixtureReader()
    fixture = reader.load_decision("unfilled-expired")
    return _UnfilledApplicationInputs(
        decision_inputs=_decision_inputs(fixture),
        execution_script=_execution_script(fixture),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("unfilled-expired")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
    )


@lru_cache(maxsize=1)
def _partial_fill_application_inputs() -> _PartialFillApplicationInputs:
    reader = FixtureReader()
    fixture = reader.load_decision("partial-fill-canceled")
    execution_script = _execution_script(fixture)
    assert isinstance(execution_script, PartialFillCanceledExecutionScript)
    return _PartialFillApplicationInputs(
        decision_inputs=_decision_inputs(fixture),
        execution_script=execution_script,
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash("partial-fill-canceled")),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
    )


@lru_cache(maxsize=1)
def _settled_no_application_inputs() -> _SettledNoApplicationInputs:
    fixture_id = "partial-fill-canceled-settled-no"
    reader = FixtureReader()
    fixture = reader.load_decision(fixture_id)
    execution_script = _execution_script(fixture)
    assert isinstance(execution_script, PartialFillCanceledExecutionScript)
    return _SettledNoApplicationInputs(
        decision_inputs=_decision_inputs(fixture),
        execution_script=execution_script,
        settlement_inputs=_settlement_inputs(reader.load_settlement(fixture_id)),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash(fixture_id)),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
    )


@lru_cache(maxsize=1)
def _settled_void_application_inputs() -> _SettledVoidApplicationInputs:
    fixture_id = "partial-fill-canceled-settled-void"
    reader = FixtureReader()
    fixture = reader.load_decision(fixture_id)
    execution_script = _execution_script(fixture)
    assert isinstance(execution_script, PartialFillCanceledExecutionScript)
    return _SettledVoidApplicationInputs(
        decision_inputs=_decision_inputs(fixture),
        execution_script=execution_script,
        settlement_inputs=_settlement_inputs(reader.load_settlement(fixture_id)),
        run_metadata=RunMetadata(
            fixture_manifest_hash=ContentHash(reader.manifest_hash(fixture_id)),
            decision_file_hash=ContentHash(fixture.file_hash),
            logical_time=fixture.logical_time,
        ),
    )


def _classified_void_fact(
    baseline: ClassifiedRuleBoundSettlementFact,
    *,
    settlement_value: Decimal | None = None,
    outcome_classification: SettlementOutcome = SettlementOutcome.VOID,
    source_id: str | None = None,
    instrument_id: str | None = None,
    rule_version_id: str | None = None,
    rule_content_hash: ContentHash | None = None,
    resolution_source: str | None = None,
    source_event_at: datetime | None = None,
    available_at: datetime | None = None,
) -> ClassifiedRuleBoundSettlementFact:
    value = baseline.settlement_value if settlement_value is None else settlement_value
    value_text = canonical_decimal(value)
    resolved_source_id = source_id or baseline.source_id
    resolved_instrument_id = instrument_id or baseline.instrument_id
    resolved_rule_version_id = rule_version_id or baseline.rule_version_id
    resolved_rule_content_hash = rule_content_hash or baseline.rule_content_hash
    resolved_resolution_source = resolution_source or baseline.resolution_source
    resolved_source_event_at = source_event_at or baseline.source_event_at
    resolved_available_at = available_at or baseline.available_at
    raw_payload = (
        f"resolution:{outcome_classification.value};"
        f"settlement_value={value_text};"
        f"instrument={resolved_instrument_id};"
        f"rule={resolved_rule_version_id};"
        f"rule_hash={resolved_rule_content_hash.value};"
        f"source={resolved_resolution_source}"
    )
    raw_payload_hash = ContentHash(sha256(raw_payload.encode("utf-8")).hexdigest())
    evidence = {
        "fixture_id": baseline.fixture_id,
        "schema_version": baseline.schema_version,
        "source_id": resolved_source_id,
        "instrument_id": resolved_instrument_id,
        "rule_version_id": resolved_rule_version_id,
        "rule_content_hash": resolved_rule_content_hash.value,
        "resolution_source": resolved_resolution_source,
        "outcome_classification": outcome_classification,
        "source_event_at": resolved_source_event_at,
        "available_at": resolved_available_at,
        "settlement_value": value_text,
        "raw_payload": raw_payload,
        "raw_payload_hash": raw_payload_hash.value,
    }
    return ClassifiedRuleBoundSettlementFact(
        source_id=resolved_source_id,
        instrument_id=resolved_instrument_id,
        rule_version_id=resolved_rule_version_id,
        source_event_at=resolved_source_event_at,
        available_at=resolved_available_at,
        settlement_value=value,
        content_hash=ContentHash(content_digest(evidence)),
        fixture_id=baseline.fixture_id,
        schema_version=baseline.schema_version,
        settlement_value_text=value_text,
        resolution_source=resolved_resolution_source,
        rule_content_hash=resolved_rule_content_hash,
        outcome_classification=outcome_classification,
        raw_payload=raw_payload,
        raw_payload_hash=raw_payload_hash,
    )


def _void_rule_for_value(
    baseline: VoidContractRuleVersion,
    settlement_value: Decimal,
) -> VoidContractRuleVersion:
    value_text = canonical_decimal(settlement_value)
    prior_fragment = f"{baseline.void_settlement_term.settlement_value_text} if it is VOID"
    replacement_fragment = f"{value_text} if it is VOID"
    exact_text = baseline.exact_text.replace(prior_fragment, replacement_fragment)
    assert exact_text.count(replacement_fragment) == 1
    rule_hash = ContentHash(sha256(exact_text.encode("utf-8")).hexdigest())
    term = VoidSettlementRuleTerm(
        outcome_classification=SettlementOutcome.VOID,
        settlement_value=settlement_value,
        settlement_value_text=value_text,
        content_hash=void_settlement_term_content_hash(
            rule_hash,
            SettlementOutcome.VOID,
            value_text,
        ),
    )
    return replace(
        baseline,
        exact_text=exact_text,
        content_hash=rule_hash,
        void_settlement_term=term,
    )


def _unfilled_simulation(capital: Money | None = None) -> GoldenSimulation:
    inputs = _unfilled_application_inputs()
    return GoldenSimulation(
        decision_inputs=inputs.decision_inputs,
        execution_script=inputs.execution_script,
        run_metadata=inputs.run_metadata,
        capital=capital or Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )


def _partial_fill_simulation(
    *,
    capital: Money | None = None,
    execution_script: PartialFillCanceledExecutionScript | None = None,
) -> GoldenSimulation:
    inputs = _partial_fill_application_inputs()
    return GoldenSimulation(
        decision_inputs=inputs.decision_inputs,
        execution_script=execution_script or inputs.execution_script,
        run_metadata=inputs.run_metadata,
        capital=capital or Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )


def _settled_no_simulation(
    *,
    capital: Money | None = None,
    execution_script: PartialFillCanceledExecutionScript | None = None,
) -> GoldenSimulation:
    inputs = _settled_no_application_inputs()
    return GoldenSimulation(
        decision_inputs=inputs.decision_inputs,
        execution_script=execution_script or inputs.execution_script,
        run_metadata=inputs.run_metadata,
        capital=capital or Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )


def _settled_void_simulation(
    *,
    capital: Money | None = None,
    execution_script: PartialFillCanceledExecutionScript | None = None,
) -> GoldenSimulation:
    inputs = _settled_void_application_inputs()
    return GoldenSimulation(
        decision_inputs=inputs.decision_inputs,
        execution_script=execution_script or inputs.execution_script,
        run_metadata=inputs.run_metadata,
        capital=capital or Money("1000"),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=DeterministicSimulator(),
    )


@lru_cache(maxsize=1)
def _partial_fill_result() -> PartialFillCanceledResult:
    result = _partial_fill_simulation().run_decision_and_execution()
    assert isinstance(result, PartialFillCanceledResult)
    return result


@lru_cache(maxsize=1)
def _settled_no_result() -> PartialFillCanceledSettledNoResult:
    inputs = _settled_no_application_inputs()
    simulation = _settled_no_simulation()
    checkpoint = simulation.run_decision_and_execution()
    assert isinstance(checkpoint, PartialExecutionCheckpoint)
    simulation.apply_settlement(inputs.settlement_inputs)
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledNoResult)
    return result


@lru_cache(maxsize=1)
def _settled_void_result() -> PartialFillCanceledSettledVoidResult:
    inputs = _settled_void_application_inputs()
    simulation = _settled_void_simulation()
    checkpoint = simulation.run_decision_and_execution()
    assert isinstance(checkpoint, PartialExecutionCheckpoint)
    simulation.apply_settlement(inputs.settlement_inputs)
    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledVoidResult)
    return result


@given(
    value=st.integers(min_value=0, max_value=1_000_000),
    increment=st.integers(min_value=1, max_value=1000),
)
def test_quantity_flooring_never_rounds_up(value: int, increment: int) -> None:
    candidate = Decimal(value) / Decimal("100")
    step = Decimal(increment) / Decimal("100")
    floored = floor_to_increment(candidate, step)
    assert floored <= candidate
    assert floored % step == 0
    assert candidate - floored < step


@given(
    yes=st.integers(min_value=0, max_value=100),
    no=st.integers(min_value=0, max_value=100),
)
def test_generated_probability_vectors_remain_exactly_bounded(yes: int, no: int) -> None:
    if yes + no > 100:
        return
    vector = ProbabilityVector(
        Decimal(yes) / Decimal(100),
        Decimal(no) / Decimal(100),
        Decimal(100 - yes - no) / Decimal(100),
    )
    assert vector.yes.value + vector.no.value + vector.void.value == Decimal(1)


@given(amount=st.integers(min_value=1, max_value=1_000_000))
def test_funding_amount_never_becomes_trading_profit(amount: int) -> None:
    ledger = Ledger()
    ledger.post(JournalFactory.contribution("property-funding", Money(amount), NOW))
    metrics = ledger.metrics()
    assert metrics.contributions.amount == Decimal(amount)
    assert metrics.ledger_equity.amount == Decimal(amount)
    assert metrics.net_trading_pnl == Decimal(0)


@given(
    cost=st.integers(min_value=1, max_value=100_000),
    proceeds=st.integers(min_value=0, max_value=200_000),
)
def test_generated_settlement_journals_balance(cost: int, proceeds: int) -> None:
    journal = JournalFactory.settlement(
        "property-settlement",
        Money(proceeds),
        Money(cost),
        NOW,
    )
    assert journal.balanced


@given(capital=st.integers(min_value=10, max_value=10_000))
def test_allocation_never_exceeds_authorized_loss_or_visible_quantity(capital: int) -> None:
    amount = Money(capital)
    policy = _policy()
    campaign = Campaign("property-campaign", amount, amount, policy)
    snapshot = CampaignSnapshot(campaign, amount, amount, Money(0))
    allocation = CapitalAllocator.allocate(
        _proposal(),
        snapshot,
        Quantity("1000", "1"),
        policy,
        decision_at=NOW,
        expected_settlement_at=NOW,
    )
    assert allocation.maximum_loss.amount <= amount.amount * Decimal("0.10")
    assert allocation.maximum_loss.amount <= campaign.authorized_loss.amount
    assert allocation.quantity.value <= Decimal("1000")
    assert allocation.quantity.value % allocation.quantity.increment == 0


@given(
    smaller=st.integers(min_value=10, max_value=2000),
    extra=st.integers(min_value=0, max_value=2000),
)
def test_capital_scaling_is_monotone_until_a_named_constraint_binds(
    smaller: int,
    extra: int,
) -> None:
    policy = _policy()

    def allocated(capital: int) -> Quantity:
        amount = Money(capital)
        campaign = Campaign("property-campaign", amount, amount, policy)
        snapshot = CampaignSnapshot(campaign, amount, amount, Money(0))
        return CapitalAllocator.allocate(
            _proposal(),
            snapshot,
            Quantity("1000", "1"),
            policy,
            decision_at=NOW,
            expected_settlement_at=NOW,
        ).quantity

    assert allocated(smaller + extra).value >= allocated(smaller).value


@given(
    coefficient=st.integers(min_value=0, max_value=999_999),
    scale=st.integers(min_value=0, max_value=6),
)
def test_canonical_decimal_round_trip_does_not_create_value(
    coefficient: int,
    scale: int,
) -> None:
    value = Decimal(coefficient).scaleb(-scale)
    assert Decimal(canonical_decimal(value)) == value


@given(age_seconds=st.integers(min_value=120, max_value=300))
def test_market_data_within_operational_freshness_window_passes(
    age_seconds: int,
) -> None:
    artifacts = _freshness_artifacts()
    before = canonical_json_bytes(
        {
            "proposal": artifacts.proposal,
            "allocation": artifacts.allocation,
            "snapshot": artifacts.snapshot,
            "information_set": artifacts.information_set,
        }
    )
    authorization_at = artifacts.market_observation_available_at + timedelta(seconds=age_seconds)

    decision = RiskKernel.evaluate(
        artifacts.proposal,
        artifacts.allocation,
        artifacts.snapshot,
        artifacts.information_set,
        artifacts.policy,
        OperationalInvariants(),
        authorization_at=authorization_at,
        market_observation_source_id=artifacts.market_observation_source_id,
    )

    assert decision.accepted
    assert decision.reason_codes == ("ALL_INVARIANTS_PASSED",)
    assert not isinstance(decision, StaleMarketRiskDecision)
    assert decision.allocation_id == artifacts.allocation.allocation_id
    assert before == canonical_json_bytes(
        {
            "proposal": artifacts.proposal,
            "allocation": artifacts.allocation,
            "snapshot": artifacts.snapshot,
            "information_set": artifacts.information_set,
        }
    )
    assert artifacts.allocation.quantity.value == Decimal("243")
    assert artifacts.allocation.maximum_loss.amount == Decimal("99.63")
    assert (
        artifacts.allocation.quantity.value * artifacts.proposal.maximum_loss_per_unit.amount
        == artifacts.allocation.maximum_loss.amount
    )
    assert canonical_decimal(artifacts.allocation.quantity.value) == "243"
    assert canonical_decimal(artifacts.allocation.maximum_loss.amount) == "99.63"


@given(age_seconds=st.integers(min_value=301, max_value=418))
def test_market_data_beyond_operational_freshness_window_rejects_only_as_stale(
    age_seconds: int,
) -> None:
    artifacts = _freshness_artifacts()
    before = canonical_json_bytes(
        {
            "proposal": artifacts.proposal,
            "allocation": artifacts.allocation,
            "snapshot": artifacts.snapshot,
            "information_set": artifacts.information_set,
        }
    )
    authorization_at = artifacts.market_observation_available_at + timedelta(seconds=age_seconds)
    newest_member = max(
        artifacts.information_set.ordered_members,
        key=lambda member: member.available_at,
    )
    if age_seconds <= 360:
        assert authorization_at - newest_member.available_at <= timedelta(
            seconds=OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
        )

    decision = RiskKernel.evaluate(
        artifacts.proposal,
        artifacts.allocation,
        artifacts.snapshot,
        artifacts.information_set,
        artifacts.policy,
        OperationalInvariants(),
        authorization_at=authorization_at,
        market_observation_source_id=artifacts.market_observation_source_id,
    )

    assert isinstance(decision, StaleMarketRiskDecision)
    assert not decision.accepted
    assert decision.reason_codes == (INVARIANT_MARKET_DATA_STALE,)
    assert decision.observation_source_id == artifacts.market_observation_source_id
    assert decision.observation_available_at == artifacts.market_observation_available_at
    assert decision.authorization_at == authorization_at
    assert decision.observed_age_seconds == age_seconds
    assert decision.maximum_age_seconds == OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
    assert decision.information_set_id == artifacts.information_set.information_set_id
    assert decision.information_set_hash == artifacts.information_set.content_hash
    assert before == canonical_json_bytes(
        {
            "proposal": artifacts.proposal,
            "allocation": artifacts.allocation,
            "snapshot": artifacts.snapshot,
            "information_set": artifacts.information_set,
        }
    )
    assert artifacts.allocation.quantity.value == Decimal("243")
    assert artifacts.allocation.maximum_loss.amount == Decimal("99.63")
    assert (
        artifacts.allocation.quantity.value * artifacts.proposal.maximum_loss_per_unit.amount
        == artifacts.allocation.maximum_loss.amount
    )


@given(
    profile=st.sampled_from(("CONSERVATIVE", "AGGRESSIVE", "MOONSHOT", "CUSTOM")),
    spread_hundredths=st.integers(min_value=2, max_value=100),
    lockup_seconds=st.integers(min_value=7499, max_value=20_000),
)
def test_campaign_policy_changes_cannot_suppress_operational_staleness(
    profile: str,
    spread_hundredths: int,
    lockup_seconds: int,
) -> None:
    artifacts = _freshness_artifacts()
    policy_identity = spread_hundredths * 100_000 + lockup_seconds
    changed_policy = CampaignPolicy(
        policy_version_id=(f"property-{profile.lower()}-{spread_hundredths}-{lockup_seconds}"),
        profile=profile,
        requested_risk_fraction="0.10",
        max_trade_loss_fraction="0.10",
        max_open_loss_fraction="1",
        min_synthetic_edge="0.01",
        max_spread=Decimal(spread_hundredths) / Decimal(100),
        max_capital_lockup_seconds=lockup_seconds,
        content_hash=ContentHash(f"{policy_identity:064x}"),
    )
    assert not hasattr(changed_policy, "max_data_age_seconds")
    changed_campaign = Campaign(
        campaign_id=artifacts.snapshot.campaign.campaign_id,
        contribution=artifacts.snapshot.campaign.contribution,
        authorized_loss=artifacts.snapshot.campaign.authorized_loss,
        policy=changed_policy,
    )
    changed_snapshot = CampaignSnapshot(
        campaign=changed_campaign,
        ledger_equity=artifacts.snapshot.ledger_equity,
        settled_cash=artifacts.snapshot.settled_cash,
        active_reservations=artifacts.snapshot.active_reservations,
    )
    changed_allocation = CapitalAllocator.allocate(
        artifacts.proposal,
        changed_snapshot,
        artifacts.information_set.market.visible_yes_quantity,
        changed_policy,
        decision_at=artifacts.market_observation_available_at + timedelta(seconds=120),
        expected_settlement_at=artifacts.information_set.rule.expected_settlement_at,
    )
    assert changed_allocation.accepted
    assert changed_allocation.quantity == artifacts.allocation.quantity
    assert changed_allocation.maximum_loss == artifacts.allocation.maximum_loss
    authorization_at = artifacts.market_observation_available_at + timedelta(seconds=301)

    decision = RiskKernel.evaluate(
        artifacts.proposal,
        changed_allocation,
        changed_snapshot,
        artifacts.information_set,
        changed_policy,
        OperationalInvariants(),
        authorization_at=authorization_at,
        market_observation_source_id=artifacts.market_observation_source_id,
    )

    assert isinstance(decision, StaleMarketRiskDecision)
    assert not decision.accepted
    assert decision.reason_codes == (INVARIANT_MARKET_DATA_STALE,)
    assert decision.observed_age_seconds == 301
    assert decision.maximum_age_seconds == OPERATIONAL_MAXIMUM_MARKET_DATA_AGE_SECONDS
    assert changed_allocation.quantity.value == Decimal("243")
    assert changed_allocation.maximum_loss.amount == Decimal("99.63")
    assert canonical_decimal(changed_allocation.quantity.value) == "243"
    assert canonical_decimal(changed_allocation.maximum_loss.amount) == "99.63"


@settings(max_examples=25, deadline=None)
@given(capital=st.integers(min_value=5, max_value=100_000))
def test_invariant_rejection_never_moves_funded_cash_or_creates_trade_state(
    capital: int,
) -> None:
    inputs = _invariant_application_inputs()
    result = run_simulation(
        decision_inputs=inputs.decision_inputs,
        execution_script=inputs.execution_script,
        run_metadata=inputs.run_metadata,
        settlement_loader=_must_not_load_settlement,
        capital=Money(capital),
        seed=20260716,
        engine_version=ENGINE_VERSION,
        simulator=_MustNotSimulate(),
    )

    assert isinstance(result, InvariantRejectedResult)
    assert result.risk_decision.reason_codes == (INVARIANT_MARKET_DATA_STALE,)
    assert len(result.journals) == 1
    assert result.journals[0].balanced
    assert result.metrics.contributions.amount == Decimal(capital)
    assert result.metrics.cash.amount == Decimal(capital)
    assert result.metrics.ledger_equity.amount == Decimal(capital)
    assert result.metrics.position_cost.amount == Decimal(0)
    assert result.metrics.realized_trading_pnl_before_fees == Decimal(0)
    assert result.metrics.fees.amount == Decimal(0)
    assert result.metrics.net_trading_pnl == Decimal(0)
    for absent in ("reservation", "order", "execution_events", "position", "settlement"):
        assert not hasattr(result, absent)


@settings(max_examples=25, deadline=None)
@given(authorized_quantity=st.integers(min_value=1, max_value=1000))
def test_unfilled_expiration_releases_exact_reservation_and_restores_capacity(
    authorized_quantity: int,
) -> None:
    quantity = Decimal(authorized_quantity)
    capital = Money(quantity * Decimal(4))
    simulation = _unfilled_simulation(capital)
    result = simulation.run_decision_and_execution()

    assert isinstance(result, UnfilledExpiredResult)
    assert result.allocation.quantity.value == quantity
    assert result.allocation.quantity.value % result.allocation.quantity.increment == 0
    expected_reservation = Money(quantity * Decimal("0.40"))
    initial, opened, released = result.reservation_lifecycle
    assert initial.state is ReservationState.HELD
    assert opened.state is ReservationState.OPEN
    assert released.state is ReservationState.RELEASED
    assert initial.maximum_loss == expected_reservation
    assert opened.maximum_loss == initial.maximum_loss
    assert released.maximum_loss == Money(0)
    assert result.released_reservation_amount == initial.maximum_loss
    assert result.released_reservation_amount.amount <= opened.maximum_loss.amount

    assert result.risk_capacity_before_order == capital
    assert result.risk_capacity_while_open.amount == (
        result.risk_capacity_before_order.amount - initial.maximum_loss.amount
    )
    assert result.risk_capacity_after_expiration == result.risk_capacity_before_order
    assert result.order.state is OrderState.EXPIRED
    assert result.order.quantity.value == quantity
    assert result.order.filled_quantity.value == Decimal(0)
    assert result.order.remaining_quantity == result.order.quantity
    assert result.order.reserved_maximum_loss == Money(0)
    assert not hasattr(result, "position")
    assert not hasattr(result, "settlement")

    assert len(result.journals) == 1
    assert result.journals[0].balanced
    assert result.metrics.contributions == capital
    assert result.metrics.cash == capital
    assert result.metrics.ledger_equity == capital
    assert result.metrics.position_cost == Money(0)
    assert result.metrics.realized_trading_pnl_before_fees == Decimal(0)
    assert result.metrics.fees == Money(0)
    assert result.metrics.net_trading_pnl == Decimal(0)
    assert canonical_decimal(result.allocation.quantity.value) == str(authorized_quantity)
    assert Decimal(canonical_decimal(initial.maximum_loss.amount)) == (quantity * Decimal("0.40"))


@settings(max_examples=25, deadline=None)
@given(
    event_index=st.integers(min_value=0, max_value=1),
    conflicting_hash_digit=st.sampled_from(("0", "f")),
)
def test_unfilled_event_identity_is_idempotent_and_conflicts_fail_closed(
    event_index: int,
    conflicting_hash_digit: str,
) -> None:
    simulation = _unfilled_simulation()
    result = simulation.run_decision_and_execution()
    assert isinstance(result, UnfilledExpiredResult)
    event = result.execution_events[event_index]
    before = canonical_json_bytes(simulation.finalize())

    duplicate = simulation.apply_unfilled_execution(event)
    assert duplicate.events == result.execution_events
    assert duplicate.reservation_lifecycle == result.reservation_lifecycle
    assert duplicate.decision.order == result.order
    assert canonical_json_bytes(simulation.finalize()) == before

    conflicting_hash = conflicting_hash_digit * 64
    if conflicting_hash == event.payload_hash.value:
        conflicting_hash = ("f" if conflicting_hash_digit == "0" else "0") * 64
    conflicting = replace(
        event,
        payload_hash=ContentHash(conflicting_hash),
    )
    with pytest.raises(SimulationConflictError, match="conflicting content"):
        simulation.apply_unfilled_execution(conflicting)
    assert canonical_json_bytes(simulation.finalize()) == before


@settings(max_examples=25, deadline=None)
@given(
    after_open=st.booleans(),
    invalid_sequence=st.integers(min_value=3, max_value=20),
)
def test_unfilled_expiration_sequence_violations_cannot_mutate_state(
    after_open: bool,
    invalid_sequence: int,
) -> None:
    result = _unfilled_simulation().run_decision_and_execution()
    assert isinstance(result, UnfilledExpiredResult)
    initial_reservation, ready_order = create_reserved_order(
        campaign_id=result.campaign.campaign_id,
        proposal=result.proposal,
        allocation=result.allocation,
        risk_decision=result.risk_decision,
    )
    assert isinstance(ready_order, ExpiringLimitOrder)
    current_order = ready_order
    current_reservation = initial_reservation
    if after_open:
        opened = apply_unfilled_order_event(
            current_order,
            current_reservation,
            result.execution_events[0],
        )
        current_order = opened.order
        current_reservation = opened.reservation
    invalid_expiration = replace(
        result.execution_events[1],
        sequence=invalid_sequence,
    )
    before = canonical_json_bytes(
        {
            "order": current_order,
            "reservation": current_reservation,
            "journals": result.journals,
            "metrics": result.metrics,
        }
    )

    with pytest.raises(ValueValidationError, match="out of sequence"):
        apply_unfilled_order_event(
            current_order,
            current_reservation,
            invalid_expiration,
        )

    assert before == canonical_json_bytes(
        {
            "order": current_order,
            "reservation": current_reservation,
            "journals": result.journals,
            "metrics": result.metrics,
        }
    )
    assert current_reservation.maximum_loss == result.initial_reservation.maximum_loss
    assert current_reservation.state is (
        ReservationState.OPEN if after_open else ReservationState.HELD
    )


@settings(max_examples=30, deadline=None)
@given(
    total_steps=st.integers(min_value=2, max_value=500),
    fill_selector=st.integers(min_value=0, max_value=10_000),
    increment_text=st.sampled_from(("0.01", "0.1", "1")),
)
def test_partial_fill_risk_conservation_is_exact_for_decimal_increments(
    total_steps: int,
    fill_selector: int,
    increment_text: str,
) -> None:
    baseline = _partial_fill_result()
    reservation, ready_order = create_reserved_order(
        campaign_id=baseline.campaign.campaign_id,
        proposal=baseline.proposal,
        allocation=baseline.allocation,
        risk_decision=baseline.risk_decision,
    )
    assert isinstance(ready_order, ExpiringLimitOrder)

    increment = Decimal(increment_text)
    fill_steps = 1 + fill_selector % (total_steps - 1)
    ordered_quantity = Decimal(total_steps) * increment
    filled_quantity = Decimal(fill_steps) * increment
    remaining_quantity = ordered_quantity - filled_quantity
    maximum_loss_per_unit = Money("0.40")
    fee_per_unit = Money("0.01")
    initial_risk = Money(maximum_loss_per_unit.amount * ordered_quantity)
    ready_order = replace(
        ready_order,
        quantity=Quantity(ordered_quantity, increment),
        filled_quantity=Quantity(0, increment),
        reserved_maximum_loss=initial_risk,
        state=OrderState.READY,
        last_execution_sequence=0,
    )
    reservation = replace(
        reservation,
        maximum_loss=initial_risk,
        state=ReservationState.HELD,
    )
    opened_event, fill_event, canceled_event = baseline.execution_events
    opened_event = replace(opened_event, quantity=Quantity(0, increment))
    fill_event = replace(
        fill_event,
        quantity=Quantity(filled_quantity, increment),
        price=Price("0.39", "0.01"),
        fee=Money(fee_per_unit.amount * filled_quantity),
    )
    canceled_event = replace(canceled_event, quantity=Quantity(0, increment))

    opened = apply_partial_fill_canceled_event(
        ready_order,
        reservation,
        None,
        opened_event,
        instrument_id=baseline.proposal.instrument_id,
        rule_version_id=baseline.proposal.rule_version_id,
        maximum_loss_per_unit=maximum_loss_per_unit,
        fee_per_unit=fee_per_unit,
    )
    partial = apply_partial_fill_canceled_event(
        opened.order,
        opened.reservation,
        opened.position,
        fill_event,
        instrument_id=baseline.proposal.instrument_id,
        rule_version_id=baseline.proposal.rule_version_id,
        maximum_loss_per_unit=maximum_loss_per_unit,
        fee_per_unit=fee_per_unit,
    )
    assert isinstance(partial.position, FilledPositionState)
    canceled = apply_partial_fill_canceled_event(
        partial.order,
        partial.reservation,
        partial.position,
        canceled_event,
        instrument_id=baseline.proposal.instrument_id,
        rule_version_id=baseline.proposal.rule_version_id,
        maximum_loss_per_unit=maximum_loss_per_unit,
        fee_per_unit=fee_per_unit,
    )

    expected_filled_risk = Money(maximum_loss_per_unit.amount * filled_quantity)
    expected_unfilled_risk = Money(maximum_loss_per_unit.amount * remaining_quantity)
    assert partial.order.filled_quantity == Quantity(filled_quantity, increment)
    assert partial.order.remaining_quantity == Quantity(remaining_quantity, increment)
    assert partial.order.filled_quantity.value < partial.order.quantity.value
    assert partial.order.remaining_quantity.value >= 0
    assert partial.reservation.maximum_loss == expected_unfilled_risk
    assert partial.position.maximum_remaining_loss == expected_filled_risk
    assert (
        active_campaign_risk(
            partial.position.maximum_remaining_loss,
            partial.reservation.maximum_loss,
        )
        == initial_risk
    )

    assert canceled.released_maximum_loss == expected_unfilled_risk
    assert canceled.reservation.maximum_loss == Money(0)
    assert canceled.position == partial.position
    assert isinstance(canceled.position, FilledPositionState)
    assert (
        active_campaign_risk(
            canceled.position.maximum_remaining_loss,
            canceled.reservation.maximum_loss,
        )
        == expected_filled_risk
    )
    capacity_before = Money(ordered_quantity * Decimal(4))
    capacity_after_fill = Money(
        capacity_before.amount
        - active_campaign_risk(
            partial.position.maximum_remaining_loss,
            partial.reservation.maximum_loss,
        ).amount
    )
    capacity_after_cancel = Money(
        capacity_before.amount
        - active_campaign_risk(
            canceled.position.maximum_remaining_loss,
            canceled.reservation.maximum_loss,
        ).amount
    )
    assert capacity_after_fill.amount == capacity_before.amount - initial_risk.amount
    assert capacity_after_cancel.amount - capacity_after_fill.amount == (
        expected_unfilled_risk.amount
    )
    assert Decimal(canonical_decimal(filled_quantity)) == filled_quantity
    assert Decimal(canonical_decimal(remaining_quantity)) == remaining_quantity
    assert filled_quantity % increment == 0
    assert remaining_quantity % increment == 0


@settings(max_examples=25, deadline=None)
@given(
    authorized_quantity=st.integers(min_value=2, max_value=1000),
    fill_percent=st.integers(min_value=1, max_value=99),
)
def test_partial_fill_application_moves_money_only_for_executed_quantity(
    authorized_quantity: int,
    fill_percent: int,
) -> None:
    inputs = _partial_fill_application_inputs()
    script = replace(
        inputs.execution_script,
        ordered_quantity="100",
        fill_quantity=str(fill_percent),
        remaining_quantity=str(100 - fill_percent),
    )
    capital = Money(Decimal(authorized_quantity) * Decimal(4))
    simulation = _partial_fill_simulation(
        capital=capital,
        execution_script=script,
    )
    result = simulation.run_decision_and_execution()

    assert isinstance(result, PartialFillCanceledResult)
    ordered = Decimal(authorized_quantity)
    filled = max(
        result.order.quantity.increment,
        floor_to_increment(
            ordered * Decimal(fill_percent) / Decimal(100),
            result.order.quantity.increment,
        ),
    )
    remaining = ordered - filled
    filled_cost = filled * Decimal("0.39")
    fill_fee = filled * Decimal("0.01")
    filled_risk = Money(filled * Decimal("0.40"))
    unfilled_risk = Money(remaining * Decimal("0.40"))
    initial_risk = Money(ordered * Decimal("0.40"))

    assert result.order.quantity == Quantity(ordered, "1")
    assert result.order.filled_quantity == Quantity(filled, "1")
    assert result.order.remaining_quantity == Quantity(remaining, "1")
    assert result.order.filled_quantity.value <= result.order.quantity.value
    assert result.order.remaining_quantity.value >= 0
    assert result.initial_reservation.maximum_loss == initial_risk
    assert result.reservation_after_open.maximum_loss == initial_risk
    assert result.reservation_after_partial_fill.maximum_loss == unfilled_risk
    assert result.position.maximum_remaining_loss == filled_risk
    assert result.active_risk_after_partial_fill == initial_risk
    assert result.released_at_cancellation == unfilled_risk
    assert result.final_reservation.maximum_loss == Money(0)
    assert result.final_active_campaign_risk == filled_risk

    assert result.risk_capacity_before_order == capital
    assert result.risk_capacity_while_fully_reserved.amount == (
        capital.amount - initial_risk.amount
    )
    assert result.risk_capacity_after_partial_fill == result.risk_capacity_while_fully_reserved
    assert result.risk_capacity_after_cancellation.amount == (capital.amount - filled_risk.amount)
    assert (
        result.risk_capacity_after_cancellation.amount
        - result.risk_capacity_after_partial_fill.amount
        == unfilled_risk.amount
    )

    assert result.position.open_quantity == result.order.filled_quantity
    assert result.position.cost_basis == Money(filled_cost)
    assert result.metrics.contributions == capital
    assert result.metrics.cash == Money(capital.amount - filled_cost - fill_fee)
    assert result.metrics.position_cost == Money(filled_cost)
    assert result.metrics.ledger_equity == Money(capital.amount - fill_fee)
    assert result.metrics.realized_trading_pnl_before_fees == Decimal(0)
    assert result.metrics.fees == Money(fill_fee)
    assert result.metrics.net_trading_pnl == -fill_fee
    assert len(result.journals) == 3
    assert all(journal.balanced for journal in result.journals)
    fill_event = result.execution_events[1]
    canceled_event = result.execution_events[2]
    execution_journals = tuple(
        journal for journal in result.journals if journal.source_id == fill_event.event_id
    )
    assert len(execution_journals) == 2
    assert all(journal.source_id != canceled_event.event_id for journal in result.journals)
    assert not hasattr(result, "settlement")


@settings(max_examples=25, deadline=None)
@given(
    conflicting_event_index=st.integers(min_value=0, max_value=2),
    conflicting_hash_digit=st.sampled_from(("0", "f")),
)
def test_partial_event_duplicates_and_conflicts_are_financially_atomic(
    conflicting_event_index: int,
    conflicting_hash_digit: str,
) -> None:
    simulation = _partial_fill_simulation()
    result = simulation.run_decision_and_execution()
    assert isinstance(result, PartialFillCanceledResult)
    before = canonical_json_bytes(simulation.finalize())

    for event in result.execution_events:
        duplicate = simulation.apply_partial_execution(event)
        assert duplicate.events == result.execution_events
        assert duplicate.reservation_lifecycle == result.reservation_lifecycle
        assert duplicate.decision.order == result.order
        assert duplicate.position == result.position
        assert duplicate.metrics == result.metrics
    assert canonical_json_bytes(simulation.finalize()) == before

    event = result.execution_events[conflicting_event_index]
    conflicting_hash = conflicting_hash_digit * 64
    if conflicting_hash == event.payload_hash.value:
        conflicting_hash = ("f" if conflicting_hash_digit == "0" else "0") * 64
    conflicting = replace(
        event,
        payload_hash=ContentHash(conflicting_hash),
    )
    with pytest.raises(SimulationConflictError, match="conflicting content"):
        simulation.apply_partial_execution(conflicting)

    after = simulation.finalize()
    assert isinstance(after, PartialFillCanceledResult)
    assert canonical_json_bytes(after) == before
    assert after.position == result.position
    assert after.journals == result.journals
    assert after.reservation_lifecycle == result.reservation_lifecycle
    assert len(after.journals) == 3


@settings(max_examples=25, deadline=None)
@given(
    scenario=st.sampled_from(
        (
            "fill_before_open",
            "cancel_before_open",
            "cancel_before_fill",
            "gap_after_open",
            "reopen_after_fill",
            "second_fill",
            "fill_after_cancel",
            "expire_after_cancel",
        )
    ),
    invalid_sequence=st.integers(min_value=4, max_value=20),
)
def test_partial_invalid_event_ordering_cannot_mutate_any_state(
    scenario: str,
    invalid_sequence: int,
) -> None:
    result = _partial_fill_result()
    reservation, ready_order = create_reserved_order(
        campaign_id=result.campaign.campaign_id,
        proposal=result.proposal,
        allocation=result.allocation,
        risk_decision=result.risk_decision,
    )
    assert isinstance(ready_order, ExpiringLimitOrder)
    opened_event, fill_event, canceled_event = result.execution_events
    opened = apply_partial_fill_canceled_event(
        ready_order,
        reservation,
        None,
        opened_event,
        instrument_id=result.proposal.instrument_id,
        rule_version_id=result.proposal.rule_version_id,
        maximum_loss_per_unit=result.proposal.maximum_loss_per_unit,
        fee_per_unit=result.proposal.fee_per_unit,
    )
    partial = apply_partial_fill_canceled_event(
        opened.order,
        opened.reservation,
        opened.position,
        fill_event,
        instrument_id=result.proposal.instrument_id,
        rule_version_id=result.proposal.rule_version_id,
        maximum_loss_per_unit=result.proposal.maximum_loss_per_unit,
        fee_per_unit=result.proposal.fee_per_unit,
    )
    assert isinstance(partial.position, FilledPositionState)
    canceled = apply_partial_fill_canceled_event(
        partial.order,
        partial.reservation,
        partial.position,
        canceled_event,
        instrument_id=result.proposal.instrument_id,
        rule_version_id=result.proposal.rule_version_id,
        maximum_loss_per_unit=result.proposal.maximum_loss_per_unit,
        fee_per_unit=result.proposal.fee_per_unit,
    )
    assert isinstance(canceled.position, FilledPositionState)

    if scenario == "fill_before_open":
        current = (ready_order, reservation, None)
        invalid_event = fill_event
    elif scenario == "cancel_before_open":
        current = (ready_order, reservation, None)
        invalid_event = canceled_event
    elif scenario == "cancel_before_fill":
        current = (opened.order, opened.reservation, opened.position)
        invalid_event = replace(canceled_event, sequence=2)
    elif scenario == "gap_after_open":
        current = (opened.order, opened.reservation, opened.position)
        invalid_event = replace(fill_event, sequence=invalid_sequence)
    elif scenario == "reopen_after_fill":
        current = (partial.order, partial.reservation, partial.position)
        invalid_event = replace(opened_event, sequence=3)
    elif scenario == "second_fill":
        current = (partial.order, partial.reservation, partial.position)
        invalid_event = replace(
            fill_event,
            event_id="execution_event_property_second_fill",
            sequence=3,
        )
    elif scenario == "fill_after_cancel":
        current = (canceled.order, canceled.reservation, canceled.position)
        invalid_event = replace(
            fill_event,
            event_id="execution_event_property_fill_after_cancel",
            sequence=4,
        )
    else:
        current = (canceled.order, canceled.reservation, canceled.position)
        invalid_event = replace(
            canceled_event,
            event_id="execution_event_property_expire_after_cancel",
            sequence=4,
            event_type=ExecutionEventType.EXPIRED,
        )

    current_order, current_reservation, current_position = current
    before = canonical_json_bytes(
        {
            "order": current_order,
            "reservation": current_reservation,
            "position": current_position,
            "journals": result.journals,
            "metrics": result.metrics,
        }
    )
    with pytest.raises(ValueValidationError):
        apply_partial_fill_canceled_event(
            current_order,
            current_reservation,
            current_position,
            invalid_event,
            instrument_id=result.proposal.instrument_id,
            rule_version_id=result.proposal.rule_version_id,
            maximum_loss_per_unit=result.proposal.maximum_loss_per_unit,
            fee_per_unit=result.proposal.fee_per_unit,
        )
    assert before == canonical_json_bytes(
        {
            "order": current_order,
            "reservation": current_reservation,
            "position": current_position,
            "journals": result.journals,
            "metrics": result.metrics,
        }
    )


@settings(max_examples=25, deadline=None)
@given(
    authorized_quantity=st.integers(min_value=2, max_value=500),
    fill_percent=st.integers(min_value=1, max_value=99),
)
def test_settled_no_application_derecognizes_only_the_executed_quantity(
    authorized_quantity: int,
    fill_percent: int,
) -> None:
    inputs = _settled_no_application_inputs()
    script = replace(
        inputs.execution_script,
        ordered_quantity="100",
        fill_quantity=str(fill_percent),
        remaining_quantity=str(100 - fill_percent),
    )
    capital = Money(Decimal(authorized_quantity) * Decimal(4))
    simulation = _settled_no_simulation(
        capital=capital,
        execution_script=script,
    )
    checkpoint = simulation.run_decision_and_execution()
    assert isinstance(checkpoint, PartialExecutionCheckpoint)
    assert isinstance(checkpoint.decision.order, ExpiringLimitOrder)
    assert isinstance(checkpoint.position, FilledPositionState)
    assert len(checkpoint.events) == 3
    assert len(checkpoint.reservation_lifecycle) == 4
    order_before_settlement = checkpoint.decision.order
    events_before_settlement = checkpoint.events
    position_before_settlement = checkpoint.position

    applied = simulation.apply_settlement(inputs.settlement_inputs)
    result = simulation.finalize()

    assert isinstance(result, PartialFillCanceledSettledNoResult)
    ordered = Decimal(authorized_quantity)
    filled = max(
        result.order.quantity.increment,
        floor_to_increment(
            ordered * Decimal(fill_percent) / Decimal(100),
            result.order.quantity.increment,
        ),
    )
    remaining = ordered - filled
    filled_cost = filled * Decimal("0.39")
    execution_fee = filled * Decimal("0.01")
    filled_risk = filled * Decimal("0.40")
    expected_cash_and_equity = capital.amount - filled_cost - execution_fee

    assert result.order == order_before_settlement
    assert result.order.state is OrderState.CANCELED
    assert result.order.quantity == Quantity(ordered, "1")
    assert result.order.filled_quantity == Quantity(filled, "1")
    assert result.order.remaining_quantity == Quantity(remaining, "1")
    assert result.order.reserved_maximum_loss == Money(0)
    assert result.execution_events == events_before_settlement
    assert tuple(event.event_type for event in result.execution_events) == (
        ExecutionEventType.OPENED,
        ExecutionEventType.FILL,
        ExecutionEventType.CANCELED,
    )
    assert (
        sum(event.event_type is ExecutionEventType.FILL for event in result.execution_events) == 1
    )

    assert result.pre_settlement_position == position_before_settlement
    assert result.pre_settlement_position.open_quantity == Quantity(filled, "1")
    assert result.pre_settlement_position.cost_basis == Money(filled_cost)
    assert result.pre_settlement_position.maximum_remaining_loss == Money(filled_risk)
    assert result.settlement == applied
    assert result.settlement.filled_quantity == Quantity(filled, "1")
    assert result.settlement.filled_quantity != result.order.quantity
    assert result.settlement.settlement_cash == Money(0)
    assert result.settlement.realized_pnl == -filled_cost
    assert result.settlement.position_risk_release == Money(filled_risk)
    assert result.settlement.reservation_release == Money(0)

    assert result.final_reservation == checkpoint.reservation_lifecycle[-1]
    assert result.final_reservation.state is ReservationState.RELEASED
    assert result.final_reservation.maximum_loss == Money(0)
    assert result.settled_position.settlement_state is SettlementState.SETTLED
    assert result.settled_position.open_quantity == Quantity(0, "1")
    assert result.settled_position.settled_quantity == Quantity(filled, "1")
    assert result.settled_position.cost_basis == Money(0)
    assert result.settled_position.maximum_remaining_loss == Money(0)
    assert result.final_active_campaign_risk == Money(0)
    assert active_campaign_risk(
        result.settled_position.maximum_remaining_loss,
        result.final_reservation.maximum_loss,
    ) == Money(0)

    assert len(result.journals) == 4
    assert all(journal.balanced for journal in result.journals)
    assert sum(journal.journal_type is JournalType.CONTRIBUTION for journal in result.journals) == 1
    assert sum(journal.journal_type is JournalType.PURCHASE for journal in result.journals) == 1
    assert sum(journal.journal_type is JournalType.FEE for journal in result.journals) == 1
    assert sum(journal.journal_type is JournalType.SETTLEMENT for journal in result.journals) == 1
    settlement_journal = next(
        journal for journal in result.journals if journal.journal_type is JournalType.SETTLEMENT
    )
    assert all(posting.amount == Money(filled_cost) for posting in settlement_journal.postings)

    assert result.metrics.contributions == capital
    assert result.metrics.cash == Money(expected_cash_and_equity)
    assert result.metrics.position_cost == Money(0)
    assert result.metrics.ledger_equity == Money(expected_cash_and_equity)
    assert result.metrics.realized_trading_pnl_before_fees == -filled_cost
    assert result.metrics.fees == Money(execution_fee)
    assert result.metrics.net_trading_pnl == -filled_cost - execution_fee
    assert (
        result.metrics.ledger_equity.amount
        == result.metrics.contributions.amount + result.metrics.net_trading_pnl
    )
    assert result.risk_capacity_after_settlement == result.metrics.ledger_equity

    for exact_value in (
        filled,
        remaining,
        filled_cost,
        execution_fee,
        filled_risk,
        expected_cash_and_equity,
        result.metrics.net_trading_pnl,
    ):
        assert Decimal(canonical_decimal(exact_value)) == exact_value


@settings(max_examples=20, deadline=None)
@given(
    conflict_kind=st.sampled_from(("file_hash", "fixture_id")),
    conflict_digit=st.sampled_from(("0", "f")),
    rule_mismatch=st.sampled_from(("version", "content_hash")),
)
def test_settled_no_duplicates_conflicts_and_rule_mismatches_are_atomic(
    conflict_kind: str,
    conflict_digit: str,
    rule_mismatch: str,
) -> None:
    inputs = _settled_no_application_inputs()
    simulation = _settled_no_simulation()
    checkpoint = simulation.run_decision_and_execution()
    assert isinstance(checkpoint, PartialExecutionCheckpoint)
    assert isinstance(checkpoint.decision.order, ExpiringLimitOrder)
    assert isinstance(checkpoint.position, FilledPositionState)
    execution_snapshot = canonical_json_bytes(
        {
            "order": checkpoint.decision.order,
            "events": checkpoint.events,
            "reservations": checkpoint.reservation_lifecycle,
            "position": checkpoint.position,
            "metrics": checkpoint.metrics,
        }
    )

    first = simulation.apply_settlement(inputs.settlement_inputs)
    before_duplicate = canonical_json_bytes(simulation.finalize())
    duplicate = simulation.apply_settlement(inputs.settlement_inputs)
    assert duplicate == first
    assert canonical_json_bytes(simulation.finalize()) == before_duplicate

    if conflict_kind == "file_hash":
        conflicting_hash = ContentHash(conflict_digit * 64)
        if conflicting_hash == inputs.settlement_inputs.settlement_file_hash:
            conflicting_hash = ContentHash(("f" if conflict_digit == "0" else "0") * 64)
        conflicting = replace(
            inputs.settlement_inputs,
            settlement_file_hash=conflicting_hash,
        )
    else:
        conflicting = replace(
            inputs.settlement_inputs,
            fixture_id="partial-fill-canceled-settled-no-conflict",
        )
    with pytest.raises(SimulationConflictError, match="conflicting content"):
        simulation.apply_settlement(conflicting)
    assert canonical_json_bytes(simulation.finalize()) == before_duplicate

    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledNoResult)
    assert (
        canonical_json_bytes(
            {
                "order": result.order,
                "events": result.execution_events,
                "reservations": result.reservation_lifecycle,
                "position": result.pre_settlement_position,
                "metrics": result.pre_settlement_metrics,
            }
        )
        == execution_snapshot
    )
    assert len(result.journals) == 4
    assert sum(journal.journal_type is JournalType.FEE for journal in result.journals) == 1
    assert sum(journal.journal_type is JournalType.SETTLEMENT for journal in result.journals) == 1

    if rule_mismatch == "version":
        mismatched_rule = replace(
            result.contract_rule,
            rule_version_id=f"{result.contract_rule.rule_version_id}-conflict",
        )
        expected_message = "rule version mismatch"
    else:
        conflicting_rule_hash = ContentHash(conflict_digit * 64)
        if conflicting_rule_hash == result.contract_rule.content_hash:
            conflicting_rule_hash = ContentHash(("f" if conflict_digit == "0" else "0") * 64)
        mismatched_rule = replace(
            result.contract_rule,
            content_hash=conflicting_rule_hash,
        )
        expected_message = "rule content hash mismatch"
    domain_snapshot = canonical_json_bytes(
        {
            "position": result.pre_settlement_position,
            "reservation": result.final_reservation,
            "order": result.order,
            "events": result.execution_events,
            "journals": result.journals,
            "metrics": result.metrics,
        }
    )
    with pytest.raises(ValueValidationError, match=expected_message):
        settle_canceled_filled_position(
            result.pre_settlement_position,
            result.final_reservation,
            result.order,
            result.information_set.instrument,
            mismatched_rule,
            result.settlement_fact,
            execution_completed_at=result.execution_events[-1].occurred_at,
        )
    assert domain_snapshot == canonical_json_bytes(
        {
            "position": result.pre_settlement_position,
            "reservation": result.final_reservation,
            "order": result.order,
            "events": result.execution_events,
            "journals": result.journals,
            "metrics": result.metrics,
        }
    )


@settings(max_examples=30, deadline=None)
@given(
    total_steps=st.integers(min_value=2, max_value=250),
    fill_selector=st.integers(min_value=0, max_value=10_000),
    increment_text=st.sampled_from(("0.01", "0.1", "1")),
)
def test_settled_no_decimal_partial_fills_conserve_money_and_risk(
    total_steps: int,
    fill_selector: int,
    increment_text: str,
) -> None:
    baseline = _settled_no_result()
    reservation, ready_order = create_reserved_order(
        campaign_id=baseline.campaign.campaign_id,
        proposal=baseline.proposal,
        allocation=baseline.allocation,
        risk_decision=baseline.risk_decision,
    )
    assert isinstance(ready_order, ExpiringLimitOrder)

    increment = Decimal(increment_text)
    fill_steps = 1 + fill_selector % (total_steps - 1)
    ordered_quantity = Decimal(total_steps) * increment
    filled_quantity = Decimal(fill_steps) * increment
    remaining_quantity = ordered_quantity - filled_quantity
    initial_risk = Money(ordered_quantity * Decimal("0.40"))
    ready_order = replace(
        ready_order,
        quantity=Quantity(ordered_quantity, increment),
        filled_quantity=Quantity(0, increment),
        reserved_maximum_loss=initial_risk,
        state=OrderState.READY,
        last_execution_sequence=0,
    )
    reservation = replace(
        reservation,
        maximum_loss=initial_risk,
        state=ReservationState.HELD,
    )
    opened_event, fill_event, canceled_event = baseline.execution_events
    opened_event = replace(opened_event, quantity=Quantity(0, increment))
    fill_event = replace(
        fill_event,
        quantity=Quantity(filled_quantity, increment),
        price=Price("0.39", "0.01"),
        fee=Money(filled_quantity * Decimal("0.01")),
    )
    canceled_event = replace(canceled_event, quantity=Quantity(0, increment))

    opened = apply_partial_fill_canceled_event(
        ready_order,
        reservation,
        None,
        opened_event,
        instrument_id=baseline.proposal.instrument_id,
        rule_version_id=baseline.proposal.rule_version_id,
        maximum_loss_per_unit=Money("0.40"),
        fee_per_unit=Money("0.01"),
    )
    partial = apply_partial_fill_canceled_event(
        opened.order,
        opened.reservation,
        opened.position,
        fill_event,
        instrument_id=baseline.proposal.instrument_id,
        rule_version_id=baseline.proposal.rule_version_id,
        maximum_loss_per_unit=Money("0.40"),
        fee_per_unit=Money("0.01"),
    )
    assert isinstance(partial.position, FilledPositionState)
    canceled = apply_partial_fill_canceled_event(
        partial.order,
        partial.reservation,
        partial.position,
        canceled_event,
        instrument_id=baseline.proposal.instrument_id,
        rule_version_id=baseline.proposal.rule_version_id,
        maximum_loss_per_unit=Money("0.40"),
        fee_per_unit=Money("0.01"),
    )
    assert isinstance(canceled.order, ExpiringLimitOrder)
    assert isinstance(canceled.position, FilledPositionState)
    canceled_snapshot = canonical_json_bytes(
        {
            "order": canceled.order,
            "reservation": canceled.reservation,
            "position": canceled.position,
            "events": (opened_event, fill_event, canceled_event),
        }
    )

    settled = settle_canceled_filled_position(
        canceled.position,
        canceled.reservation,
        canceled.order,
        baseline.information_set.instrument,
        baseline.contract_rule,
        baseline.settlement_fact,
        execution_completed_at=canceled_event.occurred_at,
    )

    expected_cost = filled_quantity * Decimal("0.39")
    expected_fee = filled_quantity * Decimal("0.01")
    expected_filled_risk = filled_quantity * Decimal("0.40")
    expected_unfilled_risk = remaining_quantity * Decimal("0.40")
    assert partial.position.cost_basis == Money(expected_cost)
    assert partial.position.maximum_remaining_loss == Money(expected_filled_risk)
    assert partial.reservation.maximum_loss == Money(expected_unfilled_risk)
    assert (
        active_campaign_risk(
            partial.position.maximum_remaining_loss,
            partial.reservation.maximum_loss,
        )
        == initial_risk
    )
    assert canceled.released_maximum_loss == Money(expected_unfilled_risk)
    assert settled.result.filled_quantity == Quantity(filled_quantity, increment)
    assert settled.result.filled_quantity != Quantity(ordered_quantity, increment)
    assert settled.result.settlement_cash == Money(0)
    assert settled.result.realized_pnl == -expected_cost
    assert settled.result.position_risk_release == Money(expected_filled_risk)
    assert settled.result.reservation_release == Money(0)
    assert settled.reservation == canceled.reservation
    assert settled.position.settlement_state is SettlementState.SETTLED
    assert settled.position.open_quantity == Quantity(0, increment)
    assert settled.position.settled_quantity == Quantity(filled_quantity, increment)
    assert settled.position.cost_basis == Money(0)
    assert settled.position.maximum_remaining_loss == Money(0)
    assert active_campaign_risk(
        settled.position.maximum_remaining_loss,
        settled.reservation.maximum_loss,
    ) == Money(0)
    assert canceled_snapshot == canonical_json_bytes(
        {
            "order": canceled.order,
            "reservation": canceled.reservation,
            "position": canceled.position,
            "events": (opened_event, fill_event, canceled_event),
        }
    )

    capital = Money(ordered_quantity * Decimal(4))
    ledger = Ledger()
    ledger.post(JournalFactory.contribution("settled-no-property-funding", capital, NOW))
    ledger.post(
        JournalFactory.purchase(fill_event.event_id, Money(expected_cost), fill_event.occurred_at)
    )
    ledger.post(
        JournalFactory.fee(fill_event.event_id, Money(expected_fee), fill_event.occurred_at)
    )
    ledger.post(
        JournalFactory.settlement(
            baseline.settlement_fact.source_id,
            Money(0),
            Money(expected_cost),
            baseline.settlement_fact.available_at,
        )
    )
    metrics = ledger.metrics()
    expected_final_equity = capital.amount - expected_cost - expected_fee

    assert len(ledger.journals) == 4
    assert all(journal.balanced for journal in ledger.journals)
    assert sum(journal.journal_type is JournalType.FEE for journal in ledger.journals) == 1
    assert sum(journal.journal_type is JournalType.SETTLEMENT for journal in ledger.journals) == 1
    assert metrics.contributions == capital
    assert metrics.cash == Money(expected_final_equity)
    assert metrics.position_cost == Money(0)
    assert metrics.ledger_equity == Money(expected_final_equity)
    assert metrics.realized_trading_pnl_before_fees == -expected_cost
    assert metrics.fees == Money(expected_fee)
    assert metrics.net_trading_pnl == -expected_cost - expected_fee
    assert metrics.ledger_equity.amount == metrics.contributions.amount + metrics.net_trading_pnl

    for exact_value in (
        ordered_quantity,
        filled_quantity,
        remaining_quantity,
        expected_cost,
        expected_fee,
        expected_filled_risk,
        expected_unfilled_risk,
        expected_final_equity,
        metrics.net_trading_pnl,
    ):
        assert Decimal(canonical_decimal(exact_value)) == exact_value


@settings(max_examples=25, deadline=None)
@given(
    authorized_quantity=st.integers(min_value=2, max_value=500),
    fill_percent=st.integers(min_value=1, max_value=99),
)
def test_settled_void_application_uses_only_executed_quantity_and_counts_fee_once(
    authorized_quantity: int,
    fill_percent: int,
) -> None:
    inputs = _settled_void_application_inputs()
    script = replace(
        inputs.execution_script,
        ordered_quantity="100",
        fill_quantity=str(fill_percent),
        remaining_quantity=str(100 - fill_percent),
    )
    capital = Money(Decimal(authorized_quantity) * Decimal(4))
    simulation = _settled_void_simulation(
        capital=capital,
        execution_script=script,
    )
    checkpoint = simulation.run_decision_and_execution()
    assert isinstance(checkpoint, PartialExecutionCheckpoint)
    assert isinstance(checkpoint.decision.order, ExpiringLimitOrder)
    assert isinstance(checkpoint.position, FilledPositionState)
    order_before_settlement = checkpoint.decision.order
    events_before_settlement = checkpoint.events
    execution_snapshot = canonical_json_bytes(
        {
            "order": order_before_settlement,
            "events": events_before_settlement,
            "reservations": checkpoint.reservation_lifecycle,
            "position": checkpoint.position,
            "metrics": checkpoint.metrics,
        }
    )

    applied = simulation.apply_settlement(inputs.settlement_inputs)
    result = simulation.finalize()

    assert isinstance(result, PartialFillCanceledSettledVoidResult)
    ordered = Decimal(authorized_quantity)
    filled = max(
        result.order.quantity.increment,
        floor_to_increment(
            ordered * Decimal(fill_percent) / Decimal(100),
            result.order.quantity.increment,
        ),
    )
    canceled = ordered - filled
    entry_cost = filled * Decimal("0.39")
    execution_fee = filled * Decimal("0.01")
    position_risk = filled * Decimal("0.40")
    settlement_cash = filled * Decimal("0.40")
    realized_contract_pnl = settlement_cash - entry_cost

    assert result.order == order_before_settlement
    assert result.execution_events == events_before_settlement
    assert (
        canonical_json_bytes(
            {
                "order": result.order,
                "events": result.execution_events,
                "reservations": result.reservation_lifecycle,
                "position": result.pre_settlement_position,
                "metrics": result.pre_settlement_metrics,
            }
        )
        == execution_snapshot
    )
    assert result.order.state is OrderState.CANCELED
    assert result.order.quantity == Quantity(ordered, "1")
    assert result.order.filled_quantity == Quantity(filled, "1")
    assert result.order.remaining_quantity == Quantity(canceled, "1")
    assert result.order.reserved_maximum_loss == Money(0)
    assert tuple(event.event_type for event in result.execution_events) == (
        ExecutionEventType.OPENED,
        ExecutionEventType.FILL,
        ExecutionEventType.CANCELED,
    )
    assert (
        sum(event.event_type is ExecutionEventType.FILL for event in result.execution_events) == 1
    )

    assert result.pre_settlement_position == checkpoint.position
    assert result.pre_settlement_position.open_quantity == Quantity(filled, "1")
    assert result.pre_settlement_position.cost_basis == Money(entry_cost)
    assert result.pre_settlement_position.maximum_remaining_loss == Money(position_risk)
    assert result.settlement == applied
    assert result.settlement.outcome_classification is SettlementOutcome.VOID
    assert result.settlement.filled_quantity == Quantity(filled, "1")
    assert result.settlement.filled_quantity != result.order.quantity
    assert result.settlement.settlement_cash == Money(settlement_cash)
    assert result.settlement.settlement_cash != Money(ordered * Decimal("0.40"))
    assert result.settlement.realized_pnl == realized_contract_pnl
    assert result.settlement.position_risk_release == Money(position_risk)
    assert result.settlement.reservation_release == Money(0)

    assert result.final_reservation == checkpoint.reservation_lifecycle[-1]
    assert result.final_reservation.state is ReservationState.RELEASED
    assert result.final_reservation.maximum_loss == Money(0)
    assert result.settled_position.settlement_state is SettlementState.SETTLED
    assert result.settled_position.outcome_classification is SettlementOutcome.VOID
    assert result.settled_position.open_quantity == Quantity(0, "1")
    assert result.settled_position.settled_quantity == Quantity(filled, "1")
    assert result.settled_position.settlement_cash == Money(settlement_cash)
    assert result.settled_position.realized_contract_pnl == realized_contract_pnl
    assert result.settled_position.cost_basis == Money(0)
    assert result.settled_position.maximum_remaining_loss == Money(0)
    assert result.final_active_campaign_risk == Money(0)

    assert len(result.journals) == 4
    assert all(journal.balanced for journal in result.journals)
    assert sum(journal.journal_type is JournalType.CONTRIBUTION for journal in result.journals) == 1
    assert sum(journal.journal_type is JournalType.PURCHASE for journal in result.journals) == 1
    assert sum(journal.journal_type is JournalType.FEE for journal in result.journals) == 1
    assert sum(journal.journal_type is JournalType.SETTLEMENT for journal in result.journals) == 1
    settlement_journal = next(
        journal for journal in result.journals if journal.journal_type is JournalType.SETTLEMENT
    )
    settlement_postings = {
        (posting.account_code, posting.side): posting.amount
        for posting in settlement_journal.postings
    }
    assert settlement_postings == {
        (AccountCode.CASH, PostingSide.DEBIT): Money(settlement_cash),
        (AccountCode.POSITION_COST, PostingSide.CREDIT): Money(entry_cost),
        (AccountCode.REALIZED_PNL, PostingSide.CREDIT): Money(realized_contract_pnl),
    }

    assert result.metrics.contributions == capital
    assert result.metrics.cash == capital
    assert result.metrics.position_cost == Money(0)
    assert result.metrics.ledger_equity == capital
    assert result.metrics.realized_trading_pnl_before_fees == realized_contract_pnl
    assert result.metrics.fees == Money(execution_fee)
    assert result.metrics.net_trading_pnl == Decimal(0)
    assert result.metrics.ledger_equity.amount == result.metrics.contributions.amount
    assert result.risk_capacity_after_settlement == capital

    for exact_value in (
        ordered,
        filled,
        canceled,
        entry_cost,
        execution_fee,
        position_risk,
        settlement_cash,
        realized_contract_pnl,
    ):
        assert Decimal(canonical_decimal(exact_value)) == exact_value


@settings(max_examples=24, deadline=None)
@given(
    conflict_kind=st.sampled_from(
        (
            "file_hash",
            "fixture_id",
            "classification",
            "value",
            "instrument",
            "rule_version",
            "source",
            "timestamp",
        )
    ),
    invalid_kind=st.sampled_from(
        (
            "instrument",
            "rule_version",
            "rule_hash",
            "rule_value",
            "classification",
            "early",
        )
    ),
    invalid_value=st.sampled_from((Decimal("-0.01"), Decimal("1.01"))),
)
def test_settled_void_duplicates_conflicts_and_invalid_rules_are_atomic(
    conflict_kind: str,
    invalid_kind: str,
    invalid_value: Decimal,
) -> None:
    inputs = _settled_void_application_inputs()
    assert isinstance(inputs.settlement_inputs.fact, ClassifiedRuleBoundSettlementFact)
    simulation = _settled_void_simulation()
    checkpoint = simulation.run_decision_and_execution()
    assert isinstance(checkpoint, PartialExecutionCheckpoint)
    assert isinstance(checkpoint.decision.order, ExpiringLimitOrder)
    assert isinstance(checkpoint.position, FilledPositionState)

    first = simulation.apply_settlement(inputs.settlement_inputs)
    settled_snapshot = canonical_json_bytes(simulation.finalize())
    duplicate = simulation.apply_settlement(inputs.settlement_inputs)
    assert duplicate == first
    assert canonical_json_bytes(simulation.finalize()) == settled_snapshot

    baseline_fact = inputs.settlement_inputs.fact
    if conflict_kind == "file_hash":
        conflicting = replace(
            inputs.settlement_inputs,
            settlement_file_hash=ContentHash("0" * 64),
        )
    elif conflict_kind == "fixture_id":
        conflicting = replace(
            inputs.settlement_inputs,
            fixture_id="partial-fill-canceled-settled-void-conflict",
        )
    elif conflict_kind == "classification":
        conflicting = replace(
            inputs.settlement_inputs,
            fact=_classified_void_fact(
                baseline_fact,
                outcome_classification=SettlementOutcome.NO,
            ),
        )
    elif conflict_kind == "value":
        conflicting = replace(
            inputs.settlement_inputs,
            fact=_classified_void_fact(
                baseline_fact,
                settlement_value=Decimal("0.41"),
            ),
        )
    elif conflict_kind == "instrument":
        conflicting = replace(
            inputs.settlement_inputs,
            fact=_classified_void_fact(
                baseline_fact,
                instrument_id=f"{baseline_fact.instrument_id}-conflict",
            ),
        )
    elif conflict_kind == "rule_version":
        conflicting = replace(
            inputs.settlement_inputs,
            fact=_classified_void_fact(
                baseline_fact,
                rule_version_id=f"{baseline_fact.rule_version_id}-conflict",
            ),
        )
    elif conflict_kind == "source":
        conflicting = replace(
            inputs.settlement_inputs,
            fact=_classified_void_fact(
                baseline_fact,
                source_id=f"{baseline_fact.source_id}-conflict",
            ),
        )
    else:
        conflicting = replace(
            inputs.settlement_inputs,
            fact=_classified_void_fact(
                baseline_fact,
                available_at=baseline_fact.available_at + timedelta(seconds=1),
            ),
        )
    with pytest.raises(SimulationConflictError, match="conflicting content"):
        simulation.apply_settlement(conflicting)
    assert canonical_json_bytes(simulation.finalize()) == settled_snapshot

    result = simulation.finalize()
    assert isinstance(result, PartialFillCanceledSettledVoidResult)
    assert result.order == checkpoint.decision.order
    assert result.execution_events == checkpoint.events
    assert len(result.journals) == 4
    assert sum(journal.journal_type is JournalType.FEE for journal in result.journals) == 1
    assert sum(journal.journal_type is JournalType.SETTLEMENT for journal in result.journals) == 1
    assert result.final_active_campaign_risk == Money(0)

    instrument = result.information_set.instrument
    rule = result.contract_rule
    fact = result.settlement_fact
    if invalid_kind == "instrument":
        instrument = replace(instrument, instrument_id=f"{instrument.instrument_id}-conflict")
        expected_message = "instrument mismatch"
    elif invalid_kind == "rule_version":
        rule = replace(rule, rule_version_id=f"{rule.rule_version_id}-conflict")
        expected_message = "rule version mismatch"
    elif invalid_kind == "rule_hash":
        fact = _classified_void_fact(
            fact,
            rule_content_hash=ContentHash("0" * 64),
        )
        expected_message = "rule content hash mismatch"
    elif invalid_kind == "rule_value":
        instrument = replace(instrument, void_settlement_value=Decimal("0.41"))
        expected_message = "instrument VOID value does not match the exact rule term"
    elif invalid_kind == "classification":
        fact = _classified_void_fact(
            fact,
            outcome_classification=SettlementOutcome.NO,
        )
        expected_message = "classification must be VOID"
    else:
        fact = _classified_void_fact(
            fact,
            source_event_at=result.execution_events[-1].occurred_at,
            available_at=result.execution_events[-1].occurred_at,
        )
        expected_message = "available before execution completed"

    domain_snapshot = canonical_json_bytes(
        {
            "order": result.order,
            "events": result.execution_events,
            "reservation": result.final_reservation,
            "position": result.pre_settlement_position,
            "journals": result.journals,
            "metrics": result.metrics,
        }
    )
    with pytest.raises(ValueValidationError, match=expected_message):
        settle_canceled_filled_void_position(
            result.pre_settlement_position,
            result.final_reservation,
            result.order,
            instrument,
            rule,
            fact,
            execution_completed_at=result.execution_events[-1].occurred_at,
        )
    assert domain_snapshot == canonical_json_bytes(
        {
            "order": result.order,
            "events": result.execution_events,
            "reservation": result.final_reservation,
            "position": result.pre_settlement_position,
            "journals": result.journals,
            "metrics": result.metrics,
        }
    )

    with pytest.raises(ValueValidationError):
        _classified_void_fact(
            baseline_fact,
            settlement_value=invalid_value,
        )
    assert canonical_json_bytes(simulation.finalize()) == settled_snapshot


@settings(max_examples=30, deadline=None)
@given(
    total_steps=st.integers(min_value=2, max_value=250),
    fill_selector=st.integers(min_value=0, max_value=10_000),
    increment_text=st.sampled_from(("0.01", "0.1", "1")),
    void_millis=st.integers(min_value=1, max_value=1000),
)
def test_settled_void_decimal_partial_fills_conserve_money_and_risk(
    total_steps: int,
    fill_selector: int,
    increment_text: str,
    void_millis: int,
) -> None:
    baseline = _settled_void_result()
    reservation, ready_order = create_reserved_order(
        campaign_id=baseline.campaign.campaign_id,
        proposal=baseline.proposal,
        allocation=baseline.allocation,
        risk_decision=baseline.risk_decision,
    )
    assert isinstance(ready_order, ExpiringLimitOrder)

    increment = Decimal(increment_text)
    fill_steps = 1 + fill_selector % (total_steps - 1)
    ordered_quantity = Decimal(total_steps) * increment
    filled_quantity = Decimal(fill_steps) * increment
    canceled_quantity = ordered_quantity - filled_quantity
    settlement_value = Decimal(void_millis) / Decimal(1000)
    initial_risk = Money(ordered_quantity * Decimal("0.40"))
    ready_order = replace(
        ready_order,
        quantity=Quantity(ordered_quantity, increment),
        filled_quantity=Quantity(0, increment),
        reserved_maximum_loss=initial_risk,
        state=OrderState.READY,
        last_execution_sequence=0,
    )
    reservation = replace(
        reservation,
        maximum_loss=initial_risk,
        state=ReservationState.HELD,
    )
    opened_event, fill_event, canceled_event = baseline.execution_events
    opened_event = replace(opened_event, quantity=Quantity(0, increment))
    fill_event = replace(
        fill_event,
        quantity=Quantity(filled_quantity, increment),
        price=Price("0.39", "0.01"),
        fee=Money(filled_quantity * Decimal("0.01")),
    )
    canceled_event = replace(canceled_event, quantity=Quantity(0, increment))

    opened = apply_partial_fill_canceled_event(
        ready_order,
        reservation,
        None,
        opened_event,
        instrument_id=baseline.proposal.instrument_id,
        rule_version_id=baseline.proposal.rule_version_id,
        maximum_loss_per_unit=Money("0.40"),
        fee_per_unit=Money("0.01"),
    )
    partial = apply_partial_fill_canceled_event(
        opened.order,
        opened.reservation,
        opened.position,
        fill_event,
        instrument_id=baseline.proposal.instrument_id,
        rule_version_id=baseline.proposal.rule_version_id,
        maximum_loss_per_unit=Money("0.40"),
        fee_per_unit=Money("0.01"),
    )
    assert isinstance(partial.position, FilledPositionState)
    canceled = apply_partial_fill_canceled_event(
        partial.order,
        partial.reservation,
        partial.position,
        canceled_event,
        instrument_id=baseline.proposal.instrument_id,
        rule_version_id=baseline.proposal.rule_version_id,
        maximum_loss_per_unit=Money("0.40"),
        fee_per_unit=Money("0.01"),
    )
    assert isinstance(canceled.order, ExpiringLimitOrder)
    assert isinstance(canceled.position, FilledPositionState)
    canceled_snapshot = canonical_json_bytes(
        {
            "order": canceled.order,
            "reservation": canceled.reservation,
            "position": canceled.position,
            "events": (opened_event, fill_event, canceled_event),
        }
    )

    rule = _void_rule_for_value(baseline.contract_rule, settlement_value)
    instrument = replace(
        baseline.information_set.instrument,
        void_settlement_value=settlement_value,
    )
    fact = _classified_void_fact(
        baseline.settlement_fact,
        settlement_value=settlement_value,
        rule_content_hash=rule.content_hash,
    )
    settled = settle_canceled_filled_void_position(
        canceled.position,
        canceled.reservation,
        canceled.order,
        instrument,
        rule,
        fact,
        execution_completed_at=canceled_event.occurred_at,
    )

    expected_cost = filled_quantity * Decimal("0.39")
    expected_fee = filled_quantity * Decimal("0.01")
    expected_filled_risk = filled_quantity * Decimal("0.40")
    expected_unfilled_risk = canceled_quantity * Decimal("0.40")
    expected_settlement_cash = filled_quantity * settlement_value
    expected_realized = expected_settlement_cash - expected_cost
    assert partial.position.cost_basis == Money(expected_cost)
    assert partial.position.maximum_remaining_loss == Money(expected_filled_risk)
    assert partial.reservation.maximum_loss == Money(expected_unfilled_risk)
    assert (
        active_campaign_risk(
            partial.position.maximum_remaining_loss,
            partial.reservation.maximum_loss,
        )
        == initial_risk
    )
    assert canceled.released_maximum_loss == Money(expected_unfilled_risk)
    assert settled.result.outcome_classification is SettlementOutcome.VOID
    assert settled.result.filled_quantity == Quantity(filled_quantity, increment)
    assert settled.result.filled_quantity != Quantity(ordered_quantity, increment)
    assert settled.result.settlement_cash == Money(expected_settlement_cash)
    assert settled.result.settlement_cash != Money(ordered_quantity * settlement_value)
    assert settled.result.realized_pnl == expected_realized
    assert settled.result.position_risk_release == Money(expected_filled_risk)
    assert settled.result.reservation_release == Money(0)
    assert settled.reservation == canceled.reservation
    assert settled.reservation.maximum_loss == Money(0)
    assert settled.position.settlement_state is SettlementState.SETTLED
    assert settled.position.outcome_classification is SettlementOutcome.VOID
    assert settled.position.open_quantity == Quantity(0, increment)
    assert settled.position.settled_quantity == Quantity(filled_quantity, increment)
    assert settled.position.settlement_value == settlement_value
    assert settled.position.settlement_cash == Money(expected_settlement_cash)
    assert settled.position.realized_contract_pnl == expected_realized
    assert settled.position.cost_basis == Money(0)
    assert settled.position.maximum_remaining_loss == Money(0)
    assert active_campaign_risk(
        settled.position.maximum_remaining_loss,
        settled.reservation.maximum_loss,
    ) == Money(0)
    assert canceled_snapshot == canonical_json_bytes(
        {
            "order": canceled.order,
            "reservation": canceled.reservation,
            "position": canceled.position,
            "events": (opened_event, fill_event, canceled_event),
        }
    )

    capital = Money(ordered_quantity * Decimal(4))
    ledger = Ledger()
    ledger.post(JournalFactory.contribution("settled-void-property-funding", capital, NOW))
    ledger.post(
        JournalFactory.purchase(fill_event.event_id, Money(expected_cost), fill_event.occurred_at)
    )
    ledger.post(
        JournalFactory.fee(fill_event.event_id, Money(expected_fee), fill_event.occurred_at)
    )
    ledger.post(
        JournalFactory.settlement(
            fact.source_id,
            Money(expected_settlement_cash),
            Money(expected_cost),
            fact.available_at,
        )
    )
    metrics = ledger.metrics()
    expected_final_cash = capital.amount - expected_cost - expected_fee + expected_settlement_cash
    expected_final_equity = capital.amount + expected_realized - expected_fee

    assert len(ledger.journals) == 4
    assert all(journal.balanced for journal in ledger.journals)
    assert sum(journal.journal_type is JournalType.FEE for journal in ledger.journals) == 1
    assert sum(journal.journal_type is JournalType.SETTLEMENT for journal in ledger.journals) == 1
    assert metrics.contributions == capital
    assert metrics.cash == Money(expected_final_cash)
    assert metrics.position_cost == Money(0)
    assert metrics.ledger_equity == Money(expected_final_equity)
    assert metrics.realized_trading_pnl_before_fees == expected_realized
    assert metrics.fees == Money(expected_fee)
    assert metrics.net_trading_pnl == expected_realized - expected_fee
    assert metrics.ledger_equity.amount == metrics.contributions.amount + metrics.net_trading_pnl

    for exact_value in (
        ordered_quantity,
        filled_quantity,
        canceled_quantity,
        settlement_value,
        expected_cost,
        expected_fee,
        expected_filled_risk,
        expected_unfilled_risk,
        expected_settlement_cash,
        expected_realized,
        expected_final_cash,
        expected_final_equity,
        metrics.net_trading_pnl,
    ):
        assert Decimal(canonical_decimal(exact_value)) == exact_value
