"""Retained-evidence adapter for the initial Event Markets research vertical.

It only reads Apex's existing PostgreSQL evidence.  No network client is
constructed here, and no output is connected to strategy or financial state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    normalized_market_snapshot,
    public_outcome_fact,
    shadow_prediction,
)
from apex.application.strategy_lab import (
    StrategyLabFeatureAdapter,
    StrategyLabFeatureDefinition,
    StrategyLabFeatureSchema,
    StrategyLabLabelDefinition,
    StrategyLabRow,
)
from apex.application.strategy_lab_shadow import (
    StrategyLabForwardFeatureAdapter,
    StrategyLabForwardPrediction,
    StrategyLabForwardRow,
    StrategyLabResolvedOutcome,
)
from apex.domain.values import content_digest, parse_decimal


@dataclass(frozen=True, slots=True)
class EventMarketsReadiness:
    total_candidate_source_rows: int
    resolved_rows: int
    configuration_complete_rows: int
    distinct_events: int
    distinct_markets: int
    rows_with_all_required_features: int
    exclusions: dict[str, int]
    integrity_warnings: tuple[str, ...]


class EventMarketsStrategyLabAdapter(StrategyLabFeatureAdapter):
    """Maps exact resolved Shadow predictions to provider-neutral ML rows.

    ``source_scope`` is an optional exact strategy configuration hash.  Without
    it, all rows are emitted and the application guard rejects mixed
    configurations rather than silently blending different strategies.
    """

    adapter_id = "event-market-v1"
    adapter_version = "1"
    schema = StrategyLabFeatureSchema(
        schema_id="event-market-v1-feature-schema-v1",
        vertical="EVENT_MARKETS",
        research_family="EVENT_MARKET_RESOLVED_PROBABILITY",
        version="V1",
        features=(
            StrategyLabFeatureDefinition(
                "market_consensus_probability", "DECIMAL", False, "IDENTITY", "DECISION_TIME"
            ),
            StrategyLabFeatureDefinition(
                "expected_fee", "DECIMAL", False, "IDENTITY", "DECISION_TIME"
            ),
            StrategyLabFeatureDefinition(
                "slippage_reserve", "DECIMAL", False, "IDENTITY", "DECISION_TIME"
            ),
            StrategyLabFeatureDefinition(
                "execution_uncertainty_reserve", "DECIMAL", False, "IDENTITY", "DECISION_TIME"
            ),
        ),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    label_definition = StrategyLabLabelDefinition(
        label_id="event-market-v1-resolved-binary-label-v1",
        vertical="EVENT_MARKETS",
        research_family="EVENT_MARKET_RESOLVED_PROBABILITY",
        version="V1",
        target_type="BINARY_PROBABILITY",
        positive_semantics="exact provider-resolved outcome present in prediction outcome set",
        negative_semantics="the other exact binary outcome",
        resolution_rule="exact retained public outcome fact matches normalized provider market",
        feature_cutoff_rule="prediction decision_at is the feature cutoff",
        availability_rule="public outcome fact available_at is the first retained label timestamp",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    def __init__(
        self,
        engine: Engine,
        *,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
    ) -> None:
        self._engine = engine
        self._strategy_id = strategy_id
        self._strategy_version = strategy_version
        self._readiness = EventMarketsReadiness(0, 0, 0, 0, 0, 0, {}, ())

    @property
    def readiness(self) -> EventMarketsReadiness:
        return self._readiness

    def source_identity(self, *, cutoff_at: datetime, source_scope: str | None = None) -> str:
        return content_digest(
            {
                "adapter": self.adapter_id,
                "version": self.adapter_version,
                "cutoff": cutoff_at,
                "configuration_scope": source_scope,
                "strategy_id": self._strategy_id,
                "strategy_version": self._strategy_version,
                "source": "retained-shadow-predictions-and-public-outcome-facts",
            }
        )

    def rows(
        self, *, cutoff_at: datetime, source_scope: str | None = None
    ) -> tuple[StrategyLabRow, ...]:
        if cutoff_at.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        candidate_statement = sa.select(
            shadow_prediction.c.prediction_id,
            shadow_prediction.c.state,
        ).where(shadow_prediction.c.decision_at <= cutoff_at)
        statement = (
            sa.select(
                shadow_prediction,
                normalized_market_snapshot.c.provider_market_ticker,
                public_outcome_fact.c.available_at.label("label_available_at"),
                public_outcome_fact.c.resolved_outcome.label("public_resolved_outcome"),
                public_outcome_fact.c.canonical_fact_hash.label("outcome_lineage_hash"),
            )
            .join(
                normalized_market_snapshot,
                shadow_prediction.c.market_evidence_id == normalized_market_snapshot.c.snapshot_id,
            )
            .join(
                public_outcome_fact,
                sa.and_(
                    public_outcome_fact.c.market_ticker
                    == normalized_market_snapshot.c.provider_market_ticker,
                    public_outcome_fact.c.state == "RESOLVED",
                ),
            )
            .where(
                shadow_prediction.c.state == "RESOLVED",
                shadow_prediction.c.decision_at <= cutoff_at,
                public_outcome_fact.c.available_at > shadow_prediction.c.decision_at,
                public_outcome_fact.c.available_at <= cutoff_at,
            )
            .order_by(
                shadow_prediction.c.decision_at,
                shadow_prediction.c.event_id,
                shadow_prediction.c.instrument_id,
                shadow_prediction.c.prediction_id,
                public_outcome_fact.c.available_at,
            )
        )
        if self._strategy_id is not None:
            candidate_statement = candidate_statement.where(
                shadow_prediction.c.strategy_id == self._strategy_id
            )
            statement = statement.where(shadow_prediction.c.strategy_id == self._strategy_id)
        if self._strategy_version is not None:
            candidate_statement = candidate_statement.where(
                shadow_prediction.c.strategy_version == self._strategy_version
            )
            statement = statement.where(
                shadow_prediction.c.strategy_version == self._strategy_version
            )
        with self._engine.connect() as connection:
            candidates = tuple(connection.execute(candidate_statement).mappings())
            values = tuple(connection.execute(statement).mappings())
        excluded: dict[str, int] = {}
        resolved_prediction_ids = {str(item["prediction_id"]) for item in values}
        for candidate in candidates:
            if str(candidate["prediction_id"]) not in resolved_prediction_ids:
                _exclude(excluded, "UNRESOLVED_OUTCOME")
        rows_by_prediction: dict[str, StrategyLabRow] = {}
        contradictory_prediction_ids: set[str] = set()
        all_events: set[str] = set()
        all_markets: set[str] = set()
        for item in values:
            if item["event_id"] is not None:
                all_events.add(str(item["event_id"]))
            if item["provider_market_ticker"] is not None:
                all_markets.add(str(item["provider_market_ticker"]))
            event_id = item["event_id"]
            if event_id is None:
                _exclude(excluded, "MISSING_INDEPENDENT_GROUPING_IDENTITY")
                continue
            configuration_hash = content_digest(
                {
                    "strategy_id": item["strategy_id"],
                    "strategy_version": item["strategy_version"],
                    "model_configuration_version": item["model_configuration_version"],
                }
            )
            if source_scope is not None and source_scope != configuration_hash:
                _exclude(excluded, "PLAN_CONFIGURATION_MISMATCH")
                continue
            try:
                outcomes = _probabilities(str(item["market_probabilities_payload"]))
                resolved = str(item["public_resolved_outcome"])
                if len(outcomes) != 2 or resolved not in outcomes:
                    _exclude(excluded, "MISSING_OR_CONTRADICTORY_OUTCOME_IDENTITY")
                    continue
                baseline = outcomes[resolved]
                if baseline < 0 or baseline > 1:
                    _exclude(excluded, "INVALID_BASELINE_PROBABILITY")
                    continue
            except (KeyError, TypeError, ValueError):
                _exclude(excluded, "MISSING_REQUIRED_FEATURE_OR_LINEAGE")
                continue
            try:
                expected_fee = _required_decimal(item["expected_fee"])
            except (KeyError, TypeError, ValueError):
                _exclude(excluded, "MISSING_COST_EVIDENCE")
                continue
            try:
                feature_values = (
                    baseline,
                    expected_fee,
                    _required_decimal(item["slippage_reserve"]),
                    _required_decimal(item["execution_uncertainty_reserve"]),
                )
            except (KeyError, TypeError, ValueError):
                _exclude(excluded, "MISSING_REQUIRED_FEATURE_OR_LINEAGE")
                continue
            label = 1 if resolved == sorted(outcomes)[0] else 0
            row = StrategyLabRow(
                row_id=str(item["prediction_id"]),
                entity_id=str(item["instrument_id"]),
                grouping_id=str(event_id),
                feature_at=item["decision_at"],
                label_available_at=item["label_available_at"],
                feature_values=feature_values,
                label=label,
                baseline_probability=baseline,
                entry_price=None,
                fee_cost=feature_values[1],
                source_lineage_hash=content_digest(
                    {
                        "prediction": item["canonical_prediction_hash"],
                        "outcome": item["outcome_lineage_hash"],
                    }
                ),
                source_configuration_hash=configuration_hash,
            )
            prior = rows_by_prediction.get(row.row_id)
            if prior is not None:
                if prior.label != row.label:
                    rows_by_prediction.pop(row.row_id, None)
                    contradictory_prediction_ids.add(row.row_id)
                    _exclude(excluded, "CONTRADICTORY_OUTCOME_IDENTITY")
                continue
            if row.row_id not in contradictory_prediction_ids:
                rows_by_prediction[row.row_id] = row
        rows = tuple(rows_by_prediction.values())
        self._readiness = EventMarketsReadiness(
            total_candidate_source_rows=len(candidates),
            resolved_rows=len(resolved_prediction_ids),
            configuration_complete_rows=len(rows),
            distinct_events=len(all_events),
            distinct_markets=len(all_markets),
            rows_with_all_required_features=len(rows),
            exclusions=dict(sorted(excluded.items())),
            integrity_warnings=("MIXED_CONFIGURATION_REJECTED_BY_DATASET_GUARD",)
            if len({row.source_configuration_hash for row in rows}) > 1
            else (),
        )
        return rows


class EventMarketsForwardFeatureAdapter(StrategyLabForwardFeatureAdapter):
    """Forward-only adapter over already-retained Event Markets evidence.

    This never constructs a provider client.  Rows are retained only when the
    original shadow prediction was unresolved at the requested cutoff and its
    exact market/event lineage and configuration are complete.
    """

    adapter_id = "event-market-retained-v1"
    adapter_version = "1"
    feature_schema_hash = EventMarketsStrategyLabAdapter.schema.canonical_hash

    def __init__(self, engine: Engine, *, configuration_hash: str | None = None) -> None:
        self._engine = engine
        self._configuration_hash = configuration_hash

    def source_identity(self, *, cutoff_at: datetime) -> str:
        return content_digest(
            {
                "adapter": self.adapter_id,
                "version": self.adapter_version,
                "cutoff": cutoff_at,
                "configuration_hash": self._configuration_hash,
                "source": "retained-unresolved-shadow-predictions",
            }
        )

    def rows(self, *, cutoff_at: datetime, maximum_rows: int) -> tuple[StrategyLabForwardRow, ...]:
        if cutoff_at.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        statement = (
            sa.select(shadow_prediction, normalized_market_snapshot.c.provider_market_ticker)
            .join(
                normalized_market_snapshot,
                shadow_prediction.c.market_evidence_id == normalized_market_snapshot.c.snapshot_id,
            )
            .where(
                shadow_prediction.c.state == "UNRESOLVED",
                shadow_prediction.c.decision_at <= cutoff_at,
                shadow_prediction.c.resolution_at > cutoff_at,
            )
            .order_by(
                shadow_prediction.c.decision_at,
                shadow_prediction.c.event_id,
                shadow_prediction.c.instrument_id,
                shadow_prediction.c.prediction_id,
            )
            .limit(maximum_rows + 1)
        )
        with self._engine.connect() as connection:
            values = tuple(connection.execute(statement).mappings())
        result: list[StrategyLabForwardRow] = []
        identities: set[tuple[str, datetime]] = set()
        for item in values[:maximum_rows]:
            event_id = item["event_id"]
            if event_id is None:
                continue
            config = content_digest(
                {
                    "strategy_id": item["strategy_id"],
                    "strategy_version": item["strategy_version"],
                    "model_configuration_version": item["model_configuration_version"],
                }
            )
            if self._configuration_hash is not None and config != self._configuration_hash:
                continue
            try:
                probabilities = _probabilities(str(item["market_probabilities_payload"]))
                if len(probabilities) != 2:
                    continue
                positive = sorted(probabilities)[0]
                baseline = probabilities[positive]
                values_for_row = (
                    baseline,
                    _required_decimal(item["expected_fee"]),
                    _required_decimal(item["slippage_reserve"]),
                    _required_decimal(item["execution_uncertainty_reserve"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            identity = (str(item["provider_market_ticker"]), item["decision_at"])
            if identity in identities:
                raise ValueError("duplicate retained forward source row")
            identities.add(identity)
            result.append(
                StrategyLabForwardRow(
                    row_id=str(item["prediction_id"]),
                    entity_id=identity[0],
                    grouping_id=str(event_id),
                    feature_at=item["decision_at"],
                    feature_values=values_for_row,
                    baseline_probability=baseline,
                    positive_outcome_identity=positive,
                    source_lineage_hash=content_digest(
                        {
                            "shadow_prediction": item["canonical_prediction_hash"],
                            "market": item["market_evidence_id"],
                        }
                    ),
                    source_configuration_hash=config,
                )
            )
        return tuple(result)

    def resolve(
        self, prediction: StrategyLabForwardPrediction, *, cutoff_at: datetime
    ) -> StrategyLabResolvedOutcome | None:
        statement = (
            sa.select(public_outcome_fact)
            .join(
                normalized_market_snapshot,
                normalized_market_snapshot.c.provider_market_ticker
                == public_outcome_fact.c.market_ticker,
            )
            .where(
                normalized_market_snapshot.c.provider_market_ticker == prediction.entity_id,
                public_outcome_fact.c.state == "RESOLVED",
                public_outcome_fact.c.available_at > prediction.feature_at,
                public_outcome_fact.c.available_at <= cutoff_at,
            )
            .order_by(public_outcome_fact.c.available_at, public_outcome_fact.c.outcome_fact_id)
        )
        with self._engine.connect() as connection:
            rows = tuple(connection.execute(statement).mappings())
        outcomes = {str(row["resolved_outcome"]) for row in rows}
        if len(outcomes) != 1:
            return None
        row = rows[0]
        return StrategyLabResolvedOutcome(
            str(row["outcome_fact_id"]),
            row["available_at"],
            str(row["resolved_outcome"]) == prediction.positive_outcome_identity,
        )


def _probabilities(payload: str) -> dict[str, Decimal]:
    decoded = json.loads(payload)
    if isinstance(decoded, dict):
        return {str(key): parse_decimal(value) for key, value in decoded.items()}
    if not isinstance(decoded, list):
        raise ValueError("probability payload is not a mapping or ordered outcome list")
    result: dict[str, Decimal] = {}
    for item in decoded:
        if not isinstance(item, dict) or set(item) != {"outcome_id", "probability"}:
            raise ValueError("probability payload item is invalid")
        outcome_id = str(item["outcome_id"])
        if not outcome_id or outcome_id in result:
            raise ValueError("probability payload outcomes are invalid")
        result[outcome_id] = parse_decimal(item["probability"])
    return result


def _required_decimal(value: object) -> Decimal:
    if value is None:
        raise ValueError("missing decimal")
    if isinstance(value, Decimal):
        value = Decimal() if value == 0 else value.normalize()
    if not isinstance(value, (str, int, Decimal)):
        raise ValueError("decimal evidence has an unsupported type")
    return parse_decimal(value, allow_negative=False)


def _exclude(values: dict[str, int], reason: str) -> None:
    values[reason] = values.get(reason, 0) + 1
