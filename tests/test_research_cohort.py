from __future__ import annotations

import pytest

from apex.domain.research_cohort import (
    GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    CohortRole,
    CohortSector,
    CohortSecurityDeclaration,
    ResearchCohortError,
    ResearchCohortPolicy,
    resolve_cohort,
)
from apex.domain.security_identity import IdentityBasis, StableSecurityIdentity


def _identity(
    symbol: str, *, stable_id: str | None = None, cik: str | None = "0000000001"
) -> StableSecurityIdentity:
    stable = stable_id or f"stable_{symbol}"
    return StableSecurityIdentity(
        stable_security_id=stable,
        identity_basis=IdentityBasis.CIK_AND_SYMBOL if cik else IdentityBasis.SYMBOL_ONLY,
        cik=cik,
        normalized_symbol=symbol.lower(),
        current_symbol=symbol,
        source_identity_hash="a" * 64,
        source_canonical_security_id=f"canonical_{symbol}",
        review_required=False,
        canonical_hash="b" * 64,
    )


def test_policy_is_a_pure_function_of_its_declared_roster() -> None:
    member = CohortSecurityDeclaration(
        symbol="ZZZ", sector=CohortSector.TECHNOLOGY, roles=(CohortRole.LARGE_CAP_EQUITY,), note="x"
    )
    first = ResearchCohortPolicy.create(
        version="V1", members=(member,), selection_methodology="m", rationale="r"
    )
    second = ResearchCohortPolicy.create(
        version="V1", members=(member,), selection_methodology="m", rationale="r"
    )
    assert first.policy_id == second.policy_id
    assert first.canonical_hash == second.canonical_hash


def test_policy_identity_changes_when_roster_changes() -> None:
    member_a = CohortSecurityDeclaration(
        symbol="ZZZ", sector=CohortSector.TECHNOLOGY, roles=(CohortRole.LARGE_CAP_EQUITY,), note="x"
    )
    member_b = CohortSecurityDeclaration(
        symbol="YYY", sector=CohortSector.TECHNOLOGY, roles=(CohortRole.LARGE_CAP_EQUITY,), note="x"
    )
    first = ResearchCohortPolicy.create(
        version="V1", members=(member_a,), selection_methodology="m", rationale="r"
    )
    second = ResearchCohortPolicy.create(
        version="V1", members=(member_a, member_b), selection_methodology="m", rationale="r"
    )
    assert first.policy_id != second.policy_id


def test_policy_rejects_duplicate_symbol() -> None:
    member = CohortSecurityDeclaration(
        symbol="ZZZ", sector=CohortSector.TECHNOLOGY, roles=(CohortRole.LARGE_CAP_EQUITY,), note="x"
    )
    with pytest.raises(ResearchCohortError, match="DUPLICATE_SYMBOL"):
        ResearchCohortPolicy.create(
            version="V1", members=(member, member), selection_methodology="m", rationale="r"
        )


def test_declaration_requires_at_least_one_role() -> None:
    with pytest.raises(ResearchCohortError, match="REQUIRES_AT_LEAST_ONE_ROLE"):
        CohortSecurityDeclaration(symbol="ZZZ", sector=CohortSector.TECHNOLOGY, roles=(), note="x")


def test_genuine_cohort_v1_is_within_the_declared_50_to_150_band() -> None:
    assert 50 <= len(GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members) <= 150


def test_genuine_cohort_v1_selection_methodology_is_not_performance_based() -> None:
    policy = GENUINE_MULTI_ASSET_COHORT_POLICY_V1
    text = policy.selection_methodology.lower()
    assert "not_performance_based" in text or "not performance" in text
    assert "return" not in policy.rationale.lower().replace("no member's historical", "")


def test_genuine_cohort_v1_spans_every_declared_role() -> None:
    policy = GENUINE_MULTI_ASSET_COHORT_POLICY_V1
    for role in CohortRole:
        assert policy.member_count_with_role(role) >= 1, f"cohort has zero members with role {role}"


def test_genuine_cohort_v1_spans_every_gics_sector_for_equities() -> None:
    equity_sectors = {
        item.sector
        for item in GENUINE_MULTI_ASSET_COHORT_POLICY_V1.members
        if CohortRole.LARGE_CAP_EQUITY in item.roles or CohortRole.SMALL_CAP_EQUITY in item.roles
    }
    expected = {
        CohortSector.TECHNOLOGY,
        CohortSector.FINANCIALS,
        CohortSector.ENERGY,
        CohortSector.HEALTH_CARE,
        CohortSector.INDUSTRIALS,
        CohortSector.CONSUMER_DISCRETIONARY,
        CohortSector.CONSUMER_STAPLES,
        CohortSector.UTILITIES,
        CohortSector.MATERIALS,
        CohortSector.REAL_ESTATE,
        CohortSector.COMMUNICATION_SERVICES,
    }
    assert expected.issubset(equity_sectors)


def test_resolve_cohort_binds_stable_security_ids_deterministically() -> None:
    member = CohortSecurityDeclaration(
        symbol="ZZZ", sector=CohortSector.TECHNOLOGY, roles=(CohortRole.LARGE_CAP_EQUITY,), note="x"
    )
    etf_member = CohortSecurityDeclaration(
        symbol="ZET",
        sector=CohortSector.BROAD_MARKET,
        roles=(CohortRole.BROAD_INDEX_ETF,),
        note="y",
    )
    policy = ResearchCohortPolicy.create(
        version="V1", members=(member, etf_member), selection_methodology="m", rationale="r"
    )
    identities = (_identity("ZZZ"), _identity("ZET"))
    resolved = resolve_cohort(policy, stable_identities=identities)
    assert resolved.fully_resolved
    by_symbol = {item.symbol: item for item in resolved.members}
    assert by_symbol["ZZZ"].stable_security_id == "stable_ZZZ"
    assert by_symbol["ZZZ"].asset_class == "COMMON_STOCK"
    assert by_symbol["ZET"].asset_class == "ETF"


def test_resolve_cohort_reports_unresolved_symbol_without_dropping_it_silently() -> None:
    member = CohortSecurityDeclaration(
        symbol="ZZZ", sector=CohortSector.TECHNOLOGY, roles=(CohortRole.LARGE_CAP_EQUITY,), note="x"
    )
    policy = ResearchCohortPolicy.create(
        version="V1", members=(member,), selection_methodology="m", rationale="r"
    )
    resolved = resolve_cohort(policy, stable_identities=())
    assert not resolved.fully_resolved
    assert resolved.unresolved_symbols == ("ZZZ",)
    assert resolved.members == ()


def test_resolve_cohort_reports_ambiguous_symbol_without_guessing() -> None:
    member = CohortSecurityDeclaration(
        symbol="ZZZ", sector=CohortSector.TECHNOLOGY, roles=(CohortRole.LARGE_CAP_EQUITY,), note="x"
    )
    policy = ResearchCohortPolicy.create(
        version="V1", members=(member,), selection_methodology="m", rationale="r"
    )
    identities = (
        _identity("ZZZ", stable_id="stable_ZZZ_A"),
        _identity("ZZZ", stable_id="stable_ZZZ_B"),
    )
    resolved = resolve_cohort(policy, stable_identities=identities)
    assert not resolved.fully_resolved
    assert resolved.ambiguous_symbols == ("ZZZ",)
