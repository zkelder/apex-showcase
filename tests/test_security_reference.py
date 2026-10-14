import json
from datetime import UTC, date, datetime, timedelta

import pytest

from apex.adapters.security_reference import (
    NASDAQ_LISTED_PATH,
    NASDAQ_OTHER_LISTED_EXCHANGE_CODES,
    NASDAQ_OTHER_LISTED_PATH,
    UNRECOGNIZED_EXCHANGE_CODE_PREFIX,
    parse_alpaca_assets,
    parse_nasdaq_symbol_directory,
    parse_sec_company_tickers,
    validate_sec_user_agent,
)
from apex.application.security_reference import (
    CreateCurrentEquityUniverse,
    InMemorySecurityReferenceRepository,
    SecurityReferenceApplicationError,
)
from apex.domain.security_reference import (
    CURRENT_LISTED_WARNING,
    CurrentEquityUniversePolicy,
    EvidenceState,
    ExecutionCapabilityState,
    ReconciliationClassification,
    SecurityReference,
    SecurityReferenceError,
    SecurityType,
    create_current_equity_universe_snapshot,
    determine_execution_capability,
    reconcile_security_references,
)

NOW = datetime(2026, 7, 29, 14, tzinfo=UTC)


def _reference(
    *,
    symbol: str = "ACME",
    security_type: SecurityType = SecurityType.COMMON_STOCK,
    tradable: EvidenceState = EvidenceState.UNKNOWN,
    active: EvidenceState = EvidenceState.YES,
    test_issue: EvidenceState = EvidenceState.NO,
    listing_status: str = "NORMAL",
    source_provider: str = "NASDAQ_TRADER",
    cik: str | None = None,
) -> SecurityReference:
    return SecurityReference.create(
        current_symbol=symbol,
        provider_symbol=symbol,
        issuer_name=f"{symbol} Corporation",
        listing_exchange="XNAS",
        listing_category="Q",
        security_type=security_type,
        common_stock=(
            EvidenceState.YES if security_type is SecurityType.COMMON_STOCK else EvidenceState.NO
        ),
        etf=EvidenceState.YES if security_type is SecurityType.ETF else EvidenceState.NO,
        adr=EvidenceState.NO,
        reit=EvidenceState.NO,
        test_issue=test_issue,
        listing_status=listing_status,
        round_lot_size=100,
        tradable=tradable,
        shortable=EvidenceState.UNKNOWN,
        marginable=EvidenceState.UNKNOWN,
        active=active,
        cik=cik,
        source_provider=source_provider,
        source_receipt_id="receipt-1",
        source_timestamp=NOW,
        source_payload_hash="0" * 64,
    )


def _captures() -> tuple[object, object, object]:
    nasdaq = (
        b"Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|"
        b"NextShares\n"
        b"ACME|Acme Corporation Common Stock|Q|N|N|100|N|N\n"
        b"TEST|Test Security Common Stock|Q|Y|N|100|N|N\n"
        b"FUND|Example Index ETF|G|N|N|100|Y|N\n"
        b"WARN|Warning Corp Common Stock|Q|N|D|100|N|N\n"
        b"File Creation Time: 0729202612:00|||||||\n"
    )
    sec = json.dumps(
        {
            "0": {"cik_str": 1001, "ticker": "ACME", "title": "Acme Corporation"},
            "1": {"cik_str": 1002, "ticker": "WARN", "title": "Warning Corp"},
        },
        separators=(",", ":"),
    ).encode()
    alpaca = json.dumps(
        [
            {
                "symbol": symbol,
                "name": name,
                "class": "us_equity",
                "exchange": "NASDAQ",
                "status": "active",
                "tradable": True,
                "shortable": True,
                "marginable": True,
            }
            for symbol, name in (
                ("ACME", "Acme Corporation"),
                ("TEST", "Test Security"),
                ("FUND", "Example Index ETF"),
                ("WARN", "Warning Corp"),
            )
        ],
        separators=(",", ":"),
    ).encode()
    return (
        parse_nasdaq_symbol_directory(
            payload=nasdaq, source_path=NASDAQ_LISTED_PATH, received_at=NOW
        ),
        parse_sec_company_tickers(payload=sec, received_at=NOW, source_timestamp=NOW),
        parse_alpaca_assets(payload=alpaca, received_at=NOW, source_timestamp=NOW),
    )


def _other_listed_payload(*, exchange_rows: str) -> bytes:
    return (
        b"ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|"
        b"NASDAQ Symbol\n" + exchange_rows.encode() + b"File Creation Time: 0729202612:30|||||||\n"
    )


def test_other_listed_directory_maps_every_documented_exchange_code() -> None:
    """otherlisted.txt's five documented Exchange codes each map to their
    canonical internal listing venue -- verified against this repository's
    own NASDAQ_OTHER_LISTED_EXCHANGE_CODES contract, not hardcoded twice."""
    rows = "".join(
        f"SYM{code}|Security {code} Common Stock|{code}|SYM{code}|N|100|N|SYM{code}\n"
        for code in NASDAQ_OTHER_LISTED_EXCHANGE_CODES
    )
    parsed = parse_nasdaq_symbol_directory(
        payload=_other_listed_payload(exchange_rows=rows),
        source_path=NASDAQ_OTHER_LISTED_PATH,
        received_at=NOW,
    )
    by_symbol = {item.current_symbol: item for item in parsed.records}
    assert len(by_symbol) == len(NASDAQ_OTHER_LISTED_EXCHANGE_CODES)
    for code, mic in NASDAQ_OTHER_LISTED_EXCHANGE_CODES.items():
        record = by_symbol[f"SYM{code}"]
        assert record.listing_exchange == mic
        # The raw one-letter code is retained verbatim regardless of mapping.
        assert record.listing_category == code
        assert "LISTING_EXCHANGE_CODE_NOT_IN_KNOWN_MAPPING" not in record.limitations


def test_other_listed_unknown_exchange_code_is_retained_raw_and_classified_explicitly() -> None:
    """An Exchange code outside the five documented values must never be
    silently mapped to a different exchange: the raw code is retained and
    the record is explicitly flagged, not guessed at."""
    payload = _other_listed_payload(
        exchange_rows="MYST|Mystery Venue Common Stock|Q|MYST|N|100|N|MYST\n"
    )
    parsed = parse_nasdaq_symbol_directory(
        payload=payload, source_path=NASDAQ_OTHER_LISTED_PATH, received_at=NOW
    )
    record = parsed.records[0]
    assert record.listing_exchange == f"{UNRECOGNIZED_EXCHANGE_CODE_PREFIX}Q"
    assert record.listing_exchange.endswith(":Q")
    assert record.listing_category == "Q"  # raw code still retained verbatim here too
    assert "LISTING_EXCHANGE_CODE_NOT_IN_KNOWN_MAPPING" in record.limitations
    # An unrecognized code is structurally distinguishable from every real
    # mapped MIC-style code -- it can never be confused with one.
    assert record.listing_exchange not in NASDAQ_OTHER_LISTED_EXCHANGE_CODES.values()


def test_other_listed_records_always_report_current_listed_status() -> None:
    """otherlisted.txt carries no Financial Status column (unlike
    nasdaqlisted.txt) -- Nasdaq Trader does not track deficient/delinquent/
    bankrupt flags for issuers primarily listed elsewhere. This is a genuine
    source characteristic, not a defect: every otherlisted.txt record must
    report CURRENT_LISTED rather than a fabricated adverse status."""
    payload = _other_listed_payload(
        exchange_rows="OTHR|Other Exchange Corporation Common Stock|N|OTHR|N|100|N|OTHR\n"
    )
    parsed = parse_nasdaq_symbol_directory(
        payload=payload, source_path=NASDAQ_OTHER_LISTED_PATH, received_at=NOW
    )
    assert parsed.records[0].listing_status == "CURRENT_LISTED"


def test_other_listed_test_issue_flag_is_preserved() -> None:
    payload = _other_listed_payload(
        exchange_rows="ZTST|Test Security Common Stock|N|ZTST|N|100|Y|ZTST\n"
    )
    parsed = parse_nasdaq_symbol_directory(
        payload=payload, source_path=NASDAQ_OTHER_LISTED_PATH, received_at=NOW
    )
    assert parsed.records[0].test_issue.value == "YES"


def test_singular_ordinary_share_and_depository_spelling_classify_correctly() -> None:
    """A genuine otherlisted.txt-only classification gap, found by inspecting
    the real expanded universe: "Ordinary Share" (singular, common in SPAC
    and foreign-private-issuer names) and "Depository" (a genuine alternate
    spelling of "Depositary" that many issuers' own listed names use) were
    left classified UNKNOWN by the plural/single-spelling-only checks, even
    though they are ordinary common-stock-equivalent and ADR-equivalent
    securities respectively. Both the original forms and the newly covered
    ones must classify correctly -- this is an additive fix, not a
    replacement."""
    payload = _other_listed_payload(
        exchange_rows=(
            "SPAC|Example Acquisition Corp - Class A Ordinary Share|N|SPAC|N|100|N|SPAC\n"
            "PLUR|Example Holdings Ltd - Ordinary Shares|N|PLUR|N|100|N|PLUR\n"
            "ADRD|Example Pharma - American Depository Shares|N|ADRD|N|100|N|ADRD\n"
            "ADRA|Example Corp - American Depositary Shares|N|ADRA|N|100|N|ADRA\n"
        )
    )
    parsed = parse_nasdaq_symbol_directory(
        payload=payload, source_path=NASDAQ_OTHER_LISTED_PATH, received_at=NOW
    )
    by_symbol = {item.current_symbol: item for item in parsed.records}
    assert by_symbol["SPAC"].security_type is SecurityType.COMMON_STOCK  # newly covered
    assert by_symbol["PLUR"].security_type is SecurityType.COMMON_STOCK  # pre-existing coverage
    assert by_symbol["ADRD"].security_type is SecurityType.ADR  # newly covered
    assert by_symbol["ADRA"].security_type is SecurityType.ADR  # pre-existing coverage
    assert by_symbol["ADRD"].adr is EvidenceState.YES
    assert by_symbol["SPAC"].common_stock is EvidenceState.YES


def test_reference_reconciliation_and_current_universe_are_deterministic() -> None:
    nasdaq, sec, alpaca = _captures()
    records = (*nasdaq.records, *sec.records, *alpaca.records)
    reconciled = reconcile_security_references(records)
    acme = next(item for item in reconciled if item.current_symbol == "ACME")

    assert ReconciliationClassification.CONFIRMED in acme.classifications
    assert acme.cik == "0000001001"
    assert acme.tradable.value == "YES"
    assert not acme.requires_review

    service = CreateCurrentEquityUniverse(InMemorySecurityReferenceRepository())
    arguments = {
        "securities": reconciled,
        "policy": CurrentEquityUniversePolicy.initial(),
        "source_receipt_ids": (
            nasdaq.receipt.receipt_id,
            sec.receipt.receipt_id,
            alpaca.receipt.receipt_id,
        ),
        "as_of_date": date(2026, 7, 29),
        "created_at": NOW,
        "idempotency_key": "universe-2026-07-29",
    }
    snapshot = service.execute(**arguments)

    assert snapshot.warning == CURRENT_LISTED_WARNING
    assert snapshot.total_considered == 4
    assert tuple(item.current_symbol for item in snapshot.decisions if item.research_eligible) == (
        "ACME",
    )
    assert dict(snapshot.research_exclusion_counts)["ADVERSE_LISTING_STATUS"] == 1
    # Execution capability is tracked in full even though every security here
    # carries Alpaca evidence (tradable=True) and none of it fed the research
    # eligibility decision above.
    acme_decision = next(item for item in snapshot.decisions if item.current_symbol == "ACME")
    assert acme_decision.execution_capability.value == "ALPACA_TRADABLE"
    assert snapshot.execution_capability_known_count == 4
    assert snapshot.execution_capability_unknown_count == 0
    assert service.execute(**arguments) == snapshot
    with pytest.raises(
        SecurityReferenceApplicationError, match="BROAD_UNIVERSE_IDEMPOTENCY_CONFLICT"
    ):
        service.execute(**{**arguments, "created_at": NOW + timedelta(seconds=1)})


def test_universe_creation_durably_records_resolved_identity_and_ambiguity() -> None:
    nasdaq, sec, alpaca = _captures()
    records = (*nasdaq.records, *sec.records, *alpaca.records)
    reconciled = reconcile_security_references(records)
    repository = InMemorySecurityReferenceRepository()
    service = CreateCurrentEquityUniverse(repository)
    snapshot = service.execute(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=(
            nasdaq.receipt.receipt_id,
            sec.receipt.receipt_id,
            alpaca.receipt.receipt_id,
        ),
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
        idempotency_key="universe-identity-2026-07-29",
    )
    assert repository.universe(snapshot.universe_id) == snapshot
    assert snapshot in repository.universes()

    for security in reconciled:
        assert repository.identity(security.canonical_hash) == security
    acme_identities = repository.identities_by_symbol("ACME")
    assert acme_identities and all(item.current_symbol == "ACME" for item in acme_identities)

    warning_identities = repository.identities_by_symbol("WARN")
    assert warning_identities
    assert repository.identities_requiring_review() == ()

    # Re-running the exact same build is idempotent and does not duplicate identities.
    service.execute(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=(
            nasdaq.receipt.receipt_id,
            sec.receipt.receipt_id,
            alpaca.receipt.receipt_id,
        ),
        as_of_date=date(2026, 7, 29),
        created_at=NOW,
        idempotency_key="universe-identity-2026-07-29",
    )
    assert len(repository.identities_by_symbol("ACME")) == len(acme_identities)


def test_reference_inputs_fail_closed() -> None:
    with pytest.raises(SecurityReferenceError, match="NASDAQ_SOURCE_PATH_NOT_ALLOWLISTED"):
        parse_nasdaq_symbol_directory(payload=b"x", source_path="/arbitrary", received_at=NOW)
    with pytest.raises(SecurityReferenceError, match="NASDAQ_FILE_CREATION_TRAILER_MISSING"):
        parse_nasdaq_symbol_directory(
            payload=b"A|B\n1|2\nnot-a-trailer\n",
            source_path=NASDAQ_LISTED_PATH,
            received_at=NOW,
        )
    with pytest.raises(SecurityReferenceError, match="SEC_IDENTIFYING_USER_AGENT_REQUIRED"):
        validate_sec_user_agent("anonymous")
    assert validate_sec_user_agent("ApexResearch/1.0 research@example.com")


def _snapshot_for(*securities: SecurityReference):
    reconciled = reconcile_security_references(securities)
    return create_current_equity_universe_snapshot(
        securities=reconciled,
        policy=CurrentEquityUniversePolicy.initial(),
        source_receipt_ids=("receipt-1",),
        as_of_date=NOW.date(),
        created_at=NOW,
    )


def test_determine_execution_capability_never_infers_a_positive_state() -> None:
    unknown_state, unknown_reasons = determine_execution_capability(
        reconcile_security_references((_reference(tradable=EvidenceState.UNKNOWN),))[0]
    )
    assert unknown_state is ExecutionCapabilityState.ALPACA_CAPABILITY_NOT_OBSERVED
    assert unknown_reasons == ("NO_ALPACA_EVIDENCE_OBSERVED",)

    not_tradable_state, not_tradable_reasons = determine_execution_capability(
        reconcile_security_references((_reference(tradable=EvidenceState.NO),))[0]
    )
    assert not_tradable_state is ExecutionCapabilityState.NOT_CONFIRMED_ALPACA_TRADABLE
    assert not_tradable_reasons == ("ALPACA_EVIDENCE_REPORTS_NOT_TRADABLE",)

    tradable_state, tradable_reasons = determine_execution_capability(
        reconcile_security_references((_reference(tradable=EvidenceState.YES),))[0]
    )
    assert tradable_state is ExecutionCapabilityState.ALPACA_TRADABLE
    assert tradable_reasons == ()


def test_valid_listed_common_equity_with_no_alpaca_evidence_remains_research_eligible() -> None:
    """The core fix: research eligibility from official evidence never
    depends on provider (Alpaca) execution evidence being present at all.
    """
    snapshot = _snapshot_for(_reference(tradable=EvidenceState.UNKNOWN))
    assert snapshot.total_considered == 1
    decision = snapshot.decisions[0]
    assert decision.research_eligible is True
    assert decision.research_reasons == ()
    # Provider execution capability remains honestly unknown -- not inferred
    # as tradable, not inferred as non-tradable, and not blocking research.
    assert decision.execution_capability is ExecutionCapabilityState.ALPACA_CAPABILITY_NOT_OBSERVED
    assert snapshot.research_eligible_count == 1
    assert snapshot.execution_capability_known_count == 0
    assert snapshot.execution_capability_unknown_count == 1


def test_explicit_alpaca_non_tradable_evidence_affects_execution_capability_only() -> None:
    """Real Alpaca non-tradability evidence is retained and reported, but it
    is an execution-capability fact, never a research-eligibility exclusion.
    """
    snapshot = _snapshot_for(_reference(tradable=EvidenceState.NO))
    decision = snapshot.decisions[0]
    assert decision.research_eligible is True
    assert "NOT_CONFIRMED_ALPACA_TRADABLE" not in decision.research_reasons
    assert decision.execution_capability is ExecutionCapabilityState.NOT_CONFIRMED_ALPACA_TRADABLE
    assert decision.execution_reasons == ("ALPACA_EVIDENCE_REPORTS_NOT_TRADABLE",)
    assert snapshot.research_eligible_count == 1
    assert snapshot.execution_capability_known_count == 1
    assert snapshot.execution_capability_unknown_count == 0


def test_alpaca_non_tradability_does_not_rewrite_official_listing_identity() -> None:
    """A second, Alpaca-sourced record reporting non-tradability must never
    overwrite the authoritative Nasdaq-sourced listing identity fields.
    """
    nasdaq_record = _reference(
        source_provider="NASDAQ_TRADER",
        security_type=SecurityType.COMMON_STOCK,
        active=EvidenceState.YES,
    )
    alpaca_record = _reference(source_provider="ALPACA", tradable=EvidenceState.NO)
    reconciled = reconcile_security_references((nasdaq_record, alpaca_record))
    security = reconciled[0]
    # Official listing identity is unaffected by the non-authoritative
    # Alpaca-sourced record's execution evidence.
    assert security.security_type is SecurityType.COMMON_STOCK
    assert security.active is EvidenceState.YES
    # Execution capability still honestly reflects the retained Alpaca
    # non-tradability evidence.
    state, _reasons = determine_execution_capability(security)
    assert state is ExecutionCapabilityState.NOT_CONFIRMED_ALPACA_TRADABLE


def test_unsupported_security_type_remains_research_excluded_with_exact_reason() -> None:
    snapshot = _snapshot_for(_reference(security_type=SecurityType.ETF, tradable=EvidenceState.YES))
    decision = snapshot.decisions[0]
    assert decision.research_eligible is False
    assert decision.research_reasons == ("SECURITY_TYPE_ETF_EXCLUDED",)
    # Execution capability is computed independently of the research
    # exclusion above -- Alpaca reports this ETF tradable regardless.
    assert decision.execution_capability is ExecutionCapabilityState.ALPACA_TRADABLE


def test_ambiguous_identity_remains_review_required_and_research_excluded() -> None:
    first = _reference(source_provider="NASDAQ_TRADER", cik="0000001001")
    second = _reference(source_provider="SEC_EDGAR", cik="0000009999")
    snapshot = _snapshot_for(first, second)
    decision = snapshot.decisions[0]
    assert decision.research_eligible is False
    assert "UNRESOLVED_IDENTITY_CONFLICT" in decision.research_reasons
    assert dict(snapshot.research_exclusion_counts)["UNRESOLVED_IDENTITY_CONFLICT"] == 1


def test_legacy_execution_capability_unknown_is_reserved_for_schema_compatibility_only() -> None:
    """``LEGACY_EXECUTION_CAPABILITY_UNKNOWN`` exists solely so a rehydrated
    pre-separation universe snapshot (see
    ``apex.adapters.security_reference_postgres``) can carry an honest,
    explicit compatibility marker. Live evidence -- however Alpaca
    tradability evidence reads -- must never produce it.
    """
    for state in (EvidenceState.YES, EvidenceState.NO, EvidenceState.UNKNOWN):
        capability, _reasons = determine_execution_capability(
            reconcile_security_references((_reference(tradable=state),))[0]
        )
        assert capability is not ExecutionCapabilityState.LEGACY_EXECUTION_CAPABILITY_UNKNOWN


def test_execution_capability_never_influences_research_exclusion_reasons() -> None:
    """Flipping Alpaca evidence across every state never changes whether an
    otherwise-eligible security is research-excluded, or why.
    """
    reasons_by_tradable = {
        state: _snapshot_for(_reference(tradable=state)).decisions[0].research_reasons
        for state in (EvidenceState.YES, EvidenceState.NO, EvidenceState.UNKNOWN)
    }
    assert set(reasons_by_tradable.values()) == {()}
