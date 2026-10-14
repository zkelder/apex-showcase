"""PostgreSQL persistence proofs for Normalized Portfolio State v1: round
trip, idempotent replay, identity-conflict rejection, point-in-time
snapshot selection, and genuine end-to-end composition from the real
Crypto Spot PAPER and Kalshi PAPER repositories -- proving zero writes to
any Coinbase/Kalshi/Securities/options/corporate table along the way.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.adapters.kalshi_operating_postgres import PostgresKalshiOperatingRepository
from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
from apex.adapters.portfolio_state_sources import PostgresPortfolioStateSource
from apex.application.crypto_paper import CreateCryptoPaperAccount
from apex.application.kalshi_operating import CreateKalshiPaperAccount
from apex.application.portfolio_state import BuildPortfolioSnapshot, PortfolioSourceSelection
from apex.domain.portfolio_state import (
    EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
    AccountKind,
    PortfolioAccountBalance,
    PortfolioPosition,
    PortfolioStateError,
    PortfolioVenue,
    PositionExposure,
    build_portfolio_snapshot,
)

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _row_count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f'SELECT count(*) FROM "{table}"')).scalar_one()


def _balance(**overrides: object) -> PortfolioAccountBalance:
    values: dict[str, object] = {
        "venue": PortfolioVenue.COINBASE,
        "account_kind": AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        "account_id": "acct-1",
        "cash": "500",
        "reserved_cash": "0",
        "as_of": NOW,
        "captured_at": NOW,
        "evidence_class": EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        "source_record_type": "Test",
        "source_record_id": "src-1",
        "source_canonical_hash": "a" * 64,
    }
    values.update(overrides)
    return PortfolioAccountBalance.create(**values)  # type: ignore[arg-type]


def _position(**overrides: object) -> PortfolioPosition:
    values: dict[str, object] = {
        "venue": PortfolioVenue.COINBASE,
        "account_kind": AccountKind.APEX_INTERNAL_PAPER_SIMULATION,
        "account_id": "acct-1",
        "instrument_id": "BTC-USD",
        "market_family": "CRYPTO_SPOT",
        "exposure": PositionExposure.LONG,
        "raw_side": "SPOT_LONG",
        "quantity": "1",
        "cost_basis": "100",
        "opened_at": NOW,
        "as_of": NOW,
        "captured_at": NOW,
        "evidence_class": EVIDENCE_CLASS_APEX_SIMULATED_PAPER_LEDGER,
        "source_record_type": "Test",
        "source_record_id": "src-1",
        "source_canonical_hash": "b" * 64,
    }
    values.update(overrides)
    return PortfolioPosition.create(**values)  # type: ignore[arg-type]


def test_snapshot_round_trips_with_balances_and_positions(postgres_engine: Engine) -> None:
    repository = PostgresPortfolioSnapshotRepository(postgres_engine)
    balance = _balance()
    position = _position()
    snapshot = build_portfolio_snapshot(
        snapshot_id="snap-roundtrip",
        as_of=NOW,
        created_at=NOW,
        account_balances=(balance,),
        positions=(position,),
        unavailable_sources=(),
    )
    replay = repository.save_portfolio_snapshot(
        idempotency_key="key-roundtrip",
        snapshot=snapshot,
        balances=(balance,),
        positions=(position,),
    )
    assert replay is False

    fetched = repository.portfolio_snapshot("snap-roundtrip")
    assert fetched is not None
    assert fetched.canonical_hash == snapshot.canonical_hash
    fetched_balances = repository.portfolio_account_balances("snap-roundtrip")
    assert len(fetched_balances) == 1
    assert fetched_balances[0].canonical_hash == balance.canonical_hash
    fetched_positions = repository.portfolio_positions("snap-roundtrip")
    assert len(fetched_positions) == 1
    assert fetched_positions[0].canonical_hash == position.canonical_hash


def test_save_portfolio_snapshot_is_idempotent_on_identical_replay(postgres_engine: Engine) -> None:
    repository = PostgresPortfolioSnapshotRepository(postgres_engine)
    snapshot = build_portfolio_snapshot(
        snapshot_id="snap-idempotent",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    first = repository.save_portfolio_snapshot(
        idempotency_key="key-idem", snapshot=snapshot, balances=(), positions=()
    )
    second = repository.save_portfolio_snapshot(
        idempotency_key="key-idem", snapshot=snapshot, balances=(), positions=()
    )
    assert first is False
    assert second is True
    assert _row_count(postgres_engine, "portfolio_snapshot") == 1


def test_save_portfolio_snapshot_rejects_idempotency_key_reuse_with_different_content(
    postgres_engine: Engine,
) -> None:
    repository = PostgresPortfolioSnapshotRepository(postgres_engine)
    first_snapshot = build_portfolio_snapshot(
        snapshot_id="snap-a",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    repository.save_portfolio_snapshot(
        idempotency_key="shared-key", snapshot=first_snapshot, balances=(), positions=()
    )
    second_snapshot = build_portfolio_snapshot(
        snapshot_id="snap-b",
        as_of=NOW + timedelta(hours=1),
        created_at=NOW + timedelta(hours=1),
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    with pytest.raises(PortfolioStateError):
        repository.save_portfolio_snapshot(
            idempotency_key="shared-key", snapshot=second_snapshot, balances=(), positions=()
        )


def test_portfolio_snapshot_active_as_of_never_selects_a_future_snapshot(
    postgres_engine: Engine,
) -> None:
    repository = PostgresPortfolioSnapshotRepository(postgres_engine)
    early = build_portfolio_snapshot(
        snapshot_id="snap-early",
        as_of=NOW - timedelta(days=2),
        created_at=NOW - timedelta(days=2),
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    late = build_portfolio_snapshot(
        snapshot_id="snap-late",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    repository.save_portfolio_snapshot(
        idempotency_key="k1", snapshot=early, balances=(), positions=()
    )
    repository.save_portfolio_snapshot(
        idempotency_key="k2", snapshot=late, balances=(), positions=()
    )

    at_early_plus_one_hour = repository.portfolio_snapshot_active_as_of(
        NOW - timedelta(days=2) + timedelta(hours=1)
    )
    assert at_early_plus_one_hour is not None
    assert at_early_plus_one_hour.snapshot_id == "snap-early"

    at_now = repository.portfolio_snapshot_active_as_of(NOW)
    assert at_now is not None
    assert at_now.snapshot_id == "snap-late"

    before_either = repository.portfolio_snapshot_active_as_of(NOW - timedelta(days=10))
    assert before_either is None

    assert repository.latest_portfolio_snapshot().snapshot_id == "snap-late"


def test_latest_portfolio_snapshot_orders_by_created_at_not_as_of(
    postgres_engine: Engine,
) -> None:
    """Regression for the genuine ambiguity Recurring Operating Health &
    Unattended Evidence Closure v1 resolved: a manually-built snapshot with
    a later ``as_of`` (e.g. built with ``as_of=now()`` for convenience) must
    never shadow a snapshot genuinely produced afterward with an earlier,
    day-scoped ``as_of`` (exactly the recurring operating cycle's own
    identity). ``latest_portfolio_snapshot`` must reflect real creation
    order; ``portfolio_snapshot_active_as_of`` must stay exactly as before,
    genuinely economic-time-ordered."""

    repository = PostgresPortfolioSnapshotRepository(postgres_engine)
    midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    # Built first (earlier created_at == NOW) but carries a later as_of than
    # the cycle snapshot below (as_of == its own created_at, "now" at build
    # time) -- the exact real shape of a manual smoketest snapshot built
    # with ``as_of=datetime.now()``. ``created_at >= as_of`` always holds.
    manual_smoketest = build_portfolio_snapshot(
        snapshot_id="snap-manual-smoketest",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    # Built second (created_at an hour after the manual snapshot above) but
    # carries the cycle's own day-scoped-midnight as_of, which is *earlier*
    # than the manual snapshot's as_of -- exactly what genuinely happened in
    # the real 2026-08-10 acceptance run this milestone diagnosed.
    genuinely_later_cycle_snapshot = build_portfolio_snapshot(
        snapshot_id="snap-genuine-cycle",
        as_of=midnight,
        created_at=NOW + timedelta(hours=1),
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    repository.save_portfolio_snapshot(
        idempotency_key="k-manual", snapshot=manual_smoketest, balances=(), positions=()
    )
    repository.save_portfolio_snapshot(
        idempotency_key="k-cycle",
        snapshot=genuinely_later_cycle_snapshot,
        balances=(),
        positions=(),
    )

    # The genuinely later-created cycle snapshot wins, not the one with the
    # merely later as_of.
    latest = repository.latest_portfolio_snapshot()
    assert latest is not None
    assert latest.snapshot_id == "snap-genuine-cycle"

    # portfolio_snapshot_active_as_of keeps its own, deliberately different,
    # as_of-ordered contract -- unaffected by this fix.
    at_manual_as_of = repository.portfolio_snapshot_active_as_of(NOW)
    assert at_manual_as_of is not None
    assert at_manual_as_of.snapshot_id == "snap-manual-smoketest"


def test_latest_portfolio_snapshot_breaks_an_exact_created_at_tie_deterministically(
    postgres_engine: Engine,
) -> None:
    repository = PostgresPortfolioSnapshotRepository(postgres_engine)
    first = build_portfolio_snapshot(
        snapshot_id="snap-tie-a",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    second = build_portfolio_snapshot(
        snapshot_id="snap-tie-b",
        as_of=NOW,
        created_at=NOW,
        account_balances=(),
        positions=(),
        unavailable_sources=(),
    )
    repository.save_portfolio_snapshot(
        idempotency_key="k-tie-a", snapshot=first, balances=(), positions=()
    )
    repository.save_portfolio_snapshot(
        idempotency_key="k-tie-b", snapshot=second, balances=(), positions=()
    )
    expected = max("snap-tie-a", "snap-tie-b")
    latest = repository.latest_portfolio_snapshot()
    assert latest is not None
    assert latest.snapshot_id == expected


def test_build_portfolio_snapshot_composes_real_crypto_and_kalshi_accounts_end_to_end(
    postgres_engine: Engine,
) -> None:
    crypto_repository = PostgresCryptoPaperRepository(postgres_engine)
    CreateCryptoPaperAccount(crypto_repository).execute(
        account_id="crypto-e2e",
        opening_cash="750",
        created_at=NOW,
        idempotency_key="crypto-e2e-key",
    )
    kalshi_repository = PostgresKalshiOperatingRepository(postgres_engine)
    CreateKalshiPaperAccount(kalshi_repository).execute(
        account_id="kalshi-e2e",
        opening_cash="250",
        created_at=NOW,
        idempotency_key="kalshi-e2e-key",
    )

    service = BuildPortfolioSnapshot(
        PostgresPortfolioSnapshotRepository(postgres_engine),
        PostgresPortfolioStateSource(postgres_engine),
    )
    result = service.execute(
        snapshot_id="snap-e2e",
        as_of=NOW,
        created_at=NOW,
        idempotency_key="build-e2e-key",
        selection=PortfolioSourceSelection(
            crypto_account_ids=("crypto-e2e", "crypto-missing"),
            kalshi_account_ids=("kalshi-e2e",),
        ),
    )
    # "crypto-missing" makes one source genuinely unavailable, so the
    # aggregate total is conservatively unknown even though both real
    # accounts were found -- a partial sum is never reported as complete.
    assert result.snapshot.total_known_cash is None
    assert result.snapshot.completeness.value == "PARTIAL_UNKNOWN_SOURCES"
    assert any("crypto-missing" in source.reason for source in result.snapshot.unavailable_sources)
    venues = {balance.venue for balance in result.account_balances}
    assert venues == {PortfolioVenue.COINBASE, PortfolioVenue.KALSHI}
    cash_values = {balance.account_id: balance.cash for balance in result.account_balances}
    assert cash_values == {"crypto-e2e": 750, "kalshi-e2e": 250}

    # Idempotent replay of the identical build.
    replay_result = service.execute(
        snapshot_id="snap-e2e",
        as_of=NOW,
        created_at=NOW,
        idempotency_key="build-e2e-key",
        selection=PortfolioSourceSelection(
            crypto_account_ids=("crypto-e2e", "crypto-missing"),
            kalshi_account_ids=("kalshi-e2e",),
        ),
    )
    assert replay_result.idempotent_replay is True


def test_building_a_portfolio_snapshot_writes_no_crypto_kalshi_or_options_rows(
    postgres_engine: Engine,
) -> None:
    crypto_repository = PostgresCryptoPaperRepository(postgres_engine)
    CreateCryptoPaperAccount(crypto_repository).execute(
        account_id="crypto-untouched",
        opening_cash="100",
        created_at=NOW,
        idempotency_key="crypto-untouched-key",
    )
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
    service = BuildPortfolioSnapshot(
        PostgresPortfolioSnapshotRepository(postgres_engine),
        PostgresPortfolioStateSource(postgres_engine),
    )
    service.execute(
        snapshot_id="snap-no-writes",
        as_of=NOW,
        created_at=NOW,
        idempotency_key="no-writes-key",
        selection=PortfolioSourceSelection(crypto_account_ids=("crypto-untouched",)),
    )
    after = {
        table: _row_count(postgres_engine, table)
        for table in unrelated_tables
        if table in existing_tables
    }
    assert before == after


def test_every_new_row_carries_authority_none(postgres_engine: Engine) -> None:
    balance = _balance()
    position = _position()
    snapshot = build_portfolio_snapshot(
        snapshot_id="snap-authority",
        as_of=NOW,
        created_at=NOW,
        account_balances=(balance,),
        positions=(position,),
        unavailable_sources=(),
    )
    repository = PostgresPortfolioSnapshotRepository(postgres_engine)
    repository.save_portfolio_snapshot(
        idempotency_key="authority-key",
        snapshot=snapshot,
        balances=(balance,),
        positions=(position,),
    )
    for table in ("portfolio_snapshot", "portfolio_account_balance", "portfolio_position"):
        with postgres_engine.connect() as connection:
            values = (
                connection.execute(sa.text(f'SELECT DISTINCT authority FROM "{table}"'))
                .scalars()
                .all()
            )
        assert set(values) == {"NONE"}
