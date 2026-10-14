"""PostgreSQL persistence proofs for Investment Mandate v1: round trip,
idempotent replay, versioned lineage, and -- the central correctness
property this milestone requires -- genuine point-in-time replay: an
Opportunity evaluated at a past instant uses the mandate version and
portfolio snapshot that were actually in effect then, never today's,
unless the caller explicitly asks to re-evaluate under today's truth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.investment_mandate_postgres import PostgresInvestmentMandateRepository
from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.application.investment_mandate import (
    CreateInvestmentMandate,
    EvaluateOpportunityAgainstMandate,
)
from apex.domain.investment_mandate import InvestmentMandateError, MandateDecisionState
from apex.domain.portfolio_state import build_portfolio_snapshot
from apex.domain.shared_opportunity import (
    SharedOpportunityProjection,
    SharedVenue,
    build_opportunity_set,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _seed_opportunity(
    engine: Engine, *, projection_id: str, generated_at: datetime
) -> SharedOpportunityProjection:
    opportunity = SharedOpportunityProjection.create(
        projection_id=projection_id,
        venue=SharedVenue.CRYPTO,
        source_record_type="Test",
        source_record_id="src-1",
        source_canonical_hash="d" * 64,
        strategy_or_method_id="TEST_METHOD",
        instrument_id="BTC-USD",
        product_or_market_id="BTC-USD",
        direction="HYPOTHETICAL_LONG_SPOT_RESEARCH",
        generated_at=generated_at,
        valid_until=generated_at + timedelta(days=30),
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=60,
        freshness_limit_seconds=3600,
        expected_gross_return="0.02",
        expected_net_return="0.01",
        comparable_value="0.4",
        comparable_value_identity=None,
        uncertainty="0.3",
        maximum_loss="50",
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="200",
        source_maximum_allocation_fraction="0.5",
        expected_holding_days=5,
        estimated_lockup_days=5,
        supporting_evidence_ids=("ev-1",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(),
    )
    opportunity_set, decisions, priorities, explanations = build_opportunity_set(
        opportunity_set_id=f"{projection_id}-set",
        created_at=generated_at,
        projections=(opportunity,),
    )
    PostgresSharedOpportunityRepository(engine).save_opportunity_bundle(
        idempotency_key=f"{projection_id}-bundle",
        opportunity_set=opportunity_set,
        projections=(opportunity,),
        decisions=decisions,
        priorities=priorities,
        explanations=explanations,
    )
    return opportunity


def _seed_snapshot(engine: Engine, *, snapshot_id: str, as_of: datetime):
    snapshot = build_portfolio_snapshot(
        snapshot_id=snapshot_id,
        as_of=as_of,
        created_at=as_of,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    PostgresPortfolioSnapshotRepository(engine).save_portfolio_snapshot(
        idempotency_key=f"{snapshot_id}-key", snapshot=snapshot, balances=(), positions=()
    )
    return snapshot


def test_mandate_round_trips_and_versions_increment(postgres_engine: Engine) -> None:
    repository = PostgresInvestmentMandateRepository(postgres_engine)
    create = CreateInvestmentMandate(repository)
    v1 = create.execute(
        idempotency_key="mandate-v1",
        lineage_id="lineage-a",
        effective_at=NOW - timedelta(days=2),
        created_at=NOW - timedelta(days=2),
        total_capital_allocated="1000",
        permitted_venues=("COINBASE",),
        permitted_market_families=("CRYPTO_SPOT",),
        maximum_aggregate_exposure_fraction="0.5",
        maximum_per_instrument_fraction="0.5",
        maximum_venue_concentration_fraction="0.5",
        maximum_market_family_concentration_fraction="0.5",
        maximum_capital_lockup_days=30,
    )
    assert v1.mandate.version == 1
    assert v1.mandate.previous_mandate_id is None
    v2 = create.execute(
        idempotency_key="mandate-v2",
        lineage_id="lineage-a",
        effective_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=1),
        total_capital_allocated="2000",
        permitted_venues=("COINBASE",),
        permitted_market_families=("CRYPTO_SPOT",),
        maximum_aggregate_exposure_fraction="0.5",
        maximum_per_instrument_fraction="0.5",
        maximum_venue_concentration_fraction="0.5",
        maximum_market_family_concentration_fraction="0.5",
        maximum_capital_lockup_days=30,
    )
    assert v2.mandate.version == 2
    assert v2.mandate.previous_mandate_id == v1.mandate.mandate_id

    fetched = repository.mandate(v1.mandate.mandate_id)
    assert fetched is not None
    assert fetched.canonical_hash == v1.mandate.canonical_hash
    assert repository.latest_mandate_version("lineage-a").mandate_id == v2.mandate.mandate_id


def test_mandate_save_is_idempotent_and_rejects_lineage_version_reuse(
    postgres_engine: Engine,
) -> None:
    repository = PostgresInvestmentMandateRepository(postgres_engine)
    create = CreateInvestmentMandate(repository)
    kwargs = dict(
        lineage_id="lineage-b",
        effective_at=NOW,
        created_at=NOW,
        total_capital_allocated="500",
        permitted_venues=("KALSHI",),
        permitted_market_families=("KALSHI_EVENT_CONTRACT",),
        maximum_aggregate_exposure_fraction="0.3",
        maximum_per_instrument_fraction="0.3",
        maximum_venue_concentration_fraction="0.3",
        maximum_market_family_concentration_fraction="0.3",
        maximum_capital_lockup_days=10,
    )
    first = create.execute(idempotency_key="idem-b", **kwargs)
    assert first.idempotent_replay is False
    replay = repository.save_mandate(mandate=first.mandate, idempotency_key="idem-b")
    assert replay is True
    assert _row_count(postgres_engine, "investment_mandate") == 1

    # A mandate at the exact same (lineage_id, version) but different
    # content and a different idempotency key must be rejected, not silently
    # accepted or silently overwritten.
    from apex.domain.investment_mandate import InvestmentMandate

    conflicting = InvestmentMandate.create(
        lineage_id="lineage-b",
        version=1,
        previous_mandate_id=None,
        effective_at=NOW,
        created_at=NOW,
        total_capital_allocated="999",
        permitted_venues=(),
        permitted_market_families=(),
        disallowed_market_families=(),
        maximum_aggregate_exposure_fraction="0",
        maximum_per_instrument_fraction="0",
        maximum_venue_concentration_fraction="0",
        maximum_market_family_concentration_fraction="0",
        maximum_capital_lockup_days=None,
    )
    with pytest.raises(InvestmentMandateError):
        repository.save_mandate(mandate=conflicting, idempotency_key="idem-b-conflict")


def test_active_mandate_selects_the_version_in_effect_at_the_given_time(
    postgres_engine: Engine,
) -> None:
    repository = PostgresInvestmentMandateRepository(postgres_engine)
    create = CreateInvestmentMandate(repository)
    t1 = NOW - timedelta(days=10)
    t2 = NOW - timedelta(days=5)
    v1 = create.execute(
        idempotency_key="active-v1",
        lineage_id="lineage-c",
        effective_at=t1,
        created_at=t1,
        total_capital_allocated="100",
        permitted_venues=(),
        permitted_market_families=(),
        maximum_aggregate_exposure_fraction="0",
        maximum_per_instrument_fraction="0",
        maximum_venue_concentration_fraction="0",
        maximum_market_family_concentration_fraction="0",
        maximum_capital_lockup_days=None,
    ).mandate
    v2 = create.execute(
        idempotency_key="active-v2",
        lineage_id="lineage-c",
        effective_at=t2,
        created_at=t2,
        total_capital_allocated="200",
        permitted_venues=(),
        permitted_market_families=(),
        maximum_aggregate_exposure_fraction="0",
        maximum_per_instrument_fraction="0",
        maximum_venue_concentration_fraction="0",
        maximum_market_family_concentration_fraction="0",
        maximum_capital_lockup_days=None,
    ).mandate

    assert repository.active_mandate("lineage-c", as_of=t1).mandate_id == v1.mandate_id
    assert (
        repository.active_mandate("lineage-c", as_of=t1 + timedelta(hours=1)).mandate_id
        == v1.mandate_id
    )
    assert repository.active_mandate("lineage-c", as_of=t2).mandate_id == v2.mandate_id
    assert repository.active_mandate("lineage-c", as_of=NOW).mandate_id == v2.mandate_id
    assert repository.active_mandate("lineage-c", as_of=t1 - timedelta(days=1)) is None


def test_historical_evaluation_uses_the_mandate_and_snapshot_active_at_that_time(
    postgres_engine: Engine,
) -> None:
    """The central point-in-time replay proof: an Opportunity evaluated at
    a past instant must use the mandate version and portfolio snapshot that
    governed then -- never today's -- unless the caller explicitly passes
    today's timestamp."""

    repository = PostgresInvestmentMandateRepository(postgres_engine)
    create = CreateInvestmentMandate(repository)
    evaluate = EvaluateOpportunityAgainstMandate(repository)

    early = NOW - timedelta(days=10)
    late = NOW - timedelta(days=1)

    restrictive_v1 = create.execute(
        idempotency_key="replay-v1",
        lineage_id="lineage-replay",
        effective_at=early,
        created_at=early,
        total_capital_allocated="1000",
        permitted_venues=(),
        permitted_market_families=(),
        maximum_aggregate_exposure_fraction="0",
        maximum_per_instrument_fraction="0",
        maximum_venue_concentration_fraction="0",
        maximum_market_family_concentration_fraction="0",
        maximum_capital_lockup_days=None,
    ).mandate
    permissive_v2 = create.execute(
        idempotency_key="replay-v2",
        lineage_id="lineage-replay",
        effective_at=late,
        created_at=late,
        total_capital_allocated="1000",
        permitted_venues=("COINBASE",),
        permitted_market_families=("CRYPTO_SPOT",),
        maximum_aggregate_exposure_fraction="0.9",
        maximum_per_instrument_fraction="0.9",
        maximum_venue_concentration_fraction="0.9",
        maximum_market_family_concentration_fraction="0.9",
        maximum_capital_lockup_days=30,
    ).mandate

    early_snapshot = _seed_snapshot(postgres_engine, snapshot_id="replay-snap-early", as_of=early)
    late_snapshot = _seed_snapshot(postgres_engine, snapshot_id="replay-snap-late", as_of=late)

    opportunity = _seed_opportunity(
        postgres_engine, projection_id="replay-proj", generated_at=early
    )

    # Evaluated as of the early instant: must use v1 (restrictive) and the
    # early snapshot, never the later, more permissive mandate/snapshot.
    early_result = evaluate.execute(
        idempotency_key="replay-eval-early",
        opportunity_id=opportunity.projection_id,
        evaluated_at=early + timedelta(hours=1),
        mandate_lineage_id="lineage-replay",
    )
    assert early_result.evaluation.mandate_id == restrictive_v1.mandate_id
    assert early_result.evaluation.mandate_version == 1
    assert early_result.evaluation.portfolio_snapshot_id == early_snapshot.snapshot_id
    assert early_result.evaluation.state is MandateDecisionState.REJECTED

    # Evaluated as of "now": must use v2 (permissive, effective `late`) and
    # the late snapshot.
    now_result = evaluate.execute(
        idempotency_key="replay-eval-now",
        opportunity_id=opportunity.projection_id,
        evaluated_at=NOW,
        mandate_lineage_id="lineage-replay",
    )
    assert now_result.evaluation.mandate_id == permissive_v2.mandate_id
    assert now_result.evaluation.mandate_version == 2
    assert now_result.evaluation.portfolio_snapshot_id == late_snapshot.snapshot_id

    # Re-running the *early* evaluation again must reproduce the identical
    # historical result -- replay never drifts toward today's mandate.
    early_replay = evaluate.execute(
        idempotency_key="replay-eval-early",
        opportunity_id=opportunity.projection_id,
        evaluated_at=early + timedelta(hours=1),
        mandate_lineage_id="lineage-replay",
    )
    assert early_replay.idempotent_replay is True
    assert early_replay.evaluation.canonical_hash == early_result.evaluation.canonical_hash
    assert early_replay.evaluation.mandate_version == 1


def test_evaluate_raises_when_lineage_has_no_active_mandate(postgres_engine: Engine) -> None:
    repository = PostgresInvestmentMandateRepository(postgres_engine)
    opportunity = _seed_opportunity(
        postgres_engine, projection_id="orphan-proj", generated_at=NOW - timedelta(hours=1)
    )
    evaluate = EvaluateOpportunityAgainstMandate(repository)
    with pytest.raises(InvestmentMandateError):
        evaluate.execute(
            idempotency_key="orphan-eval",
            opportunity_id=opportunity.projection_id,
            evaluated_at=NOW,
            mandate_lineage_id="lineage-with-no-mandate",
        )


def test_evaluate_raises_when_no_portfolio_snapshot_exists_yet(postgres_engine: Engine) -> None:
    repository = PostgresInvestmentMandateRepository(postgres_engine)
    create = CreateInvestmentMandate(repository)
    create.execute(
        idempotency_key="no-snap-mandate",
        lineage_id="lineage-no-snapshot",
        effective_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=1),
        total_capital_allocated="100",
        permitted_venues=(),
        permitted_market_families=(),
        maximum_aggregate_exposure_fraction="0",
        maximum_per_instrument_fraction="0",
        maximum_venue_concentration_fraction="0",
        maximum_market_family_concentration_fraction="0",
        maximum_capital_lockup_days=None,
    )
    opportunity = _seed_opportunity(
        postgres_engine, projection_id="no-snap-proj", generated_at=NOW - timedelta(hours=1)
    )
    evaluate = EvaluateOpportunityAgainstMandate(repository)
    with pytest.raises(InvestmentMandateError):
        evaluate.execute(
            idempotency_key="no-snap-eval",
            opportunity_id=opportunity.projection_id,
            evaluated_at=NOW,
            mandate_lineage_id="lineage-no-snapshot",
        )


def test_explicit_portfolio_snapshot_override_still_rejects_a_future_snapshot(
    postgres_engine: Engine,
) -> None:
    repository = PostgresInvestmentMandateRepository(postgres_engine)
    create = CreateInvestmentMandate(repository)
    create.execute(
        idempotency_key="future-snap-mandate",
        lineage_id="lineage-future-snap",
        effective_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=1),
        total_capital_allocated="100",
        permitted_venues=(),
        permitted_market_families=(),
        maximum_aggregate_exposure_fraction="0",
        maximum_per_instrument_fraction="0",
        maximum_venue_concentration_fraction="0",
        maximum_market_family_concentration_fraction="0",
        maximum_capital_lockup_days=None,
    )
    future_snapshot = _seed_snapshot(
        postgres_engine, snapshot_id="future-snap", as_of=NOW + timedelta(days=1)
    )
    opportunity = _seed_opportunity(
        postgres_engine, projection_id="future-snap-proj", generated_at=NOW - timedelta(hours=1)
    )
    evaluate = EvaluateOpportunityAgainstMandate(repository)
    with pytest.raises(InvestmentMandateError):
        evaluate.execute(
            idempotency_key="future-snap-eval",
            opportunity_id=opportunity.projection_id,
            evaluated_at=NOW,
            mandate_lineage_id="lineage-future-snap",
            portfolio_snapshot_id=future_snapshot.snapshot_id,
        )


def test_mandate_and_evaluation_writes_touch_no_crypto_kalshi_or_options_tables(
    postgres_engine: Engine,
) -> None:
    unrelated_tables = (
        "apex_crypto_paper_account",
        "apex_kalshi_paper_account",
        "options_chain_snapshot",
        "corporate_filing_event",
    )
    existing_tables = set(sa.inspect(postgres_engine).get_table_names())
    before = {
        table: _row_count(postgres_engine, table)
        for table in unrelated_tables
        if table in existing_tables
    }
    repository = PostgresInvestmentMandateRepository(postgres_engine)
    create = CreateInvestmentMandate(repository)
    create.execute(
        idempotency_key="untouched-mandate",
        lineage_id="lineage-untouched",
        effective_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=1),
        total_capital_allocated="100",
        permitted_venues=("COINBASE",),
        permitted_market_families=("CRYPTO_SPOT",),
        maximum_aggregate_exposure_fraction="0.5",
        maximum_per_instrument_fraction="0.5",
        maximum_venue_concentration_fraction="0.5",
        maximum_market_family_concentration_fraction="0.5",
        maximum_capital_lockup_days=30,
    )
    _seed_snapshot(postgres_engine, snapshot_id="untouched-snap", as_of=NOW - timedelta(hours=1))
    opportunity = _seed_opportunity(
        postgres_engine, projection_id="untouched-proj", generated_at=NOW - timedelta(hours=2)
    )
    EvaluateOpportunityAgainstMandate(repository).execute(
        idempotency_key="untouched-eval",
        opportunity_id=opportunity.projection_id,
        evaluated_at=NOW,
        mandate_lineage_id="lineage-untouched",
        portfolio_snapshot_id="untouched-snap",
    )
    after = {
        table: _row_count(postgres_engine, table)
        for table in unrelated_tables
        if table in existing_tables
    }
    assert before == after


def test_every_new_mandate_and_evaluation_row_carries_authority_none(
    postgres_engine: Engine,
) -> None:
    repository = PostgresInvestmentMandateRepository(postgres_engine)
    create = CreateInvestmentMandate(repository)
    create.execute(
        idempotency_key="authority-mandate",
        lineage_id="lineage-authority",
        effective_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=1),
        total_capital_allocated="100",
        permitted_venues=("COINBASE",),
        permitted_market_families=("CRYPTO_SPOT",),
        maximum_aggregate_exposure_fraction="0.5",
        maximum_per_instrument_fraction="0.5",
        maximum_venue_concentration_fraction="0.5",
        maximum_market_family_concentration_fraction="0.5",
        maximum_capital_lockup_days=30,
    )
    _seed_snapshot(postgres_engine, snapshot_id="authority-snap", as_of=NOW - timedelta(hours=1))
    opportunity = _seed_opportunity(
        postgres_engine, projection_id="authority-proj", generated_at=NOW - timedelta(hours=2)
    )
    EvaluateOpportunityAgainstMandate(repository).execute(
        idempotency_key="authority-eval",
        opportunity_id=opportunity.projection_id,
        evaluated_at=NOW,
        mandate_lineage_id="lineage-authority",
        portfolio_snapshot_id="authority-snap",
    )
    for table in ("investment_mandate", "mandate_evaluation"):
        with postgres_engine.connect() as connection:
            values = (
                connection.execute(sa.text(f'SELECT DISTINCT authority FROM "{table}"'))
                .scalars()
                .all()
            )
        assert set(values) == {"NONE"}
