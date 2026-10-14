from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from apex.adapters.coinbase_universe_public import FixtureCoinbaseUniverseSource
from apex.adapters.crypto_market_pulse_postgres import (
    PostgresCryptoMarketPulseRepository,
)
from apex.adapters.shared_opportunity_sources import RetainedSharedOpportunitySource
from apex.application.crypto_market_pulse import (
    ExecuteCryptoMarketPulseRefresh,
    RegisterCryptoRefreshPlan,
    RunCurrentCryptoRelativeStrength,
    RunCurrentCryptoReversalRegime,
)
from apex.application.crypto_universe import (
    CaptureCryptoUniverseObservations,
    CapturePublicCryptoProducts,
    MaterializeCryptoUniverse,
)
from apex.domain.crypto_market_pulse import RefreshState

NOW = datetime(2026, 7, 29, tzinfo=UTC)


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


@pytest.mark.postgres
def test_crypto_market_pulse_is_durable_immutable_and_downgrade_guarded(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresCryptoMarketPulseRepository(postgres_engine)
    source = FixtureCoinbaseUniverseSource()
    capture = CapturePublicCryptoProducts(repository, source).execute(
        capture_id="postgres-pulse-products",
        requested_at=NOW,
    )
    CaptureCryptoUniverseObservations(repository, source).execute(
        capture_id=capture.capture_id,
        interval_start=datetime(2026, 1, 1, tzinfo=UTC),
        interval_end=datetime(2026, 7, 1, tzinfo=UTC),
        requested_at=NOW,
    )
    universe = MaterializeCryptoUniverse(repository).execute(
        snapshot_id="postgres-pulse-universe",
        capture_id=capture.capture_id,
        created_at=NOW,
    )
    plan = RegisterCryptoRefreshPlan(repository).execute(
        plan_id="postgres-pulse-plan",
        universe_snapshot_id=universe.snapshot_id,
        actor="persistence-test",
        created_at=NOW,
        current_market_evidence=True,
    )
    result = ExecuteCryptoMarketPulseRefresh(repository, source).execute(
        refresh_id="postgres-pulse-refresh",
        plan_id=plan.plan_id,
        as_of=datetime(2026, 7, 30, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )
    assert result.state is RefreshState.COMPLETED
    assert result.pulse is not None
    assert result.summary is not None
    assert result.network_requests_performed == 20
    assert all(
        item.best_bid is not None
        and item.best_ask is not None
        and item.visible_depth is not None
        and item.quote_raw_evidence_hash is not None
        for item in result.checkpoints
    )
    current = RunCurrentCryptoRelativeStrength(repository).execute(
        run_id="postgres-current-relative-strength-v1",
        pulse_id=result.pulse.pulse_id,
        generated_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )
    replay = RunCurrentCryptoRelativeStrength(repository).execute(
        run_id="postgres-current-relative-strength-v1",
        pulse_id=result.pulse.pulse_id,
        generated_at=datetime(2026, 7, 30, 2, tzinfo=UTC),
    )
    assert replay.idempotent_replay
    assert replay.run == current.run
    assert replay.opportunities == current.opportunities
    reversal = RunCurrentCryptoReversalRegime(repository).execute(
        run_id="postgres-current-reversal-regime-v1",
        pulse_id=result.pulse.pulse_id,
        generated_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )
    reversal_replay = RunCurrentCryptoReversalRegime(repository).execute(
        run_id="postgres-current-reversal-regime-v1",
        pulse_id=result.pulse.pulse_id,
        generated_at=datetime(2026, 7, 30, 2, tzinfo=UTC),
    )
    assert reversal_replay.idempotent_replay
    assert reversal_replay.run == reversal.run
    assert reversal_replay.opportunities == reversal.opportunities

    fresh = PostgresCryptoMarketPulseRepository(postgres_engine)
    assert fresh.refresh_plan(plan.plan_id) == plan
    assert fresh.refresh_run(result.refresh.refresh_id) == result.refresh
    assert fresh.pulse_checkpoints(result.refresh.refresh_id) == result.checkpoints
    assert fresh.pulse_snapshot(result.pulse.pulse_id) == result.pulse
    assert tuple(
        sorted(
            (
                item
                for item in fresh.opportunity_lineages()
                if item.refresh_id == result.refresh.refresh_id
            ),
            key=lambda item: item.lineage_id,
        )
    ) == tuple(sorted(result.lineages, key=lambda item: item.lineage_id))
    assert fresh.market_change_summary(result.summary.summary_id) == result.summary
    assert fresh.discovery_run(current.run.run_id) == current.run
    assert fresh.discovery_run(reversal.run.run_id) == reversal.run
    assert (
        tuple(
            sorted(
                (
                    item
                    for item in fresh.opportunities()
                    if item.discovery_run_id == current.run.run_id
                ),
                key=lambda item: (item.rank, item.product_id),
            )
        )
        == current.opportunities
    )
    projections = RetainedSharedOpportunitySource(postgres_engine).crypto_projections(
        pulse_id=result.pulse.pulse_id,
        as_of=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )
    assert len(projections) == 30
    assert all(
        item.maximum_loss == item.required_hypothetical_capital
        and item.maximum_loss is not None
        and item.maximum_sensible_research_allocation is not None
        and item.liquidity_state == "SUFFICIENT"
        and item.modeled_fees == Decimal("0.012")
        and item.modeled_slippage == Decimal("0.002")
        and ("quote_evidence_quality", "COMPLETE") in item.source_details
        for item in projections[:10]
    )
    current_projections = tuple(
        item
        for item in projections
        if item.strategy_or_method_id == "CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1"
    )
    assert len(current_projections) == 10
    assert all(
        item.maximum_loss == item.required_hypothetical_capital
        and item.modeled_fees == Decimal("0.012")
        and item.modeled_slippage == Decimal("0.002")
        and not item.paper_eligible
        and not item.live_eligible
        for item in current_projections
    )
    reversal_projections = tuple(
        item
        for item in projections
        if item.strategy_or_method_id == "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1"
    )
    assert len(reversal_projections) == 10
    assert all(
        ("return_7d", "") in item.source_details
        or any(key == "return_7d" for key, _ in item.source_details)
        for item in reversal_projections
    )

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE crypto_market_pulse_refresh SET authority = 'PAPER' "
                "WHERE refresh_id = :refresh_id"
            ),
            {"refresh_id": result.refresh.refresh_id},
        )

    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0057 with retained crypto market-pulse evidence",
        ):
            command.downgrade(_config(connection), "0056_crypto_universe_quality")
        connection.rollback()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0057_crypto_market_pulse"
        )
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0


@pytest.mark.postgres
def test_pulse_snapshots_orders_by_genuine_recency_not_pulse_id_string(
    postgres_engine: sa.Engine,
) -> None:
    """Regression test for the exact defect diagnosed in the first genuine
    activated cycle: ``pulse_snapshots`` used to ``ORDER BY pulse_id`` (a
    content-derived string), so a pulse whose id happened to sort later
    alphabetically -- even though it was captured *earlier* in calendar time
    -- was treated as "the latest pulse" by every ``pulses[-1]`` caller
    (``PostgresOperatingStateSource.snapshot``, and this same module's own
    prior-pulse change-summary lookup). In the genuine cycle that stale
    selection sourced every Coinbase Opportunity from a three-day-old pulse
    instead of the fresh same-cycle one, so all thirty came back EXPIRED.
    Reproduce the exact ordering inversion: the chronologically *earlier*
    pulse gets an alphabetically *larger* id, and the chronologically
    *later* pulse gets an alphabetically *smaller* id -- exactly mirroring
    "...genuine-public-book-2026-08-02-v1..." sorting after
    "...genuine-2026-07-30-v1..."."""

    repository = PostgresCryptoMarketPulseRepository(postgres_engine)
    source = FixtureCoinbaseUniverseSource()
    capture = CapturePublicCryptoProducts(repository, source).execute(
        capture_id="postgres-pulse-order-products",
        requested_at=NOW,
    )
    CaptureCryptoUniverseObservations(repository, source).execute(
        capture_id=capture.capture_id,
        interval_start=datetime(2026, 1, 1, tzinfo=UTC),
        interval_end=datetime(2026, 7, 1, tzinfo=UTC),
        requested_at=NOW,
    )
    universe = MaterializeCryptoUniverse(repository).execute(
        snapshot_id="postgres-pulse-order-universe",
        capture_id=capture.capture_id,
        created_at=NOW,
    )
    # Two separate plans stand in for the genuine two-plan history (the
    # original daily plan and the later additive book-capture plan) so each
    # refresh can use its own unconstrained as_of without the
    # (plan_id, as_of) uniqueness or daily-cadence gating of a shared plan.
    early_plan = RegisterCryptoRefreshPlan(repository).execute(
        plan_id="postgres-pulse-order-zzz-early-plan",
        universe_snapshot_id=universe.snapshot_id,
        actor="persistence-test",
        created_at=NOW,
        current_market_evidence=True,
    )
    late_plan = RegisterCryptoRefreshPlan(repository).execute(
        plan_id="postgres-pulse-order-aaa-late-plan",
        universe_snapshot_id=universe.snapshot_id,
        actor="persistence-test",
        created_at=NOW,
        current_market_evidence=True,
    )
    early_result = ExecuteCryptoMarketPulseRefresh(repository, source).execute(
        refresh_id="postgres-pulse-order-zzz-early-refresh",
        plan_id=early_plan.plan_id,
        as_of=datetime(2026, 7, 28, tzinfo=UTC),
        requested_at=datetime(2026, 7, 28, 1, tzinfo=UTC),
    )
    late_result = ExecuteCryptoMarketPulseRefresh(repository, source).execute(
        refresh_id="postgres-pulse-order-aaa-late-refresh",
        plan_id=late_plan.plan_id,
        as_of=datetime(2026, 7, 30, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )
    assert early_result.pulse is not None
    assert late_result.pulse is not None
    # Confirm the inversion this test depends on actually holds before
    # trusting the ordering assertion below.
    assert early_result.pulse.pulse_id > late_result.pulse.pulse_id
    assert early_result.pulse.as_of < late_result.pulse.as_of

    fresh = PostgresCryptoMarketPulseRepository(postgres_engine)
    ordered = fresh.pulse_snapshots()
    early_index = next(
        index for index, item in enumerate(ordered) if item.pulse_id == early_result.pulse.pulse_id
    )
    late_index = next(
        index for index, item in enumerate(ordered) if item.pulse_id == late_result.pulse.pulse_id
    )
    # The defect: naive ORDER BY pulse_id would have put "aaa-late" (the
    # chronologically later pulse) *before* "zzz-early" in this tuple, so
    # pulses[-1] -- "the latest pulse" -- would have resolved to the stale
    # one. The fix requires the chronologically earlier pulse to still
    # appear first despite its alphabetically later id.
    assert early_index < late_index

    # The same defect existed in universe_snapshots(); prove it is fixed too
    # using the identical alphabetical-inversion technique. manifest_hash is
    # content-derived from the source capture, so two distinct snapshots
    # (rather than two registrations of the same capture) need their own
    # captures to avoid a genuine duplicate-content conflict.
    from apex.adapters.crypto_universe_postgres import PostgresCryptoUniverseRepository
    from apex.application.crypto_universe import MaterializeCryptoUniverse as _Materialize

    universe_repository = PostgresCryptoUniverseRepository(postgres_engine)
    early_capture = CapturePublicCryptoProducts(repository, source).execute(
        capture_id="postgres-universe-order-early-products",
        requested_at=datetime(2026, 7, 27, tzinfo=UTC),
    )
    late_capture = CapturePublicCryptoProducts(repository, source).execute(
        capture_id="postgres-universe-order-late-products",
        requested_at=datetime(2026, 7, 29, tzinfo=UTC),
    )
    early_universe = _Materialize(universe_repository).execute(
        snapshot_id="postgres-universe-order-zzz-early",
        capture_id=early_capture.capture_id,
        created_at=datetime(2026, 7, 28, tzinfo=UTC),
    )
    late_universe = _Materialize(universe_repository).execute(
        snapshot_id="postgres-universe-order-aaa-late",
        capture_id=late_capture.capture_id,
        created_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    assert early_universe.snapshot_id > late_universe.snapshot_id
    assert early_universe.created_at < late_universe.created_at
    ordered_universes = universe_repository.universe_snapshots()
    early_universe_index = next(
        index
        for index, item in enumerate(ordered_universes)
        if item.snapshot_id == early_universe.snapshot_id
    )
    late_universe_index = next(
        index
        for index, item in enumerate(ordered_universes)
        if item.snapshot_id == late_universe.snapshot_id
    )
    assert early_universe_index < late_universe_index
