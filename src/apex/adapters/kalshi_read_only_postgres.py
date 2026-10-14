"""Immutable PostgreSQL evidence for Kalshi private read-only bring-up."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.application.kalshi_read_only import KalshiReadOnlyRepository
from apex.domain.kalshi_read_only import (
    KalshiBalanceEvidence,
    KalshiCapabilityCapture,
    KalshiCapabilityClass,
    KalshiCapabilityRefresh,
    KalshiEndpointClass,
    KalshiInspectionAttempt,
    KalshiInspectionStatus,
    KalshiReadApproval,
    KalshiReadOnlyError,
    KalshiRefreshDisposition,
    KalshiRequestEvidence,
)
from apex.domain.live_readiness import Provider, ProviderAccountStatus

kalshi_read_only_metadata = sa.MetaData()


def _table(name: str, identity: str, *columns: Any) -> sa.Table:
    table = sa.Table(
        name,
        kalshi_read_only_metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text, nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("provider_write_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("authority", sa.String(20), nullable=False, server_default="NONE"),
        sa.CheckConstraint("length(canonical_hash) = 64", name=f"ck_{name}_hash"),
        sa.CheckConstraint("provider_write_count = 0", name=f"ck_{name}_zero_writes"),
        sa.CheckConstraint("authority = 'NONE'", name=f"ck_{name}_authority"),
    )
    table.append_constraint(
        sa.CheckConstraint(
            "lower(payload) NOT LIKE '%\"api_key\":%' "
            "AND lower(payload) NOT LIKE '%\"api_key_id\":%' "
            "AND lower(payload) NOT LIKE '%\"key_id\":%' "
            "AND lower(payload) NOT LIKE '%\"private_key\":%' "
            "AND lower(payload) NOT LIKE '%\"authorization\":%' "
            "AND lower(payload) NOT LIKE '%kalshi-access-%' "
            "AND lower(payload) NOT LIKE '%\"signature\":%' "
            "AND lower(payload) NOT LIKE '%\"bearer\":%' "
            "AND lower(payload) NOT LIKE '%-----begin %' "
            "AND lower(payload) NOT LIKE '%bearer %'",
            name=f"ck_{name}_no_secret_material",
        )
    )
    table.append_constraint(
        sa.CheckConstraint(
            "lower(payload) NOT LIKE '%/orders%' "
            "AND lower(payload) NOT LIKE '%/withdraw%' "
            "AND lower(payload) NOT LIKE '%/transfer%' "
            "AND lower(payload) NOT LIKE '%/cancel%'",
            name=f"ck_{name}_no_mutation_path",
        )
    )
    return table


kalshi_read_approval_table = _table(
    "kalshi_private_read_approval",
    "approval_id",
    sa.Column("credential_reference_id", sa.String(300), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("read_only_only", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("zero_provider_writes", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("preview_allowed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("trade_allowed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("cancellation_allowed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("fund_movement_allowed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.CheckConstraint("read_only_only", name="ck_kalshi_private_approval_read_only"),
    sa.CheckConstraint(
        "zero_provider_writes AND NOT preview_allowed AND NOT trade_allowed "
        "AND NOT cancellation_allowed AND NOT fund_movement_allowed",
        name="ck_kalshi_private_approval_zero_authority",
    ),
)
kalshi_read_attempt_table = _table(
    "kalshi_private_read_attempt",
    "attempt_id",
    sa.Column("approval_id", sa.String(300), nullable=True),
    sa.Column("status", sa.String(40), nullable=False),
    sa.Column("request_count", sa.Integer, nullable=False),
    sa.CheckConstraint(
        "request_count >= 0 AND request_count <= 10", name="ck_kalshi_attempt_request_bound"
    ),
)
kalshi_capability_capture_table = _table(
    "kalshi_read_only_capability_capture",
    "capture_id",
    sa.Column("approval_id", sa.String(300), nullable=False),
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("request_count", sa.Integer, nullable=False),
    sa.CheckConstraint(
        "request_count > 0 AND request_count <= 10", name="ck_kalshi_capture_request_bound"
    ),
)
kalshi_capability_refresh_table = _table(
    "kalshi_read_only_capability_refresh",
    "refresh_id",
    sa.Column("approval_id", sa.String(300), nullable=True),
    sa.Column("request_count", sa.Integer, nullable=False),
    sa.CheckConstraint(
        "request_count >= 0 AND request_count <= 10", name="ck_kalshi_refresh_request_bound"
    ),
)

KALSHI_READ_ONLY_TABLES = tuple(kalshi_read_only_metadata.tables)


def _json(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _payload(value: object) -> str:
    return json.dumps(_json(asdict(cast(Any, value))), sort_keys=True, separators=(",", ":"))


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise KalshiReadOnlyError("KALSHI_RETAINED_PAYLOAD_INVALID")
    return cast(dict[str, Any], value)


def _dt(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _approval(row: RowMapping) -> KalshiReadApproval:
    value = _data(row)
    return KalshiReadApproval(
        approval_id=str(value["approval_id"]),
        provider=Provider(str(value["provider"])),
        credential_reference_id=str(value["credential_reference_id"]),
        account_purpose=str(value["account_purpose"]),
        allowed_endpoint_classes=tuple(
            KalshiEndpointClass(item) for item in value["allowed_endpoint_classes"]
        ),
        maximum_request_count=int(value["maximum_request_count"]),
        timeout_seconds=int(value["timeout_seconds"]),
        approved_at=_dt(value["approved_at"]),
        expires_at=_dt(value["expires_at"]),
        actor=str(value["actor"]),
        reason=str(value["reason"]),
        idempotency_key=str(value["idempotency_key"]),
        read_only_only=bool(value["read_only_only"]),
        zero_provider_writes=bool(value["zero_provider_writes"]),
        preview_allowed=bool(value["preview_allowed"]),
        trade_allowed=bool(value["trade_allowed"]),
        cancellation_allowed=bool(value["cancellation_allowed"]),
        fund_movement_allowed=bool(value["fund_movement_allowed"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
    )


def _request(value: dict[str, Any]) -> KalshiRequestEvidence:
    return KalshiRequestEvidence(
        request_id=str(value["request_id"]),
        endpoint_class=KalshiEndpointClass(str(value["endpoint_class"])),
        method=str(value["method"]),
        host=str(value["host"]),
        path=str(value["path"]),
        response_status=int(value["response_status"]),
        response_hash=str(value["response_hash"]),
        requested_at=_dt(value["requested_at"]),
        completed_at=_dt(value["completed_at"]),
        response_bytes=int(value["response_bytes"]),
        redacted=bool(value["redacted"]),
        provider_write_count=int(value["provider_write_count"]),
    )


def _balance(value: dict[str, Any]) -> KalshiBalanceEvidence:
    return KalshiBalanceEvidence(
        currency=str(value["currency"]),
        available_amount=Decimal(str(value["available_amount"])),
        portfolio_value=None
        if value["portfolio_value"] is None
        else Decimal(str(value["portfolio_value"])),
        observed_at=_dt(value["observed_at"]),
        source_hash=str(value["source_hash"]),
    )


def _capture(row: RowMapping) -> KalshiCapabilityCapture:
    value = _data(row)
    return KalshiCapabilityCapture(
        capture_id=str(value["capture_id"]),
        approval_id=str(value["approval_id"]),
        credential_reference_id=str(value["credential_reference_id"]),
        account_purpose=str(value["account_purpose"]),
        redacted_account_fingerprint=str(value["redacted_account_fingerprint"]),
        account_status=ProviderAccountStatus(str(value["account_status"])),
        account_type=str(value["account_type"]),
        permission_evidence=tuple((str(a), str(b)) for a, b in value["permission_evidence"]),
        capability_classes=tuple(
            KalshiCapabilityClass(item) for item in value["capability_classes"]
        ),
        balances=tuple(_balance(item) for item in value["balances"]),
        maker_fee_rate=None
        if value["maker_fee_rate"] is None
        else Decimal(str(value["maker_fee_rate"])),
        taker_fee_rate=None
        if value["taker_fee_rate"] is None
        else Decimal(str(value["taker_fee_rate"])),
        account_tier=None if value["account_tier"] is None else str(value["account_tier"]),
        fee_limitations=tuple(str(item) for item in value["fee_limitations"]),
        account_restrictions=tuple(str(item) for item in value["account_restrictions"]),
        requests=tuple(_request(item) for item in value["requests"]),
        observed_at=_dt(value["observed_at"]),
        fresh_until=_dt(value["fresh_until"]),
        redaction_proof=tuple(str(item) for item in value["redaction_proof"]),
        limitations=tuple(str(item) for item in value["limitations"]),
        warnings=tuple(str(item) for item in value["warnings"]),
        canonical_hash=str(value["canonical_hash"]),
        provider_request_count=int(value["provider_request_count"]),
        provider_write_count=int(value["provider_write_count"]),
        authority=str(value["authority"]),
        capital_authority=str(value["capital_authority"]),
        live_authority=str(value["live_authority"]),
    )


def _attempt(row: RowMapping) -> KalshiInspectionAttempt:
    value = _data(row)
    return KalshiInspectionAttempt(
        attempt_id=str(value["attempt_id"]),
        approval_id=None if value["approval_id"] is None else str(value["approval_id"]),
        credential_reference_id=None
        if value["credential_reference_id"] is None
        else str(value["credential_reference_id"]),
        status=KalshiInspectionStatus(str(value["status"])),
        blocked_reason=None if value["blocked_reason"] is None else str(value["blocked_reason"]),
        capture_id=None if value["capture_id"] is None else str(value["capture_id"]),
        request_count=int(value["request_count"]),
        provider_write_count=int(value["provider_write_count"]),
        warnings=tuple(str(item) for item in value["warnings"]),
        attempted_at=_dt(value["attempted_at"]),
        canonical_hash=str(value["canonical_hash"]),
        authority=str(value["authority"]),
    )


def _refresh(row: RowMapping) -> KalshiCapabilityRefresh:
    value = _data(row)
    return KalshiCapabilityRefresh(
        refresh_id=str(value["refresh_id"]),
        due=bool(value["due"]),
        disposition=KalshiRefreshDisposition(str(value["disposition"])),
        reasons=tuple(str(item) for item in value["reasons"]),
        approval_id=None if value["approval_id"] is None else str(value["approval_id"]),
        request_count=int(value["request_count"]),
        output_id=None if value["output_id"] is None else str(value["output_id"]),
        status=str(value["status"]),
        warnings=tuple(str(item) for item in value["warnings"]),
        evaluated_at=_dt(value["evaluated_at"]),
        canonical_hash=str(value["canonical_hash"]),
        provider_write_count=int(value["provider_write_count"]),
        authority=str(value["authority"]),
    )


T = TypeVar("T")


class PostgresKalshiReadOnlyRepository(KalshiReadOnlyRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _save(self, table: sa.Table, identity: str, value: T) -> T:
        key = str(getattr(value, identity))
        canonical_hash = str(cast(Any, value).canonical_hash)
        with self._engine.begin() as connection:
            existing = (
                connection.execute(sa.select(table).where(table.c[identity] == key))
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if str(existing["canonical_hash"]) != canonical_hash:
                    raise KalshiReadOnlyError("KALSHI_READ_ONLY_ID_CONFLICT")
                return value
            columns: dict[str, object] = {
                identity: key,
                "payload": _payload(value),
                "canonical_hash": canonical_hash,
                "provider_write_count": 0,
                "authority": "NONE",
            }
            if isinstance(value, KalshiReadApproval):
                columns.update(
                    credential_reference_id=value.credential_reference_id,
                    expires_at=value.expires_at,
                    read_only_only=True,
                    zero_provider_writes=True,
                    preview_allowed=False,
                    trade_allowed=False,
                    cancellation_allowed=False,
                    fund_movement_allowed=False,
                )
            elif isinstance(value, KalshiInspectionAttempt):
                columns.update(
                    approval_id=value.approval_id,
                    status=value.status.value,
                    request_count=value.request_count,
                )
            elif isinstance(value, KalshiCapabilityCapture):
                columns.update(
                    approval_id=value.approval_id,
                    observed_at=value.observed_at,
                    request_count=value.provider_request_count,
                )
            elif isinstance(value, KalshiCapabilityRefresh):
                columns.update(approval_id=value.approval_id, request_count=value.request_count)
            connection.execute(table.insert().values(**columns))
        return value

    def _one(
        self, table: sa.Table, identity: str, key: str, loader: Callable[[RowMapping], T]
    ) -> T | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(sa.select(table).where(table.c[identity] == key))
                .mappings()
                .one_or_none()
            )
        return None if row is None else loader(row)

    def _all(self, table: sa.Table, loader: Callable[[RowMapping], T]) -> tuple[T, ...]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(sa.select(table).order_by(next(iter(table.primary_key.columns))))
                .mappings()
                .all()
            )
        return tuple(loader(row) for row in rows)

    def save_approval(self, value: KalshiReadApproval) -> KalshiReadApproval:
        return self._save(kalshi_read_approval_table, "approval_id", value)

    def approval(self, approval_id: str) -> KalshiReadApproval | None:
        return self._one(kalshi_read_approval_table, "approval_id", approval_id, _approval)

    def approvals(self) -> tuple[KalshiReadApproval, ...]:
        return self._all(kalshi_read_approval_table, _approval)

    def save_attempt(self, value: KalshiInspectionAttempt) -> KalshiInspectionAttempt:
        return self._save(kalshi_read_attempt_table, "attempt_id", value)

    def attempt(self, attempt_id: str) -> KalshiInspectionAttempt | None:
        return self._one(kalshi_read_attempt_table, "attempt_id", attempt_id, _attempt)

    def attempts(self) -> tuple[KalshiInspectionAttempt, ...]:
        return self._all(kalshi_read_attempt_table, _attempt)

    def save_capture(self, value: KalshiCapabilityCapture) -> KalshiCapabilityCapture:
        return self._save(kalshi_capability_capture_table, "capture_id", value)

    def capture(self, capture_id: str) -> KalshiCapabilityCapture | None:
        return self._one(kalshi_capability_capture_table, "capture_id", capture_id, _capture)

    def captures(self) -> tuple[KalshiCapabilityCapture, ...]:
        return self._all(kalshi_capability_capture_table, _capture)

    def save_refresh(self, value: KalshiCapabilityRefresh) -> KalshiCapabilityRefresh:
        return self._save(kalshi_capability_refresh_table, "refresh_id", value)

    def refreshes(self) -> tuple[KalshiCapabilityRefresh, ...]:
        return self._all(kalshi_capability_refresh_table, _refresh)
