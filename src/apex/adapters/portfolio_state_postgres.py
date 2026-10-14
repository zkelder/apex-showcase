"""Immutable PostgreSQL evidence for normalized portfolio-state snapshots.

Persists normalized ``PortfolioAccountBalance``/``PortfolioPosition``/
``PortfolioSnapshot`` evidence, implementing
``application.portfolio_state.PortfolioStateRepository``.  Deliberately
does not import the existing Crypto/Kalshi/Securities PAPER repositories
(that composition lives in ``adapters.portfolio_state_sources``, which
implements ``PortfolioStateSource`` instead) -- this module is imported by
``adapters.postgres`` for its table registration, and some of those older
per-vertical adapters themselves import from ``adapters.postgres``, so
composing them here would create an import cycle. No provider client,
credential, or network capability lives here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.domain.portfolio_state import (
    AccountKind,
    PortfolioAccountBalance,
    PortfolioCompleteness,
    PortfolioPosition,
    PortfolioSnapshot,
    PortfolioStateError,
    PortfolioVenue,
    PositionExposure,
    UnavailablePortfolioSource,
)
from apex.domain.values import canonical_json_bytes

metadata = sa.MetaData()


def _evidence_table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
    )


balance_table = _evidence_table(
    "portfolio_account_balance",
    "balance_id",
    sa.Column("venue", sa.String(40), nullable=False),
    sa.Column("account_kind", sa.String(40), nullable=False),
    sa.Column("account_id", sa.String(300), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
)
position_table = _evidence_table(
    "portfolio_position",
    "position_id",
    sa.Column("venue", sa.String(40), nullable=False),
    sa.Column("account_kind", sa.String(40), nullable=False),
    sa.Column("account_id", sa.String(300), nullable=False),
    sa.Column("instrument_id", sa.String(300), nullable=False),
    sa.Column("market_family", sa.String(60), nullable=False),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
)
snapshot_table = _evidence_table(
    "portfolio_snapshot",
    "snapshot_id",
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("completeness", sa.String(40), nullable=False),
    sa.Column("source_manifest_hash", sa.String(64), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
)

PORTFOLIO_STATE_IMMUTABLE_TABLES = tuple(metadata.tables)
PORTFOLIO_STATE_TABLES = PORTFOLIO_STATE_IMMUTABLE_TABLES


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise PortfolioStateError("PORTFOLIO_STATE_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PortfolioStateError("PORTFOLIO_STATE_RETAINED_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _pairs(value: object) -> tuple[tuple[str, Decimal], ...]:
    if not isinstance(value, list):
        raise PortfolioStateError("PORTFOLIO_STATE_RETAINED_COLLECTION_INVALID")
    return tuple((str(item[0]), Decimal(str(item[1]))) for item in value)


def _balance(row: RowMapping) -> PortfolioAccountBalance:
    value = _data(row)
    return PortfolioAccountBalance(
        balance_id=str(value["balance_id"]),
        venue=PortfolioVenue(value["venue"]),
        account_kind=AccountKind(value["account_kind"]),
        account_id=str(value["account_id"]),
        currency=str(value["currency"]),
        cash=_optional_decimal(value["cash"]),
        reserved_cash=_optional_decimal(value["reserved_cash"]),
        available_cash=_optional_decimal(value["available_cash"]),
        buying_power=_optional_decimal(value["buying_power"]),
        equity=_optional_decimal(value["equity"]),
        realized_pnl=_optional_decimal(value["realized_pnl"]),
        as_of=_time(value["as_of"]),
        captured_at=_time(value["captured_at"]),
        evidence_class=str(value["evidence_class"]),
        source_record_type=str(value["source_record_type"]),
        source_record_id=str(value["source_record_id"]),
        source_canonical_hash=str(value["source_canonical_hash"]),
        unavailable_fields=_strings(value["unavailable_fields"]),
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _position(row: RowMapping) -> PortfolioPosition:
    value = _data(row)
    return PortfolioPosition(
        position_id=str(value["position_id"]),
        venue=PortfolioVenue(value["venue"]),
        account_kind=AccountKind(value["account_kind"]),
        account_id=str(value["account_id"]),
        instrument_id=str(value["instrument_id"]),
        market_family=str(value["market_family"]),
        exposure=PositionExposure(value["exposure"]),
        raw_side=str(value["raw_side"]),
        quantity=_optional_decimal(value["quantity"]),
        cost_basis=_optional_decimal(value["cost_basis"]),
        mark_value=_optional_decimal(value["mark_value"]),
        exposure_value=_optional_decimal(value["exposure_value"]),
        exposure_value_source=(
            None if value["exposure_value_source"] is None else str(value["exposure_value_source"])
        ),
        unrealized_pnl=_optional_decimal(value["unrealized_pnl"]),
        realized_pnl=_optional_decimal(value["realized_pnl"]),
        opened_at=None if value["opened_at"] is None else _time(value["opened_at"]),
        as_of=_time(value["as_of"]),
        captured_at=_time(value["captured_at"]),
        evidence_class=str(value["evidence_class"]),
        source_record_type=str(value["source_record_type"]),
        source_record_id=str(value["source_record_id"]),
        source_canonical_hash=str(value["source_canonical_hash"]),
        unavailable_fields=_strings(value["unavailable_fields"]),
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _snapshot(row: RowMapping) -> PortfolioSnapshot:
    value = _data(row)
    return PortfolioSnapshot(
        snapshot_id=str(value["snapshot_id"]),
        version=int(value["version"]),
        as_of=_time(value["as_of"]),
        created_at=_time(value["created_at"]),
        account_balance_ids=_strings(value["account_balance_ids"]),
        position_ids=_strings(value["position_ids"]),
        unavailable_sources=tuple(
            UnavailablePortfolioSource(
                PortfolioVenue(item["venue"]),
                AccountKind(item["account_kind"]),
                str(item["reason"]),
            )
            for item in value["unavailable_sources"]
        ),
        total_known_cash=_optional_decimal(value["total_known_cash"]),
        total_known_reserved_cash=_optional_decimal(value["total_known_reserved_cash"]),
        total_known_position_value=_optional_decimal(value["total_known_position_value"]),
        total_known_portfolio_value=_optional_decimal(value["total_known_portfolio_value"]),
        exposure_by_venue=_pairs(value["exposure_by_venue"]),
        exposure_by_market_family=_pairs(value["exposure_by_market_family"]),
        exposure_by_instrument=_pairs(value["exposure_by_instrument"]),
        positions_with_unknown_exposure_value=_strings(
            value["positions_with_unknown_exposure_value"]
        ),
        completeness=PortfolioCompleteness(value["completeness"]),
        source_manifest_hash=str(value["source_manifest_hash"]),
        warnings=_strings(value["warnings"]),
        limitations=_strings(value["limitations"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


class PostgresPortfolioSnapshotRepository:
    """Implements ``application.portfolio_state.PortfolioStateRepository``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    @staticmethod
    def _existing_hash(
        connection: sa.Connection, table: sa.Table, identity: str, value: str
    ) -> str | None:
        return connection.scalar(
            sa.select(table.c.canonical_hash).where(table.c[identity] == value)
        )

    def portfolio_snapshot(self, snapshot_id: str) -> PortfolioSnapshot | None:
        row = self._one(
            sa.select(snapshot_table).where(snapshot_table.c.snapshot_id == snapshot_id)
        )
        return None if row is None else _snapshot(row)

    def portfolio_snapshot_active_as_of(self, as_of: datetime) -> PortfolioSnapshot | None:
        """Point-in-time replay contract: the most economically-relevant
        snapshot whose own ``as_of`` was at or before the given evaluation
        instant -- deliberately ordered by ``as_of`` (a caller-supplied
        economic timestamp, e.g. a day-scoped cycle cutoff or a deliberate
        backfill date), never by when the row happened to be inserted.
        Replaying a past evaluation must see only what was economically true
        by that point, regardless of database write order. Unaffected by
        the ``latest_portfolio_snapshot`` fix below -- this is a genuinely
        different, already-correct contract for its own purpose."""

        row = self._one(
            sa.select(snapshot_table)
            .where(snapshot_table.c.as_of <= as_of)
            .order_by(snapshot_table.c.as_of.desc(), snapshot_table.c.snapshot_id.desc())
            .limit(1)
        )
        return None if row is None else _snapshot(row)

    def latest_portfolio_snapshot(self) -> PortfolioSnapshot | None:
        """ "Right now" contract: the most recently *produced* snapshot,
        i.e. the freshest genuine observation Apex currently holds --
        deliberately distinct from ``portfolio_snapshot_active_as_of``'s
        point-in-time-replay contract above.

        Recurring Operating Health & Unattended Evidence Closure v1: this
        previously ordered by ``as_of DESC`` -- the same caller-supplied
        economic timestamp ``portfolio_snapshot_active_as_of`` correctly
        uses for replay. That was a genuine defect here specifically: a
        manually-built snapshot whose ``as_of`` happened to be set later
        (e.g. "now" at build time) could permanently shadow a genuinely
        fresher snapshot produced afterward with an earlier, day-scoped
        ``as_of`` (exactly what the recurring operating cycle uses), even
        though the second one was unambiguously created later.

        Fixed to order by ``created_at`` instead, which is safe against
        that ambiguity by construction, not by convention: every known
        writer (``BuildPortfolioSnapshot`` via both the recurring
        ``PORTFOLIO_SNAPSHOT_REFRESH`` task and the standalone
        ``portfolio-snapshot-build`` CLI command) always sets
        ``created_at`` from genuine wall-clock time at the moment of the
        call -- it is never caller-suppliable via any argument, unlike
        ``as_of``, which is deliberately caller-controlled for backfills,
        testing, and day-scoped cycle identity. A future-dated ``as_of``
        can never make a snapshot look artificially "latest" here.
        ``snapshot_id`` breaks an exact ``created_at`` tie deterministically
        (matching the precedent above), which is the only way two genuine
        writes could collide -- ``created_at`` itself can never be later
        than the moment it was set, so no snapshot can appear "created in
        the future" relative to a later query."""

        row = self._one(
            sa.select(snapshot_table)
            .order_by(snapshot_table.c.created_at.desc(), snapshot_table.c.snapshot_id.desc())
            .limit(1)
        )
        return None if row is None else _snapshot(row)

    def portfolio_account_balances(self, snapshot_id: str) -> tuple[PortfolioAccountBalance, ...]:
        snapshot = self.portfolio_snapshot(snapshot_id)
        if snapshot is None:
            return ()
        balances = tuple(
            balance
            for balance_id in snapshot.account_balance_ids
            if (balance := self._balance_by_id(balance_id)) is not None
        )
        if len(balances) != len(snapshot.account_balance_ids):
            raise PortfolioStateError("PORTFOLIO_SNAPSHOT_BALANCE_LINEAGE_INCOMPLETE")
        return balances

    def portfolio_positions(self, snapshot_id: str) -> tuple[PortfolioPosition, ...]:
        snapshot = self.portfolio_snapshot(snapshot_id)
        if snapshot is None:
            return ()
        positions = tuple(
            position
            for position_id in snapshot.position_ids
            if (position := self._position_by_id(position_id)) is not None
        )
        if len(positions) != len(snapshot.position_ids):
            raise PortfolioStateError("PORTFOLIO_SNAPSHOT_POSITION_LINEAGE_INCOMPLETE")
        return positions

    def _balance_by_id(self, balance_id: str) -> PortfolioAccountBalance | None:
        row = self._one(sa.select(balance_table).where(balance_table.c.balance_id == balance_id))
        return None if row is None else _balance(row)

    def _position_by_id(self, position_id: str) -> PortfolioPosition | None:
        row = self._one(
            sa.select(position_table).where(position_table.c.position_id == position_id)
        )
        return None if row is None else _position(row)

    def save_portfolio_snapshot(
        self,
        *,
        idempotency_key: str,
        snapshot: PortfolioSnapshot,
        balances: tuple[PortfolioAccountBalance, ...],
        positions: tuple[PortfolioPosition, ...],
    ) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = self._existing_hash(
                    connection, snapshot_table, "snapshot_id", snapshot.snapshot_id
                )
                if existing is not None:
                    retained_key = connection.scalar(
                        sa.select(snapshot_table.c.idempotency_key).where(
                            snapshot_table.c.snapshot_id == snapshot.snapshot_id
                        )
                    )
                    if existing != snapshot.canonical_hash or retained_key != idempotency_key:
                        raise PortfolioStateError("PORTFOLIO_SNAPSHOT_IDENTITY_CONFLICT")
                    return True
                if connection.scalar(
                    sa.select(snapshot_table.c.snapshot_id).where(
                        snapshot_table.c.idempotency_key == idempotency_key
                    )
                ):
                    raise PortfolioStateError("PORTFOLIO_SNAPSHOT_IDEMPOTENCY_CONFLICT")
                for balance in balances:
                    prior = self._existing_hash(
                        connection, balance_table, "balance_id", balance.balance_id
                    )
                    if prior is not None and prior != balance.canonical_hash:
                        raise PortfolioStateError("PORTFOLIO_BALANCE_IDENTITY_CONFLICT")
                    if prior is None:
                        connection.execute(
                            balance_table.insert().values(
                                balance_id=balance.balance_id,
                                venue=balance.venue.value,
                                account_kind=balance.account_kind.value,
                                account_id=balance.account_id,
                                as_of=balance.as_of,
                                captured_at=balance.captured_at,
                                payload=_payload(balance),
                                canonical_hash=balance.canonical_hash,
                                authority="NONE",
                            )
                        )
                for position in positions:
                    prior = self._existing_hash(
                        connection, position_table, "position_id", position.position_id
                    )
                    if prior is not None and prior != position.canonical_hash:
                        raise PortfolioStateError("PORTFOLIO_POSITION_IDENTITY_CONFLICT")
                    if prior is None:
                        connection.execute(
                            position_table.insert().values(
                                position_id=position.position_id,
                                venue=position.venue.value,
                                account_kind=position.account_kind.value,
                                account_id=position.account_id,
                                instrument_id=position.instrument_id,
                                market_family=position.market_family,
                                as_of=position.as_of,
                                captured_at=position.captured_at,
                                payload=_payload(position),
                                canonical_hash=position.canonical_hash,
                                authority="NONE",
                            )
                        )
                connection.execute(
                    snapshot_table.insert().values(
                        snapshot_id=snapshot.snapshot_id,
                        as_of=snapshot.as_of,
                        created_at=snapshot.created_at,
                        completeness=snapshot.completeness.value,
                        source_manifest_hash=snapshot.source_manifest_hash,
                        idempotency_key=idempotency_key,
                        payload=_payload(snapshot),
                        canonical_hash=snapshot.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise PortfolioStateError("PORTFOLIO_SNAPSHOT_PERSISTENCE_CONFLICT") from error

    def workspace(self) -> dict[str, object]:
        latest = self.latest_portfolio_snapshot()
        if latest is None:
            return {
                "portfolio_snapshot": None,
                "account_balances": (),
                "positions": (),
                "warnings": ("NO_PORTFOLIO_SNAPSHOT_RETAINED",),
                "authority": "NONE",
            }
        return {
            "portfolio_snapshot": latest,
            "account_balances": self.portfolio_account_balances(latest.snapshot_id),
            "positions": self.portfolio_positions(latest.snapshot_id),
            "warnings": latest.warnings,
            "authority": "NONE",
        }
