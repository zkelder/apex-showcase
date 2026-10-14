"""Canonicalize retained Alpaca pages into existing Securities evidence.

This adapter accepts only already-retained provider pages and separately
retained calendar/instrument evidence.  It never invents an exchange session,
asset identity, or availability timestamp from a later provider retrieval.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from apex.adapters.alpaca_market_data import ProviderRawPage
from apex.application.securities import SecuritiesCalendarPort
from apex.domain.real_securities_data import (
    ACTION_TIMING_WARNING,
    DERIVED_EOD_AVAILABLE_V1,
    RETROSPECTIVE_CORPORATE_ACTION_V1,
    derived_eod_available,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
    SecuritiesCalendarSession,
    SecuritiesCorporateActionEvidence,
    SecuritiesCorporateActionType,
    SecuritiesInstrument,
)
from apex.domain.values import content_digest


class AlpacaSecuritiesNormalizationError(ValueError):
    """An exact retained provider page cannot be canonicalized safely."""


@dataclass(frozen=True, slots=True)
class NormalizedAlpacaEvidence:
    bars: tuple[SecuritiesBarEvidence, ...]
    corporate_actions: tuple[SecuritiesCorporateActionEvidence, ...]
    raw_page_hashes: tuple[str, ...]
    warnings: tuple[str, ...]


class AlpacaSecuritiesEvidenceNormalizer:
    """One-way conversion from bounded provider pages to domain evidence."""

    def normalize_bars(
        self,
        *,
        pages: Sequence[ProviderRawPage],
        instruments: Sequence[SecuritiesInstrument],
        calendar: SecuritiesCalendarPort,
        calendar_identity: str,
    ) -> NormalizedAlpacaEvidence:
        instrument_by_symbol = _instrument_index(instruments)
        bars: list[SecuritiesBarEvidence] = []
        page_hashes: list[str] = []
        seen: set[tuple[str, str]] = set()
        for page in pages:
            document = _document(page)
            raw_bars = document.get("bars")
            if not isinstance(raw_bars, dict):
                raise AlpacaSecuritiesNormalizationError("ALPACA_BARS_OBJECT_REQUIRED")
            page_hashes.append(page.raw_sha256)
            for symbol, values in raw_bars.items():
                if not isinstance(symbol, str) or not isinstance(values, list):
                    raise AlpacaSecuritiesNormalizationError("ALPACA_BARS_SYMBOL_SHAPE_INVALID")
                instrument = instrument_by_symbol.get(symbol.upper())
                if instrument is None:
                    raise AlpacaSecuritiesNormalizationError(
                        "ALPACA_BAR_SYMBOL_OUTSIDE_FROZEN_UNIVERSE"
                    )
                for raw in values:
                    if not isinstance(raw, dict):
                        raise AlpacaSecuritiesNormalizationError("ALPACA_BAR_RECORD_REQUIRED")
                    session_date = _session_date(raw.get("t"))
                    session = _calendar_session(calendar, calendar_identity, session_date)
                    key = (instrument.instrument_id, session_date)
                    if key in seen:
                        raise AlpacaSecuritiesNormalizationError("ALPACA_DUPLICATE_BAR_PERIOD")
                    seen.add(key)
                    bars.append(
                        SecuritiesBarEvidence(
                            instrument_id=instrument.instrument_id,
                            interval=SecuritiesBarInterval.ONE_DAY,
                            adjustment_state=SecuritiesAdjustmentState.RAW,
                            period_start=session.opens_at,
                            period_end=session.closes_at,
                            available_at=derived_eod_available(session_close=session.closes_at),
                            open=_decimal(raw.get("o"), "open"),
                            high=_decimal(raw.get("h"), "high"),
                            low=_decimal(raw.get("l"), "low"),
                            close=_decimal(raw.get("c"), "close"),
                            volume=_decimal(raw.get("v"), "volume"),
                            source_identity=f"alpaca-raw-page:{page.raw_sha256}",
                        )
                    )
        if not bars:
            raise AlpacaSecuritiesNormalizationError("ALPACA_BARS_EMPTY")
        return NormalizedAlpacaEvidence(
            bars=tuple(sorted(bars, key=lambda item: (item.instrument_id, item.period_start))),
            corporate_actions=(),
            raw_page_hashes=tuple(page_hashes),
            warnings=(DERIVED_EOD_AVAILABLE_V1,),
        )

    def normalize_corporate_actions(
        self,
        *,
        pages: Sequence[ProviderRawPage],
        instruments: Sequence[SecuritiesInstrument],
    ) -> NormalizedAlpacaEvidence:
        instrument_by_symbol = _instrument_index(instruments)
        actions: list[SecuritiesCorporateActionEvidence] = []
        page_hashes: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for page in pages:
            document = _document(page)
            raw_actions = document.get("corporate_actions")
            if isinstance(raw_actions, dict):
                grouped: list[dict[str, object]] = []
                for group, values in raw_actions.items():
                    if not isinstance(group, str) or not isinstance(values, list):
                        raise AlpacaSecuritiesNormalizationError("ALPACA_ACTIONS_ARRAY_REQUIRED")
                    action_type = {
                        "cash_dividends": "cash_dividend",
                        "forward_splits": "split",
                    }.get(group)
                    if action_type is None:
                        raise AlpacaSecuritiesNormalizationError("ALPACA_ACTION_TYPE_UNSUPPORTED")
                    for value in values:
                        if not isinstance(value, dict):
                            raise AlpacaSecuritiesNormalizationError(
                                "ALPACA_ACTION_RECORD_REQUIRED"
                            )
                        grouped.append({**value, "ca_type": action_type})
                raw_actions = grouped
            if not isinstance(raw_actions, list):
                raise AlpacaSecuritiesNormalizationError("ALPACA_ACTIONS_ARRAY_REQUIRED")
            page_hashes.append(page.raw_sha256)
            for raw in raw_actions:
                if not isinstance(raw, dict):
                    raise AlpacaSecuritiesNormalizationError("ALPACA_ACTION_RECORD_REQUIRED")
                symbol = raw.get("symbol")
                if not isinstance(symbol, str) or symbol.upper() not in instrument_by_symbol:
                    raise AlpacaSecuritiesNormalizationError(
                        "ALPACA_ACTION_SYMBOL_OUTSIDE_FROZEN_UNIVERSE"
                    )
                instrument = instrument_by_symbol[symbol.upper()]
                action_type, fields = _action_fields(raw)
                effective_date = _action_effective_date(raw)
                key = (instrument.instrument_id, action_type.value, effective_date)
                if key in seen:
                    raise AlpacaSecuritiesNormalizationError("ALPACA_DUPLICATE_CORPORATE_ACTION")
                seen.add(key)
                actions.append(
                    SecuritiesCorporateActionEvidence(
                        instrument_id=instrument.instrument_id,
                        action_type=action_type,
                        effective_date=effective_date,
                        available_at=page.received_at,
                        source_identity=f"alpaca-raw-page:{page.raw_sha256}",
                        **fields,
                    )
                )
        return NormalizedAlpacaEvidence(
            bars=(),
            corporate_actions=tuple(
                sorted(
                    actions,
                    key=lambda item: (item.instrument_id, item.effective_date, item.action_id),
                )
            ),
            raw_page_hashes=tuple(page_hashes),
            warnings=(ACTION_TIMING_WARNING, RETROSPECTIVE_CORPORATE_ACTION_V1),
        )


def normalized_evidence_hash(value: NormalizedAlpacaEvidence) -> str:
    return content_digest(value)


def _document(page: ProviderRawPage) -> Mapping[str, object]:
    try:
        value = json.loads(page.body.decode("utf-8"), parse_float=str, parse_int=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlpacaSecuritiesNormalizationError("ALPACA_RAW_JSON_INVALID") from error
    if not isinstance(value, dict):
        raise AlpacaSecuritiesNormalizationError("ALPACA_RAW_RESPONSE_OBJECT_REQUIRED")
    return value


def _instrument_index(
    instruments: Sequence[SecuritiesInstrument],
) -> dict[str, SecuritiesInstrument]:
    index = {item.symbol: item for item in instruments}
    if not index or len(index) != len(instruments):
        raise AlpacaSecuritiesNormalizationError("ALPACA_INSTRUMENT_EVIDENCE_INVALID")
    return index


def _session_date(value: object) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise AlpacaSecuritiesNormalizationError("ALPACA_BAR_TIMESTAMP_REQUIRED")
    return value[:10]


def _calendar_session(
    calendar: SecuritiesCalendarPort, calendar_identity: str, session_date: str
) -> SecuritiesCalendarSession:
    sessions = calendar.sessions_between(
        calendar_identity=calendar_identity,
        start_date=session_date,
        end_date=session_date,
    )
    if len(sessions) != 1:
        raise AlpacaSecuritiesNormalizationError("ALPACA_BAR_SESSION_NOT_RETAINED")
    return sessions[0]


def _decimal(value: object, label: str) -> str:
    if isinstance(value, bool) or value is None:
        raise AlpacaSecuritiesNormalizationError(f"ALPACA_BAR_{label.upper()}_REQUIRED")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise AlpacaSecuritiesNormalizationError(f"ALPACA_BAR_{label.upper()}_INVALID") from error
    if not parsed.is_finite():
        raise AlpacaSecuritiesNormalizationError(f"ALPACA_BAR_{label.upper()}_INVALID")
    return format(parsed, "f")


def _action_effective_date(raw: Mapping[str, object]) -> str:
    for name in ("ex_date", "effective_date", "date"):
        value = raw.get(name)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    raise AlpacaSecuritiesNormalizationError("ALPACA_ACTION_EFFECTIVE_DATE_REQUIRED")


def _action_fields(
    raw: Mapping[str, object],
) -> tuple[SecuritiesCorporateActionType, dict[str, str]]:
    raw_type = raw.get("ca_type", raw.get("type"))
    if not isinstance(raw_type, str):
        raise AlpacaSecuritiesNormalizationError("ALPACA_ACTION_TYPE_REQUIRED")
    normalized = raw_type.strip().lower().replace("-", "_")
    if normalized in {"cash_dividend", "dividend"}:
        value = raw.get("cash", raw.get("rate"))
        return SecuritiesCorporateActionType.CASH_DIVIDEND, {"cash_amount": _decimal(value, "cash")}
    if normalized == "split":
        if raw.get("split_ratio") is not None:
            ratio = _decimal(raw.get("split_ratio"), "split_ratio")
        elif raw.get("new_rate") is not None and raw.get("old_rate") is not None:
            old = Decimal(_decimal(raw.get("old_rate"), "old_rate"))
            new = Decimal(_decimal(raw.get("new_rate"), "new_rate"))
            if old == 0:
                raise AlpacaSecuritiesNormalizationError("ALPACA_ACTION_SPLIT_RATIO_INVALID")
            ratio = format(new / old, "f")
        else:
            raise AlpacaSecuritiesNormalizationError("ALPACA_ACTION_SPLIT_RATIO_REQUIRED")
        return SecuritiesCorporateActionType.SPLIT, {"split_ratio": ratio}
    if normalized in {"symbol_change", "symbolchange"}:
        successor = raw.get("new_symbol", raw.get("to_symbol"))
        if not isinstance(successor, str) or not successor.strip():
            raise AlpacaSecuritiesNormalizationError("ALPACA_ACTION_SUCCESSOR_REQUIRED")
        return SecuritiesCorporateActionType.SYMBOL_CHANGE, {"successor_symbol": successor}
    if normalized in {"delisting", "delisted"}:
        return SecuritiesCorporateActionType.DELISTING, {}
    raise AlpacaSecuritiesNormalizationError("ALPACA_ACTION_TYPE_UNSUPPORTED")
