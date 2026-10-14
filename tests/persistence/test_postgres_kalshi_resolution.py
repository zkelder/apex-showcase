"""PostgreSQL persistence proofs for Kalshi Settlement & Resolution Evidence
Maturation v1: migration 0107, ``PostgresKalshiResolutionFactRepository``.
Zero genuine network requests -- every fact here is hand-built via
``domain.kalshi_resolution.build_kalshi_resolution_fact`` from synthetic raw
fields, exactly like ``tests/persistence/test_postgres_live_market_observation.py``
constructs its observations directly rather than through a live capture."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.kalshi_resolution_postgres import PostgresKalshiResolutionFactRepository
from apex.domain.kalshi_resolution import (
    KalshiMarketResolutionFact,
    KalshiResolutionError,
    build_kalshi_resolution_fact,
)
from apex.domain.research_evidence import EvidenceClass

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _fact(**overrides: object) -> KalshiMarketResolutionFact:
    values: dict[str, object] = {
        "market_ticker": "KX-TICK",
        "event_ticker": "KX-EVENT",
        "provider_status": "finalized",
        "raw_result": "yes",
        "settlement_value_dollars": "1.0000",
        "settlement_ts": NOW,
        "close_time": NOW,
        "source_endpoint_identity": "kalshi-public-market-followup-v1",
        "evidence_class": EvidenceClass.GENUINE_PUBLIC,
        "captured_at": NOW,
        "run_id": "test-run",
    }
    values.update(overrides)
    return build_kalshi_resolution_fact(**values)  # type: ignore[arg-type]


def test_record_persists_and_round_trips_every_field(postgres_engine: Engine) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    fact = _fact()
    assert repository.record(fact=fact) is False
    fresh = PostgresKalshiResolutionFactRepository(postgres_engine)
    round_tripped = fresh.latest_fact(market_ticker="KX-TICK", as_of=NOW)
    assert round_tripped == fact


def test_record_is_idempotent_on_replay(postgres_engine: Engine) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    fact = _fact()
    assert repository.record(fact=fact) is False
    assert repository.record(fact=fact) is True
    assert len(repository.facts_for_ticker(market_ticker="KX-TICK")) == 1


def test_record_raises_on_hash_conflict_for_an_identical_id(postgres_engine: Engine) -> None:
    """Structurally unreachable via the builder (the id is itself
    content-derived) -- tested here by hand-forcing a collision, the only
    way to exercise this defense-in-depth path at all."""

    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    original = _fact()
    repository.record(fact=original)
    tampered = KalshiMarketResolutionFact(
        resolution_fact_id=original.resolution_fact_id,
        market_ticker=original.market_ticker,
        event_ticker=original.event_ticker,
        provider_status=original.provider_status,
        resolution_state=original.resolution_state,
        raw_result="NO",  # <-- genuinely different content
        resolved_result="NO",
        settlement_value_dollars=original.settlement_value_dollars,
        settlement_ts=original.settlement_ts,
        close_time=original.close_time,
        source_endpoint_identity=original.source_endpoint_identity,
        evidence_class=original.evidence_class,
        captured_at=original.captured_at,
        run_id="a-different-run",
        canonical_hash="f" * 64,
        authority="NONE",
    )
    with pytest.raises(KalshiResolutionError):
        repository.record(fact=tampered)


def test_facts_for_ticker_returns_full_lineage_oldest_first(postgres_engine: Engine) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    older = _fact(
        provider_status="closed",
        raw_result=None,
        settlement_value_dollars=None,
        settlement_ts=None,
        captured_at=NOW - timedelta(hours=2),
    )
    newer = _fact(captured_at=NOW)
    repository.record(fact=newer)
    repository.record(fact=older)
    lineage = repository.facts_for_ticker(market_ticker="KX-TICK")
    assert [item.resolution_fact_id for item in lineage] == [
        older.resolution_fact_id,
        newer.resolution_fact_id,
    ]


def test_census_counts_current_state_by_market_and_distinct_tickers(
    postgres_engine: Engine,
) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    repository.record(fact=_fact(market_ticker="KX-A"))
    repository.record(
        fact=_fact(
            market_ticker="KX-B",
            provider_status="determined",
            raw_result=None,
            settlement_value_dollars=None,
            settlement_ts=None,
        )
    )
    census = repository.census()
    assert census["total_fact_rows"] == 2
    assert census["distinct_market_tickers"] == 2
    assert census["current_state_by_market"]["FINALIZED"] == 1
    assert census["current_state_by_market"]["RESULT_DETERMINED"] == 1
    assert census["latest_captured_at"] is not None


def test_census_current_state_counts_each_market_once_by_its_latest_fact_only(
    postgres_engine: Engine,
) -> None:
    """Postgres parity with the in-memory proof: a market with several
    retained observations must contribute to exactly one
    ``current_state_by_market`` bucket -- its latest -- never a bucket per
    historical fact row."""

    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    repository.record(
        fact=_fact(
            provider_status="closed",
            raw_result=None,
            settlement_value_dollars=None,
            settlement_ts=None,
            captured_at=NOW - timedelta(hours=2),
        )
    )
    repository.record(
        fact=_fact(
            provider_status="determined",
            raw_result="yes",
            settlement_value_dollars=None,
            settlement_ts=None,
            captured_at=NOW - timedelta(hours=1),
        )
    )
    repository.record(fact=_fact(provider_status="finalized", raw_result="yes", captured_at=NOW))
    census = repository.census()
    assert census["total_fact_rows"] == 3
    assert census["distinct_market_tickers"] == 1
    assert census["current_state_by_market"] == {"FINALIZED": 1}


# ---------------------------------------------------------------------------
# Point-in-time correctness (Part 9) -- Postgres parity with the in-memory
# tests, mirroring ``test_postgres_live_market_observation.py``'s own
# ``latest_observation`` point-in-time proofs exactly (Live Observation
# Point-in-Time Race Fix v1).
# ---------------------------------------------------------------------------


def test_latest_fact_bounds_by_as_of_and_prefers_the_valid_past_row(
    postgres_engine: Engine,
) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    past_non_terminal = _fact(
        provider_status="closed",
        raw_result=None,
        settlement_value_dollars=None,
        settlement_ts=None,
        captured_at=NOW - timedelta(hours=1),
    )
    future_finalized = _fact(captured_at=NOW + timedelta(hours=1))
    repository.record(fact=past_non_terminal)
    repository.record(fact=future_finalized)

    result = repository.latest_fact(market_ticker="KX-TICK", as_of=NOW)
    assert result is not None
    assert result.resolution_fact_id == past_non_terminal.resolution_fact_id
    assert result.resolved_result is None


def test_latest_fact_with_only_future_rows_is_absent_not_the_future_row(
    postgres_engine: Engine,
) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    repository.record(fact=_fact(captured_at=NOW + timedelta(hours=1)))
    assert repository.latest_fact(market_ticker="KX-TICK", as_of=NOW) is None


def test_latest_fact_includes_a_row_captured_exactly_at_as_of(postgres_engine: Engine) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    exact = _fact(captured_at=NOW)
    repository.record(fact=exact)
    result = repository.latest_fact(market_ticker="KX-TICK", as_of=NOW)
    assert result is not None
    assert result.resolution_fact_id == exact.resolution_fact_id


def test_latest_fact_scoped_to_the_requested_ticker_only(postgres_engine: Engine) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    repository.record(fact=_fact(market_ticker="KX-OTHER"))
    assert repository.latest_fact(market_ticker="KX-TICK", as_of=NOW) is None


# ---------------------------------------------------------------------------
# Immutability / migration boundary
# ---------------------------------------------------------------------------


def test_kalshi_resolution_fact_is_immutable(postgres_engine: Engine) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    fact = _fact()
    repository.record(fact=fact)
    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE kalshi_resolution_fact SET authority = 'PAPER' "
                "WHERE resolution_fact_id = :id"
            ),
            {"id": fact.resolution_fact_id},
        )
    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text("DELETE FROM kalshi_resolution_fact WHERE resolution_fact_id = :id"),
            {"id": fact.resolution_fact_id},
        )


def test_cannot_downgrade_0107_with_retained_evidence(postgres_engine: Engine) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    repository.record(fact=_fact())
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0107 with retained kalshi_resolution_fact evidence",
        ):
            command.downgrade(_config(connection), "0106_live_market_observation")
        connection.rollback()
        # Deliberately NOT "0108_kalshi_baseline_v2" (Kalshi
        # Baseline Quote Capture & Economic Maturation v1): with
        # ``transaction_per_migration=True`` (migrations/env.py), a
        # multi-step ``command.downgrade`` from head commits each
        # successful step independently. 0108's own downgrade has no
        # retained ``MARKET_QUOTE_OBSERVATION`` evidence in this fixture, so
        # it steps down and commits cleanly *before* 0107's own guard (the
        # one this test targets) fires and rolls back only its own step --
        # mirrors test_postgres_kalshi_baseline_quote.py's own
        # already-documented analogous case (0104 stepping down cleanly
        # before 0103's guard) exactly.
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0107_kalshi_resolution_fact"
        )


def test_authority_is_always_none(postgres_engine: Engine) -> None:
    repository = PostgresKalshiResolutionFactRepository(postgres_engine)
    fact = _fact()
    repository.record(fact=fact)
    with postgres_engine.connect() as connection:
        authority = connection.scalar(
            sa.text("SELECT authority FROM kalshi_resolution_fact WHERE resolution_fact_id = :id"),
            {"id": fact.resolution_fact_id},
        )
    assert authority == "NONE"
