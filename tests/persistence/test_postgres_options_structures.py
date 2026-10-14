"""PostgreSQL persistence proofs for migration 0086: the seven genuine
defined-risk options structure economics tables round-trip, replay
idempotently, enforce immutability, and block a destructive downgrade
while evidence is retained -- built strictly on top of the already-
retained genuine quote-chain evidence from migration 0085.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.options_quote_intelligence_postgres import (
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.adapters.options_structures_postgres import (
    OPTIONS_STRUCTURES_TABLES,
    PostgresOptionsStructuresRepository,
)
from apex.application.options_structures import (
    BuildStructuresForUnderlying,
    BuildStructuresForUnderlyingCommand,
    CreateOptionsFeePolicy,
    CreateOptionsSlippagePolicy,
    CreateOptionsStructureResearchPolicy,
    CreateStructurePricingPolicy,
)
from apex.domain.options_intelligence import (
    ExerciseStyle,
    OptionContract,
    OptionType,
    SettlementStyle,
)
from apex.domain.options_quote_intelligence import (
    DEFAULT_OPTIONS_COHORT_SYMBOLS,
    CaptureRunStatus,
    OptionChainCaptureBounds,
    OptionChainCapturePlan,
    OptionChainCaptureRun,
    OptionQuoteObservation,
    create_options_chain_snapshot,
)
from apex.domain.options_structures import (
    OptionsFeePolicy,
    OptionsSlippagePolicy,
    OptionsStructureResearchPolicy,
    StructurePricingPolicy,
    StructureResearchFeatures,
    StructureType,
)
from apex.domain.research_cohort import CohortRole, CohortSector, ResolvedCohortMember

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
HEAD_REVISION = "0118_evidence_gap_recovery"
NOW = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
UNDERLYING = "security-spy-stable"


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _plan() -> OptionChainCapturePlan:
    # OptionChainCapturePlan.create requires a 10-25 member cohort; SPY
    # (this file's UNDERLYING) must resolve to the exact stable id every
    # other fixture here already assumes, so it is declared explicitly and
    # the remaining declared cohort symbols pad out the required range.
    members = (
        ResolvedCohortMember(
            stable_security_id=UNDERLYING,
            symbol="SPY",
            sector=CohortSector.BROAD_MARKET,
            roles=(CohortRole.BROAD_INDEX_ETF,),
            asset_class="ETF",
        ),
        *(
            ResolvedCohortMember(
                stable_security_id=f"security-{symbol.lower()}-stable",
                symbol=symbol,
                sector=CohortSector.BROAD_MARKET,
                roles=(CohortRole.BROAD_INDEX_ETF,),
                asset_class="ETF",
            )
            for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS
            if symbol != "SPY"
        ),
    )
    return OptionChainCapturePlan.create(
        cohort_policy_id="research_cohort_policy_test",
        cohort_policy_hash="0" * 64,
        members=members,
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        created_at=NOW,
    )


def _run(plan_id: str) -> OptionChainCaptureRun:
    return OptionChainCaptureRun.create(
        plan_id=plan_id,
        requested_at=NOW,
        completed_at=NOW,
        status=CaptureRunStatus.COMPLETE,
        underlyings_requested=1,
        underlyings_captured=1,
        provider_request_count=2,
        payload_bytes=1000,
        contracts_considered=2,
        contracts_quoted=2,
        snapshot_ids=(),
        reason=None,
    )


def _contract(strike: str, option_type: OptionType) -> OptionContract:
    return OptionContract.create(
        provider_contract_id=f"SPY20260821{option_type.value[0]}{strike}",
        underlying_canonical_security_id=UNDERLYING,
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


def _observation(
    strike: str, option_type: OptionType, bid: str, ask: str
) -> OptionQuoteObservation:
    return OptionQuoteObservation.create(
        contract=_contract(strike, option_type),
        expected_underlying_canonical_security_id=UNDERLYING,
        bid=bid,
        ask=ask,
        bid_size="10",
        ask_size="10",
        last_trade=str((Decimal(bid) + Decimal(ask)) / 2),
        trade_size="5",
        underlying_reference_price="500",
        volume="100",
        open_interest=None,
        provider_implied_volatility=None,
        provider_delta=None,
        provider_gamma=None,
        provider_theta=None,
        provider_vega=None,
        provider_rho=None,
        market_status="OPEN",
        quote_timestamp=NOW,
        availability_timestamp=NOW,
        ingestion_timestamp=NOW,
        captured_at=NOW,
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )


def _observations() -> tuple[OptionQuoteObservation, ...]:
    return (
        _observation("495", OptionType.CALL, "9.00", "9.20"),
        _observation("500", OptionType.CALL, "6.00", "6.20"),
    )


def _seed_chain(quote_repository: PostgresOptionsQuoteIntelligenceRepository):
    """Persist a genuine, FK-satisfying quote-chain snapshot -- the exact
    evidence a defined-risk structure candidate must bind to."""

    plan = quote_repository.save_plan(_plan())
    run = quote_repository.save_run(_run(plan.plan_id))
    observations = _observations()
    quote_repository.save_observations(observations)
    snapshot = create_options_chain_snapshot(
        plan_id=plan.plan_id,
        run_id=run.run_id,
        underlying_canonical_security_id=UNDERLYING,
        underlying_symbol="SPY",
        underlying_reference_price=Decimal("500"),
        underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
        captured_at=NOW,
        observations=observations,
        as_of=date(2026, 8, 10),
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )
    quote_repository.save_snapshot(snapshot)
    return snapshot, observations


def _seed_policies(structures_repository: PostgresOptionsStructuresRepository):
    pricing = CreateStructurePricingPolicy(structures_repository).execute(
        policy=StructurePricingPolicy.create()
    )
    fee = CreateOptionsFeePolicy(structures_repository).execute(policy=OptionsFeePolicy.create())
    slippage = CreateOptionsSlippagePolicy(structures_repository).execute(
        policy=OptionsSlippagePolicy.create()
    )
    structure_policy = CreateOptionsStructureResearchPolicy(structures_repository).execute(
        policy=OptionsStructureResearchPolicy.create(
            pricing_policy=pricing, fee_policy=fee, slippage_policy=slippage, created_at=NOW
        )
    )
    return pricing, fee, slippage, structure_policy


def _seed_everything(postgres_engine: Engine):
    quote_repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    structures_repository = PostgresOptionsStructuresRepository(postgres_engine)
    snapshot, observations = _seed_chain(quote_repository)
    _pricing, fee, slippage, structure_policy = _seed_policies(structures_repository)
    outcome = BuildStructuresForUnderlying(structures_repository).execute(
        BuildStructuresForUnderlyingCommand(
            structure_policy=structure_policy,
            fee_policy=fee,
            slippage_policy=slippage,
            snapshot=snapshot,
            observations=observations,
            as_of=NOW.date(),
            evaluated_at=NOW,
        )
    )
    assert outcome.structures, "fixture must genuinely produce at least one structure"
    return structures_repository, structure_policy, outcome


def test_clean_database_is_at_current_head_with_options_structures_tables(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(OPTIONS_STRUCTURES_TABLES)
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )


def test_fee_policy_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresOptionsStructuresRepository(postgres_engine)
    policy = OptionsFeePolicy.create()
    repository.save_fee_policy(policy)
    repository.save_fee_policy(policy)  # idempotent replay, never a conflict
    fresh = PostgresOptionsStructuresRepository(postgres_engine)
    assert fresh.fee_policy_by_id(policy.policy_id) == policy
    assert fresh.latest_fee_policy() == policy


def test_slippage_policy_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresOptionsStructuresRepository(postgres_engine)
    policy = OptionsSlippagePolicy.create()
    repository.save_slippage_policy(policy)
    repository.save_slippage_policy(policy)
    fresh = PostgresOptionsStructuresRepository(postgres_engine)
    assert fresh.slippage_policy_by_id(policy.policy_id) == policy
    assert fresh.latest_slippage_policy() == policy


def test_pricing_policy_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresOptionsStructuresRepository(postgres_engine)
    policy = StructurePricingPolicy.create()
    repository.save_pricing_policy(policy)
    repository.save_pricing_policy(policy)
    fresh = PostgresOptionsStructuresRepository(postgres_engine)
    assert fresh.pricing_policy_by_id(policy.policy_id) == policy
    assert fresh.latest_pricing_policy() == policy


def test_structure_policy_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresOptionsStructuresRepository(postgres_engine)
    _pricing, _fee, _slippage, policy = _seed_policies(repository)
    fresh = PostgresOptionsStructuresRepository(postgres_engine)
    assert fresh.structure_policy_by_id(policy.policy_id) == policy
    assert fresh.latest_structure_policy() == policy


def test_structures_round_trip_bulk_and_are_idempotent(postgres_engine: Engine) -> None:
    repository, _policy, outcome = _seed_everything(postgres_engine)
    repository.save_structures(outcome.structures)  # idempotent replay
    fresh = PostgresOptionsStructuresRepository(postgres_engine)
    for structure in outcome.structures:
        assert fresh.structure_by_id(structure.structure_id) == structure
    assert set(fresh.structures_for_underlying(UNDERLYING)) == set(outcome.structures)


def test_structures_by_ids_bulk_fetch_matches_individual_lookups_and_skips_missing(
    postgres_engine: Engine,
) -> None:
    _repository, _policy, outcome = _seed_everything(postgres_engine)
    fresh = PostgresOptionsStructuresRepository(postgres_engine)
    requested = (outcome.structures[0].structure_id, "nonexistent-structure-id")
    fetched = fresh.structures_by_ids(requested)
    assert {item.structure_id for item in fetched} == {outcome.structures[0].structure_id}
    assert fresh.structures_by_ids(()) == ()


def test_research_snapshot_and_features_round_trip_and_are_idempotent(
    postgres_engine: Engine,
) -> None:
    _repository, _policy, outcome = _seed_everything(postgres_engine)
    fresh = PostgresOptionsStructuresRepository(postgres_engine)
    research_snapshot = fresh.research_snapshot_by_id(
        outcome.research_snapshot.research_snapshot_id
    )
    assert research_snapshot == outcome.research_snapshot
    assert fresh.latest_research_snapshot_for_underlying(UNDERLYING) == outcome.research_snapshot
    assert fresh.research_snapshots_for_underlying(UNDERLYING) == (outcome.research_snapshot,)
    assert fresh.latest_research_snapshot_for_underlying("security-nonexistent") is None
    for structure in outcome.structures:
        features = fresh.research_features_by_structure(structure.structure_id)
        assert features is not None
        assert features == StructureResearchFeatures.from_candidate(structure)


def test_structure_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    repository, _policy, outcome = _seed_everything(postgres_engine)
    del repository
    structure = outcome.structures[0]
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE option_structure_candidate SET structure_type = 'PUT_DEBIT_VERTICAL' "
                "WHERE structure_id = :id"
            ),
            {"id": structure.structure_id},
        )


def test_research_snapshot_immutable_trigger_rejects_delete(postgres_engine: Engine) -> None:
    repository, _policy, outcome = _seed_everything(postgres_engine)
    del repository
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "DELETE FROM options_structure_research_snapshot WHERE research_snapshot_id = :id"
            ),
            {"id": outcome.research_snapshot.research_snapshot_id},
        )


def test_fee_policy_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    repository = PostgresOptionsStructuresRepository(postgres_engine)
    policy = OptionsFeePolicy.create()
    repository.save_fee_policy(policy)
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text("UPDATE options_fee_policy SET version = 'TAMPERED' WHERE policy_id = :id"),
            {"id": policy.policy_id},
        )


# --------------------------------------------------------------------------
# Structure economics sanity, replayed through genuine persisted evidence
# --------------------------------------------------------------------------


def test_persisted_structure_economics_are_sane(postgres_engine: Engine) -> None:
    _repository, _policy, outcome = _seed_everything(postgres_engine)
    for structure in outcome.structures:
        economics = structure.economics
        assert economics.maximum_loss > 0
        assert economics.maximum_profit > 0
        assert economics.capital_required > 0
        # Exact-formula reproduction is already covered exhaustively at the
        # domain layer (test_options_structures.py); here just confirm the
        # persisted ratio is directionally consistent with profit/loss
        # (rounding means it will not equal an unrounded re-division).
        implied_ratio = economics.maximum_profit / economics.maximum_loss
        assert abs(economics.reward_risk_ratio - implied_ratio) < Decimal("0.0001")
        if structure.structure_type in (
            StructureType.CALL_DEBIT_VERTICAL,
            StructureType.PUT_DEBIT_VERTICAL,
        ):
            # Debit: net_amount (post-cost total capital paid) equals
            # capital_required and maximum_loss; gross_amount (pre-cost
            # debit) is strictly smaller once fees/slippage are added.
            assert economics.net_amount == economics.capital_required == economics.maximum_loss
            assert economics.gross_amount < economics.net_amount
        else:
            assert economics.net_amount == economics.maximum_profit
        assert structure.authority == "NONE"


# --------------------------------------------------------------------------
# Migration downgrade protection
# --------------------------------------------------------------------------


def test_retained_evidence_blocks_destructive_0086_downgrade(postgres_engine: Engine) -> None:
    repository = PostgresOptionsStructuresRepository(postgres_engine)
    repository.save_fee_policy(OptionsFeePolicy.create())
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0086 with retained defined-risk options structure evidence",
        ):
            command.downgrade(_config(connection), "0085_options_quote_intelligence")
        connection.rollback()
    # Migration 0087 now sits on top of this one and is empty in this
    # test's scenario (nothing here ever touches its table), so alembic's
    # multi-revision downgrade genuinely completes 0087's own downgrade
    # step (no retained evidence, no guard trip) *before* reaching 0086's
    # guard, which then blocks further progress -- each migration's own
    # downgrade step commits independently, so the rollback above undoes
    # only the failed 0086 step, not the already-completed 0087 one. The
    # database is therefore genuinely left at 0086, not at HEAD_REVISION;
    # asserting that explicitly (rather than "unchanged") is what actually
    # happened, confirmed against the real database -- mirroring exactly
    # how ``test_postgres_options_quote_intelligence.py``'s identical 0085
    # downgrade-protection test already documents the same mechanic.
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0086_defined_risk_options_eco"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0086_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(OPTIONS_STRUCTURES_TABLES)
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0085_options_quote_intelligence")
    assert set(OPTIONS_STRUCTURES_TABLES).isdisjoint(sa.inspect(postgres_engine).get_table_names())
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(OPTIONS_STRUCTURES_TABLES)


# --------------------------------------------------------------------------
# Coinbase/Kalshi independence
# --------------------------------------------------------------------------


def test_options_structures_never_touches_crypto_or_kalshi_tables(postgres_engine: Engine) -> None:
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in ("apex_crypto_paper_account", "apex_kalshi_paper_account")
            if table in sa.inspect(postgres_engine).get_table_names()
        }
    _repository, _policy, _outcome = _seed_everything(postgres_engine)
    with postgres_engine.connect() as connection:
        after = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) for table in before
        }
    assert before == after
