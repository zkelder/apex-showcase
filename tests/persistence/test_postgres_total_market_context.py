from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.securities_postgres import PostgresSecuritiesRepository
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.adapters.total_market_context_postgres import (
    PostgresTotalMarketContextRepository,
)
from apex.adapters.total_market_context_sources import (
    GENUINE_TOTAL_MARKET_DATASET,
    RetainedTotalMarketContextSource,
)
from apex.application.shared_opportunity import (
    MaterializeSharedOpportunitySet,
    ProposeSharedResearchAllocation,
)
from apex.application.total_market_context import (
    AttachTotalMarketContext,
    MaterializeTotalMarketContextSnapshot,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesAssetClass,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
    SecuritiesDataBatchState,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
)
from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.total_market_context import (
    ContextComponent,
    ContextComponentType,
    ContextSourceBundle,
    OverallContextState,
    TotalMarketContextError,
    UnavailableContextComponent,
)
from apex.domain.values import content_digest

NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _projection(identity: str, venue: SharedVenue) -> SharedOpportunityProjection:
    return SharedOpportunityProjection.create(
        projection_id=f"context-postgres-{identity}",
        venue=venue,
        source_record_type="ContextFixtureOpportunity",
        source_record_id=f"context-source-{identity}",
        source_canonical_hash=content_digest((identity, "context-source")),
        strategy_or_method_id="CONTEXT_FIXTURE_V1",
        instrument_id=identity,
        product_or_market_id=f"{identity}-market",
        direction="RESEARCH_ONLY",
        generated_at=NOW,
        valid_until=NOW + timedelta(days=1),
        evidence_class="HISTORICAL_PUBLIC",
        freshness_seconds=10,
        freshness_limit_seconds=300,
        comparable_value="0.01",
        uncertainty="0.1",
        maximum_loss="10",
        modeled_fees="1",
        modeled_slippage="1",
        costs_retained=True,
        liquidity_state="SUFFICIENT",
        liquidity_limitations=(),
        required_hypothetical_capital="100",
        maximum_sensible_research_allocation="100",
        source_maximum_allocation_fraction=None,
        expected_holding_days=1,
        estimated_lockup_days=1,
        supporting_evidence_ids=(f"context-evidence-{identity}",),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(),
        source_details=(("market_title", "Will CPI inflation exceed 3 percent?"),),
    )


class _SharedSource:
    def crypto_projections(
        self, *, pulse_id: str, as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        return (_projection("crypto", SharedVenue.CRYPTO),)

    def kalshi_projections(
        self, *, evaluation_ids: tuple[str, ...], as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        return (_projection("kalshi", SharedVenue.KALSHI),)

    def securities_projections(self, *, as_of: datetime) -> tuple[SharedOpportunityProjection, ...]:
        return ()


def _component(component_type: ContextComponentType, subject: str) -> ContextComponent:
    return ContextComponent.create(
        component_id=f"context-postgres-{component_type.value.lower()}",
        component_type=component_type,
        source_observation_id=f"context-bar-{component_type.value.lower()}",
        provider="FIXTURE_RETAINED",
        subject=subject,
        observation_at=NOW - timedelta(minutes=2),
        available_at=NOW - timedelta(minutes=1),
        ingested_at=NOW,
        current_value="105"
        if component_type is not ContextComponentType.MARKET_VOLATILITY
        else "0.2",
        prior_comparable_value=(
            "100" if component_type is not ContextComponentType.MARKET_VOLATILITY else "0.18"
        ),
        freshness_seconds=60,
        freshness_limit_seconds=3600,
        quality_status="VALID",
        evidence_class="FIXTURE",
        limitations=("FIXTURE_ONLY",),
        interpretation_category=f"{subject}_FIXTURE",
        supporting_evidence_ids=(f"context-lineage-{subject}",),
    )


class _ContextSource:
    def load(self, *, source_batch_id: str, as_of: datetime) -> ContextSourceBundle:
        components = (
            _component(ContextComponentType.US_EQUITY_MARKET, "SPY"),
            _component(ContextComponentType.TREASURY_RATE, "TLT"),
            _component(ContextComponentType.MARKET_VOLATILITY, "SPY"),
            _component(ContextComponentType.COMMODITY_OR_INFLATION, "USO"),
        )
        unavailable = tuple(
            UnavailableContextComponent(item, "NOT_RETAINED")
            for item in ContextComponentType
            if item not in {component.component_type for component in components}
        )
        return ContextSourceBundle(components, unavailable)


@pytest.mark.postgres
def test_total_market_context_is_immutable_idempotent_non_mutating_and_guarded(
    postgres_engine: sa.Engine,
) -> None:
    before: dict[str, int] = {}
    with postgres_engine.connect() as connection:
        for table in (
            '"order"',
            "position",
            "risk_reservation",
            "execution_event",
            "ledger_journal",
            "crypto_research_campaign",
            "crypto_holdout_event",
        ):
            before[table] = int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)

    shared_repository = PostgresSharedOpportunityRepository(postgres_engine)
    shared = MaterializeSharedOpportunitySet(shared_repository, _SharedSource()).execute(
        opportunity_set_id="context-postgres-shared-set",
        crypto_pulse_id="exact-pulse",
        kalshi_evaluation_ids=("exact-kalshi",),
        as_of=NOW,
        idempotency_key="context-postgres-shared-key",
    )
    allocation = ProposeSharedResearchAllocation(shared_repository).execute(
        proposal_id="context-postgres-allocation",
        opportunity_set_id=shared.opportunity_set.opportunity_set_id,
        idempotency_key="context-postgres-allocation-key",
    )
    decisions_before = tuple(item.canonical_hash for item in shared.decisions)
    priorities_before = tuple(item.canonical_hash for item in shared.priorities)
    allocation_before = allocation.proposal.canonical_hash

    repository = PostgresTotalMarketContextRepository(postgres_engine)
    snapshot_service = MaterializeTotalMarketContextSnapshot(repository, _ContextSource())
    first = snapshot_service.execute(
        snapshot_id="context-postgres-snapshot",
        source_batch_id="context-postgres-batch",
        as_of=NOW,
        idempotency_key="context-postgres-snapshot-key",
    )
    replay = snapshot_service.execute(
        snapshot_id="context-postgres-snapshot",
        source_batch_id="context-postgres-batch",
        as_of=NOW,
        idempotency_key="context-postgres-snapshot-key",
    )
    assert not first.idempotent_replay
    assert replay.idempotent_replay
    assert replay.snapshot == first.snapshot
    with pytest.raises(
        TotalMarketContextError,
        match="TOTAL_MARKET_SNAPSHOT_IDENTITY_CONFLICT",
    ):
        snapshot_service.execute(
            snapshot_id="context-postgres-snapshot",
            source_batch_id="different-context-postgres-batch",
            as_of=NOW,
            idempotency_key="context-postgres-snapshot-key",
        )

    attach_service = AttachTotalMarketContext(repository)
    enriched = attach_service.execute(
        enriched_set_id="context-postgres-enriched-set",
        opportunity_set_id=shared.opportunity_set.opportunity_set_id,
        context_snapshot_id=first.snapshot.snapshot_id,
        attached_at=NOW,
        idempotency_key="context-postgres-attach-key",
    )
    enriched_replay = attach_service.execute(
        enriched_set_id="context-postgres-enriched-set",
        opportunity_set_id=shared.opportunity_set.opportunity_set_id,
        context_snapshot_id=first.snapshot.snapshot_id,
        attached_at=NOW,
        idempotency_key="context-postgres-attach-key",
    )
    assert not enriched.idempotent_replay
    assert enriched_replay.idempotent_replay
    second_enriched = attach_service.execute(
        enriched_set_id="context-postgres-enriched-set-v2",
        opportunity_set_id=shared.opportunity_set.opportunity_set_id,
        context_snapshot_id=first.snapshot.snapshot_id,
        attached_at=NOW,
        idempotency_key="context-postgres-attach-v2-key",
    )
    assert not second_enriched.idempotent_replay
    assert set(second_enriched.enriched_set.attachment_ids).isdisjoint(
        enriched.enriched_set.attachment_ids
    )
    with pytest.raises(
        TotalMarketContextError,
        match="TOTAL_MARKET_ENRICHED_SET_IDENTITY_CONFLICT",
    ):
        attach_service.execute(
            enriched_set_id="context-postgres-enriched-set",
            opportunity_set_id=shared.opportunity_set.opportunity_set_id,
            context_snapshot_id=first.snapshot.snapshot_id,
            attached_at=NOW + timedelta(seconds=1),
            idempotency_key="context-postgres-attach-key",
        )
    assert enriched.enriched_set.eligibility_unchanged
    assert enriched.enriched_set.priority_unchanged
    assert enriched.enriched_set.allocation_unchanged
    assert len(enriched.attachments) == 2
    assert repository.context_snapshot(first.snapshot.snapshot_id) == first.snapshot
    assert repository.enriched_set(enriched.enriched_set.enriched_set_id) == enriched.enriched_set

    assert (
        tuple(
            item.canonical_hash
            for item in shared_repository.eligibility_decisions(
                shared.opportunity_set.opportunity_set_id
            )
        )
        == decisions_before
    )
    assert (
        tuple(
            item.canonical_hash
            for item in shared_repository.priorities(shared.opportunity_set.opportunity_set_id)
        )
        == priorities_before
    )
    assert (
        shared_repository.allocation_for_set(
            shared.opportunity_set.opportunity_set_id
        ).canonical_hash
        == allocation_before
    )

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE total_market_context_component SET authority = 'PAPER' "
                "WHERE component_id = 'context-postgres-us_equity_market'"
            )
        )

    with postgres_engine.connect() as connection:
        for table, count in before.items():
            assert int(connection.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0) == count
        # Downgrading now hits migration 0093's own retained-evidence guard
        # first (it is newer and blocks with retained ``total_market_
        # context_component`` rows present) -- 0059's own guard, further
        # down the chain, is never reached from head while any component
        # row survives. See ``test_downgrade_0093_blocks_with_retained_
        # component_evidence`` below for a direct, single-step proof of
        # 0093's own guard. This long a walk never observably commits any
        # intermediate step on this connection, though: the final
        # ``connection.rollback()`` reverts the whole attempt back to head,
        # not to 0093 -- see the direct single-step proof below for the
        # resting-point behavior of a short, adjacent-target walk instead.
        # Verified empirically after adding migration 0095 (Counterfactual
        # Decision Classification & Interpretation Versioning v1) directly
        # above 0094.
        with pytest.raises(
            Exception,
            match="cannot downgrade 0093 with retained context component evidence",
        ):
            command.downgrade(_config(connection), "0058_shared_opportunity")
        connection.rollback()
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0118_evidence_gap_recovery"
        )


# --------------------------------------------------------------------------
# Recurring Operating Health & Unattended Evidence Closure v1: the genuine
# ``TOTAL_MARKET_COMPONENT_IDENTITY_CONFLICT`` root cause, reproduced and
# fixed through the real ``RetainedTotalMarketContextSource`` (not a
# fixture source) -- a component's own ``freshness_seconds`` is inherently
# as-of-relative, so re-observing an unchanged source batch on a later day
# must mint genuinely new, non-colliding component identity, and that
# identity must agree with the persistence layer's own uniqueness guard
# (migration 0093).
# --------------------------------------------------------------------------


def _live_bar(instrument_id: str, day: str, close: str) -> SecuritiesBarEvidence:
    period_start = datetime.fromisoformat(f"{day}T13:30:00+00:00")
    period_end = datetime.fromisoformat(f"{day}T20:00:00+00:00")
    return SecuritiesBarEvidence(
        instrument_id=instrument_id,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=period_start,
        period_end=period_end,
        available_at=period_end + timedelta(minutes=30),
        open=close,
        high=str(Decimal(close) + 1),
        low=str(Decimal(close) - 1),
        close=close,
        volume="1000",
        source_identity="TEST_TMC_LIVE",
    )


def _live_session(day: str) -> SecuritiesCalendarSession:
    return SecuritiesCalendarSession.create(
        calendar_identity="XNYS",
        calendar_version="test-tmc-live-v1",
        timezone="America/New_York",
        session_date=day,
        opens_at=datetime.fromisoformat(f"{day}T13:30:00+00:00"),
        closes_at=datetime.fromisoformat(f"{day}T20:00:00+00:00"),
        is_early_close=False,
        source_identity="TEST_TMC_LIVE_CALENDAR",
    )


def _seed_live_batch(
    postgres_engine: Engine, *, batch_id: str, last_day: str, day_count: int
) -> SecuritiesMarketDataBatch:
    """Seeds a genuine ``GENUINE_TOTAL_MARKET_DATASET`` batch with real
    SPY/TLT/USO daily bars ending exactly at ``last_day``, through the same
    ``PostgresSecuritiesRepository.save_dataset`` real persistence path any
    genuine capture uses -- so ``RetainedTotalMarketContextSource`` reads it
    exactly as it would real captured evidence. ``RetainedTotalMarket
    ContextSource.load`` never reads ``securities_calendar_session``, but
    ``save_dataset`` itself requires a non-empty session sequence (an empty
    list makes SQLAlchemy's bulk insert emit one all-NULL ``DEFAULT VALUES``
    row instead of a no-op), so one genuine session per date is seeded too."""

    from datetime import date as _date

    last = _date.fromisoformat(last_day)
    dates = [
        (last - timedelta(days=day_count - 1 - index)).isoformat() for index in range(day_count)
    ]
    sessions = tuple(_live_session(day) for day in dates)
    symbols = ("SPY", "TLT", "USO")
    instruments = tuple(
        SecuritiesInstrument(
            instrument_id=f"{batch_id}-{symbol.lower()}",
            symbol=symbol,
            asset_class=SecuritiesAssetClass.ETF,
            exchange_mic="ARCX",
            currency="USD",
            timezone="America/New_York",
            valid_from="0001-01-01",
            valid_to=None,
            source_identity="TEST_TMC_LIVE",
        )
        for symbol in symbols
    )
    bars: list[SecuritiesBarEvidence] = []
    for offset, symbol in enumerate(symbols):
        for index, day in enumerate(dates):
            close = str(Decimal("100") + Decimal(offset * 10) + Decimal(index) * Decimal("0.1"))
            bars.append(_live_bar(f"{batch_id}-{symbol.lower()}", day, close))
    batch = SecuritiesMarketDataBatch(
        batch_id=batch_id,
        dataset_name=GENUINE_TOTAL_MARKET_DATASET,
        state=SecuritiesDataBatchState.READY,
        imported_at=datetime.fromisoformat(f"{dates[-1]}T21:00:00+00:00"),
        source_artifact="x",
        source_sha256="a" * 64,
        source_byte_size=1,
        canonical_artifact="y",
        canonical_sha256="b" * 64,
        canonical_byte_size=1,
        instrument_count=len(instruments),
        session_count=len(dates),
        bar_count=len(bars),
        corporate_action_count=0,
        first_period_start=datetime.fromisoformat(f"{dates[0]}T13:30:00+00:00"),
        last_period_end=datetime.fromisoformat(f"{dates[-1]}T20:00:00+00:00"),
        idempotency_key=f"idempotency-{batch_id}",
        warnings=(),
        canonical_hash=content_digest(f"batch-{batch_id}"),
    )
    PostgresSecuritiesRepository(postgres_engine).save_dataset(
        batch=batch, instruments=instruments, sessions=sessions, bars=bars, corporate_actions=()
    )
    return batch


@pytest.mark.postgres
def test_retained_source_rebuilds_a_snapshot_for_the_same_batch_on_a_later_day(
    postgres_engine: Engine,
) -> None:
    """Regression for the genuine ``TOTAL_MARKET_COMPONENT_IDENTITY_CONFLICT``
    root-caused during Recurring Operating Health & Unattended Evidence
    Closure v1: a component's own ``freshness_seconds`` is inherently
    as-of-relative, so re-observing the identical source batch on a later
    day must mint genuinely new, non-colliding component rows -- proven
    through the real ``RetainedTotalMarketContextSource``/
    ``MaterializeTotalMarketContextSnapshot``/``PostgresTotalMarketContext
    Repository`` path, not a fixture source. Also proves both genuine-ready
    sides: a fresh as-of classifies real state; a genuinely stale as-of
    honestly reports STALE rather than fabricating freshness."""

    batch = _seed_live_batch(
        postgres_engine, batch_id="tmc-live-rebuild-batch", last_day="2026-07-27", day_count=45
    )
    source = RetainedTotalMarketContextSource(postgres_engine)
    repository = PostgresTotalMarketContextRepository(postgres_engine)
    service = MaterializeTotalMarketContextSnapshot(repository, source)

    # Genuinely sufficient (fresh) evidence: an as-of close to the batch's
    # own latest bar succeeds and classifies real, non-stale state.
    first_as_of = datetime.fromisoformat("2026-07-30T00:00:00+00:00")
    first = service.execute(
        snapshot_id="tmc-live-rebuild-snapshot-day1",
        source_batch_id=batch.batch_id,
        as_of=first_as_of,
        idempotency_key="tmc-live-rebuild-snapshot-day1-key",
    )
    assert not first.idempotent_replay
    assert len(first.components) == 4
    assert all(item.state.value != "STALE" for item in first.components)
    assert first.snapshot.overall_state is not OverallContextState.INSUFFICIENT_CONTEXT

    # A genuinely later day, same unchanged batch -- before the fix, this
    # raised TOTAL_MARKET_COMPONENT_IDENTITY_CONFLICT because every
    # component's identity ignored its own as-of date. Also genuinely
    # insufficient (stale) evidence: it must succeed and honestly report
    # STALE, never silently fabricate freshness.
    second_as_of = datetime.fromisoformat("2026-08-10T00:00:00+00:00")
    second = service.execute(
        snapshot_id="tmc-live-rebuild-snapshot-day2",
        source_batch_id=batch.batch_id,
        as_of=second_as_of,
        idempotency_key="tmc-live-rebuild-snapshot-day2-key",
    )
    assert not second.idempotent_replay
    assert len(second.components) == 4
    assert all(item.state.value == "STALE" for item in second.components)
    assert all(item.contribution == 0 for item in second.components)
    assert second.snapshot.overall_state is OverallContextState.INSUFFICIENT_CONTEXT

    assert {item.component_id for item in first.components}.isdisjoint(
        item.component_id for item in second.components
    )
    with postgres_engine.connect() as connection:
        count = connection.scalar(
            sa.text(
                "SELECT count(*) FROM total_market_context_component WHERE source_batch_id = :b"
            ),
            {"b": batch.batch_id},
        )
    assert count == 8


@pytest.mark.postgres
def test_retained_source_replay_stays_idempotent_on_the_identical_day(
    postgres_engine: Engine,
) -> None:
    """The fix does not weaken same-day idempotent replay: an exact repeat
    call for the identical as-of reuses the identical retained component
    rows rather than minting duplicates."""

    batch = _seed_live_batch(
        postgres_engine, batch_id="tmc-live-replay-batch", last_day="2026-07-27", day_count=45
    )
    source = RetainedTotalMarketContextSource(postgres_engine)
    repository = PostgresTotalMarketContextRepository(postgres_engine)
    service = MaterializeTotalMarketContextSnapshot(repository, source)
    as_of = datetime.fromisoformat("2026-07-30T00:00:00+00:00")

    first = service.execute(
        snapshot_id="tmc-live-replay-snapshot",
        source_batch_id=batch.batch_id,
        as_of=as_of,
        idempotency_key="tmc-live-replay-snapshot-key",
    )
    replay = service.execute(
        snapshot_id="tmc-live-replay-snapshot",
        source_batch_id=batch.batch_id,
        as_of=as_of,
        idempotency_key="tmc-live-replay-snapshot-key",
    )
    assert not first.idempotent_replay
    assert replay.idempotent_replay
    assert {item.component_id for item in first.components} == {
        item.component_id for item in replay.components
    }
    with postgres_engine.connect() as connection:
        count = connection.scalar(
            sa.text(
                "SELECT count(*) FROM total_market_context_component WHERE source_batch_id = :b"
            ),
            {"b": batch.batch_id},
        )
    assert count == 4


@pytest.mark.postgres
def test_downgrade_0093_blocks_with_retained_component_evidence(postgres_engine: Engine) -> None:
    """Migration 0093's own single-step downgrade guard, proved directly:
    it must refuse to run while any ``total_market_context_component`` row
    survives, exactly like every other migration's retained-evidence
    downgrade guard in this repository."""

    batch = _seed_live_batch(
        postgres_engine,
        batch_id="tmc-live-downgrade-guard-batch",
        last_day="2026-07-27",
        day_count=45,
    )
    source = RetainedTotalMarketContextSource(postgres_engine)
    repository = PostgresTotalMarketContextRepository(postgres_engine)
    MaterializeTotalMarketContextSnapshot(repository, source).execute(
        snapshot_id="tmc-live-downgrade-guard-snapshot",
        source_batch_id=batch.batch_id,
        as_of=datetime.fromisoformat("2026-07-30T00:00:00+00:00"),
        idempotency_key="tmc-live-downgrade-guard-snapshot-key",
    )
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0093 with retained context component evidence",
        ):
            command.downgrade(_config(connection), "0092_portfolio_mandate")
        connection.rollback()
        # 0094 (Counterfactual Decision Ledger) sits directly above 0093 and
        # owns no retained evidence here, so its own single empty-table
        # downgrade step commits genuinely before 0093's guard is reached and
        # blocks the walk -- unlike the longer multi-step walk exercised in
        # ``test_total_market_context_is_immutable_idempotent_non_mutating_
        # and_guarded`` above (which never observably commits past 0094), a
        # short adjacent-target walk like this one lands on 0093 itself, not
        # head. Either resting point is equally valid proof that 0093's
        # guard blocked the requested target; this assertion pins the one
        # this specific target distance deterministically produces.
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0093_tmc_component_as_of"
        )
