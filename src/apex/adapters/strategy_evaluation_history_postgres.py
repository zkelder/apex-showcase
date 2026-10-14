"""Immutable PostgreSQL evidence for Strategy Evaluation History & Promotion
Lineage v1.

Two append-only tables, never mutated, never deleted:
``internal_strategy_evaluation`` (one row per genuinely distinct
``domain.strategy_evaluation_history.InternalStrategyEvaluationRecord``) and
``internal_strategy_promotion`` (one row per genuinely distinct
``domain.strategy_evaluation_history.InternalStrategyPromotionRecord``).
Implements ``application.strategy_evaluation_history``'s two repository
Protocols. No provider client, credential, or network capability lives
here, and no row this module writes ever carries an ``authority`` other
than ``'NONE'``.

Mirrors ``adapters.external_strategy_research_postgres.py``'s own
"idempotent insert-or-conflict, payload stored verbatim as canonical JSON,
round-trip through the domain constructor with a hash assertion" idiom
throughout -- deliberately reused rather than re-derived (see ``domain.
strategy_evaluation_history``'s own module docstring for why these stay
physically separate tables from ``external_strategy_definition``/
``external_strategy_evaluation`` despite the shared idiom).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.application.strategy_evaluation_history import StrategyEvaluationHistoryResearchError
from apex.domain.external_strategy import ExternalStrategyQualification
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_definition import (
    StrategyLifecycleState,
    StrategyRuleKind,
    StrategySourceKind,
)
from apex.domain.strategy_evaluation import StrategyEvaluationKind, StrategyEvaluationResult
from apex.domain.strategy_evaluation_history import (
    InternalStrategyEvaluationRecord,
    InternalStrategyPromotionRecord,
)
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()

_SOURCE_KINDS = ("INTERNAL", "EXTERNAL")
_VENUES = ("CRYPTO", "KALSHI", "SECURITIES")
_RULE_KINDS = (
    "BUY_AND_HOLD",
    "MOVING_AVERAGE_TREND",
    "TIME_SERIES_MOMENTUM",
    "MEAN_REVERSION",
    "INTERNAL_METHOD_REFERENCE",
)
_LIFECYCLE_STATES = ("RESEARCH_ONLY", "PAPER_ELIGIBLE", "ACTIVE", "RETIRED")
_QUALIFICATIONS = (
    "RESEARCH_ONLY_INSUFFICIENT_SAMPLE",
    "RESEARCH_ONLY_NONPOSITIVE_NET_RETURN",
    "WOULD_QUALIFY_FOR_PAPER_ELIGIBLE",
)

internal_strategy_evaluation_table = sa.Table(
    "internal_strategy_evaluation",
    metadata,
    sa.Column("evaluation_id", sa.String(300), primary_key=True),
    sa.Column("strategy_id", sa.String(300), nullable=False),
    sa.Column("strategy_version", sa.Integer, nullable=False),
    sa.Column("source_kind", sa.String(16), nullable=False),
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("rule_kind", sa.String(40), nullable=False),
    sa.Column("instrument_ids", sa.Text(), nullable=False),
    sa.Column("cycle_id", sa.String(300), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("capture_id", sa.String(300), nullable=False),
    sa.Column("evaluation_policy_version", sa.String(60), nullable=False),
    sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("qualification", sa.String(50), nullable=False),
    sa.Column("lifecycle_state_before", sa.String(20), nullable=False),
    sa.Column("lifecycle_state_recommended_after", sa.String(20), nullable=False),
    sa.Column("sample_size", sa.Integer, nullable=False),
    sa.Column("net_return", sa.Numeric, nullable=True),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
)

internal_strategy_promotion_table = sa.Table(
    "internal_strategy_promotion",
    metadata,
    sa.Column("promotion_id", sa.String(300), primary_key=True),
    sa.Column("strategy_id", sa.String(300), nullable=False),
    sa.Column("strategy_version", sa.Integer, nullable=False),
    sa.Column("evaluation_id", sa.String(300), nullable=False),
    sa.Column("lifecycle_state_before", sa.String(20), nullable=False),
    sa.Column("lifecycle_state_after", sa.String(20), nullable=False),
    sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("promotion_policy_version", sa.String(60), nullable=False),
    sa.Column("actor", sa.String(200), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=True),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    sa.ForeignKeyConstraint(
        ["evaluation_id"],
        ["internal_strategy_evaluation.evaluation_id"],
        name="fk_internal_strategy_promotion_evaluation",
    ),
)

STRATEGY_EVALUATION_HISTORY_TABLES = ("internal_strategy_evaluation", "internal_strategy_promotion")


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _int_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StrategyEvaluationHistoryResearchError(
            "INTERNAL_STRATEGY_EVALUATION_PAYLOAD_FIELD_NOT_INT"
        )
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else _int_value(value)


def _str_value(value: object) -> str:
    if not isinstance(value, str):
        raise StrategyEvaluationHistoryResearchError(
            "INTERNAL_STRATEGY_EVALUATION_PAYLOAD_FIELD_NOT_STR"
        )
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise StrategyEvaluationHistoryResearchError(
            "INTERNAL_STRATEGY_EVALUATION_PAYLOAD_FIELD_NOT_LIST"
        )
    return tuple(_str_value(item) for item in value)


def _candidate_from_dict(d: Mapping[str, object]) -> StrategyEvaluationResult:
    return StrategyEvaluationResult(
        strategy_id=_str_value(d["strategy_id"]),
        strategy_version=_int_value(d["strategy_version"]),
        source_kind=StrategySourceKind(_str_value(d["source_kind"])),
        evaluation_kind=StrategyEvaluationKind(_str_value(d["evaluation_kind"])),
        benchmark_identity=_str_value(d["benchmark_identity"]),
        window_start=None if d["window_start"] is None else _time(d["window_start"]),
        window_end=_time(d["window_end"]),
        candidate_count=_int_value(d["candidate_count"]),
        trade_count=_int_value(d["trade_count"]),
        abstained_count=_int_value(d["abstained_count"]),
        sample_size=_int_value(d["sample_size"]),
        win_count=_optional_int(d["win_count"]),
        loss_count=_optional_int(d["loss_count"]),
        win_rate=_optional_decimal(d["win_rate"]),
        average_trade_return=_optional_decimal(d["average_trade_return"]),
        median_trade_return=_optional_decimal(d["median_trade_return"]),
        gross_return=_optional_decimal(d["gross_return"]),
        net_return=_optional_decimal(d["net_return"]),
        maximum_drawdown=_optional_decimal(d["maximum_drawdown"]),
        turnover=_optional_decimal(d["turnover"]),
        total_fees=_optional_decimal(d["total_fees"]),
        total_slippage=_optional_decimal(d["total_slippage"]),
        total_spread=_optional_decimal(d["total_spread"]),
        fee_bps=_optional_decimal(d["fee_bps"]),
        spread_bps=_optional_decimal(d["spread_bps"]),
        slippage_bps=_optional_decimal(d["slippage_bps"]),
        evidence_class=_str_value(d["evidence_class"]),
        evidence_provenance=_str_value(d["evidence_provenance"]),
        unavailable_fields=_strings(d["unavailable_fields"]),
        canonical_hash=_str_value(d["canonical_hash"]),
    )


def _evaluation_from_row(row: RowMapping) -> InternalStrategyEvaluationRecord:
    payload = json.loads(str(row["payload"]))
    candidate = _candidate_from_dict(payload["candidate"])
    instrument_ids = tuple(str(item) for item in json.loads(str(row["instrument_ids"])))
    reconstructed = InternalStrategyEvaluationRecord(
        evaluation_id=str(row["evaluation_id"]),
        strategy_id=str(row["strategy_id"]),
        strategy_version=int(row["strategy_version"]),
        source_kind=StrategySourceKind(str(row["source_kind"])),
        venue=SharedVenue(str(row["venue"])),
        instrument_ids=instrument_ids,
        rule_kind=StrategyRuleKind(str(row["rule_kind"])),
        cycle_id=str(row["cycle_id"]),
        as_of=_time(row["as_of"]),
        capture_id=str(row["capture_id"]),
        evaluation_policy_version=str(row["evaluation_policy_version"]),
        evaluated_at=_time(row["evaluated_at"]),
        candidate=candidate,
        qualification=ExternalStrategyQualification(str(row["qualification"])),
        lifecycle_state_before=StrategyLifecycleState(str(row["lifecycle_state_before"])),
        lifecycle_state_recommended_after=StrategyLifecycleState(
            str(row["lifecycle_state_recommended_after"])
        ),
        canonical_hash=str(row["canonical_hash"]),
        authority=str(row["authority"]),
    )
    return reconstructed


def _promotion_from_row(row: RowMapping) -> InternalStrategyPromotionRecord:
    return InternalStrategyPromotionRecord(
        promotion_id=str(row["promotion_id"]),
        strategy_id=str(row["strategy_id"]),
        strategy_version=int(row["strategy_version"]),
        evaluation_id=str(row["evaluation_id"]),
        lifecycle_state_before=StrategyLifecycleState(str(row["lifecycle_state_before"])),
        lifecycle_state_after=StrategyLifecycleState(str(row["lifecycle_state_after"])),
        promoted_at=_time(row["promoted_at"]),
        promotion_policy_version=str(row["promotion_policy_version"]),
        actor=str(row["actor"]),
        canonical_hash=str(row["canonical_hash"]),
        authority=str(row["authority"]),
    )


class PostgresInternalStrategyEvaluationRepository:
    """Implements ``application.strategy_evaluation_history.
    InternalStrategyEvaluationRepository`` against real PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(self, *, record: InternalStrategyEvaluationRecord) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = connection.execute(
                    sa.select(internal_strategy_evaluation_table.c.canonical_hash).where(
                        internal_strategy_evaluation_table.c.evaluation_id == record.evaluation_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != record.canonical_hash:
                        raise StrategyEvaluationHistoryResearchError(
                            "INTERNAL_STRATEGY_EVALUATION_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    internal_strategy_evaluation_table.insert().values(
                        evaluation_id=record.evaluation_id,
                        strategy_id=record.strategy_id,
                        strategy_version=record.strategy_version,
                        source_kind=record.source_kind.value,
                        venue=record.venue.value,
                        rule_kind=record.rule_kind.value,
                        instrument_ids=_payload(list(record.instrument_ids)),
                        cycle_id=record.cycle_id,
                        as_of=record.as_of,
                        capture_id=record.capture_id,
                        evaluation_policy_version=record.evaluation_policy_version,
                        evaluated_at=record.evaluated_at,
                        qualification=record.qualification.value,
                        lifecycle_state_before=record.lifecycle_state_before.value,
                        lifecycle_state_recommended_after=(
                            record.lifecycle_state_recommended_after.value
                        ),
                        sample_size=record.candidate.sample_size,
                        net_return=record.candidate.net_return,
                        canonical_hash=record.canonical_hash,
                        payload=_payload({"candidate": record.candidate}),
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise StrategyEvaluationHistoryResearchError(
                "INTERNAL_STRATEGY_EVALUATION_PERSISTENCE_CONFLICT"
            ) from error

    def get(self, *, evaluation_id: str) -> InternalStrategyEvaluationRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(internal_strategy_evaluation_table).where(
                        internal_strategy_evaluation_table.c.evaluation_id == evaluation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _evaluation_from_row(row)

    def for_strategy(self, *, strategy_id: str) -> tuple[InternalStrategyEvaluationRecord, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(internal_strategy_evaluation_table)
                .where(internal_strategy_evaluation_table.c.strategy_id == strategy_id)
                .order_by(
                    internal_strategy_evaluation_table.c.evaluated_at.desc(),
                    internal_strategy_evaluation_table.c.evaluation_id.desc(),
                )
            ).mappings()
            return tuple(_evaluation_from_row(row) for row in rows)

    def latest_for_strategy(self, *, strategy_id: str) -> InternalStrategyEvaluationRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(internal_strategy_evaluation_table)
                    .where(internal_strategy_evaluation_table.c.strategy_id == strategy_id)
                    .order_by(
                        internal_strategy_evaluation_table.c.evaluated_at.desc(),
                        internal_strategy_evaluation_table.c.evaluation_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _evaluation_from_row(row)


class PostgresInternalStrategyPromotionRepository:
    """Implements ``application.strategy_evaluation_history.
    InternalStrategyPromotionRepository`` against real PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(self, *, record: InternalStrategyPromotionRecord) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = connection.execute(
                    sa.select(internal_strategy_promotion_table.c.canonical_hash).where(
                        internal_strategy_promotion_table.c.promotion_id == record.promotion_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != record.canonical_hash:
                        raise StrategyEvaluationHistoryResearchError(
                            "INTERNAL_STRATEGY_PROMOTION_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    internal_strategy_promotion_table.insert().values(
                        promotion_id=record.promotion_id,
                        strategy_id=record.strategy_id,
                        strategy_version=record.strategy_version,
                        evaluation_id=record.evaluation_id,
                        lifecycle_state_before=record.lifecycle_state_before.value,
                        lifecycle_state_after=record.lifecycle_state_after.value,
                        promoted_at=record.promoted_at,
                        promotion_policy_version=record.promotion_policy_version,
                        actor=record.actor,
                        canonical_hash=record.canonical_hash,
                        payload=None,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise StrategyEvaluationHistoryResearchError(
                "INTERNAL_STRATEGY_PROMOTION_PERSISTENCE_CONFLICT"
            ) from error

    def get(self, *, promotion_id: str) -> InternalStrategyPromotionRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(internal_strategy_promotion_table).where(
                        internal_strategy_promotion_table.c.promotion_id == promotion_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _promotion_from_row(row)

    def for_strategy(self, *, strategy_id: str) -> tuple[InternalStrategyPromotionRecord, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(internal_strategy_promotion_table)
                .where(internal_strategy_promotion_table.c.strategy_id == strategy_id)
                .order_by(internal_strategy_promotion_table.c.promoted_at)
            ).mappings()
            return tuple(_promotion_from_row(row) for row in rows)
