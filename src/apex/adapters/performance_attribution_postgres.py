"""Immutable PostgreSQL storage for authority-free PAPER attribution evidence."""

from __future__ import annotations

import json
import types
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast, get_args, get_origin, get_type_hints

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.domain.performance_attribution import (
    SHARED_PERFORMANCE_ATTRIBUTION_V1,
    STRATEGY_HEALTH_POLICY_V1,
    AttributionComponent,
    PaperEvidenceClass,
    PaperPerformanceSummary,
    PaperVenue,
    PerformanceAttributionError,
    SharedPerformanceAttribution,
    StrategyHealthAssessment,
    StrategyHealthPolicy,
)
from apex.domain.values import canonical_json_bytes, content_digest, parse_decimal

metadata = sa.MetaData()


def _table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
        sa.Column("live_authority", sa.String(16), nullable=False),
    )


policy_table = _table("shared_performance_policy", "policy_id")
attribution_table = _table(
    "shared_performance_attribution",
    "attribution_id",
    sa.Column("policy_id", sa.String(300), nullable=False),
    sa.Column("source_id", sa.String(300), nullable=False),
    sa.Column("source_hash", sa.String(64), nullable=False),
    sa.Column("venue", sa.String(40), nullable=False),
    sa.Column("evidence_class", sa.String(24), nullable=False),
    sa.Column("strategy_or_method_id", sa.String(300), nullable=False),
    sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(240), nullable=False, unique=True),
)
component_table = _table(
    "shared_performance_component",
    "component_id",
    sa.Column("attribution_id", sa.String(300), nullable=False),
    sa.Column("category", sa.String(40), nullable=False),
)
health_table = _table(
    "strategy_health_assessment",
    "health_id",
    sa.Column("policy_id", sa.String(300), nullable=False),
    sa.Column("strategy_or_method_id", sa.String(300), nullable=False),
    sa.Column("strategy_or_method_version", sa.String(300), nullable=False),
    sa.Column("evidence_class", sa.String(24), nullable=False),
    sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
    sa.Column("state", sa.String(40), nullable=False),
    sa.Column("idempotency_key", sa.String(240), nullable=False, unique=True),
)
venue_summary_table = _table(
    "paper_venue_performance_summary",
    "summary_id",
    sa.Column("scope_type", sa.String(20), nullable=False),
    sa.Column("scope_id", sa.String(300), nullable=False),
    sa.Column("evidence_class", sa.String(24), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(240), nullable=False, unique=True),
)
portfolio_summary_table = _table(
    "paper_portfolio_performance_summary",
    "summary_id",
    sa.Column("scope_type", sa.String(20), nullable=False),
    sa.Column("scope_id", sa.String(300), nullable=False),
    sa.Column("evidence_class", sa.String(24), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(240), nullable=False, unique=True),
)

TABLE_NAMES = tuple(metadata.tables)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise PerformanceAttributionError("ATTRIBUTION_RETAINED_PAYLOAD_INVALID")
    return value


def _restore(type_hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(type_hint)
    arguments = get_args(type_hint)
    if origin is tuple:
        item_type = arguments[0] if arguments else Any
        return tuple(_restore(item_type, item) for item in value)
    if origin in {types.UnionType, getattr(types, "UnionType", object)}:
        possible = tuple(item for item in arguments if item is not type(None))
        return _restore(possible[0], value) if possible else None
    if type_hint is Decimal:
        return parse_decimal(str(value), allow_negative=True)
    if type_hint is datetime:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    if isinstance(type_hint, type) and issubclass(type_hint, Enum):
        return type_hint(value)
    if isinstance(type_hint, type) and is_dataclass(type_hint):
        hints = get_type_hints(type_hint)
        backwards_compatible_optional_fields = {
            "reservation_id",
            "reservation_original_amount",
            "reservation_current_amount",
            "reservation_consumed_amount",
        }
        return type_hint(
            **{
                field.name: _restore(
                    hints[field.name],
                    None
                    if field.name in backwards_compatible_optional_fields
                    and field.name not in value
                    else value[field.name],
                )
                for field in fields(type_hint)
            }
        )
    return value


def _object[T](row: RowMapping, result_type: type[T]) -> T:
    return cast(T, _restore(result_type, _data(row)))


class PostgresPerformanceAttributionRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def attribution(self, attribution_id: str) -> SharedPerformanceAttribution | None:
        row = self._one(
            sa.select(attribution_table).where(attribution_table.c.attribution_id == attribution_id)
        )
        return None if row is None else _object(row, SharedPerformanceAttribution)

    def attribution_for_source(
        self, source_id: str, source_hash: str, evidence_class: PaperEvidenceClass
    ) -> SharedPerformanceAttribution | None:
        row = self._one(
            sa.select(attribution_table).where(
                attribution_table.c.source_id == source_id,
                attribution_table.c.source_hash == source_hash,
                attribution_table.c.evidence_class == evidence_class.value,
            )
        )
        return None if row is None else _object(row, SharedPerformanceAttribution)

    def attributions(
        self, evidence_class: PaperEvidenceClass | None = None
    ) -> tuple[SharedPerformanceAttribution, ...]:
        query = sa.select(attribution_table)
        if evidence_class is not None:
            query = query.where(attribution_table.c.evidence_class == evidence_class.value)
        with self._engine.connect() as connection:
            rows = connection.execute(
                query.order_by(
                    attribution_table.c.materialized_at, attribution_table.c.attribution_id
                )
            ).mappings()
            return tuple(_object(row, SharedPerformanceAttribution) for row in rows)

    def components(self, attribution_id: str) -> tuple[AttributionComponent, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(component_table)
                .where(component_table.c.attribution_id == attribution_id)
                .order_by(component_table.c.component_id)
            ).mappings()
            return tuple(_object(row, AttributionComponent) for row in rows)

    def health(self, health_id: str) -> StrategyHealthAssessment | None:
        row = self._one(sa.select(health_table).where(health_table.c.health_id == health_id))
        return None if row is None else _object(row, StrategyHealthAssessment)

    def health_assessments(
        self, evidence_class: PaperEvidenceClass | None = None
    ) -> tuple[StrategyHealthAssessment, ...]:
        query = sa.select(health_table)
        if evidence_class is not None:
            query = query.where(health_table.c.evidence_class == evidence_class.value)
        with self._engine.connect() as connection:
            rows = connection.execute(
                query.order_by(health_table.c.window_end, health_table.c.health_id)
            ).mappings()
            return tuple(_object(row, StrategyHealthAssessment) for row in rows)

    def _latest_summary(
        self, table: sa.Table, scope_id: str, evidence_class: PaperEvidenceClass
    ) -> PaperPerformanceSummary | None:
        row = self._one(
            sa.select(table)
            .where(table.c.scope_id == scope_id, table.c.evidence_class == evidence_class.value)
            .order_by(table.c.as_of.desc(), table.c.summary_id.desc())
            .limit(1)
        )
        return None if row is None else _object(row, PaperPerformanceSummary)

    def venue_summary(
        self, venue: PaperVenue, evidence_class: PaperEvidenceClass
    ) -> PaperPerformanceSummary | None:
        return self._latest_summary(venue_summary_table, venue.value, evidence_class)

    def portfolio_summary(
        self, evidence_class: PaperEvidenceClass
    ) -> PaperPerformanceSummary | None:
        return self._latest_summary(
            portfolio_summary_table, "COMBINED_PAPER_RESEARCH_PORTFOLIO", evidence_class
        )

    def method_summaries(
        self, evidence_class: PaperEvidenceClass
    ) -> tuple[PaperPerformanceSummary, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(portfolio_summary_table)
                .where(
                    portfolio_summary_table.c.scope_type == "STRATEGY_OR_METHOD",
                    portfolio_summary_table.c.evidence_class == evidence_class.value,
                )
                .order_by(
                    portfolio_summary_table.c.scope_id,
                    portfolio_summary_table.c.as_of.desc(),
                    portfolio_summary_table.c.summary_id.desc(),
                )
            ).mappings()
            latest: dict[str, PaperPerformanceSummary] = {}
            for row in rows:
                summary = _object(row, PaperPerformanceSummary)
                latest.setdefault(summary.scope_id, summary)
            return tuple(latest[key] for key in sorted(latest))

    def _ensure_policies(self, connection: sa.Connection) -> None:
        for identity, payload in (
            (
                SHARED_PERFORMANCE_ATTRIBUTION_V1,
                {"policy_id": SHARED_PERFORMANCE_ATTRIBUTION_V1, "version": 1, "authority": "NONE"},
            ),
            (STRATEGY_HEALTH_POLICY_V1, StrategyHealthPolicy.v1()),
        ):
            digest = content_digest(payload)
            retained = connection.scalar(
                sa.select(policy_table.c.canonical_hash).where(policy_table.c.policy_id == identity)
            )
            if retained is None:
                connection.execute(
                    policy_table.insert().values(
                        policy_id=identity,
                        payload=_payload(payload),
                        canonical_hash=digest,
                        authority="NONE",
                        live_authority="NONE",
                    )
                )
            elif retained != digest:
                raise PerformanceAttributionError("ATTRIBUTION_POLICY_IDENTITY_CONFLICT")

    def save_attribution(
        self,
        *,
        idempotency_key: str,
        attribution: SharedPerformanceAttribution,
        components: tuple[AttributionComponent, ...],
    ) -> bool:
        try:
            with self._engine.begin() as connection:
                self._ensure_policies(connection)
                old = connection.execute(
                    sa.select(
                        attribution_table.c.canonical_hash, attribution_table.c.idempotency_key
                    ).where(attribution_table.c.attribution_id == attribution.attribution_id)
                ).one_or_none()
                if old is not None:
                    if (
                        old.canonical_hash != attribution.canonical_hash
                        or old.idempotency_key != idempotency_key
                    ):
                        raise PerformanceAttributionError("ATTRIBUTION_IDENTITY_CONFLICT")
                    return True
                if (
                    connection.scalar(
                        sa.select(attribution_table.c.attribution_id).where(
                            attribution_table.c.idempotency_key == idempotency_key
                        )
                    )
                    is not None
                ):
                    raise PerformanceAttributionError("ATTRIBUTION_IDEMPOTENCY_CONFLICT")
                connection.execute(
                    attribution_table.insert().values(
                        attribution_id=attribution.attribution_id,
                        policy_id=attribution.policy_id,
                        source_id=attribution.source_id,
                        source_hash=attribution.source_hash,
                        venue=attribution.venue.value,
                        evidence_class=attribution.evidence_class.value,
                        strategy_or_method_id=attribution.strategy_or_method_id,
                        materialized_at=attribution.materialized_at,
                        idempotency_key=idempotency_key,
                        payload=_payload(attribution),
                        canonical_hash=attribution.canonical_hash,
                        authority="NONE",
                        live_authority="NONE",
                    )
                )
                for component in components:
                    connection.execute(
                        component_table.insert().values(
                            component_id=component.component_id,
                            attribution_id=component.attribution_id,
                            category=component.category.value,
                            payload=_payload(component),
                            canonical_hash=component.canonical_hash,
                            authority="NONE",
                            live_authority="NONE",
                        )
                    )
            return False
        except IntegrityError as error:
            raise PerformanceAttributionError("ATTRIBUTION_PERSISTENCE_CONFLICT") from error

    def save_health(self, *, idempotency_key: str, assessment: StrategyHealthAssessment) -> bool:
        return self._save_evidence(
            health_table,
            "health_id",
            assessment.health_id,
            idempotency_key,
            assessment,
            policy_id=assessment.policy_id,
            strategy_or_method_id=assessment.strategy_or_method_id,
            strategy_or_method_version=assessment.strategy_or_method_version,
            evidence_class=assessment.evidence_class.value,
            window_end=assessment.window_end,
            state=assessment.state.value,
        )

    def save_summary(self, *, idempotency_key: str, summary: PaperPerformanceSummary) -> bool:
        table = venue_summary_table if summary.scope_type == "VENUE" else portfolio_summary_table
        return self._save_evidence(
            table,
            "summary_id",
            summary.summary_id,
            idempotency_key,
            summary,
            scope_type=summary.scope_type,
            scope_id=summary.scope_id,
            evidence_class=summary.evidence_class.value,
            as_of=summary.as_of,
        )

    def _save_evidence(
        self,
        table: sa.Table,
        identity_column: str,
        identity: str,
        idempotency_key: str,
        value: object,
        **columns: object,
    ) -> bool:
        canonical_hash = str(cast(Any, value).canonical_hash)
        with self._engine.begin() as connection:
            self._ensure_policies(connection)
            old = connection.execute(
                sa.select(table.c.canonical_hash, table.c.idempotency_key).where(
                    table.c[identity_column] == identity
                )
            ).one_or_none()
            if old is not None:
                if old.canonical_hash != canonical_hash or old.idempotency_key != idempotency_key:
                    raise PerformanceAttributionError("ATTRIBUTION_DERIVATIVE_IDENTITY_CONFLICT")
                return True
            if (
                connection.scalar(
                    sa.select(table.c[identity_column]).where(
                        table.c.idempotency_key == idempotency_key
                    )
                )
                is not None
            ):
                raise PerformanceAttributionError("ATTRIBUTION_DERIVATIVE_IDEMPOTENCY_CONFLICT")
            connection.execute(
                table.insert().values(
                    **{identity_column: identity},
                    **columns,
                    idempotency_key=idempotency_key,
                    payload=_payload(value),
                    canonical_hash=canonical_hash,
                    authority="NONE",
                    live_authority="NONE",
                )
            )
        return False

    def workspace(self) -> dict[str, object]:
        evidence_class = PaperEvidenceClass.GENUINE_PAPER
        retained_attributions = self.attributions(evidence_class)
        current_attributions: dict[str, SharedPerformanceAttribution] = {}
        for attribution_item in retained_attributions:
            current_attributions[attribution_item.source_id] = attribution_item
        attributions = tuple(current_attributions[key] for key in sorted(current_attributions))
        retained_health = self.health_assessments(evidence_class)
        current_health: dict[tuple[str, str], StrategyHealthAssessment] = {}
        for health_item in retained_health:
            current_health[
                (health_item.strategy_or_method_id, health_item.strategy_or_method_version)
            ] = health_item
        health = tuple(current_health[key] for key in sorted(current_health))
        return {
            "policy": StrategyHealthPolicy.v1(),
            "attributions": attributions,
            "health": health,
            "method_summaries": self.method_summaries(evidence_class),
            "venue_summaries": tuple(
                filter(None, (self.venue_summary(venue, evidence_class) for venue in PaperVenue))
            ),
            "portfolio_summary": self.portfolio_summary(evidence_class),
            "paper_evidence_counts": {
                "attributions": len(attributions),
                "fills": sum(bool(item.fill_ids) for item in attributions),
                "settled": sum(item.settlement_id is not None for item in attributions),
                "reconciliation_issues": sum(
                    bool(item.reconciliation_issues) for item in attributions
                ),
            },
            "warning": "PAPER PERFORMANCE IS NOT REAL EARNINGS",
            "authority": "NONE",
            "live_authority": "NONE",
        }
