"""Orchestration-level proofs for the stable-identity/options-relink
application services: idempotent replay, conflict rejection, mixed
historical/current identity coexistence, and backward-compatible resolution
across two universe builds -- all against in-memory repositories, mirroring
`InMemorySecurityReferenceRepository`'s existing role for
`CreateCurrentEquityUniverse`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from apex.application.security_identity import (
    BuildStableIdentityProjection,
    InMemorySecurityIdentityRepository,
    RelinkOptionContracts,
    ResolveLegacyIdentities,
    SecurityIdentityApplicationError,
)
from apex.application.security_reference import (
    CreateCurrentEquityUniverse,
    InMemorySecurityReferenceRepository,
)
from apex.domain.options_intelligence import (
    ExerciseStyle,
    OptionContract,
    OptionType,
    SettlementStyle,
    UnderlyingLinkOutcome,
)
from apex.domain.security_reference import (
    CurrentEquityUniversePolicy,
    EvidenceState,
    SecurityReference,
    SecurityType,
    reconcile_security_references,
)

NOW = datetime(2026, 8, 6, tzinfo=UTC)
POLICY = CurrentEquityUniversePolicy.initial()


def _record(
    symbol: str,
    provider: str,
    *,
    cik: str | None = None,
    name: str | None = None,
    exchange: str = "",
) -> SecurityReference:
    return SecurityReference.create(
        current_symbol=symbol,
        provider_symbol=symbol,
        issuer_name=name or f"{symbol} Corp",
        listing_exchange=exchange,
        listing_category="X",
        security_type=SecurityType.COMMON_STOCK if exchange else SecurityType.UNKNOWN,
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


def _repos() -> tuple[InMemorySecurityReferenceRepository, InMemorySecurityIdentityRepository]:
    return InMemorySecurityReferenceRepository(), InMemorySecurityIdentityRepository()


def test_build_stable_identity_projection_is_idempotent_by_key() -> None:
    security_repo, identity_repo = _repos()
    reconciled = reconcile_security_references((_record("A", "SEC_EDGAR", cik="1090872"),))
    service = BuildStableIdentityProjection(
        security_repository=security_repo, identity_repository=identity_repo
    )
    first = service.execute(
        securities=reconciled,
        policy=POLICY,
        source_receipt_ids=("r1",),
        raw_source_record_count=1,
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
        idempotency_key="key-1",
    )
    second = service.execute(
        securities=reconciled,
        policy=POLICY,
        source_receipt_ids=("r1",),
        raw_source_record_count=1,
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
        idempotency_key="key-1",
    )
    assert first.universe.universe_id == second.universe.universe_id
    assert first.universe.canonical_hash == second.universe.canonical_hash
    # Exact replay never duplicates persisted identity/resolution rows.
    assert identity_repo.stable_identity_count() == 1
    assert identity_repo.resolution_count() == 0  # no prior run -> nothing superseded/ambiguous


def test_build_stable_identity_projection_rejects_conflicting_replay() -> None:
    security_repo, identity_repo = _repos()
    service = BuildStableIdentityProjection(
        security_repository=security_repo, identity_repository=identity_repo
    )
    reconciled_a = reconcile_security_references((_record("A", "SEC_EDGAR", cik="1090872"),))
    service.execute(
        securities=reconciled_a,
        policy=POLICY,
        source_receipt_ids=("r1",),
        raw_source_record_count=1,
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
        idempotency_key="key-1",
    )
    reconciled_b = reconcile_security_references((_record("B", "SEC_EDGAR", cik="2222222"),))
    with pytest.raises(SecurityIdentityApplicationError):
        service.execute(
            securities=reconciled_b,
            policy=POLICY,
            source_receipt_ids=("r1",),
            raw_source_record_count=1,
            as_of_date=date(2026, 8, 6),
            created_at=NOW,
            idempotency_key="key-1",
        )


def test_second_stable_build_detects_supersession_against_first() -> None:
    """The full multi-run lifecycle: build once, rename a security, build
    again -- the resolution ledger records the automatic SUPERSEDED lineage,
    and stable_identity_count reflects only currently-live identities."""
    security_repo, identity_repo = _repos()
    service = BuildStableIdentityProjection(
        security_repository=security_repo, identity_repository=identity_repo
    )
    fb = reconcile_security_references((_record("FB", "SEC_EDGAR", cik="1326801"),))
    service.execute(
        securities=fb,
        policy=POLICY,
        source_receipt_ids=("r1",),
        raw_source_record_count=1,
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
        idempotency_key="run-1",
    )
    meta = reconcile_security_references((_record("META", "SEC_EDGAR", cik="1326801"),))
    result = service.execute(
        securities=meta,
        policy=POLICY,
        source_receipt_ids=("r1",),
        raw_source_record_count=1,
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
        idempotency_key="run-2",
    )
    assert result.superseded_count == 1
    superseded = [r for r in result.resolutions if r.resolution_state.value == "SUPERSEDED"]
    assert len(superseded) == 1
    assert superseded[0].resolution_method == "CIK_MATCH_SYMBOL_CHANGED"


def test_resolve_legacy_identities_handles_mixed_historical_and_current_rows() -> None:
    """Two universe builds retained side by side: the older build's legacy
    id resolves ALIAS, the newer/current build's resolves CURRENT, and both
    point at the identical stable id -- proving mixed historical/current
    identity coexistence resolves correctly."""
    security_repo, identity_repo = _repos()
    old_reconciled = reconcile_security_references((_record("A", "SEC_EDGAR", cik="1090872"),))
    CreateCurrentEquityUniverse(security_repo).execute(
        securities=old_reconciled,
        policy=POLICY,
        source_receipt_ids=("r1",),
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
        idempotency_key="legacy-old",
    )
    new_reconciled = reconcile_security_references(
        (
            _record("A", "SEC_EDGAR", cik="1090872"),
            _record("A", "NASDAQ_TRADER", name="Agilent Technologies", exchange="XNYS"),
        )
    )
    CreateCurrentEquityUniverse(security_repo).execute(
        securities=new_reconciled,
        policy=POLICY,
        source_receipt_ids=("r1",),
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
        idempotency_key="legacy-new",
    )
    assert old_reconciled[0].canonical_security_id != new_reconciled[0].canonical_security_id

    projection = BuildStableIdentityProjection(
        security_repository=security_repo, identity_repository=identity_repo
    ).execute(
        securities=new_reconciled,
        policy=POLICY,
        source_receipt_ids=("r1",),
        raw_source_record_count=2,
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
        idempotency_key="stable-1",
    )
    result = ResolveLegacyIdentities(
        security_repository=security_repo, identity_repository=identity_repo
    ).execute(latest_securities=new_reconciled, recorded_at=NOW)

    assert result.legacy_identity_count == 2
    assert result.current_count == 1
    assert result.alias_count == 1
    stable_id = projection.identities[0].stable_security_id
    old_resolution = identity_repo.resolutions_for_old_id(old_reconciled[0].canonical_security_id)
    new_resolution = identity_repo.resolutions_for_old_id(new_reconciled[0].canonical_security_id)
    assert old_resolution[0].resolution_state.value == "ALIAS"
    assert old_resolution[0].resolved_stable_security_id == stable_id
    assert new_resolution[0].resolution_state.value == "CURRENT"
    assert new_resolution[0].resolved_stable_security_id == stable_id
    # Reverse lookup: every historical identity id referring to this security.
    aliases = identity_repo.resolutions_to_stable_id(stable_id)
    assert {item.old_identity_id for item in aliases} == {
        old_reconciled[0].canonical_security_id,
        new_reconciled[0].canonical_security_id,
    }


def test_resolve_legacy_identities_is_idempotent_on_repeat_sweep() -> None:
    security_repo, identity_repo = _repos()
    reconciled = reconcile_security_references((_record("A", "SEC_EDGAR", cik="1090872"),))
    CreateCurrentEquityUniverse(security_repo).execute(
        securities=reconciled,
        policy=POLICY,
        source_receipt_ids=("r1",),
        as_of_date=date(2026, 8, 6),
        created_at=NOW,
        idempotency_key="legacy-1",
    )
    service = ResolveLegacyIdentities(
        security_repository=security_repo, identity_repository=identity_repo
    )
    service.execute(latest_securities=reconciled, recorded_at=NOW)
    service.execute(latest_securities=reconciled, recorded_at=NOW)
    assert identity_repo.resolution_count() == 1  # not duplicated on repeat sweep


def _contract(root: str, underlying: str, historical_id: str) -> OptionContract:
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


def test_relink_option_contracts_application_service_persists_linkages() -> None:
    identity_repo = InMemorySecurityIdentityRepository()
    contract = _contract("META", "META", "legacy-old-id")
    linkages = RelinkOptionContracts(identity_repository=identity_repo).execute(
        contracts=(contract,),
        previously_rejected=(("GOEVQ", 5),),
        stable_symbol_index={"META": "apex_stable_security_meta"},
        linkage_run_id="run-1",
        recorded_at=NOW,
    )
    assert len(linkages) == 2
    persisted = identity_repo.option_linkages_for_run("run-1")
    assert len(persisted) == 2
    assert identity_repo.latest_option_linkage_run_id() == "run-1"
    linked = next(item for item in persisted if item.raw_underlying_symbol == "META")
    assert linked.outcome is UnderlyingLinkOutcome.LINKED
    unlinked = next(item for item in persisted if item.raw_underlying_symbol == "GOEVQ")
    assert unlinked.outcome is UnderlyingLinkOutcome.UNLINKED


def test_relink_option_contracts_current_universe_scoping_excludes_stale_run() -> None:
    """Historical linkage from an older run must never be counted as
    current: option_linkages_for_run scopes strictly to one linkage_run_id."""
    identity_repo = InMemorySecurityIdentityRepository()
    old_contract = _contract("OLD", "OLD", "legacy-old")
    RelinkOptionContracts(identity_repository=identity_repo).execute(
        contracts=(old_contract,),
        previously_rejected=(),
        stable_symbol_index={"OLD": "apex_stable_security_old"},
        linkage_run_id="run-1",
        recorded_at=NOW,
    )
    new_contract = _contract("NEW", "NEW", "legacy-new")
    RelinkOptionContracts(identity_repository=identity_repo).execute(
        contracts=(new_contract,),
        previously_rejected=(),
        stable_symbol_index={"NEW": "apex_stable_security_new"},
        linkage_run_id="run-2",
        recorded_at=NOW,
    )
    current_only = identity_repo.option_linkages_for_run("run-2")
    assert len(current_only) == 1
    assert current_only[0].raw_underlying_symbol == "NEW"
    assert identity_repo.latest_option_linkage_run_id() == "run-2"
