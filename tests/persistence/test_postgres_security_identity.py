from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.security_identity_postgres import PostgresSecurityIdentityRepository
from apex.adapters.security_reference_postgres import PostgresSecurityReferenceRepository
from apex.domain.options_intelligence import (
    ExerciseStyle,
    OptionContract,
    OptionType,
    SettlementStyle,
    UnderlyingLinkOutcome,
)
from apex.domain.security_identity import (
    IdentityResolution,
    OldIdentityKind,
    OptionUnderlyingStableLinkage,
    StableSecurityIdentity,
)
from apex.domain.security_reference import (
    EvidenceState,
    SecurityReference,
    SecurityType,
    reconcile_security_references,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 6, 15, tzinfo=UTC)
SECURITY_IDENTITY_TABLES = {
    "security_stable_identity",
    "security_identity_resolution",
    "options_underlying_stable_linkage",
}


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _record(
    symbol: str, provider: str, *, cik: str | None = None, exchange: str = ""
) -> SecurityReference:
    return SecurityReference.create(
        current_symbol=symbol,
        provider_symbol=symbol,
        issuer_name=f"{symbol} Corp",
        listing_exchange=exchange,
        listing_category="X",
        security_type=SecurityType.UNKNOWN,
        common_stock=EvidenceState.UNKNOWN,
        etf=EvidenceState.UNKNOWN,
        adr=EvidenceState.UNKNOWN,
        reit=EvidenceState.UNKNOWN,
        test_issue=EvidenceState.NO,
        listing_status="NORMAL",
        round_lot_size=None,
        tradable=EvidenceState.UNKNOWN,
        shortable=EvidenceState.UNKNOWN,
        marginable=EvidenceState.UNKNOWN,
        active=EvidenceState.YES,
        cik=cik,
        source_provider=provider,
        source_receipt_id="receipt-1",
        source_timestamp=NOW,
        source_payload_hash="0" * 64,
    )


def _stable_identity(
    engine: Engine, symbol: str = "A", cik: str | None = "1090872"
) -> StableSecurityIdentity:
    """Build a stable identity AND durably persist the legacy reconciled
    identity it derives from -- ``security_stable_identity.source_identity_hash``
    carries a real foreign key onto ``security_reference_identity.canonical_hash``
    (see ``BuildStableIdentityProjection``'s docstring), so every genuine
    stable identity has a backing legacy row by construction. Bypassing that
    here (deriving without ever persisting the legacy row) would test an
    identity shape this milestone's own application service never produces.
    """
    reconciled = reconcile_security_references((_record(symbol, "SEC_EDGAR", cik=cik),))[0]
    PostgresSecurityReferenceRepository(engine).save_identities((reconciled,), recorded_at=NOW)
    return StableSecurityIdentity.derive(reconciled)


def test_clean_database_is_at_current_head_with_security_identity_tables(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= SECURITY_IDENTITY_TABLES
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0118_evidence_gap_recovery"
        )


def test_stable_identity_and_resolution_survive_a_fresh_repository_instance(
    postgres_engine: Engine,
) -> None:
    repository = PostgresSecurityIdentityRepository(postgres_engine)
    stable = _stable_identity(postgres_engine)
    repository.save_stable_identities((stable,), recorded_at=NOW)
    resolution = IdentityResolution.create(
        old_identity_id="apex_security_legacy_old",
        old_identity_kind=OldIdentityKind.LEGACY_CANONICAL_SECURITY_ID,
        resolved_stable_security_id=stable.stable_security_id,
        resolution_state="ALIAS",
        automatic=True,
        resolution_method="DIRECT_RECONCILED_CIK_AND_SYMBOL",
        evidence=("hash-1",),
    )
    repository.save_resolutions((resolution,), recorded_at=NOW)

    fresh = PostgresSecurityIdentityRepository(postgres_engine)
    assert fresh.stable_identity(stable.canonical_hash) == stable
    assert fresh.stable_identities_by_stable_id(stable.stable_security_id) == (stable,)
    assert fresh.latest_stable_identities() == (stable,)
    assert fresh.stable_identity_count() == 1
    assert fresh.resolutions_for_old_id("apex_security_legacy_old") == (resolution,)
    assert fresh.resolutions_to_stable_id(stable.stable_security_id) == (resolution,)
    assert fresh.resolution_count() == 1
    assert dict(fresh.resolution_state_counts()) == {"ALIAS": 1}


def test_stable_identity_save_is_idempotent_by_canonical_hash(postgres_engine: Engine) -> None:
    repository = PostgresSecurityIdentityRepository(postgres_engine)
    stable = _stable_identity(postgres_engine)
    repository.save_stable_identities((stable,), recorded_at=NOW)
    repository.save_stable_identities((stable,), recorded_at=NOW)  # exact replay
    assert repository.stable_identity_count() == 1
    assert len(repository.stable_identities_by_stable_id(stable.stable_security_id)) == 1


def test_alias_and_current_resolutions_round_trip_and_resolve_both_directions(
    postgres_engine: Engine,
) -> None:
    """The concrete backward-compatibility proof at the database boundary:
    an old (ALIAS) legacy id and the current legacy id both resolve to the
    identical stable id, and the reverse lookup finds both."""
    repository = PostgresSecurityIdentityRepository(postgres_engine)
    security_repository = PostgresSecurityReferenceRepository(postgres_engine)
    old = reconcile_security_references((_record("A", "SEC_EDGAR", cik="1090872"),))[0]
    new = reconcile_security_references(
        (
            _record("A", "SEC_EDGAR", cik="1090872"),
            _record("A", "NASDAQ_TRADER", exchange="XNYS"),
        )
    )[0]
    assert old.canonical_security_id != new.canonical_security_id
    security_repository.save_identities((old, new), recorded_at=NOW)
    stable = StableSecurityIdentity.derive(new)
    repository.save_stable_identities((stable,), recorded_at=NOW)
    alias = IdentityResolution.create(
        old_identity_id=old.canonical_security_id,
        old_identity_kind=OldIdentityKind.LEGACY_CANONICAL_SECURITY_ID,
        resolved_stable_security_id=stable.stable_security_id,
        resolution_state="ALIAS",
        automatic=True,
        resolution_method="DIRECT_RECONCILED_CIK_AND_SYMBOL",
        evidence=(old.canonical_hash,),
    )
    current = IdentityResolution.create(
        old_identity_id=new.canonical_security_id,
        old_identity_kind=OldIdentityKind.LEGACY_CANONICAL_SECURITY_ID,
        resolved_stable_security_id=stable.stable_security_id,
        resolution_state="CURRENT",
        automatic=True,
        resolution_method="DIRECT_RECONCILED_CIK_AND_SYMBOL",
        evidence=(new.canonical_hash,),
    )
    repository.save_resolutions((alias, current), recorded_at=NOW)

    fresh = PostgresSecurityIdentityRepository(postgres_engine)
    assert (
        fresh.resolutions_for_old_id(old.canonical_security_id)[0].resolution_state.value == "ALIAS"
    )
    assert (
        fresh.resolutions_for_old_id(new.canonical_security_id)[0].resolution_state.value
        == "CURRENT"
    )
    reverse = fresh.resolutions_to_stable_id(stable.stable_security_id)
    assert {item.old_identity_id for item in reverse} == {
        old.canonical_security_id,
        new.canonical_security_id,
    }


def test_ambiguous_resolutions_are_bounded_and_never_automatic(postgres_engine: Engine) -> None:
    repository = PostgresSecurityIdentityRepository(postgres_engine)
    ambiguous_rows = tuple(
        IdentityResolution.create(
            old_identity_id=f"apex_stable_security_old_{index}",
            old_identity_kind=OldIdentityKind.STABLE_SECURITY_ID,
            resolved_stable_security_id=f"apex_stable_security_new_{index}",
            resolution_state="AMBIGUOUS_REQUIRES_REVIEW",
            automatic=False,
            resolution_method="MULTIPLE_CANDIDATE_PREDECESSORS",
            evidence=(f"hash-{index}",),
        )
        for index in range(5)
    )
    repository.save_resolutions(ambiguous_rows, recorded_at=NOW)
    bounded = repository.ambiguous_resolutions(limit=3)
    assert len(bounded) == 3
    assert all(not item.automatic for item in bounded)
    assert all(item.resolution_state.value == "AMBIGUOUS_REQUIRES_REVIEW" for item in bounded)


def _option_contract(root: str, underlying: str, historical_id: str) -> OptionContract:
    return OptionContract.create(
        provider_contract_id="|".join((root, "C", "2026-09-18", "100", underlying)),
        underlying_canonical_security_id=historical_id,
        standardized_symbol=None,
        option_type=OptionType.CALL,
        strike="100",
        expiration="2026-09-18",
        exercise_style=ExerciseStyle.UNKNOWN,
        settlement_style=SettlementStyle.UNKNOWN,
        multiplier="100",
        deliverable=None,
        listing_exchange=None,
        state="CURRENT",
        contract_size_state="STANDARD_ASSUMPTION_UNCONFIRMED",
        adjustment_state="UNKNOWN",
        source_provider="NASDAQ_OPTIONS_DIRECTORY",
        reference_receipt_ids=("r1",),
    )


def test_option_linkage_persists_and_scopes_to_its_own_run(postgres_engine: Engine) -> None:
    repository = PostgresSecurityIdentityRepository(postgres_engine)
    linkage = OptionUnderlyingStableLinkage.create(
        raw_underlying_symbol="META",
        outcome=UnderlyingLinkOutcome.LINKED,
        resolved_stable_security_id="apex_stable_security_meta",
        candidate_stable_security_ids=("apex_stable_security_meta",),
        historical_underlying_security_id="legacy-old-fb-id",
        reasons=("EXACT_SYMBOL_MATCH",),
        contract_count=42,
        linkage_run_id="run-1",
    )
    repository.save_option_linkages((linkage,), recorded_at=NOW)

    fresh = PostgresSecurityIdentityRepository(postgres_engine)
    assert fresh.option_linkages_for_run("run-1") == (linkage,)
    assert fresh.latest_option_linkage_run_id() == "run-1"
    assert fresh.option_linkage_run_ids() == ("run-1",)
    assert fresh.option_linkage("META") == linkage
    assert fresh.option_linkages_for_run("nonexistent-run") == ()


def test_option_linkage_save_is_idempotent(postgres_engine: Engine) -> None:
    repository = PostgresSecurityIdentityRepository(postgres_engine)
    linkage = OptionUnderlyingStableLinkage.create(
        raw_underlying_symbol="GOEVQ",
        outcome=UnderlyingLinkOutcome.UNLINKED,
        resolved_stable_security_id=None,
        candidate_stable_security_ids=(),
        historical_underlying_security_id=None,
        reasons=("NO_MATCHING_RECONCILED_IDENTITY",),
        contract_count=10,
        linkage_run_id="run-1",
    )
    repository.save_option_linkages((linkage,), recorded_at=NOW)
    repository.save_option_linkages((linkage,), recorded_at=NOW)
    assert len(repository.option_linkages_for_run("run-1")) == 1


def test_retained_security_identity_evidence_blocks_destructive_0076_downgrade(
    postgres_engine: Engine,
) -> None:
    repository = PostgresSecurityIdentityRepository(postgres_engine)
    repository.save_stable_identities((_stable_identity(postgres_engine),), recorded_at=NOW)
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0076 with retained security_stable_identity evidence",
        ):
            command.downgrade(_config(connection), "0075_broad_market_research")
        connection.rollback()
    with postgres_engine.connect() as connection:
        # Empirically (not 0077, the overall head): downgrading from head
        # through two revisions -- 0077's own (unblocked, no historical
        # market-data evidence exists here) then 0076's (blocked) -- leaves
        # 0077's already-applied downgrade committed and only 0076's failed
        # step rolled back. The final `upgrade(..., "head")` below restores
        # 0077 regardless, so later tests still see a fully-migrated schema.
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0076_stable_security_identity"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")


def test_0076_empty_downgrade_reupgrades_cleanly(postgres_engine: Engine) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= SECURITY_IDENTITY_TABLES
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0075_broad_market_research")
    assert SECURITY_IDENTITY_TABLES.isdisjoint(sa.inspect(postgres_engine).get_table_names())
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
    assert set(sa.inspect(postgres_engine).get_table_names()) >= SECURITY_IDENTITY_TABLES


def test_stable_identity_rows_are_immutable_at_the_database_boundary(
    postgres_engine: Engine,
) -> None:
    repository = PostgresSecurityIdentityRepository(postgres_engine)
    stable = _stable_identity(postgres_engine)
    repository.save_stable_identities((stable,), recorded_at=NOW)
    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="immutable Apex row cannot be changed"):
            connection.execute(
                sa.text(
                    "UPDATE security_stable_identity SET review_required = true "
                    "WHERE canonical_hash = :hash"
                ),
                {"hash": stable.canonical_hash},
            )
        connection.rollback()
    with postgres_engine.connect() as connection:
        with pytest.raises(Exception, match="immutable Apex row cannot be changed"):
            connection.execute(
                sa.text("DELETE FROM security_stable_identity WHERE canonical_hash = :hash"),
                {"hash": stable.canonical_hash},
            )
        connection.rollback()
