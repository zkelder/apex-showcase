"""Bounded official-current reference parsers for Nasdaq Trader, SEC, and Alpaca.

The network policy accepts no arbitrary URL.  Parsing is independently usable
with retained provider-shaped fixtures, while genuine capture can only target
the allowlisted official hosts and paths declared below.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from apex.domain.security_reference import (
    EvidenceState,
    SecurityReference,
    SecurityReferenceError,
    SecurityType,
    SourceReceipt,
)

NASDAQ_TRADER_SYMBOL_DIRECTORY_V1 = "NASDAQ_TRADER_SYMBOL_DIRECTORY_V1"
SEC_EDGAR_COMPANY_IDENTITY_V1 = "SEC_EDGAR_COMPANY_IDENTITY_V1"
ALPACA_ASSET_CAPABILITY_V1 = "ALPACA_ASSET_CAPABILITY_V1"
NASDAQ_TRADER_HOST = "www.nasdaqtrader.com"
NASDAQ_LISTED_PATH = "/dynamic/SymDir/nasdaqlisted.txt"
NASDAQ_OTHER_LISTED_PATH = "/dynamic/SymDir/otherlisted.txt"
SEC_DATA_HOST = "www.sec.gov"
SEC_TICKERS_PATH = "/files/company_tickers.json"
MAX_REFERENCE_BYTES = 12_000_000
# otherlisted.txt's documented "Exchange" column: the primary-listing venue
# for a security not listed on Nasdaq itself. Nasdaq Trader's own published
# field definitions document exactly these five single-letter codes; there
# is no sixth. An exchange code observed outside this set is retained raw
# rather than guessed at -- see UNRECOGNIZED_EXCHANGE_CODE_PREFIX below.
NASDAQ_OTHER_LISTED_EXCHANGE_CODES: Mapping[str, str] = {
    "N": "XNYS",  # New York Stock Exchange
    "A": "XASE",  # NYSE American (formerly AMEX)
    "P": "ARCX",  # NYSE Arca
    "Z": "BATS",  # Cboe BZX (formerly BATS)
    "V": "IEXG",  # Investors Exchange (IEX)
}
# Prefixed onto the raw code rather than passed through bare, so an
# unrecognized value can never be silently confused with a real MIC-style
# listing_exchange -- it stays visibly distinct while the raw code (after
# the colon) is fully retained.
UNRECOGNIZED_EXCHANGE_CODE_PREFIX = "UNRECOGNIZED_EXCHANGE_CODE:"
SEC_ALLOWED_FACTS = frozenset(
    {
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "NetIncomeLoss",
        "Assets",
        "Liabilities",
        "CashAndCashEquivalentsAtCarryingValue",
        "NetCashProvidedByUsedInOperatingActivities",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "EarningsPerShareDiluted",
    }
)


@dataclass(frozen=True, slots=True)
class ParsedReferenceCapture:
    receipt: SourceReceipt
    records: tuple[SecurityReference, ...]


def parse_nasdaq_symbol_directory(
    *,
    payload: bytes,
    source_path: str,
    received_at: datetime,
) -> ParsedReferenceCapture:
    if source_path not in {NASDAQ_LISTED_PATH, NASDAQ_OTHER_LISTED_PATH}:
        raise SecurityReferenceError("NASDAQ_SOURCE_PATH_NOT_ALLOWLISTED")
    if not payload or len(payload) > MAX_REFERENCE_BYTES:
        raise SecurityReferenceError("NASDAQ_REFERENCE_SIZE_INVALID")
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as error:
        raise SecurityReferenceError("NASDAQ_REFERENCE_NOT_ASCII") from error
    lines = text.splitlines()
    if len(lines) < 3:
        raise SecurityReferenceError("NASDAQ_REFERENCE_TOO_SHORT")
    trailer = lines[-1].split("|")
    if not trailer[0].startswith("File Creation Time:"):
        raise SecurityReferenceError("NASDAQ_FILE_CREATION_TRAILER_MISSING")
    try:
        source_timestamp = datetime.strptime(
            trailer[0].removeprefix("File Creation Time:").strip(), "%m%d%Y%H:%M"
        ).replace(tzinfo=UTC)
    except ValueError as error:
        raise SecurityReferenceError("NASDAQ_FILE_CREATION_TIME_INVALID") from error
    digest = hashlib.sha256(payload).hexdigest()
    receipt = SourceReceipt.create(
        provider="NASDAQ_TRADER",
        source_timestamp=source_timestamp,
        received_at=received_at,
        payload_sha256=digest,
        byte_count=len(payload),
        source_locator=f"https://{NASDAQ_TRADER_HOST}{source_path}",
        limitations=("CURRENT_MARKET_REFERENCE_ONLY", "NOT_HISTORICAL_POINT_IN_TIME"),
    )
    rows = csv.DictReader(io.StringIO("\n".join(lines[:-1])), delimiter="|")
    if rows.fieldnames is None:
        raise SecurityReferenceError("NASDAQ_HEADER_MISSING")
    records: list[SecurityReference] = []
    for ordinal, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise SecurityReferenceError(f"NASDAQ_MALFORMED_ROW_{ordinal}")
        records.append(_nasdaq_record(row, receipt, digest, source_timestamp, source_path))
    if not records:
        raise SecurityReferenceError("NASDAQ_REFERENCE_EMPTY")
    return ParsedReferenceCapture(receipt, tuple(records))


def _nasdaq_record(
    row: Mapping[str, str],
    receipt: SourceReceipt,
    digest: str,
    source_timestamp: datetime,
    source_path: str,
) -> SecurityReference:
    record_limitations: tuple[str, ...] = (
        "TYPE_CLASSIFICATION_FROM_OFFICIAL_DIRECTORY_FIELDS_AND_NAME",
    )
    if source_path == NASDAQ_LISTED_PATH:
        symbol = row.get("Symbol", "")
        name = row.get("Security Name", "")
        category = row.get("Market Category", "")
        exchange = "XNAS"
        test = row.get("Test Issue", "")
        financial = row.get("Financial Status", "")
        lot = row.get("Round Lot Size", "")
        etf = row.get("ETF", "")
    else:
        symbol = row.get("ACT Symbol", "")
        name = row.get("Security Name", "")
        category = row.get("Exchange", "")
        if category in NASDAQ_OTHER_LISTED_EXCHANGE_CODES:
            exchange = NASDAQ_OTHER_LISTED_EXCHANGE_CODES[category]
        else:
            # Retain the raw code (visible after the prefix) rather than
            # guessing at or silently dropping an exchange Nasdaq Trader's
            # documented otherlisted.txt codes don't cover.
            exchange = f"{UNRECOGNIZED_EXCHANGE_CODE_PREFIX}{category}"
            record_limitations = (*record_limitations, "LISTING_EXCHANGE_CODE_NOT_IN_KNOWN_MAPPING")
        test = row.get("Test Issue", "")
        financial = ""
        lot = row.get("Round Lot Size", "")
        etf = row.get("ETF", "")
    if not symbol or not name or test not in {"Y", "N"} or etf not in {"Y", "N"}:
        raise SecurityReferenceError("NASDAQ_REQUIRED_FIELD_INVALID")
    round_lot = int(lot) if lot.isdigit() else None
    security_type, common, adr, reit = _classify_nasdaq_name(name=name, is_etf=etf == "Y")
    status = {
        "D": "DEFICIENT",
        "E": "DELINQUENT",
        "Q": "BANKRUPT",
        "N": "NORMAL",
        "G": "DEFICIENT_BANKRUPT",
        "H": "DEFICIENT_DELINQUENT",
        "J": "DELINQUENT_BANKRUPT",
        "K": "DEFICIENT_DELINQUENT_BANKRUPT",
    }.get(financial, "CURRENT_LISTED")
    return SecurityReference.create(
        current_symbol=symbol,
        provider_symbol=symbol,
        issuer_name=name,
        listing_exchange=exchange,
        listing_category=category,
        security_type=security_type,
        common_stock=common,
        etf=EvidenceState.YES if etf == "Y" else EvidenceState.NO,
        adr=adr,
        reit=reit,
        test_issue=EvidenceState.YES if test == "Y" else EvidenceState.NO,
        listing_status=status,
        round_lot_size=round_lot,
        tradable=EvidenceState.UNKNOWN,
        shortable=EvidenceState.UNKNOWN,
        marginable=EvidenceState.UNKNOWN,
        active=EvidenceState.YES,
        cik=None,
        source_provider="NASDAQ_TRADER",
        source_receipt_id=receipt.receipt_id,
        source_timestamp=source_timestamp,
        source_payload_hash=digest,
        limitations=record_limitations,
    )


def _classify_nasdaq_name(
    *, name: str, is_etf: bool
) -> tuple[SecurityType, EvidenceState, EvidenceState, EvidenceState]:
    upper = name.upper()
    if is_etf:
        return SecurityType.ETF, EvidenceState.NO, EvidenceState.NO, EvidenceState.NO
    if "WARRANT" in upper:
        return SecurityType.WARRANT, EvidenceState.NO, EvidenceState.NO, EvidenceState.NO
    if " PREFERRED " in f" {upper} " or "PFD" in upper:
        return SecurityType.PREFERRED, EvidenceState.NO, EvidenceState.NO, EvidenceState.NO
    if " UNIT" in upper:
        return SecurityType.UNIT, EvidenceState.NO, EvidenceState.NO, EvidenceState.NO
    if " RIGHT" in upper:
        return SecurityType.RIGHT, EvidenceState.NO, EvidenceState.NO, EvidenceState.NO
    # "Depository" is a genuine, common alternate spelling of "Depositary" in
    # official issuer names (SEC/legal usage favors "depositary"; many
    # foreign private issuers' own listed names use "depository" instead) --
    # matching only one spelling left real ADR-equivalent securities UNKNOWN.
    if "ADR" in upper or "DEPOSITARY" in upper or "DEPOSITORY" in upper:
        return SecurityType.ADR, EvidenceState.NO, EvidenceState.YES, EvidenceState.NO
    if " REIT" in upper or "REAL ESTATE INVESTMENT TRUST" in upper:
        return SecurityType.REIT_COMMON, EvidenceState.YES, EvidenceState.NO, EvidenceState.YES
    # "ORDINARY SHARE" (no required trailing "S") still matches the plural
    # form as a substring; it additionally covers the singular form common
    # in SPAC and foreign-private-issuer names ("Class A Ordinary Share"),
    # which the plural-only check previously left classified UNKNOWN.
    if "COMMON STOCK" in upper or "COMMON SHARES" in upper or "ORDINARY SHARE" in upper:
        return SecurityType.COMMON_STOCK, EvidenceState.YES, EvidenceState.NO, EvidenceState.NO
    return SecurityType.UNKNOWN, EvidenceState.UNKNOWN, EvidenceState.UNKNOWN, EvidenceState.UNKNOWN


def parse_sec_company_tickers(
    *, payload: bytes, received_at: datetime, source_timestamp: datetime
) -> ParsedReferenceCapture:
    if not payload or len(payload) > MAX_REFERENCE_BYTES:
        raise SecurityReferenceError("SEC_TICKER_REFERENCE_SIZE_INVALID")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SecurityReferenceError("SEC_TICKER_REFERENCE_JSON_INVALID") from error
    if not isinstance(decoded, dict):
        raise SecurityReferenceError("SEC_TICKER_REFERENCE_OBJECT_REQUIRED")
    digest = hashlib.sha256(payload).hexdigest()
    receipt = SourceReceipt.create(
        provider="SEC_EDGAR",
        source_timestamp=source_timestamp,
        received_at=received_at,
        payload_sha256=digest,
        byte_count=len(payload),
        source_locator=f"https://{SEC_DATA_HOST}{SEC_TICKERS_PATH}",
        limitations=("CURRENT_FILING_IDENTITY_ONLY", "NOT_ALL_SECURITIES_ARE_SEC_REPORTING"),
    )
    records: list[SecurityReference] = []
    for value in decoded.values():
        if not isinstance(value, dict):
            raise SecurityReferenceError("SEC_TICKER_RECORD_INVALID")
        ticker = value.get("ticker")
        title = value.get("title")
        cik = value.get("cik_str")
        if not isinstance(ticker, str) or not isinstance(title, str) or not isinstance(cik, int):
            raise SecurityReferenceError("SEC_TICKER_FIELDS_INVALID")
        records.append(
            SecurityReference.create(
                current_symbol=ticker,
                provider_symbol=ticker,
                issuer_name=title,
                listing_exchange="",
                listing_category="SEC_REPORTING_ISSUER",
                security_type=SecurityType.UNKNOWN,
                common_stock=EvidenceState.UNKNOWN,
                etf=EvidenceState.UNKNOWN,
                adr=EvidenceState.UNKNOWN,
                reit=EvidenceState.UNKNOWN,
                test_issue=EvidenceState.NO,
                listing_status="SEC_REPORTING_CURRENT_IDENTITY",
                round_lot_size=None,
                tradable=EvidenceState.UNKNOWN,
                shortable=EvidenceState.UNKNOWN,
                marginable=EvidenceState.UNKNOWN,
                active=EvidenceState.UNKNOWN,
                cik=str(cik),
                source_provider="SEC_EDGAR",
                source_receipt_id=receipt.receipt_id,
                source_timestamp=source_timestamp,
                source_payload_hash=digest,
                limitations=("SEC_IDENTITY_IS_NOT_LISTING_OR_TRADABILITY_PROOF",),
            )
        )
    return ParsedReferenceCapture(receipt, tuple(records))


def parse_alpaca_assets(
    *, payload: bytes, received_at: datetime, source_timestamp: datetime
) -> ParsedReferenceCapture:
    if not payload or len(payload) > MAX_REFERENCE_BYTES:
        raise SecurityReferenceError("ALPACA_ASSET_REFERENCE_SIZE_INVALID")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SecurityReferenceError("ALPACA_ASSET_REFERENCE_JSON_INVALID") from error
    if not isinstance(decoded, list):
        raise SecurityReferenceError("ALPACA_ASSET_ARRAY_REQUIRED")
    digest = hashlib.sha256(payload).hexdigest()
    receipt = SourceReceipt.create(
        provider="ALPACA",
        source_timestamp=source_timestamp,
        received_at=received_at,
        payload_sha256=digest,
        byte_count=len(payload),
        source_locator="https://paper-api.alpaca.markets/v2/assets",
        limitations=("OPERATIONAL_CAPABILITY_EVIDENCE_NOT_AUTHORITATIVE_LISTING_PROOF",),
    )
    records: list[SecurityReference] = []
    for value in decoded:
        if not isinstance(value, dict):
            raise SecurityReferenceError("ALPACA_ASSET_RECORD_INVALID")
        symbol = value.get("symbol")
        name = value.get("name")
        if not isinstance(symbol, str) or not isinstance(name, str):
            raise SecurityReferenceError("ALPACA_ASSET_FIELDS_INVALID")
        asset_class = value.get("class")
        security_type = (
            SecurityType.COMMON_STOCK if asset_class == "us_equity" else SecurityType.UNKNOWN
        )
        records.append(
            SecurityReference.create(
                current_symbol=symbol,
                provider_symbol=symbol,
                issuer_name=name,
                listing_exchange={
                    "NASDAQ": "XNAS",
                    "NYSE": "XNYS",
                    "AMEX": "XASE",
                    "ARCA": "ARCX",
                    "BATS": "BATS",
                }.get(str(value.get("exchange", "")).upper(), str(value.get("exchange", ""))),
                listing_category=str(asset_class or ""),
                security_type=security_type,
                common_stock=(
                    EvidenceState.YES
                    if security_type is SecurityType.COMMON_STOCK
                    else EvidenceState.UNKNOWN
                ),
                etf=EvidenceState.UNKNOWN,
                adr=EvidenceState.UNKNOWN,
                reit=EvidenceState.UNKNOWN,
                test_issue=EvidenceState.UNKNOWN,
                listing_status=str(value.get("status", "")).upper(),
                round_lot_size=None,
                tradable=_state(value.get("tradable")),
                shortable=_state(value.get("shortable")),
                marginable=_state(value.get("marginable")),
                active=(EvidenceState.YES if value.get("status") == "active" else EvidenceState.NO),
                cik=None,
                source_provider="ALPACA",
                source_receipt_id=receipt.receipt_id,
                source_timestamp=source_timestamp,
                source_payload_hash=digest,
                limitations=("ALPACA_TYPE_IS_CAPABILITY_CLASS_ONLY",),
            )
        )
    return ParsedReferenceCapture(receipt, tuple(records))


def _state(value: object) -> EvidenceState:
    if value is True:
        return EvidenceState.YES
    if value is False:
        return EvidenceState.NO
    return EvidenceState.UNKNOWN


def validate_sec_user_agent(value: str) -> str:
    """Require the SEC-requested application and contact identity."""
    normalized = " ".join(value.split())
    if len(normalized) < 10 or "@" not in normalized or "/" not in normalized:
        raise SecurityReferenceError("SEC_IDENTIFYING_USER_AGENT_REQUIRED")
    return normalized
