"""Pure, provider-neutral securities research evidence and SHADOW rules.

The types in this module describe retained market-data evidence and deterministic
research outputs.  They deliberately have no order, position, reservation,
settlement, ledger, broker, or capital semantics.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise

from apex.domain.values import (
    DecimalInput,
    ValueValidationError,
    canonical_timestamp,
    content_digest,
    deterministic_id,
    parse_decimal,
)

SECURITIES_SHADOW_WARNING = "SECURITIES SHADOW — NO CAPITAL RESERVED"
EXTERNAL_ENGINE_WARNING = "EXTERNAL ENGINE OUTPUT IS EVIDENCE, NOT APEX AUTHORITY"


class SecuritiesError(ValueError):
    """A securities evidence or research invariant was violated."""


class SecuritiesAssetClass(StrEnum):
    COMMON_STOCK = "COMMON_STOCK"
    ETF = "ETF"


class SecuritiesBarInterval(StrEnum):
    ONE_DAY = "ONE_DAY"
    ONE_HOUR = "ONE_HOUR"


class SecuritiesAdjustmentState(StrEnum):
    RAW = "RAW"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    TOTAL_RETURN_ADJUSTED = "TOTAL_RETURN_ADJUSTED"


class SecuritiesCorporateActionType(StrEnum):
    SPLIT = "SPLIT"
    CASH_DIVIDEND = "CASH_DIVIDEND"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    DELISTING = "DELISTING"


class SecuritiesDataBatchState(StrEnum):
    READY = "READY"
    REJECTED = "REJECTED"


class SecuritiesStrategyKind(StrEnum):
    CASH_BASELINE = "CASH_BASELINE"
    BUY_AND_HOLD = "BUY_AND_HOLD"
    TIME_SERIES_MOMENTUM = "TIME_SERIES_MOMENTUM"
    CROSS_SECTIONAL_MOMENTUM = "CROSS_SECTIONAL_MOMENTUM"
    STRATEGY_LAB_MODEL = "STRATEGY_LAB_MODEL"


class SecuritiesSignalDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class SecuritiesResearchRunState(StrEnum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class ExternalSecuritiesEngineState(StrEnum):
    COMPLETED = "COMPLETED"
    LEAN_RUNTIME_UNAVAILABLE = "LEAN_RUNTIME_UNAVAILABLE"
    FAILED = "FAILED"


class SecuritiesConformanceClassification(StrEnum):
    CONFORMANT = "CONFORMANT"
    EXPLAINED_DIFFERENCE = "EXPLAINED_DIFFERENCE"
    MATERIAL_MISMATCH = "MATERIAL_MISMATCH"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    INSUFFICIENT_COMPARABLE_OUTPUT = "INSUFFICIENT_COMPARABLE_OUTPUT"


@dataclass(frozen=True, slots=True, init=False)
class SecuritiesInstrument:
    instrument_id: str
    symbol: str
    asset_class: SecuritiesAssetClass
    exchange_mic: str
    currency: str
    timezone: str
    valid_from: str
    valid_to: str | None
    source_identity: str
    content_hash: str

    def __init__(
        self,
        *,
        instrument_id: str,
        symbol: str,
        asset_class: SecuritiesAssetClass | str,
        exchange_mic: str,
        currency: str,
        timezone: str,
        valid_from: str,
        valid_to: str | None,
        source_identity: str,
        content_hash: str | None = None,
    ) -> None:
        normalized_symbol = symbol.strip().upper()
        if not instrument_id or not normalized_symbol or len(normalized_symbol) > 32:
            raise SecuritiesError("instrument identity and symbol are required")
        if not exchange_mic or len(exchange_mic) > 12:
            raise SecuritiesError("instrument exchange MIC is required")
        if currency != "USD":
            raise SecuritiesError("Stage 1 securities evidence supports USD instruments only")
        if not timezone or not source_identity:
            raise SecuritiesError("instrument timezone and source identity are required")
        _parse_iso_date(valid_from, "instrument valid_from")
        if valid_to is not None and _parse_iso_date(
            valid_to, "instrument valid_to"
        ) < _parse_iso_date(valid_from, "instrument valid_from"):
            raise SecuritiesError("instrument valid_to precedes valid_from")
        payload = {
            "instrument_id": instrument_id,
            "symbol": normalized_symbol,
            "asset_class": SecuritiesAssetClass(asset_class),
            "exchange_mic": exchange_mic,
            "currency": currency,
            "timezone": timezone,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_identity": source_identity,
        }
        expected_hash = content_digest(payload)
        if content_hash is not None and content_hash != expected_hash:
            raise SecuritiesError("instrument content hash does not match canonical evidence")
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "symbol", normalized_symbol)
        object.__setattr__(self, "asset_class", SecuritiesAssetClass(asset_class))
        object.__setattr__(self, "exchange_mic", exchange_mic)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "timezone", timezone)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_to", valid_to)
        object.__setattr__(self, "source_identity", source_identity)
        object.__setattr__(self, "content_hash", expected_hash)


@dataclass(frozen=True, slots=True)
class SecuritiesCalendarSession:
    session_id: str
    calendar_identity: str
    calendar_version: str
    timezone: str
    session_date: str
    opens_at: datetime
    closes_at: datetime
    is_early_close: bool
    source_identity: str
    content_hash: str

    @classmethod
    def create(
        cls,
        *,
        calendar_identity: str,
        calendar_version: str,
        timezone: str,
        session_date: str,
        opens_at: datetime,
        closes_at: datetime,
        is_early_close: bool,
        source_identity: str,
    ) -> SecuritiesCalendarSession:
        _parse_iso_date(session_date, "calendar session date")
        _require_aware(opens_at, "calendar open")
        _require_aware(closes_at, "calendar close")
        if opens_at >= closes_at:
            raise SecuritiesError("calendar session must open before it closes")
        payload = {
            "calendar_identity": calendar_identity,
            "calendar_version": calendar_version,
            "timezone": timezone,
            "session_date": session_date,
            "opens_at": opens_at,
            "closes_at": closes_at,
            "is_early_close": is_early_close,
            "source_identity": source_identity,
        }
        return cls(
            session_id=deterministic_id("securities_session", payload),
            calendar_identity=calendar_identity,
            calendar_version=calendar_version,
            timezone=timezone,
            session_date=session_date,
            opens_at=opens_at,
            closes_at=closes_at,
            is_early_close=is_early_close,
            source_identity=source_identity,
            content_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True, init=False)
class SecuritiesBarEvidence:
    bar_id: str
    instrument_id: str
    interval: SecuritiesBarInterval
    adjustment_state: SecuritiesAdjustmentState
    period_start: datetime
    period_end: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_identity: str
    content_hash: str

    def __init__(
        self,
        *,
        instrument_id: str,
        interval: SecuritiesBarInterval | str,
        adjustment_state: SecuritiesAdjustmentState | str,
        period_start: datetime,
        period_end: datetime,
        available_at: datetime,
        open: DecimalInput,
        high: DecimalInput,
        low: DecimalInput,
        close: DecimalInput,
        volume: DecimalInput,
        source_identity: str,
        bar_id: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        _require_aware(period_start, "bar period_start")
        _require_aware(period_end, "bar period_end")
        _require_aware(available_at, "bar available_at")
        if period_start >= period_end:
            raise SecuritiesError("bar period_start must precede period_end")
        if available_at < period_end:
            raise SecuritiesError("bar cannot be available before its period ends")
        parsed_open = parse_decimal(open, allow_zero=False)
        parsed_high = parse_decimal(high, allow_zero=False)
        parsed_low = parse_decimal(low, allow_zero=False)
        parsed_close = parse_decimal(close, allow_zero=False)
        parsed_volume = parse_decimal(volume)
        if parsed_low > min(parsed_open, parsed_close):
            raise SecuritiesError("bar low exceeds open or close")
        if parsed_high < max(parsed_open, parsed_close):
            raise SecuritiesError("bar high is below open or close")
        if parsed_low > parsed_high:
            raise SecuritiesError("bar low exceeds high")
        if not instrument_id or not source_identity:
            raise SecuritiesError("bar instrument and source identity are required")
        payload = {
            "instrument_id": instrument_id,
            "interval": SecuritiesBarInterval(interval),
            "adjustment_state": SecuritiesAdjustmentState(adjustment_state),
            "period_start": period_start,
            "period_end": period_end,
            "available_at": available_at,
            "open": parsed_open,
            "high": parsed_high,
            "low": parsed_low,
            "close": parsed_close,
            "volume": parsed_volume,
            "source_identity": source_identity,
        }
        expected_id = deterministic_id("securities_bar", payload)
        expected_hash = content_digest(payload)
        if bar_id is not None and bar_id != expected_id:
            raise SecuritiesError("bar identifier does not match canonical evidence")
        if content_hash is not None and content_hash != expected_hash:
            raise SecuritiesError("bar content hash does not match canonical evidence")
        object.__setattr__(self, "bar_id", expected_id)
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "interval", SecuritiesBarInterval(interval))
        object.__setattr__(self, "adjustment_state", SecuritiesAdjustmentState(adjustment_state))
        object.__setattr__(self, "period_start", period_start)
        object.__setattr__(self, "period_end", period_end)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "open", parsed_open)
        object.__setattr__(self, "high", parsed_high)
        object.__setattr__(self, "low", parsed_low)
        object.__setattr__(self, "close", parsed_close)
        object.__setattr__(self, "volume", parsed_volume)
        object.__setattr__(self, "source_identity", source_identity)
        object.__setattr__(self, "content_hash", expected_hash)


@dataclass(frozen=True, slots=True, init=False)
class SecuritiesCorporateActionEvidence:
    action_id: str
    instrument_id: str
    action_type: SecuritiesCorporateActionType
    effective_date: str
    available_at: datetime
    split_ratio: Decimal | None
    cash_amount: Decimal | None
    successor_symbol: str | None
    source_identity: str
    content_hash: str

    def __init__(
        self,
        *,
        instrument_id: str,
        action_type: SecuritiesCorporateActionType | str,
        effective_date: str,
        available_at: datetime,
        split_ratio: DecimalInput | None = None,
        cash_amount: DecimalInput | None = None,
        successor_symbol: str | None = None,
        source_identity: str,
        action_id: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        parsed_type = SecuritiesCorporateActionType(action_type)
        _parse_iso_date(effective_date, "corporate action effective_date")
        _require_aware(available_at, "corporate action available_at")
        parsed_ratio = (
            parse_decimal(split_ratio, allow_zero=False) if split_ratio is not None else None
        )
        parsed_cash = parse_decimal(cash_amount) if cash_amount is not None else None
        normalized_successor = successor_symbol.strip().upper() if successor_symbol else None
        if parsed_type is SecuritiesCorporateActionType.SPLIT:
            if parsed_ratio is None or parsed_cash is not None or normalized_successor is not None:
                raise SecuritiesError("SPLIT requires only a positive split_ratio")
        elif parsed_type is SecuritiesCorporateActionType.CASH_DIVIDEND:
            if parsed_cash is None or parsed_ratio is not None or normalized_successor is not None:
                raise SecuritiesError("CASH_DIVIDEND requires only a cash_amount")
        elif parsed_type is SecuritiesCorporateActionType.SYMBOL_CHANGE:
            if normalized_successor is None or parsed_ratio is not None or parsed_cash is not None:
                raise SecuritiesError("SYMBOL_CHANGE requires only a successor_symbol")
        elif (
            parsed_ratio is not None or parsed_cash is not None or normalized_successor is not None
        ):
            raise SecuritiesError("DELISTING accepts no value fields")
        payload = {
            "instrument_id": instrument_id,
            "action_type": parsed_type,
            "effective_date": effective_date,
            "available_at": available_at,
            "split_ratio": parsed_ratio,
            "cash_amount": parsed_cash,
            "successor_symbol": normalized_successor,
            "source_identity": source_identity,
        }
        expected_id = deterministic_id("securities_action", payload)
        expected_hash = content_digest(payload)
        if action_id is not None and action_id != expected_id:
            raise SecuritiesError("corporate-action identifier does not match canonical evidence")
        if content_hash is not None and content_hash != expected_hash:
            raise SecuritiesError("corporate-action hash does not match canonical evidence")
        object.__setattr__(self, "action_id", expected_id)
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "action_type", parsed_type)
        object.__setattr__(self, "effective_date", effective_date)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "split_ratio", parsed_ratio)
        object.__setattr__(self, "cash_amount", parsed_cash)
        object.__setattr__(self, "successor_symbol", normalized_successor)
        object.__setattr__(self, "source_identity", source_identity)
        object.__setattr__(self, "content_hash", expected_hash)


@dataclass(frozen=True, slots=True)
class SecuritiesMarketDataBatch:
    batch_id: str
    dataset_name: str
    state: SecuritiesDataBatchState
    imported_at: datetime
    source_artifact: str
    source_sha256: str
    source_byte_size: int
    canonical_artifact: str
    canonical_sha256: str
    canonical_byte_size: int
    instrument_count: int
    session_count: int
    bar_count: int
    corporate_action_count: int
    first_period_start: datetime
    last_period_end: datetime
    idempotency_key: str
    warnings: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class SecuritiesStrategySpecification:
    specification_id: str
    strategy_id: str
    kind: SecuritiesStrategyKind
    version: str
    interval: SecuritiesBarInterval
    adjustment_state: SecuritiesAdjustmentState
    lookback_sessions: int
    rebalance_every_sessions: int
    top_quantile: Decimal
    allow_research_short: bool
    estimated_cost_bps: Decimal
    strategy_lab_deployment_id: str | None
    created_at: datetime
    canonical_payload: dict[str, object]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        strategy_id: str,
        kind: SecuritiesStrategyKind | str,
        version: str,
        interval: SecuritiesBarInterval | str = SecuritiesBarInterval.ONE_DAY,
        adjustment_state: SecuritiesAdjustmentState | str = SecuritiesAdjustmentState.RAW,
        lookback_sessions: int = 20,
        rebalance_every_sessions: int = 5,
        top_quantile: DecimalInput = "0.25",
        allow_research_short: bool = False,
        estimated_cost_bps: DecimalInput = "1",
        strategy_lab_deployment_id: str | None = None,
        created_at: datetime,
    ) -> SecuritiesStrategySpecification:
        parsed_kind = SecuritiesStrategyKind(kind)
        parsed_quantile = parse_decimal(top_quantile, allow_zero=False)
        parsed_cost = parse_decimal(estimated_cost_bps)
        _require_aware(created_at, "strategy specification created_at")
        if not strategy_id or not version:
            raise SecuritiesError("strategy identity and version are required")
        if not 1 <= lookback_sessions <= 252:
            raise SecuritiesError("lookback_sessions must be between 1 and 252")
        if not 1 <= rebalance_every_sessions <= 63:
            raise SecuritiesError("rebalance cadence must be between 1 and 63 sessions")
        if parsed_quantile > 1:
            raise SecuritiesError("top_quantile must not exceed one")
        if parsed_cost > 100:
            raise SecuritiesError("estimated research cost exceeds the 100 bps bound")
        if (
            parsed_kind is SecuritiesStrategyKind.STRATEGY_LAB_MODEL
            and strategy_lab_deployment_id is None
        ):
            raise SecuritiesError("Strategy Lab research requires an exact deployment")
        if (
            parsed_kind is not SecuritiesStrategyKind.STRATEGY_LAB_MODEL
            and strategy_lab_deployment_id is not None
        ):
            raise SecuritiesError("baseline specifications cannot name a Strategy Lab deployment")
        payload: dict[str, object] = {
            "strategy_id": strategy_id,
            "kind": parsed_kind,
            "version": version,
            "interval": SecuritiesBarInterval(interval),
            "adjustment_state": SecuritiesAdjustmentState(adjustment_state),
            "lookback_sessions": lookback_sessions,
            "rebalance_every_sessions": rebalance_every_sessions,
            "top_quantile": parsed_quantile,
            "allow_research_short": allow_research_short,
            "estimated_cost_bps": parsed_cost,
            "strategy_lab_deployment_id": strategy_lab_deployment_id,
        }
        canonical_hash = content_digest(payload)
        return cls(
            specification_id=deterministic_id("securities_spec", payload),
            strategy_id=strategy_id,
            kind=parsed_kind,
            version=version,
            interval=SecuritiesBarInterval(interval),
            adjustment_state=SecuritiesAdjustmentState(adjustment_state),
            lookback_sessions=lookback_sessions,
            rebalance_every_sessions=rebalance_every_sessions,
            top_quantile=parsed_quantile,
            allow_research_short=allow_research_short,
            estimated_cost_bps=parsed_cost,
            strategy_lab_deployment_id=strategy_lab_deployment_id,
            created_at=created_at,
            canonical_payload=payload,
            canonical_hash=canonical_hash,
        )


@dataclass(frozen=True, slots=True)
class SecuritiesSignal:
    signal_id: str
    research_run_id: str
    specification_id: str
    instrument_id: str
    signal_at: datetime
    direction: SecuritiesSignalDirection
    target_weight: Decimal
    reference_close: Decimal
    feature_start: datetime
    feature_end: datetime
    source_bar_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    model_deployment_id: str | None
    model_artifact_hash: str | None
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class SecuritiesResearchMetrics:
    session_count: int
    instrument_count: int
    signal_count: int
    long_count: int
    short_count: int
    flat_count: int
    missing_observation_count: int
    rejected_observation_count: int
    coverage_ratio: Decimal
    benchmark_return: Decimal
    hypothetical_gross_return: Decimal
    hypothetical_cost: Decimal
    hypothetical_net_return: Decimal
    maximum_drawdown: Decimal
    turnover: Decimal
    hit_rate: Decimal
    average_gross_exposure: Decimal
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecuritiesResearchRun:
    research_run_id: str
    campaign_id: str
    specification_id: str
    data_batch_id: str
    universe: tuple[str, ...]
    start_date: str
    end_date: str
    decision_cutoff: datetime
    state: SecuritiesResearchRunState
    benchmark_instrument_id: str
    metrics: SecuritiesResearchMetrics
    idempotency_key: str
    started_at: datetime
    completed_at: datetime
    canonical_input_hash: str
    canonical_result_hash: str
    warning_banner: str = SECURITIES_SHADOW_WARNING


@dataclass(frozen=True, slots=True)
class ExternalSecuritiesSignal:
    instrument_id: str
    signal_at: datetime
    direction: SecuritiesSignalDirection
    target_weight: Decimal


@dataclass(frozen=True, slots=True)
class ExternalSecuritiesEngineRequest:
    research_run_id: str
    engine_identity: str
    engine_version: str
    specification_hash: str
    dataset_hash: str
    universe: tuple[str, ...]
    start_date: str
    end_date: str
    benchmark_instrument_id: str
    estimated_cost_bps: Decimal
    corporate_action_hashes: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class ExternalSecuritiesEngineRun:
    external_run_id: str
    research_run_id: str
    engine_identity: str
    engine_version: str
    state: ExternalSecuritiesEngineState
    request_hash: str
    result_hash: str | None
    signals: tuple[ExternalSecuritiesSignal, ...]
    benchmark_return: Decimal | None
    hypothetical_gross_return: Decimal | None
    hypothetical_cost: Decimal | None
    warnings: tuple[str, ...]
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class SecuritiesResearchConformanceReport:
    report_id: str
    research_run_id: str
    external_run_id: str
    classification: SecuritiesConformanceClassification
    compared_signal_count: int
    matching_signal_count: int
    differing_signal_count: int
    missing_signal_count: int
    benchmark_difference: Decimal | None
    gross_return_difference: Decimal | None
    cost_difference: Decimal | None
    reason_codes: tuple[str, ...]
    created_at: datetime
    canonical_input_hash: str
    canonical_result_hash: str
    warning_banner: str = EXTERNAL_ENGINE_WARNING


def validate_dataset_evidence(
    *,
    instruments: Sequence[SecuritiesInstrument],
    sessions: Sequence[SecuritiesCalendarSession],
    bars: Sequence[SecuritiesBarEvidence],
    corporate_actions: Sequence[SecuritiesCorporateActionEvidence],
) -> tuple[str, ...]:
    """Validate one immutable batch without mutating or adjusting raw evidence."""

    if not instruments or not sessions or not bars:
        raise SecuritiesError("a dataset requires instruments, calendar sessions, and bars")
    instrument_by_id = _unique_by(
        instruments, lambda item: item.instrument_id, "duplicate instrument identity"
    )
    _unique_by(sessions, lambda item: item.session_id, "duplicate calendar session")
    _unique_by(bars, lambda item: item.bar_id, "duplicate bar evidence")
    _unique_by(
        corporate_actions,
        lambda item: item.action_id,
        "duplicate corporate-action evidence",
    )
    session_windows = {(item.opens_at, item.closes_at) for item in sessions}
    ordering: dict[
        tuple[str, SecuritiesBarInterval, SecuritiesAdjustmentState],
        list[SecuritiesBarEvidence],
    ] = defaultdict(list)
    for bar in bars:
        if bar.instrument_id not in instrument_by_id:
            raise SecuritiesError("bar references an unknown instrument")
        ordering[(bar.instrument_id, bar.interval, bar.adjustment_state)].append(bar)
        if (
            bar.interval is SecuritiesBarInterval.ONE_DAY
            and (
                bar.period_start,
                bar.period_end,
            )
            not in session_windows
        ):
            raise SecuritiesError("daily bar does not match a retained calendar session")
    for grouped_bars in ordering.values():
        sorted_bars = sorted(grouped_bars, key=lambda item: item.period_start)
        for previous, current in pairwise(sorted_bars):
            if current.period_start < previous.period_end:
                raise SecuritiesError("overlapping bars are not valid retained evidence")
    actions_by_instrument: dict[str, list[SecuritiesCorporateActionEvidence]] = defaultdict(list)
    for action in corporate_actions:
        if action.instrument_id not in instrument_by_id:
            raise SecuritiesError("corporate action references an unknown instrument")
        actions_by_instrument[action.instrument_id].append(action)
    for instrument_id, grouped_actions in actions_by_instrument.items():
        instrument = instrument_by_id[instrument_id]
        sorted_actions = sorted(grouped_actions, key=lambda item: item.effective_date)
        successor_symbols: set[str] = set()
        delisting_seen = False
        for action in sorted_actions:
            if action.effective_date < instrument.valid_from:
                raise SecuritiesError("corporate action predates instrument validity")
            if delisting_seen:
                raise SecuritiesError("corporate action appears after retained delisting")
            if action.action_type is SecuritiesCorporateActionType.SYMBOL_CHANGE:
                assert action.successor_symbol is not None
                if (
                    action.successor_symbol in successor_symbols
                    or action.successor_symbol == instrument.symbol
                ):
                    raise SecuritiesError("symbol lifecycle contains a duplicate symbol")
                successor_symbols.add(action.successor_symbol)
            if action.action_type is SecuritiesCorporateActionType.DELISTING:
                delisting_seen = True
                if instrument.valid_to is not None and action.effective_date != instrument.valid_to:
                    raise SecuritiesError("delisting does not match instrument valid_to")
    adjustment_states = {bar.adjustment_state for bar in bars}
    warnings: list[str] = []
    if SecuritiesAdjustmentState.RAW not in adjustment_states:
        warnings.append("RAW_BARS_ABSENT")
    if len(adjustment_states) == 1:
        warnings.append("SINGLE_ADJUSTMENT_STATE")
    return tuple(warnings)


def build_baseline_signals(
    *,
    research_run_id: str,
    specification: SecuritiesStrategySpecification,
    bars: Sequence[SecuritiesBarEvidence],
    universe: Sequence[str],
    start_date: str,
    end_date: str,
    decision_cutoff: datetime,
) -> tuple[SecuritiesSignal, ...]:
    """Build deterministic research signals from retained point-in-time bars."""

    _parse_iso_date(start_date, "research start")
    _parse_iso_date(end_date, "research end")
    if start_date > end_date:
        raise SecuritiesError("research start date follows end date")
    _require_aware(decision_cutoff, "research decision cutoff")
    if not universe or len(universe) > 500:
        raise SecuritiesError("research universe must contain between 1 and 500 instruments")
    if specification.kind is SecuritiesStrategyKind.STRATEGY_LAB_MODEL:
        raise SecuritiesError("Strategy Lab signals require the model adapter")

    selected = [
        bar
        for bar in bars
        if bar.instrument_id in universe
        and bar.interval is specification.interval
        and bar.adjustment_state is specification.adjustment_state
        and start_date <= bar.period_end.date().isoformat() <= end_date
        and bar.available_at <= decision_cutoff
    ]
    grouped: dict[str, list[SecuritiesBarEvidence]] = defaultdict(list)
    for bar in selected:
        grouped[bar.instrument_id].append(bar)
    for instrument_id in universe:
        grouped[instrument_id].sort(key=lambda item: item.period_end)
    if not any(grouped.values()):
        raise SecuritiesError("no point-in-time bars match the research request")

    if specification.kind is SecuritiesStrategyKind.CROSS_SECTIONAL_MOMENTUM:
        return _cross_sectional_signals(
            research_run_id=research_run_id,
            specification=specification,
            grouped=grouped,
        )
    signals: list[SecuritiesSignal] = []
    for instrument_id in universe:
        instrument_bars = grouped[instrument_id]
        for index, bar in enumerate(instrument_bars):
            direction = SecuritiesSignalDirection.FLAT
            reason_codes: tuple[str, ...]
            source: tuple[SecuritiesBarEvidence, ...] = (bar,)
            if specification.kind is SecuritiesStrategyKind.CASH_BASELINE:
                reason_codes = ("CASH_BASELINE",)
            elif specification.kind is SecuritiesStrategyKind.BUY_AND_HOLD:
                if index == len(instrument_bars) - 1:
                    reason_codes = ("DETERMINISTIC_TERMINAL_FLAT",)
                else:
                    direction = SecuritiesSignalDirection.LONG
                    reason_codes = ("BUY_AND_HOLD_LONG",)
            else:
                if index < specification.lookback_sessions:
                    reason_codes = ("INSUFFICIENT_LOOKBACK",)
                elif index % specification.rebalance_every_sessions != 0:
                    previous_direction = (
                        signals[-1].direction
                        if signals and signals[-1].instrument_id == instrument_id
                        else SecuritiesSignalDirection.FLAT
                    )
                    direction = previous_direction
                    reason_codes = ("RETAIN_PREVIOUS_DIRECTION",)
                    source = tuple(
                        instrument_bars[index - specification.lookback_sessions : index + 1]
                    )
                else:
                    source = tuple(
                        instrument_bars[index - specification.lookback_sessions : index + 1]
                    )
                    momentum = _return(source[0].close, source[-1].close)
                    if momentum > 0:
                        direction = SecuritiesSignalDirection.LONG
                        reason_codes = ("POSITIVE_TRAILING_RETURN",)
                    elif momentum < 0 and specification.allow_research_short:
                        direction = SecuritiesSignalDirection.SHORT
                        reason_codes = ("NEGATIVE_TRAILING_RETURN_RESEARCH_SHORT",)
                    else:
                        reason_codes = ("NON_POSITIVE_TRAILING_RETURN",)
            signals.append(
                _signal(
                    research_run_id=research_run_id,
                    specification=specification,
                    instrument_id=instrument_id,
                    bar=bar,
                    direction=direction,
                    target_weight=_direction_weight(direction),
                    source=source,
                    reason_codes=reason_codes,
                )
            )
    return tuple(signals)


def calculate_research_metrics(
    *,
    signals: Sequence[SecuritiesSignal],
    bars: Sequence[SecuritiesBarEvidence],
    universe: Sequence[str],
    benchmark_instrument_id: str,
    estimated_cost_bps: Decimal,
) -> SecuritiesResearchMetrics:
    """Calculate transparent hypothetical metrics from next retained closes."""

    if benchmark_instrument_id not in universe:
        raise SecuritiesError("benchmark must be a member of the explicit universe")
    close_by_key = {
        (bar.instrument_id, bar.period_end): bar.close
        for bar in bars
        if bar.instrument_id in universe
    }
    bars_by_instrument: dict[str, list[SecuritiesBarEvidence]] = defaultdict(list)
    for bar in bars:
        if bar.instrument_id in universe:
            bars_by_instrument[bar.instrument_id].append(bar)
    next_close: dict[tuple[str, datetime], Decimal] = {}
    for instrument_bars in bars_by_instrument.values():
        ordered = sorted(instrument_bars, key=lambda item: item.period_end)
        for current, following in pairwise(ordered):
            next_close[(current.instrument_id, current.period_end)] = following.close

    returns: list[Decimal] = []
    turnover = Decimal(0)
    exposure = Decimal(0)
    hits = 0
    comparable = 0
    previous_weights: dict[str, Decimal] = defaultdict(Decimal)
    missing = 0
    equity = Decimal(1)
    peak = Decimal(1)
    maximum_drawdown = Decimal(0)
    session_returns: dict[datetime, list[Decimal]] = defaultdict(list)
    for signal in sorted(signals, key=lambda item: (item.signal_at, item.instrument_id)):
        weight = signal.target_weight
        turnover += abs(weight - previous_weights[signal.instrument_id])
        previous_weights[signal.instrument_id] = weight
        exposure += abs(weight)
        following_close = next_close.get((signal.instrument_id, signal.signal_at))
        if following_close is None:
            missing += 1
            continue
        reference = close_by_key[(signal.instrument_id, signal.signal_at)]
        underlying_return = _return(reference, following_close)
        strategy_return = weight * underlying_return
        returns.append(strategy_return)
        session_returns[signal.signal_at].append(strategy_return)
        if signal.direction is not SecuritiesSignalDirection.FLAT:
            comparable += 1
            if strategy_return > 0:
                hits += 1
    for values in session_returns.values():
        session_return = sum(values, Decimal(0)) / Decimal(max(1, len(universe)))
        equity *= Decimal(1) + session_return
        peak = max(peak, equity)
        if peak > 0:
            maximum_drawdown = min(maximum_drawdown, equity / peak - Decimal(1))

    benchmark_bars = sorted(
        bars_by_instrument.get(benchmark_instrument_id, ()), key=lambda item: item.period_end
    )
    benchmark_return = (
        _return(benchmark_bars[0].close, benchmark_bars[-1].close)
        if len(benchmark_bars) >= 2
        else Decimal(0)
    )
    gross_return = sum(returns, Decimal(0)) / Decimal(max(1, len(universe)))
    cost = turnover * estimated_cost_bps / Decimal(10000)
    coverage_denominator = max(1, len(signals))
    warnings = [SECURITIES_SHADOW_WARNING]
    if missing:
        warnings.append("TERMINAL_OBSERVATIONS_HAVE_NO_FORWARD_RETURN")
    return SecuritiesResearchMetrics(
        session_count=len({signal.signal_at for signal in signals}),
        instrument_count=len(set(universe)),
        signal_count=len(signals),
        long_count=sum(signal.direction is SecuritiesSignalDirection.LONG for signal in signals),
        short_count=sum(signal.direction is SecuritiesSignalDirection.SHORT for signal in signals),
        flat_count=sum(signal.direction is SecuritiesSignalDirection.FLAT for signal in signals),
        missing_observation_count=missing,
        rejected_observation_count=0,
        coverage_ratio=_bounded_decimal(
            Decimal(len(signals) - missing) / Decimal(coverage_denominator)
        ),
        benchmark_return=_bounded_decimal(benchmark_return),
        hypothetical_gross_return=_bounded_decimal(gross_return),
        hypothetical_cost=_bounded_decimal(cost),
        hypothetical_net_return=_bounded_decimal(gross_return - cost),
        maximum_drawdown=_bounded_decimal(maximum_drawdown),
        turnover=_bounded_decimal(turnover),
        hit_rate=_bounded_decimal(
            Decimal(hits) / Decimal(comparable) if comparable else Decimal(0)
        ),
        average_gross_exposure=_bounded_decimal(exposure / Decimal(coverage_denominator)),
        warnings=tuple(warnings),
    )


def compare_external_evidence(
    *,
    research_run: SecuritiesResearchRun,
    apex_signals: Sequence[SecuritiesSignal],
    external_run: ExternalSecuritiesEngineRun,
    created_at: datetime,
) -> SecuritiesResearchConformanceReport:
    """Compare external research evidence while preserving Apex authority."""

    _require_aware(created_at, "conformance created_at")
    input_payload = {
        "research_run_id": research_run.research_run_id,
        "external_run_id": external_run.external_run_id,
        "apex_result_hash": research_run.canonical_result_hash,
        "external_result_hash": external_run.result_hash,
    }
    canonical_input_hash = content_digest(input_payload)
    if external_run.state is ExternalSecuritiesEngineState.LEAN_RUNTIME_UNAVAILABLE:
        classification = SecuritiesConformanceClassification.ENGINE_UNAVAILABLE
        matching = differing = missing = 0
        reasons = ("LEAN_RUNTIME_UNAVAILABLE",)
    elif not external_run.signals:
        classification = SecuritiesConformanceClassification.INSUFFICIENT_COMPARABLE_OUTPUT
        matching = differing = 0
        missing = len(apex_signals)
        reasons = ("EXTERNAL_SIGNALS_ABSENT",)
    else:
        apex_by_key = {
            (signal.instrument_id, signal.signal_at): (
                signal.direction,
                signal.target_weight,
            )
            for signal in apex_signals
        }
        external_by_key = {
            (signal.instrument_id, signal.signal_at): (
                signal.direction,
                signal.target_weight,
            )
            for signal in external_run.signals
        }
        keys = set(apex_by_key) | set(external_by_key)
        matching = sum(
            key in apex_by_key
            and key in external_by_key
            and apex_by_key[key] == external_by_key[key]
            for key in keys
        )
        missing = sum(key not in apex_by_key or key not in external_by_key for key in keys)
        differing = len(keys) - matching - missing
        if not missing and not differing:
            classification = SecuritiesConformanceClassification.CONFORMANT
            reasons = ("EXACT_SIGNAL_LINEAGE_MATCH",)
        elif not missing and differing <= max(1, len(keys) // 20):
            classification = SecuritiesConformanceClassification.EXPLAINED_DIFFERENCE
            reasons = ("BOUNDED_SIGNAL_DIFFERENCE",)
        else:
            classification = SecuritiesConformanceClassification.MATERIAL_MISMATCH
            reasons = ("SIGNAL_LINEAGE_MISMATCH",)
    benchmark_difference = _optional_difference(
        research_run.metrics.benchmark_return, external_run.benchmark_return
    )
    gross_difference = _optional_difference(
        research_run.metrics.hypothetical_gross_return,
        external_run.hypothetical_gross_return,
    )
    cost_difference = _optional_difference(
        research_run.metrics.hypothetical_cost,
        external_run.hypothetical_cost,
    )
    result_payload = {
        "classification": classification,
        "matching": matching,
        "differing": differing,
        "missing": missing,
        "benchmark_difference": benchmark_difference,
        "gross_return_difference": gross_difference,
        "cost_difference": cost_difference,
        "reason_codes": reasons,
    }
    return SecuritiesResearchConformanceReport(
        report_id=deterministic_id(
            "securities_conformance",
            {
                "canonical_input_hash": canonical_input_hash,
                "canonical_result_hash": content_digest(result_payload),
            },
        ),
        research_run_id=research_run.research_run_id,
        external_run_id=external_run.external_run_id,
        classification=classification,
        compared_signal_count=matching + differing,
        matching_signal_count=matching,
        differing_signal_count=differing,
        missing_signal_count=missing,
        benchmark_difference=benchmark_difference,
        gross_return_difference=gross_difference,
        cost_difference=cost_difference,
        reason_codes=reasons,
        created_at=created_at,
        canonical_input_hash=canonical_input_hash,
        canonical_result_hash=content_digest(result_payload),
    )


def _cross_sectional_signals(
    *,
    research_run_id: str,
    specification: SecuritiesStrategySpecification,
    grouped: Mapping[str, Sequence[SecuritiesBarEvidence]],
) -> tuple[SecuritiesSignal, ...]:
    by_date: dict[datetime, dict[str, tuple[Decimal, tuple[SecuritiesBarEvidence, ...]]]] = (
        defaultdict(dict)
    )
    for instrument_id, bars in grouped.items():
        for index, bar in enumerate(bars):
            if index < specification.lookback_sessions:
                continue
            source = tuple(bars[index - specification.lookback_sessions : index + 1])
            by_date[bar.period_end][instrument_id] = (
                _return(source[0].close, source[-1].close),
                source,
            )
    signals: list[SecuritiesSignal] = []
    last_weights: dict[str, Decimal] = defaultdict(Decimal)
    for session_index, signal_at in enumerate(sorted(by_date)):
        candidates = by_date[signal_at]
        if session_index % specification.rebalance_every_sessions:
            selected = {
                instrument_id for instrument_id, weight in last_weights.items() if weight > 0
            }
        else:
            eligible = sorted(
                (
                    (momentum, instrument_id)
                    for instrument_id, (momentum, _source) in candidates.items()
                    if momentum > 0
                ),
                key=lambda item: (-item[0], item[1]),
            )
            with localcontext() as context:
                context.prec = 28
                count = max(
                    1,
                    int(
                        (Decimal(len(candidates)) * specification.top_quantile).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    ),
                )
            selected = {instrument_id for _momentum, instrument_id in eligible[:count]}
        target_weight = Decimal(1) / Decimal(len(selected)) if selected else Decimal(0)
        for instrument_id in sorted(candidates):
            _momentum, source = candidates[instrument_id]
            bar = source[-1]
            direction = (
                SecuritiesSignalDirection.LONG
                if instrument_id in selected
                else SecuritiesSignalDirection.FLAT
            )
            weight = target_weight if direction is SecuritiesSignalDirection.LONG else Decimal(0)
            last_weights[instrument_id] = weight
            signals.append(
                _signal(
                    research_run_id=research_run_id,
                    specification=specification,
                    instrument_id=instrument_id,
                    bar=bar,
                    direction=direction,
                    target_weight=weight,
                    source=source,
                    reason_codes=(
                        "TOP_POSITIVE_MOMENTUM_QUANTILE"
                        if direction is SecuritiesSignalDirection.LONG
                        else "OUTSIDE_TOP_POSITIVE_MOMENTUM_QUANTILE",
                    ),
                )
            )
    return tuple(signals)


def _signal(
    *,
    research_run_id: str,
    specification: SecuritiesStrategySpecification,
    instrument_id: str,
    bar: SecuritiesBarEvidence,
    direction: SecuritiesSignalDirection,
    target_weight: Decimal,
    source: Sequence[SecuritiesBarEvidence],
    reason_codes: tuple[str, ...],
) -> SecuritiesSignal:
    parsed_weight = _bounded_decimal(target_weight)
    payload = {
        "research_run_id": research_run_id,
        "specification_id": specification.specification_id,
        "instrument_id": instrument_id,
        "signal_at": bar.period_end,
        "direction": direction,
        "target_weight": parsed_weight,
        "reference_close": bar.close,
        "feature_start": source[0].period_start,
        "feature_end": source[-1].period_end,
        "source_bar_ids": tuple(item.bar_id for item in source),
        "reason_codes": reason_codes,
    }
    return SecuritiesSignal(
        signal_id=deterministic_id("securities_signal", payload),
        research_run_id=research_run_id,
        specification_id=specification.specification_id,
        instrument_id=instrument_id,
        signal_at=bar.period_end,
        direction=direction,
        target_weight=parsed_weight,
        reference_close=bar.close,
        feature_start=source[0].period_start,
        feature_end=source[-1].period_end,
        source_bar_ids=tuple(item.bar_id for item in source),
        reason_codes=reason_codes,
        model_deployment_id=None,
        model_artifact_hash=None,
        canonical_hash=content_digest(payload),
    )


def _direction_weight(direction: SecuritiesSignalDirection) -> Decimal:
    if direction is SecuritiesSignalDirection.LONG:
        return Decimal(1)
    if direction is SecuritiesSignalDirection.SHORT:
        return Decimal(-1)
    return Decimal(0)


def _return(first: Decimal, last: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return (last - first) / first


def _bounded_decimal(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return parse_decimal(
            value.quantize(Decimal("0.00000001")),
            allow_negative=True,
        )


def _optional_difference(first: Decimal, second: Decimal | None) -> Decimal | None:
    return None if second is None else _bounded_decimal(first - second)


def _parse_iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise SecuritiesError(f"{label} must be an ISO date") from error


def _require_aware(value: datetime, label: str) -> None:
    try:
        canonical_timestamp(value)
    except ValueValidationError as error:
        raise SecuritiesError(f"{label} must be timezone-aware") from error


def _unique_by[T](
    values: Iterable[T],
    key: Callable[[T], str],
    message: str,
) -> dict[str, T]:
    result: dict[str, T] = {}
    for value in values:
        identity = key(value)
        if identity in result:
            raise SecuritiesError(message)
        result[identity] = value
    return result
