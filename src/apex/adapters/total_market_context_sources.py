"""Retained-only Total Market context source adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.postgres import (
    securities_bar_evidence,
    securities_instrument,
    securities_market_data_batch,
)
from apex.domain.total_market_context import (
    COMPONENT_FRESHNESS_LIMIT_SECONDS,
    ContextComponent,
    ContextComponentType,
    ContextSourceBundle,
    TotalMarketContextError,
    UnavailableContextComponent,
)

GENUINE_TOTAL_MARKET_DATASET = "genuine-alpaca-iex-raw-fixed-universe-v1"


def _retained_decimal(value: object) -> Decimal:
    """Remove storage-only scale without changing the retained numeric value."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise TotalMarketContextError("TOTAL_MARKET_SOURCE_DECIMAL_INVALID")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return Decimal(rendered)


def _age_seconds(as_of: datetime, observed_at: datetime) -> int:
    if as_of.tzinfo is None or observed_at.tzinfo is None:
        raise TotalMarketContextError("TOTAL_MARKET_SOURCE_TIME_INVALID")
    seconds = int((as_of.astimezone(UTC) - observed_at.astimezone(UTC)).total_seconds())
    if seconds < 0:
        raise TotalMarketContextError("TOTAL_MARKET_SOURCE_AFTER_CONTEXT_CUTOFF")
    return seconds


def _annualized_volatility(closes: tuple[Decimal, ...]) -> Decimal:
    if len(closes) < 21:
        raise TotalMarketContextError("TOTAL_MARKET_VOLATILITY_HISTORY_INSUFFICIENT")
    returns = tuple(closes[index] / closes[index - 1] - 1 for index in range(1, len(closes)))
    average = sum(returns, Decimal()) / Decimal(len(returns))
    variance = sum(((value - average) ** 2 for value in returns), Decimal()) / Decimal(len(returns))
    with localcontext() as context:
        context.prec = 28
        return (variance.sqrt() * Decimal(252).sqrt()).quantize(Decimal("0.00000001"))


class RetainedTotalMarketContextSource:
    """Build components from one exact immutable Securities data batch."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, *, source_batch_id: str, as_of: datetime) -> ContextSourceBundle:
        if as_of.tzinfo is None:
            raise TotalMarketContextError("TOTAL_MARKET_SOURCE_TIME_INVALID")
        cutoff = as_of.astimezone(UTC)
        with self._engine.connect() as connection:
            batch = (
                connection.execute(
                    sa.select(securities_market_data_batch).where(
                        securities_market_data_batch.c.batch_id == source_batch_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if batch is None:
                raise TotalMarketContextError("TOTAL_MARKET_SOURCE_BATCH_NOT_FOUND")
            if (
                str(batch["dataset_name"]) != GENUINE_TOTAL_MARKET_DATASET
                or str(batch["state"]) != "READY"
            ):
                raise TotalMarketContextError("TOTAL_MARKET_SOURCE_BATCH_NOT_GENUINE_READY")
            if cast(datetime, batch["imported_at"]).astimezone(UTC) > cutoff:
                raise TotalMarketContextError("TOTAL_MARKET_SOURCE_BATCH_AFTER_CONTEXT_CUTOFF")
            rows = tuple(
                connection.execute(
                    sa.select(
                        securities_bar_evidence,
                        securities_instrument.c.symbol,
                    )
                    .join(
                        securities_instrument,
                        securities_instrument.c.instrument_id
                        == securities_bar_evidence.c.instrument_id,
                    )
                    .where(securities_bar_evidence.c.batch_id == source_batch_id)
                    .where(securities_instrument.c.symbol.in_(("SPY", "TLT", "USO")))
                    .where(securities_bar_evidence.c.interval == "ONE_DAY")
                    .where(securities_bar_evidence.c.adjustment_state == "RAW")
                    .where(securities_bar_evidence.c.available_at <= cutoff)
                    .order_by(
                        securities_instrument.c.symbol,
                        securities_bar_evidence.c.period_start,
                    )
                ).mappings()
            )
        by_symbol: dict[str, list[RowMapping]] = {}
        for row in rows:
            by_symbol.setdefault(str(row["symbol"]), []).append(row)
        components: list[ContextComponent] = []
        for symbol, component_type, interpretation, limitations in (
            (
                "SPY",
                ContextComponentType.US_EQUITY_MARKET,
                "SPY_20_SESSION_TOTAL_MARKET_PRICE_DIRECTION",
                (
                    "SPY_IS_AN_ETF_PRICE_OBSERVATION_NOT_THE_FULL_US_MARKET",
                    "PRICE_DIRECTION_DOES_NOT_PREDICT_OPPORTUNITY_RETURNS",
                ),
            ),
            (
                "TLT",
                ContextComponentType.TREASURY_RATE,
                "TLT_20_SESSION_LONG_DURATION_TREASURY_PRICE_PROXY",
                (
                    "TLT_PRICE_DIRECTION_IS_NOT_A_TREASURY_YIELD",
                    "DURATION_AND_FUND_EFFECTS_LIMIT_RATE_INTERPRETATION",
                ),
            ),
            (
                "USO",
                ContextComponentType.COMMODITY_OR_INFLATION,
                "USO_20_SESSION_OIL_PRICE_PROXY",
                (
                    "USO_IS_A_FUND_PRICE_PROXY_NOT_SPOT_OIL_OR_INFLATION",
                    "FUTURES_ROLL_EFFECTS_MAY_AFFECT_THE FUND_PRICE",
                ),
            ),
        ):
            symbol_rows = by_symbol.get(symbol, [])
            if len(symbol_rows) < 21:
                continue
            selected = symbol_rows[-21:]
            components.append(
                self._price_component(
                    batch=batch,
                    rows=selected,
                    symbol=symbol,
                    component_type=component_type,
                    interpretation=interpretation,
                    limitations=limitations,
                    as_of=cutoff,
                )
            )
        spy_rows = by_symbol.get("SPY", [])
        if len(spy_rows) >= 42:
            current_rows = spy_rows[-21:]
            prior_rows = spy_rows[-42:-21]
            latest = current_rows[-1]
            current_volatility = _annualized_volatility(
                tuple(_retained_decimal(item["close"]) for item in current_rows)
            )
            prior_volatility = _annualized_volatility(
                tuple(_retained_decimal(item["close"]) for item in prior_rows)
            )
            components.append(
                ContextComponent.create(
                    component_id=(
                        f"total-market-{source_batch_id}-{cutoff.date().isoformat()}-"
                        "spy-realized-volatility-v1"
                    ),
                    component_type=ContextComponentType.MARKET_VOLATILITY,
                    source_observation_id=str(latest["bar_id"]),
                    provider="ALPACA_IEX_RETAINED",
                    subject="SPY",
                    observation_at=cast(datetime, latest["period_end"]),
                    available_at=cast(datetime, latest["available_at"]),
                    ingested_at=cast(datetime, batch["imported_at"]),
                    current_value=current_volatility,
                    prior_comparable_value=prior_volatility,
                    freshness_seconds=_age_seconds(cutoff, cast(datetime, latest["available_at"])),
                    freshness_limit_seconds=COMPONENT_FRESHNESS_LIMIT_SECONDS,
                    quality_status=self._quality_status(batch, current_rows),
                    evidence_class="GENUINE_PUBLIC",
                    limitations=(
                        "REALIZED_VOLATILITY_IS_DERIVED_FROM_RETAINED_SPY_DAILY_CLOSES",
                        "REALIZED_VOLATILITY_IS_NOT_VIX_OR_FORWARD_IMPLIED_VOLATILITY",
                    ),
                    interpretation_category="SPY_20_SESSION_ANNUALIZED_REALIZED_VOLATILITY",
                    supporting_evidence_ids=self._lineage(
                        source_batch_id,
                        str(batch["canonical_hash"]),
                        current_rows,
                    ),
                )
            )
        available_types = {item.component_type for item in components}
        unavailable_reasons = {
            ContextComponentType.US_EQUITY_MARKET: "EXACT_SPY_21_SESSION_HISTORY_UNAVAILABLE",
            ContextComponentType.TREASURY_RATE: "EXACT_TLT_21_SESSION_HISTORY_UNAVAILABLE",
            ContextComponentType.YIELD_CURVE: "NO_RETAINED_MULTI_MATURITY_YIELD_OBSERVATIONS",
            ContextComponentType.MARKET_VOLATILITY: "EXACT_SPY_42_SESSION_HISTORY_UNAVAILABLE",
            ContextComponentType.US_DOLLAR: "NO_RETAINED_US_DOLLAR_INDEX_OBSERVATION",
            ContextComponentType.COMMODITY_OR_INFLATION: "EXACT_USO_21_SESSION_HISTORY_UNAVAILABLE",
            ContextComponentType.MACRO_EVENT_PROXIMITY: "NO_RETAINED_MACRO_RELEASE_CALENDAR",
        }
        unavailable = tuple(
            UnavailableContextComponent(component_type, unavailable_reasons[component_type])
            for component_type in ContextComponentType
            if component_type not in available_types
        )
        return ContextSourceBundle(tuple(components), unavailable)

    @staticmethod
    def _lineage(
        source_batch_id: str,
        source_batch_hash: str,
        rows: list[RowMapping],
    ) -> tuple[str, ...]:
        return (
            source_batch_id,
            source_batch_hash,
            *(str(item["bar_id"]) for item in rows),
            *(str(item["content_hash"]) for item in rows),
        )

    @staticmethod
    def _quality_status(batch: RowMapping, rows: list[RowMapping]) -> str:
        if str(batch["state"]) != "READY" or len(rows) < 21:
            return "BLOCKED"
        periods = tuple(str(item["period_start"]) for item in rows)
        if len(set(periods)) != len(periods):
            return "BLOCKED"
        if len({str(item["adjustment_state"]) for item in rows}) != 1:
            return "BLOCKED"
        return "VALID"

    def _price_component(
        self,
        *,
        batch: RowMapping,
        rows: list[RowMapping],
        symbol: str,
        component_type: ContextComponentType,
        interpretation: str,
        limitations: tuple[str, ...],
        as_of: datetime,
    ) -> ContextComponent:
        latest = rows[-1]
        prior = rows[0]
        return ContextComponent.create(
            component_id=(
                f"total-market-{batch['batch_id']!s}-{as_of.date().isoformat()}-"
                f"{symbol.lower()}-20-session-v1"
            ),
            component_type=component_type,
            source_observation_id=str(latest["bar_id"]),
            provider="ALPACA_IEX_RETAINED",
            subject=symbol,
            observation_at=cast(datetime, latest["period_end"]),
            available_at=cast(datetime, latest["available_at"]),
            ingested_at=cast(datetime, batch["imported_at"]),
            current_value=_retained_decimal(latest["close"]),
            prior_comparable_value=_retained_decimal(prior["close"]),
            freshness_seconds=_age_seconds(as_of, cast(datetime, latest["available_at"])),
            freshness_limit_seconds=COMPONENT_FRESHNESS_LIMIT_SECONDS,
            quality_status=self._quality_status(batch, rows),
            evidence_class="GENUINE_PUBLIC",
            limitations=limitations,
            interpretation_category=interpretation,
            supporting_evidence_ids=self._lineage(
                str(batch["batch_id"]),
                str(batch["canonical_hash"]),
                rows,
            ),
        )
