"""Immutable PostgreSQL evidence for Live Market Observation Foundation v1.

Two tables, mirroring ``crypto_market_pulse_postgres.py``'s refresh/checkpoint
structure and ``kalshi_baseline_quote_postgres.py``'s narrow-columns +
canonical-``payload`` + ``canonical_hash`` + ``authority`` persistence idiom:
``live_market_observation_round`` (one row per bounded capture round) and
``live_market_observation`` (one row per ``(round_id, instrument_id,
observation_type)``), append-only, never mutated, never deleted.

Every dataclass this module persists is fully canonicalizable (str, Decimal,
datetime, Enum, tuple, None, and nested dataclasses) by ``apex.domain.values.
canonical_json_bytes``, so ``payload`` is built by serializing the whole
retained value directly rather than hand-listing fields -- the same idiom
``kalshi_baseline_quote_postgres.py`` uses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.domain.live_market_observation import (
    LiveMarketObservation,
    LiveMarketObservationError,
    LiveObservationCaptureRound,
    LiveObservationQuality,
    LiveObservationRoundStatus,
    LiveObservationType,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()

live_market_observation_round_table = sa.Table(
    "live_market_observation_round",
    metadata,
    sa.Column("round_id", sa.String(300), primary_key=True),
    sa.Column("plan_id", sa.String(300), nullable=False),
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("provider", sa.String(20), nullable=False),
    sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

live_market_observation_table = sa.Table(
    "live_market_observation",
    metadata,
    sa.Column("observation_id", sa.String(300), primary_key=True),
    sa.Column(
        "round_id",
        sa.String(300),
        sa.ForeignKey("live_market_observation_round.round_id"),
        nullable=False,
    ),
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("instrument_id", sa.String(300), nullable=False),
    sa.Column("observation_type", sa.String(20), nullable=False),
    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("evidence_class", sa.String(20), nullable=False),
    sa.Column("quality", sa.String(10), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)

LIVE_MARKET_OBSERVATION_TABLES = (
    "live_market_observation_round",
    "live_market_observation",
)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise LiveMarketObservationError("LIVE_OBSERVATION_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _round(row: RowMapping) -> LiveObservationCaptureRound:
    value = _data(row)
    return LiveObservationCaptureRound(
        round_id=str(value["round_id"]),
        plan_id=str(value["plan_id"]),
        venue=SharedVenue(value["venue"]),
        provider=str(value["provider"]),
        window_start=_time(value["window_start"]),
        requested_at=_time(value["requested_at"]),
        completed_at=_optional_time(value["completed_at"]),
        status=LiveObservationRoundStatus(value["status"]),
        requested_instrument_count=int(value["requested_instrument_count"]),
        succeeded_count=int(value["succeeded_count"]),
        failed_count=int(value["failed_count"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _observation(row: RowMapping) -> LiveMarketObservation:
    value = _data(row)
    return LiveMarketObservation(
        observation_id=str(value["observation_id"]),
        round_id=str(value["round_id"]),
        venue=SharedVenue(value["venue"]),
        provider=str(value["provider"]),
        instrument_id=str(value["instrument_id"]),
        observation_type=LiveObservationType(value["observation_type"]),
        captured_at=_time(value["captured_at"]),
        provider_event_at=_optional_time(value["provider_event_at"]),
        session_context=(
            None if value["session_context"] is None else str(value["session_context"])
        ),
        bid_price=_decimal(value["bid_price"]),
        ask_price=_decimal(value["ask_price"]),
        bid_size=_decimal(value["bid_size"]),
        ask_size=_decimal(value["ask_size"]),
        last_trade_price=_decimal(value["last_trade_price"]),
        bar_open=_decimal(value["bar_open"]),
        bar_high=_decimal(value["bar_high"]),
        bar_low=_decimal(value["bar_low"]),
        bar_close=_decimal(value["bar_close"]),
        bar_volume=_decimal(value["bar_volume"]),
        bar_interval_seconds=(
            None if value["bar_interval_seconds"] is None else int(value["bar_interval_seconds"])
        ),
        book_depth_bid=_decimal(value["book_depth_bid"]),
        book_depth_ask=_decimal(value["book_depth_ask"]),
        evidence_class=EvidenceClass(value["evidence_class"]),
        provider_schema_version=str(value["provider_schema_version"]),
        source_identity=str(value["source_identity"]),
        quality=LiveObservationQuality(value["quality"]),
        quality_reasons=tuple(str(item) for item in value["quality_reasons"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


class PostgresLiveMarketObservationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def capture_round(self, round_id: str) -> LiveObservationCaptureRound | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(live_market_observation_round_table).where(
                        live_market_observation_round_table.c.round_id == round_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _round(row)

    def save_capture_round(self, value: LiveObservationCaptureRound) -> LiveObservationCaptureRound:
        """Idempotent: replaying the identical ``round_id``/``canonical_hash``
        (a genuine crash-resume replay of the same observation-window-minute)
        returns the retained value and writes nothing; a conflicting payload
        under the same ``round_id`` raises rather than silently overwriting
        retained evidence. Mirrors ``PostgresKalshiBaselineQuoteRepository.
        save_quote`` exactly. The ``(plan_id, window_start)`` unique
        constraint is a second, defensive layer restating the domain
        identity formula at the database level."""

        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(live_market_observation_round_table.c.canonical_hash).where(
                        live_market_observation_round_table.c.round_id == value.round_id
                    )
                )
                if existing is not None:
                    if existing != value.canonical_hash:
                        raise LiveMarketObservationError("LIVE_OBSERVATION_ROUND_IDENTITY_CONFLICT")
                    return value
                connection.execute(
                    live_market_observation_round_table.insert().values(
                        round_id=value.round_id,
                        plan_id=value.plan_id,
                        venue=value.venue.value,
                        provider=value.provider,
                        window_start=value.window_start,
                        requested_at=value.requested_at,
                        completed_at=value.completed_at,
                        status=value.status.value,
                        payload=_payload(value),
                        canonical_hash=value.canonical_hash,
                        authority="NONE",
                    )
                )
            return value
        except IntegrityError as error:
            raise LiveMarketObservationError(
                "LIVE_OBSERVATION_ROUND_PERSISTENCE_CONFLICT"
            ) from error

    def observation(self, observation_id: str) -> LiveMarketObservation | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(live_market_observation_table).where(
                        live_market_observation_table.c.observation_id == observation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _observation(row)

    def save_observation(self, value: LiveMarketObservation) -> LiveMarketObservation:
        """Idempotent, mirroring ``save_capture_round`` exactly. The
        ``(round_id, instrument_id, observation_type)`` unique constraint is
        the defensive, database-level restatement of ``observation_id``'s
        own identity formula."""

        try:
            with self._engine.begin() as connection:
                existing = connection.scalar(
                    sa.select(live_market_observation_table.c.canonical_hash).where(
                        live_market_observation_table.c.observation_id == value.observation_id
                    )
                )
                if existing is not None:
                    if existing != value.canonical_hash:
                        raise LiveMarketObservationError("LIVE_OBSERVATION_IDENTITY_CONFLICT")
                    return value
                connection.execute(
                    live_market_observation_table.insert().values(
                        observation_id=value.observation_id,
                        round_id=value.round_id,
                        venue=value.venue.value,
                        instrument_id=value.instrument_id,
                        observation_type=value.observation_type.value,
                        captured_at=value.captured_at,
                        evidence_class=value.evidence_class.value,
                        quality=value.quality.value,
                        payload=_payload(value),
                        canonical_hash=value.canonical_hash,
                        authority="NONE",
                    )
                )
            return value
        except IntegrityError as error:
            raise LiveMarketObservationError("LIVE_OBSERVATION_PERSISTENCE_CONFLICT") from error

    def observations_for_round(self, round_id: str) -> tuple[LiveMarketObservation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(live_market_observation_table)
                .where(live_market_observation_table.c.round_id == round_id)
                .order_by(live_market_observation_table.c.instrument_id)
            ).mappings()
            return tuple(_observation(row) for row in rows)

    def latest_observation(
        self,
        *,
        venue: SharedVenue,
        instrument_id: str,
        observation_type: LiveObservationType,
        as_of: datetime,
    ) -> LiveMarketObservation | None:
        """Point-in-time lookup: the most recent retained observation whose
        ``captured_at`` is at-or-before the caller's exact ``as_of`` --
        never the unbounded global-latest row. Live Observation Point-in-Time
        Race Fix v1: without this bound, an independently-scheduled live-
        observer worker writing a fresh row after a caller's own (possibly
        already-frozen) ``as_of`` would surface a future observation here,
        which ``evaluate_contemporaneous_observation`` then correctly, but
        unhelpfully, rejects via ``LIVE_OBSERVATION_FUTURE_TIMESTAMP`` --
        even when a genuinely fresh, point-in-time-valid observation already
        exists. Ties on ``captured_at`` break on ``observation_id`` (a
        content-addressed, not chronological, string) purely for deterministic
        replay -- not a recency signal -- so a repeated query against
        unchanged retained data always resolves to the identical row."""

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(live_market_observation_table)
                    .where(
                        live_market_observation_table.c.venue == venue.value,
                        live_market_observation_table.c.instrument_id == instrument_id,
                        live_market_observation_table.c.observation_type == observation_type.value,
                        live_market_observation_table.c.captured_at <= as_of,
                    )
                    .order_by(
                        live_market_observation_table.c.captured_at.desc(),
                        live_market_observation_table.c.observation_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _observation(row)

    def observations_for_instrument_window(
        self,
        *,
        instrument_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[LiveMarketObservation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(live_market_observation_table)
                .where(
                    live_market_observation_table.c.instrument_id == instrument_id,
                    live_market_observation_table.c.captured_at >= window_start,
                    live_market_observation_table.c.captured_at <= window_end,
                )
                .order_by(
                    live_market_observation_table.c.captured_at.asc(),
                    live_market_observation_table.c.observation_id.asc(),
                )
            ).mappings()
            return tuple(_observation(row) for row in rows)

    def census(self) -> dict[str, object]:
        """Bounded operator diagnostic: retained observation counts by venue
        and quality -- never a substitute for reading a specific round's own
        observations."""

        with self._engine.connect() as connection:
            by_venue_quality: dict[str, int] = {
                f"{venue}/{quality}": int(count)
                for venue, quality, count in connection.execute(
                    sa.select(
                        live_market_observation_table.c.venue,
                        live_market_observation_table.c.quality,
                        sa.func.count(),
                    ).group_by(
                        live_market_observation_table.c.venue,
                        live_market_observation_table.c.quality,
                    )
                ).all()
            }
            total_observations = connection.scalar(
                sa.select(sa.func.count()).select_from(live_market_observation_table)
            )
            total_rounds = connection.scalar(
                sa.select(sa.func.count()).select_from(live_market_observation_round_table)
            )
        return {
            "total_round_rows": int(total_rounds or 0),
            "total_observation_rows": int(total_observations or 0),
            "by_venue_and_quality": by_venue_quality,
        }
