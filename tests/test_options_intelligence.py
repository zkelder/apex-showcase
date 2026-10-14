import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.options_reference import (
    MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES,
    MAX_OPTIONS_REFERENCE_BYTES,
    NASDAQ_OPTIONS_DIRECTORY_PATH,
    NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL,
    NASDAQ_OPTIONS_HOST,
    alpaca_options_capability,
    occ_public_reference_capability,
    parse_alpaca_option_contracts,
    parse_alpaca_option_snapshot,
    parse_nasdaq_options_directory,
)
from apex.adapters.options_reference_source import (
    HttpsNasdaqOptionsDirectoryTransport,
    NasdaqOptionsDirectoryCapabilityUnavailable,
    NasdaqOptionsDirectoryClient,
    NasdaqOptionsDirectoryClientError,
    NasdaqOptionsHttpResponse,
    read_bounded_stream,
)
from apex.application.options_intelligence import (
    CreateDefinedRiskOptionsShadowProposal,
    InMemoryOptionsIntelligenceRepository,
    OptionsIntelligenceApplicationError,
    RecordOptionableUnderlyings,
    RecordOptionContracts,
    RecordOptionProviderCapability,
)
from apex.domain.options_intelligence import (
    CapabilityState,
    OptionsIntelligenceError,
    OptionsStrategyType,
    OptionUnderlyingLinkIndex,
    UnderlyingLinkOutcome,
    compare_stock_and_option_expression,
    create_option_chain_snapshot,
    derive_optionable_underlyings,
    evaluate_defined_risk_options,
)

NOW = datetime(2026, 7, 29, 15, tzinfo=UTC)
EXPIRATION = "2026-09-18"


def _chain() -> object:
    contracts_payload = json.dumps(
        {
            "option_contracts": [
                {
                    "id": f"id-{kind}-{strike}",
                    "symbol": f"ACME{kind}{strike}",
                    "underlying_symbol": "ACME",
                    "type": kind,
                    "strike_price": str(strike),
                    "expiration_date": EXPIRATION,
                    "size": "100",
                    "status": "active",
                }
                for kind in ("call", "put")
                for strike in (95, 100, 105)
            ]
        },
        separators=(",", ":"),
    ).encode()
    reference = parse_alpaca_option_contracts(
        payload=contracts_payload,
        received_at=NOW,
        source_timestamp=NOW,
        underlying_ids={"ACME": "security-acme"},
    )
    evidence = []
    for contract in reference.contracts:
        distance = abs(int(contract.strike) - 100)
        evidence.append(
            parse_alpaca_option_snapshot(
                contract=contract,
                payload={
                    "latestQuote": {
                        "bp": str(Decimal("4.8") - Decimal(distance) / 2),
                        "ap": str(Decimal("5.0") - Decimal(distance) / 2),
                    },
                    "latestTrade": {"p": "4.9"},
                    "dailyVolume": "500",
                    "openInterest": "1000",
                    "impliedVolatility": "0.25",
                    "greeks": {
                        "delta": "0.5" if contract.option_type.value == "CALL" else "-0.5",
                        "gamma": "0.03",
                        "theta": "-0.04",
                        "vega": "0.12",
                    },
                },
                quote_timestamp=NOW,
                staleness_seconds=5,
                source_receipt_id=reference.receipt.receipt_id,
            )
        )
    return create_option_chain_snapshot(
        underlying_canonical_security_id="security-acme",
        captured_at=NOW,
        underlying_price="100",
        expiration_start=date(2026, 8, 1),
        expiration_end=date(2026, 10, 1),
        maximum_strike_distance="0.1",
        maximum_contract_count=20,
        market_evidence=tuple(evidence),
        provider="ALPACA_OPTIONS",
        source_receipt_ids=(reference.receipt.receipt_id,),
    )


def test_defined_risk_chain_payoffs_comparison_and_shadow() -> None:
    chain = _chain()
    candidates = evaluate_defined_risk_options(chain=chain, as_of_date=date(2026, 7, 29))
    strategies = {candidate.strategy_type for candidate in candidates}

    assert OptionsStrategyType.LONG_CALL in strategies
    assert OptionsStrategyType.LONG_PUT in strategies
    assert OptionsStrategyType.CALL_DEBIT_SPREAD in strategies
    assert OptionsStrategyType.PUT_DEBIT_SPREAD in strategies
    assert all(candidate.maximum_loss > 0 for candidate in candidates)
    spreads = [
        candidate
        for candidate in candidates
        if candidate.strategy_type
        in {OptionsStrategyType.CALL_DEBIT_SPREAD, OptionsStrategyType.PUT_DEBIT_SPREAD}
    ]
    assert all(candidate.maximum_gain is not None for candidate in spreads)
    assert all(len(candidate.legs) == 2 for candidate in spreads)
    assert all(candidate.legs[1].side == "SHORT_COVERED" for candidate in spreads)

    comparison = compare_stock_and_option_expression(
        underlying_canonical_security_id="security-acme",
        stock_price="100",
        expected_holding_sessions=20,
        candidate=candidates[0],
    )
    assert comparison.stock_capital_required == Decimal("10000")
    assert comparison.option_capital_required == candidates[0].maximum_loss
    assert comparison.selection in {"DEFINED_RISK_OPTION", "STOCK"}

    service = CreateDefinedRiskOptionsShadowProposal(InMemoryOptionsIntelligenceRepository())
    proposal = service.execute(
        discovery_run_id="broad-run-1",
        chains=(chain,),
        candidates=candidates[:3],
        calculation_policy_ids=("payoff-v1", "liquidity-v1"),
        idempotency_key="options-shadow-1",
    )
    assert proposal.authority == "OBSERVATION_ONLY"
    assert proposal.hypothetical_contracts_each == 1
    assert "NO OPTION ORDER AUTHORITY IS ENABLED" in proposal.warnings
    assert not hasattr(proposal, "orders")
    assert (
        service.execute(
            discovery_run_id="broad-run-1",
            chains=(chain,),
            candidates=candidates[:3],
            calculation_policy_ids=("payoff-v1", "liquidity-v1"),
            idempotency_key="options-shadow-1",
        )
        == proposal
    )
    with pytest.raises(
        OptionsIntelligenceApplicationError, match="OPTIONS_SHADOW_IDEMPOTENCY_CONFLICT"
    ):
        service.execute(
            discovery_run_id="changed",
            chains=(chain,),
            candidates=candidates[:3],
            calculation_policy_ids=("payoff-v1", "liquidity-v1"),
            idempotency_key="options-shadow-1",
        )


def test_options_capability_bounds_liquidity_and_missing_greeks_are_honest() -> None:
    assert (
        occ_public_reference_capability(checked_at=NOW).contract_discovery
        is CapabilityState.PAID_REFERENCE_REQUIRED
    )
    assert alpaca_options_capability().multi_leg_paper is CapabilityState.UNAVAILABLE

    chain = _chain()
    first = chain.contracts[0]
    missing = parse_alpaca_option_snapshot(
        contract=first.contract,
        payload={
            "latestQuote": {"bp": "0", "ap": "1"},
            "latestTrade": {"p": "1"},
        },
        quote_timestamp=NOW - timedelta(hours=1),
        staleness_seconds=3600,
        source_receipt_id="receipt",
    )
    assert {"ZERO_BID", "STALE_QUOTE"}.issubset(missing.exclusions)
    assert missing.delta is None
    constrained = create_option_chain_snapshot(
        underlying_canonical_security_id="security-acme",
        captured_at=NOW,
        underlying_price="100",
        expiration_start=date(2026, 8, 1),
        expiration_end=date(2026, 10, 1),
        maximum_strike_distance="0.1",
        maximum_contract_count=1,
        market_evidence=(missing,),
        provider="ALPACA_OPTIONS",
        source_receipt_ids=("receipt",),
    )
    assert not evaluate_defined_risk_options(chain=constrained, as_of_date=date(2026, 7, 29))


def test_optionable_underlying_decisions_are_durable_and_distinguish_no_contracts() -> None:
    payload = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"
        b"ACME|N|P|09/18/2026|95.00|ACME|Acme Corporation|N\n"
        b"File Creation Time: 0729202611:00|||||||\n"
    )
    parsed = parse_nasdaq_options_directory(
        payload=payload, received_at=NOW, underlying_ids={"ACME": "security-acme"}
    )
    repository = InMemoryOptionsIntelligenceRepository()
    RecordOptionContracts(repository).execute(contracts=parsed.contracts, recorded_at=NOW)
    assert len(repository.contracts()) == 2
    assert len(repository.contracts_for_underlying("security-acme")) == 2

    decisions = RecordOptionableUnderlyings(repository).execute(
        universe_security_ids=("security-acme", "security-beta"),
        contracts=repository.contracts(),
        source_provider="NASDAQ_OPTIONS_DIRECTORY",
        as_of="2026-07-29",
        recorded_at=NOW,
    )
    assert len(decisions) == 2
    acme_decision = repository.optionable_underlying("security-acme")
    beta_decision = repository.optionable_underlying("security-beta")
    assert acme_decision is not None and acme_decision.optionable
    assert acme_decision.contract_count == 2
    assert beta_decision is not None and not beta_decision.optionable
    assert beta_decision.reasons == ("NO_CONTRACTS_IN_CAPTURED_REFERENCE",)
    assert {item.canonical_security_id for item in repository.optionable_underlyings()} == {
        "security-acme",
        "security-beta",
    }

    with pytest.raises(OptionsIntelligenceError, match="OPTIONABLE_UNDERLYING_UNIVERSE_REQUIRED"):
        derive_optionable_underlyings(
            universe_security_ids=(), contracts=(), source_provider="X", as_of="2026-07-29"
        )


def test_provider_capabilities_are_durably_recorded_and_queryable_by_provider() -> None:
    repository = InMemoryOptionsIntelligenceRepository()
    occ = RecordOptionProviderCapability(repository).execute(
        capability=occ_public_reference_capability(checked_at=NOW), recorded_at=NOW
    )
    alpaca = RecordOptionProviderCapability(repository).execute(
        capability=alpaca_options_capability(), recorded_at=NOW
    )
    assert repository.capabilities() == (occ, alpaca) or repository.capabilities() == (alpaca, occ)
    assert repository.capabilities(provider="OCC") == (occ,)
    assert repository.capabilities(provider="ALPACA_MARKET_DATA") == (alpaca,)


class _OptionsTransport:
    def __init__(self, response: NasdaqOptionsHttpResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, int, int, int]] = []

    def get(
        self,
        *,
        host: str,
        path: str,
        timeout_seconds: int,
        maximum_response_bytes: int,
        maximum_total_runtime_seconds: int,
    ) -> NasdaqOptionsHttpResponse:
        self.calls.append(
            (host, path, timeout_seconds, maximum_response_bytes, maximum_total_runtime_seconds)
        )
        return self.response


def test_options_directory_client_is_one_request_bounded_and_reference_only() -> None:
    payload = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"
        b"File Creation Time: 0729202611:00|||||||\n"
    )
    transport = _OptionsTransport(NasdaqOptionsHttpResponse(200, payload))
    raw, parsed = NasdaqOptionsDirectoryClient(transport).capture(
        received_at=NOW,
        underlying_ids={"ACME": "security-acme"},
        timeout_seconds=5,
        maximum_response_bytes=len(payload),
    )
    assert raw == payload
    assert len(parsed.contracts) == 1
    assert transport.calls == [
        (NASDAQ_OPTIONS_HOST, NASDAQ_OPTIONS_DIRECTORY_PATH, 5, len(payload), 180)
    ]
    contract = parsed.contracts[0]
    # Reference identity only: no quote, Greek, or implied-volatility field exists to capture.
    assert not hasattr(contract, "bid")
    assert not hasattr(contract, "implied_volatility")

    oversized = _OptionsTransport(NasdaqOptionsHttpResponse(200, payload))
    with pytest.raises(
        NasdaqOptionsDirectoryClientError, match="NASDAQ_OPTIONS_RESPONSE_SIZE_INVALID"
    ):
        NasdaqOptionsDirectoryClient(oversized).capture(
            received_at=NOW,
            underlying_ids={"ACME": "security-acme"},
            timeout_seconds=5,
            maximum_response_bytes=len(payload) - 1,
        )


def test_nasdaq_options_directory_uses_documented_series_schema() -> None:
    payload = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"
        b"File Creation Time: 0729202611:00|||||||\n"
    )

    parsed = parse_nasdaq_options_directory(
        payload=payload,
        received_at=NOW,
        underlying_ids={"ACME": "security-acme"},
    )

    assert parsed.contracts[0].provider_contract_id == "ACME|C|2026-09-18|105.00|ACME"
    assert parsed.contracts[0].standardized_symbol is None
    assert parsed.contracts[0].expiration == EXPIRATION
    assert "ROOT_SYMBOL_IS_NOT_STANDARDIZED_CONTRACT_SYMBOL" in parsed.contracts[0].limitations


def test_options_directory_source_is_verified_contract_level_not_underlying_level() -> None:
    """First verify what the official payload actually contains: every row
    carries its own strike, expiration, and call/put side, so this source is
    genuinely contract (option-series) level, never merely a list of
    optionable underlyings mislabeled as contracts.
    """
    assert NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL == "CONTRACT_LEVEL"
    payload = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"
        b"ACME|N|C|10/16/2026|110.00|ACME|Acme Corporation|N\n"
        b"ACME|N|P|09/18/2026|95.00|ACME|Acme Corporation|N\n"
        b"File Creation Time: 0729202611:00|||||||\n"
    )
    parsed = parse_nasdaq_options_directory(
        payload=payload, received_at=NOW, underlying_ids={"ACME": "security-acme"}
    )
    # Three distinct contracts for one underlying -- not one row per
    # underlying with quotes/strikes inferred.
    assert len(parsed.contracts) == 3
    assert parsed.linked_underlying_count == 1
    assert {item.expiration for item in parsed.contracts} == {"2026-09-18", "2026-10-16"}
    assert {item.strike for item in parsed.contracts} == {
        Decimal("105.00"),
        Decimal("95.00"),
        Decimal("110.00"),
    }


def test_options_directory_linkage_never_joins_ambiguously_and_reports_exact_counts() -> None:
    payload = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
        b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n"
        b"BRKA|N|C|09/18/2026|300.00|BRK/A|Berkshire Hathaway|N\n"
        b"DUPA|N|C|09/18/2026|10.00|DUP A|Duplicate Co|N\n"
        b"GHOST|N|C|09/18/2026|50.00|GHOST|Ghost Corp|N\n"
        b"File Creation Time: 0729202611:00|||||||\n"
    )
    underlying_ids = {
        "ACME": "security-acme",
        "BRK.A": "security-brk-a",
        "DUP.A": "security-1",
        "DUP-A": "security-2",
    }
    parsed = parse_nasdaq_options_directory(
        payload=payload, received_at=NOW, underlying_ids=underlying_ids
    )
    assert parsed.total_rows == 4
    # ACME links by exact match; BRK/A links only via normalized-punctuation
    # fallback (the file spells it differently from our reconciled symbol).
    assert len(parsed.contracts) == 2
    assert parsed.linked_underlying_count == 2
    assert {item.underlying_canonical_security_id for item in parsed.contracts} == {
        "security-acme",
        "security-brk-a",
    }
    brk_contract = next(
        item
        for item in parsed.contracts
        if item.underlying_canonical_security_id == "security-brk-a"
    )
    assert "UNDERLYING_LINKED_VIA_NORMALIZED_PUNCTUATION_MATCH" in brk_contract.limitations
    # DUP A normalizes to a key with two distinct candidate identities: never
    # joined despite the text match.
    assert parsed.ambiguous_count == 1
    # GHOST matches nothing in the reconciled universe: never silently dropped.
    assert parsed.unlinked_count == 1
    ghost_rejection = next(
        item for item in parsed.rejections if item.raw_underlying_symbol == "GHOST"
    )
    assert ghost_rejection.outcome is UnderlyingLinkOutcome.UNLINKED
    assert ghost_rejection.reasons == ("NO_MATCHING_RECONCILED_IDENTITY",)
    assert ghost_rejection.candidate_security_ids == ()
    dup_rejection = next(
        item for item in parsed.rejections if item.raw_underlying_symbol == "DUP A"
    )
    assert dup_rejection.outcome is UnderlyingLinkOutcome.AMBIGUOUS
    assert set(dup_rejection.candidate_security_ids) == {"security-1", "security-2"}
    assert dict(parsed.rejection_reason_counts) == {
        "MULTIPLE_NORMALIZED_IDENTITY_MATCHES": 1,
        "NO_MATCHING_RECONCILED_IDENTITY": 1,
    }
    # Every rejection is traceable to the exact raw payload it came from.
    digest = hashlib.sha256(payload).hexdigest()
    assert all(item.source_payload_hash == digest for item in parsed.rejections)


def test_expanding_the_universe_with_other_listed_identities_newly_links_underlyings() -> None:
    """Linkage depends only on the underlying_ids map handed in -- it has no
    concept of which official listing source (nasdaqlisted.txt vs
    otherlisted.txt) contributed an identity. A security only reachable via
    otherlisted.txt reconciliation is UNLINKED (not silently dropped) before
    that source is captured, and genuinely LINKED once the expanded
    universe's identity is included -- without recapturing the options
    contract data or mutating the earlier rejection evidence."""
    payload = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
        b"OTHR|N|C|09/18/2026|50.00|OTHR|Other Exchange Corporation|N\n"
        b"File Creation Time: 0729202611:00|||||||\n"
    )
    before = parse_nasdaq_options_directory(payload=payload, received_at=NOW, underlying_ids={})
    assert before.linked_underlying_count == 0
    assert before.unlinked_count == 1
    assert before.rejections[0].raw_underlying_symbol == "OTHR"
    assert before.rejections[0].reasons == ("NO_MATCHING_RECONCILED_IDENTITY",)

    after = parse_nasdaq_options_directory(
        payload=payload, received_at=NOW, underlying_ids={"OTHR": "security-othr"}
    )
    assert after.linked_underlying_count == 1
    assert after.unlinked_count == 0
    assert after.contracts[0].underlying_canonical_security_id == "security-othr"
    # The raw retained payload and its hash are identical either way -- only
    # the reconciled-identity universe changed, not the options evidence.
    assert before.receipt.payload_sha256 == after.receipt.payload_sha256


def test_underlying_link_index_exact_normalized_ambiguous_and_unlinked_outcomes() -> None:
    index = OptionUnderlyingLinkIndex.build({"ACME": "security-acme", "BRK.A": "security-brk-a"})

    exact = index.resolve("acme")
    assert exact.outcome is UnderlyingLinkOutcome.LINKED
    assert exact.canonical_security_id == "security-acme"
    assert exact.reasons == ("EXACT_SYMBOL_MATCH",)

    normalized = index.resolve("BRK/A")
    assert normalized.outcome is UnderlyingLinkOutcome.LINKED
    assert normalized.canonical_security_id == "security-brk-a"
    assert normalized.reasons == ("NORMALIZED_PUNCTUATION_MATCH",)

    unlinked = index.resolve("NOPE")
    assert unlinked.outcome is UnderlyingLinkOutcome.UNLINKED
    assert unlinked.canonical_security_id is None
    assert unlinked.reasons == ("NO_MATCHING_RECONCILED_IDENTITY",)

    ambiguous_index = OptionUnderlyingLinkIndex.build(
        {"BRK.A": "security-1", "BRK-A": "security-2"}
    )
    ambiguous = ambiguous_index.resolve("BRK A")
    assert ambiguous.outcome is UnderlyingLinkOutcome.AMBIGUOUS
    assert ambiguous.canonical_security_id is None
    assert set(ambiguous.candidate_security_ids) == {"security-1", "security-2"}


def test_nasdaq_options_directory_bound_is_source_specific_and_exceeds_generic_bound() -> None:
    assert MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES == 120_000_000
    assert MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES > MAX_OPTIONS_REFERENCE_BYTES
    # Bigger than the generic 20 MB bound that governs every other options
    # payload in this codebase, but still well below the 120 MB
    # source-specific one: this proves the parser is not silently bound by
    # the smaller, generic constant.  It fails later (malformed structure),
    # never on size.
    below_source_bound_above_generic_bound = b"0" * (MAX_OPTIONS_REFERENCE_BYTES + 1_000_000)
    with pytest.raises(OptionsIntelligenceError, match="NASDAQ_OPTIONS_FILE_TRAILER_REQUIRED"):
        parse_nasdaq_options_directory(
            payload=below_source_bound_above_generic_bound, received_at=NOW, underlying_ids={}
        )
    # Above the source-specific bound: fails closed on size, not schema --
    # the earlier 20 MB failure is never mistaken for a successful partial
    # snapshot, and neither is any payload beyond the new bound.
    with pytest.raises(OptionsIntelligenceError, match="NASDAQ_OPTIONS_DIRECTORY_SIZE_INVALID"):
        parse_nasdaq_options_directory(
            payload=b"0" * (MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES + 1),
            received_at=NOW,
            underlying_ids={},
        )


def test_read_bounded_stream_reconstructs_bytes_with_incremental_hash() -> None:
    chunks = [b"abc", b"def", b"ghi", b""]

    def reader(amount: int) -> bytes:
        return chunks.pop(0)

    body, digest, truncated = read_bounded_stream(
        reader,
        maximum_response_bytes=100,
        chunk_bytes=3,
        declared_length=9,
        deadline=time.monotonic() + 10,
    )
    assert body == b"abcdefghi"
    assert digest == hashlib.sha256(b"abcdefghi").hexdigest()
    assert truncated is False


def test_read_bounded_stream_aborts_before_buffering_the_full_oversized_body() -> None:
    reads: list[bytes] = []
    chunks = [b"x" * 10, b"y" * 10, b"z" * 10, b""]

    def reader(amount: int) -> bytes:
        chunk = chunks.pop(0)
        reads.append(chunk)
        return chunk

    with pytest.raises(
        NasdaqOptionsDirectoryClientError, match="NASDAQ_OPTIONS_RESPONSE_TOO_LARGE"
    ):
        read_bounded_stream(
            reader,
            maximum_response_bytes=15,
            chunk_bytes=10,
            declared_length=None,
            deadline=time.monotonic() + 10,
        )
    # Rejected as soon as the second chunk crosses the bound; the third
    # chunk (which would complete a 30-byte body) was never read at all.
    assert reads == [b"x" * 10, b"y" * 10]


def test_read_bounded_stream_detects_truncation_against_declared_length() -> None:
    chunks = [b"abc", b""]

    def reader(amount: int) -> bytes:
        return chunks.pop(0)

    _body, _digest, truncated = read_bounded_stream(
        reader,
        maximum_response_bytes=100,
        chunk_bytes=3,
        declared_length=10,
        deadline=time.monotonic() + 10,
    )
    assert truncated is True


def test_read_bounded_stream_enforces_total_runtime_deadline() -> None:
    with pytest.raises(
        NasdaqOptionsDirectoryCapabilityUnavailable, match="NASDAQ_OPTIONS_CAPTURE_RUNTIME_EXCEEDED"
    ):
        read_bounded_stream(
            lambda amount: b"a",
            maximum_response_bytes=100,
            chunk_bytes=1,
            declared_length=None,
            deadline=time.monotonic() - 1,
        )


class _FakeHttpResponse:
    def __init__(self, status: int, chunks: list[bytes], headers: dict[str, str]) -> None:
        self.status = status
        self._chunks = chunks
        self._headers = headers

    def getheader(self, name: str) -> str | None:
        return self._headers.get(name)

    def read(self, amount: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeHttpsConnection:
    def __init__(self, response: _FakeHttpResponse) -> None:
        self._response = response
        self.closed = False

    def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
        del method, path, headers

    def getresponse(self) -> _FakeHttpResponse:
        return self._response

    def close(self) -> None:
        self.closed = True


def test_https_transport_streams_hashes_and_rejects_a_truncated_response(monkeypatch) -> None:
    fake_response = _FakeHttpResponse(200, [b"only-part"], {"Content-Length": "100"})
    fake_connection = _FakeHttpsConnection(fake_response)
    monkeypatch.setattr(
        "apex.adapters.options_reference_source.http.client.HTTPSConnection",
        lambda *args, **kwargs: fake_connection,
    )
    transport = HttpsNasdaqOptionsDirectoryTransport()
    with pytest.raises(
        NasdaqOptionsDirectoryClientError, match="NASDAQ_OPTIONS_RESPONSE_TRUNCATED"
    ):
        transport.get(
            host=NASDAQ_OPTIONS_HOST,
            path=NASDAQ_OPTIONS_DIRECTORY_PATH,
            timeout_seconds=5,
            maximum_response_bytes=1000,
            maximum_total_runtime_seconds=10,
        )
    assert fake_connection.closed is True


def test_https_transport_succeeds_with_incremental_hash_matching_the_full_body(monkeypatch) -> None:
    payload = b"a" * 30
    fake_response = _FakeHttpResponse(
        200, [payload[:10], payload[10:20], payload[20:], b""], {"Content-Length": "30"}
    )
    fake_connection = _FakeHttpsConnection(fake_response)
    monkeypatch.setattr(
        "apex.adapters.options_reference_source.http.client.HTTPSConnection",
        lambda *args, **kwargs: fake_connection,
    )
    transport = HttpsNasdaqOptionsDirectoryTransport()
    response = transport.get(
        host=NASDAQ_OPTIONS_HOST,
        path=NASDAQ_OPTIONS_DIRECTORY_PATH,
        timeout_seconds=5,
        maximum_response_bytes=1000,
        maximum_total_runtime_seconds=10,
    )
    assert response.body == payload
    assert response.sha256 == hashlib.sha256(payload).hexdigest()
    assert response.truncated is False
    assert fake_connection.closed is True


def test_options_directory_client_enforces_total_runtime_bound_distinct_from_read_timeout() -> None:
    with pytest.raises(
        NasdaqOptionsDirectoryClientError, match="NASDAQ_OPTIONS_CAPTURE_BOUNDS_INVALID"
    ):
        NasdaqOptionsDirectoryClient().capture(
            received_at=NOW,
            underlying_ids={},
            timeout_seconds=5,
            maximum_total_runtime_seconds=100_000,
        )


def test_options_directory_malformed_rows_fail_closed_and_missing_schema_columns_are_rejected() -> (
    None
):
    header = (
        b"Root Symbol|Options Closing Type|Options Type|Expiration Date|"
        b"Explicit Strike Price|Underlying Symbol|Underlying Issue Name|Pending\n"
    )
    trailer = b"File Creation Time: 0729202611:00|||||||\n"

    # An unrecognized Options Type value.
    with pytest.raises(OptionsIntelligenceError, match="NASDAQ_OPTIONS_ROW_INVALID"):
        parse_nasdaq_options_directory(
            payload=header + b"ACME|N|X|09/18/2026|105.00|ACME|Acme Corporation|N\n" + trailer,
            received_at=NOW,
            underlying_ids={"ACME": "security-acme"},
        )

    # An unrecognized Options Closing Type value.
    with pytest.raises(OptionsIntelligenceError, match="NASDAQ_OPTIONS_ROW_INVALID"):
        parse_nasdaq_options_directory(
            payload=header + b"ACME|Z|C|09/18/2026|105.00|ACME|Acme Corporation|N\n" + trailer,
            received_at=NOW,
            underlying_ids={"ACME": "security-acme"},
        )

    # An unrecognized Pending value.
    with pytest.raises(OptionsIntelligenceError, match="NASDAQ_OPTIONS_ROW_INVALID"):
        parse_nasdaq_options_directory(
            payload=header + b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|MAYBE\n" + trailer,
            received_at=NOW,
            underlying_ids={"ACME": "security-acme"},
        )

    # A malformed expiration date.
    with pytest.raises(OptionsIntelligenceError, match="NASDAQ_OPTIONS_EXPIRATION_INVALID"):
        parse_nasdaq_options_directory(
            payload=header + b"ACME|N|C|2026-09-18|105.00|ACME|Acme Corporation|N\n" + trailer,
            received_at=NOW,
            underlying_ids={"ACME": "security-acme"},
        )

    # A schema change dropping a required column fails closed rather than
    # silently parsing a partial or reinterpreted row shape.
    with pytest.raises(OptionsIntelligenceError, match="NASDAQ_OPTIONS_SCHEMA_UNSUPPORTED"):
        parse_nasdaq_options_directory(
            payload=(
                b"Root Symbol|Options Type|Expiration Date|Explicit Strike Price|"
                b"Underlying Symbol|Underlying Issue Name|Pending\n"
                b"ACME|C|09/18/2026|105.00|ACME|Acme Corporation|N\n" + trailer
            ),
            received_at=NOW,
            underlying_ids={"ACME": "security-acme"},
        )

    # Missing the mandatory trailer sentinel: an incomplete/truncated capture
    # is never accepted as a complete one.
    with pytest.raises(OptionsIntelligenceError, match="NASDAQ_OPTIONS_FILE_TRAILER_REQUIRED"):
        parse_nasdaq_options_directory(
            payload=header + b"ACME|N|C|09/18/2026|105.00|ACME|Acme Corporation|N\n",
            received_at=NOW,
            underlying_ids={"ACME": "security-acme"},
        )
