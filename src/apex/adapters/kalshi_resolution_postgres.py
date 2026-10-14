"""Immutable PostgreSQL evidence for Kalshi Settlement & Resolution Evidence
Maturation v1.

Persists one ``KalshiMarketResolutionFact`` row per genuinely distinct
observed Kalshi market resolution/settlement content -- append-only, never
mutated, never deleted. Implements ``application.kalshi_resolution.
KalshiResolutionFactRepository``. No provider client, credential, or
network capability lives here, and no row this module writes ever carries
an ``authority`` other than ``'NONE'``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.domain.kalshi_resolution import (
    KalshiMarketResolutionFact,
    KalshiResolutionError,
    KalshiResolutionState,
)
from apex.domain.research_evidence import EvidenceClass

metadata = sa.MetaData()

resolution_fact_table = sa.Table(
    "kalshi_resolution_fact",
    metadata,
    sa.Column("resolution_fact_id", sa.String(300), primary_key=True),
    sa.Column("market_ticker", sa.String(300), nullable=False),
    sa.Column("event_ticker", sa.String(300), nullable=True),
    sa.Column("provider_status", sa.String(30), nullable=False),
    sa.Column("resolution_state", sa.String(30), nullable=False),
    sa.Column("raw_result", sa.String(20), nullable=True),
    sa.Column("resolved_result", sa.String(20), nullable=True),
    sa.Column("settlement_value_dollars", sa.Numeric, nullable=True),
    sa.Column("settlement_ts", sa.DateTime(timezone=True), nullable=True),
    sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
    sa.Column("source_endpoint_identity", sa.String(80), nullable=False),
    sa.Column("evidence_class", sa.String(20), nullable=False),
    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("run_id", sa.String(300), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

KALSHI_RESOLUTION_FACT_TABLES = ("kalshi_resolution_fact",)


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _fact(row: RowMapping) -> KalshiMarketResolutionFact:
    return KalshiMarketResolutionFact(
        resolution_fact_id=str(row["resolution_fact_id"]),
        market_ticker=str(row["market_ticker"]),
        event_ticker=(None if row["event_ticker"] is None else str(row["event_ticker"])),
        provider_status=str(row["provider_status"]),
        resolution_state=KalshiResolutionState(row["resolution_state"]),
        raw_result=(None if row["raw_result"] is None else str(row["raw_result"])),
        resolved_result=(None if row["resolved_result"] is None else str(row["resolved_result"])),
        settlement_value_dollars=_optional_decimal(row["settlement_value_dollars"]),
        settlement_ts=_optional_time(row["settlement_ts"]),
        close_time=_optional_time(row["close_time"]),
        source_endpoint_identity=str(row["source_endpoint_identity"]),
        evidence_class=EvidenceClass(row["evidence_class"]),
        captured_at=_time(row["captured_at"]),
        run_id=str(row["run_id"]),
        canonical_hash=str(row["canonical_hash"]),
        authority=str(row["authority"]),
    )


class PostgresKalshiResolutionFactRepository:
    """Implements ``application.kalshi_resolution.
    KalshiResolutionFactRepository`` against real PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def latest_fact(
        self, *, market_ticker: str, as_of: datetime
    ) -> KalshiMarketResolutionFact | None:
        """Point-in-time bounded, mirroring ``PostgresLiveMarketObservation
        Repository.latest_observation`` exactly (Live Observation
        Point-in-Time Race Fix v1): ``captured_at <= as_of`` is applied
        inside the query itself, before ordering and limiting -- never
        fetched unbounded and filtered in Python -- so a fact genuinely
        captured after the caller's own ``as_of`` can never be surfaced,
        no matter how much more recent or authoritative it is."""

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(resolution_fact_table)
                    .where(
                        resolution_fact_table.c.market_ticker == market_ticker,
                        resolution_fact_table.c.captured_at <= as_of,
                    )
                    .order_by(
                        resolution_fact_table.c.captured_at.desc(),
                        resolution_fact_table.c.resolution_fact_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _fact(row)

    def record(self, *, fact: KalshiMarketResolutionFact) -> bool:
        """Persist one resolution fact idempotently -- mirrors
        ``PostgresKalshiBaselineQuoteRepository.save_quote``'s exact
        idempotent-insert-or-conflict shape."""

        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(resolution_fact_table.c.canonical_hash).where(
                        resolution_fact_table.c.resolution_fact_id == fact.resolution_fact_id
                    )
                )
                if existing is not None:
                    if existing != fact.canonical_hash:
                        raise KalshiResolutionError("KALSHI_RESOLUTION_FACT_IDENTITY_CONFLICT")
                    return True
                connection.execute(
                    resolution_fact_table.insert().values(
                        resolution_fact_id=fact.resolution_fact_id,
                        market_ticker=fact.market_ticker,
                        event_ticker=fact.event_ticker,
                        provider_status=fact.provider_status,
                        resolution_state=fact.resolution_state.value,
                        raw_result=fact.raw_result,
                        resolved_result=fact.resolved_result,
                        settlement_value_dollars=fact.settlement_value_dollars,
                        settlement_ts=fact.settlement_ts,
                        close_time=fact.close_time,
                        source_endpoint_identity=fact.source_endpoint_identity,
                        evidence_class=fact.evidence_class.value,
                        captured_at=fact.captured_at,
                        run_id=fact.run_id,
                        canonical_hash=fact.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise KalshiResolutionError("KALSHI_RESOLUTION_FACT_PERSISTENCE_CONFLICT") from error

    def facts_for_ticker(self, *, market_ticker: str) -> tuple[KalshiMarketResolutionFact, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(resolution_fact_table)
                .where(resolution_fact_table.c.market_ticker == market_ticker)
                .order_by(
                    resolution_fact_table.c.captured_at, resolution_fact_table.c.resolution_fact_id
                )
            ).mappings()
            return tuple(_fact(row) for row in rows)

    def census(self) -> dict[str, object]:
        """Bounded operator diagnostic backing the operator-observability
        surface this milestone adds (Part 12). ``current_state_by_market``
        is a snapshot over each distinct ticker's *latest* retained fact
        (Postgres ``DISTINCT ON``, ordered exactly like ``latest_fact``) --
        never a raw tally of historical fact rows -- so it answers "how
        many tracked markets are currently unresolved" honestly even for a
        market with several retained observations (e.g. closed ->
        determined -> finalized all genuinely retained for one ticker would
        otherwise double- or triple-count it)."""

        latest_per_ticker = (
            sa.select(
                resolution_fact_table.c.market_ticker, resolution_fact_table.c.resolution_state
            )
            .distinct(resolution_fact_table.c.market_ticker)
            .order_by(
                resolution_fact_table.c.market_ticker,
                resolution_fact_table.c.captured_at.desc(),
                resolution_fact_table.c.resolution_fact_id.desc(),
            )
            .subquery()
        )
        with self._engine.connect() as connection:
            by_state = {
                str(state): int(count)
                for state, count in connection.execute(
                    sa.select(latest_per_ticker.c.resolution_state, sa.func.count()).group_by(
                        latest_per_ticker.c.resolution_state
                    )
                ).all()
            }
            total = connection.scalar(sa.select(sa.func.count()).select_from(resolution_fact_table))
            distinct_tickers = connection.scalar(
                sa.select(sa.func.count(sa.distinct(resolution_fact_table.c.market_ticker)))
            )
            latest_captured_at = connection.scalar(
                sa.select(sa.func.max(resolution_fact_table.c.captured_at))
            )
        return {
            "total_fact_rows": int(total or 0),
            "distinct_market_tickers": int(distinct_tickers or 0),
            "current_state_by_market": by_state,
            "latest_captured_at": (
                None if latest_captured_at is None else _time(latest_captured_at).isoformat()
            ),
        }
