"""Immutable PostgreSQL evidence for reusable stateful Crypto research."""

from __future__ import annotations

from typing import Any, Protocol, cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.research_operations_postgres import _object, _payload
from apex.domain.crypto_stateful_research import (
    CryptoExecutionHistory,
    CryptoStatefulCurrentSignal,
    CryptoStatefulExperimentRun,
    CryptoStatefulMethodSpecification,
    CryptoStatefulResearchError,
    ResearchProposalRefinement,
)

metadata = sa.MetaData()


class _HasCanonicalHash(Protocol):
    canonical_hash: str


def _table(name: str, identity: str, *columns: Any) -> sa.Table:
    return sa.Table(
        name,
        metadata,
        sa.Column(identity, sa.String(300), primary_key=True),
        *columns,
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("canonical_hash", sa.String(64), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
        sa.Column("live_authority", sa.String(16), nullable=False),
    )


refinement_table = _table(
    "research_proposal_refinement",
    "refinement_id",
    sa.Column("original_proposal_id", sa.String(300), nullable=False),
    sa.Column("refined_proposal_id", sa.String(300), nullable=False, unique=True),
    sa.Column("method_id", sa.String(300), nullable=False),
    sa.Column("refined_at", sa.DateTime(timezone=True), nullable=False),
)
specification_table = _table(
    "crypto_stateful_method_specification",
    "specification_id",
    sa.Column("proposal_id", sa.String(300), nullable=False, unique=True),
    sa.Column("method_id", sa.String(300), nullable=False, unique=True),
    sa.Column("implemented_at", sa.DateTime(timezone=True), nullable=False),
)
history_table = _table(
    "crypto_execution_history",
    "history_id",
    sa.Column("product_id", sa.String(160), nullable=False),
    sa.Column("source_observation_id", sa.String(300), nullable=False, unique=True),
    sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
)
run_table = _table(
    "crypto_stateful_experiment_run",
    "run_id",
    sa.Column("proposal_id", sa.String(300), nullable=False, unique=True),
    sa.Column("specification_id", sa.String(300), nullable=False),
    sa.Column("dataset_id", sa.String(300), nullable=False),
    sa.Column("result_hash", sa.String(64), nullable=False),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
)
signal_table = _table(
    "crypto_stateful_current_signal",
    "signal_id",
    sa.Column("method_id", sa.String(300), nullable=False),
    sa.Column("experiment_run_id", sa.String(300), nullable=False),
    sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
)

TABLE_NAMES = tuple(metadata.tables)


class PostgresCryptoStatefulResearchRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _one(self, statement: sa.Select[Any]) -> RowMapping | None:
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def _rows(self, statement: sa.Select[Any]) -> tuple[RowMapping, ...]:
        with self._engine.connect() as connection:
            return tuple(connection.execute(statement).mappings())

    def _save(
        self,
        *,
        table: sa.Table,
        identity_name: str,
        identity: str,
        value: object,
        extra: dict[str, object],
    ) -> bool:
        retained = self._one(sa.select(table).where(table.c[identity_name] == identity))
        canonical_hash = cast(_HasCanonicalHash, value).canonical_hash
        if retained is not None:
            if retained["canonical_hash"] != canonical_hash:
                raise CryptoStatefulResearchError("CRYPTO_STATEFUL_PERSISTENCE_IDENTITY_CONFLICT")
            return False
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    sa.insert(table).values(
                        **{identity_name: identity},
                        **extra,
                        payload=_payload(value),
                        canonical_hash=canonical_hash,
                        authority=str(getattr(value, "authority", "NONE")),
                        live_authority=str(getattr(value, "live_authority", "NONE")),
                    )
                )
        except IntegrityError as error:
            raise CryptoStatefulResearchError("CRYPTO_STATEFUL_PERSISTENCE_CONFLICT") from error
        return True

    def refinement(self, refinement_id: str) -> ResearchProposalRefinement | None:
        row = self._one(
            sa.select(refinement_table).where(refinement_table.c.refinement_id == refinement_id)
        )
        return None if row is None else _object(row, ResearchProposalRefinement)

    def refinements(self) -> tuple[ResearchProposalRefinement, ...]:
        return tuple(
            _object(row, ResearchProposalRefinement)
            for row in self._rows(
                sa.select(refinement_table).order_by(
                    refinement_table.c.refined_at, refinement_table.c.refinement_id
                )
            )
        )

    def save_refinement(self, value: ResearchProposalRefinement) -> bool:
        return self._save(
            table=refinement_table,
            identity_name="refinement_id",
            identity=value.refinement_id,
            value=value,
            extra={
                "original_proposal_id": value.original_proposal_id,
                "refined_proposal_id": value.refined_proposal_id,
                "method_id": value.method_id,
                "refined_at": value.refined_at,
            },
        )

    def specification(self, specification_id: str) -> CryptoStatefulMethodSpecification | None:
        row = self._one(
            sa.select(specification_table).where(
                specification_table.c.specification_id == specification_id
            )
        )
        return None if row is None else _object(row, CryptoStatefulMethodSpecification)

    def specifications(self) -> tuple[CryptoStatefulMethodSpecification, ...]:
        return tuple(
            _object(row, CryptoStatefulMethodSpecification)
            for row in self._rows(
                sa.select(specification_table).order_by(
                    specification_table.c.implemented_at,
                    specification_table.c.specification_id,
                )
            )
        )

    def save_specification(self, value: CryptoStatefulMethodSpecification) -> bool:
        return self._save(
            table=specification_table,
            identity_name="specification_id",
            identity=value.specification_id,
            value=value,
            extra={
                "proposal_id": value.proposal_id,
                "method_id": value.method_id,
                "implemented_at": value.implemented_at,
            },
        )

    def execution_history(self, history_id: str) -> CryptoExecutionHistory | None:
        row = self._one(sa.select(history_table).where(history_table.c.history_id == history_id))
        return None if row is None else _object(row, CryptoExecutionHistory)

    def execution_histories(self) -> tuple[CryptoExecutionHistory, ...]:
        return tuple(
            _object(row, CryptoExecutionHistory)
            for row in self._rows(sa.select(history_table).order_by(history_table.c.history_id))
        )

    def save_execution_history(self, value: CryptoExecutionHistory) -> bool:
        return self._save(
            table=history_table,
            identity_name="history_id",
            identity=value.history_id,
            value=value,
            extra={
                "product_id": value.product_id,
                "source_observation_id": value.source_observation_id,
                "captured_at": value.captured_at,
            },
        )

    def experiment_run(self, run_id: str) -> CryptoStatefulExperimentRun | None:
        row = self._one(sa.select(run_table).where(run_table.c.run_id == run_id))
        return None if row is None else _object(row, CryptoStatefulExperimentRun)

    def experiment_runs(self) -> tuple[CryptoStatefulExperimentRun, ...]:
        return tuple(
            _object(row, CryptoStatefulExperimentRun)
            for row in self._rows(
                sa.select(run_table).order_by(run_table.c.completed_at, run_table.c.run_id)
            )
        )

    def save_experiment_run(self, value: CryptoStatefulExperimentRun) -> bool:
        return self._save(
            table=run_table,
            identity_name="run_id",
            identity=value.run_id,
            value=value,
            extra={
                "proposal_id": value.proposal_id,
                "specification_id": value.specification_id,
                "dataset_id": value.dataset_id,
                "result_hash": value.result_hash,
                "completed_at": value.completed_at,
            },
        )

    def current_signal(self, signal_id: str) -> CryptoStatefulCurrentSignal | None:
        row = self._one(sa.select(signal_table).where(signal_table.c.signal_id == signal_id))
        return None if row is None else _object(row, CryptoStatefulCurrentSignal)

    def current_signals(self) -> tuple[CryptoStatefulCurrentSignal, ...]:
        return tuple(
            _object(row, CryptoStatefulCurrentSignal)
            for row in self._rows(
                sa.select(signal_table).order_by(
                    signal_table.c.generated_at, signal_table.c.signal_id
                )
            )
        )

    def save_current_signal(self, value: CryptoStatefulCurrentSignal) -> bool:
        return self._save(
            table=signal_table,
            identity_name="signal_id",
            identity=value.signal_id,
            value=value,
            extra={
                "method_id": value.method_id,
                "experiment_run_id": value.experiment_run_id,
                "generated_at": value.generated_at,
                "state": value.state.value,
            },
        )
