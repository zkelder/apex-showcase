"""Genuine, credential-gated Alpaca options quote-chain capability check and
bounded capture client.

Reuses ``AlpacaMarketDataHttpAdapter``'s reviewed GET-only endpoint policy
exactly as-is (the ``option_chain_snapshots`` and ``latest_snapshots``
endpoint entries already registered there); this module adds no new
credential path, no new transport, and no order/account/portfolio
capability. Capability is never inferred from authentication alone: when
``APCA_API_KEY_ID``/``APCA_API_SECRET_KEY`` are absent, every function here
makes zero network requests and reports that honestly.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from apex.adapters.alpaca_market_data import (
    AlpacaMarketDataHttpAdapter,
    SecuritiesProviderHttpError,
)
from apex.domain.options_intelligence import (
    CapabilityState,
    ExerciseStyle,
    OptionContract,
    OptionProviderCapabilities,
    OptionType,
    SettlementStyle,
)
from apex.domain.options_quote_intelligence import (
    ChainSampleCandidate,
    OptionChainCaptureBounds,
    OptionQuoteObservation,
    OptionsQuoteProviderCapabilitySnapshot,
    StratifiedChainSelection,
    select_contracts_stratified,
)
from apex.domain.research_cohort import CohortRole, CohortSector, ResolvedCohortMember

ALPACA_OPTIONS_QUOTE_PROVIDER = "ALPACA_OPTIONS_MARKET_DATA"
ALPACA_UNDERLYING_PRICE_SOURCE = "ALPACA_LATEST_STOCK_SNAPSHOT_IEX"
_OCC_SYMBOL_PATTERN = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
_ALPACA_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$"
)


class AlpacaOptionsQuoteSourceError(RuntimeError):
    pass


def parse_occ_contract_symbol(symbol: str) -> tuple[str, str, OptionType, Decimal] | None:
    """Parse a standard OCC option symbol (``ROOT + YYMMDD + C|P + strike*1000``,
    8-digit zero-padded) into ``(root, expiration_iso_date, type, strike)``.

    Returns ``None`` -- never raises -- for a malformed symbol: one bad row
    from the provider must never fail an entire bounded capture.
    """

    match = _OCC_SYMBOL_PATTERN.match(symbol.strip().upper())
    if match is None:
        return None
    root, yymmdd, side, strike_digits = match.groups()
    try:
        expiration = date(2000 + int(yymmdd[0:2]), int(yymmdd[2:4]), int(yymmdd[4:6])).isoformat()
    except ValueError:
        return None
    option_type = OptionType.CALL if side == "C" else OptionType.PUT
    strike = Decimal(strike_digits) / Decimal(1000)
    if strike <= 0:
        return None
    return root, expiration, option_type, strike


def build_alpaca_option_contract(
    *,
    contract_symbol: str,
    underlying_canonical_security_id: str,
    receipt_id: str,
) -> OptionContract | None:
    """One fresh, provider-neutral ``OptionContract`` identity derived
    directly from a live snapshot's contract-symbol key -- never a lookup
    against a separate contracts endpoint, and never a re-use of a
    different provider's ``canonical_contract_id`` for the same real-world
    series (see module docstring in ``domain.options_quote_intelligence``).
    """

    parsed = parse_occ_contract_symbol(contract_symbol)
    if parsed is None:
        return None
    _root, expiration, option_type, strike = parsed
    return OptionContract.create(
        provider_contract_id=contract_symbol,
        underlying_canonical_security_id=underlying_canonical_security_id,
        standardized_symbol=contract_symbol,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        exercise_style=ExerciseStyle.AMERICAN,
        settlement_style=SettlementStyle.PHYSICAL,
        multiplier="100",
        deliverable="100 SHARES",
        listing_exchange=None,
        state="ACTIVE",
        contract_size_state="STANDARD",
        adjustment_state="UNKNOWN",
        source_provider=ALPACA_OPTIONS_QUOTE_PROVIDER,
        reference_receipt_ids=(receipt_id,),
        limitations=("CONTRACT_IDENTITY_DERIVED_FROM_LIVE_OCC_SYMBOL_NOT_A_SEPARATE_LOOKUP",),
    )


def parse_alpaca_options_timestamp(value: str) -> datetime:
    match = _ALPACA_TIMESTAMP_PATTERN.match(value.strip())
    if match is None:
        raise AlpacaOptionsQuoteSourceError("ALPACA_OPTIONS_TIMESTAMP_INVALID")
    base = match.group("base")
    frac = (match.group("frac") or "").ljust(6, "0")[:6]
    offset = match.group("offset")
    offset = "+00:00" if offset == "Z" else offset
    try:
        parsed = datetime.fromisoformat(f"{base}.{frac}{offset}")
    except ValueError as error:
        raise AlpacaOptionsQuoteSourceError("ALPACA_OPTIONS_TIMESTAMP_INVALID") from error
    return parsed.astimezone(UTC)


def _decimal_field(source: dict[str, Any], key: str) -> Decimal | None:
    value = source.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise AlpacaOptionsQuoteSourceError("ALPACA_OPTIONS_NUMERIC_FIELD_INVALID")
    if isinstance(value, (str, int, Decimal)):
        return Decimal(value)
    raise AlpacaOptionsQuoteSourceError("ALPACA_OPTIONS_NUMERIC_FIELD_INVALID")


@dataclass(frozen=True, slots=True)
class ParsedAlpacaOptionSnapshotEntry:
    """Every raw field one ``snapshots["<symbol>"]`` entry can carry,
    exactly as documented at ``docs.alpaca.markets/us/reference/optionsnapshots``
    -- parsed with ``Decimal``-exact JSON number handling (``parse_float=Decimal``),
    never through an intermediate binary float."""

    contract_symbol: str
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    last_trade: Decimal | None
    trade_size: Decimal | None
    daily_volume: Decimal | None
    implied_volatility: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    rho: Decimal | None
    quote_timestamp: datetime | None


def parse_alpaca_option_snapshot_entry(
    contract_symbol: str, payload: dict[str, Any]
) -> ParsedAlpacaOptionSnapshotEntry:
    quote = payload.get("latestQuote")
    quote_dict = quote if isinstance(quote, dict) else {}
    trade = payload.get("latestTrade")
    trade_dict = trade if isinstance(trade, dict) else {}
    greeks = payload.get("greeks")
    greeks_dict = greeks if isinstance(greeks, dict) else {}
    daily_bar = payload.get("dailyBar")
    daily_bar_dict = daily_bar if isinstance(daily_bar, dict) else {}
    quote_timestamp_raw = quote_dict.get("t")
    quote_timestamp = (
        parse_alpaca_options_timestamp(quote_timestamp_raw)
        if isinstance(quote_timestamp_raw, str)
        else None
    )
    return ParsedAlpacaOptionSnapshotEntry(
        contract_symbol=contract_symbol,
        bid=_decimal_field(quote_dict, "bp"),
        ask=_decimal_field(quote_dict, "ap"),
        bid_size=_decimal_field(quote_dict, "bs"),
        ask_size=_decimal_field(quote_dict, "as"),
        last_trade=_decimal_field(trade_dict, "p"),
        trade_size=_decimal_field(trade_dict, "s"),
        daily_volume=_decimal_field(daily_bar_dict, "v"),
        implied_volatility=_decimal_field(payload, "impliedVolatility"),
        delta=_decimal_field(greeks_dict, "delta"),
        gamma=_decimal_field(greeks_dict, "gamma"),
        theta=_decimal_field(greeks_dict, "theta"),
        vega=_decimal_field(greeks_dict, "vega"),
        rho=_decimal_field(greeks_dict, "rho"),
        quote_timestamp=quote_timestamp,
    )


def parse_alpaca_option_chain_snapshot_page(
    body: bytes,
) -> tuple[dict[str, ParsedAlpacaOptionSnapshotEntry], str | None]:
    """Parse one raw ``option_chain_snapshots`` page body.

    Returns ``(entries_by_contract_symbol, next_page_token)``. Fails
    closed -- never returns a partial silently-truncated result -- on a
    response shape that does not match the documented schema.
    """

    try:
        decoded = json.loads(body.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlpacaOptionsQuoteSourceError("ALPACA_OPTIONS_SNAPSHOT_JSON_INVALID") from error
    if not isinstance(decoded, dict):
        raise AlpacaOptionsQuoteSourceError("ALPACA_OPTIONS_SNAPSHOT_OBJECT_REQUIRED")
    snapshots = decoded.get("snapshots")
    if snapshots is None:
        snapshots = {}
    if not isinstance(snapshots, dict):
        raise AlpacaOptionsQuoteSourceError("ALPACA_OPTIONS_SNAPSHOT_MAP_REQUIRED")
    entries: dict[str, ParsedAlpacaOptionSnapshotEntry] = {}
    for symbol, payload in snapshots.items():
        if not isinstance(symbol, str) or not isinstance(payload, dict):
            raise AlpacaOptionsQuoteSourceError("ALPACA_OPTIONS_SNAPSHOT_ENTRY_INVALID")
        entries[symbol] = parse_alpaca_option_snapshot_entry(symbol, payload)
    token = decoded.get("next_page_token")
    if token is not None and not isinstance(token, str):
        raise AlpacaOptionsQuoteSourceError("ALPACA_OPTIONS_SNAPSHOT_PAGE_TOKEN_INVALID")
    return entries, token


def parse_alpaca_underlying_reference_price(body: bytes, symbol: str) -> Decimal | None:
    """Parse ``/v2/stocks/snapshots`` for one symbol's current reference
    price: the latest trade price when present, else the two-sided quote
    midpoint. Returns ``None`` -- never a fabricated price -- when neither
    is present."""

    try:
        decoded = json.loads(body.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlpacaOptionsQuoteSourceError("ALPACA_UNDERLYING_SNAPSHOT_JSON_INVALID") from error
    if not isinstance(decoded, dict):
        raise AlpacaOptionsQuoteSourceError("ALPACA_UNDERLYING_SNAPSHOT_OBJECT_REQUIRED")
    entry = decoded.get(symbol)
    if not isinstance(entry, dict):
        return None
    trade = entry.get("latestTrade")
    if isinstance(trade, dict):
        price = _decimal_field(trade, "p")
        if price is not None and price > 0:
            return price
    quote = entry.get("latestQuote")
    if isinstance(quote, dict):
        bid = _decimal_field(quote, "bp")
        ask = _decimal_field(quote, "ap")
        if bid is not None and ask is not None and ask >= bid > 0:
            return (bid + ask) / 2
    return None


_PROBE_SYNTHETIC_STABLE_SECURITY_ID = "PROBE_SYNTHETIC_IDENTITY_NOT_A_STABLE_SECURITY"


def check_alpaca_options_quote_capability(
    *,
    adapter: AlpacaMarketDataHttpAdapter | None = None,
    probe_symbol: str = "SPY",
    timeout_seconds: int = 10,
    maximum_response_bytes: int = 2_000_000,
) -> OptionsQuoteProviderCapabilitySnapshot:
    """Bounded, GET-only capability probe.

    Reuses ``AlpacaOptionsChainCaptureClient.capture_underlying`` with the
    default ``OptionChainCaptureBounds`` -- the identical, already-proven
    bounded per-underlying capture path -- rather than a separate, narrower
    ad hoc single-page query. Empirically (genuine capability inspection
    against this environment's real Alpaca credentials), an *unfiltered*
    ``option_chain_snapshots`` request for a liquid underlying returns a
    ``next_page_token`` no matter how small ``limit`` is set, because
    pagination continues until the provider's full, un-narrowed chain is
    exhausted rather than until one page is "full"; a naive one-page probe
    therefore always raised ``PROVIDER_PAGE_LIMIT_REACHED`` even though the
    endpoint is genuinely reachable and correctly entitled. Narrowing the
    probe query by expiration window and moneyness band -- the exact bound
    the real capture already applies -- avoids that failure without
    broadening any request limit. This adds no new credential path, no new
    transport, and no new endpoint: it is the same reviewed capture call
    the genuine 19-underlying capture makes for every other underlying.

    Returns evidence, never raises for an ordinary provider-side outcome;
    makes exactly zero requests when credentials are absent, and up to
    ``OptionChainCaptureBounds.create().max_requests_per_underlying``
    (currently 3) when they are present.
    """

    instance = adapter or AlpacaMarketDataHttpAdapter()
    checked_at = datetime.now(tz=UTC)
    if not instance.credentials_available:
        capability = OptionProviderCapabilities.create(
            provider=ALPACA_OPTIONS_QUOTE_PROVIDER,
            contract_discovery=CapabilityState.UNKNOWN,
            quotes=CapabilityState.UNKNOWN,
            trades=CapabilityState.UNKNOWN,
            snapshots=CapabilityState.UNKNOWN,
            implied_volatility=CapabilityState.UNKNOWN,
            greeks=CapabilityState.UNKNOWN,
            open_interest=CapabilityState.UNKNOWN,
            volume=CapabilityState.UNKNOWN,
            limitations=(
                "ALPACA_CREDENTIALS_ABSENT_ZERO_REQUESTS_MADE",
                "CAPABILITY_UNKNOWN_NEVER_INFERRED_FROM_AUTHENTICATION",
                "HISTORICAL_OPTIONS_QUOTE_CAPTURE_NOT_ATTEMPTED_THIS_MILESTONE",
            ),
        )
        return OptionsQuoteProviderCapabilitySnapshot.create(
            checked_at=checked_at,
            credentials_available=False,
            requests_made=0,
            capability=capability,
            unavailable_reason="ALPACA_CREDENTIALS_ABSENT",
        )
    probe_member = ResolvedCohortMember(
        stable_security_id=_PROBE_SYNTHETIC_STABLE_SECURITY_ID,
        symbol=probe_symbol,
        sector=CohortSector.BROAD_MARKET,
        roles=(CohortRole.BROAD_INDEX_ETF,),
        asset_class="ETF",
    )
    outcome = AlpacaOptionsChainCaptureClient(instance).capture_underlying(
        member=probe_member,
        bounds=OptionChainCaptureBounds.create(),
        feed="indicative",
        captured_at=checked_at,
        timeout_seconds=timeout_seconds,
    )
    if outcome.status in ("BLOCKED_PROVIDER_ERROR", "NO_UNDERLYING_PRICE"):
        unavailable = outcome.status == "BLOCKED_PROVIDER_ERROR"
        capability = OptionProviderCapabilities.create(
            provider=ALPACA_OPTIONS_QUOTE_PROVIDER,
            contract_discovery=CapabilityState.UNKNOWN,
            quotes=CapabilityState.UNAVAILABLE if unavailable else CapabilityState.UNKNOWN,
            trades=CapabilityState.UNAVAILABLE if unavailable else CapabilityState.UNKNOWN,
            snapshots=CapabilityState.UNAVAILABLE if unavailable else CapabilityState.UNKNOWN,
            implied_volatility=(
                CapabilityState.UNAVAILABLE if unavailable else CapabilityState.UNKNOWN
            ),
            greeks=CapabilityState.UNAVAILABLE if unavailable else CapabilityState.UNKNOWN,
            open_interest=CapabilityState.UNKNOWN,
            volume=CapabilityState.UNAVAILABLE if unavailable else CapabilityState.UNKNOWN,
            limitations=(f"PROVIDER_ERROR:{outcome.reason}",) if outcome.reason else (),
        )
        return OptionsQuoteProviderCapabilitySnapshot.create(
            checked_at=checked_at,
            credentials_available=True,
            requests_made=outcome.request_count,
            capability=capability,
            unavailable_reason=outcome.reason,
        )
    # Checked across *every* returned entry, not just the first: Alpaca
    # populates IV/Greeks/quote/trade per-contract conditionally (e.g. a
    # contract with no recent quote genuinely has no computed IV even
    # though the *field* exists on the schema), so a single-sample check
    # against an arbitrary first contract is not a representative capability
    # read -- genuine capability inspection against this environment's real
    # credentials found exactly this: entry 0 alone often lacks IV/Greeks
    # while a clear majority of the same response's other entries have it.
    entries = outcome.contract_entries
    total = len(entries)
    quote_count = sum(1 for item in entries if item.bid is not None or item.ask is not None)
    trade_count = sum(1 for item in entries if item.last_trade is not None)
    greeks_count = sum(1 for item in entries if item.delta is not None)
    iv_count = sum(1 for item in entries if item.implied_volatility is not None)
    volume_count = sum(1 for item in entries if item.daily_volume is not None)
    capability = OptionProviderCapabilities.create(
        provider=ALPACA_OPTIONS_QUOTE_PROVIDER,
        contract_discovery=CapabilityState.AVAILABLE,
        quotes=CapabilityState.AVAILABLE if quote_count else CapabilityState.UNAVAILABLE,
        trades=CapabilityState.AVAILABLE if trade_count else CapabilityState.UNAVAILABLE,
        snapshots=CapabilityState.AVAILABLE,
        implied_volatility=CapabilityState.AVAILABLE if iv_count else CapabilityState.UNAVAILABLE,
        greeks=CapabilityState.AVAILABLE if greeks_count else CapabilityState.UNAVAILABLE,
        open_interest=CapabilityState.UNAVAILABLE,
        volume=CapabilityState.AVAILABLE if volume_count else CapabilityState.UNAVAILABLE,
        limitations=(
            "FEED_LEVEL_INDICATIVE_ZERO_COST_DEFAULT_PROBED",
            "OPRA_FEED_ENTITLEMENT_NOT_SEPARATELY_CONFIRMED",
            "OPEN_INTEREST_NOT_PRESENT_ON_SNAPSHOT_ENDPOINT_SCHEMA",
            "RATE_LIMIT_DOCUMENTED_200_RPM_BASIC_10000_RPM_ALGO_TRADER_PLUS",
            f"PROBE_CONTRACTS_RETURNED:{total}",
            f"PROBE_QUOTE_COVERAGE:{quote_count}/{total}",
            f"PROBE_TRADE_COVERAGE:{trade_count}/{total}",
            f"PROBE_GREEKS_COVERAGE:{greeks_count}/{total}",
            f"PROBE_IV_COVERAGE:{iv_count}/{total}",
            f"PROBE_VOLUME_COVERAGE:{volume_count}/{total}",
            "EMPTY_PROBE_RESPONSE" if total == 0 else "PROBE_RESPONSE_NON_EMPTY",
        ),
    )
    return OptionsQuoteProviderCapabilitySnapshot.create(
        checked_at=checked_at,
        credentials_available=True,
        requests_made=outcome.request_count,
        capability=capability,
        unavailable_reason=None,
    )


@dataclass(frozen=True, slots=True)
class UnderlyingCaptureOutcome:
    symbol: str
    stable_security_id: str
    status: str
    underlying_reference_price: Decimal | None
    underlying_price_source: str | None
    underlying_price_timestamp: datetime | None
    contract_entries: tuple[ParsedAlpacaOptionSnapshotEntry, ...]
    request_count: int
    bytes_received: int
    reason: str | None
    source_receipt_ids: tuple[str, ...]
    selection: StratifiedChainSelection | None
    # Distinct contract symbols returned across every paginated response,
    # before OCC-symbol parsing or stratified selection -- reported
    # separately from ``selection.contracts_available`` (post OCC-parse,
    # pre-cap) so a parse failure would be visible rather than silently
    # folded into one count.
    contracts_returned_by_provider: int = 0


class AlpacaOptionsChainCaptureClient:
    """Bounded, GET-only, per-underlying options-chain capture.

    Every call is scoped by an ``OptionChainCaptureBounds`` declared before
    any request is made; nothing here writes to the provider or persists
    anything itself -- it returns evidence for the application layer to
    turn into ``OptionQuoteObservation``/``OptionsChainSnapshot`` rows.
    """

    def __init__(self, adapter: AlpacaMarketDataHttpAdapter | None = None) -> None:
        self._adapter = adapter or AlpacaMarketDataHttpAdapter()

    @property
    def credentials_available(self) -> bool:
        return self._adapter.credentials_available

    def capture_underlying(
        self,
        *,
        member: ResolvedCohortMember,
        bounds: OptionChainCaptureBounds,
        feed: str,
        captured_at: datetime,
        timeout_seconds: int = 15,
    ) -> UnderlyingCaptureOutcome:
        if not self._adapter.credentials_available:
            return UnderlyingCaptureOutcome(
                member.symbol,
                member.stable_security_id,
                "BLOCKED_CREDENTIALS_ABSENT",
                None,
                None,
                None,
                (),
                0,
                0,
                "ALPACA_CREDENTIALS_ABSENT",
                (),
                None,
            )
        deadline = time.monotonic() + bounds.max_runtime_seconds_per_underlying
        request_count = 0
        bytes_received = 0
        try:
            underlying_pages = self._adapter.fetch_pages(
                endpoint_id="latest_snapshots",
                query={"symbols": member.symbol, "feed": "iex"},
                maximum_pages=1,
                maximum_response_bytes=bounds.max_bytes_per_underlying,
                timeout_seconds=timeout_seconds,
            )
        except SecuritiesProviderHttpError as error:
            return UnderlyingCaptureOutcome(
                member.symbol,
                member.stable_security_id,
                "BLOCKED_PROVIDER_ERROR",
                None,
                None,
                None,
                (),
                0,
                0,
                error.code,
                (),
                None,
            )
        request_count += 1
        underlying_page = underlying_pages[0]
        bytes_received += len(underlying_page.body)
        underlying_price = parse_alpaca_underlying_reference_price(
            underlying_page.body, member.symbol
        )
        if underlying_price is None:
            return UnderlyingCaptureOutcome(
                member.symbol,
                member.stable_security_id,
                "NO_UNDERLYING_PRICE",
                None,
                None,
                None,
                (),
                request_count,
                bytes_received,
                "ALPACA_UNDERLYING_PRICE_UNAVAILABLE",
                (underlying_page.raw_sha256,),
                None,
            )
        expiration_start = captured_at.date() + timedelta(days=bounds.dte_minimum)
        expiration_end = captured_at.date() + timedelta(days=bounds.dte_maximum)
        strike_low = underlying_price * (1 - bounds.moneyness_band)
        strike_high = underlying_price * (1 + bounds.moneyness_band)
        entries: dict[str, ParsedAlpacaOptionSnapshotEntry] = {}
        receipts = [underlying_page.raw_sha256]
        query: dict[str, str] = {
            "feed": feed,
            "limit": "1000",
            "expiration_date_gte": expiration_start.isoformat(),
            "expiration_date_lte": expiration_end.isoformat(),
            "strike_price_gte": str(strike_low.quantize(Decimal("0.01"))),
            "strike_price_lte": str(strike_high.quantize(Decimal("0.01"))),
        }
        remaining_requests = bounds.max_requests_per_underlying - request_count
        for _ in range(max(0, remaining_requests)):
            if time.monotonic() > deadline:
                break
            if bytes_received >= bounds.max_bytes_per_underlying:
                break
            current_query = dict(query)
            path_params = {"underlying_symbol": member.symbol}
            try:
                # ``fetch_one_page`` (not ``fetch_pages``) deliberately:
                # a real, liquid underlying's genuine full chain routinely
                # exceeds one page even after the expiration/moneyness
                # narrowing above, so the provider's response commonly
                # still carries a ``next_page_token`` on page one. This
                # loop already manages its own multi-request continuation.
                page = self._adapter.fetch_one_page(
                    endpoint_id="option_chain_snapshots",
                    query=current_query,
                    path_params=path_params,
                    maximum_response_bytes=bounds.max_bytes_per_underlying - bytes_received,
                    timeout_seconds=timeout_seconds,
                )
            except SecuritiesProviderHttpError as error:
                if entries:
                    break
                return UnderlyingCaptureOutcome(
                    member.symbol,
                    member.stable_security_id,
                    "BLOCKED_PROVIDER_ERROR",
                    underlying_price,
                    ALPACA_UNDERLYING_PRICE_SOURCE,
                    underlying_page.received_at,
                    (),
                    request_count,
                    bytes_received,
                    error.code,
                    tuple(receipts),
                    None,
                )
            request_count += 1
            bytes_received += len(page.body)
            receipts.append(page.raw_sha256)
            page_entries, next_token = parse_alpaca_option_chain_snapshot_page(page.body)
            entries.update(page_entries)
            # Deliberately does *not* stop merely because ``len(entries)``
            # has already reached ``max_contracts_per_underlying`` (milestone
            # section 4): stopping there is exactly what let a dense chain's
            # provider-response order silently decide which contracts survive
            # the cap. Continuing to page -- bounded by the same already-
            # declared ``max_requests_per_underlying``/``max_bytes_per_
            # underlying``/``max_runtime_seconds_per_underlying``, never
            # widened -- gathers a broader, more representative candidate
            # pool for ``select_contracts_stratified`` below to choose from.
            # Only a genuinely exhausted provider (``next_token is None``) or
            # an exhausted request/byte/runtime budget ends the loop early.
            if next_token is None:
                break
            query["page_token"] = next_token
        candidates = tuple(
            ChainSampleCandidate(symbol, parsed[1], parsed[2], parsed[3])
            for symbol, parsed in (
                (symbol, parse_occ_contract_symbol(symbol)) for symbol in entries
            )
            if parsed is not None
        )
        selection = select_contracts_stratified(
            candidates,
            underlying_price=underlying_price,
            captured_at=captured_at,
            max_contracts=bounds.max_contracts_per_underlying,
        )
        bounded_entries = tuple(entries[symbol] for symbol in selection.selected_contract_symbols)
        status = "CAPTURED" if bounded_entries else "EMPTY"
        return UnderlyingCaptureOutcome(
            member.symbol,
            member.stable_security_id,
            status,
            underlying_price,
            ALPACA_UNDERLYING_PRICE_SOURCE,
            underlying_page.received_at,
            bounded_entries,
            request_count,
            bytes_received,
            None if bounded_entries else "NO_CONTRACTS_IN_BOUNDED_RANGE",
            tuple(receipts),
            selection,
            len(entries),
        )


def observation_from_alpaca_entry(
    *,
    entry: ParsedAlpacaOptionSnapshotEntry,
    underlying_canonical_security_id: str,
    underlying_reference_price: Decimal,
    captured_at: datetime,
    receipt_id: str,
) -> tuple[OptionContract, OptionQuoteObservation] | None:
    """Bind one parsed live snapshot entry to a fresh contract identity and
    a fully-classified observation. Returns ``None`` -- never raises --
    when the contract symbol itself does not parse, so one malformed row
    never fails an entire chain capture."""

    contract = build_alpaca_option_contract(
        contract_symbol=entry.contract_symbol,
        underlying_canonical_security_id=underlying_canonical_security_id,
        receipt_id=receipt_id,
    )
    if contract is None:
        return None
    quote_timestamp = entry.quote_timestamp or captured_at
    observation = OptionQuoteObservation.create(
        contract=contract,
        expected_underlying_canonical_security_id=underlying_canonical_security_id,
        bid=entry.bid,
        ask=entry.ask,
        bid_size=entry.bid_size,
        ask_size=entry.ask_size,
        last_trade=entry.last_trade,
        trade_size=entry.trade_size,
        underlying_reference_price=underlying_reference_price,
        volume=entry.daily_volume,
        open_interest=None,
        provider_implied_volatility=entry.implied_volatility,
        provider_delta=entry.delta,
        provider_gamma=entry.gamma,
        provider_theta=entry.theta,
        provider_vega=entry.vega,
        provider_rho=entry.rho,
        market_status="UNKNOWN",
        quote_timestamp=quote_timestamp,
        availability_timestamp=captured_at,
        ingestion_timestamp=captured_at,
        captured_at=captured_at,
        provider=ALPACA_OPTIONS_QUOTE_PROVIDER,
        source_receipt_ids=(receipt_id,),
    )
    return contract, observation
