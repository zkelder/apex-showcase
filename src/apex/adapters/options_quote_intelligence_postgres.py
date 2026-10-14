"""PostgreSQL repository for genuine options quote-chain and volatility
intelligence.

Follows the ``payload``-column pattern ``apex.adapters.research_evidence_postgres``
established and ``apex.adapters.options_intelligence_postgres`` already
uses for this same domain family: one canonical JSON payload column per
row (restored exactly via ``canonical_rehydrate.rehydrate``) plus a small
set of indexed columns for the query shapes the application layer actually
needs. Every table carries ``authority = 'NONE'``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.options_quote_intelligence import (
    OptionChainCapturePlan,
    OptionChainCaptureRun,
    OptionQuoteObservation,
    OptionsChainSnapshot,
    OptionsQuoteProviderCapabilitySnapshot,
    OptionsVolatilityResearchFeatures,
)
from apex.domain.values import canonical_json_bytes

OPTIONS_QUOTE_INTELLIGENCE_TABLES = (
    "options_quote_capability",
    "options_chain_capture_plan",
    "options_chain_capture_run",
    "options_quote_observation",
    "options_chain_snapshot",
    "options_volatility_research_feature",
)
OPTIONS_QUOTE_INTELLIGENCE_IMMUTABLE_TABLES = OPTIONS_QUOTE_INTELLIGENCE_TABLES

metadata = sa.MetaData()

options_quote_capability = sa.Table(
    "options_quote_capability",
    metadata,
    sa.Column("canonical_hash", sa.String(64), primary_key=True),
    sa.Column("provider", sa.String(80), nullable=False),
    sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("credentials_available", sa.Boolean(), nullable=False),
    sa.Column("requests_made", sa.Integer(), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_chain_capture_plan = sa.Table(
    "options_chain_capture_plan",
    metadata,
    sa.Column("plan_id", sa.String(160), primary_key=True),
    sa.Column("cohort_policy_id", sa.String(160), nullable=False),
    sa.Column("provider", sa.String(80), nullable=False),
    sa.Column("feed", sa.String(16), nullable=False),
    sa.Column("member_count", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_chain_capture_run = sa.Table(
    "options_chain_capture_run",
    metadata,
    sa.Column("run_id", sa.String(160), primary_key=True),
    sa.Column(
        "plan_id",
        sa.String(160),
        sa.ForeignKey("options_chain_capture_plan.plan_id"),
        nullable=False,
    ),
    sa.Column("status", sa.String(40), nullable=False),
    sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_quote_observation = sa.Table(
    "options_quote_observation",
    metadata,
    sa.Column("observation_id", sa.String(160), primary_key=True),
    sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
    sa.Column("canonical_contract_id", sa.String(160), nullable=False),
    sa.Column("expiration", sa.String(10), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("quote_timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ingestion_timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_chain_snapshot = sa.Table(
    "options_chain_snapshot",
    metadata,
    sa.Column("snapshot_id", sa.String(160), primary_key=True),
    sa.Column(
        "plan_id",
        sa.String(160),
        sa.ForeignKey("options_chain_capture_plan.plan_id"),
        nullable=False,
    ),
    sa.Column(
        "run_id", sa.String(160), sa.ForeignKey("options_chain_capture_run.run_id"), nullable=False
    ),
    sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
    sa.Column("underlying_symbol", sa.String(16), nullable=False),
    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("data_quality_status", sa.String(20), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_volatility_research_feature = sa.Table(
    "options_volatility_research_feature",
    metadata,
    sa.Column("feature_id", sa.String(160), primary_key=True),
    sa.Column(
        "snapshot_id",
        sa.String(160),
        sa.ForeignKey("options_chain_snapshot.snapshot_id"),
        nullable=False,
    ),
    sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
    sa.Column("as_of", sa.String(10), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
sa.Index(
    "ix_oqi_observation_underlying",
    options_quote_observation.c.underlying_canonical_security_id,
)
sa.Index(
    "ix_oqi_snapshot_underlying_captured",
    options_chain_snapshot.c.underlying_canonical_security_id,
    options_chain_snapshot.c.captured_at,
)
sa.Index(
    "ix_oqi_feature_underlying_as_of",
    options_volatility_research_feature.c.underlying_canonical_security_id,
    options_volatility_research_feature.c.as_of,
)


class OptionsQuoteIntelligencePersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise OptionsQuoteIntelligencePersistenceError("STORED_PAYLOAD_INVALID")
    return value


def _capability(row: RowMapping) -> OptionsQuoteProviderCapabilitySnapshot:
    return rehydrate(OptionsQuoteProviderCapabilitySnapshot, _decoded(row))


def _plan(row: RowMapping) -> OptionChainCapturePlan:
    return rehydrate(OptionChainCapturePlan, _decoded(row))


def _run(row: RowMapping) -> OptionChainCaptureRun:
    return rehydrate(OptionChainCaptureRun, _decoded(row))


def _observation(row: RowMapping) -> OptionQuoteObservation:
    return rehydrate(OptionQuoteObservation, _decoded(row))


def _snapshot(row: RowMapping) -> OptionsChainSnapshot:
    return rehydrate(OptionsChainSnapshot, _decoded(row))


def _features(row: RowMapping) -> OptionsVolatilityResearchFeatures:
    return rehydrate(OptionsVolatilityResearchFeatures, _decoded(row))


class PostgresOptionsQuoteIntelligenceRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- capability ---------------------------------------------------

    def save_capability(
        self, snapshot: OptionsQuoteProviderCapabilitySnapshot
    ) -> OptionsQuoteProviderCapabilitySnapshot:
        with self._engine.begin() as connection:
            exists = connection.execute(
                sa.select(options_quote_capability.c.canonical_hash).where(
                    options_quote_capability.c.canonical_hash == snapshot.canonical_hash
                )
            ).scalar_one_or_none()
            if exists is None:
                connection.execute(
                    options_quote_capability.insert().values(
                        canonical_hash=snapshot.canonical_hash,
                        provider=snapshot.capability.provider,
                        checked_at=snapshot.checked_at,
                        credentials_available=snapshot.credentials_available,
                        requests_made=snapshot.requests_made,
                        payload=_payload(snapshot),
                        authority="NONE",
                    )
                )
        return snapshot

    def latest_capability(self, provider: str) -> OptionsQuoteProviderCapabilitySnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_quote_capability)
                    .where(options_quote_capability.c.provider == provider)
                    .order_by(options_quote_capability.c.checked_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _capability(row) if row is not None else None

    def capabilities(
        self, provider: str | None = None
    ) -> tuple[OptionsQuoteProviderCapabilitySnapshot, ...]:
        statement = sa.select(options_quote_capability).order_by(
            options_quote_capability.c.checked_at
        )
        if provider is not None:
            statement = statement.where(options_quote_capability.c.provider == provider)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings()
            return tuple(_capability(row) for row in rows)

    # -- capture plan ---------------------------------------------------

    def save_plan(self, plan: OptionChainCapturePlan) -> OptionChainCapturePlan:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(options_chain_capture_plan.c.plan_id).where(
                        options_chain_capture_plan.c.plan_id == plan.plan_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        options_chain_capture_plan.insert().values(
                            plan_id=plan.plan_id,
                            cohort_policy_id=plan.cohort_policy_id,
                            provider=plan.provider,
                            feed=plan.feed,
                            member_count=len(plan.members),
                            created_at=plan.created_at,
                            payload=_payload(plan),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsQuoteIntelligencePersistenceError("PLAN_PERSISTENCE_CONFLICT") from error
        return plan

    def plan_by_id(self, plan_id: str) -> OptionChainCapturePlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_chain_capture_plan).where(
                        options_chain_capture_plan.c.plan_id == plan_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _plan(row) if row is not None else None

    def latest_plan(self) -> OptionChainCapturePlan | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_chain_capture_plan)
                    .order_by(options_chain_capture_plan.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _plan(row) if row is not None else None

    def plans(self) -> tuple[OptionChainCapturePlan, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_chain_capture_plan).order_by(
                    options_chain_capture_plan.c.created_at
                )
            ).mappings()
            return tuple(_plan(row) for row in rows)

    # -- capture run ---------------------------------------------------

    def save_run(self, run: OptionChainCaptureRun) -> OptionChainCaptureRun:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(options_chain_capture_run.c.run_id).where(
                        options_chain_capture_run.c.run_id == run.run_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        options_chain_capture_run.insert().values(
                            run_id=run.run_id,
                            plan_id=run.plan_id,
                            status=run.status.value,
                            requested_at=run.requested_at,
                            completed_at=run.completed_at,
                            payload=_payload(run),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsQuoteIntelligencePersistenceError("RUN_PERSISTENCE_CONFLICT") from error
        return run

    def run_by_id(self, run_id: str) -> OptionChainCaptureRun | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_chain_capture_run).where(
                        options_chain_capture_run.c.run_id == run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _run(row) if row is not None else None

    def runs_for_plan(self, plan_id: str) -> tuple[OptionChainCaptureRun, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_chain_capture_run)
                .where(options_chain_capture_run.c.plan_id == plan_id)
                .order_by(options_chain_capture_run.c.requested_at)
            ).mappings()
            return tuple(_run(row) for row in rows)

    # -- observations ---------------------------------------------------

    def save_observations(
        self, observations: tuple[OptionQuoteObservation, ...]
    ) -> tuple[OptionQuoteObservation, ...]:
        if not observations:
            return observations
        with self._engine.begin() as connection:
            ids = tuple({item.observation_id for item in observations})
            existing = set(
                connection.execute(
                    sa.select(options_quote_observation.c.observation_id).where(
                        options_quote_observation.c.observation_id.in_(ids)
                    )
                ).scalars()
            )
            pending: dict[str, dict[str, object]] = {}
            for observation in observations:
                if observation.observation_id in existing:
                    continue
                pending.setdefault(
                    observation.observation_id,
                    {
                        "observation_id": observation.observation_id,
                        "underlying_canonical_security_id": (
                            observation.contract.underlying_canonical_security_id
                        ),
                        "canonical_contract_id": observation.contract.canonical_contract_id,
                        "expiration": observation.contract.expiration,
                        "status": observation.status.value,
                        "quote_timestamp": observation.quote_timestamp,
                        "ingestion_timestamp": observation.ingestion_timestamp,
                        "payload": _payload(observation),
                        "authority": "NONE",
                    },
                )
            rows = list(pending.values())
            if rows:
                connection.execute(options_quote_observation.insert(), rows)
        return observations

    def observation_by_id(self, observation_id: str) -> OptionQuoteObservation | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_quote_observation).where(
                        options_quote_observation.c.observation_id == observation_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _observation(row) if row is not None else None

    def observations_by_ids(
        self, observation_ids: tuple[str, ...]
    ) -> tuple[OptionQuoteObservation, ...]:
        if not observation_ids:
            return ()
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_quote_observation).where(
                    options_quote_observation.c.observation_id.in_(observation_ids)
                )
            ).mappings()
            return tuple(_observation(row) for row in rows)

    # -- snapshot ---------------------------------------------------

    def save_snapshot(self, snapshot: OptionsChainSnapshot) -> OptionsChainSnapshot:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(options_chain_snapshot.c.snapshot_id).where(
                        options_chain_snapshot.c.snapshot_id == snapshot.snapshot_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        options_chain_snapshot.insert().values(
                            snapshot_id=snapshot.snapshot_id,
                            plan_id=snapshot.plan_id,
                            run_id=snapshot.run_id,
                            underlying_canonical_security_id=snapshot.underlying_canonical_security_id,
                            underlying_symbol=snapshot.underlying_symbol,
                            captured_at=snapshot.captured_at,
                            data_quality_status=snapshot.data_quality_status.value,
                            payload=_payload(snapshot),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsQuoteIntelligencePersistenceError(
                "SNAPSHOT_PERSISTENCE_CONFLICT"
            ) from error
        return snapshot

    def snapshot_by_id(self, snapshot_id: str) -> OptionsChainSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_chain_snapshot).where(
                        options_chain_snapshot.c.snapshot_id == snapshot_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _snapshot(row) if row is not None else None

    def snapshots_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionsChainSnapshot, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_chain_snapshot)
                .where(
                    options_chain_snapshot.c.underlying_canonical_security_id
                    == underlying_canonical_security_id
                )
                .order_by(options_chain_snapshot.c.captured_at)
            ).mappings()
            return tuple(_snapshot(row) for row in rows)

    def latest_snapshot_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> OptionsChainSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_chain_snapshot)
                    .where(
                        options_chain_snapshot.c.underlying_canonical_security_id
                        == underlying_canonical_security_id
                    )
                    .order_by(options_chain_snapshot.c.captured_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _snapshot(row) if row is not None else None

    def snapshot_captured_at(self, snapshot_id: str) -> datetime | None:
        """Securities Follow-Up Temporal Integrity v1: one snapshot's exact
        retained ``captured_at`` by id, reading the column only (never the
        heavy payload), or ``None`` if no such snapshot is retained."""

        with self._engine.connect() as connection:
            value = connection.scalar(
                sa.select(options_chain_snapshot.c.captured_at).where(
                    options_chain_snapshot.c.snapshot_id == snapshot_id
                )
            )
        return None if value is None else value.astimezone(UTC)

    def earliest_snapshot_for_underlying_at_or_after(
        self, underlying_canonical_security_id: str, at_or_after: datetime
    ) -> OptionsChainSnapshot | None:
        """Securities Research -> Shared Opportunity Recurring Bridge v1.
        The earliest already-retained snapshot at or after ``at_or_after`` --
        never the nearest-available, never a live provider request -- backing
        the counterfactual follow-up bridge's terminal-price lookup
        (``application.counterfactual_followup.
        SecuritiesUnderlyingPriceSource``). Uses the existing
        ``ix_oqi_snapshot_underlying_captured`` index (same ``(underlying,
        captured_at)`` shape ``latest_snapshot_for_underlying`` above already
        relies on)."""

        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_chain_snapshot)
                    .where(
                        options_chain_snapshot.c.underlying_canonical_security_id
                        == underlying_canonical_security_id,
                        options_chain_snapshot.c.captured_at >= at_or_after,
                    )
                    .order_by(options_chain_snapshot.c.captured_at)
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _snapshot(row) if row is not None else None

    # -- research features ---------------------------------------------------

    def save_research_features(
        self, features: OptionsVolatilityResearchFeatures
    ) -> OptionsVolatilityResearchFeatures:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(options_volatility_research_feature.c.feature_id).where(
                        options_volatility_research_feature.c.feature_id == features.feature_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        options_volatility_research_feature.insert().values(
                            feature_id=features.feature_id,
                            snapshot_id=features.snapshot_id,
                            underlying_canonical_security_id=(
                                features.underlying_canonical_security_id
                            ),
                            as_of=features.as_of,
                            payload=_payload(features),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsQuoteIntelligencePersistenceError(
                "FEATURE_PERSISTENCE_CONFLICT"
            ) from error
        return features

    def research_features_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionsVolatilityResearchFeatures, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_volatility_research_feature)
                .where(
                    options_volatility_research_feature.c.underlying_canonical_security_id
                    == underlying_canonical_security_id
                )
                .order_by(options_volatility_research_feature.c.as_of)
            ).mappings()
            return tuple(_features(row) for row in rows)

    def research_features_by_snapshot(
        self, snapshot_id: str
    ) -> OptionsVolatilityResearchFeatures | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_volatility_research_feature).where(
                        options_volatility_research_feature.c.snapshot_id == snapshot_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _features(row) if row is not None else None
