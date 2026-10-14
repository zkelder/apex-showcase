"""Append-only PostgreSQL adapter for Kalshi operating and PAPER evidence."""

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
from sqlalchemy.orm import sessionmaker

from apex.adapters.postgres import (
    binary_complement_evaluation,
    market_orderbook_level,
    market_orderbook_snapshot,
    normalized_market_snapshot,
)
from apex.application.kalshi_operating import KalshiOperatingRepository
from apex.domain.kalshi_operating import (
    KalshiCapability,
    KalshiCurrentOpportunity,
    KalshiEvidenceClass,
    KalshiOperatingError,
    KalshiPaperEligibility,
    KalshiPaperEligibilityScan,
    KalshiPaperOperation,
    KalshiPaperState,
    KalshiRefreshRecord,
    KalshiSide,
)
from apex.domain.values import canonical_json_bytes, deterministic_id, parse_decimal

metadata = sa.MetaData()

capability_table = sa.Table(
    "apex_kalshi_capability_observation",
    metadata,
    sa.Column("capability_id", sa.String(300), primary_key=True),
    sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("provider_requests", sa.Integer(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
)

refresh_table = sa.Table(
    "apex_kalshi_current_refresh",
    metadata,
    sa.Column("refresh_id", sa.String(300), primary_key=True),
    sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("evidence_class", sa.String(30), nullable=False),
    sa.Column("market_discovery_run_id", sa.String(300), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("provider_requests", sa.Integer(), nullable=False),
    sa.Column("provider_writes", sa.Integer(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
)

account_table = sa.Table(
    "apex_kalshi_paper_account",
    metadata,
    sa.Column("account_id", sa.String(300), primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("opening_cash", sa.Numeric(38, 8), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
)

state_table = sa.Table(
    "apex_kalshi_paper_state_snapshot",
    metadata,
    sa.Column("snapshot_id", sa.String(300), primary_key=True),
    sa.Column("account_id", sa.String(300), nullable=False),
    sa.Column("sequence", sa.Integer(), nullable=False),
    sa.Column("operation_type", sa.String(80), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("prior_state_hash", sa.String(64), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("provider_requests", sa.Integer(), nullable=False),
    sa.Column("provider_writes", sa.Integer(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
    sa.UniqueConstraint("account_id", "sequence"),
)

scan_table = sa.Table(
    "apex_kalshi_paper_eligibility_scan",
    metadata,
    sa.Column("scan_id", sa.String(300), primary_key=True),
    sa.Column("refresh_id", sa.String(300), nullable=False),
    sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("provider_requests", sa.Integer(), nullable=False),
    sa.Column("provider_writes", sa.Integer(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
)

TABLE_NAMES = tuple(metadata.tables)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise KalshiOperatingError("KALSHI_RETAINED_PAYLOAD_INVALID")
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
        if not isinstance(value, dict):
            raise KalshiOperatingError("KALSHI_RETAINED_DATACLASS_INVALID")
        hints = get_type_hints(type_hint)
        return type_hint(
            **{
                field.name: _restore(hints[field.name], value[field.name])
                for field in fields(type_hint)
            }
        )
    return value


def _object[T](row: RowMapping, result_type: type[T]) -> T:
    return cast(T, _restore(result_type, _data(row)))


class PostgresKalshiOperatingRepository(KalshiOperatingRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def retained_market(self, snapshot_id: str) -> dict[str, object] | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(normalized_market_snapshot).where(
                        normalized_market_snapshot.c.snapshot_id == snapshot_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else dict(row)

    def retained_orderbook(
        self, orderbook_snapshot_id: str
    ) -> tuple[dict[str, object], tuple[dict[str, object], ...]] | None:
        with self._engine.connect() as connection:
            book = (
                connection.execute(
                    sa.select(market_orderbook_snapshot).where(
                        market_orderbook_snapshot.c.orderbook_snapshot_id == orderbook_snapshot_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if book is None:
                return None
            levels = tuple(
                dict(row)
                for row in connection.execute(
                    sa.select(market_orderbook_level)
                    .where(market_orderbook_level.c.orderbook_snapshot_id == orderbook_snapshot_id)
                    .order_by(
                        market_orderbook_level.c.side,
                        market_orderbook_level.c.ordinal,
                    )
                ).mappings()
            )
        return dict(book), levels

    def retained_orderbook_liquidity(
        self,
        *,
        evidence_id: str,
        market_ticker: str,
        side: KalshiSide,
        observed_at: datetime,
        maximum_price: Decimal,
        exact_price: Decimal | None = None,
    ) -> int:
        with self._engine.connect() as connection:
            snapshot = (
                connection.execute(
                    sa.select(market_orderbook_snapshot).where(
                        market_orderbook_snapshot.c.orderbook_snapshot_id == evidence_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if snapshot is None:
                raise KalshiOperatingError("KALSHI_PAPER_RETAINED_ORDERBOOK_NOT_FOUND")
            if (
                snapshot["provider_market_ticker"] != market_ticker
                or snapshot["observed_at"] != observed_at
                or snapshot["data_quality_state"] != "VALID"
            ):
                raise KalshiOperatingError("KALSHI_PAPER_ORDERBOOK_EVIDENCE_MISMATCH")
            price_conditions = [
                market_orderbook_level.c.derived_ask_price <= maximum_price,
            ]
            if exact_price is not None:
                price_conditions.append(market_orderbook_level.c.derived_ask_price == exact_price)
            visible = connection.execute(
                sa.select(sa.func.coalesce(sa.func.sum(market_orderbook_level.c.quantity), 0))
                .where(market_orderbook_level.c.orderbook_snapshot_id == evidence_id)
                .where(market_orderbook_level.c.derived_ask_side == side.value)
                .where(*price_conditions)
            ).scalar_one()
        quantity = Decimal(visible)
        if quantity != quantity.to_integral_value():
            raise KalshiOperatingError("KALSHI_PAPER_NONINTEGRAL_VISIBLE_LIQUIDITY")
        return int(quantity)

    def capability(self, capability_id: str) -> KalshiCapability | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    sa.select(capability_table).where(
                        capability_table.c.capability_id == capability_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _object(row, KalshiCapability)

    def latest_capability(self) -> KalshiCapability | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    sa.select(capability_table)
                    .order_by(
                        capability_table.c.inspected_at.desc(),
                        capability_table.c.capability_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _object(row, KalshiCapability)

    def save_capability(self, *, capability: KalshiCapability, idempotency_key: str) -> bool:
        return self._insert_immutable(
            table=capability_table,
            identity=capability_table.c.capability_id,
            identity_value=capability.capability_id,
            idempotency_key=idempotency_key,
            canonical_hash=capability.canonical_hash,
            values={
                "capability_id": capability.capability_id,
                "inspected_at": capability.inspected_at,
                "idempotency_key": idempotency_key,
                "payload": _payload(capability),
                "canonical_hash": capability.canonical_hash,
                "provider_requests": capability.provider_requests,
                "authority": capability.authority,
                "live_authority": capability.live_authority,
            },
            conflict_reason="KALSHI_CAPABILITY_IDENTITY_CONFLICT",
        )

    def refresh(self, refresh_id: str) -> KalshiRefreshRecord | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    sa.select(refresh_table).where(refresh_table.c.refresh_id == refresh_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return cast(
            KalshiRefreshRecord,
            _restore(KalshiRefreshRecord, _data(row)["refresh"]),
        )

    def latest_refresh(self) -> KalshiRefreshRecord | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    sa.select(refresh_table)
                    .order_by(
                        refresh_table.c.as_of.desc(),
                        refresh_table.c.refresh_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return cast(
            KalshiRefreshRecord,
            _restore(KalshiRefreshRecord, _data(row)["refresh"]),
        )

    def opportunities_for_refresh(self, refresh_id: str) -> tuple[KalshiCurrentOpportunity, ...]:
        with self._session_factory() as session:
            row = (
                session.execute(
                    sa.select(refresh_table).where(refresh_table.c.refresh_id == refresh_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return ()
        return cast(
            tuple[KalshiCurrentOpportunity, ...],
            _restore(
                tuple[KalshiCurrentOpportunity, ...],
                _data(row)["opportunities"],
            ),
        )

    def save_refresh(
        self,
        *,
        refresh: KalshiRefreshRecord,
        opportunities: tuple[KalshiCurrentOpportunity, ...],
        idempotency_key: str,
    ) -> bool:
        return self._insert_immutable(
            table=refresh_table,
            identity=refresh_table.c.refresh_id,
            identity_value=refresh.refresh_id,
            idempotency_key=idempotency_key,
            canonical_hash=refresh.canonical_hash,
            values={
                "refresh_id": refresh.refresh_id,
                "as_of": refresh.as_of,
                "evidence_class": refresh.evidence_class.value,
                "market_discovery_run_id": refresh.market_discovery_run_id,
                "idempotency_key": idempotency_key,
                "payload": _payload({"refresh": refresh, "opportunities": opportunities}),
                "canonical_hash": refresh.canonical_hash,
                "provider_requests": refresh.provider_requests,
                "provider_writes": refresh.provider_writes,
                "authority": refresh.authority,
                "live_authority": refresh.live_authority,
            },
            conflict_reason="KALSHI_REFRESH_IDENTITY_CONFLICT",
        )

    def scan(self, scan_id: str) -> KalshiPaperEligibilityScan | None:
        with self._session_factory() as session:
            row = (
                session.execute(sa.select(scan_table).where(scan_table.c.scan_id == scan_id))
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return cast(
            KalshiPaperEligibilityScan,
            _restore(KalshiPaperEligibilityScan, _data(row)["scan"]),
        )

    def scan_decisions(self, scan_id: str) -> tuple[KalshiPaperEligibility, ...]:
        with self._session_factory() as session:
            row = (
                session.execute(sa.select(scan_table).where(scan_table.c.scan_id == scan_id))
                .mappings()
                .one_or_none()
            )
        if row is None:
            return ()
        return cast(
            tuple[KalshiPaperEligibility, ...],
            _restore(
                tuple[KalshiPaperEligibility, ...],
                _data(row)["decisions"],
            ),
        )

    def save_scan(
        self,
        *,
        scan: KalshiPaperEligibilityScan,
        decisions: tuple[KalshiPaperEligibility, ...],
        idempotency_key: str,
    ) -> bool:
        return self._insert_immutable(
            table=scan_table,
            identity=scan_table.c.scan_id,
            identity_value=scan.scan_id,
            idempotency_key=idempotency_key,
            canonical_hash=scan.canonical_hash,
            values={
                "scan_id": scan.scan_id,
                "refresh_id": scan.refresh_id,
                "evaluated_at": scan.evaluated_at,
                "idempotency_key": idempotency_key,
                "payload": _payload({"scan": scan, "decisions": decisions}),
                "canonical_hash": scan.canonical_hash,
                "provider_requests": scan.provider_requests,
                "provider_writes": 0,
                "authority": scan.authority,
                "live_authority": scan.live_authority,
            },
            conflict_reason="KALSHI_PAPER_SCAN_IDENTITY_CONFLICT",
        )

    def state(self, account_id: str) -> KalshiPaperState | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    sa.select(state_table)
                    .where(state_table.c.account_id == account_id)
                    .order_by(state_table.c.sequence.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _object(row, KalshiPaperState)

    def account_ids(self, *, limit: int = 100, offset: int = 0) -> tuple[str, ...]:
        """Return a bounded identity-ordered inventory for read-only operator views."""

        if not 1 <= limit <= 100 or offset < 0:
            raise KalshiOperatingError("KALSHI_PAPER_ACCOUNT_PAGINATION_INVALID")
        with self._session_factory() as session:
            rows = session.scalars(
                sa.select(account_table.c.account_id)
                .order_by(account_table.c.account_id)
                .limit(limit)
                .offset(offset)
            ).all()
        return tuple(str(value) for value in rows)

    def save_account(self, *, state: KalshiPaperState, idempotency_key: str) -> bool:
        account = state.account
        with self._session_factory() as session, session.begin():
            retained = (
                session.execute(
                    sa.select(account_table).where(account_table.c.account_id == account.account_id)
                )
                .mappings()
                .one_or_none()
            )
            if retained is not None:
                if (
                    str(retained["canonical_hash"]) != account.canonical_hash
                    or str(retained["idempotency_key"]) != idempotency_key
                ):
                    raise KalshiOperatingError("KALSHI_PAPER_ACCOUNT_IDENTITY_CONFLICT")
                return True
            try:
                session.execute(
                    account_table.insert().values(
                        account_id=account.account_id,
                        created_at=account.created_at,
                        opening_cash=account.opening_cash,
                        idempotency_key=idempotency_key,
                        payload=_payload(account),
                        canonical_hash=account.canonical_hash,
                        authority=account.authority,
                        live_authority=account.live_authority,
                    )
                )
                session.execute(
                    state_table.insert().values(
                        snapshot_id=deterministic_id(
                            "kalshi_paper_state",
                            {"account_id": account.account_id, "sequence": 1},
                        ),
                        account_id=account.account_id,
                        sequence=1,
                        operation_type="CREATE_ACCOUNT",
                        idempotency_key=idempotency_key,
                        prior_state_hash=None,
                        created_at=account.created_at,
                        payload=_payload(state),
                        canonical_hash=state.canonical_hash,
                        provider_requests=0,
                        provider_writes=0,
                        authority=state.authority,
                        live_authority=state.live_authority,
                    )
                )
            except IntegrityError as error:
                raise KalshiOperatingError("KALSHI_PAPER_ACCOUNT_IDENTITY_CONFLICT") from error
        return False

    def save_state(
        self,
        *,
        state: KalshiPaperState,
        prior_state_hash: str,
        operation: KalshiPaperOperation,
    ) -> bool:
        with self._session_factory() as session, session.begin():
            account = session.execute(
                sa.select(account_table.c.account_id)
                .where(account_table.c.account_id == state.account.account_id)
                .with_for_update()
            ).one_or_none()
            if account is None:
                raise KalshiOperatingError("KALSHI_PAPER_ACCOUNT_NOT_FOUND")
            replay = (
                session.execute(
                    sa.select(state_table).where(
                        state_table.c.idempotency_key == operation.idempotency_key
                    )
                )
                .mappings()
                .one_or_none()
            )
            if replay is not None:
                retained = _object(replay, KalshiPaperState)
                retained_operation = next(
                    (
                        item
                        for item in retained.operations
                        if item.idempotency_key == operation.idempotency_key
                    ),
                    None,
                )
                if retained_operation != operation:
                    raise KalshiOperatingError("KALSHI_PAPER_IDEMPOTENCY_CONFLICT")
                return True
            latest = (
                session.execute(
                    sa.select(state_table)
                    .where(state_table.c.account_id == state.account.account_id)
                    .order_by(state_table.c.sequence.desc())
                    .limit(1)
                )
                .mappings()
                .one()
            )
            if str(latest["canonical_hash"]) != prior_state_hash:
                raise KalshiOperatingError("KALSHI_PAPER_STATE_CONCURRENCY_CONFLICT")
            sequence = int(latest["sequence"]) + 1
            try:
                session.execute(
                    state_table.insert().values(
                        snapshot_id=deterministic_id(
                            "kalshi_paper_state",
                            {
                                "account_id": state.account.account_id,
                                "sequence": sequence,
                            },
                        ),
                        account_id=state.account.account_id,
                        sequence=sequence,
                        operation_type=operation.operation_type,
                        idempotency_key=operation.idempotency_key,
                        prior_state_hash=prior_state_hash,
                        created_at=state.updated_at,
                        payload=_payload(state),
                        canonical_hash=state.canonical_hash,
                        provider_requests=0,
                        provider_writes=0,
                        authority=state.authority,
                        live_authority=state.live_authority,
                    )
                )
            except IntegrityError as error:
                raise KalshiOperatingError("KALSHI_PAPER_STATE_CONFLICT") from error
        return False

    def _insert_immutable(
        self,
        *,
        table: sa.Table,
        identity: sa.Column[Any],
        identity_value: str,
        idempotency_key: str,
        canonical_hash: str,
        values: dict[str, object],
        conflict_reason: str,
    ) -> bool:
        with self._session_factory() as session, session.begin():
            retained = (
                session.execute(sa.select(table).where(identity == identity_value))
                .mappings()
                .one_or_none()
            )
            if retained is not None:
                if (
                    str(retained["canonical_hash"]) != canonical_hash
                    or str(retained["idempotency_key"]) != idempotency_key
                ):
                    raise KalshiOperatingError(conflict_reason)
                return True
            try:
                session.execute(table.insert().values(**values))
            except IntegrityError as error:
                raise KalshiOperatingError("KALSHI_IDEMPOTENCY_CONFLICT") from error
        return False


class PostgresKalshiCurrentOpportunitySource:
    """Project exact retained complement rows; no provider or financial access."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def build(
        self,
        *,
        refresh_id: str,
        evidence_class: KalshiEvidenceClass,
        evaluation_ids: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[KalshiCurrentOpportunity, ...]:
        values: list[KalshiCurrentOpportunity] = []
        with self._engine.connect() as connection:
            for evaluation_id in evaluation_ids:
                row = (
                    connection.execute(
                        sa.select(
                            binary_complement_evaluation,
                            normalized_market_snapshot.c.event_ticker,
                            normalized_market_snapshot.c.status.label("market_status"),
                            normalized_market_snapshot.c.close_time,
                            normalized_market_snapshot.c.latest_expiration_time,
                            normalized_market_snapshot.c.rule_content_hash,
                            normalized_market_snapshot.c.canonical_snapshot_hash,
                            normalized_market_snapshot.c.data_quality_state.label("market_quality"),
                            market_orderbook_snapshot.c.data_quality_state.label(
                                "orderbook_quality"
                            ),
                            market_orderbook_snapshot.c.yes_bid_level_count,
                            market_orderbook_snapshot.c.no_bid_level_count,
                            market_orderbook_snapshot.c.canonical_orderbook_hash,
                        )
                        .join(
                            normalized_market_snapshot,
                            normalized_market_snapshot.c.snapshot_id
                            == binary_complement_evaluation.c.market_snapshot_id,
                        )
                        .join(
                            market_orderbook_snapshot,
                            market_orderbook_snapshot.c.orderbook_snapshot_id
                            == binary_complement_evaluation.c.orderbook_snapshot_id,
                        )
                        .where(binary_complement_evaluation.c.evaluation_id == evaluation_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise KalshiOperatingError("KALSHI_CURRENT_EVALUATION_NOT_FOUND")
                observed = cast(datetime, row["evaluated_at"])
                close = cast(datetime | None, row["close_time"]) or observed
                settlement = cast(datetime | None, row["latest_expiration_time"]) or close
                levels = connection.execute(
                    sa.select(
                        market_orderbook_level.c.derived_ask_side,
                        market_orderbook_level.c.quantity,
                    ).where(
                        market_orderbook_level.c.orderbook_snapshot_id
                        == row["orderbook_snapshot_id"]
                    )
                ).all()
                liquidity = {
                    side: int(
                        sum(
                            (
                                cast(Decimal, level.quantity)
                                for level in levels
                                if str(level.derived_ask_side) == side
                            ),
                            Decimal(0),
                        )
                    )
                    for side in ("YES", "NO")
                }
                common_reasons = tuple(
                    value
                    for value in (
                        (
                            None
                            if str(row["state"]) == "CANDIDATE"
                            else f"SOURCE_EVALUATION_{row['state']}"
                        ),
                        (
                            None
                            if str(row["market_quality"]) == "ELIGIBLE"
                            else "MARKET_QUALITY_DISQUALIFYING"
                        ),
                        (
                            None
                            if str(row["orderbook_quality"]) == "VALID"
                            else "ORDERBOOK_QUALITY_DISQUALIFYING"
                        ),
                        (None if row["event_ticker"] is not None else "EVENT_IDENTITY_UNAVAILABLE"),
                        (
                            None
                            if row["latest_expiration_time"] is not None
                            else "SETTLEMENT_METADATA_UNAVAILABLE"
                        ),
                    )
                    if value is not None
                )
                for side, price_column in (
                    (KalshiSide.YES, "weighted_yes_ask"),
                    (KalshiSide.NO, "weighted_no_ask"),
                ):
                    raw_price = cast(Decimal | None, row[price_column])
                    values.append(
                        KalshiCurrentOpportunity.create(
                            opportunity_id=(f"kalshi-current-{evaluation_id}-{side.value.lower()}"),
                            refresh_id=refresh_id,
                            evaluation_id=evaluation_id,
                            evidence_class=evidence_class,
                            market_ticker=str(row["provider_market_ticker"]),
                            event_ticker=str(row["event_ticker"] or row["provider_market_ticker"]),
                            rule_hash=str(row["rule_content_hash"] or ""),
                            side=side,
                            price=Decimal(0) if raw_price is None else raw_price,
                            visible_liquidity=liquidity[side.value],
                            close_at=close,
                            settlement_at=settlement,
                            evidence_observed_at=observed,
                            as_of=as_of,
                            supporting_evidence_ids=(
                                str(row["market_snapshot_id"]),
                                str(row["orderbook_snapshot_id"]),
                                str(row["canonical_snapshot_hash"]),
                                str(row["canonical_orderbook_hash"]),
                                evaluation_id,
                                *(
                                    ()
                                    if row["fee_snapshot_id"] is None
                                    else (str(row["fee_snapshot_id"]),)
                                ),
                            ),
                            market_open=str(row["market_status"]) == "open",
                            book_complete=(
                                int(row["yes_bid_level_count"]) > 0
                                and int(row["no_bid_level_count"]) > 0
                            ),
                            fee_retained=row["fee_snapshot_id"] is not None,
                            disqualifying_reasons=common_reasons,
                        )
                    )
        return tuple(values)
