"""PostgreSQL persistence proofs for migration 0085: the six genuine
options quote-chain and volatility intelligence tables round-trip, replay
idempotently, enforce immutability, and block a destructive downgrade
while evidence is retained.
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
    OPTIONS_QUOTE_INTELLIGENCE_TABLES,
    PostgresOptionsQuoteIntelligenceRepository,
)
from apex.domain.options_intelligence import (
    CapabilityState,
    ExerciseStyle,
    OptionContract,
    OptionProviderCapabilities,
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
    OptionsChainSnapshot,
    OptionsQuoteProviderCapabilitySnapshot,
    OptionsVolatilityResearchFeatures,
    create_options_chain_snapshot,
)
from apex.domain.research_cohort import CohortRole, CohortSector, ResolvedCohortMember

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
# Bumped by migration 0086 (Defined-Risk Options Structure Economics and
# Research Integration v1), which revises this one -- the alembic head
# moves forward, this migration's own tables/behavior do not change.
HEAD_REVISION = "0118_evidence_gap_recovery"
NOW = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)
UNDERLYING = "security-spy-stable"


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _members() -> tuple[ResolvedCohortMember, ...]:
    return tuple(
        sorted(
            (
                ResolvedCohortMember(
                    stable_security_id=f"stable-{symbol.lower()}",
                    symbol=symbol,
                    sector=CohortSector.BROAD_MARKET,
                    roles=(CohortRole.BROAD_INDEX_ETF,),
                    asset_class="ETF",
                )
                for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS
            ),
            key=lambda item: item.symbol,
        )
    )


def _plan() -> OptionChainCapturePlan:
    return OptionChainCapturePlan.create(
        cohort_policy_id="research_cohort_policy_test",
        cohort_policy_hash="0" * 64,
        members=_members(),
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        created_at=NOW,
    )


def _capability() -> OptionsQuoteProviderCapabilitySnapshot:
    capability = OptionProviderCapabilities.create(
        provider="ALPACA_OPTIONS_MARKET_DATA",
        contract_discovery=CapabilityState.UNKNOWN,
        quotes=CapabilityState.UNKNOWN,
        trades=CapabilityState.UNKNOWN,
        snapshots=CapabilityState.UNKNOWN,
        implied_volatility=CapabilityState.UNKNOWN,
        greeks=CapabilityState.UNKNOWN,
        open_interest=CapabilityState.UNKNOWN,
        volume=CapabilityState.UNKNOWN,
        limitations=("ALPACA_CREDENTIALS_ABSENT_ZERO_REQUESTS_MADE",),
    )
    return OptionsQuoteProviderCapabilitySnapshot.create(
        checked_at=NOW,
        credentials_available=False,
        requests_made=0,
        capability=capability,
        unavailable_reason="ALPACA_CREDENTIALS_ABSENT",
    )


def _run(plan_id: str) -> OptionChainCaptureRun:
    return OptionChainCaptureRun.create(
        plan_id=plan_id,
        requested_at=NOW,
        completed_at=NOW,
        status=CaptureRunStatus.BLOCKED_CREDENTIALS_ABSENT,
        underlyings_requested=19,
        underlyings_captured=0,
        provider_request_count=0,
        payload_bytes=0,
        contracts_considered=0,
        contracts_quoted=0,
        snapshot_ids=(),
        reason="ALPACA_CREDENTIALS_ABSENT",
    )


def _contract(strike: str, option_type: OptionType) -> OptionContract:
    return OptionContract.create(
        provider_contract_id=f"SPY20260918{option_type.value[0]}{strike}",
        underlying_canonical_security_id=UNDERLYING,
        standardized_symbol=None,
        option_type=option_type,
        strike=strike,
        expiration="2026-09-18",
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
        _observation("480", OptionType.PUT, "1.10", "1.30"),
        _observation("500", OptionType.PUT, "8.00", "8.20"),
        _observation("500", OptionType.CALL, "9.00", "9.20"),
        _observation("520", OptionType.CALL, "1.50", "1.70"),
    )


def _snapshot(plan_id: str, run_id: str) -> OptionsChainSnapshot:
    return create_options_chain_snapshot(
        plan_id=plan_id,
        run_id=run_id,
        underlying_canonical_security_id=UNDERLYING,
        underlying_symbol="SPY",
        underlying_reference_price=Decimal("500"),
        underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
        captured_at=NOW,
        observations=_observations(),
        as_of=date(2026, 8, 8),
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )


def test_clean_database_is_at_current_head_with_options_quote_intelligence_tables(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(
        OPTIONS_QUOTE_INTELLIGENCE_TABLES
    )
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )


def test_capability_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    capability = _capability()
    repository.save_capability(capability)
    repository.save_capability(capability)  # idempotent replay, never a conflict
    fresh = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    assert fresh.latest_capability("ALPACA_OPTIONS_MARKET_DATA") == capability
    assert fresh.capabilities() == (capability,)
    assert fresh.capabilities(provider="SOME_OTHER_PROVIDER") == ()


def test_plan_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = _plan()
    repository.save_plan(plan)
    repository.save_plan(plan)
    fresh = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    assert fresh.plan_by_id(plan.plan_id) == plan
    assert fresh.latest_plan() == plan
    assert fresh.plans() == (plan,)


def test_run_round_trips_and_is_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = _plan()
    repository.save_plan(plan)
    run = _run(plan.plan_id)
    repository.save_run(run)
    repository.save_run(run)
    fresh = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    assert fresh.run_by_id(run.run_id) == run
    assert fresh.runs_for_plan(plan.plan_id) == (run,)


def test_observations_round_trip_bulk_and_are_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    observations = _observations()
    repository.save_observations(observations)
    repository.save_observations(observations)  # idempotent replay
    fresh = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    for observation in observations:
        assert fresh.observation_by_id(observation.observation_id) == observation


def test_observations_by_ids_bulk_fetch_matches_individual_lookups_and_skips_missing(
    postgres_engine: Engine,
) -> None:
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    observations = _observations()
    repository.save_observations(observations)
    fresh = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    requested_ids = (
        observations[0].observation_id,
        "nonexistent-observation-id",
        observations[2].observation_id,
    )
    fetched = fresh.observations_by_ids(requested_ids)
    assert {item.observation_id for item in fetched} == {
        observations[0].observation_id,
        observations[2].observation_id,
    }
    assert fresh.observations_by_ids(()) == ()


def test_snapshot_and_research_feature_round_trip_and_are_idempotent(
    postgres_engine: Engine,
) -> None:
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = _plan()
    repository.save_plan(plan)
    run = _run(plan.plan_id)
    repository.save_run(run)
    repository.save_observations(_observations())
    snapshot = _snapshot(plan.plan_id, run.run_id)
    repository.save_snapshot(snapshot)
    repository.save_snapshot(snapshot)  # idempotent replay
    features = OptionsVolatilityResearchFeatures.from_snapshot(snapshot)
    repository.save_research_features(features)
    repository.save_research_features(features)

    fresh = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    assert fresh.snapshot_by_id(snapshot.snapshot_id) == snapshot
    assert fresh.snapshots_for_underlying(UNDERLYING) == (snapshot,)
    assert fresh.latest_snapshot_for_underlying(UNDERLYING) == snapshot
    assert fresh.latest_snapshot_for_underlying("security-nonexistent") is None
    assert fresh.research_features_by_snapshot(snapshot.snapshot_id) == features
    assert fresh.research_features_for_underlying(UNDERLYING) == (features,)


def test_snapshot_immutable_trigger_rejects_update(postgres_engine: Engine) -> None:
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    plan = _plan()
    repository.save_plan(plan)
    run = _run(plan.plan_id)
    repository.save_run(run)
    snapshot = _snapshot(plan.plan_id, run.run_id)
    repository.save_snapshot(snapshot)
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text(
                "UPDATE options_chain_snapshot SET data_quality_status = 'EMPTY' "
                "WHERE snapshot_id = :id"
            ),
            {"id": snapshot.snapshot_id},
        )


def test_observation_immutable_trigger_rejects_delete(postgres_engine: Engine) -> None:
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    observation = _observations()[0]
    repository.save_observations((observation,))
    with postgres_engine.begin() as connection, pytest.raises(Exception, match="immutable"):
        connection.execute(
            sa.text("DELETE FROM options_quote_observation WHERE observation_id = :id"),
            {"id": observation.observation_id},
        )


# --------------------------------------------------------------------------
# Migration downgrade protection
# --------------------------------------------------------------------------


def test_retained_evidence_blocks_destructive_0085_downgrade(postgres_engine: Engine) -> None:
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    repository.save_capability(_capability())
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0085 with retained options quote intelligence evidence",
        ):
            command.downgrade(_config(connection), "0084_research_replication")
        connection.rollback()
    # Migration 0086 now sits on top of this one and is empty in this
    # test's scenario (nothing here ever touches its tables), so alembic's
    # multi-revision downgrade genuinely completes 0086's own downgrade
    # step (no retained evidence, no guard trip) *before* reaching 0085's
    # guard, which then blocks further progress -- each migration's own
    # downgrade step commits independently, so the rollback above undoes
    # only the failed 0085 step, not the already-completed 0086 one. The
    # database is therefore genuinely left at 0085, not at HEAD_REVISION;
    # asserting that explicitly (rather than "unchanged") is what actually
    # happened, confirmed against the real database.
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0085_options_quote_intelligence"
        )
        assert set(OPTIONS_QUOTE_INTELLIGENCE_TABLES) <= set(
            sa.inspect(connection).get_table_names()
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
        )


def test_0085_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(
        OPTIONS_QUOTE_INTELLIGENCE_TABLES
    )
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0084_research_replication")
    assert set(OPTIONS_QUOTE_INTELLIGENCE_TABLES).isdisjoint(
        sa.inspect(postgres_engine).get_table_names()
    )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= set(
        OPTIONS_QUOTE_INTELLIGENCE_TABLES
    )


# --------------------------------------------------------------------------
# Coinbase/Kalshi independence
# --------------------------------------------------------------------------


def test_options_quote_intelligence_never_touches_crypto_or_kalshi_tables(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        before = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"'))
            for table in ("apex_crypto_paper_account", "apex_kalshi_paper_account")
            if table in sa.inspect(postgres_engine).get_table_names()
        }
    repository = PostgresOptionsQuoteIntelligenceRepository(postgres_engine)
    repository.save_capability(_capability())
    plan = _plan()
    repository.save_plan(plan)
    repository.save_run(_run(plan.plan_id))
    with postgres_engine.connect() as connection:
        after = {
            table: connection.scalar(sa.text(f'SELECT count(*) FROM "{table}"')) for table in before
        }
    assert before == after
