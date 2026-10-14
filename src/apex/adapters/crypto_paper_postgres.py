"""Append-only PostgreSQL persistence for internal Crypto Spot PAPER state."""

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

from apex.application.crypto_paper import CryptoPaperRepository
from apex.domain.crypto_paper import (
    CryptoPaperAccount,
    CryptoPaperCapability,
    CryptoPaperEligibility,
    CryptoPaperEligibilityScan,
    CryptoPaperError,
    CryptoPaperOperation,
    CryptoPaperProviderContract,
    CryptoPaperState,
)
from apex.domain.values import canonical_json_bytes, parse_decimal

metadata = sa.MetaData()

contract_table = sa.Table(
    "apex_crypto_paper_provider_contract",
    metadata,
    sa.Column("contract_id", sa.String(300), primary_key=True),
    sa.Column("product_id", sa.String(300), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("provider_requests", sa.Integer(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
)

capability_table = sa.Table(
    "apex_crypto_paper_capability_observation",
    metadata,
    sa.Column("capability_id", sa.String(300), primary_key=True),
    sa.Column("contract_id", sa.String(300), nullable=True),
    sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("provider_requests", sa.Integer(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
)

account_table = sa.Table(
    "apex_crypto_paper_account",
    metadata,
    sa.Column("account_id", sa.String(300), primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
)

state_table = sa.Table(
    "apex_crypto_paper_state_snapshot",
    metadata,
    sa.Column("snapshot_id", sa.String(300), primary_key=True),
    sa.Column("account_id", sa.String(300), nullable=False),
    sa.Column("sequence", sa.Integer(), nullable=False),
    sa.Column("operation_type", sa.String(60), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("prior_state_hash", sa.String(64), nullable=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("provider_requests", sa.Integer(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
    sa.UniqueConstraint(
        "account_id",
        "sequence",
        name="uq_crypto_paper_state_account_sequence",
    ),
)

scan_table = sa.Table(
    "apex_crypto_paper_eligibility_scan",
    metadata,
    sa.Column("scan_id", sa.String(300), primary_key=True),
    sa.Column("opportunity_set_id", sa.String(300), nullable=False),
    sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("provider_requests", sa.Integer(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
    sa.Column("live_authority", sa.String(16), nullable=False),
)

TABLE_NAMES = tuple(metadata.tables)


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise CryptoPaperError("CRYPTO_PAPER_RETAINED_PAYLOAD_INVALID")
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
            raise CryptoPaperError("CRYPTO_PAPER_RETAINED_DATACLASS_INVALID")
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


class PostgresCryptoPaperRepository(CryptoPaperRepository):
    def __init__(self, engine: Engine) -> None:
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def contract(self, contract_id: str) -> CryptoPaperProviderContract | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    sa.select(contract_table).where(contract_table.c.contract_id == contract_id)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _object(row, CryptoPaperProviderContract)

    def save_contract(
        self,
        *,
        contract: CryptoPaperProviderContract,
        idempotency_key: str,
    ) -> bool:
        values: dict[str, object] = {
            "contract_id": contract.contract_id,
            "product_id": contract.product_id,
            "idempotency_key": idempotency_key,
            "payload": _payload(contract),
            "canonical_hash": contract.canonical_hash,
            "provider_requests": 0,
            "authority": contract.authority,
            "live_authority": contract.live_authority,
        }
        return self._insert_immutable(
            table=contract_table,
            identity=contract_table.c.contract_id,
            identity_value=contract.contract_id,
            idempotency_key=idempotency_key,
            canonical_hash=contract.canonical_hash,
            values=values,
            conflict_reason="CRYPTO_PAPER_CONTRACT_IDENTITY_CONFLICT",
        )

    def capability(self, capability_id: str) -> CryptoPaperCapability | None:
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
        return None if row is None else _object(row, CryptoPaperCapability)

    def latest_capability(self) -> CryptoPaperCapability | None:
        """Read the newest retained capability observation without probing a provider."""

        with self._session_factory() as session:
            row = (
                session.execute(
                    sa.select(capability_table)
                    .order_by(capability_table.c.inspected_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _object(row, CryptoPaperCapability)

    def save_capability(
        self,
        *,
        capability: CryptoPaperCapability,
        idempotency_key: str,
    ) -> bool:
        values: dict[str, object] = {
            "capability_id": capability.capability_id,
            "contract_id": capability.contract_id,
            "inspected_at": capability.inspected_at,
            "idempotency_key": idempotency_key,
            "payload": _payload(capability),
            "canonical_hash": capability.canonical_hash,
            "provider_requests": capability.provider_requests,
            "authority": capability.paper_authority,
            "live_authority": capability.live_authority,
        }
        return self._insert_immutable(
            table=capability_table,
            identity=capability_table.c.capability_id,
            identity_value=capability.capability_id,
            idempotency_key=idempotency_key,
            canonical_hash=capability.canonical_hash,
            values=values,
            conflict_reason="CRYPTO_PAPER_CAPABILITY_IDENTITY_CONFLICT",
        )

    def account(self, account_id: str) -> CryptoPaperAccount | None:
        with self._session_factory() as session:
            row = (
                session.execute(
                    sa.select(account_table).where(account_table.c.account_id == account_id)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _object(row, CryptoPaperAccount)

    def accounts(self, *, limit: int = 100, offset: int = 0) -> tuple[CryptoPaperAccount, ...]:
        """Return a bounded identity-ordered inventory for read-only operator views."""

        if not 1 <= limit <= 100 or offset < 0:
            raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_PAGINATION_INVALID")
        with self._session_factory() as session:
            rows = (
                session.execute(
                    sa.select(account_table)
                    .order_by(account_table.c.account_id)
                    .limit(limit)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        return tuple(_object(row, CryptoPaperAccount) for row in rows)

    def state(self, account_id: str) -> CryptoPaperState | None:
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
        return None if row is None else _object(row, CryptoPaperState)

    def save_account(
        self,
        *,
        account: CryptoPaperAccount,
        state: CryptoPaperState,
        idempotency_key: str,
    ) -> bool:
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
                    raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_IDENTITY_CONFLICT")
                return True
            try:
                session.execute(
                    account_table.insert().values(
                        account_id=account.account_id,
                        created_at=account.created_at,
                        idempotency_key=idempotency_key,
                        payload=_payload(account),
                        canonical_hash=account.canonical_hash,
                        authority=account.authority,
                        live_authority=account.live_authority,
                    )
                )
                session.execute(
                    state_table.insert().values(
                        snapshot_id=f"{account.account_id}-state-1",
                        account_id=account.account_id,
                        sequence=1,
                        operation_type="CREATE_ACCOUNT",
                        idempotency_key=f"{idempotency_key}:state",
                        prior_state_hash=None,
                        payload=_payload(state),
                        canonical_hash=state.canonical_hash,
                        created_at=account.created_at,
                        provider_requests=0,
                        authority=state.authority,
                        live_authority=state.live_authority,
                    )
                )
            except IntegrityError as error:
                raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_IDENTITY_CONFLICT") from error
        return False

    def save_state(
        self,
        *,
        state: CryptoPaperState,
        prior_state_hash: str,
        operation: CryptoPaperOperation,
    ) -> bool:
        with self._session_factory() as session, session.begin():
            account_row = session.execute(
                sa.select(account_table.c.account_id)
                .where(account_table.c.account_id == state.account.account_id)
                .with_for_update()
            ).one_or_none()
            if account_row is None:
                raise CryptoPaperError("CRYPTO_PAPER_ACCOUNT_NOT_FOUND")
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
                retained = _object(replay, CryptoPaperState)
                retained_operation = next(
                    (
                        item
                        for item in retained.operations
                        if item.idempotency_key == operation.idempotency_key
                    ),
                    None,
                )
                if retained_operation != operation:
                    raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT")
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
                raise CryptoPaperError("CRYPTO_PAPER_STATE_CONCURRENCY_CONFLICT")
            sequence = int(latest["sequence"]) + 1
            try:
                session.execute(
                    state_table.insert().values(
                        snapshot_id=f"{state.account.account_id}-state-{sequence}",
                        account_id=state.account.account_id,
                        sequence=sequence,
                        operation_type=operation.operation_type,
                        idempotency_key=operation.idempotency_key,
                        prior_state_hash=prior_state_hash,
                        payload=_payload(state),
                        canonical_hash=state.canonical_hash,
                        created_at=state.account_state.updated_at,
                        provider_requests=0,
                        authority=state.authority,
                        live_authority=state.live_authority,
                    )
                )
            except IntegrityError as error:
                raise CryptoPaperError("CRYPTO_PAPER_STATE_CONFLICT") from error
        return False

    def scan(self, scan_id: str) -> CryptoPaperEligibilityScan | None:
        with self._session_factory() as session:
            row = (
                session.execute(sa.select(scan_table).where(scan_table.c.scan_id == scan_id))
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return cast(
            CryptoPaperEligibilityScan,
            _restore(CryptoPaperEligibilityScan, _data(row)["scan"]),
        )

    def scan_decisions(self, scan_id: str) -> tuple[CryptoPaperEligibility, ...]:
        with self._session_factory() as session:
            row = (
                session.execute(sa.select(scan_table).where(scan_table.c.scan_id == scan_id))
                .mappings()
                .one_or_none()
            )
        if row is None:
            return ()
        return cast(
            tuple[CryptoPaperEligibility, ...],
            _restore(
                tuple[CryptoPaperEligibility, ...],
                _data(row)["decisions"],
            ),
        )

    def save_scan(
        self,
        *,
        scan: CryptoPaperEligibilityScan,
        decisions: tuple[CryptoPaperEligibility, ...],
        idempotency_key: str,
    ) -> bool:
        values = {
            "scan_id": scan.scan_id,
            "opportunity_set_id": scan.opportunity_set_id,
            "evaluated_at": scan.evaluated_at,
            "idempotency_key": idempotency_key,
            "payload": _payload({"scan": scan, "decisions": decisions}),
            "canonical_hash": scan.canonical_hash,
            "provider_requests": scan.provider_requests,
            "authority": scan.authority,
            "live_authority": scan.live_authority,
        }
        return self._insert_immutable(
            table=scan_table,
            identity=scan_table.c.scan_id,
            identity_value=scan.scan_id,
            idempotency_key=idempotency_key,
            canonical_hash=scan.canonical_hash,
            values=values,
            conflict_reason="CRYPTO_PAPER_SCAN_IDENTITY_CONFLICT",
        )

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
                    raise CryptoPaperError(conflict_reason)
                return True
            try:
                session.execute(table.insert().values(**values))
            except IntegrityError as error:
                raise CryptoPaperError("CRYPTO_PAPER_IDEMPOTENCY_CONFLICT") from error
        return False
