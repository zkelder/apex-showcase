"""Historical Total Market Context source (section 7 of the replay milestone).

Satisfies the *existing* ``TotalMarketContextSource`` protocol
(``application.total_market_context.TotalMarketContextSource``) unchanged --
so the existing ``MaterializeTotalMarketContextSnapshot`` application service
and ``domain.total_market_context`` classification logic (``ContextComponent
.create``/``build_context_snapshot``) are reused exactly as-is. Only the
*evidence* differs: this adapter reads genuine point-in-time bars from a
historical replay's ``HistoricalDatasetSnapshot`` (gated by ``available_at <=
as_of``), never the live production batch and never wall-clock time.

``source_batch_id`` in the protocol's ``load()`` signature is repurposed here
to mean a ``HistoricalDatasetSnapshot.snapshot_id`` -- the parameter name is
generic enough in the protocol itself; this is the identical "one exact
immutable evidence identity in, one bundle out" shape the live adapter
(``adapters.total_market_context_sources.RetainedTotalMarketContextSource``)
already uses, just resolved against replay evidence instead of the one
genuine live batch.

Do not use this to fabricate current context for a historical date: every
component is built only from bars whose ``available_at`` was at or before
the replayed ``as_of``, and the resulting snapshot's own ``created_at`` is
that historical ``as_of``, never wall-clock "now".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.historical_market_data_postgres import PostgresHistoricalMarketDataRepository
from apex.adapters.postgres import securities_bar_evidence, securities_instrument
from apex.domain.total_market_context import (
    COMPONENT_FRESHNESS_LIMIT_SECONDS,
    ContextComponent,
    ContextComponentType,
    ContextSourceBundle,
    TotalMarketContextError,
    UnavailableContextComponent,
)

REFERENCE_SYMBOLS = ("SPY", "TLT", "USO")


def _retained_decimal(value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TotalMarketContextError("TOTAL_MARKET_HISTORICAL_SOURCE_DECIMAL_INVALID")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return Decimal(rendered)


def _age_seconds(as_of: datetime, observed_at: datetime) -> int:
    if as_of.tzinfo is None or observed_at.tzinfo is None:
        raise TotalMarketContextError("TOTAL_MARKET_HISTORICAL_SOURCE_TIME_INVALID")
    seconds = int((as_of.astimezone(UTC) - observed_at.astimezone(UTC)).total_seconds())
    if seconds < 0:
        raise TotalMarketContextError("TOTAL_MARKET_HISTORICAL_SOURCE_AFTER_CONTEXT_CUTOFF")
    return seconds


def _annualized_volatility(closes: tuple[Decimal, ...]) -> Decimal:
    if len(closes) < 21:
        raise TotalMarketContextError("TOTAL_MARKET_HISTORICAL_VOLATILITY_HISTORY_INSUFFICIENT")
    returns = tuple(closes[index] / closes[index - 1] - 1 for index in range(1, len(closes)))
    average = sum(returns, Decimal()) / Decimal(len(returns))
    variance = sum(((value - average) ** 2 for value in returns), Decimal()) / Decimal(len(returns))
    with localcontext() as context:
        context.prec = 28
        return (variance.sqrt() * Decimal(252).sqrt()).quantize(Decimal("0.00000001"))


class HistoricalTotalMarketContextSource:
    """Build components from one exact immutable historical dataset
    snapshot's point-in-time bars -- never from current/live evidence."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, *, source_batch_id: str, as_of: datetime) -> ContextSourceBundle:
        if as_of.tzinfo is None:
            raise TotalMarketContextError("TOTAL_MARKET_HISTORICAL_SOURCE_TIME_INVALID")
        cutoff = as_of.astimezone(UTC)
        historical_repository = PostgresHistoricalMarketDataRepository(self._engine)
        snapshot = historical_repository.snapshot_by_id(source_batch_id)
        if snapshot is None:
            raise TotalMarketContextError("TOTAL_MARKET_HISTORICAL_SNAPSHOT_NOT_FOUND")
        if not snapshot.source_batch_ids:
            return ContextSourceBundle((), self._all_unavailable())
        with self._engine.connect() as connection:
            rows = tuple(
                connection.execute(
                    sa.select(securities_bar_evidence, securities_instrument.c.symbol)
                    .join(
                        securities_instrument,
                        securities_instrument.c.instrument_id
                        == securities_bar_evidence.c.instrument_id,
                    )
                    .where(securities_bar_evidence.c.batch_id.in_(snapshot.source_batch_ids))
                    .where(securities_instrument.c.symbol.in_(REFERENCE_SYMBOLS))
                    .where(securities_bar_evidence.c.interval == "ONE_DAY")
                    .where(securities_bar_evidence.c.adjustment_state == "RAW")
                    .where(securities_bar_evidence.c.available_at <= cutoff)
                    .order_by(
                        securities_instrument.c.symbol, securities_bar_evidence.c.period_start
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
                "HISTORICAL_SPY_20_SESSION_TOTAL_MARKET_PRICE_DIRECTION",
                (
                    "SPY_IS_AN_ETF_PRICE_OBSERVATION_NOT_THE_FULL_US_MARKET",
                    "PRICE_DIRECTION_DOES_NOT_PREDICT_OPPORTUNITY_RETURNS",
                    "HISTORICAL_REPLAY_EVIDENCE_NOT_LIVE_CONTEXT",
                ),
            ),
            (
                "TLT",
                ContextComponentType.TREASURY_RATE,
                "HISTORICAL_TLT_20_SESSION_LONG_DURATION_TREASURY_PRICE_PROXY",
                (
                    "TLT_PRICE_DIRECTION_IS_NOT_A_TREASURY_YIELD",
                    "DURATION_AND_FUND_EFFECTS_LIMIT_RATE_INTERPRETATION",
                    "HISTORICAL_REPLAY_EVIDENCE_NOT_LIVE_CONTEXT",
                ),
            ),
            (
                "USO",
                ContextComponentType.COMMODITY_OR_INFLATION,
                "HISTORICAL_USO_20_SESSION_OIL_PRICE_PROXY",
                (
                    "USO_IS_A_FUND_PRICE_PROXY_NOT_SPOT_OIL_OR_INFLATION",
                    "FUTURES_ROLL_EFFECTS_MAY_AFFECT_THE_FUND_PRICE",
                    "HISTORICAL_REPLAY_EVIDENCE_NOT_LIVE_CONTEXT",
                ),
            ),
        ):
            symbol_rows = by_symbol.get(symbol, [])
            if len(symbol_rows) < 21:
                continue
            selected = symbol_rows[-21:]
            components.append(
                self._price_component(
                    snapshot_id=snapshot.snapshot_id,
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
                        f"historical-total-market-{snapshot.snapshot_id}-"
                        f"{cutoff.date().isoformat()}-spy-realized-volatility-v1"
                    ),
                    component_type=ContextComponentType.MARKET_VOLATILITY,
                    source_observation_id=str(latest["bar_id"]),
                    provider="HISTORICAL_REPLAY_RETAINED",
                    subject="SPY",
                    observation_at=cast(datetime, latest["period_end"]),
                    available_at=cast(datetime, latest["available_at"]),
                    ingested_at=cutoff,
                    current_value=current_volatility,
                    prior_comparable_value=prior_volatility,
                    freshness_seconds=_age_seconds(cutoff, cast(datetime, latest["available_at"])),
                    freshness_limit_seconds=COMPONENT_FRESHNESS_LIMIT_SECONDS,
                    quality_status=self._quality_status(current_rows),
                    evidence_class="HISTORICAL_PUBLIC",
                    limitations=(
                        "REALIZED_VOLATILITY_IS_DERIVED_FROM_RETAINED_HISTORICAL_SPY_DAILY_CLOSES",
                        "REALIZED_VOLATILITY_IS_NOT_VIX_OR_FORWARD_IMPLIED_VOLATILITY",
                        "HISTORICAL_REPLAY_EVIDENCE_NOT_LIVE_CONTEXT",
                    ),
                    interpretation_category="HISTORICAL_SPY_20_SESSION_ANNUALIZED_REALIZED_VOLATILITY",
                    supporting_evidence_ids=self._lineage(snapshot.snapshot_id, current_rows),
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

    def _all_unavailable(self) -> tuple[UnavailableContextComponent, ...]:
        return tuple(
            UnavailableContextComponent(component_type, "HISTORICAL_SNAPSHOT_HAS_NO_SOURCE_BATCHES")
            for component_type in ContextComponentType
        )

    @staticmethod
    def _lineage(snapshot_id: str, rows: list[RowMapping]) -> tuple[str, ...]:
        return (
            snapshot_id,
            *(str(item["bar_id"]) for item in rows),
            *(str(item["content_hash"]) for item in rows),
        )

    @staticmethod
    def _quality_status(rows: list[RowMapping]) -> str:
        if len(rows) < 21:
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
        snapshot_id: str,
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
                f"historical-total-market-{snapshot_id}-{as_of.date().isoformat()}-"
                f"{symbol.lower()}-20-session-v1"
            ),
            component_type=component_type,
            source_observation_id=str(latest["bar_id"]),
            provider="HISTORICAL_REPLAY_RETAINED",
            subject=symbol,
            observation_at=cast(datetime, latest["period_end"]),
            available_at=cast(datetime, latest["available_at"]),
            ingested_at=as_of,
            current_value=_retained_decimal(latest["close"]),
            prior_comparable_value=_retained_decimal(prior["close"]),
            freshness_seconds=_age_seconds(as_of, cast(datetime, latest["available_at"])),
            freshness_limit_seconds=COMPONENT_FRESHNESS_LIMIT_SECONDS,
            quality_status=self._quality_status(rows),
            evidence_class="HISTORICAL_PUBLIC",
            limitations=limitations,
            interpretation_category=interpretation,
            supporting_evidence_ids=self._lineage(snapshot_id, rows),
        )
