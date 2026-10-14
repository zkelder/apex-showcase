"""Bind retained Alpaca pages to existing immutable Securities evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from apex.adapters.alpaca_market_data import ProviderRawPage
from apex.adapters.alpaca_securities_normalizer import AlpacaSecuritiesEvidenceNormalizer
from apex.adapters.securities_data import LocalSecuritiesDatasetAdapter, securities_record
from apex.adapters.xnys_calendar import ReviewedXNYSCalendar
from apex.application.historical_market_data import HistoricalChunkCanonicalizationResult
from apex.application.real_securities_data import RealSecuritiesDataError
from apex.application.securities import (
    ImportSecuritiesData,
    ImportSecuritiesDataCommand,
    SecuritiesRepository,
)
from apex.domain.real_securities_data import (
    SecuritiesProviderFetchOperation,
    SecuritiesProviderFetchOperationState,
)
from apex.domain.securities import (
    SecuritiesAssetClass,
    SecuritiesCorporateActionEvidence,
    SecuritiesInstrument,
    SecuritiesMarketDataBatch,
)
from apex.domain.values import content_digest

_FIXED_ETF_SYMBOLS = frozenset(
    {
        "SPY",
        "QQQ",
        "DIA",
        "IWM",
        "EFA",
        "EEM",
        "TLT",
        "LQD",
        "GLD",
        "USO",
        "XLF",
        "XLK",
        "XLE",
        "XLV",
        "XLI",
        "XLP",
    }
)


@dataclass(frozen=True, slots=True)
class CanonicalizedRetainedAlpacaOperation:
    batch: SecuritiesMarketDataBatch
    normalized_instrument_count: int
    normalized_bar_count: int
    excluded_record_count: int
    exclusion_reasons: tuple[str, ...]
    identity_manifest_hash: str
    calendar_hash: str
    raw_artifact_set_hash: str


class RetainedAlpacaCanonicalizer:
    """One-way production composition over already-completed local evidence."""

    def __init__(
        self,
        *,
        securities_repository: SecuritiesRepository,
        dataset: LocalSecuritiesDatasetAdapter,
        raw_root: Path = Path(".local/securities/providers/alpaca"),
    ) -> None:
        self._securities_repository = securities_repository
        self._dataset = dataset
        self._raw_root = raw_root

    def canonicalize(
        self,
        *,
        historical_operation: SecuritiesProviderFetchOperation,
        identity_operation: SecuritiesProviderFetchOperation | None,
        action_operation: SecuritiesProviderFetchOperation | None,
        idempotency_key: str,
        imported_at: datetime,
    ) -> CanonicalizedRetainedAlpacaOperation:
        _completed(historical_operation, "HISTORICAL_DAILY_BARS")
        if historical_operation.feed != "IEX" or historical_operation.adjustment != "raw":
            raise RealSecuritiesDataError("RETAINED_ALPACA_HISTORICAL_POLICY_MISMATCH")
        if historical_operation.start_at is None or historical_operation.end_at is None:
            raise RealSecuritiesDataError("RETAINED_ALPACA_HISTORICAL_RANGE_REQUIRED")
        if set(historical_operation.symbols) != _FIXED_ETF_SYMBOLS:
            raise RealSecuritiesDataError("RETAINED_ALPACA_FIXED_UNIVERSE_REQUIRED")
        historical_pages = _load_pages(historical_operation, self._raw_root)
        instruments = _instruments(
            symbols=historical_operation.symbols,
            identity_pages=(
                ()
                if identity_operation is None
                else _identity_pages(identity_operation, self._raw_root)
            ),
        )
        calendar = ReviewedXNYSCalendar(
            start_date=historical_operation.start_at.date().isoformat(),
            end_date=historical_operation.end_at.date().isoformat(),
        )
        normalizer = AlpacaSecuritiesEvidenceNormalizer()
        normalized = normalizer.normalize_bars(
            pages=historical_pages,
            instruments=instruments,
            calendar=calendar,
            calendar_identity="XNYS",
        )
        actions: tuple[SecuritiesCorporateActionEvidence, ...] = ()
        if action_operation is not None:
            _completed(action_operation, "CORPORATE_ACTIONS")
            action_normalized = normalizer.normalize_corporate_actions(
                pages=_load_pages(action_operation, self._raw_root), instruments=instruments
            )
            actions = action_normalized.corporate_actions
        records = tuple(
            securities_record(item)
            for item in (*instruments, *calendar.sessions, *normalized.bars, *actions)
        )
        payload_hash = content_digest(
            {
                "historical_operation": historical_operation.canonical_result_hash,
                "historical_raw_set": historical_operation.raw_artifact_set_hash,
                "identity_operation": None
                if identity_operation is None
                else identity_operation.canonical_result_hash,
                "identity_manifest": tuple(item.content_hash for item in instruments),
                "calendar": tuple(item.content_hash for item in calendar.sessions),
                "actions": None
                if action_operation is None
                else action_operation.canonical_result_hash,
            }
        )
        source = f"generated/alpaca-retained-{payload_hash}.jsonl"
        self._dataset.write_generated_jsonl(relative_location=source, records=records)
        batch = ImportSecuritiesData(
            repository=self._securities_repository, market_data=self._dataset
        ).execute(
            ImportSecuritiesDataCommand(
                managed_source=source,
                dataset_name="genuine-alpaca-iex-raw-fixed-universe-v1",
                idempotency_key=idempotency_key,
                imported_at=imported_at,
            )
        )
        return CanonicalizedRetainedAlpacaOperation(
            batch=batch,
            normalized_instrument_count=len(instruments),
            normalized_bar_count=len(normalized.bars),
            excluded_record_count=0,
            exclusion_reasons=(),
            identity_manifest_hash=content_digest(tuple(item.content_hash for item in instruments)),
            calendar_hash=content_digest(tuple(item.content_hash for item in calendar.sessions)),
            raw_artifact_set_hash=historical_operation.raw_artifact_set_hash or "",
        )


def _completed(operation: SecuritiesProviderFetchOperation, expected_type: str) -> None:
    if (
        operation.operation_type.value != expected_type
        or operation.state is not SecuritiesProviderFetchOperationState.COMPLETED
    ):
        raise RealSecuritiesDataError("RETAINED_ALPACA_OPERATION_NOT_COMPLETED_OR_EXPECTED_TYPE")
    if not operation.raw_artifact_relative_path or not operation.raw_artifact_set_hash:
        raise RealSecuritiesDataError("RETAINED_ALPACA_RAW_ARTIFACT_REQUIRED")


def _identity_pages(
    operation: SecuritiesProviderFetchOperation, root: Path
) -> tuple[ProviderRawPage, ...]:
    _completed(operation, "ASSET_SNAPSHOT")
    return _load_pages(operation, root)


def _load_pages(
    operation: SecuritiesProviderFetchOperation, root: Path
) -> tuple[ProviderRawPage, ...]:
    _completed(operation, operation.operation_type.value)
    relative_path = operation.raw_artifact_relative_path
    if relative_path is None:
        raise RealSecuritiesDataError("RETAINED_ALPACA_RAW_ARTIFACT_REQUIRED")
    directory = (Path.cwd() / relative_path).resolve()
    expected_root = root.resolve()
    if not directory.is_relative_to(expected_root) or directory.is_symlink():
        raise RealSecuritiesDataError("RETAINED_ALPACA_ARTIFACT_PATH_INVALID")
    manifest_bytes = (directory / "manifest.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != operation.raw_artifact_set_hash:
        raise RealSecuritiesDataError("RETAINED_ALPACA_ARTIFACT_SET_HASH_MISMATCH")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    pages: list[ProviderRawPage] = []
    for item in manifest.get("pages", []):
        if not isinstance(item, dict):
            raise RealSecuritiesDataError("RETAINED_ALPACA_PAGE_MANIFEST_INVALID")
        raw_name = item.get("raw_relative_path")
        if not isinstance(raw_name, str) or "/" in raw_name:
            raise RealSecuritiesDataError("RETAINED_ALPACA_PAGE_PATH_INVALID")
        body = (directory / raw_name).read_bytes()
        if hashlib.sha256(body).hexdigest() != item.get("raw_sha256"):
            raise RealSecuritiesDataError("RETAINED_ALPACA_RAW_PAGE_HASH_MISMATCH")
        pages.append(
            ProviderRawPage(
                endpoint_id=str(item["endpoint_identity"]),
                query=dict(item["query"]),
                status=int(item["status"]),
                headers=dict(item["response_headers"]),
                body=body,
                received_at=datetime.fromisoformat(str(item["response_timestamp"])),
                page_ordinal=int(item["page_ordinal"]),
                next_page_token_hash=item.get("next_page_token_hash"),
                provider_request_id=item.get("provider_request_id"),
                retry_delays_seconds=tuple(item.get("retry_delays_seconds", ())),
            )
        )
    if len(pages) != operation.page_count or not pages:
        raise RealSecuritiesDataError("RETAINED_ALPACA_PAGINATION_INCOMPLETE")
    if tuple(item.page_ordinal for item in pages) != tuple(range(1, len(pages) + 1)):
        raise RealSecuritiesDataError("RETAINED_ALPACA_PAGINATION_ORDINAL_INVALID")
    if pages[-1].next_page_token_hash is not None:
        raise RealSecuritiesDataError("RETAINED_ALPACA_PAGINATION_INCOMPLETE")
    return tuple(pages)


def _instruments(
    *, symbols: Sequence[str], identity_pages: Sequence[ProviderRawPage]
) -> tuple[SecuritiesInstrument, ...]:
    current: dict[str, dict[str, object]] = {}
    for page in identity_pages:
        document = json.loads(page.body.decode("utf-8"))
        if not isinstance(document, list):
            raise RealSecuritiesDataError("RETAINED_ALPACA_ASSET_RESPONSE_INVALID")
        for row in document:
            if isinstance(row, dict) and isinstance(row.get("symbol"), str):
                current[row["symbol"].upper()] = row
    result: list[SecuritiesInstrument] = []
    for symbol in sorted(symbols):
        row = current.get(symbol)
        provider_id = row.get("id") if row is not None else None
        source = (
            "CURRENT_SNAPSHOT_ONLY"
            if row is not None
            else "OPERATOR_REVIEWED_FIXED_UNIVERSE_IDENTITY_V1"
        )
        if row is not None:
            source += "+OPERATOR_REVIEWED_FIXED_UNIVERSE_IDENTITY_V1"
        result.append(
            SecuritiesInstrument(
                instrument_id=(
                    f"alpaca:{provider_id}"
                    if isinstance(provider_id, str)
                    else f"operator:{symbol}"
                ),
                symbol=symbol,
                asset_class=SecuritiesAssetClass.ETF,
                exchange_mic="XNYS",
                currency="USD",
                timezone="America/New_York",
                valid_from="0001-01-01",
                valid_to=None,
                source_identity=f"{source}+LISTING_VALIDITY_UNKNOWN",
            )
        )
    return tuple(result)


class HistoricalAlpacaCanonicalizer:
    """General, stable-identity-driven sibling of ``RetainedAlpacaCanonicalizer``.

    Unlike that fixed 16-ETF engineering-validation tool, this canonicalizer
    accepts an explicit, caller-supplied instrument list -- already anchored
    on ``stable_security_id`` via ``domain.historical_market_data.
    historical_instrument_id`` -- instead of rebuilding instrument identity
    from Alpaca's own current asset-master snapshot, and is not restricted to
    any fixed symbol set. It reuses the identical raw-page loading, calendar,
    and normalizer machinery as ``RetainedAlpacaCanonicalizer`` and persists
    through the identical ``ImportSecuritiesData`` batch-import path -- no new
    durable bar store, no second import pipeline.

    Corporate actions are deliberately out of scope for this per-chunk path
    in v1 (it fetches only ``HISTORICAL_DAILY_BARS``): fetching them would add
    a second provider operation per chunk. ``SecuritiesCorporateActionEvidence``
    and the ``CORPORATE_ACTIONS`` endpoint remain fully available for a
    follow-up that wires an optional action operation through exactly as
    ``RetainedAlpacaCanonicalizer`` already demonstrates.
    """

    def __init__(
        self,
        *,
        securities_repository: SecuritiesRepository,
        dataset: LocalSecuritiesDatasetAdapter,
        raw_root: Path = Path(".local/securities/providers/alpaca"),
    ) -> None:
        self._securities_repository = securities_repository
        self._dataset = dataset
        self._raw_root = raw_root

    def canonicalize_chunk(
        self,
        *,
        historical_operation: SecuritiesProviderFetchOperation,
        instruments: tuple[SecuritiesInstrument, ...],
        idempotency_key: str,
        imported_at: datetime,
    ) -> HistoricalChunkCanonicalizationResult:
        _completed(historical_operation, "HISTORICAL_DAILY_BARS")
        if historical_operation.feed != "IEX" or historical_operation.adjustment != "raw":
            raise RealSecuritiesDataError("HISTORICAL_ALPACA_POLICY_MISMATCH")
        if historical_operation.start_at is None or historical_operation.end_at is None:
            raise RealSecuritiesDataError("HISTORICAL_ALPACA_RANGE_REQUIRED")
        if not instruments:
            raise RealSecuritiesDataError("HISTORICAL_ALPACA_INSTRUMENTS_REQUIRED")
        if set(historical_operation.symbols) != {item.symbol for item in instruments}:
            raise RealSecuritiesDataError("HISTORICAL_ALPACA_INSTRUMENT_SYMBOL_MISMATCH")
        pages = _load_pages(historical_operation, self._raw_root)
        calendar = ReviewedXNYSCalendar(
            start_date=historical_operation.start_at.date().isoformat(),
            end_date=historical_operation.end_at.date().isoformat(),
        )
        normalizer = AlpacaSecuritiesEvidenceNormalizer()
        normalized = normalizer.normalize_bars(
            pages=pages,
            instruments=instruments,
            calendar=calendar,
            calendar_identity="XNYS",
        )
        counts: dict[str, int] = {item.instrument_id: 0 for item in instruments}
        for bar in normalized.bars:
            counts[bar.instrument_id] = counts.get(bar.instrument_id, 0) + 1
        records = tuple(
            securities_record(item) for item in (*instruments, *calendar.sessions, *normalized.bars)
        )
        payload_hash = content_digest(
            {
                "historical_operation": historical_operation.canonical_result_hash,
                "historical_raw_set": historical_operation.raw_artifact_set_hash,
                "instruments": tuple(item.content_hash for item in instruments),
                "calendar": tuple(item.content_hash for item in calendar.sessions),
            }
        )
        source = f"generated/historical-alpaca-{payload_hash}.jsonl"
        self._dataset.write_generated_jsonl(relative_location=source, records=records)
        batch = ImportSecuritiesData(
            repository=self._securities_repository, market_data=self._dataset
        ).execute(
            ImportSecuritiesDataCommand(
                managed_source=source,
                dataset_name="genuine-historical-alpaca-iex-raw-v1",
                idempotency_key=idempotency_key,
                imported_at=imported_at,
            )
        )
        return HistoricalChunkCanonicalizationResult(
            batch=batch,
            instrument_bar_counts=tuple(sorted(counts.items())),
        )
