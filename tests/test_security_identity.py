"""Stable, source-preference-invariant security identity and resolution
lineage -- deterministic proofs for every scenario the identity contract in
``apex.domain.security_identity`` claims to hold, plus the options-underlying
relink layered on top of it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apex.domain.options_intelligence import (
    ExerciseStyle,
    OptionContract,
    OptionType,
    SettlementStyle,
    UnderlyingLinkOutcome,
)
from apex.domain.security_identity import (
    IdentityBasis,
    IdentityResolution,
    OldIdentityKind,
    OptionUnderlyingStableLinkage,
    SecurityIdentityError,
    StableSecurityIdentity,
    apply_stable_identity,
    collect_raw_underlying_evidence,
    compute_stable_security_id,
    detect_stable_identity_supersession,
    detect_symbol_punctuation_collisions,
    normalized_symbol_key,
    raw_underlying_symbol_from_contract,
    relink_option_underlyings_to_stable_identity,
    resolve_legacy_security,
)
from apex.domain.security_reference import (
    EvidenceState,
    ReconciledSecurity,
    SecurityReference,
    SecurityType,
    reconcile_security_references,
)

NOW = datetime(2026, 8, 6, tzinfo=UTC)


def _record(
    symbol: str,
    provider: str,
    *,
    cik: str | None = None,
    name: str | None = None,
    exchange: str = "",
    security_type: SecurityType = SecurityType.UNKNOWN,
    tradable: EvidenceState = EvidenceState.UNKNOWN,
    active: EvidenceState = EvidenceState.YES,
) -> SecurityReference:
    return SecurityReference.create(
        current_symbol=symbol,
        provider_symbol=symbol,
        issuer_name=name or f"{symbol} Corp",
        listing_exchange=exchange,
        listing_category="X",
        security_type=security_type,
        common_stock=EvidenceState.UNKNOWN,
        etf=EvidenceState.UNKNOWN,
        adr=EvidenceState.UNKNOWN,
        reit=EvidenceState.UNKNOWN,
        test_issue=EvidenceState.NO,
        listing_status="NORMAL",
        round_lot_size=None,
        tradable=tradable,
        shortable=EvidenceState.UNKNOWN,
        marginable=EvidenceState.UNKNOWN,
        active=active,
        cik=cik,
        source_provider=provider,
        source_receipt_id="receipt-1",
        source_timestamp=NOW,
        source_payload_hash="0" * 64,
    )


def _reconciled(*records: SecurityReference, symbol: str) -> ReconciledSecurity:
    return next(
        item for item in reconcile_security_references(records) if item.current_symbol == symbol
    )


def _contract(
    *, root: str, underlying: str, historical_id: str, strike: str = "100"
) -> OptionContract:
    provider_contract_id = "|".join((root, "C", "2026-09-18", strike, underlying))
    return OptionContract.create(
        provider_contract_id=provider_contract_id,
        underlying_canonical_security_id=historical_id,
        standardized_symbol=None,
        option_type=OptionType.CALL,
        strike=strike,
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
        reference_receipt_ids=("receipt-1",),
    )


# --- Stable identity: invariance under every source-preference scenario ---


def test_stable_id_unchanged_when_sec_evidence_arrives_before_nasdaq() -> None:
    sec_first = _reconciled(_record("A", "SEC_EDGAR", cik="1090872"), symbol="A")
    both = _reconciled(
        _record("A", "SEC_EDGAR", cik="1090872"),
        _record(
            "A",
            "NASDAQ_TRADER",
            name="Agilent Technologies Common Stock",
            exchange="XNYS",
            security_type=SecurityType.COMMON_STOCK,
        ),
        symbol="A",
    )
    assert sec_first.canonical_security_id != both.canonical_security_id  # legacy id moves
    assert (
        StableSecurityIdentity.derive(sec_first).stable_security_id
        == StableSecurityIdentity.derive(both).stable_security_id
    )


def test_stable_id_unchanged_when_nasdaq_evidence_arrives_before_sec() -> None:
    """Reconciliation input order never affects the stable key: it is a pure
    function of the final reconciled consensus, not of arrival order."""
    order_a = reconcile_security_references(
        (
            _record("A", "SEC_EDGAR", cik="1090872"),
            _record("A", "NASDAQ_TRADER", name="Agilent Technologies", exchange="XNYS"),
        )
    )
    order_b = reconcile_security_references(
        (
            _record("A", "NASDAQ_TRADER", name="Agilent Technologies", exchange="XNYS"),
            _record("A", "SEC_EDGAR", cik="1090872"),
        )
    )
    stable_a = StableSecurityIdentity.derive(order_a[0])
    stable_b = StableSecurityIdentity.derive(order_b[0])
    assert stable_a.stable_security_id == stable_b.stable_security_id


def test_stable_id_unchanged_by_later_otherlisted_enrichment() -> None:
    before = _reconciled(_record("IBM", "NASDAQ_TRADER", exchange="XNAS"), symbol="IBM")
    after = _reconciled(
        _record("IBM", "NASDAQ_TRADER", exchange="XNAS"),
        _record("IBM", "SEC_EDGAR", cik="0000051143"),
        symbol="IBM",
    )
    # Consensus CIK only appears once SEC evidence exists at all -- adding it
    # alongside an already-present Nasdaq member enriches, it does not
    # replace, so the stable key (which already had no CIK to lose) is only
    # ever adding evidence, never contradicting it.
    before_stable = StableSecurityIdentity.derive(before)
    after_stable = StableSecurityIdentity.derive(after)
    assert before_stable.identity_basis is IdentityBasis.SYMBOL_ONLY
    assert after_stable.identity_basis is IdentityBasis.CIK_AND_SYMBOL
    assert before_stable.stable_security_id != after_stable.stable_security_id
    # This is the one scenario where enrichment does change the raw key
    # (SYMBOL_ONLY -> CIK_AND_SYMBOL); it is still a genuine, evidenced
    # strengthening, not a source-preference artifact, and
    # detect_stable_identity_supersession (tested below) resolves it as a
    # confident, automatic SUPERSEDED continuation, never silently dropped.


def test_stable_id_unchanged_by_source_priority_change() -> None:
    """The stable key never references source_provider/preference at all."""
    security = _reconciled(
        _record("A", "SEC_EDGAR", cik="1090872"),
        _record("A", "NASDAQ_TRADER", name="Agilent Technologies", exchange="XNYS"),
        symbol="A",
    )
    stable = StableSecurityIdentity.derive(security)
    assert stable.stable_security_id == compute_stable_security_id(
        cik=security.cik, current_symbol=security.current_symbol
    )


def test_stable_id_unchanged_by_descriptive_name_change() -> None:
    before = _reconciled(
        _record("META", "SEC_EDGAR", cik="1326801", name="Facebook Inc"), symbol="META"
    )
    after = _reconciled(
        _record("META", "SEC_EDGAR", cik="1326801", name="Meta Platforms Inc"), symbol="META"
    )
    assert before.issuer_name != after.issuer_name
    assert (
        StableSecurityIdentity.derive(before).stable_security_id
        == StableSecurityIdentity.derive(after).stable_security_id
    )


def test_stable_id_unchanged_by_listing_metadata_enrichment() -> None:
    before = _record("XYZ", "NASDAQ_TRADER", exchange="XNAS")
    after = SecurityReference.create(
        current_symbol="XYZ",
        provider_symbol="XYZ",
        issuer_name="XYZ Corp",
        listing_exchange="XNAS",
        listing_category="Q",
        security_type=SecurityType.COMMON_STOCK,
        common_stock=EvidenceState.YES,
        etf=EvidenceState.NO,
        adr=EvidenceState.NO,
        reit=EvidenceState.NO,
        test_issue=EvidenceState.NO,
        listing_status="NORMAL",
        round_lot_size=100,
        tradable=EvidenceState.UNKNOWN,
        shortable=EvidenceState.UNKNOWN,
        marginable=EvidenceState.UNKNOWN,
        active=EvidenceState.YES,
        cik=None,
        source_provider="NASDAQ_TRADER",
        source_receipt_id="receipt-1",
        source_timestamp=NOW,
        source_payload_hash="0" * 64,
    )
    before_reconciled = _reconciled(before, symbol="XYZ")
    after_reconciled = _reconciled(after, symbol="XYZ")
    assert (
        StableSecurityIdentity.derive(before_reconciled).stable_security_id
        == StableSecurityIdentity.derive(after_reconciled).stable_security_id
    )


def test_stable_id_unchanged_by_provider_execution_capability_change() -> None:
    unknown = _reconciled(_record("XYZ", "ALPACA", tradable=EvidenceState.UNKNOWN), symbol="XYZ")
    tradable = _reconciled(_record("XYZ", "ALPACA", tradable=EvidenceState.YES), symbol="XYZ")
    assert (
        StableSecurityIdentity.derive(unknown).stable_security_id
        == StableSecurityIdentity.derive(tradable).stable_security_id
    )


def test_stable_id_unaffected_by_research_eligibility_policy() -> None:
    """CurrentEquityUniversePolicy decisions never feed the stable key at
    all -- proven by construction: StableSecurityIdentity.derive never reads
    eligibility, so an ETF and a common stock sharing hypothetical identical
    (cik, symbol) evidence would collide identically regardless of policy.
    This test instead proves the *practical* case: excluded and included
    security types both get a real, usable stable id."""
    etf = _reconciled(
        _record("SPY", "NASDAQ_TRADER", security_type=SecurityType.ETF, exchange="ARCX"),
        symbol="SPY",
    )
    common = _reconciled(
        _record("A", "NASDAQ_TRADER", security_type=SecurityType.COMMON_STOCK, exchange="XNYS"),
        symbol="A",
    )
    assert StableSecurityIdentity.derive(etf).stable_security_id
    assert StableSecurityIdentity.derive(common).stable_security_id


def test_class_share_symbols_are_distinct_stable_identities() -> None:
    class_a = _reconciled(
        _record("BRK.A", "NASDAQ_TRADER", cik=None, exchange="XNYS"), symbol="BRK.A"
    )
    class_b = _reconciled(
        _record("BRK.B", "NASDAQ_TRADER", cik=None, exchange="XNYS"), symbol="BRK.B"
    )
    assert (
        StableSecurityIdentity.derive(class_a).stable_security_id
        != StableSecurityIdentity.derive(class_b).stable_security_id
    )


def test_normalized_symbol_key_collapses_punctuation_but_not_class() -> None:
    assert (
        normalized_symbol_key("BRK.A")
        == normalized_symbol_key("BRK-A")
        == normalized_symbol_key("BRK A")
    )
    assert normalized_symbol_key("BRK.A") != normalized_symbol_key("BRK.B")


def test_compute_stable_security_id_requires_a_symbol() -> None:
    with pytest.raises(SecurityIdentityError):
        compute_stable_security_id(cik=None, current_symbol="   ")


# --- Legacy resolution: backward compatibility -----------------------------


def test_resolve_legacy_security_marks_current_and_alias_to_the_same_stable_id() -> None:
    old = _reconciled(_record("A", "SEC_EDGAR", cik="1090872"), symbol="A")
    new = _reconciled(
        _record("A", "SEC_EDGAR", cik="1090872"),
        _record("A", "NASDAQ_TRADER", name="Agilent Technologies", exchange="XNYS"),
        symbol="A",
    )
    old_stable, old_resolution = resolve_legacy_security(old, is_current=False)
    new_stable, new_resolution = resolve_legacy_security(new, is_current=True)
    assert old_stable.stable_security_id == new_stable.stable_security_id
    assert old_resolution.resolution_state.value == "ALIAS"
    assert old_resolution.automatic is True
    assert new_resolution.resolution_state.value == "CURRENT"
    assert old_resolution.resolved_stable_security_id == new_resolution.resolved_stable_security_id
    assert old_resolution.old_identity_id == old.canonical_security_id
    assert old_resolution.old_identity_kind is OldIdentityKind.LEGACY_CANONICAL_SECURITY_ID


def test_resolve_legacy_security_reuses_existing_requires_review_signal() -> None:
    conflicting = reconcile_security_references(
        (
            _record("Z", "SEC_EDGAR", cik="1111111"),
            _record("Z", "PROVIDER_X", cik="2222222"),
        )
    )[0]
    assert conflicting.requires_review is True
    _, resolution = resolve_legacy_security(conflicting, is_current=True)
    assert resolution.resolution_state.value == "AMBIGUOUS_REQUIRES_REVIEW"
    assert resolution.automatic is False


def test_apply_stable_identity_preserves_canonical_hash_and_rejects_mismatch() -> None:
    security = _reconciled(_record("A", "SEC_EDGAR", cik="1090872"), symbol="A")
    stable = StableSecurityIdentity.derive(security)
    projected = apply_stable_identity(security, stable)
    assert projected.canonical_security_id == stable.stable_security_id
    assert projected.canonical_hash == security.canonical_hash
    assert projected.current_symbol == security.current_symbol

    other = _reconciled(_record("B", "SEC_EDGAR", cik="9999999"), symbol="B")
    with pytest.raises(SecurityIdentityError):
        apply_stable_identity(other, stable)


# --- Cross-run supersession: genuine identity events -----------------------


def test_symbol_change_same_cik_is_automatic_superseded() -> None:
    fb = _reconciled(_record("FB", "SEC_EDGAR", cik="1326801"), symbol="FB")
    meta = _reconciled(_record("META", "SEC_EDGAR", cik="1326801"), symbol="META")
    fb_stable = StableSecurityIdentity.derive(fb)
    meta_stable = StableSecurityIdentity.derive(meta)
    resolutions = detect_stable_identity_supersession(current=(meta_stable,), prior=(fb_stable,))
    assert len(resolutions) == 1
    resolution = resolutions[0]
    assert resolution.resolution_state.value == "SUPERSEDED"
    assert resolution.automatic is True
    assert resolution.resolution_method == "CIK_MATCH_SYMBOL_CHANGED"
    assert resolution.old_identity_id == fb_stable.stable_security_id
    assert resolution.resolved_stable_security_id == meta_stable.stable_security_id
    assert resolution.old_identity_kind is OldIdentityKind.STABLE_SECURITY_ID


def test_cik_enrichment_same_symbol_is_automatic_superseded() -> None:
    before = _reconciled(_record("IBM", "NASDAQ_TRADER", exchange="XNAS"), symbol="IBM")
    after = _reconciled(
        _record("IBM", "NASDAQ_TRADER", exchange="XNAS"),
        _record("IBM", "SEC_EDGAR", cik="0000051143"),
        symbol="IBM",
    )
    before_stable = StableSecurityIdentity.derive(before)
    after_stable = StableSecurityIdentity.derive(after)
    resolutions = detect_stable_identity_supersession(
        current=(after_stable,), prior=(before_stable,)
    )
    assert len(resolutions) == 1
    assert resolutions[0].resolution_state.value == "SUPERSEDED"
    assert resolutions[0].automatic is True
    assert resolutions[0].resolution_method == "SYMBOL_MATCH_CIK_ENRICHED"


def test_different_issuer_reusing_a_ticker_is_ambiguous_never_merged() -> None:
    """Two real but different CIKs sharing one symbol: a possible genuine
    ticker reuse Apex cannot confirm from current-snapshot evidence alone --
    reported ambiguous, never silently merged onto one identity."""
    old_issuer = _reconciled(_record("ZZZ", "SEC_EDGAR", cik="1111111"), symbol="ZZZ")
    new_issuer = _reconciled(_record("ZZZ", "SEC_EDGAR", cik="2222222"), symbol="ZZZ")
    old_stable = StableSecurityIdentity.derive(old_issuer)
    new_stable = StableSecurityIdentity.derive(new_issuer)
    resolutions = detect_stable_identity_supersession(current=(new_stable,), prior=(old_stable,))
    assert len(resolutions) == 1
    assert resolutions[0].resolution_state.value == "AMBIGUOUS_REQUIRES_REVIEW"
    assert resolutions[0].automatic is False
    assert resolutions[0].resolution_method == "CONFLICTING_CIK_SAME_SYMBOL"


def test_multiple_candidate_predecessors_are_ambiguous() -> None:
    new = StableSecurityIdentity.derive(
        _reconciled(_record("NEWCO", "SEC_EDGAR", cik="5555555"), symbol="NEWCO")
    )
    candidate_one = StableSecurityIdentity.derive(
        _reconciled(_record("OLDCO", "SEC_EDGAR", cik="5555555"), symbol="OLDCO")
    )
    candidate_two = StableSecurityIdentity.derive(
        _reconciled(_record("NEWCO", "SEC_EDGAR", cik="6666666"), symbol="NEWCO")
    )
    resolutions = detect_stable_identity_supersession(
        current=(new,), prior=(candidate_one, candidate_two)
    )
    assert len(resolutions) == 2
    assert all(item.resolution_state.value == "AMBIGUOUS_REQUIRES_REVIEW" for item in resolutions)
    assert all(not item.automatic for item in resolutions)
    assert {item.old_identity_id for item in resolutions} == {
        candidate_one.stable_security_id,
        candidate_two.stable_security_id,
    }


def test_genuinely_new_identity_supersedes_nothing() -> None:
    unrelated_prior = StableSecurityIdentity.derive(
        _reconciled(_record("OLD", "SEC_EDGAR", cik="1111111"), symbol="OLD")
    )
    brand_new = StableSecurityIdentity.derive(
        _reconciled(_record("IPO", "SEC_EDGAR", cik="9999999"), symbol="IPO")
    )
    resolutions = detect_stable_identity_supersession(
        current=(unrelated_prior, brand_new), prior=(unrelated_prior,)
    )
    assert resolutions == ()


def test_unaffected_prior_identity_is_left_alone() -> None:
    unchanged = StableSecurityIdentity.derive(
        _reconciled(_record("AAPL", "NASDAQ_TRADER", exchange="XNAS"), symbol="AAPL")
    )
    resolutions = detect_stable_identity_supersession(current=(unchanged,), prior=(unchanged,))
    assert resolutions == ()


# --- Same-run punctuation cross-reference -----------------------------------


def test_symbol_punctuation_collision_flags_both_sides_without_merging() -> None:
    dash = StableSecurityIdentity.derive(
        _reconciled(_record("AGM-A", "SEC_EDGAR", cik="1046025"), symbol="AGM-A")
    )
    dot = StableSecurityIdentity.derive(
        _reconciled(
            _record(
                "AGM.A",
                "NASDAQ_TRADER",
                name="Federal Agricultural Mortgage Class A",
                exchange="XNYS",
            ),
            symbol="AGM.A",
        )
    )
    assert dash.stable_security_id != dot.stable_security_id
    resolutions = detect_symbol_punctuation_collisions((dash, dot))
    assert len(resolutions) == 2
    assert all(item.resolution_state.value == "AMBIGUOUS_REQUIRES_REVIEW" for item in resolutions)
    assert all(not item.automatic for item in resolutions)
    ids = {item.old_identity_id for item in resolutions}
    assert ids == {dash.stable_security_id, dot.stable_security_id}
    # Both remain independently resolvable -- nothing was merged.
    for item in resolutions:
        assert item.resolved_stable_security_id is None


def test_no_punctuation_collision_when_symbols_are_unrelated() -> None:
    a = StableSecurityIdentity.derive(
        _reconciled(_record("AAPL", "NASDAQ_TRADER", exchange="XNAS"), symbol="AAPL")
    )
    b = StableSecurityIdentity.derive(
        _reconciled(_record("MSFT", "NASDAQ_TRADER", exchange="XNAS"), symbol="MSFT")
    )
    assert detect_symbol_punctuation_collisions((a, b)) == ()


# --- IdentityResolution invariants -----------------------------------------


def test_identity_resolution_rejects_automatic_ambiguity() -> None:
    with pytest.raises(SecurityIdentityError):
        IdentityResolution.create(
            old_identity_id="apex_security_x",
            old_identity_kind=OldIdentityKind.LEGACY_CANONICAL_SECURITY_ID,
            resolved_stable_security_id=None,
            resolution_state="AMBIGUOUS_REQUIRES_REVIEW",
            automatic=True,
            resolution_method="TEST",
            evidence=("hash-1",),
        )


def test_identity_resolution_supports_equivalent_and_distinct_security_vocabulary() -> None:
    """DISTINCT_SECURITY and EQUIVALENT are supported vocabulary this
    milestone's automatic pipeline never emits on its own (it conservatively
    reports AMBIGUOUS_REQUIRES_REVIEW instead) -- reserved for a future
    manual-review resolution workflow. Proven directly here."""
    equivalent = IdentityResolution.create(
        old_identity_id="apex_stable_security_old",
        old_identity_kind=OldIdentityKind.STABLE_SECURITY_ID,
        resolved_stable_security_id="apex_stable_security_new",
        resolution_state="EQUIVALENT",
        automatic=False,
        resolution_method="OPERATOR_CONFIRMED_ALIAS_LISTING",
        evidence=("hash-1", "hash-2"),
    )
    assert equivalent.resolution_state.value == "EQUIVALENT"
    distinct = IdentityResolution.create(
        old_identity_id="apex_stable_security_a",
        old_identity_kind=OldIdentityKind.STABLE_SECURITY_ID,
        resolved_stable_security_id=None,
        resolution_state="DISTINCT_SECURITY",
        automatic=False,
        resolution_method="OPERATOR_CONFIRMED_UNRELATED_ISSUERS",
        evidence=("hash-1",),
    )
    assert distinct.resolution_state.value == "DISTINCT_SECURITY"
    with pytest.raises(SecurityIdentityError):
        IdentityResolution.create(
            old_identity_id="apex_stable_security_a",
            old_identity_kind=OldIdentityKind.STABLE_SECURITY_ID,
            resolved_stable_security_id=None,
            resolution_state="DISTINCT_SECURITY",
            automatic=True,
            resolution_method="TEST",
            evidence=("hash-1",),
        )


def test_identity_resolution_requires_evidence_and_identity() -> None:
    with pytest.raises(SecurityIdentityError):
        IdentityResolution.create(
            old_identity_id="",
            old_identity_kind=OldIdentityKind.LEGACY_CANONICAL_SECURITY_ID,
            resolved_stable_security_id="apex_stable_security_x",
            resolution_state="CURRENT",
            automatic=True,
            resolution_method="TEST",
            evidence=("hash-1",),
        )
    with pytest.raises(SecurityIdentityError):
        IdentityResolution.create(
            old_identity_id="apex_security_x",
            old_identity_kind=OldIdentityKind.LEGACY_CANONICAL_SECURITY_ID,
            resolved_stable_security_id="apex_stable_security_x",
            resolution_state="CURRENT",
            automatic=True,
            resolution_method="TEST",
            evidence=(),
        )


def test_identity_resolution_is_content_addressed_and_replay_safe() -> None:
    kwargs = dict(
        old_identity_id="apex_security_x",
        old_identity_kind=OldIdentityKind.LEGACY_CANONICAL_SECURITY_ID,
        resolved_stable_security_id="apex_stable_security_x",
        resolution_state="CURRENT",
        automatic=True,
        resolution_method="TEST",
        evidence=("hash-1",),
    )
    first = IdentityResolution.create(**kwargs)
    second = IdentityResolution.create(**kwargs)
    assert first.canonical_hash == second.canonical_hash
    assert first == second


# --- Options-underlying relink against the stable identity space -----------


def test_raw_underlying_symbol_from_contract_recovers_exact_directory_text() -> None:
    contract = _contract(root="META", underlying="META", historical_id="legacy-id")
    assert raw_underlying_symbol_from_contract(contract) == "META"


def test_raw_underlying_symbol_recovers_text_even_when_root_differs() -> None:
    contract = _contract(root="META1", underlying="META", historical_id="legacy-id")
    assert raw_underlying_symbol_from_contract(contract) == "META"


def test_collect_raw_underlying_evidence_groups_by_raw_symbol_and_counts() -> None:
    contracts = (
        _contract(root="META", underlying="META", historical_id="legacy-id", strike="100"),
        _contract(root="META", underlying="META", historical_id="legacy-id", strike="110"),
    )
    evidence = collect_raw_underlying_evidence(contracts)
    assert len(evidence) == 1
    assert evidence[0].raw_underlying_symbol == "META"
    assert evidence[0].contract_count == 2
    assert evidence[0].historical_underlying_security_id == "legacy-id"


def test_collect_raw_underlying_evidence_fails_closed_on_conflicting_historical_ids() -> None:
    contracts = (
        _contract(root="META", underlying="META", historical_id="legacy-id-1"),
        _contract(root="META", underlying="META", historical_id="legacy-id-2"),
    )
    with pytest.raises(SecurityIdentityError):
        collect_raw_underlying_evidence(contracts)


def test_relink_reuses_retained_contract_unchanged_and_derives_new_linkage() -> None:
    """The immutable retained contract's own underlying id is never
    overwritten; the derived linkage is a separate record entirely."""
    contract = _contract(root="META", underlying="META", historical_id="legacy-old-fb-id")
    stable = StableSecurityIdentity.derive(
        _reconciled(_record("META", "SEC_EDGAR", cik="1326801"), symbol="META")
    )
    evidence = collect_raw_underlying_evidence((contract,))
    linkages = relink_option_underlyings_to_stable_identity(
        raw_evidence=evidence,
        previously_rejected=(),
        stable_symbol_index={"META": stable.stable_security_id},
        linkage_run_id="run-1",
    )
    assert contract.underlying_canonical_security_id == "legacy-old-fb-id"  # unchanged
    assert len(linkages) == 1
    linkage = linkages[0]
    assert linkage.outcome is UnderlyingLinkOutcome.LINKED
    assert linkage.resolved_stable_security_id == stable.stable_security_id
    assert linkage.historical_underlying_security_id == "legacy-old-fb-id"
    assert linkage.contract_count == 1
    assert linkage.linkage_run_id == "run-1"


def test_relink_newly_links_a_previously_rejected_etf_symbol() -> None:
    """A symbol only reachable via otherlisted.txt (never captured at the
    time of the original options-directory capture) was UNLINKED then;
    relinking against the expanded stable universe newly links it, without
    recapturing the options source."""
    stable = StableSecurityIdentity.derive(
        _reconciled(_record("AAAU", "NASDAQ_TRADER", security_type=SecurityType.ETF), symbol="AAAU")
    )
    linkages = relink_option_underlyings_to_stable_identity(
        raw_evidence=(),
        previously_rejected=(("AAAU", 12),),
        stable_symbol_index={"AAAU": stable.stable_security_id},
        linkage_run_id="run-1",
    )
    assert len(linkages) == 1
    assert linkages[0].outcome is UnderlyingLinkOutcome.LINKED
    assert linkages[0].resolved_stable_security_id == stable.stable_security_id
    assert linkages[0].historical_underlying_security_id is None
    assert linkages[0].contract_count == 12


def test_relink_class_share_symbol_ambiguous_via_normalized_fallback() -> None:
    dash = StableSecurityIdentity.derive(
        _reconciled(_record("BRK-A", "SEC_EDGAR", cik="1067983"), symbol="BRK-A")
    )
    dot = StableSecurityIdentity.derive(
        _reconciled(
            _record("BRK.A", "NASDAQ_TRADER", name="Berkshire Hathaway Class A", exchange="XNYS"),
            symbol="BRK.A",
        )
    )
    contract = _contract(root="BRKA", underlying="BRKA", historical_id="legacy-id")
    evidence = collect_raw_underlying_evidence((contract,))
    linkages = relink_option_underlyings_to_stable_identity(
        raw_evidence=evidence,
        previously_rejected=(),
        stable_symbol_index={"BRK-A": dash.stable_security_id, "BRK.A": dot.stable_security_id},
        linkage_run_id="run-1",
    )
    assert len(linkages) == 1
    assert linkages[0].outcome is UnderlyingLinkOutcome.AMBIGUOUS
    assert linkages[0].resolved_stable_security_id is None
    assert set(linkages[0].candidate_stable_security_ids) == {
        dash.stable_security_id,
        dot.stable_security_id,
    }


def test_relink_unlinked_symbol_retains_raw_symbol_and_reason_without_guessing() -> None:
    linkages = relink_option_underlyings_to_stable_identity(
        raw_evidence=(),
        previously_rejected=(("GOEVQ", 10),),
        stable_symbol_index={"AAPL": "apex_stable_security_aapl"},
        linkage_run_id="run-1",
    )
    assert len(linkages) == 1
    assert linkages[0].outcome is UnderlyingLinkOutcome.UNLINKED
    assert linkages[0].resolved_stable_security_id is None
    assert linkages[0].reasons == ("NO_MATCHING_RECONCILED_IDENTITY",)
    assert linkages[0].raw_underlying_symbol == "GOEVQ"


def test_relink_requires_a_linkage_run_id() -> None:
    with pytest.raises(SecurityIdentityError):
        relink_option_underlyings_to_stable_identity(
            raw_evidence=(),
            previously_rejected=(),
            stable_symbol_index={},
            linkage_run_id="",
        )


def test_relink_is_idempotent_and_produces_no_duplicate_linkage_decisions() -> None:
    """Repeated relink against identical evidence is content-addressed and
    replay-safe: identical inputs always produce an identical canonical_hash,
    so a persistence layer's insert-if-not-exists dedup never double-counts."""
    stable = StableSecurityIdentity.derive(
        _reconciled(_record("META", "SEC_EDGAR", cik="1326801"), symbol="META")
    )
    contract = _contract(root="META", underlying="META", historical_id="legacy-id")
    evidence = collect_raw_underlying_evidence((contract,))
    first = relink_option_underlyings_to_stable_identity(
        raw_evidence=evidence,
        previously_rejected=(),
        stable_symbol_index={"META": stable.stable_security_id},
        linkage_run_id="run-1",
    )
    second = relink_option_underlyings_to_stable_identity(
        raw_evidence=evidence,
        previously_rejected=(),
        stable_symbol_index={"META": stable.stable_security_id},
        linkage_run_id="run-1",
    )
    assert first[0].canonical_hash == second[0].canonical_hash


def test_option_underlying_stable_linkage_rejects_inconsistent_outcome_and_resolution() -> None:
    with pytest.raises(SecurityIdentityError):
        OptionUnderlyingStableLinkage.create(
            raw_underlying_symbol="META",
            outcome=UnderlyingLinkOutcome.LINKED,
            resolved_stable_security_id=None,
            candidate_stable_security_ids=(),
            historical_underlying_security_id=None,
            reasons=("EXACT_SYMBOL_MATCH",),
            contract_count=1,
            linkage_run_id="run-1",
        )
    with pytest.raises(SecurityIdentityError):
        OptionUnderlyingStableLinkage.create(
            raw_underlying_symbol="META",
            outcome=UnderlyingLinkOutcome.UNLINKED,
            resolved_stable_security_id="apex_stable_security_x",
            candidate_stable_security_ids=(),
            historical_underlying_security_id=None,
            reasons=("NO_MATCHING_RECONCILED_IDENTITY",),
            contract_count=1,
            linkage_run_id="run-1",
        )
