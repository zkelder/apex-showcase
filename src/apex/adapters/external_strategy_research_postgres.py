"""Immutable PostgreSQL evidence for External Strategy Evaluation v1.

Two append-only tables, never mutated, never deleted:
``external_strategy_definition`` (one row per genuinely distinct, versioned
``StrategyDefinition``) and ``external_strategy_evaluation`` (one row per
genuinely distinct ``domain.external_strategy.ExternalStrategyEvaluationRecord``).
Implements ``application.external_strategy_research``'s two repository
Protocols. No provider client, credential, or network capability lives
here, and no row this module writes ever carries an ``authority`` other
than ``'NONE'``.

Nested dataclass content (``StrategyDefinition.canonical_payload``, and a
hand-built ``{"candidate": ..., "benchmark_comparisons": ...}`` payload for
an evaluation) is stored verbatim as canonical JSON text -- mirroring
``kalshi_baseline_quote_postgres.py``'s own ``_payload()``/``canonical_json_
bytes`` idiom exactly, reused here rather than re-derived. A definition row
is reconstructed by replaying the stored fields back through
``StrategyDefinition.create`` itself (not by hand-assembling the frozen
dataclass) and asserting the recomputed ``canonical_hash``/``strategy_id``
still match what was stored -- the identical construction logic that
produced the row in the first place, so a round-trip bug becomes a loud
assertion failure here rather than a silently corrupted read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.application.external_strategy_research import ExternalStrategyResearchError
from apex.domain.external_strategy import (
    ExternalStrategyEvaluationRecord,
    ExternalStrategyQualification,
)
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_definition import (
    StrategyDefinition,
    StrategyEvidenceRequirements,
    StrategyHorizonKind,
    StrategyLifecycleState,
    StrategyProvenance,
    StrategyRuleKind,
    StrategyRuleParameters,
    StrategySourceKind,
)
from apex.domain.strategy_evaluation import (
    ComparisonVerdict,
    StrategyComparison,
    StrategyEvaluationKind,
    StrategyEvaluationResult,
)
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()

external_strategy_definition_table = sa.Table(
    "external_strategy_definition",
    metadata,
    sa.Column("strategy_id", sa.String(300), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("parent_strategy_id", sa.String(300), nullable=True),
    sa.Column("source_kind", sa.String(16), nullable=False),
    sa.Column("display_name", sa.String(200), nullable=False),
    sa.Column("venue", sa.String(20), nullable=False),
    sa.Column("rule_kind", sa.String(40), nullable=False),
    sa.Column("lifecycle_state", sa.String(20), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
)

external_strategy_evaluation_table = sa.Table(
    "external_strategy_evaluation",
    metadata,
    sa.Column("evaluation_id", sa.String(300), primary_key=True),
    sa.Column("strategy_id", sa.String(300), nullable=False),
    sa.Column("strategy_version", sa.Integer, nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("capture_id", sa.String(300), nullable=False),
    sa.Column("evaluation_policy_version", sa.String(60), nullable=False),
    sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("qualification", sa.String(50), nullable=False),
    sa.Column("sample_size", sa.Integer, nullable=False),
    sa.Column("net_return", sa.Numeric, nullable=True),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    sa.ForeignKeyConstraint(
        ["strategy_id", "strategy_version"],
        ["external_strategy_definition.strategy_id", "external_strategy_definition.version"],
    ),
)

EXTERNAL_STRATEGY_RESEARCH_TABLES = ("external_strategy_definition", "external_strategy_evaluation")


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _int_value(value: object) -> int:
    # Deliberately rejects bool (a bool IS an int in Python, but never a
    # genuine field value here) -- matches domain.external_strategy's own
    # ``_parse_rule_parameters`` int-field discipline.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExternalStrategyResearchError("EXTERNAL_STRATEGY_PAYLOAD_FIELD_NOT_INT")
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else _int_value(value)


def _str_value(value: object) -> str:
    if not isinstance(value, str):
        raise ExternalStrategyResearchError("EXTERNAL_STRATEGY_PAYLOAD_FIELD_NOT_STR")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ExternalStrategyResearchError("EXTERNAL_STRATEGY_PAYLOAD_FIELD_NOT_LIST")
    return tuple(_str_value(item) for item in value)


def _rule_parameters(raw: Mapping[str, object]) -> StrategyRuleParameters:
    def _decimal(name: str) -> Decimal | None:
        return _optional_decimal(raw.get(name))

    return StrategyRuleParameters(
        fast_window_days=_optional_int(raw.get("fast_window_days")),
        slow_window_days=_optional_int(raw.get("slow_window_days")),
        momentum_lookback_days=_optional_int(raw.get("momentum_lookback_days")),
        momentum_entry_threshold=_decimal("momentum_entry_threshold"),
        reversion_lookback_days=_optional_int(raw.get("reversion_lookback_days")),
        reversion_entry_threshold=_decimal("reversion_entry_threshold"),
        maximum_holding_days=_optional_int(raw.get("maximum_holding_days")),
        referenced_internal_method_id=(
            None
            if raw.get("referenced_internal_method_id") is None
            else _str_value(raw["referenced_internal_method_id"])
        ),
    )


def _evidence_requirements(raw: Mapping[str, object]) -> StrategyEvidenceRequirements:
    return StrategyEvidenceRequirements(
        minimum_history_days=_int_value(raw["minimum_history_days"]),
        requires_contemporaneous_observation=bool(raw["requires_contemporaneous_observation"]),
        maximum_observation_staleness_seconds=_optional_int(
            raw.get("maximum_observation_staleness_seconds")
        ),
    )


def _provenance(raw: Mapping[str, object]) -> StrategyProvenance:
    return StrategyProvenance(
        reference_kind=str(raw["reference_kind"]),
        reference_description=str(raw["reference_description"]),
        origin_note=str(raw["origin_note"]),
    )


def _definition_from_row(row: RowMapping) -> StrategyDefinition:
    payload = json.loads(str(row["payload"]))
    reconstructed = StrategyDefinition.create(
        source_kind=StrategySourceKind(payload["source_kind"]),
        display_name=str(payload["display_name"]),
        venue=SharedVenue(payload["venue"]),
        instrument_ids=tuple(str(item) for item in payload["instrument_ids"]),
        horizon_kind=StrategyHorizonKind(payload["horizon_kind"]),
        rule_kind=StrategyRuleKind(payload["rule_kind"]),
        rule_parameters=_rule_parameters(payload["rule_parameters"]),
        evidence_requirements=_evidence_requirements(payload["evidence_requirements"]),
        provenance=_provenance(payload["provenance"]),
        created_at=_time(row["created_at"]),
        version=int(row["version"]),
        parent_strategy_id=(
            None if row["parent_strategy_id"] is None else str(row["parent_strategy_id"])
        ),
        lifecycle_state=StrategyLifecycleState(payload["lifecycle_state"]),
    )
    if reconstructed.canonical_hash != str(
        row["canonical_hash"]
    ) or reconstructed.strategy_id != str(row["strategy_id"]):
        # Defense in depth only: StrategyDefinition.create is deterministic
        # and this exact payload shape is what wrote the row -- a mismatch
        # here can only mean a genuine round-trip/serialization bug, never
        # legitimate data drift on an immutable, trigger-protected table.
        raise ExternalStrategyResearchError("EXTERNAL_STRATEGY_DEFINITION_ROUND_TRIP_MISMATCH")
    return reconstructed


def _evaluation_result_from_dict(d: Mapping[str, object]) -> StrategyEvaluationResult:
    return StrategyEvaluationResult(
        strategy_id=_str_value(d["strategy_id"]),
        strategy_version=_int_value(d["strategy_version"]),
        source_kind=StrategySourceKind(_str_value(d["source_kind"])),
        evaluation_kind=StrategyEvaluationKind(_str_value(d["evaluation_kind"])),
        benchmark_identity=_str_value(d["benchmark_identity"]),
        window_start=_optional_time(d["window_start"]),
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


def _comparison_from_dict(d: Mapping[str, object]) -> StrategyComparison:
    return StrategyComparison(
        candidate_strategy_id=_str_value(d["candidate_strategy_id"]),
        candidate_version=_int_value(d["candidate_version"]),
        candidate_evaluation_kind=StrategyEvaluationKind(
            _str_value(d["candidate_evaluation_kind"])
        ),
        benchmark_strategy_id=_str_value(d["benchmark_strategy_id"]),
        benchmark_version=_int_value(d["benchmark_version"]),
        benchmark_evaluation_kind=StrategyEvaluationKind(
            _str_value(d["benchmark_evaluation_kind"])
        ),
        net_return_delta=_optional_decimal(d["net_return_delta"]),
        win_rate_delta=_optional_decimal(d["win_rate_delta"]),
        maximum_drawdown_delta=_optional_decimal(d["maximum_drawdown_delta"]),
        comparability_notes=_strings(d["comparability_notes"]),
        verdict=ComparisonVerdict(_str_value(d["verdict"])),
        compared_at=_time(d["compared_at"]),
        canonical_hash=_str_value(d["canonical_hash"]),
    )


def _evaluation_from_row(row: RowMapping) -> ExternalStrategyEvaluationRecord:
    payload = json.loads(str(row["payload"]))
    candidate = _evaluation_result_from_dict(payload["candidate"])
    comparisons = tuple(_comparison_from_dict(item) for item in payload["benchmark_comparisons"])
    return ExternalStrategyEvaluationRecord(
        evaluation_id=str(row["evaluation_id"]),
        strategy_id=str(row["strategy_id"]),
        strategy_version=int(row["strategy_version"]),
        as_of=_time(row["as_of"]),
        capture_id=str(row["capture_id"]),
        evaluation_policy_version=str(row["evaluation_policy_version"]),
        evaluated_at=_time(row["evaluated_at"]),
        candidate=candidate,
        benchmark_comparisons=comparisons,
        qualification=ExternalStrategyQualification(row["qualification"]),
        canonical_hash=str(row["canonical_hash"]),
        authority=str(row["authority"]),
    )


class PostgresExternalStrategyDefinitionRepository:
    """Implements ``application.external_strategy_research.
    ExternalStrategyDefinitionRepository`` against real PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(self, *, definition: StrategyDefinition) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = connection.execute(
                    sa.select(external_strategy_definition_table.c.canonical_hash).where(
                        external_strategy_definition_table.c.strategy_id == definition.strategy_id,
                        external_strategy_definition_table.c.version == definition.version,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != definition.canonical_hash:
                        raise ExternalStrategyResearchError(
                            "EXTERNAL_STRATEGY_DEFINITION_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    external_strategy_definition_table.insert().values(
                        strategy_id=definition.strategy_id,
                        version=definition.version,
                        parent_strategy_id=definition.parent_strategy_id,
                        source_kind=definition.source_kind.value,
                        display_name=definition.display_name,
                        venue=definition.venue.value,
                        rule_kind=definition.rule_kind.value,
                        lifecycle_state=definition.lifecycle_state.value,
                        canonical_hash=definition.canonical_hash,
                        payload=_payload(definition.canonical_payload),
                        created_at=definition.created_at,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise ExternalStrategyResearchError(
                "EXTERNAL_STRATEGY_DEFINITION_PERSISTENCE_CONFLICT"
            ) from error

    def get(self, *, strategy_id: str, version: int) -> StrategyDefinition | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(external_strategy_definition_table).where(
                        external_strategy_definition_table.c.strategy_id == strategy_id,
                        external_strategy_definition_table.c.version == version,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _definition_from_row(row)

    def versions(self, *, strategy_id: str) -> tuple[StrategyDefinition, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(external_strategy_definition_table)
                .where(external_strategy_definition_table.c.strategy_id == strategy_id)
                .order_by(external_strategy_definition_table.c.version)
            ).mappings()
            return tuple(_definition_from_row(row) for row in rows)

    def latest_version(self, *, strategy_id: str) -> StrategyDefinition | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(external_strategy_definition_table)
                    .where(external_strategy_definition_table.c.strategy_id == strategy_id)
                    .order_by(external_strategy_definition_table.c.version.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _definition_from_row(row)

    def all_root_strategies(self) -> tuple[StrategyDefinition, ...]:
        latest_per_strategy = (
            sa.select(
                external_strategy_definition_table.c.strategy_id,
                external_strategy_definition_table.c.version,
            )
            .distinct(external_strategy_definition_table.c.strategy_id)
            .order_by(
                external_strategy_definition_table.c.strategy_id,
                external_strategy_definition_table.c.version.desc(),
            )
            .subquery()
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(external_strategy_definition_table)
                .join(
                    latest_per_strategy,
                    sa.and_(
                        external_strategy_definition_table.c.strategy_id
                        == latest_per_strategy.c.strategy_id,
                        external_strategy_definition_table.c.version
                        == latest_per_strategy.c.version,
                    ),
                )
                .order_by(external_strategy_definition_table.c.created_at)
            ).mappings()
            return tuple(_definition_from_row(row) for row in rows)


class PostgresExternalStrategyEvaluationRepository:
    """Implements ``application.external_strategy_research.
    ExternalStrategyEvaluationRepository`` against real PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(self, *, record: ExternalStrategyEvaluationRecord) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = connection.execute(
                    sa.select(external_strategy_evaluation_table.c.canonical_hash).where(
                        external_strategy_evaluation_table.c.evaluation_id == record.evaluation_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != record.canonical_hash:
                        raise ExternalStrategyResearchError(
                            "EXTERNAL_STRATEGY_EVALUATION_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    external_strategy_evaluation_table.insert().values(
                        evaluation_id=record.evaluation_id,
                        strategy_id=record.strategy_id,
                        strategy_version=record.strategy_version,
                        as_of=record.as_of,
                        capture_id=record.capture_id,
                        evaluation_policy_version=record.evaluation_policy_version,
                        evaluated_at=record.evaluated_at,
                        qualification=record.qualification.value,
                        sample_size=record.candidate.sample_size,
                        net_return=record.candidate.net_return,
                        canonical_hash=record.canonical_hash,
                        payload=_payload(
                            {
                                "candidate": record.candidate,
                                "benchmark_comparisons": record.benchmark_comparisons,
                            }
                        ),
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise ExternalStrategyResearchError(
                "EXTERNAL_STRATEGY_EVALUATION_PERSISTENCE_CONFLICT"
            ) from error

    def get(self, *, evaluation_id: str) -> ExternalStrategyEvaluationRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(external_strategy_evaluation_table).where(
                        external_strategy_evaluation_table.c.evaluation_id == evaluation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _evaluation_from_row(row)

    def for_strategy(self, *, strategy_id: str) -> tuple[ExternalStrategyEvaluationRecord, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(external_strategy_evaluation_table)
                .where(external_strategy_evaluation_table.c.strategy_id == strategy_id)
                .order_by(
                    external_strategy_evaluation_table.c.evaluated_at.desc(),
                    external_strategy_evaluation_table.c.evaluation_id.desc(),
                )
            ).mappings()
            return tuple(_evaluation_from_row(row) for row in rows)

    def latest_for_strategy_version(
        self, *, strategy_id: str, version: int
    ) -> ExternalStrategyEvaluationRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(external_strategy_evaluation_table)
                    .where(
                        external_strategy_evaluation_table.c.strategy_id == strategy_id,
                        external_strategy_evaluation_table.c.strategy_version == version,
                    )
                    .order_by(
                        external_strategy_evaluation_table.c.evaluated_at.desc(),
                        external_strategy_evaluation_table.c.evaluation_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _evaluation_from_row(row)
