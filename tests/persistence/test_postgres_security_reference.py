from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.security_reference import (
    NASDAQ_LISTED_PATH,
    parse_nasdaq_symbol_directory,
    parse_sec_company_tickers,
)
from apex.adapters.security_reference_postgres import (
    SCHEMA_COMPATIBILITY_CURRENT,
    SCHEMA_COMPATIBILITY_INCOMPATIBLE,
    SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1,
    PostgresSecurityReferenceRepository,
)
from apex.application.security_reference import (
    CreateCurrentEquityUniverse,
    SecurityReferenceApplicationError,
)
from apex.domain.security_reference import (
    CurrentEquityUniversePolicy,
    ExecutionCapabilityState,
    reconcile_security_references,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 29, 15, tzinfo=UTC)
SECURITY_REFERENCE_TABLES = {
    "security_reference_identity",
    "security_reference_universe_snapshot",
}


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _reconciled() -> tuple[tuple[object, ...], tuple[str, str]]:
    nasdaq = parse_nasdaq_symbol_directory(
        payload=(ROOT / "fixtures" / "broad-research" / "nasdaqlisted.txt").read_bytes(),
        source_path=NASDAQ_LISTED_PATH,
        received_at=NOW,
    )
    sec = parse_sec_company_tickers(
        payload=(ROOT / "fixtures" / "broad-research" / "sec_company_tickers.json").read_bytes(),
        received_at=NOW,
        source_timestamp=NOW,
    )
    records = (*nasdaq.records, *sec.records)
    reconciled = reconcile_security_references(records)
    return reconciled, (nasdaq.receipt.receipt_id, sec.receipt.receipt_id)


def test_clean_database_is_at_current_head_with_security_reference_tables(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= SECURITY_REFERENCE_TABLES
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0118_evidence_gap_recovery"
        )


def test_universe_and_identity_evidence_survive_a_fresh_repository_instance(
    postgres_engine: Engine,
) -> None:
    reconciled, receipt_ids = _reconciled()
    repository = PostgresSecurityReferenceRepository(postgres_engine)
    snapshot = CreateCurrentEquityUniverse(repository).execute(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=receipt_ids,
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
        idempotency_key="universe-persistence-2026-07-29",
    )

    fresh = PostgresSecurityReferenceRepository(postgres_engine)
    assert fresh.universe_by_key("universe-persistence-2026-07-29") == snapshot
    assert fresh.universe(snapshot.universe_id) == snapshot
    assert snapshot in fresh.universes()
    # Research eligibility and provider execution capability are both
    # retained and both survive a fresh repository instance -- neither is
    # dropped or silently re-derived. No Alpaca evidence was merged into
    # this fixture, so every decision's execution capability is honestly
    # "not observed", independent of its research eligibility.
    reloaded = fresh.universe(snapshot.universe_id)
    assert reloaded is not None
    assert reloaded.research_eligible_count == snapshot.research_eligible_count
    assert reloaded.execution_capability_counts == snapshot.execution_capability_counts
    assert reloaded.execution_capability_unknown_count == reloaded.total_considered
    assert reloaded.execution_capability_known_count == 0
    for decision in reloaded.decisions:
        assert decision.execution_capability.value == "ALPACA_CAPABILITY_NOT_OBSERVED"
    for security in reconciled:
        assert fresh.identity(security.canonical_hash) == security
    acme = next(item for item in reconciled if item.current_symbol == "ACME")
    assert acme in fresh.identities_by_symbol("ACME")
    expected_review = tuple(
        sorted(
            (item for item in reconciled if item.requires_review),
            key=lambda item: item.canonical_hash,
        )
    )
    assert fresh.identities_requiring_review() == expected_review

    # Replaying the identical build is a free, non-duplicating idempotent read.
    replay = CreateCurrentEquityUniverse(fresh).execute(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=receipt_ids,
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
        idempotency_key="universe-persistence-2026-07-29",
    )
    assert replay == snapshot
    assert len(fresh.identities_by_symbol("ACME")) == 1

    with pytest.raises(
        SecurityReferenceApplicationError, match="BROAD_UNIVERSE_IDEMPOTENCY_CONFLICT"
    ):
        CreateCurrentEquityUniverse(fresh).execute(
            securities=reconciled,
            policy=CurrentEquityUniversePolicy.initial(),
            source_receipt_ids=receipt_ids,
            as_of_date=date(2026, 7, 30),
            created_at=NOW,
            idempotency_key="universe-persistence-2026-07-29",
        )


LEGACY_UNIVERSE_PAYLOAD = {
    "universe_id": "broad_equity_universe_legacy0000000000000",
    "policy_id": "CURRENT_LIQUID_US_EQUITY_UNIVERSE_V1",
    "policy_hash": "0" * 64,
    "as_of_date": "2026-07-28",
    "created_at": "2026-07-28T09:00:00Z",
    "source_receipt_ids": [],
    "total_considered": 1,
    "security_type_counts": [["COMMON_STOCK", 1]],
    "eligible_count": 0,
    "exclusion_counts": [["NOT_CONFIRMED_ALPACA_TRADABLE", 1]],
    "decisions": [
        {
            "canonical_security_id": "apex_security_legacy00000000000000000000",
            "current_symbol": "LEGACY",
            "eligible": False,
            "reasons": ["NOT_CONFIRMED_ALPACA_TRADABLE"],
            "security_hash": "1" * 64,
        }
    ],
    "provider_disagreements": [],
    "warning": "CURRENT-LISTED UNIVERSE — NOT HISTORICAL POINT-IN-TIME MEMBERSHIP",
    "canonical_hash": "2" * 64,
}


def _insert_raw_universe_row(
    engine: Engine,
    *,
    idempotency_key: str,
    universe_id: str,
    policy_id: str,
    policy_hash: str,
    as_of_date_value: date,
    total_considered: int,
    eligible_count: int,
    created_at: datetime,
    canonical_hash: str,
    payload: dict[str, object],
) -> None:
    """Insert a row directly by SQL, bypassing the repository's own
    ``save_universe`` -- exactly how a row written by an earlier code
    version, using an earlier payload shape, is genuinely retained today.
    """
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO security_reference_universe_snapshot "
                "(idempotency_key, universe_id, policy_id, policy_hash, as_of_date, "
                "total_considered, eligible_count, created_at, canonical_hash, payload, authority) "
                "VALUES (:idempotency_key, :universe_id, :policy_id, :policy_hash, :as_of_date, "
                ":total_considered, :eligible_count, :created_at, :canonical_hash, "
                ":payload, 'NONE')"
            ),
            {
                "idempotency_key": idempotency_key,
                "universe_id": universe_id,
                "policy_id": policy_id,
                "policy_hash": policy_hash,
                "as_of_date": as_of_date_value,
                "total_considered": total_considered,
                "eligible_count": eligible_count,
                "created_at": created_at,
                "canonical_hash": canonical_hash,
                "payload": json.dumps(payload),
            },
        )


def test_legacy_pre_separation_universe_snapshot_rehydrates_without_dropping_or_rewriting_it(
    postgres_engine: Engine,
) -> None:
    """A universe_snapshot row retained before research/execution separation
    carries no ``research_eligible``/``execution_capability`` field under any
    name. It must stay exactly as retained -- immutable, durable, never
    deleted, mutated, or backfilled with inferred values -- while still being
    genuinely readable through every current-shape read method instead of
    silently disappearing from them.
    """
    reconciled, receipt_ids = _reconciled()
    repository = PostgresSecurityReferenceRepository(postgres_engine)
    genuine = CreateCurrentEquityUniverse(repository).execute(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=receipt_ids,
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
        idempotency_key="universe-current-shape-2026-07-29",
    )
    _insert_raw_universe_row(
        postgres_engine,
        idempotency_key="universe-legacy-pre-separation",
        universe_id=LEGACY_UNIVERSE_PAYLOAD["universe_id"],
        policy_id=LEGACY_UNIVERSE_PAYLOAD["policy_id"],
        policy_hash=LEGACY_UNIVERSE_PAYLOAD["policy_hash"],
        as_of_date_value=date(2026, 7, 28),
        total_considered=1,
        eligible_count=0,
        created_at=datetime(2026, 7, 28, 9, tzinfo=UTC),
        canonical_hash=LEGACY_UNIVERSE_PAYLOAD["canonical_hash"],
        payload=LEGACY_UNIVERSE_PAYLOAD,
    )

    fresh = PostgresSecurityReferenceRepository(postgres_engine)

    # The legacy row is provably untouched at the SQL level: exactly one row,
    # its stored payload text byte-for-byte the same as what was inserted.
    with postgres_engine.connect() as connection:
        row = connection.execute(
            sa.text(
                "SELECT count(*) AS row_count, "
                "max(payload) FILTER (WHERE universe_id = :id) AS stored_payload "
                "FROM security_reference_universe_snapshot WHERE universe_id = :id"
            ),
            {"id": LEGACY_UNIVERSE_PAYLOAD["universe_id"]},
        ).one()
        assert row.row_count == 1
        assert json.loads(row.stored_payload) == LEGACY_UNIVERSE_PAYLOAD

    # It is genuinely rehydrated -- not skipped -- by every current-shape read
    # method, both by exact universe_id and by its own idempotency key.
    by_id = fresh.universe(LEGACY_UNIVERSE_PAYLOAD["universe_id"])
    by_key = fresh.universe_by_key("universe-legacy-pre-separation")
    assert by_id is not None
    assert by_id == by_key

    # The original research inclusion/exclusion result and its reasons are
    # preserved exactly -- never recomputed against today's policy.
    assert by_id.research_eligible_count == 0
    assert by_id.research_exclusion_counts == (("NOT_CONFIRMED_ALPACA_TRADABLE", 1),)
    # No positive or negative Alpaca capability is invented.
    assert by_id.execution_capability_counts == (
        (ExecutionCapabilityState.LEGACY_EXECUTION_CAPABILITY_UNKNOWN.value, 1),
    )
    assert by_id.execution_capability_known_count == 0
    assert by_id.execution_capability_unknown_count == 1
    decision = by_id.decisions[0]
    assert (
        decision.execution_capability
        is ExecutionCapabilityState.LEGACY_EXECUTION_CAPABILITY_UNKNOWN
    )
    # Exact snapshot ID, member IDs, hashes, timestamps, and source lineage.
    assert by_id.universe_id == LEGACY_UNIVERSE_PAYLOAD["universe_id"]
    assert by_id.canonical_hash == LEGACY_UNIVERSE_PAYLOAD["canonical_hash"]
    assert by_id.created_at == datetime(2026, 7, 28, 9, tzinfo=UTC)
    assert (
        decision.canonical_security_id
        == LEGACY_UNIVERSE_PAYLOAD["decisions"][0]["canonical_security_id"]
    )
    assert decision.security_hash == LEGACY_UNIVERSE_PAYLOAD["decisions"][0]["security_hash"]

    # Mixed old/new listings retain every row -- repository counts never
    # silently shrink relative to what is actually retained.
    universes = fresh.universes()
    assert genuine in universes
    assert by_id in universes
    assert len(universes) == 2

    evidence = fresh.universe_evidence()
    assert len(evidence) == 2
    by_universe_id = {item.universe_id: item for item in evidence}
    assert by_universe_id[genuine.universe_id].schema_compatibility == SCHEMA_COMPATIBILITY_CURRENT
    assert (
        by_universe_id[LEGACY_UNIVERSE_PAYLOAD["universe_id"]].schema_compatibility
        == SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1
    )
    assert all(item.compatibility_blocker is None for item in evidence)
    assert all(item.snapshot is not None for item in evidence)

    # Replay is deterministic: a second fresh repository instance rehydrates
    # byte-identical results.
    again = PostgresSecurityReferenceRepository(postgres_engine)
    assert again.universe(LEGACY_UNIVERSE_PAYLOAD["universe_id"]) == by_id
    assert again.universe_evidence() == evidence


def test_retained_incompatible_universe_snapshot_is_visible_not_dropped_or_crashing(
    postgres_engine: Engine,
) -> None:
    """A row whose payload matches neither the current nor the one known
    legacy schema must never crash a repository-wide read, and must never be
    omitted from the evidence that reports it exists.
    """
    reconciled, receipt_ids = _reconciled()
    repository = PostgresSecurityReferenceRepository(postgres_engine)
    genuine = CreateCurrentEquityUniverse(repository).execute(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=receipt_ids,
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
        idempotency_key="universe-current-shape-incompatible-check-2026-07-29",
    )
    _insert_raw_universe_row(
        postgres_engine,
        idempotency_key="universe-truly-incompatible",
        universe_id="broad_equity_universe_mystery0000000000000",
        policy_id="UNKNOWN_POLICY",
        policy_hash="9" * 64,
        as_of_date_value=date(2026, 7, 27),
        total_considered=0,
        eligible_count=0,
        created_at=datetime(2026, 7, 27, 9, tzinfo=UTC),
        canonical_hash="8" * 64,
        payload={"universe_id": "broad_equity_universe_mystery0000000000000", "shape": "unknown"},
    )

    fresh = PostgresSecurityReferenceRepository(postgres_engine)

    # It never crashes a repository-wide read...
    universes = fresh.universes()
    evidence = fresh.universe_evidence()

    # ...and it is not silently omitted: the repository-level evidence method
    # reports both retained rows, but only the rehydratable one comes back as
    # a typed snapshot.
    assert len(evidence) == 2
    assert len(universes) == 1
    assert genuine in universes

    incompatible = next(
        item
        for item in evidence
        if item.universe_id == "broad_equity_universe_mystery0000000000000"
    )
    assert incompatible.schema_compatibility == SCHEMA_COMPATIBILITY_INCOMPATIBLE
    assert incompatible.snapshot is None
    assert incompatible.idempotency_key == "universe-truly-incompatible"
    assert incompatible.compatibility_blocker

    # The row itself is untouched -- retained exactly as inserted.
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM security_reference_universe_snapshot "
                    "WHERE idempotency_key = :key"
                ),
                {"key": "universe-truly-incompatible"},
            )
            == 1
        )


def test_universe_snapshot_rows_are_immutable_after_commit(postgres_engine: Engine) -> None:
    reconciled, receipt_ids = _reconciled()
    repository = PostgresSecurityReferenceRepository(postgres_engine)
    CreateCurrentEquityUniverse(repository).execute(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=receipt_ids,
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
        idempotency_key="universe-snapshot-immutability-2026-07-29",
    )
    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text("UPDATE security_reference_universe_snapshot SET eligible_count = 999")
        )


def test_retained_identity_rows_are_immutable_after_commit(postgres_engine: Engine) -> None:
    reconciled, receipt_ids = _reconciled()
    repository = PostgresSecurityReferenceRepository(postgres_engine)
    CreateCurrentEquityUniverse(repository).execute(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=receipt_ids,
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
        idempotency_key="universe-immutability-2026-07-29",
    )
    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(sa.text("UPDATE security_reference_identity SET requires_review = true"))


def test_retained_broad_market_evidence_blocks_destructive_0075_downgrade(
    postgres_engine: Engine,
) -> None:
    reconciled, receipt_ids = _reconciled()
    repository = PostgresSecurityReferenceRepository(postgres_engine)
    snapshot = CreateCurrentEquityUniverse(repository).execute(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=receipt_ids,
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
        idempotency_key="universe-downgrade-guard-2026-07-29",
    )
    tables_before = tuple(sorted(sa.inspect(postgres_engine).get_table_names()))
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0075 with retained security_reference_identity evidence",
        ):
            command.downgrade(_config(connection), "0074_paper_activation")
        connection.rollback()
    # Alembic commits each revision's downgrade separately: 0076's own
    # downgrade (unrelated to this security_reference guard) already
    # succeeded and committed before 0075's guard fired and rolled back only
    # its own step, so the database genuinely rests at 0075 here -- not back
    # at head yet.
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0075_broad_market_research"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0118_evidence_gap_recovery"
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM security_reference_universe_snapshot "
                    "WHERE universe_id = :universe_id"
                ),
                {"universe_id": snapshot.universe_id},
            )
            == 1
        )
    assert tuple(sorted(sa.inspect(postgres_engine).get_table_names())) == tables_before
