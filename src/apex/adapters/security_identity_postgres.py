"""PostgreSQL repository for durable stable-security-identity evidence.

This is the durable boundary for what ``security_identity`` owns: derived
stable identities, the identity-resolution/equivalence ledger (legacy and
stable IDs alike), and the options-underlying-to-stable-identity relink
ledger. It stores no reconciled/legacy identity evidence of its own (that
remains ``security_reference_postgres``'s job) and no option contract/
capability reference evidence (``options_intelligence_postgres``'s job) --
only what this milestone adds on top of both.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.security_identity import (
    IdentityResolution,
    OptionUnderlyingStableLinkage,
    StableSecurityIdentity,
)
from apex.domain.values import canonical_json_bytes

SECURITY_IDENTITY_TABLES = (
    "security_stable_identity",
    "security_identity_resolution",
    "options_underlying_stable_linkage",
)

metadata = sa.MetaData()

security_stable_identity = sa.Table(
    "security_stable_identity",
    metadata,
    sa.Column("canonical_hash", sa.String(64), primary_key=True),
    sa.Column("stable_security_id", sa.String(160), nullable=False),
    sa.Column("identity_basis", sa.String(24), nullable=False),
    sa.Column("cik", sa.String(10), nullable=True),
    sa.Column("normalized_symbol", sa.String(32), nullable=False),
    sa.Column("current_symbol", sa.String(32), nullable=False),
    sa.Column("source_identity_hash", sa.String(64), nullable=False),
    sa.Column("source_canonical_security_id", sa.String(160), nullable=False),
    sa.Column("review_required", sa.Boolean(), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
security_identity_resolution = sa.Table(
    "security_identity_resolution",
    metadata,
    sa.Column("canonical_hash", sa.String(64), primary_key=True),
    sa.Column("old_identity_id", sa.String(160), nullable=False),
    sa.Column("old_identity_kind", sa.String(40), nullable=False),
    sa.Column("resolved_stable_security_id", sa.String(160), nullable=True),
    sa.Column("resolution_state", sa.String(40), nullable=False),
    sa.Column("automatic", sa.Boolean(), nullable=False),
    sa.Column("resolution_method", sa.String(80), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
options_underlying_stable_linkage = sa.Table(
    "options_underlying_stable_linkage",
    metadata,
    sa.Column("canonical_hash", sa.String(64), primary_key=True),
    sa.Column("raw_underlying_symbol", sa.String(32), nullable=False),
    sa.Column("outcome", sa.String(16), nullable=False),
    sa.Column("resolved_stable_security_id", sa.String(160), nullable=True),
    sa.Column("historical_underlying_security_id", sa.String(160), nullable=True),
    sa.Column("contract_count", sa.Integer(), nullable=False),
    sa.Column("linkage_run_id", sa.String(160), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)


class SecurityIdentityPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise SecurityIdentityPersistenceError("STORED_PAYLOAD_INVALID")
    return value


def _stable_identity(row: RowMapping) -> StableSecurityIdentity:
    return rehydrate(StableSecurityIdentity, _decoded(row))


def _resolution(row: RowMapping) -> IdentityResolution:
    return rehydrate(IdentityResolution, _decoded(row))


def _linkage(row: RowMapping) -> OptionUnderlyingStableLinkage:
    return rehydrate(OptionUnderlyingStableLinkage, _decoded(row))


class PostgresSecurityIdentityRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- stable identities --------------------------------------------

    def save_stable_identities(
        self, identities: tuple[StableSecurityIdentity, ...], *, recorded_at: datetime
    ) -> tuple[StableSecurityIdentity, ...]:
        if not identities:
            return identities
        with self._engine.begin() as connection:
            existing = set(
                connection.execute(
                    sa.select(security_stable_identity.c.canonical_hash).where(
                        security_stable_identity.c.canonical_hash.in_(
                            {item.canonical_hash for item in identities}
                        )
                    )
                ).scalars()
            )
            for item in identities:
                if item.canonical_hash in existing:
                    continue
                connection.execute(
                    security_stable_identity.insert().values(
                        canonical_hash=item.canonical_hash,
                        stable_security_id=item.stable_security_id,
                        identity_basis=item.identity_basis.value,
                        cik=item.cik,
                        normalized_symbol=item.normalized_symbol,
                        current_symbol=item.current_symbol,
                        source_identity_hash=item.source_identity_hash,
                        source_canonical_security_id=item.source_canonical_security_id,
                        review_required=item.review_required,
                        recorded_at=recorded_at,
                        payload=_payload(item),
                        authority="NONE",
                    )
                )
        return identities

    def stable_identity(self, canonical_hash: str) -> StableSecurityIdentity | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(security_stable_identity).where(
                        security_stable_identity.c.canonical_hash == canonical_hash
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _stable_identity(row) if row is not None else None

    def stable_identities_by_stable_id(
        self, stable_security_id: str
    ) -> tuple[StableSecurityIdentity, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(security_stable_identity)
                .where(security_stable_identity.c.stable_security_id == stable_security_id)
                .order_by(security_stable_identity.c.recorded_at)
            ).mappings()
            return tuple(_stable_identity(row) for row in rows)

    def latest_stable_identities(self) -> tuple[StableSecurityIdentity, ...]:
        """The latest known row per distinct ``stable_security_id``, across
        every projection run ever recorded -- the correct, already-deduped
        ``prior`` input for a subsequent
        ``detect_stable_identity_supersession`` call.
        """
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(security_stable_identity).order_by(security_stable_identity.c.recorded_at)
            ).mappings()
            latest: dict[str, StableSecurityIdentity] = {}
            for row in rows:
                item = _stable_identity(row)
                latest[item.stable_security_id] = item
            return tuple(sorted(latest.values(), key=lambda item: item.stable_security_id))

    def stable_identity_count(self) -> int:
        with self._engine.connect() as connection:
            return int(
                connection.scalar(
                    sa.select(
                        sa.func.count(sa.distinct(security_stable_identity.c.stable_security_id))
                    )
                )
                or 0
            )

    # --- identity resolution ledger ------------------------------------

    def save_resolutions(
        self, resolutions: tuple[IdentityResolution, ...], *, recorded_at: datetime
    ) -> tuple[IdentityResolution, ...]:
        if not resolutions:
            return resolutions
        with self._engine.begin() as connection:
            existing = set(
                connection.execute(
                    sa.select(security_identity_resolution.c.canonical_hash).where(
                        security_identity_resolution.c.canonical_hash.in_(
                            {item.canonical_hash for item in resolutions}
                        )
                    )
                ).scalars()
            )
            for item in resolutions:
                if item.canonical_hash in existing:
                    continue
                connection.execute(
                    security_identity_resolution.insert().values(
                        canonical_hash=item.canonical_hash,
                        old_identity_id=item.old_identity_id,
                        old_identity_kind=item.old_identity_kind.value,
                        resolved_stable_security_id=item.resolved_stable_security_id,
                        resolution_state=item.resolution_state.value,
                        automatic=item.automatic,
                        resolution_method=item.resolution_method,
                        recorded_at=recorded_at,
                        payload=_payload(item),
                        authority="NONE",
                    )
                )
        return resolutions

    def resolutions_for_old_id(self, old_identity_id: str) -> tuple[IdentityResolution, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(security_identity_resolution)
                .where(security_identity_resolution.c.old_identity_id == old_identity_id)
                .order_by(security_identity_resolution.c.recorded_at)
            ).mappings()
            return tuple(_resolution(row) for row in rows)

    def resolutions_to_stable_id(self, stable_security_id: str) -> tuple[IdentityResolution, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(security_identity_resolution)
                .where(
                    security_identity_resolution.c.resolved_stable_security_id == stable_security_id
                )
                .order_by(security_identity_resolution.c.recorded_at)
            ).mappings()
            return tuple(_resolution(row) for row in rows)

    def ambiguous_resolutions(self, *, limit: int = 200) -> tuple[IdentityResolution, ...]:
        """A bounded list for operator surfaces -- never the full ledger."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(security_identity_resolution)
                .where(
                    security_identity_resolution.c.resolution_state.in_(
                        ("AMBIGUOUS_REQUIRES_REVIEW", "DISTINCT_SECURITY")
                    )
                )
                .order_by(security_identity_resolution.c.recorded_at.desc())
                .limit(limit)
            ).mappings()
            return tuple(_resolution(row) for row in rows)

    def resolution_state_counts(self) -> tuple[tuple[str, int], ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    security_identity_resolution.c.resolution_state, sa.func.count()
                ).group_by(security_identity_resolution.c.resolution_state)
            ).all()
            return tuple(sorted((str(state), int(count)) for state, count in rows))

    def resolution_count(self) -> int:
        with self._engine.connect() as connection:
            return int(
                connection.scalar(
                    sa.select(sa.func.count()).select_from(security_identity_resolution)
                )
                or 0
            )

    # --- options-underlying stable linkage ------------------------------

    def save_option_linkages(
        self, linkages: tuple[OptionUnderlyingStableLinkage, ...], *, recorded_at: datetime
    ) -> tuple[OptionUnderlyingStableLinkage, ...]:
        if not linkages:
            return linkages
        with self._engine.begin() as connection:
            existing = set(
                connection.execute(
                    sa.select(options_underlying_stable_linkage.c.canonical_hash).where(
                        options_underlying_stable_linkage.c.canonical_hash.in_(
                            {item.canonical_hash for item in linkages}
                        )
                    )
                ).scalars()
            )
            for item in linkages:
                if item.canonical_hash in existing:
                    continue
                connection.execute(
                    options_underlying_stable_linkage.insert().values(
                        canonical_hash=item.canonical_hash,
                        raw_underlying_symbol=item.raw_underlying_symbol,
                        outcome=item.outcome.value,
                        resolved_stable_security_id=item.resolved_stable_security_id,
                        historical_underlying_security_id=item.historical_underlying_security_id,
                        contract_count=item.contract_count,
                        linkage_run_id=item.linkage_run_id,
                        recorded_at=recorded_at,
                        payload=_payload(item),
                        authority="NONE",
                    )
                )
        return linkages

    def option_linkages_for_run(
        self, linkage_run_id: str
    ) -> tuple[OptionUnderlyingStableLinkage, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_underlying_stable_linkage)
                .where(options_underlying_stable_linkage.c.linkage_run_id == linkage_run_id)
                .order_by(options_underlying_stable_linkage.c.raw_underlying_symbol)
            ).mappings()
            return tuple(_linkage(row) for row in rows)

    def latest_option_linkage_run_id(self) -> str | None:
        with self._engine.connect() as connection:
            return connection.scalar(
                sa.select(options_underlying_stable_linkage.c.linkage_run_id)
                .order_by(options_underlying_stable_linkage.c.recorded_at.desc())
                .limit(1)
            )

    def option_linkage_run_ids(self) -> tuple[str, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_underlying_stable_linkage.c.linkage_run_id).distinct()
            ).scalars()
            return tuple(sorted(set(rows)))

    def option_linkage(self, raw_underlying_symbol: str) -> OptionUnderlyingStableLinkage | None:
        """The most recently recorded linkage decision for one raw symbol,
        across every run -- not scoped to one linkage_run_id."""
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_underlying_stable_linkage)
                    .where(
                        options_underlying_stable_linkage.c.raw_underlying_symbol
                        == raw_underlying_symbol
                    )
                    .order_by(options_underlying_stable_linkage.c.recorded_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _linkage(row) if row is not None else None
