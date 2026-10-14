from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from apex.adapters.coinbase_universe_public import FixtureCoinbaseUniverseSource
from apex.adapters.live_market_observation_postgres import (
    PostgresLiveMarketObservationRepository,
)
from apex.application.live_market_observation import (
    ExecuteLiveMarketObservationRound,
    ObtainContemporaneousMarketObservation,
)
from apex.domain.live_market_observation import (
    ContemporaneousObservationDisposition,
    LiveMarketObservation,
    LiveMarketObservationError,
    LiveObservationQuality,
    LiveObservationRoundStatus,
    LiveObservationType,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD")


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


@pytest.mark.postgres
def test_live_market_observation_is_durable_immutable_and_downgrade_guarded(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresLiveMarketObservationRepository(postgres_engine)
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())

    result = service.execute(
        plan_id="postgres-live-observation-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=PRODUCTS,
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )
    assert result.round.status is LiveObservationRoundStatus.COMPLETED
    assert len(result.observations) == len(PRODUCTS)

    fresh = PostgresLiveMarketObservationRepository(postgres_engine)
    assert fresh.capture_round(result.round.round_id) == result.round
    assert fresh.observations_for_round(result.round.round_id) == result.observations
    for observation in result.observations:
        assert fresh.observation(observation.observation_id) == observation

    hook = ObtainContemporaneousMarketObservation(fresh)
    hook_result = hook.execute(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=NOW + timedelta(seconds=30),
        max_staleness_seconds=90,
    )
    assert hook_result.disposition is ContemporaneousObservationDisposition.SATISFIED
    assert hook_result.observation is not None
    assert hook_result.observation.observation_id in {o.observation_id for o in result.observations}

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE live_market_observation SET authority = 'PAPER' "
                "WHERE observation_id = :observation_id"
            ),
            {"observation_id": result.observations[0].observation_id},
        )

    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0106 with retained live_market_observation evidence",
        ):
            command.downgrade(_config(connection), "0105_securities_structure_perf")
        connection.rollback()
        # Deliberately not the current head (Kalshi Settlement & Resolution
        # Evidence Maturation v1, migration 0107): each migration step in a
        # multi-step ``command.downgrade`` commits independently, so
        # 0107's own downgrade (zero retained ``kalshi_resolution_fact``
        # rows in this test's fixture) already completed and committed
        # *before* 0106's guard below it was ever reached. The resting
        # point after 0106's own guard fires is therefore always "one step
        # below whatever the true head is", not head itself -- exactly the
        # same shape as ``test_postgres_kalshi_baseline_quote.py``'s own
        # documented 0103/0104 case.
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0106_live_market_observation"
        )


@pytest.mark.postgres
def test_latest_observation_prefers_genuine_recency_over_insertion_order(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresLiveMarketObservationRepository(postgres_engine)
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())

    later = service.execute(
        plan_id="postgres-live-observation-order-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=NOW + timedelta(minutes=5),
        requested_at=NOW + timedelta(minutes=5),
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )
    earlier = service.execute(
        plan_id="postgres-live-observation-order-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )
    # Inserted out of chronological order (later window saved first) -- the
    # repository must still resolve "latest" by captured_at, never by
    # insertion order or id string.
    assert earlier.round.window_start < later.round.window_start

    fresh = PostgresLiveMarketObservationRepository(postgres_engine)
    latest = fresh.latest_observation(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=NOW + timedelta(minutes=10),
    )
    assert latest is not None
    assert latest.observation_id == later.observations[0].observation_id


# ---- Live Observation Point-in-Time Race Fix v1 -----------------------
#
# The same three scenarios are reproduced verbatim against the in-memory
# double in tests/test_live_market_observation_application.py, so both
# implementations of LiveMarketObservationRepository are proven to agree.
# Reproduces the exact race shape that froze the first real
# STRATEGY_PAPER_TRIAL_EXECUTION dispatch on 2026-08-27 (see memory
# apex-docker-wsl-outage-recovery-strategy-paper-race-v1): an independently-
# scheduled live-observer worker writes a fresh row a few milliseconds AFTER
# a caller's own (already-frozen) as_of, alongside a genuinely valid
# observation captured shortly before it.


@pytest.mark.postgres
def test_latest_observation_bounds_by_as_of_and_prefers_the_valid_past_row(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresLiveMarketObservationRepository(postgres_engine)
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    as_of = NOW

    valid_past = service.execute(
        plan_id="postgres-live-observation-race-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=as_of - timedelta(seconds=60),
        requested_at=as_of - timedelta(seconds=60),
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )
    future_row = service.execute(
        plan_id="postgres-live-observation-race-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=as_of + timedelta(milliseconds=22),
        requested_at=as_of + timedelta(milliseconds=22),
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )
    assert future_row.round.window_start > as_of  # sanity: genuinely future relative to as_of

    fresh = PostgresLiveMarketObservationRepository(postgres_engine)
    latest = fresh.latest_observation(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
    )
    assert latest is not None
    assert latest.observation_id == valid_past.observations[0].observation_id
    assert latest.observation_id != future_row.observations[0].observation_id

    # The domain staleness gate must accept it cleanly -- the whole point of
    # the fix is that Phase D no longer raises LIVE_OBSERVATION_FUTURE_
    # TIMESTAMP on a real, already-available past observation merely because
    # a newer row also exists.
    hook_result = ObtainContemporaneousMarketObservation(fresh).execute(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
        max_staleness_seconds=90,
    )
    assert hook_result.disposition is ContemporaneousObservationDisposition.SATISFIED
    assert hook_result.observation is not None
    assert hook_result.observation.observation_id == valid_past.observations[0].observation_id


@pytest.mark.postgres
def test_latest_observation_with_only_future_rows_is_absent_not_the_future_row(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresLiveMarketObservationRepository(postgres_engine)
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    as_of = NOW

    service.execute(
        plan_id="postgres-live-observation-only-future-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=as_of + timedelta(seconds=1),
        requested_at=as_of + timedelta(seconds=1),
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    fresh = PostgresLiveMarketObservationRepository(postgres_engine)
    latest = fresh.latest_observation(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
    )
    assert latest is None

    hook_result = ObtainContemporaneousMarketObservation(fresh).execute(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
        max_staleness_seconds=90,
    )
    assert hook_result.disposition is ContemporaneousObservationDisposition.ABSENT
    assert hook_result.observation is None


@pytest.mark.postgres
def test_latest_observation_includes_a_row_captured_exactly_at_as_of(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresLiveMarketObservationRepository(postgres_engine)
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    as_of = NOW

    exact = service.execute(
        plan_id="postgres-live-observation-exact-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=as_of,
        requested_at=as_of,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    fresh = PostgresLiveMarketObservationRepository(postgres_engine)
    latest = fresh.latest_observation(
        venue=SharedVenue.CRYPTO,
        instrument_id="BTC-USD",
        observation_type=LiveObservationType.QUOTE,
        as_of=as_of,
    )
    assert latest is not None
    assert latest.observation_id == exact.observations[0].observation_id


@pytest.mark.postgres
def test_save_observation_is_idempotent_and_rejects_identity_conflicts(
    postgres_engine: sa.Engine,
) -> None:
    repository = PostgresLiveMarketObservationRepository(postgres_engine)
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    result = service.execute(
        plan_id="postgres-live-observation-conflict-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=("BTC-USD",),
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )
    original = result.observations[0]

    # Genuine replay of the identical row: idempotent no-op, not an error.
    assert repository.save_observation(original) == original

    conflicting = LiveMarketObservation.create(
        round_id=original.round_id,
        venue=original.venue,
        provider=original.provider,
        instrument_id=original.instrument_id,
        observation_type=original.observation_type,
        captured_at=original.captured_at,
        bid_price=Decimal("999999.99"),
        ask_price=Decimal("1000000.00"),
        evidence_class=EvidenceClass.FIXTURE,
        provider_schema_version=original.provider_schema_version,
        source_identity=original.source_identity,
        quality=LiveObservationQuality.PASSED,
    )
    assert conflicting.observation_id == original.observation_id
    assert conflicting.canonical_hash != original.canonical_hash

    with pytest.raises(LiveMarketObservationError, match="LIVE_OBSERVATION_IDENTITY_CONFLICT"):
        repository.save_observation(conflicting)


@pytest.mark.postgres
def test_census_reports_retained_counts(postgres_engine: sa.Engine) -> None:
    repository = PostgresLiveMarketObservationRepository(postgres_engine)
    service = ExecuteLiveMarketObservationRound(repository, FixtureCoinbaseUniverseSource())
    service.execute(
        plan_id="postgres-live-observation-census-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=PRODUCTS,
        window_start=NOW,
        requested_at=NOW,
        provider_schema_version="COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
    )

    census = repository.census()
    assert census["total_round_rows"] == 1
    assert census["total_observation_rows"] == len(PRODUCTS)
    assert census["by_venue_and_quality"] == {"CRYPTO/PASSED": len(PRODUCTS)}
