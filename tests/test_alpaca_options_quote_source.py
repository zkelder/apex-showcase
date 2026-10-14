"""Genuine, credential-gated Alpaca options quote-chain adapter proofs.

No real network call is ever made here: a fake transport stands in for
``AlpacaMarketDataHttpAdapter``'s HTTP layer, shaped exactly like Alpaca's
documented ``/v1beta1/options/snapshots/{underlying_symbol}`` and
``/v2/stocks/snapshots`` responses. Credential-absence proofs make zero
transport calls at all -- there is nothing for the fake to intercept.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.adapters.alpaca_market_data import (
    ALPACA_ENDPOINTS,
    AlpacaMarketDataHttpAdapter,
    ProviderHttpResponse,
    SecuritiesProviderHttpError,
)
from apex.adapters.alpaca_options_quote_source import (
    AlpacaOptionsChainCaptureClient,
    AlpacaOptionsQuoteSourceError,
    build_alpaca_option_contract,
    check_alpaca_options_quote_capability,
    observation_from_alpaca_entry,
    parse_alpaca_option_chain_snapshot_page,
    parse_alpaca_options_timestamp,
    parse_alpaca_underlying_reference_price,
    parse_occ_contract_symbol,
)
from apex.domain.options_intelligence import OptionType
from apex.domain.options_quote_intelligence import OptionChainCaptureBounds
from apex.domain.research_cohort import CohortRole, CohortSector, ResolvedCohortMember

NOW = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)


@dataclass
class FakeTransport:
    responses: Mapping[str, bytes]
    calls: list[tuple[str, str]] = field(default_factory=list)

    def request(self, *, host, target, headers, timeout_seconds, maximum_response_bytes):
        self.calls.append((host, target))
        for marker, body in self.responses.items():
            if marker in target:
                return ProviderHttpResponse(status=200, headers={}, body=body, received_at=NOW)
        raise AssertionError(f"unexpected target {target}")


def _underlying_body(price: str = "500.12") -> bytes:
    return json.dumps(
        {"SPY": {"latestTrade": {"p": price}, "latestQuote": {"bp": "500.10", "ap": "500.14"}}}
    ).encode()


def _chain_body() -> bytes:
    return json.dumps(
        {
            "snapshots": {
                "SPY260918C00500000": {
                    "latestQuote": {
                        "bp": "9.00",
                        "ap": "9.20",
                        "bs": "10",
                        "as": "12",
                        "t": "2026-08-08T14:59:59.123456789Z",
                    },
                    "latestTrade": {"p": "9.10", "s": "3"},
                    "greeks": {"delta": "0.5", "gamma": "0.02", "theta": "-0.05", "vega": "0.4"},
                    "impliedVolatility": "0.15",
                    "dailyBar": {"v": "1200"},
                },
                "SPY260918P00500000": {
                    "latestQuote": {
                        "bp": "8.00",
                        "ap": "8.20",
                        "bs": "5",
                        "as": "6",
                        "t": "2026-08-08T14:59:59Z",
                    },
                    "latestTrade": {"p": "8.10", "s": "2"},
                    "greeks": {"delta": "-0.5", "gamma": "0.02", "theta": "-0.05", "vega": "0.4"},
                    "impliedVolatility": "0.16",
                    "dailyBar": {"v": "900"},
                },
            },
            "next_page_token": None,
        }
    ).encode()


def _member() -> ResolvedCohortMember:
    return ResolvedCohortMember(
        stable_security_id="security-spy-stable",
        symbol="SPY",
        sector=CohortSector.BROAD_MARKET,
        roles=(CohortRole.BROAD_INDEX_ETF,),
        asset_class="ETF",
    )


@pytest.fixture(autouse=True)
def _clean_credentials():
    saved_id = os.environ.pop("APCA_API_KEY_ID", None)
    saved_secret = os.environ.pop("APCA_API_SECRET_KEY", None)
    yield
    os.environ.pop("APCA_API_KEY_ID", None)
    os.environ.pop("APCA_API_SECRET_KEY", None)
    if saved_id is not None:
        os.environ["APCA_API_KEY_ID"] = saved_id
    if saved_secret is not None:
        os.environ["APCA_API_SECRET_KEY"] = saved_secret


# ---------------------------------------------------------------------------
# OCC contract symbol parsing
# ---------------------------------------------------------------------------


def test_occ_symbol_parses_root_expiration_type_and_strike() -> None:
    parsed = parse_occ_contract_symbol("SPY260918C00500000")
    assert parsed is not None
    root, expiration, option_type, strike = parsed
    assert root == "SPY"
    assert expiration == "2026-09-18"
    assert option_type is OptionType.CALL
    assert strike == Decimal("500")


def test_occ_symbol_parses_a_put_with_a_fractional_strike() -> None:
    parsed = parse_occ_contract_symbol("AAPL260116P00150500")
    assert parsed is not None
    _root, _expiration, option_type, strike = parsed
    assert option_type is OptionType.PUT
    assert strike == Decimal("150.5")


def test_occ_symbol_malformed_returns_none_never_raises() -> None:
    assert parse_occ_contract_symbol("not-a-contract-symbol") is None
    assert parse_occ_contract_symbol("SPY99999999X00500000") is None
    assert parse_occ_contract_symbol("") is None


def test_build_alpaca_option_contract_returns_none_for_a_malformed_symbol() -> None:
    contract = build_alpaca_option_contract(
        contract_symbol="garbage",
        underlying_canonical_security_id="security-spy-stable",
        receipt_id="receipt-1",
    )
    assert contract is None


def test_build_alpaca_option_contract_reuses_the_existing_contract_identity_model() -> None:
    contract = build_alpaca_option_contract(
        contract_symbol="SPY260918C00500000",
        underlying_canonical_security_id="security-spy-stable",
        receipt_id="receipt-1",
    )
    assert contract is not None
    assert contract.strike == Decimal("500")
    assert contract.underlying_canonical_security_id == "security-spy-stable"
    assert contract.source_provider == "ALPACA_OPTIONS_MARKET_DATA"


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


def test_alpaca_timestamp_truncates_nanosecond_precision_to_microseconds() -> None:
    parsed = parse_alpaca_options_timestamp("2026-08-08T14:59:59.123456789Z")
    assert parsed.microsecond == 123456
    assert parsed.tzinfo is UTC


def test_alpaca_timestamp_accepts_whole_second_z_suffix() -> None:
    parsed = parse_alpaca_options_timestamp("2026-08-08T14:59:59Z")
    assert parsed == datetime(2026, 8, 8, 14, 59, 59, tzinfo=UTC)


def test_alpaca_timestamp_rejects_malformed_input() -> None:
    with pytest.raises(AlpacaOptionsQuoteSourceError):
        parse_alpaca_options_timestamp("not-a-timestamp")


# ---------------------------------------------------------------------------
# Snapshot page / underlying price parsing (Decimal-exact, never float)
# ---------------------------------------------------------------------------


def test_chain_snapshot_page_parses_every_documented_field_as_decimal() -> None:
    entries, token = parse_alpaca_option_chain_snapshot_page(_chain_body())
    assert token is None
    assert set(entries) == {"SPY260918C00500000", "SPY260918P00500000"}
    call = entries["SPY260918C00500000"]
    assert call.bid == Decimal("9.00")
    assert call.ask == Decimal("9.20")
    assert call.delta == Decimal("0.5")
    assert call.implied_volatility == Decimal("0.15")
    assert call.daily_volume == Decimal("1200")
    assert call.quote_timestamp is not None


def test_chain_snapshot_page_rejects_a_non_object_body() -> None:
    with pytest.raises(AlpacaOptionsQuoteSourceError):
        parse_alpaca_option_chain_snapshot_page(b"[1,2,3]")


def test_underlying_reference_price_prefers_last_trade_over_quote_midpoint() -> None:
    price = parse_alpaca_underlying_reference_price(_underlying_body("500.12"), "SPY")
    assert price == Decimal("500.12")


def test_underlying_reference_price_falls_back_to_midpoint_without_a_trade() -> None:
    body = json.dumps({"SPY": {"latestQuote": {"bp": "500.10", "ap": "500.14"}}}).encode()
    price = parse_alpaca_underlying_reference_price(body, "SPY")
    assert price == Decimal("500.12")


def test_underlying_reference_price_is_none_never_fabricated_without_evidence() -> None:
    body = json.dumps({"SPY": {}}).encode()
    assert parse_alpaca_underlying_reference_price(body, "SPY") is None
    assert parse_alpaca_underlying_reference_price(json.dumps({}).encode(), "SPY") is None


# ---------------------------------------------------------------------------
# GET-only endpoint allowlist (milestone section 3): the options quote
# path reuses this identical, already-reviewed adapter -- proves it can
# reach only market-data/reference endpoints, never order/account/
# portfolio/position mutation.
# ---------------------------------------------------------------------------


def test_endpoint_allowlist_contains_only_the_five_reviewed_get_only_endpoints() -> None:
    assert set(ALPACA_ENDPOINTS) == {
        "historical_daily_bars",
        "latest_snapshots",
        "corporate_actions",
        "asset_master",
        "option_chain_snapshots",
    }
    prohibited_substrings = (
        "order",
        "account",
        "position",
        "activit",
        "watchlist",
        "exercise",
        "assignment",
        "portfolio",
    )
    for endpoint in ALPACA_ENDPOINTS.values():
        lowered_path = endpoint.path.lower()
        for substring in prohibited_substrings:
            assert substring not in lowered_path, f"{endpoint.identity} path leaks {substring!r}"


def test_options_endpoint_is_get_only_market_data_never_the_trading_host() -> None:
    endpoint = ALPACA_ENDPOINTS["option_chain_snapshots"]
    assert endpoint.host == "data.alpaca.markets"
    assert endpoint.host != "paper-api.alpaca.markets"
    assert endpoint.path == "/v1beta1/options/snapshots/{underlying_symbol}"


def test_fetch_pages_and_fetch_one_page_both_reject_an_unreviewed_endpoint_id() -> None:
    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    adapter = AlpacaMarketDataHttpAdapter(transport=FakeTransport(responses={}))
    for prohibited in ("orders", "accounts", "positions", "exercises"):
        with pytest.raises(SecuritiesProviderHttpError, match="PROVIDER_ENDPOINT_REJECTED"):
            adapter.fetch_pages(
                endpoint_id=prohibited,
                query={},
                maximum_pages=1,
                maximum_response_bytes=10_000,
                timeout_seconds=10,
            )
        with pytest.raises(SecuritiesProviderHttpError, match="PROVIDER_ENDPOINT_REJECTED"):
            adapter.fetch_one_page(
                endpoint_id=prohibited,
                query={},
                maximum_response_bytes=10_000,
                timeout_seconds=10,
            )


# ---------------------------------------------------------------------------
# Capability: never inferred from authentication
# ---------------------------------------------------------------------------


def test_capability_check_makes_zero_requests_when_credentials_are_absent() -> None:
    transport = FakeTransport(responses={})
    adapter = AlpacaMarketDataHttpAdapter(transport=transport)
    result = check_alpaca_options_quote_capability(adapter=adapter)
    assert result.credentials_available is False
    assert result.requests_made == 0
    assert not transport.calls
    assert result.capability.quotes.value == "UNKNOWN"
    assert result.capability.greeks.value == "UNKNOWN"
    assert result.unavailable_reason == "ALPACA_CREDENTIALS_ABSENT"


def test_capability_check_makes_a_small_bounded_request_set_when_credentials_are_present() -> None:
    """Reuses the identical bounded ``capture_underlying`` path the real
    19-underlying capture uses (never a separate, narrower ad hoc query):
    one request for the underlying reference price plus one bounded chain
    page -- never unbounded, and never more than the declared
    ``max_requests_per_underlying`` policy."""

    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    transport = FakeTransport(
        responses={
            "/v2/stocks/snapshots": _underlying_body(),
            "/v1beta1/options/snapshots/": _chain_body(),
        }
    )
    adapter = AlpacaMarketDataHttpAdapter(transport=transport)
    result = check_alpaca_options_quote_capability(adapter=adapter)
    assert result.credentials_available is True
    assert result.requests_made == 2
    assert len(transport.calls) == 2
    assert result.capability.contract_discovery.value == "AVAILABLE"
    assert result.capability.quotes.value == "AVAILABLE"
    assert result.capability.greeks.value == "AVAILABLE"
    assert result.capability.implied_volatility.value == "AVAILABLE"
    assert result.capability.open_interest.value == "UNAVAILABLE"
    assert "PROBE_GREEKS_COVERAGE:2/2" in result.capability.limitations
    assert "PROBE_IV_COVERAGE:2/2" in result.capability.limitations


def test_capability_check_samples_every_returned_entry_not_just_the_first() -> None:
    """A single-sample (entry-zero-only) capability read is not
    representative: genuine capability inspection found Alpaca populates
    IV/Greeks per-contract conditionally, so a probe that only inspected
    entry zero could honestly-but-misleadingly report ``UNAVAILABLE`` for a
    field that a clear majority of the same response's other entries have."""

    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    chain = json.dumps(
        {
            "snapshots": {
                # entry 0 (alphabetically/insertion first): no greeks/IV at all.
                "SPY260918C00500000": {
                    "latestQuote": {"bp": "9.00", "ap": "9.20"},
                },
                # entry 1: has greeks/IV.
                "SPY260918P00500000": {
                    "latestQuote": {"bp": "8.00", "ap": "8.20"},
                    "greeks": {"delta": "-0.5"},
                    "impliedVolatility": "0.16",
                },
            },
            "next_page_token": None,
        }
    ).encode()
    transport = FakeTransport(
        responses={"/v2/stocks/snapshots": _underlying_body(), "/v1beta1/options/snapshots/": chain}
    )
    adapter = AlpacaMarketDataHttpAdapter(transport=transport)
    result = check_alpaca_options_quote_capability(adapter=adapter)
    assert result.capability.greeks.value == "AVAILABLE"
    assert result.capability.implied_volatility.value == "AVAILABLE"
    assert "PROBE_GREEKS_COVERAGE:1/2" in result.capability.limitations
    assert "PROBE_IV_COVERAGE:1/2" in result.capability.limitations


def test_capability_check_reports_honestly_when_underlying_price_is_unavailable() -> None:
    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    transport = FakeTransport(responses={"/v2/stocks/snapshots": json.dumps({}).encode()})
    adapter = AlpacaMarketDataHttpAdapter(transport=transport)
    result = check_alpaca_options_quote_capability(adapter=adapter)
    assert result.credentials_available is True
    assert result.requests_made == 1
    assert result.unavailable_reason == "ALPACA_UNDERLYING_PRICE_UNAVAILABLE"
    assert result.capability.contract_discovery.value == "UNKNOWN"
    assert result.capability.quotes.value == "UNKNOWN"


# ---------------------------------------------------------------------------
# Bounded capture client
# ---------------------------------------------------------------------------


def test_capture_underlying_makes_zero_requests_when_credentials_are_absent() -> None:
    transport = FakeTransport(responses={})
    client = AlpacaOptionsChainCaptureClient(
        adapter=AlpacaMarketDataHttpAdapter(transport=transport)
    )
    outcome = client.capture_underlying(
        member=_member(),
        bounds=OptionChainCaptureBounds.create(),
        feed="indicative",
        captured_at=NOW,
    )
    assert outcome.status == "BLOCKED_CREDENTIALS_ABSENT"
    assert outcome.request_count == 0
    assert not transport.calls


def test_capture_underlying_genuinely_parses_a_bounded_two_request_capture() -> None:
    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    transport = FakeTransport(
        responses={
            "/v2/stocks/snapshots": _underlying_body(),
            "/v1beta1/options/snapshots/": _chain_body(),
        }
    )
    client = AlpacaOptionsChainCaptureClient(
        adapter=AlpacaMarketDataHttpAdapter(transport=transport)
    )
    outcome = client.capture_underlying(
        member=_member(),
        bounds=OptionChainCaptureBounds.create(),
        feed="indicative",
        captured_at=NOW,
    )
    assert outcome.status == "CAPTURED"
    assert outcome.request_count == 2
    assert outcome.underlying_reference_price == Decimal("500.12")
    assert len(outcome.contract_entries) == 2

    built = [
        observation_from_alpaca_entry(
            entry=entry,
            underlying_canonical_security_id=_member().stable_security_id,
            underlying_reference_price=outcome.underlying_reference_price,
            captured_at=NOW,
            receipt_id="receipt-1",
        )
        for entry in outcome.contract_entries
    ]
    assert all(item is not None for item in built)
    statuses = {item[1].status.value for item in built if item is not None}
    assert statuses == {"OBSERVED"}


def _paginated_chain_body(*, contract_count: int, has_more: bool, start_index: int = 0) -> bytes:
    snapshots = {}
    for offset in range(contract_count):
        index = start_index + offset
        strike = f"{(500 + index) * 1000:08d}"
        snapshots[f"SPY260918C{strike}"] = {
            "latestQuote": {"bp": "1.00", "ap": "1.20", "t": "2026-08-08T14:59:59Z"},
        }
    body: dict[str, object] = {"snapshots": snapshots}
    if has_more:
        body["next_page_token"] = "next-page-cursor"
    else:
        body["next_page_token"] = None
    return json.dumps(body).encode()


@dataclass
class SequencedTransport:
    """Like ``FakeTransport``, but the chain endpoint returns a different
    body on each successive call (keyed by call ordinal) -- needed to prove
    genuine multi-page continuation, not just single-page behavior."""

    underlying_body: bytes
    chain_bodies: list[bytes]
    calls: list[tuple[str, str]] = field(default_factory=list)

    def request(self, *, host, target, headers, timeout_seconds, maximum_response_bytes):
        self.calls.append((host, target))
        if "/v2/stocks/snapshots" in target:
            return ProviderHttpResponse(
                status=200, headers={}, body=self.underlying_body, received_at=NOW
            )
        if "/v1beta1/options/snapshots/" in target:
            ordinal = sum(1 for _h, t in self.calls if "/v1beta1/options/snapshots/" in t) - 1
            return ProviderHttpResponse(
                status=200, headers={}, body=self.chain_bodies[ordinal], received_at=NOW
            )
        raise AssertionError(f"unexpected target {target}")


def test_fetch_one_page_returns_a_single_page_even_when_a_next_page_token_remains() -> None:
    """Regression proof for the real pagination defect genuine capability
    inspection found: a liquid underlying's full options chain routinely
    exceeds one page even after expiration/moneyness narrowing, so
    ``fetch_pages(maximum_pages=1)`` (which discards the page and raises
    ``PROVIDER_PAGE_LIMIT_REACHED`` whenever a token remains) turned an
    ordinary, well-within-policy underlying into a false
    ``BLOCKED_PROVIDER_ERROR`` with zero contracts retained.
    ``fetch_one_page`` must return the page instead."""

    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    transport = FakeTransport(
        responses={
            "/v1beta1/options/snapshots/": _paginated_chain_body(contract_count=3, has_more=True)
        }
    )
    adapter = AlpacaMarketDataHttpAdapter(transport=transport)
    page = adapter.fetch_one_page(
        endpoint_id="option_chain_snapshots",
        query={"feed": "indicative", "limit": "1000"},
        path_params={"underlying_symbol": "SPY"},
        maximum_response_bytes=2_000_000,
        timeout_seconds=10,
    )
    entries, token = parse_alpaca_option_chain_snapshot_page(page.body)
    assert len(entries) == 3
    assert token == "next-page-cursor"


def test_fetch_one_page_makes_zero_requests_when_credentials_are_absent() -> None:
    transport = FakeTransport(responses={})
    adapter = AlpacaMarketDataHttpAdapter(transport=transport)
    with pytest.raises(Exception, match="ALPACA_CREDENTIALS_REQUIRED"):
        adapter.fetch_one_page(
            endpoint_id="option_chain_snapshots",
            query={"feed": "indicative"},
            path_params={"underlying_symbol": "SPY"},
            maximum_response_bytes=2_000_000,
            timeout_seconds=10,
        )
    assert not transport.calls


def test_capture_underlying_keeps_paging_within_budget_after_the_cap_is_already_reached() -> None:
    """Regression proof for the dense-chain concentration defect (milestone
    "Defined-Risk Options Structure Economics" section 4): page one alone
    already satisfies ``max_contracts_per_underlying``, but a next_page_token
    remains and per-underlying request budget is not yet exhausted, so the
    client must keep paging -- gathering a broader candidate pool for
    ``select_contracts_stratified`` to choose from -- instead of stopping
    the instant the raw cap is first satisfied (the old behavior, which let
    provider response order alone decide which contracts survived)."""

    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    transport = SequencedTransport(
        underlying_body=_underlying_body(),
        chain_bodies=[
            _paginated_chain_body(contract_count=5, has_more=True),
            _paginated_chain_body(contract_count=5, has_more=False, start_index=5),
        ],
    )
    client = AlpacaOptionsChainCaptureClient(
        adapter=AlpacaMarketDataHttpAdapter(transport=transport)
    )
    bounds = OptionChainCaptureBounds.create(max_contracts_per_underlying=5)
    outcome = client.capture_underlying(
        member=_member(), bounds=bounds, feed="indicative", captured_at=NOW
    )
    assert outcome.status == "CAPTURED"
    assert outcome.request_count == 3
    assert len(outcome.contract_entries) == 5
    # Two chain-page requests, not one: the remaining token and remaining
    # budget (default max_requests_per_underlying=3) are both genuinely used
    # even though page one alone already reached the cap.
    assert sum(1 for _h, t in transport.calls if "/v1beta1/options/snapshots/" in t) == 2
    assert outcome.selection is not None
    assert outcome.selection.contracts_available == 10
    assert outcome.selection.contracts_selected == 5
    assert outcome.selection.contracts_excluded_by_cap == 5


def test_capture_underlying_stops_paging_once_the_request_budget_is_exhausted() -> None:
    """A chain that keeps reporting more data available must not page
    forever: the loop still stops once ``max_requests_per_underlying`` is
    spent, even with a next_page_token remaining."""

    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    transport = SequencedTransport(
        underlying_body=_underlying_body(),
        chain_bodies=[
            _paginated_chain_body(contract_count=2, has_more=True),
            _paginated_chain_body(contract_count=2, has_more=True, start_index=2),
        ],
    )
    client = AlpacaOptionsChainCaptureClient(
        adapter=AlpacaMarketDataHttpAdapter(transport=transport)
    )
    bounds = OptionChainCaptureBounds.create(
        max_contracts_per_underlying=200, max_requests_per_underlying=3
    )
    outcome = client.capture_underlying(
        member=_member(), bounds=bounds, feed="indicative", captured_at=NOW
    )
    assert outcome.status == "CAPTURED"
    assert outcome.request_count == 3
    assert sum(1 for _h, t in transport.calls if "/v1beta1/options/snapshots/" in t) == 2
    assert len(outcome.contract_entries) == 4


def test_capture_underlying_continues_to_a_second_page_when_genuinely_needed() -> None:
    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    transport = SequencedTransport(
        underlying_body=_underlying_body(),
        chain_bodies=[
            _paginated_chain_body(contract_count=2, has_more=True),
            _paginated_chain_body(contract_count=2, has_more=False),
        ],
    )
    client = AlpacaOptionsChainCaptureClient(
        adapter=AlpacaMarketDataHttpAdapter(transport=transport)
    )
    bounds = OptionChainCaptureBounds.create(
        max_contracts_per_underlying=200, max_requests_per_underlying=3
    )
    outcome = client.capture_underlying(
        member=_member(), bounds=bounds, feed="indicative", captured_at=NOW
    )
    assert outcome.status == "CAPTURED"
    assert outcome.request_count == 3
    assert sum(1 for _h, t in transport.calls if "/v1beta1/options/snapshots/" in t) == 2


def test_capture_underlying_reports_no_underlying_price_honestly() -> None:
    os.environ["APCA_API_KEY_ID"] = "fake-id"
    os.environ["APCA_API_SECRET_KEY"] = "fake-secret"
    transport = FakeTransport(responses={"/v2/stocks/snapshots": json.dumps({}).encode()})
    client = AlpacaOptionsChainCaptureClient(
        adapter=AlpacaMarketDataHttpAdapter(transport=transport)
    )
    outcome = client.capture_underlying(
        member=_member(),
        bounds=OptionChainCaptureBounds.create(),
        feed="indicative",
        captured_at=NOW,
    )
    assert outcome.status == "NO_UNDERLYING_PRICE"
    assert outcome.request_count == 1
    assert outcome.contract_entries == ()
