"""Official-reference-shaped options parsers and honest capability evidence."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from apex.domain.options_intelligence import (
    CapabilityState,
    ExerciseStyle,
    OptionContract,
    OptionMarketEvidence,
    OptionProviderCapabilities,
    OptionsIntelligenceError,
    OptionType,
    OptionUnderlyingLinkIndex,
    OptionUnderlyingLinkRejection,
    SettlementStyle,
    UnderlyingLinkOutcome,
)
from apex.domain.security_reference import SourceReceipt

NASDAQ_OPTIONS_DIRECTORY_V1 = "NASDAQ_OPTIONS_DIRECTORY_V1"
OCC_OPTIONS_REFERENCE_V1 = "OCC_OPTIONS_REFERENCE_V1"
ALPACA_OPTION_CONTRACT_ADAPTER_V1 = "ALPACA_OPTION_CONTRACT_ADAPTER_V1"
NASDAQ_OPTIONS_HOST = "www.nasdaqtrader.com"
NASDAQ_OPTIONS_DIRECTORY_PATH = "/dynamic/SymDir/options.txt"
# Bounds the Alpaca-sourced options-reference payloads only (contract listing
# and per-contract snapshot): small, authenticated, provider-paginated
# responses unrelated in scale to the public Nasdaq options directory below.
MAX_OPTIONS_REFERENCE_BYTES = 20_000_000
# The official Nasdaq options-directory file is unlike every other bounded
# source in this codebase: it lists one row per *option series* (root,
# expiration, strike, call/put) across the whole US options market, not one
# row per underlying.  A genuine capture on 2026-08-06 observed a payload of
# approximately 88.7 MB -- well past the generic MAX_OPTIONS_REFERENCE_BYTES
# above, and a genuine provider-scale characteristic, not a defect. This
# bound is scoped to that one source only; it is set to roughly 1.35x the
# observed genuine scale (~120 MB) so ordinary day-to-day growth in listed
# series does not immediately re-trip it, while still failing closed against
# an unbounded, malformed, or runaway response.
MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES = 120_000_000
# Verified against the retained genuine capture: every row carries its own
# Expiration Date and Explicit Strike Price -- this source is contract
# (option-series) level, not merely a list of optionable underlyings.
NASDAQ_OPTIONS_DIRECTORY_SCHEMA_LEVEL = "CONTRACT_LEVEL"


@dataclass(frozen=True, slots=True)
class ParsedOptionReference:
    receipt: SourceReceipt
    contracts: tuple[OptionContract, ...]
    total_rows: int = 0
    rejections: tuple[OptionUnderlyingLinkRejection, ...] = ()

    @property
    def linked_underlying_count(self) -> int:
        return len({item.underlying_canonical_security_id for item in self.contracts})

    @property
    def unlinked_count(self) -> int:
        return sum(1 for item in self.rejections if item.outcome is UnderlyingLinkOutcome.UNLINKED)

    @property
    def ambiguous_count(self) -> int:
        return sum(1 for item in self.rejections if item.outcome is UnderlyingLinkOutcome.AMBIGUOUS)

    @property
    def rejection_reason_counts(self) -> tuple[tuple[str, int], ...]:
        counts: dict[str, int] = {}
        for item in self.rejections:
            for reason in item.reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return tuple(sorted(counts.items()))


def parse_nasdaq_options_directory(
    *,
    payload: bytes,
    received_at: datetime,
    underlying_ids: dict[str, str],
) -> ParsedOptionReference:
    """Parse retained machine-readable directory evidence.

    The parser intentionally fails closed if the official file schema changes.
    It never falls back to HTML or an unofficial mirror.  Every row is
    resolved against ``underlying_ids`` (an already-reconciled security
    universe) through ``OptionUnderlyingLinkIndex``: a row whose underlying
    symbol cannot be linked to exactly one canonical identity is never
    silently dropped -- it is retained as an ``OptionUnderlyingLinkRejection``
    with its exact reason, aggregated by distinct raw symbol.
    """
    if not payload or len(payload) > MAX_NASDAQ_OPTIONS_DIRECTORY_BYTES:
        raise OptionsIntelligenceError("NASDAQ_OPTIONS_DIRECTORY_SIZE_INVALID")
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise OptionsIntelligenceError("NASDAQ_OPTIONS_DIRECTORY_NOT_ASCII") from error
    if len(lines) < 3 or not lines[-1].startswith("File Creation Time:"):
        raise OptionsIntelligenceError("NASDAQ_OPTIONS_FILE_TRAILER_REQUIRED")
    try:
        source_timestamp = datetime.strptime(
            lines[-1].split("|", 1)[0].removeprefix("File Creation Time:").strip(),
            "%m%d%Y%H:%M",
        ).replace(tzinfo=UTC)
    except ValueError as error:
        raise OptionsIntelligenceError("NASDAQ_OPTIONS_FILE_TIME_INVALID") from error
    digest = hashlib.sha256(payload).hexdigest()
    receipt = SourceReceipt.create(
        provider="NASDAQ_OPTIONS_DIRECTORY",
        source_timestamp=source_timestamp,
        received_at=received_at,
        payload_sha256=digest,
        byte_count=len(payload),
        source_locator=f"https://{NASDAQ_OPTIONS_HOST}{NASDAQ_OPTIONS_DIRECTORY_PATH}",
        limitations=("CURRENT_REFERENCE_ONLY", "NOT_COMPLETE_OCC_SERIES_COVERAGE"),
    )
    rows = csv.DictReader(io.StringIO("\n".join(lines[:-1])), delimiter="|")
    required = {
        "Root Symbol",
        "Options Closing Type",
        "Options Type",
        "Expiration Date",
        "Explicit Strike Price",
        "Underlying Symbol",
        "Underlying Issue Name",
        "Pending",
    }
    if rows.fieldnames is None or not required.issubset(rows.fieldnames):
        raise OptionsIntelligenceError("NASDAQ_OPTIONS_SCHEMA_UNSUPPORTED")
    link_index = OptionUnderlyingLinkIndex.build(underlying_ids)
    contracts: list[OptionContract] = []
    rejected_rows: dict[str, dict[str, object]] = {}
    total_rows = 0
    for row in rows:
        total_rows += 1
        underlying = row["Underlying Symbol"].strip().upper()
        link = link_index.resolve(underlying)
        if link.outcome is not UnderlyingLinkOutcome.LINKED:
            bucket = rejected_rows.setdefault(
                link.raw_underlying_symbol,
                {
                    "outcome": link.outcome,
                    "row_count": 0,
                    "candidate_security_ids": link.candidate_security_ids,
                    "reasons": link.reasons,
                },
            )
            bucket["row_count"] = int(bucket["row_count"]) + 1  # type: ignore[call-overload]
            continue
        canonical_id = link.canonical_security_id
        assert canonical_id is not None
        option_type = {"C": OptionType.CALL, "P": OptionType.PUT}.get(row["Options Type"])
        closing_type = row["Options Closing Type"]
        if (
            option_type is None
            or closing_type not in {"N", "L"}
            or row["Pending"] not in {"Y", "N"}
        ):
            raise OptionsIntelligenceError("NASDAQ_OPTIONS_ROW_INVALID")
        root = row["Root Symbol"].strip().upper()
        try:
            expiration = datetime.strptime(row["Expiration Date"], "%m/%d/%Y").date().isoformat()
        except ValueError as error:
            raise OptionsIntelligenceError("NASDAQ_OPTIONS_EXPIRATION_INVALID") from error
        provider_contract_id = "|".join(
            (
                root,
                row["Options Type"],
                expiration,
                row["Explicit Strike Price"],
                underlying,
            )
        )
        limitations = [
            "DIRECTORY_FIELDS_ONLY",
            f"CLOSING_TYPE_{closing_type}",
            "ROOT_SYMBOL_IS_NOT_STANDARDIZED_CONTRACT_SYMBOL",
        ]
        if root != underlying:
            limitations.append("OPTIONS_ROOT_DIFFERS_FROM_UNDERLYING_SYMBOL")
        if "NORMALIZED_PUNCTUATION_MATCH" in link.reasons:
            limitations.append("UNDERLYING_LINKED_VIA_NORMALIZED_PUNCTUATION_MATCH")
        contracts.append(
            OptionContract.create(
                provider_contract_id=provider_contract_id,
                underlying_canonical_security_id=canonical_id,
                standardized_symbol=None,
                option_type=option_type,
                strike=row["Explicit Strike Price"],
                expiration=expiration,
                exercise_style=ExerciseStyle.UNKNOWN,
                settlement_style=SettlementStyle.UNKNOWN,
                multiplier="100",
                deliverable=None,
                listing_exchange="NASDAQ_OPTIONS_REFERENCE",
                state="PENDING" if row["Pending"] == "Y" else "CURRENT",
                contract_size_state="STANDARD_ASSUMPTION_UNCONFIRMED",
                adjustment_state="UNKNOWN",
                source_provider="NASDAQ_OPTIONS_DIRECTORY",
                reference_receipt_ids=(receipt.receipt_id,),
                limitations=tuple(limitations),
            )
        )
    rejections = tuple(
        OptionUnderlyingLinkRejection.create(
            raw_underlying_symbol=symbol,
            outcome=bucket["outcome"],  # type: ignore[arg-type]
            row_count=bucket["row_count"],  # type: ignore[arg-type]
            candidate_security_ids=bucket["candidate_security_ids"],  # type: ignore[arg-type]
            reasons=bucket["reasons"],  # type: ignore[arg-type]
            source_payload_hash=digest,
        )
        for symbol, bucket in sorted(rejected_rows.items())
    )
    return ParsedOptionReference(receipt, tuple(contracts), total_rows, rejections)


def occ_public_reference_capability(*, checked_at: datetime) -> OptionProviderCapabilities:
    if checked_at.tzinfo is None:
        raise OptionsIntelligenceError("OCC_CAPABILITY_TIMESTAMP_MUST_BE_AWARE")
    return OptionProviderCapabilities.create(
        provider="OCC",
        contract_discovery=CapabilityState.PAID_REFERENCE_REQUIRED,
        quotes=CapabilityState.UNAVAILABLE,
        trades=CapabilityState.UNAVAILABLE,
        snapshots=CapabilityState.UNAVAILABLE,
        implied_volatility=CapabilityState.UNAVAILABLE,
        greeks=CapabilityState.UNAVAILABLE,
        open_interest=CapabilityState.PAID_REFERENCE_REQUIRED,
        volume=CapabilityState.PAID_REFERENCE_REQUIRED,
        exercise_assignment_observation=CapabilityState.UNAVAILABLE,
        limitations=(
            "NO_DYNAMIC_HTML_SCRAPING",
            "NO_PAID_OCC_DEPENDENCY",
            "COMPLETE_SERIES_OR_OPEN_INTEREST_REQUIRES_LICENSED_REFERENCE",
        ),
    )


def alpaca_options_capability() -> OptionProviderCapabilities:
    return OptionProviderCapabilities.create(
        provider="ALPACA_MARKET_DATA",
        contract_discovery=CapabilityState.AVAILABLE,
        quotes=CapabilityState.AVAILABLE,
        trades=CapabilityState.AVAILABLE,
        snapshots=CapabilityState.AVAILABLE,
        implied_volatility=CapabilityState.AVAILABLE,
        greeks=CapabilityState.AVAILABLE,
        open_interest=CapabilityState.AVAILABLE,
        volume=CapabilityState.AVAILABLE,
        multi_leg_paper=CapabilityState.UNAVAILABLE,
        exercise_assignment_observation=CapabilityState.UNKNOWN,
        limitations=(
            "READ_ONLY_RESEARCH_CAPABILITY",
            "NO_OPTION_ORDER_AUTHORITY",
            "ENTITLEMENT_AND_FEED_COVERAGE_MAY_LIMIT_FIELDS",
        ),
    )


def parse_alpaca_option_contracts(
    *,
    payload: bytes,
    received_at: datetime,
    source_timestamp: datetime,
    underlying_ids: dict[str, str],
) -> ParsedOptionReference:
    if not payload or len(payload) > MAX_OPTIONS_REFERENCE_BYTES:
        raise OptionsIntelligenceError("ALPACA_OPTION_CONTRACT_SIZE_INVALID")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OptionsIntelligenceError("ALPACA_OPTION_CONTRACT_JSON_INVALID") from error
    values = decoded.get("option_contracts") if isinstance(decoded, dict) else None
    if not isinstance(values, list):
        raise OptionsIntelligenceError("ALPACA_OPTION_CONTRACT_ARRAY_REQUIRED")
    digest = hashlib.sha256(payload).hexdigest()
    receipt = SourceReceipt.create(
        provider="ALPACA_OPTIONS",
        source_timestamp=source_timestamp,
        received_at=received_at,
        payload_sha256=digest,
        byte_count=len(payload),
        source_locator="https://paper-api.alpaca.markets/v2/options/contracts",
        limitations=("OPERATIONAL_REFERENCE_NOT_AUTHORITATIVE_OCC_COVERAGE",),
    )
    contracts: list[OptionContract] = []
    for value in values:
        if not isinstance(value, dict):
            raise OptionsIntelligenceError("ALPACA_OPTION_CONTRACT_RECORD_INVALID")
        underlying = value.get("underlying_symbol")
        canonical_id = underlying_ids.get(str(underlying).upper())
        if canonical_id is None:
            continue
        option_type = {"call": OptionType.CALL, "put": OptionType.PUT}.get(str(value.get("type")))
        if option_type is None:
            raise OptionsIntelligenceError("ALPACA_OPTION_TYPE_INVALID")
        contracts.append(
            OptionContract.create(
                provider_contract_id=str(value.get("id", "")),
                underlying_canonical_security_id=canonical_id,
                standardized_symbol=str(value.get("symbol")) if value.get("symbol") else None,
                option_type=option_type,
                strike=str(value.get("strike_price", "")),
                expiration=str(value.get("expiration_date", "")),
                exercise_style=ExerciseStyle.AMERICAN,
                settlement_style=SettlementStyle.PHYSICAL,
                multiplier=str(value.get("size", "100")),
                deliverable="100 SHARES" if str(value.get("size", "100")) == "100" else None,
                listing_exchange=None,
                state=str(value.get("status", "unknown")).upper(),
                contract_size_state=(
                    "STANDARD" if str(value.get("size", "100")) == "100" else "NONSTANDARD"
                ),
                adjustment_state="UNKNOWN",
                source_provider="ALPACA_OPTIONS",
                reference_receipt_ids=(receipt.receipt_id,),
                limitations=("CORPORATE_ACTION_ADJUSTMENT_STATE_NOT_PROVEN",),
            )
        )
    return ParsedOptionReference(receipt, tuple(contracts))


def parse_alpaca_option_snapshot(
    *,
    contract: OptionContract,
    payload: dict[str, object],
    quote_timestamp: datetime,
    staleness_seconds: int,
    source_receipt_id: str,
) -> OptionMarketEvidence:
    quote = payload.get("latestQuote")
    trade = payload.get("latestTrade")
    greeks = payload.get("greeks")
    if not isinstance(quote, dict):
        raise OptionsIntelligenceError("ALPACA_OPTION_QUOTE_REQUIRED")
    trade_value = trade if isinstance(trade, dict) else {}
    greek_values = greeks if isinstance(greeks, dict) else {}
    return OptionMarketEvidence.create(
        contract=contract,
        bid=_string_or_none(quote.get("bp")),
        ask=_string_or_none(quote.get("ap")),
        last_trade=_string_or_none(trade_value.get("p")),
        volume=_string_or_none(payload.get("dailyVolume")),
        open_interest=_string_or_none(payload.get("openInterest")),
        implied_volatility=_string_or_none(payload.get("impliedVolatility")),
        delta=_string_or_none(greek_values.get("delta")),
        gamma=_string_or_none(greek_values.get("gamma")),
        theta=_string_or_none(greek_values.get("theta")),
        vega=_string_or_none(greek_values.get("vega")),
        quote_timestamp=quote_timestamp,
        staleness_seconds=staleness_seconds,
        provider="ALPACA_OPTIONS",
        source_receipt_ids=(source_receipt_id,),
    )


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int)):
        return str(value)
    raise OptionsIntelligenceError("ALPACA_OPTION_NUMERIC_FIELD_INVALID")
