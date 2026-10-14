"""Append-only PostgreSQL evidence for unified non-executing LIVE preflight."""

from __future__ import annotations

import json
import types
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.application.live_preflight import LivePreflightRepository
from apex.domain.live_preflight import (
    InternalPreflight,
    LiveIntentEligibilityDecision,
    LiveIntentInvalidation,
    LiveIntentProposal,
    LivePreflightAssessment,
    LivePreflightError,
    ProviderPreviewApproval,
    ProviderPreviewAttempt,
    ProviderPreviewCapture,
)

live_preflight_metadata = sa.MetaData()


def _table(name: str, identity: str, *columns: Any) -> sa.Table:
    table = sa.Table(
        name,
        live_preflight_metadata,
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
            "AND lower(payload) NOT LIKE '%\"private_key\":%' "
            "AND lower(payload) NOT LIKE '%\"authorization\":%' "
            "AND lower(payload) NOT LIKE '%\"bearer\":%' "
            "AND lower(payload) NOT LIKE '%-----begin %' "
            "AND lower(payload) NOT LIKE '%bearer %'",
            name=f"ck_{name}_no_secret_material",
        )
    )
    return table


live_intent_table = _table(
    "live_intent_proposal",
    "intent_id",
    sa.Column("allocation_amount", sa.Numeric(38, 18), nullable=False),
    sa.Column("provider_order_id", sa.String(300), nullable=True),
    sa.Column("reservation_id", sa.String(300), nullable=True),
    sa.CheckConstraint("allocation_amount > 0", name="ck_live_intent_nonzero_allocation"),
    sa.CheckConstraint("provider_order_id IS NULL", name="ck_live_intent_no_provider_order"),
    sa.CheckConstraint("reservation_id IS NULL", name="ck_live_intent_no_reservation"),
)
live_intent_invalidation_table = _table(
    "live_intent_invalidation",
    "invalidation_id",
    sa.Column("intent_id", sa.String(300), nullable=False),
)
live_intent_eligibility_table = _table(
    "live_intent_eligibility_decision",
    "decision_id",
    sa.Column("intent_id", sa.String(300), nullable=False),
)
live_internal_preflight_table = _table(
    "live_internal_preflight",
    "preflight_id",
    sa.Column("intent_id", sa.String(300), nullable=False),
    sa.Column("provider_request_count", sa.Integer, nullable=False, server_default="0"),
    sa.CheckConstraint(
        "provider_request_count = 0", name="ck_live_internal_preflight_zero_requests"
    ),
)
provider_preview_approval_table = _table(
    "provider_preview_approval",
    "approval_id",
    sa.Column("intent_id", sa.String(300), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("preview_only", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("maximum_request_count", sa.Integer, nullable=False),
    sa.CheckConstraint("preview_only", name="ck_provider_preview_approval_preview_only"),
    sa.CheckConstraint(
        "maximum_request_count BETWEEN 1 AND 3",
        name="ck_provider_preview_approval_request_bound",
    ),
)
provider_preview_capture_table = _table(
    "provider_preview_capture",
    "capture_id",
    sa.Column("intent_id", sa.String(300), nullable=False),
    sa.Column("approval_id", sa.String(300), nullable=True),
    sa.Column("provider_request_count", sa.Integer, nullable=False),
    sa.Column("provider_order_id", sa.String(300), nullable=True),
    sa.CheckConstraint(
        "provider_request_count BETWEEN 0 AND 1",
        name="ck_provider_preview_capture_request_bound",
    ),
    sa.CheckConstraint("provider_order_id IS NULL", name="ck_provider_preview_capture_no_order"),
)
provider_preview_attempt_table = _table(
    "provider_preview_attempt",
    "attempt_id",
    sa.Column("intent_id", sa.String(300), nullable=False),
    sa.Column("approval_id", sa.String(300), nullable=True),
    sa.Column("provider_request_count", sa.Integer, nullable=False),
    sa.CheckConstraint(
        "provider_request_count BETWEEN 0 AND 1",
        name="ck_provider_preview_attempt_request_bound",
    ),
)
live_preflight_assessment_table = _table(
    "live_preflight_assessment",
    "assessment_id",
    sa.Column("intent_id", sa.String(300), nullable=False),
)

LIVE_PREFLIGHT_TABLES = tuple(live_preflight_metadata.tables)


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


def _decode(annotation: object, value: object) -> object:
    if value is None:
        return None
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is tuple:
        item_type = arguments[0] if len(arguments) == 2 and arguments[1] is Ellipsis else None
        if item_type is not None:
            return tuple(_decode(item_type, item) for item in cast(list[object], value))
        return tuple(
            _decode(item_annotation, item)
            for item_annotation, item in zip(arguments, cast(list[object], value), strict=True)
        )
    if origin in (Union, types.UnionType):
        selected = next((item for item in arguments if item is not type(None)), object)
        return _decode(selected, value)
    if annotation is datetime:
        return datetime.fromisoformat(str(value))
    if annotation is Decimal:
        return Decimal(str(value))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    return value


T = TypeVar("T")


def _object[T](row: RowMapping, value_type: type[T]) -> T:
    raw = json.loads(str(row["payload"]))
    if not isinstance(raw, dict):
        raise LivePreflightError("LIVE_PREFLIGHT_RETAINED_PAYLOAD_INVALID")
    hints = get_type_hints(value_type)
    values = {
        name: _decode(hints[name], value) for name, value in cast(dict[str, object], raw).items()
    }
    return value_type(**values)


class PostgresLivePreflightRepository(LivePreflightRepository):
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _save(self, table: sa.Table, identity: str, value: T) -> T:
        key = str(cast(Any, value).__getattribute__(identity))
        canonical_hash = str(cast(Any, value).canonical_hash)
        with self._engine.begin() as connection:
            existing = (
                connection.execute(sa.select(table).where(table.c[identity] == key))
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if str(existing["canonical_hash"]) != canonical_hash:
                    raise LivePreflightError("LIVE_PREFLIGHT_ID_CONFLICT")
                return _object(existing, type(value))
            columns: dict[str, object] = {
                identity: key,
                "payload": _payload(value),
                "canonical_hash": canonical_hash,
                "provider_write_count": 0,
                "authority": "NONE",
            }
            if isinstance(value, LiveIntentProposal):
                columns.update(
                    allocation_amount=value.allocation_amount,
                    provider_order_id=None,
                    reservation_id=None,
                )
            elif isinstance(value, (LiveIntentInvalidation, LiveIntentEligibilityDecision)):
                columns.update(intent_id=value.intent_id)
            elif isinstance(value, InternalPreflight):
                columns.update(intent_id=value.intent_id, provider_request_count=0)
            elif isinstance(value, ProviderPreviewApproval):
                columns.update(
                    intent_id=value.intent_id,
                    expires_at=value.expires_at,
                    preview_only=True,
                    maximum_request_count=value.maximum_request_count,
                )
            elif isinstance(value, ProviderPreviewCapture):
                columns.update(
                    intent_id=value.intent_id,
                    approval_id=value.approval_id,
                    provider_request_count=value.provider_request_count,
                    provider_order_id=None,
                )
            elif isinstance(value, ProviderPreviewAttempt):
                columns.update(
                    intent_id=value.intent_id,
                    approval_id=value.approval_id,
                    provider_request_count=value.request_count,
                )
            elif isinstance(value, LivePreflightAssessment):
                columns.update(intent_id=value.intent_id)
            connection.execute(table.insert().values(**columns))
        return value

    def _one(
        self,
        table: sa.Table,
        identity: str,
        key: str,
        loader: Callable[[RowMapping], T],
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

    def save_intent(self, value: LiveIntentProposal) -> LiveIntentProposal:
        return self._save(live_intent_table, "intent_id", value)

    def intent(self, intent_id: str) -> LiveIntentProposal | None:
        return self._one(
            live_intent_table,
            "intent_id",
            intent_id,
            lambda row: _object(row, LiveIntentProposal),
        )

    def intents(self) -> tuple[LiveIntentProposal, ...]:
        return self._all(live_intent_table, lambda row: _object(row, LiveIntentProposal))

    def save_invalidation(self, value: LiveIntentInvalidation) -> LiveIntentInvalidation:
        return self._save(live_intent_invalidation_table, "invalidation_id", value)

    def invalidations(self, intent_id: str) -> tuple[LiveIntentInvalidation, ...]:
        return tuple(
            item
            for item in self._all(
                live_intent_invalidation_table,
                lambda row: _object(row, LiveIntentInvalidation),
            )
            if item.intent_id == intent_id
        )

    def invalidations_by_intent_id(self) -> dict[str, tuple[LiveIntentInvalidation, ...]]:
        """Batched form of ``invalidations()``: every retained invalidation,
        grouped by ``intent_id``, from exactly one query -- for callers (the
        Operator Console Live Readiness/Live Preflight read models) that
        would otherwise call ``invalidations(intent_id)`` once per retained
        intent, each re-scanning this same unfiltered table from scratch.
        Reuses the identical ``_all``/``_object`` read ``invalidations()``
        itself uses, so it is provably the same query and the same row
        decoding, issued once instead of N times; grouping preserves each
        intent's rows in the same primary-key order ``invalidations(intent_id)``
        would return them in. Additive: ``invalidations()`` is unchanged and
        still the correct call for a single intent_id outside a per-intent-id
        loop."""

        grouped: dict[str, list[LiveIntentInvalidation]] = {}
        for item in self._all(
            live_intent_invalidation_table,
            lambda row: _object(row, LiveIntentInvalidation),
        ):
            grouped.setdefault(item.intent_id, []).append(item)
        return {key: tuple(values) for key, values in grouped.items()}

    def save_eligibility(
        self, value: LiveIntentEligibilityDecision
    ) -> LiveIntentEligibilityDecision:
        return self._save(live_intent_eligibility_table, "decision_id", value)

    def eligibility(self, decision_id: str) -> LiveIntentEligibilityDecision | None:
        return self._one(
            live_intent_eligibility_table,
            "decision_id",
            decision_id,
            lambda row: _object(row, LiveIntentEligibilityDecision),
        )

    def save_preflight(self, value: InternalPreflight) -> InternalPreflight:
        return self._save(live_internal_preflight_table, "preflight_id", value)

    def preflight(self, preflight_id: str) -> InternalPreflight | None:
        return self._one(
            live_internal_preflight_table,
            "preflight_id",
            preflight_id,
            lambda row: _object(row, InternalPreflight),
        )

    def preflights(self) -> tuple[InternalPreflight, ...]:
        return self._all(
            live_internal_preflight_table,
            lambda row: _object(row, InternalPreflight),
        )

    def save_preview_approval(self, value: ProviderPreviewApproval) -> ProviderPreviewApproval:
        return self._save(provider_preview_approval_table, "approval_id", value)

    def preview_approval(self, approval_id: str) -> ProviderPreviewApproval | None:
        return self._one(
            provider_preview_approval_table,
            "approval_id",
            approval_id,
            lambda row: _object(row, ProviderPreviewApproval),
        )

    def preview_approvals(self) -> tuple[ProviderPreviewApproval, ...]:
        return self._all(
            provider_preview_approval_table,
            lambda row: _object(row, ProviderPreviewApproval),
        )

    def save_preview_capture(self, value: ProviderPreviewCapture) -> ProviderPreviewCapture:
        return self._save(provider_preview_capture_table, "capture_id", value)

    def preview_capture(self, capture_id: str) -> ProviderPreviewCapture | None:
        return self._one(
            provider_preview_capture_table,
            "capture_id",
            capture_id,
            lambda row: _object(row, ProviderPreviewCapture),
        )

    def preview_captures(self) -> tuple[ProviderPreviewCapture, ...]:
        return self._all(
            provider_preview_capture_table,
            lambda row: _object(row, ProviderPreviewCapture),
        )

    def save_preview_attempt(self, value: ProviderPreviewAttempt) -> ProviderPreviewAttempt:
        return self._save(provider_preview_attempt_table, "attempt_id", value)

    def preview_attempt(self, attempt_id: str) -> ProviderPreviewAttempt | None:
        return self._one(
            provider_preview_attempt_table,
            "attempt_id",
            attempt_id,
            lambda row: _object(row, ProviderPreviewAttempt),
        )

    def preview_attempts(self) -> tuple[ProviderPreviewAttempt, ...]:
        return self._all(
            provider_preview_attempt_table,
            lambda row: _object(row, ProviderPreviewAttempt),
        )

    def save_assessment(self, value: LivePreflightAssessment) -> LivePreflightAssessment:
        return self._save(live_preflight_assessment_table, "assessment_id", value)

    def assessment(self, assessment_id: str) -> LivePreflightAssessment | None:
        return self._one(
            live_preflight_assessment_table,
            "assessment_id",
            assessment_id,
            lambda row: _object(row, LivePreflightAssessment),
        )

    def assessments(self) -> tuple[LivePreflightAssessment, ...]:
        return self._all(
            live_preflight_assessment_table,
            lambda row: _object(row, LivePreflightAssessment),
        )
