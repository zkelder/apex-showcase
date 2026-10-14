"""PostgreSQL repository for durable security-reference identity and universe evidence.

This is the durable boundary for the two things ``security_reference`` owns:
resolved identity plus ambiguity (``ReconciledSecurity``), and the immutable
current-listed universe snapshot built from it.  It stores no market data, no
option, no broad-equity-quality, and no capital-authority evidence — those
belong to their own repositories.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.canonical_rehydrate import RehydrationError, rehydrate
from apex.domain.security_reference import (
    APEX_CURRENT_US_EQUITY_UNIVERSE_SNAPSHOT_V1,
    CurrentEquityUniverseSnapshot,
    ExecutionCapabilityState,
    ReconciledSecurity,
)
from apex.domain.values import canonical_json_bytes

SECURITY_REFERENCE_TABLES = (
    "security_reference_identity",
    "security_reference_universe_snapshot",
)

metadata = sa.MetaData()

security_reference_identity = sa.Table(
    "security_reference_identity",
    metadata,
    sa.Column("canonical_hash", sa.String(64), primary_key=True),
    sa.Column("canonical_security_id", sa.String(160), nullable=False),
    sa.Column("current_symbol", sa.String(32), nullable=False),
    sa.Column("requires_review", sa.Boolean(), nullable=False),
    sa.Column("classifications", sa.ARRAY(sa.Text()), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
security_reference_universe_snapshot = sa.Table(
    "security_reference_universe_snapshot",
    metadata,
    sa.Column("idempotency_key", sa.String(160), primary_key=True),
    sa.Column("universe_id", sa.String(160), nullable=False),
    sa.Column("policy_id", sa.String(160), nullable=False),
    sa.Column("policy_hash", sa.String(64), nullable=False),
    sa.Column("as_of_date", sa.Date(), nullable=False),
    sa.Column("total_considered", sa.Integer(), nullable=False),
    sa.Column("eligible_count", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class SecurityReferencePersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise SecurityReferencePersistenceError("STORED_PAYLOAD_INVALID")
    return value


def _identity(row: RowMapping) -> ReconciledSecurity:
    return rehydrate(ReconciledSecurity, _decoded(row))


# --- Legacy (pre research/execution-separation) universe-snapshot compatibility ---
#
# A universe snapshot written before "Genuine Broad-Market Coverage
# Completion v1" recorded exactly one eligibility decision per security
# (bare ``eligible``/``reasons``) and had no provider-execution-capability
# concept at all -- no ``research_eligible_count``, ``execution_capability``,
# or related field exists under any name in that payload. That row is
# immutable evidence: it is never updated, deleted, or backfilled here. It
# must still be readable through today's repository API instead of quietly
# disappearing from every read built for the current shape.
#
# These ``SCHEMA_COMPATIBILITY_*`` values are returned to callers -- never
# stored -- so an operator can always distinguish a row whose payload
# matches the current schema, one that only matches this known legacy
# schema, and one that matches neither and is retained exactly as-is
# pending manual review.
SCHEMA_COMPATIBILITY_CURRENT = "CURRENT"
SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1 = "LEGACY_PRE_EXECUTION_SEPARATION_V1"
SCHEMA_COMPATIBILITY_INCOMPATIBLE = "INCOMPATIBLE"
LEGACY_PRE_EXECUTION_SEPARATION_SCHEMA_MARKER = (
    "INFERRED_LEGACY_PRE_EXECUTION_SEPARATION_UNIVERSE_SNAPSHOT"
)
LEGACY_EXECUTION_CAPABILITY_REASON = "LEGACY_SCHEMA_PREDATES_EXECUTION_CAPABILITY_SEPARATION"


@dataclass(frozen=True, slots=True)
class RetainedUniverseSnapshotEvidence:
    """One retained ``security_reference_universe_snapshot`` row, exposed
    regardless of whether its stored payload matches the current schema, the
    one known pre-separation legacy schema, or neither.

    ``snapshot`` is populated for ``CURRENT`` and
    ``LEGACY_PRE_EXECUTION_SEPARATION_V1`` alike, and is ``None`` only for
    ``INCOMPATIBLE``, where ``compatibility_blocker`` names exactly why the
    stored payload could not be rehydrated without inventing a fact it does
    not contain. This is a read-time projection only -- it never updates,
    deletes, or backfills the underlying row.
    """

    idempotency_key: str
    universe_id: str | None
    payload_schema_marker: str | None
    schema_compatibility: str
    compatibility_blocker: str | None
    snapshot: CurrentEquityUniverseSnapshot | None


def _universe_schema_marker(raw: dict[str, Any]) -> str:
    """Identify a payload's schema by the fields it is known to always
    carry -- neither schema ever stamped an explicit version marker of its
    own, so this is necessarily shape-based, not a declared-field lookup.
    """
    if "research_eligible_count" in raw and "execution_capability_counts" in raw:
        return SCHEMA_COMPATIBILITY_CURRENT
    if "eligible_count" in raw and "exclusion_counts" in raw and "decisions" in raw:
        return SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1
    return SCHEMA_COMPATIBILITY_INCOMPATIBLE


def _legacy_universe_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate a pre-separation universe payload into the current
    ``CurrentEquityUniverseSnapshot`` payload shape, so the same
    schema-agnostic ``rehydrate`` used for every other payload reconstructs
    it exactly instead of a hand-rolled, easier-to-get-subtly-wrong parallel
    decoder.

    Every legacy value is carried through completely unchanged, including
    the original research ``eligible``/``reasons`` decision (now under its
    current field names, never recomputed against today's policy). The only
    values introduced here are an honest, uniformly "unknown" execution
    decision: this function can never assert, positively or negatively,
    that a security was Alpaca tradable, because the retained legacy
    payload carries no such evidence at all.
    """
    decisions = [
        {
            "canonical_security_id": item["canonical_security_id"],
            "current_symbol": item["current_symbol"],
            "research_eligible": item["eligible"],
            "research_reasons": item["reasons"],
            "execution_capability": (
                ExecutionCapabilityState.LEGACY_EXECUTION_CAPABILITY_UNKNOWN.value
            ),
            "execution_reasons": [LEGACY_EXECUTION_CAPABILITY_REASON],
            "security_hash": item["security_hash"],
        }
        for item in raw["decisions"]
    ]
    total_considered = raw["total_considered"]
    execution_capability_counts = (
        [[ExecutionCapabilityState.LEGACY_EXECUTION_CAPABILITY_UNKNOWN.value, total_considered]]
        if total_considered
        else []
    )
    return {
        "universe_id": raw["universe_id"],
        "policy_id": raw["policy_id"],
        "policy_hash": raw["policy_hash"],
        "as_of_date": raw["as_of_date"],
        "created_at": raw["created_at"],
        "source_receipt_ids": raw["source_receipt_ids"],
        "total_considered": total_considered,
        "security_type_counts": raw["security_type_counts"],
        "research_eligible_count": raw["eligible_count"],
        "research_exclusion_counts": raw["exclusion_counts"],
        "execution_capability_counts": execution_capability_counts,
        "execution_capability_known_count": 0,
        "execution_capability_unknown_count": total_considered,
        "decisions": decisions,
        "provider_disagreements": raw["provider_disagreements"],
        "warning": raw["warning"],
        "canonical_hash": raw["canonical_hash"],
    }


def _universe_id_hint(raw: object) -> str | None:
    universe_id = raw.get("universe_id") if isinstance(raw, dict) else None
    return universe_id if isinstance(universe_id, str) else None


def _decode_universe(idempotency_key: str, payload_text: str) -> RetainedUniverseSnapshotEvidence:
    """Decode one retained row's payload into an explicit, never-raising
    evidence projection.

    This is the single place that knows about more than one on-disk
    ``security_reference_universe_snapshot`` payload shape; every read
    method on this repository is built on top of it so none of them can
    independently reintroduce a silent skip. A row this cannot faithfully
    reconstruct under a known schema is retained exactly as read from
    PostgreSQL -- it is never updated, deleted, or backfilled here -- and is
    returned with ``snapshot=None`` and an exact ``compatibility_blocker``
    instead of being raised or dropped.
    """
    try:
        raw = json.loads(payload_text)
    except json.JSONDecodeError as error:
        return RetainedUniverseSnapshotEvidence(
            idempotency_key,
            None,
            None,
            SCHEMA_COMPATIBILITY_INCOMPATIBLE,
            f"PAYLOAD_JSON_INVALID: {error}",
            None,
        )
    if not isinstance(raw, dict):
        return RetainedUniverseSnapshotEvidence(
            idempotency_key,
            None,
            None,
            SCHEMA_COMPATIBILITY_INCOMPATIBLE,
            "PAYLOAD_IS_NOT_A_JSON_OBJECT",
            None,
        )
    universe_id_hint = _universe_id_hint(raw)
    marker = _universe_schema_marker(raw)
    if marker == SCHEMA_COMPATIBILITY_CURRENT:
        try:
            snapshot = rehydrate(CurrentEquityUniverseSnapshot, raw)
        except RehydrationError as error:
            return RetainedUniverseSnapshotEvidence(
                idempotency_key,
                universe_id_hint,
                APEX_CURRENT_US_EQUITY_UNIVERSE_SNAPSHOT_V1,
                SCHEMA_COMPATIBILITY_INCOMPATIBLE,
                str(error),
                None,
            )
        return RetainedUniverseSnapshotEvidence(
            idempotency_key,
            snapshot.universe_id,
            APEX_CURRENT_US_EQUITY_UNIVERSE_SNAPSHOT_V1,
            SCHEMA_COMPATIBILITY_CURRENT,
            None,
            snapshot,
        )
    if marker == SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1:
        try:
            snapshot = rehydrate(CurrentEquityUniverseSnapshot, _legacy_universe_payload(raw))
        except (RehydrationError, KeyError, TypeError) as error:
            return RetainedUniverseSnapshotEvidence(
                idempotency_key,
                universe_id_hint,
                LEGACY_PRE_EXECUTION_SEPARATION_SCHEMA_MARKER,
                SCHEMA_COMPATIBILITY_INCOMPATIBLE,
                f"{type(error).__name__}: {error}",
                None,
            )
        return RetainedUniverseSnapshotEvidence(
            idempotency_key,
            snapshot.universe_id,
            LEGACY_PRE_EXECUTION_SEPARATION_SCHEMA_MARKER,
            SCHEMA_COMPATIBILITY_LEGACY_PRE_EXECUTION_SEPARATION_V1,
            None,
            snapshot,
        )
    return RetainedUniverseSnapshotEvidence(
        idempotency_key,
        universe_id_hint,
        None,
        SCHEMA_COMPATIBILITY_INCOMPATIBLE,
        "PAYLOAD_SHAPE_NOT_RECOGNIZED_AS_CURRENT_OR_LEGACY",
        None,
    )


class PostgresSecurityReferenceRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def universe_by_key(self, idempotency_key: str) -> CurrentEquityUniverseSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(security_reference_universe_snapshot).where(
                        security_reference_universe_snapshot.c.idempotency_key == idempotency_key
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return _decode_universe(row["idempotency_key"], str(row["payload"])).snapshot

    def save_universe(
        self, idempotency_key: str, snapshot: CurrentEquityUniverseSnapshot
    ) -> CurrentEquityUniverseSnapshot:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    security_reference_universe_snapshot.insert().values(
                        idempotency_key=idempotency_key,
                        universe_id=snapshot.universe_id,
                        policy_id=snapshot.policy_id,
                        policy_hash=snapshot.policy_hash,
                        as_of_date=date.fromisoformat(snapshot.as_of_date),
                        total_considered=snapshot.total_considered,
                        eligible_count=snapshot.research_eligible_count,
                        created_at=snapshot.created_at,
                        canonical_hash=snapshot.canonical_hash,
                        payload=_payload(snapshot),
                        authority="NONE",
                    )
                )
        except IntegrityError as error:
            raise SecurityReferencePersistenceError("UNIVERSE_PERSISTENCE_CONFLICT") from error
        return snapshot

    def universe(self, universe_id: str) -> CurrentEquityUniverseSnapshot | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(security_reference_universe_snapshot).where(
                        security_reference_universe_snapshot.c.universe_id == universe_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return _decode_universe(row["idempotency_key"], str(row["payload"])).snapshot

    def universe_evidence(self) -> tuple[RetainedUniverseSnapshotEvidence, ...]:
        """Every retained universe-snapshot row, exactly once, regardless of
        whether its payload matches the current schema, the known legacy
        schema, or neither.

        This is the basis for ``universes()`` and for repository/operator
        counts -- it is the one method on this repository that can never
        silently shrink relative to the retained row count, so callers that
        need to distinguish current, legacy-compatible, and genuinely
        incompatible evidence (rather than just the rehydratable subset)
        should use this instead of ``universes()``.
        """
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(security_reference_universe_snapshot).order_by(
                    security_reference_universe_snapshot.c.created_at
                )
            ).mappings()
            return tuple(
                _decode_universe(row["idempotency_key"], str(row["payload"])) for row in rows
            )

    def universes(self) -> tuple[CurrentEquityUniverseSnapshot, ...]:
        """Every retained snapshot rehydratable under the current or the
        known legacy schema -- current and legacy-compatible rows alike, in
        ``created_at`` order. A row that is genuinely incompatible with both
        (see ``universe_evidence()``) has no ``CurrentEquityUniverseSnapshot``
        to return here by construction, but it is never dropped silently:
        ``universe_evidence()`` still reports it by row ID, schema marker,
        and exact compatibility blocker.
        """
        return tuple(
            item.snapshot for item in self.universe_evidence() if item.snapshot is not None
        )

    def save_identities(
        self, securities: tuple[ReconciledSecurity, ...], *, recorded_at: datetime
    ) -> tuple[ReconciledSecurity, ...]:
        if not securities:
            return securities
        with self._engine.begin() as connection:
            existing = set(
                connection.execute(
                    sa.select(security_reference_identity.c.canonical_hash).where(
                        security_reference_identity.c.canonical_hash.in_(
                            {item.canonical_hash for item in securities}
                        )
                    )
                ).scalars()
            )
            for security in securities:
                if security.canonical_hash in existing:
                    continue
                connection.execute(
                    security_reference_identity.insert().values(
                        canonical_hash=security.canonical_hash,
                        canonical_security_id=security.canonical_security_id,
                        current_symbol=security.current_symbol,
                        requires_review=security.requires_review,
                        classifications=[item.value for item in security.classifications],
                        recorded_at=recorded_at,
                        payload=_payload(security),
                        authority="NONE",
                    )
                )
        return securities

    def identity(self, canonical_hash: str) -> ReconciledSecurity | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(security_reference_identity).where(
                        security_reference_identity.c.canonical_hash == canonical_hash
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _identity(row) if row is not None else None

    def identities_by_symbol(self, current_symbol: str) -> tuple[ReconciledSecurity, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(security_reference_identity)
                .where(security_reference_identity.c.current_symbol == current_symbol)
                .order_by(security_reference_identity.c.recorded_at)
            ).mappings()
            return tuple(_identity(row) for row in rows)

    def identities_requiring_review(self) -> tuple[ReconciledSecurity, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(security_reference_identity)
                .where(security_reference_identity.c.requires_review.is_(True))
                .order_by(security_reference_identity.c.current_symbol)
            ).mappings()
            return tuple(_identity(row) for row in rows)

    def identities(self) -> tuple[ReconciledSecurity, ...]:
        """Every retained reconciled-identity row, from every universe build
        ever recorded -- not bounded, not scoped to the latest build.

        This is a deliberate, explicit full sweep for the one thing that
        genuinely needs it: backward-compatible legacy-identity resolution
        (``application.security_identity.ResolveLegacyIdentities``), invoked
        by its own dedicated CLI command, never by a default operator view.
        """
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(security_reference_identity).order_by(
                    security_reference_identity.c.recorded_at
                )
            ).mappings()
            return tuple(_identity(row) for row in rows)
