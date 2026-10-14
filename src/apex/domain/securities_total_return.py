"""Deterministic total-return evaluation lineage over retained RAW evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesCorporateActionEvidence,
    SecuritiesCorporateActionType,
)
from apex.domain.values import content_digest

TOTAL_RETURN_POLICY_ID = "APEX_DERIVED_TOTAL_RETURN_V1"


class TotalReturnLineageError(ValueError):
    """A retained action cannot safely produce total-return evaluation evidence."""


@dataclass(frozen=True, slots=True)
class TotalReturnLineage:
    bars: tuple[SecuritiesBarEvidence, ...]
    source_hash: str
    action_hash: str
    result_hash: str
    exclusion_reasons: tuple[str, ...]


def derive_total_return_bars(
    *, bars: Sequence[SecuritiesBarEvidence], actions: Sequence[SecuritiesCorporateActionEvidence]
) -> TotalReturnLineage:
    """Create replayable evaluation bars; actions are outcomes, never features."""
    action_by_key: dict[tuple[str, str], list[SecuritiesCorporateActionEvidence]] = defaultdict(
        list
    )
    for action in actions:
        if action.action_type not in {
            SecuritiesCorporateActionType.SPLIT,
            SecuritiesCorporateActionType.CASH_DIVIDEND,
        }:
            raise TotalReturnLineageError("TOTAL_RETURN_UNSUPPORTED_CORPORATE_ACTION")
        action_by_key[(action.instrument_id, action.effective_date)].append(action)
    grouped: dict[str, list[SecuritiesBarEvidence]] = defaultdict(list)
    for bar in bars:
        if bar.adjustment_state is not SecuritiesAdjustmentState.RAW:
            raise TotalReturnLineageError("TOTAL_RETURN_RAW_SOURCE_REQUIRED")
        grouped[bar.instrument_id].append(bar)
    result: list[SecuritiesBarEvidence] = []
    for instrument_id, series in grouped.items():
        index = Decimal("1")
        previous_close: Decimal | None = None
        for bar in sorted(series, key=lambda item: item.period_end):
            day_actions = action_by_key[(instrument_id, bar.period_end.date().isoformat())]
            split = Decimal("1")
            dividend = Decimal("0")
            for action in day_actions:
                if action.action_type is SecuritiesCorporateActionType.SPLIT:
                    assert action.split_ratio is not None
                    split *= action.split_ratio
                else:
                    assert action.cash_amount is not None
                    dividend += action.cash_amount
            if previous_close is not None:
                if previous_close <= 0:
                    raise TotalReturnLineageError("TOTAL_RETURN_PRIOR_CLOSE_REQUIRED")
                index *= ((bar.close * split) + dividend) / previous_close
            index = _rounded(index)
            scale = index / bar.close
            lineage = content_digest(
                {
                    "policy": TOTAL_RETURN_POLICY_ID,
                    "raw": bar.content_hash,
                    "actions": tuple(item.content_hash for item in day_actions),
                    "index": index,
                }
            )
            result.append(
                SecuritiesBarEvidence(
                    instrument_id=bar.instrument_id,
                    interval=bar.interval,
                    adjustment_state=SecuritiesAdjustmentState.TOTAL_RETURN_ADJUSTED,
                    period_start=bar.period_start,
                    period_end=bar.period_end,
                    available_at=bar.available_at,
                    open=_rounded(bar.open * scale),
                    high=_rounded(bar.high * scale),
                    low=_rounded(bar.low * scale),
                    close=_rounded(bar.close * scale),
                    volume=_rounded(bar.volume / split),
                    source_identity=f"{TOTAL_RETURN_POLICY_ID}:{lineage}",
                )
            )
            previous_close = bar.close
    source_hash = content_digest(tuple(item.content_hash for item in bars))
    action_hash = content_digest(tuple(item.content_hash for item in actions))
    return TotalReturnLineage(
        bars=tuple(sorted(result, key=lambda item: (item.instrument_id, item.period_start))),
        source_hash=source_hash,
        action_hash=action_hash,
        result_hash=content_digest(tuple(item.content_hash for item in result)),
        exclusion_reasons=(),
    )


def _rounded(value: Decimal) -> Decimal:
    """The derived artifact follows Apex's eight-place Decimal evidence policy."""
    return value.quantize(Decimal("0.00000001"))
