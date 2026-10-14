"""PostgreSQL repository for the retained options-forward-evidence
checkpoint (Fresh Options Evidence Operations and Structure Research
Baseline v1).

Follows the identical ``payload``-column pattern
``apex.adapters.options_quote_intelligence_postgres``/``apex.adapters.
options_structures_postgres`` already use for this same evidence family:
one canonical JSON payload column (restored exactly via
``canonical_rehydrate.rehydrate``) plus a small set of indexed columns for
the query shapes the application layer actually needs. The single table
carries ``authority = 'NONE'``.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.canonical_rehydrate import rehydrate
from apex.domain.options_research_operations import OptionsForwardEvidenceCheckpoint
from apex.domain.values import canonical_json_bytes

OPTIONS_RESEARCH_OPERATIONS_TABLES = ("options_forward_evidence_checkpoint",)
OPTIONS_RESEARCH_OPERATIONS_IMMUTABLE_TABLES = OPTIONS_RESEARCH_OPERATIONS_TABLES

metadata = sa.MetaData()

options_forward_evidence_checkpoint = sa.Table(
    "options_forward_evidence_checkpoint",
    metadata,
    sa.Column("checkpoint_id", sa.String(160), primary_key=True),
    sa.Column(
        "plan_id",
        sa.String(160),
        sa.ForeignKey("options_chain_capture_plan.plan_id"),
        nullable=False,
    ),
    sa.Column("session_id", sa.String(10), nullable=False),
    sa.Column("capture_window_policy_id", sa.String(160), nullable=False),
    sa.Column("window_open_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("window_close_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("provider", sa.String(80), nullable=False),
    sa.Column("feed", sa.String(16), nullable=False),
    sa.Column("underlyings_requested_count", sa.Integer(), nullable=False),
    sa.Column("underlyings_captured_count", sa.Integer(), nullable=False),
    sa.Column("structures_evaluated", sa.Integer(), nullable=False),
    sa.Column("structures_retained", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", sa.Text(), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False),
)
sa.Index(
    "ix_ofec_plan_session_created",
    options_forward_evidence_checkpoint.c.plan_id,
    options_forward_evidence_checkpoint.c.session_id,
    options_forward_evidence_checkpoint.c.created_at,
)


class OptionsResearchOperationsPersistenceError(RuntimeError):
    pass


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _decoded(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise OptionsResearchOperationsPersistenceError("STORED_PAYLOAD_INVALID")
    return value


def _checkpoint(row: RowMapping) -> OptionsForwardEvidenceCheckpoint:
    return rehydrate(OptionsForwardEvidenceCheckpoint, _decoded(row))


class PostgresOptionsResearchOperationsRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save_checkpoint(
        self, checkpoint: OptionsForwardEvidenceCheckpoint
    ) -> OptionsForwardEvidenceCheckpoint:
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(options_forward_evidence_checkpoint.c.checkpoint_id).where(
                        options_forward_evidence_checkpoint.c.checkpoint_id
                        == checkpoint.checkpoint_id
                    )
                ).scalar_one_or_none()
                if exists is None:
                    connection.execute(
                        options_forward_evidence_checkpoint.insert().values(
                            checkpoint_id=checkpoint.checkpoint_id,
                            plan_id=checkpoint.plan_id,
                            session_id=checkpoint.session_id,
                            capture_window_policy_id=checkpoint.capture_window_policy_id,
                            window_open_at=checkpoint.window_open_at,
                            window_close_at=checkpoint.window_close_at,
                            provider=checkpoint.provider,
                            feed=checkpoint.feed,
                            underlyings_requested_count=checkpoint.underlyings_requested_count,
                            underlyings_captured_count=checkpoint.underlyings_captured_count,
                            structures_evaluated=checkpoint.structures_evaluated,
                            structures_retained=checkpoint.structures_retained,
                            created_at=checkpoint.created_at,
                            payload=_payload(checkpoint),
                            authority="NONE",
                        )
                    )
        except IntegrityError as error:
            raise OptionsResearchOperationsPersistenceError(
                "CHECKPOINT_PERSISTENCE_CONFLICT"
            ) from error
        return checkpoint

    def checkpoint_by_id(self, checkpoint_id: str) -> OptionsForwardEvidenceCheckpoint | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_forward_evidence_checkpoint).where(
                        options_forward_evidence_checkpoint.c.checkpoint_id == checkpoint_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _checkpoint(row) if row is not None else None

    def checkpoints_for_session(
        self, *, plan_id: str, session_id: str
    ) -> tuple[OptionsForwardEvidenceCheckpoint, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_forward_evidence_checkpoint)
                .where(
                    options_forward_evidence_checkpoint.c.plan_id == plan_id,
                    options_forward_evidence_checkpoint.c.session_id == session_id,
                )
                .order_by(options_forward_evidence_checkpoint.c.created_at)
            ).mappings()
            return tuple(_checkpoint(row) for row in rows)

    def checkpoints_for_plan(self, plan_id: str) -> tuple[OptionsForwardEvidenceCheckpoint, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(options_forward_evidence_checkpoint)
                .where(options_forward_evidence_checkpoint.c.plan_id == plan_id)
                .order_by(options_forward_evidence_checkpoint.c.created_at)
            ).mappings()
            return tuple(_checkpoint(row) for row in rows)

    def latest_checkpoint_for_session(
        self, *, plan_id: str, session_id: str
    ) -> OptionsForwardEvidenceCheckpoint | None:
        matches = self.checkpoints_for_session(plan_id=plan_id, session_id=session_id)
        return matches[-1] if matches else None

    def latest_checkpoint(self, plan_id: str) -> OptionsForwardEvidenceCheckpoint | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(options_forward_evidence_checkpoint)
                    .where(options_forward_evidence_checkpoint.c.plan_id == plan_id)
                    .order_by(options_forward_evidence_checkpoint.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _checkpoint(row) if row is not None else None

    def completed_session_ids(self, plan_id: str) -> frozenset[str]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    options_forward_evidence_checkpoint.c.session_id,
                    options_forward_evidence_checkpoint.c.underlyings_captured_count,
                    options_forward_evidence_checkpoint.c.underlyings_requested_count,
                ).where(options_forward_evidence_checkpoint.c.plan_id == plan_id)
            )
            return frozenset(
                session_id
                for session_id, captured, requested in rows
                if requested > 0 and captured == requested
            )
