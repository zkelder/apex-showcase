"""Defined-Risk Options Structure Economics and Research Integration v1:
application-layer idempotency proofs, using the in-memory repository (no
database, no network).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from apex.application.options_structures import (
    BuildStructuresAcrossCohort,
    BuildStructuresAcrossCohortCommand,
    BuildStructuresForUnderlying,
    BuildStructuresForUnderlyingCommand,
    CreateOptionsFeePolicy,
    CreateOptionsSlippagePolicy,
    CreateOptionsStructureResearchPolicy,
    CreateStructurePricingPolicy,
    InMemoryOptionsStructuresRepository,
)
from apex.domain.options_intelligence import (
    ExerciseStyle,
    OptionContract,
    OptionType,
    SettlementStyle,
)
from apex.domain.options_quote_intelligence import (
    OptionQuoteObservation,
    create_options_chain_snapshot,
)
from apex.domain.options_structures import (
    OptionsFeePolicy,
    OptionsSlippagePolicy,
    OptionsStructureResearchPolicy,
    StructurePricingPolicy,
)

NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
AS_OF = NOW.date()
UNDERLYING_ID = "security-spy-stable"


def make_observation(
    *, symbol: str, option_type: OptionType, strike: str, bid: str, ask: str, underlying_id: str
) -> OptionQuoteObservation:
    side = "C" if option_type is OptionType.CALL else "P"
    contract = OptionContract.create(
        provider_contract_id=f"{symbol}260821{side}{strike}",
        underlying_canonical_security_id=underlying_id,
        standardized_symbol=None,
        option_type=option_type,
        strike=strike,
        expiration="2026-08-21",
        exercise_style=ExerciseStyle.AMERICAN,
        settlement_style=SettlementStyle.PHYSICAL,
        multiplier="100",
        deliverable="100 SHARES",
        listing_exchange=None,
        state="ACTIVE",
        contract_size_state="STANDARD",
        adjustment_state="UNKNOWN",
        source_provider="ALPACA_OPTIONS_MARKET_DATA",
        reference_receipt_ids=("receipt-1",),
    )
    return OptionQuoteObservation.create(
        contract=contract,
        expected_underlying_canonical_security_id=underlying_id,
        bid=bid,
        ask=ask,
        bid_size="10",
        ask_size="10",
        last_trade=None,
        trade_size=None,
        underlying_reference_price="500",
        volume=None,
        open_interest=None,
        provider_implied_volatility=None,
        provider_delta=None,
        provider_gamma=None,
        provider_theta=None,
        provider_vega=None,
        provider_rho=None,
        market_status="UNKNOWN",
        quote_timestamp=NOW,
        availability_timestamp=NOW,
        ingestion_timestamp=NOW,
        captured_at=NOW,
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )


def chain_for(underlying_id: str, *, symbol: str = "SPY") -> tuple[OptionQuoteObservation, ...]:
    return (
        make_observation(
            symbol=symbol,
            option_type=OptionType.CALL,
            strike="495",
            bid="9.00",
            ask="9.20",
            underlying_id=underlying_id,
        ),
        make_observation(
            symbol=symbol,
            option_type=OptionType.CALL,
            strike="500",
            bid="6.00",
            ask="6.20",
            underlying_id=underlying_id,
        ),
    )


def snapshot_for(underlying_id: str, *, symbol: str = "SPY"):
    observations = chain_for(underlying_id, symbol=symbol)
    snapshot = create_options_chain_snapshot(
        plan_id="plan-1",
        run_id="run-1",
        underlying_canonical_security_id=underlying_id,
        underlying_symbol=symbol,
        underlying_reference_price=Decimal("500"),
        underlying_price_source="TEST",
        captured_at=NOW,
        observations=observations,
        as_of=AS_OF,
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )
    return snapshot, observations


def build_policies(
    repository: InMemoryOptionsStructuresRepository, **overrides: object
) -> OptionsStructureResearchPolicy:
    pricing = CreateStructurePricingPolicy(repository).execute(
        policy=StructurePricingPolicy.create()
    )
    fee = CreateOptionsFeePolicy(repository).execute(policy=OptionsFeePolicy.create())
    slippage = CreateOptionsSlippagePolicy(repository).execute(
        policy=OptionsSlippagePolicy.create()
    )
    policy = OptionsStructureResearchPolicy.create(
        pricing_policy=pricing,
        fee_policy=fee,
        slippage_policy=slippage,
        created_at=NOW,
        **overrides,
    )
    return CreateOptionsStructureResearchPolicy(repository).execute(policy=policy)


class TestPolicyIdempotency:
    def test_creating_the_identical_fee_policy_twice_returns_the_same_row(self) -> None:
        repository = InMemoryOptionsStructuresRepository()
        service = CreateOptionsFeePolicy(repository)
        first = service.execute(policy=OptionsFeePolicy.create())
        second = service.execute(policy=OptionsFeePolicy.create())
        assert first.policy_id == second.policy_id
        assert len(repository.structures_for_underlying("anything")) == 0  # sanity: no cross-talk

    def test_creating_the_identical_structure_policy_twice_returns_the_same_row(self) -> None:
        repository = InMemoryOptionsStructuresRepository()
        pricing = StructurePricingPolicy.create()
        fee = OptionsFeePolicy.create()
        slippage = OptionsSlippagePolicy.create()
        service = CreateOptionsStructureResearchPolicy(repository)
        policy = OptionsStructureResearchPolicy.create(
            pricing_policy=pricing, fee_policy=fee, slippage_policy=slippage, created_at=NOW
        )
        first = service.execute(policy=policy)
        second = service.execute(
            policy=OptionsStructureResearchPolicy.create(
                pricing_policy=pricing, fee_policy=fee, slippage_policy=slippage, created_at=NOW
            )
        )
        assert first.policy_id == second.policy_id
        assert repository.latest_structure_policy() is not None


class TestBuildStructuresForUnderlying:
    def test_execute_persists_structures_features_and_research_snapshot(self) -> None:
        repository = InMemoryOptionsStructuresRepository()
        policy = build_policies(repository)
        fee = repository.fee_policy_by_id(policy.fee_policy_id)
        slippage = repository.slippage_policy_by_id(policy.slippage_policy_id)
        assert fee is not None and slippage is not None
        snapshot, observations = snapshot_for(UNDERLYING_ID)

        outcome = BuildStructuresForUnderlying(repository).execute(
            BuildStructuresForUnderlyingCommand(
                structure_policy=policy,
                fee_policy=fee,
                slippage_policy=slippage,
                snapshot=snapshot,
                observations=observations,
                as_of=AS_OF,
                evaluated_at=NOW,
            )
        )
        assert len(outcome.structures) >= 1
        for structure in outcome.structures:
            assert repository.structure_by_id(structure.structure_id) is not None
            assert repository.research_features_by_structure(structure.structure_id) is not None
        assert (
            repository.research_snapshot_by_id(outcome.research_snapshot.research_snapshot_id)
            is not None
        )
        assert (
            repository.latest_research_snapshot_for_underlying(UNDERLYING_ID).research_snapshot_id
            == outcome.research_snapshot.research_snapshot_id
        )

    def test_re_executing_with_identical_evidence_is_a_durable_no_op(self) -> None:
        repository = InMemoryOptionsStructuresRepository()
        policy = build_policies(repository)
        fee = repository.fee_policy_by_id(policy.fee_policy_id)
        slippage = repository.slippage_policy_by_id(policy.slippage_policy_id)
        assert fee is not None and slippage is not None
        snapshot, observations = snapshot_for(UNDERLYING_ID)
        service = BuildStructuresForUnderlying(repository)
        command = BuildStructuresForUnderlyingCommand(
            structure_policy=policy,
            fee_policy=fee,
            slippage_policy=slippage,
            snapshot=snapshot,
            observations=observations,
            as_of=AS_OF,
            evaluated_at=NOW,
        )
        first = service.execute(command)
        second = service.execute(command)
        assert (
            first.research_snapshot.research_snapshot_id
            == second.research_snapshot.research_snapshot_id
        )
        assert {item.structure_id for item in first.structures} == {
            item.structure_id for item in second.structures
        }
        # No duplicate rows accumulated across the two identical runs.
        assert len(repository.structures_for_underlying(UNDERLYING_ID)) == len(first.structures)


class TestBuildStructuresAcrossCohort:
    def test_processes_every_member_and_aggregates_structures(self) -> None:
        repository = InMemoryOptionsStructuresRepository()
        policy = build_policies(repository)
        fee = repository.fee_policy_by_id(policy.fee_policy_id)
        slippage = repository.slippage_policy_by_id(policy.slippage_policy_id)
        assert fee is not None and slippage is not None
        members = tuple(
            snapshot_for(f"security-{symbol}-stable", symbol=symbol)
            for symbol in ("SPY", "QQQ", "AAPL")
        )
        outcome = BuildStructuresAcrossCohort(repository).execute(
            BuildStructuresAcrossCohortCommand(
                structure_policy=policy,
                fee_policy=fee,
                slippage_policy=slippage,
                members=members,
                as_of=AS_OF,
                evaluated_at=NOW,
            )
        )
        assert outcome.underlyings_processed == 3
        assert outcome.underlyings_skipped_by_total_cap == 0
        assert not outcome.total_structures_cap_reached
        assert len(outcome.research_snapshots) == 3
        assert len(outcome.structures) >= 3

    def test_stops_before_the_total_structures_cap_never_partially_truncates_an_underlying(
        self,
    ) -> None:
        repository = InMemoryOptionsStructuresRepository()
        policy = build_policies(
            repository, maximum_structures_per_underlying=1, maximum_total_structures=1
        )
        fee = repository.fee_policy_by_id(policy.fee_policy_id)
        slippage = repository.slippage_policy_by_id(policy.slippage_policy_id)
        assert fee is not None and slippage is not None
        members = tuple(
            snapshot_for(f"security-{symbol}-stable", symbol=symbol)
            for symbol in ("SPY", "QQQ", "AAPL")
        )
        outcome = BuildStructuresAcrossCohort(repository).execute(
            BuildStructuresAcrossCohortCommand(
                structure_policy=policy,
                fee_policy=fee,
                slippage_policy=slippage,
                members=members,
                as_of=AS_OF,
                evaluated_at=NOW,
            )
        )
        assert outcome.total_structures_cap_reached
        assert outcome.underlyings_skipped_by_total_cap >= 1
        assert outcome.underlyings_processed < 3
        # Every persisted underlying's own retained set is fully intact
        # (bounded by maximum_structures_per_underlying, never chopped
        # mid-underlying by the cross-underlying total cap).
        for research_snapshot in outcome.research_snapshots:
            persisted = repository.structures_for_underlying(
                research_snapshot.underlying_canonical_security_id
            )
            assert {item.structure_id for item in persisted} == set(
                research_snapshot.retained_structure_ids
            )


class TestResearchSeamNeverCreatesOpportunities:
    def test_application_module_never_imports_or_constructs_opportunity_machinery(self) -> None:
        import inspect

        import apex.application.options_structures as module

        source = inspect.getsource(module)
        for forbidden in (
            "SharedOpportunity",
            "Opportunity(",
            "PaperOrder",
            "submit_order",
            "OrderProposal",
        ):
            assert forbidden not in source
