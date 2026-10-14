"""PostgreSQL repository for defined-risk options structure economics and
research integration.

Follows the ``payload``-column pattern
``apex.adapters.options_quote_intelligence_postgres`` already uses for the
evidence family this module builds on: one canonical JSON payload column
per row (restored exactly via ``canonical_rehydrate.rehydrate``) plus a
small set of indexed columns for the query shapes the application layer
actually needs. Every table carries ``authority = 'NONE'``.

``options_fee_policy``/``options_slippage_policy``/``structure_pricing_
policy`` carry one DB-only ``created_at`` column that is *not* part of the
domain object's own content (those three domain dataclasses have no such
field -- their identity and canonical content are fixed, small, versioned
policy declarations). That column exists purely so ``latest_*_policy``
queries have something to order by; it is set from the wall clock at
insert time here, never rehydrated back onto the domain object.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.options_structures import (
    OptionsFeePolicy,
    OptionsSlippagePolicy,
    OptionsStructureResearchPolicy,
    OptionStructureCandidate,
    StructurePricingPolicy,
    StructureResearchFeatures,
    StructureResearchSnapshot,
)
from apex.domain.values import canonical_json_bytes

OPTIONS_STRUCTURES_TABLES = (
    "options_fee_policy",
    "options_slippage_policy",
    "structure_pricing_policy",
    "options_structure_research_policy",
    "option_structure_candidate",
    "options_structure_research_snapshot",
    "options_structure_research_feature",
)
OPTIONS_STRUCTURES_IMMUTABLE_TABLES = OPTIONS_STRUCTURES_TABLES

metadata = sa.MetaData()

options_fee_policy = sa.Table(
    "options_fee_policy",
    metadata,
    sa.Column("policy_id", sa.String(160), primary_key=True),
    sa.Column("version", sa.String(80), nullable=False),
    sa.Column("per_contract_fee", sa.Numeric(18, 8), nullable=False),
    sa.Column("fee_evidence_status", sa.String(40), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_slippage_policy = sa.Table(
    "options_slippage_policy",
    metadata,
    sa.Column("policy_id", sa.String(160), primary_key=True),
    sa.Column("version", sa.String(80), nullable=False),
    sa.Column("methodology", sa.String(80), nullable=False),
    sa.Column("adverse_fraction_of_spread", sa.Numeric(18, 8), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
structure_pricing_policy = sa.Table(
    "structure_pricing_policy",
    metadata,
    sa.Column("policy_id", sa.String(160), primary_key=True),
    sa.Column("version", sa.String(80), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_structure_research_policy = sa.Table(
    "options_structure_research_policy",
    metadata,
    sa.Column("policy_id", sa.String(160), primary_key=True),
    sa.Column("version", sa.String(80), nullable=False),
    sa.Column("dte_minimum", sa.Integer(), nullable=False),
    sa.Column("dte_maximum", sa.Integer(), nullable=False),
    sa.Column("maximum_structures_per_underlying", sa.Integer(), nullable=False),
    sa.Column("maximum_total_structures", sa.Integer(), nullable=False),
    sa.Column(
        "pricing_policy_id",
        sa.String(160),
        sa.ForeignKey("structure_pricing_policy.policy_id"),
        nullable=False,
    ),
    sa.Column(
        "fee_policy_id",
        sa.String(160),
        sa.ForeignKey("options_fee_policy.policy_id"),
        nullable=False,
    ),
    sa.Column(
        "slippage_policy_id",
        sa.String(160),
        sa.ForeignKey("options_slippage_policy.policy_id"),
        nullable=False,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
option_structure_candidate = sa.Table(
    "option_structure_candidate",
    metadata,
    sa.Column("structure_id", sa.String(160), primary_key=True),
    sa.Column("structure_type", sa.String(40), nullable=False),
    sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
    sa.Column("expiration", sa.String(10), nullable=False),
    sa.Column(
        "structure_policy_id",
        sa.String(160),
        sa.ForeignKey("options_structure_research_policy.policy_id"),
        nullable=False,
    ),
    sa.Column(
        "source_chain_snapshot_id",
        sa.String(160),
        sa.ForeignKey("options_chain_snapshot.snapshot_id"),
        nullable=False,
    ),
    sa.Column("long_canonical_contract_id", sa.String(160), nullable=False),
    sa.Column("short_canonical_contract_id", sa.String(160), nullable=False),
    sa.Column(
        "long_observation_id",
        sa.String(160),
        sa.ForeignKey("options_quote_observation.observation_id"),
        nullable=False,
    ),
    sa.Column(
        "short_observation_id",
        sa.String(160),
        sa.ForeignKey("options_quote_observation.observation_id"),
        nullable=False,
    ),
    sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_structure_research_snapshot = sa.Table(
    "options_structure_research_snapshot",
    metadata,
    sa.Column("research_snapshot_id", sa.String(160), primary_key=True),
    sa.Column(
        "structure_policy_id",
        sa.String(160),
        sa.ForeignKey("options_structure_research_policy.policy_id"),
        nullable=False,
    ),
    sa.Column(
        "source_chain_snapshot_id",
        sa.String(160),
        sa.ForeignKey("options_chain_snapshot.snapshot_id"),
        nullable=False,
    ),
    sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
    sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_structure_research_feature = sa.Table(
    "options_structure_research_feature",
    metadata,
    sa.Column("feature_id", sa.String(160), primary_key=True),
    sa.Column(
        "structure_id",
        sa.String(160),
        sa.ForeignKey("option_structure_candidate.structure_id"),
        nullable=False,
    ),
    sa.Column("underlying_canonical_security_id", sa.String(160), nullable=False),
    sa.Column("as_of", sa.String(10), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
sa.Index(
    "ix_osi_candidate_underlying_expiration",
    option_structure_candidate.c.underlying_canonical_security_id,
    option_structure_candidate.c.expiration,
)
sa.Index(
    "ix_osi_snapshot_underlying_evaluated",
    options_structure_research_snapshot.c.underlying_canonical_security_id,
    options_structure_research_snapshot.c.evaluated_at,
)
sa.Index(
    "ix_osi_feature_underlying_as_of",
    options_structure_research_feature.c.underlying_canonical_security_id,
    options_structure_research_feature.c.as_of,
)


class OptionsStructuresPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise OptionsStructuresPersistenceError("STORED_PAYLOAD_INVALID")
    return value


def _fee_policy(row: RowMapping) -> OptionsFeePolicy:
    return rehydrate(OptionsFeePolicy, _decoded(row))


def _slippage_policy(row: RowMapping) -> OptionsSlippagePolicy:
    return rehydrate(OptionsSlippagePolicy, _decoded(row))


def _pricing_policy(row: RowMapping) -> StructurePricingPolicy:
    return rehydrate(StructurePricingPolicy, _decoded(row))


def _structure_policy(row: RowMapping) -> OptionsStructureResearchPolicy:
    return rehydrate(OptionsStructureResearchPolicy, _decoded(row))


def _structure(row: RowMapping) -> OptionStructureCandidate:
    return rehydrate(OptionStructureCandidate, _decoded(row))


def _research_snapshot(row: RowMapping) -> StructureResearchSnapshot:
    return rehydrate(StructureResearchSnapshot, _decoded(row))


def _research_features(row: RowMapping) -> StructureResearchFeatures:
    return rehydrate(StructureResearchFeatures, _decoded(row))


class PostgresOptionsStructuresRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- fee policy ---------------------------------------------------

    def save_fee_policy(self, policy: OptionsFeePolicy) -> OptionsFeePolicy:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(options_fee_policy.c.policy_id).where(
                        options_fee_policy.c.policy_id == policy.policy_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        options_fee_policy.insert().values(
                            policy_id=policy.policy_id,
                            version=policy.version,
                            per_contract_fee=policy.per_contract_fee,
                            fee_evidence_status=policy.fee_evidence_status,
                            created_at=datetime.now(tz=UTC),
                            payload=_payload(policy),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsStructuresPersistenceError("FEE_POLICY_PERSISTENCE_CONFLICT") from error
        return policy

    def fee_policy_by_id(self, policy_id: str) -> OptionsFeePolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_fee_policy).where(options_fee_policy.c.policy_id == policy_id)
                )
                .mappings()
                .one_or_none()
            )
        return _fee_policy(row) if row is not None else None

    def latest_fee_policy(self) -> OptionsFeePolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_fee_policy)
                    .order_by(options_fee_policy.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _fee_policy(row) if row is not None else None

    # -- slippage policy ---------------------------------------------------

    def save_slippage_policy(self, policy: OptionsSlippagePolicy) -> OptionsSlippagePolicy:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(options_slippage_policy.c.policy_id).where(
                        options_slippage_policy.c.policy_id == policy.policy_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        options_slippage_policy.insert().values(
                            policy_id=policy.policy_id,
                            version=policy.version,
                            methodology=policy.methodology,
                            adverse_fraction_of_spread=policy.adverse_fraction_of_spread,
                            created_at=datetime.now(tz=UTC),
                            payload=_payload(policy),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsStructuresPersistenceError(
                "SLIPPAGE_POLICY_PERSISTENCE_CONFLICT"
            ) from error
        return policy

    def slippage_policy_by_id(self, policy_id: str) -> OptionsSlippagePolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_slippage_policy).where(
                        options_slippage_policy.c.policy_id == policy_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _slippage_policy(row) if row is not None else None

    def latest_slippage_policy(self) -> OptionsSlippagePolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_slippage_policy)
                    .order_by(options_slippage_policy.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _slippage_policy(row) if row is not None else None

    # -- pricing policy ---------------------------------------------------

    def save_pricing_policy(self, policy: StructurePricingPolicy) -> StructurePricingPolicy:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(structure_pricing_policy.c.policy_id).where(
                        structure_pricing_policy.c.policy_id == policy.policy_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        structure_pricing_policy.insert().values(
                            policy_id=policy.policy_id,
                            version=policy.version,
                            created_at=datetime.now(tz=UTC),
                            payload=_payload(policy),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsStructuresPersistenceError(
                "PRICING_POLICY_PERSISTENCE_CONFLICT"
            ) from error
        return policy

    def pricing_policy_by_id(self, policy_id: str) -> StructurePricingPolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(structure_pricing_policy).where(
                        structure_pricing_policy.c.policy_id == policy_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _pricing_policy(row) if row is not None else None

    def latest_pricing_policy(self) -> StructurePricingPolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(structure_pricing_policy)
                    .order_by(structure_pricing_policy.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _pricing_policy(row) if row is not None else None

    # -- structure research policy ---------------------------------------------------

    def save_structure_policy(
        self, policy: OptionsStructureResearchPolicy
    ) -> OptionsStructureResearchPolicy:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(options_structure_research_policy.c.policy_id).where(
                        options_structure_research_policy.c.policy_id == policy.policy_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        options_structure_research_policy.insert().values(
                            policy_id=policy.policy_id,
                            version=policy.version,
                            dte_minimum=policy.dte_minimum,
                            dte_maximum=policy.dte_maximum,
                            maximum_structures_per_underlying=policy.maximum_structures_per_underlying,
                            maximum_total_structures=policy.maximum_total_structures,
                            pricing_policy_id=policy.pricing_policy_id,
                            fee_policy_id=policy.fee_policy_id,
                            slippage_policy_id=policy.slippage_policy_id,
                            created_at=policy.created_at,
                            payload=_payload(policy),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsStructuresPersistenceError(
                "STRUCTURE_POLICY_PERSISTENCE_CONFLICT"
            ) from error
        return policy

    def structure_policy_by_id(self, policy_id: str) -> OptionsStructureResearchPolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_structure_research_policy).where(
                        options_structure_research_policy.c.policy_id == policy_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _structure_policy(row) if row is not None else None

    def latest_structure_policy(self) -> OptionsStructureResearchPolicy | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_structure_research_policy)
                    .order_by(options_structure_research_policy.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _structure_policy(row) if row is not None else None

    # -- structures ---------------------------------------------------

    def save_structures(
        self, structures: tuple[OptionStructureCandidate, ...]
    ) -> tuple[OptionStructureCandidate, ...]:
        if not structures:
            return structures
        with self._engine.begin() as connection:
            ids = tuple({item.structure_id for item in structures})
            existing = set(
                connection.execute(
                    sa.select(option_structure_candidate.c.structure_id).where(
                        option_structure_candidate.c.structure_id.in_(ids)
                    )
                ).scalars()
            )
            pending: dict[str, dict[str, object]] = {}
            for structure in structures:
                if structure.structure_id in existing:
                    continue
                pending.setdefault(
                    structure.structure_id,
                    {
                        "structure_id": structure.structure_id,
                        "structure_type": structure.structure_type.value,
                        "underlying_canonical_security_id": (
                            structure.underlying_canonical_security_id
                        ),
                        "expiration": structure.expiration,
                        "structure_policy_id": structure.structure_policy_id,
                        "source_chain_snapshot_id": structure.source_chain_snapshot_id,
                        "long_canonical_contract_id": structure.long_leg.canonical_contract_id,
                        "short_canonical_contract_id": structure.short_leg.canonical_contract_id,
                        "long_observation_id": structure.long_leg.observation_id,
                        "short_observation_id": structure.short_leg.observation_id,
                        "evaluated_at": structure.evaluated_at,
                        "payload": _payload(structure),
                        "authority": "NONE",
                    },
                )
            rows = list(pending.values())
            if rows:
                connection.execute(option_structure_candidate.insert(), rows)
        return structures

    def structure_by_id(self, structure_id: str) -> OptionStructureCandidate | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(option_structure_candidate).where(
                        option_structure_candidate.c.structure_id == structure_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _structure(row) if row is not None else None

    def structures_by_ids(
        self, structure_ids: tuple[str, ...]
    ) -> tuple[OptionStructureCandidate, ...]:
        if not structure_ids:
            return ()
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(option_structure_candidate).where(
                    option_structure_candidate.c.structure_id.in_(structure_ids)
                )
            ).mappings()
            return tuple(_structure(row) for row in rows)

    def structures_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionStructureCandidate, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(option_structure_candidate)
                .where(
                    option_structure_candidate.c.underlying_canonical_security_id
                    == underlying_canonical_security_id
                )
                .order_by(option_structure_candidate.c.structure_id)
            ).mappings()
            return tuple(_structure(row) for row in rows)

    def latest_structures_by_underlying(self) -> tuple[OptionStructureCandidate, ...]:
        """Securities Research -> Shared Opportunity Recurring Bridge v1. One
        ``evaluated_at`` cohort per underlying: every retained candidate whose
        ``evaluated_at`` equals that underlying's own most recent
        ``evaluated_at`` -- the current trading session's structures, never
        the full historical backlog. A single batched round trip (join
        against a per-underlying ``MAX(evaluated_at)`` subquery), not one
        query per underlying -- mirrors the N+1-avoidance idiom
        ``PostgresSharedOpportunityRepository.projections_by_id`` already
        established for this exact reason (Operator Console Follow-Up
        Performance v1)."""

        latest_per_underlying = (
            sa.select(
                option_structure_candidate.c.underlying_canonical_security_id,
                sa.func.max(option_structure_candidate.c.evaluated_at).label("latest_evaluated_at"),
            )
            .group_by(option_structure_candidate.c.underlying_canonical_security_id)
            .subquery()
        )
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(option_structure_candidate)
                .join(
                    latest_per_underlying,
                    sa.and_(
                        option_structure_candidate.c.underlying_canonical_security_id
                        == latest_per_underlying.c.underlying_canonical_security_id,
                        option_structure_candidate.c.evaluated_at
                        == latest_per_underlying.c.latest_evaluated_at,
                    ),
                )
                .order_by(
                    option_structure_candidate.c.underlying_canonical_security_id,
                    option_structure_candidate.c.structure_id,
                )
            ).mappings()
            return tuple(_structure(row) for row in rows)

    # -- research snapshot ---------------------------------------------------

    def save_research_snapshot(
        self, snapshot: StructureResearchSnapshot
    ) -> StructureResearchSnapshot:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(options_structure_research_snapshot.c.research_snapshot_id).where(
                        options_structure_research_snapshot.c.research_snapshot_id
                        == snapshot.research_snapshot_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        options_structure_research_snapshot.insert().values(
                            research_snapshot_id=snapshot.research_snapshot_id,
                            structure_policy_id=snapshot.structure_policy_id,
                            source_chain_snapshot_id=snapshot.source_chain_snapshot_id,
                            underlying_canonical_security_id=snapshot.underlying_canonical_security_id,
                            evaluated_at=snapshot.evaluated_at,
                            payload=_payload(snapshot),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsStructuresPersistenceError(
                "RESEARCH_SNAPSHOT_PERSISTENCE_CONFLICT"
            ) from error
        return snapshot

    def research_snapshot_by_id(
        self, research_snapshot_id: str
    ) -> StructureResearchSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_structure_research_snapshot).where(
                        options_structure_research_snapshot.c.research_snapshot_id
                        == research_snapshot_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _research_snapshot(row) if row is not None else None

    def latest_research_snapshot_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> StructureResearchSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_structure_research_snapshot)
                    .where(
                        options_structure_research_snapshot.c.underlying_canonical_security_id
                        == underlying_canonical_security_id
                    )
                    .order_by(options_structure_research_snapshot.c.evaluated_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _research_snapshot(row) if row is not None else None

    def research_snapshots_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[StructureResearchSnapshot, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_structure_research_snapshot)
                .where(
                    options_structure_research_snapshot.c.underlying_canonical_security_id
                    == underlying_canonical_security_id
                )
                .order_by(options_structure_research_snapshot.c.evaluated_at)
            ).mappings()
            return tuple(_research_snapshot(row) for row in rows)

    # -- research features ---------------------------------------------------

    def save_research_features(
        self, features: tuple[StructureResearchFeatures, ...]
    ) -> tuple[StructureResearchFeatures, ...]:
        if not features:
            return features
        with self._engine.begin() as connection:
            ids = tuple({item.feature_id for item in features})
            existing = set(
                connection.execute(
                    sa.select(options_structure_research_feature.c.feature_id).where(
                        options_structure_research_feature.c.feature_id.in_(ids)
                    )
                ).scalars()
            )
            pending: dict[str, dict[str, object]] = {}
            for item in features:
                if item.feature_id in existing:
                    continue
                pending.setdefault(
                    item.feature_id,
                    {
                        "feature_id": item.feature_id,
                        "structure_id": item.structure_id,
                        "underlying_canonical_security_id": item.underlying_canonical_security_id,
                        "as_of": item.as_of,
                        "payload": _payload(item),
                        "authority": "NONE",
                    },
                )
            rows = list(pending.values())
            if rows:
                connection.execute(options_structure_research_feature.insert(), rows)
        return features

    def research_features_by_structure(self, structure_id: str) -> StructureResearchFeatures | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_structure_research_feature).where(
                        options_structure_research_feature.c.structure_id == structure_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _research_features(row) if row is not None else None
