"""Immutable PostgreSQL evidence for Strategy Version Provenance v1.

Implements ``application.strategy_version_provenance.
StrategyVersionProvenanceRepository`` over the four append-only tables that
migration ``0115_strategy_provenance`` creates. Follows the repository's
existing idiom: idempotent insert-or-conflict keyed by deterministic
identity, with every row re-verified through the domain on read
(``rehydrate_*``). Stored hashes are never trusted as-is.

``record_trial_with_provenance`` writes the ``internal_strategy_paper_trial``
row and its ``strategy_paper_trial_provenance`` row in one transaction. The
0115 constraint trigger rejects a non-legacy trial row without its provenance
at commit, so either both rows exist or neither does.

Constraint and data errors (``IntegrityError``/``DataError``, e.g. a value the
``artifact::jsonb`` CHECK cannot parse) are converted to
``StrategyVersionProvenanceResearchError`` so callers fail closed on the
provenance path alone. A connectivity outage still propagates like every
other write.

No provider client, credential, or network capability lives here, and no row
this module writes carries an ``authority`` other than ``'NONE'``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import DataError, IntegrityError

from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
    internal_strategy_paper_trial_table,
)
from apex.application.strategy_version_provenance import (
    StrategyVersionProvenanceResearchError,
)
from apex.domain.strategy_definition import StrategyLifecycleState
from apex.domain.strategy_paper_trial_lifecycle import StrategyPaperTrialRecord
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyEvaluationSubject,
    StrategyPaperTrialProvenance,
    StrategyVersionProvenanceError,
    StrategyVersionRecord,
    StrategyVersionRef,
    StrategyVersionRegistry,
    StrategyVersionTransition,
    StrategyVersionTransitionKind,
    rehydrate_strategy_evaluation_subject,
    rehydrate_strategy_paper_trial_provenance,
    rehydrate_strategy_version_record,
    rehydrate_strategy_version_transition,
)

metadata = sa.MetaData()

strategy_version_table = sa.Table(
    "strategy_version",
    metadata,
    sa.Column("registry", sa.String(40), primary_key=True),
    sa.Column("strategy_id", sa.String(300), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("definition_hash", sa.String(64), primary_key=True),
    sa.Column("lifecycle_state", sa.String(20), nullable=False),
    sa.Column("parent_version", sa.Integer, nullable=True),
    sa.Column("parent_definition_hash", sa.String(64), nullable=True),
    sa.Column("artifact", sa.Text(), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
)

strategy_evaluation_subject_table = sa.Table(
    "strategy_evaluation_subject",
    metadata,
    sa.Column("evaluation_source", sa.String(40), primary_key=True),
    sa.Column("evaluation_id", sa.String(300), primary_key=True),
    sa.Column("internal_evaluation_id", sa.String(300), nullable=True),
    sa.Column("external_evaluation_id", sa.String(300), nullable=True),
    sa.Column("evaluation_hash", sa.String(64), nullable=False),
    sa.Column("registry", sa.String(40), nullable=False),
    sa.Column("strategy_id", sa.String(300), nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("definition_hash", sa.String(64), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
)

strategy_version_transition_table = sa.Table(
    "strategy_version_transition",
    metadata,
    sa.Column("transition_id", sa.String(300), primary_key=True),
    sa.Column("kind", sa.String(40), nullable=False),
    sa.Column("registry", sa.String(40), nullable=False),
    sa.Column("strategy_id", sa.String(300), nullable=False),
    sa.Column("from_version", sa.Integer, nullable=False),
    sa.Column("from_definition_hash", sa.String(64), nullable=False),
    sa.Column("to_version", sa.Integer, nullable=False),
    sa.Column("to_definition_hash", sa.String(64), nullable=False),
    sa.Column("evaluation_source", sa.String(40), nullable=False),
    sa.Column("evaluation_id", sa.String(300), nullable=False),
    sa.Column("evaluation_hash", sa.String(64), nullable=False),
    sa.Column("gate_policy_version", sa.String(60), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
)

strategy_paper_trial_provenance_table = sa.Table(
    "strategy_paper_trial_provenance",
    metadata,
    sa.Column("trial_id", sa.String(300), primary_key=True),
    sa.Column("strategy_id", sa.String(300), nullable=False),
    sa.Column("strategy_version", sa.Integer, nullable=False),
    sa.Column("trial_legacy_adopted", sa.Boolean, nullable=False),
    sa.Column("trial_canonical_hash", sa.String(64), nullable=False),
    sa.Column("transition_id", sa.String(300), nullable=False),
    sa.Column("registry", sa.String(40), nullable=False),
    sa.Column("definition_hash", sa.String(64), nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
)

STRATEGY_VERSION_PROVENANCE_TABLES = (
    "strategy_version",
    "strategy_evaluation_subject",
    "strategy_version_transition",
    "strategy_paper_trial_provenance",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _version_from_row(row: RowMapping) -> StrategyVersionRecord:
    registry = StrategyVersionRegistry(str(row["registry"]))
    parent_version = row["parent_version"]
    return rehydrate_strategy_version_record(
        ref=StrategyVersionRef(
            registry=registry,
            strategy_id=str(row["strategy_id"]),
            version=int(row["version"]),
            definition_hash=str(row["definition_hash"]),
        ),
        lifecycle_state=StrategyLifecycleState(str(row["lifecycle_state"])),
        parent_ref=(
            None
            if parent_version is None
            else StrategyVersionRef(
                registry=registry,
                strategy_id=str(row["strategy_id"]),
                version=int(parent_version),
                definition_hash=str(row["parent_definition_hash"]),
            )
        ),
        artifact=str(row["artifact"]).encode("utf-8"),
        canonical_hash=str(row["canonical_hash"]),
    )


def _subject_from_row(row: RowMapping) -> StrategyEvaluationSubject:
    return rehydrate_strategy_evaluation_subject(
        evaluation_source=StrategyEvaluationSource(str(row["evaluation_source"])),
        evaluation_id=str(row["evaluation_id"]),
        evaluation_hash=str(row["evaluation_hash"]),
        subject=StrategyVersionRef(
            registry=StrategyVersionRegistry(str(row["registry"])),
            strategy_id=str(row["strategy_id"]),
            version=int(row["version"]),
            definition_hash=str(row["definition_hash"]),
        ),
        canonical_hash=str(row["canonical_hash"]),
    )


def _transition_from_row(row: RowMapping) -> StrategyVersionTransition:
    registry = StrategyVersionRegistry(str(row["registry"]))
    strategy_id = str(row["strategy_id"])
    return rehydrate_strategy_version_transition(
        kind=StrategyVersionTransitionKind(str(row["kind"])),
        from_ref=StrategyVersionRef(
            registry=registry,
            strategy_id=strategy_id,
            version=int(row["from_version"]),
            definition_hash=str(row["from_definition_hash"]),
        ),
        to_ref=StrategyVersionRef(
            registry=registry,
            strategy_id=strategy_id,
            version=int(row["to_version"]),
            definition_hash=str(row["to_definition_hash"]),
        ),
        evaluation_source=StrategyEvaluationSource(str(row["evaluation_source"])),
        evaluation_id=str(row["evaluation_id"]),
        evaluation_hash=str(row["evaluation_hash"]),
        gate_policy_version=str(row["gate_policy_version"]),
        transition_id=str(row["transition_id"]),
        canonical_hash=str(row["canonical_hash"]),
    )


def _trial_provenance_from_row(row: RowMapping) -> StrategyPaperTrialProvenance:
    return rehydrate_strategy_paper_trial_provenance(
        trial_id=str(row["trial_id"]),
        trial_hash=str(row["trial_canonical_hash"]),
        transition_id=str(row["transition_id"]),
        version_ref=StrategyVersionRef(
            registry=StrategyVersionRegistry(str(row["registry"])),
            strategy_id=str(row["strategy_id"]),
            version=int(row["strategy_version"]),
            definition_hash=str(row["definition_hash"]),
        ),
        canonical_hash=str(row["canonical_hash"]),
    )


class PostgresStrategyVersionProvenanceRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _insert_or_replay(
        self,
        connection: Connection,
        *,
        table: sa.Table,
        key: dict[str, object],
        canonical_hash: str,
        values: dict[str, object],
        conflict_code: str,
    ) -> bool:
        existing = connection.execute(
            sa.select(table.c.canonical_hash).where(
                *(table.c[name] == value for name, value in key.items())
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing != canonical_hash:
                raise StrategyVersionProvenanceResearchError(conflict_code)
            return True
        connection.execute(table.insert().values(**values))
        return False

    def record_version(self, *, record: StrategyVersionRecord) -> bool:
        ref = record.ref
        parent = record.parent_ref
        try:
            with self._engine.begin() as connection:
                return self._insert_or_replay(
                    connection,
                    table=strategy_version_table,
                    key={
                        "registry": ref.registry.value,
                        "strategy_id": ref.strategy_id,
                        "version": ref.version,
                        "definition_hash": ref.definition_hash,
                    },
                    canonical_hash=record.canonical_hash,
                    values={
                        "registry": ref.registry.value,
                        "strategy_id": ref.strategy_id,
                        "version": ref.version,
                        "definition_hash": ref.definition_hash,
                        "lifecycle_state": record.lifecycle_state.value,
                        "parent_version": None if parent is None else parent.version,
                        "parent_definition_hash": (
                            None if parent is None else parent.definition_hash
                        ),
                        "artifact": record.artifact.decode("utf-8"),
                        "canonical_hash": record.canonical_hash,
                        "recorded_at": _now(),
                        "authority": "NONE",
                    },
                    conflict_code="STRATEGY_VERSION_IDENTITY_CONFLICT",
                )
        except (IntegrityError, DataError) as error:
            raise StrategyVersionProvenanceResearchError(
                "STRATEGY_VERSION_PERSISTENCE_CONFLICT"
            ) from error

    def record_subject(self, *, subject: StrategyEvaluationSubject) -> bool:
        ref = subject.subject
        internal = (
            subject.evaluation_source is StrategyEvaluationSource.INTERNAL_STRATEGY_EVALUATION
        )
        try:
            with self._engine.begin() as connection:
                return self._insert_or_replay(
                    connection,
                    table=strategy_evaluation_subject_table,
                    key={
                        "evaluation_source": subject.evaluation_source.value,
                        "evaluation_id": subject.evaluation_id,
                    },
                    canonical_hash=subject.canonical_hash,
                    values={
                        "evaluation_source": subject.evaluation_source.value,
                        "evaluation_id": subject.evaluation_id,
                        "internal_evaluation_id": subject.evaluation_id if internal else None,
                        "external_evaluation_id": None if internal else subject.evaluation_id,
                        "evaluation_hash": subject.evaluation_hash,
                        "registry": ref.registry.value,
                        "strategy_id": ref.strategy_id,
                        "version": ref.version,
                        "definition_hash": ref.definition_hash,
                        "canonical_hash": subject.canonical_hash,
                        "recorded_at": _now(),
                        "authority": "NONE",
                    },
                    conflict_code="STRATEGY_EVALUATION_SUBJECT_IDENTITY_CONFLICT",
                )
        except (IntegrityError, DataError) as error:
            raise StrategyVersionProvenanceResearchError(
                "STRATEGY_EVALUATION_SUBJECT_PERSISTENCE_CONFLICT"
            ) from error

    def record_transition(self, *, transition: StrategyVersionTransition) -> bool:
        try:
            with self._engine.begin() as connection:
                return self._insert_or_replay(
                    connection,
                    table=strategy_version_transition_table,
                    key={"transition_id": transition.transition_id},
                    canonical_hash=transition.canonical_hash,
                    values={
                        "transition_id": transition.transition_id,
                        "kind": transition.kind.value,
                        "registry": transition.from_ref.registry.value,
                        "strategy_id": transition.from_ref.strategy_id,
                        "from_version": transition.from_ref.version,
                        "from_definition_hash": transition.from_ref.definition_hash,
                        "to_version": transition.to_ref.version,
                        "to_definition_hash": transition.to_ref.definition_hash,
                        "evaluation_source": transition.evaluation_source.value,
                        "evaluation_id": transition.evaluation_id,
                        "evaluation_hash": transition.evaluation_hash,
                        "gate_policy_version": transition.gate_policy_version,
                        "canonical_hash": transition.canonical_hash,
                        "recorded_at": _now(),
                        "authority": "NONE",
                    },
                    conflict_code="STRATEGY_VERSION_TRANSITION_IDENTITY_CONFLICT",
                )
        except (IntegrityError, DataError) as error:
            raise StrategyVersionProvenanceResearchError(
                "STRATEGY_VERSION_TRANSITION_PERSISTENCE_CONFLICT"
            ) from error

    def record_trial_with_provenance(
        self, *, trial: StrategyPaperTrialRecord, provenance: StrategyPaperTrialProvenance
    ) -> bool:
        if trial.trial_id != provenance.trial_id or trial.canonical_hash != provenance.trial_hash:
            raise StrategyVersionProvenanceResearchError(
                "STRATEGY_PAPER_TRIAL_PROVENANCE_TRIAL_MISMATCH"
            )
        ref = provenance.version_ref
        try:
            with self._engine.begin() as connection:
                trial_replay = self._insert_or_replay(
                    connection,
                    table=internal_strategy_paper_trial_table,
                    key={"trial_id": trial.trial_id},
                    canonical_hash=trial.canonical_hash,
                    values={
                        "trial_id": trial.trial_id,
                        "strategy_id": trial.strategy_id,
                        "strategy_version": trial.strategy_version,
                        "instrument_id": trial.instrument_id,
                        "account_id": trial.account_id,
                        "generation": trial.generation,
                        "opened_at": trial.opened_at,
                        "opening_authorization_id": trial.opening_authorization_id,
                        "opening_order_id": trial.opening_order_id,
                        "opening_realized_pnl_baseline": trial.opening_realized_pnl_baseline,
                        "horizon_days": trial.horizon_days,
                        "horizon_target_at": trial.horizon_target_at,
                        "cooldown_days": trial.cooldown_days,
                        "policy_version": trial.policy_version,
                        "legacy_adopted": trial.legacy_adopted,
                        "canonical_hash": trial.canonical_hash,
                        "authority": "NONE",
                    },
                    conflict_code="STRATEGY_PAPER_TRIAL_IDENTITY_CONFLICT",
                )
                provenance_replay = self._insert_or_replay(
                    connection,
                    table=strategy_paper_trial_provenance_table,
                    key={"trial_id": provenance.trial_id},
                    canonical_hash=provenance.canonical_hash,
                    values={
                        "trial_id": provenance.trial_id,
                        "strategy_id": ref.strategy_id,
                        "strategy_version": ref.version,
                        "trial_legacy_adopted": trial.legacy_adopted,
                        "trial_canonical_hash": provenance.trial_hash,
                        "transition_id": provenance.transition_id,
                        "registry": ref.registry.value,
                        "definition_hash": ref.definition_hash,
                        "canonical_hash": provenance.canonical_hash,
                        "recorded_at": _now(),
                        "authority": "NONE",
                    },
                    conflict_code="STRATEGY_PAPER_TRIAL_PROVENANCE_IDENTITY_CONFLICT",
                )
                if trial_replay != provenance_replay:
                    # Structurally unreachable after 0115 (the constraint
                    # trigger forbids a lone non-legacy trial row). Refuse
                    # rather than half-heal a row pair the database already
                    # says cannot exist.
                    raise StrategyVersionProvenanceResearchError(
                        "STRATEGY_PAPER_TRIAL_PROVENANCE_PARTIAL_STATE"
                    )
                return trial_replay
        except (IntegrityError, DataError) as error:
            raise StrategyVersionProvenanceResearchError(
                "STRATEGY_PAPER_TRIAL_PROVENANCE_PERSISTENCE_CONFLICT"
            ) from error

    def _rows(self, statement: sa.Select[tuple[object, ...]]) -> list[RowMapping]:
        with self._engine.connect() as connection:
            return list(connection.execute(statement).mappings())

    def version(self, *, ref: StrategyVersionRef) -> StrategyVersionRecord | None:
        rows = self._rows(
            sa.select(strategy_version_table).where(
                strategy_version_table.c.registry == ref.registry.value,
                strategy_version_table.c.strategy_id == ref.strategy_id,
                strategy_version_table.c.version == ref.version,
                strategy_version_table.c.definition_hash == ref.definition_hash,
            )
        )
        return _version_from_row(rows[0]) if rows else None

    def subject_for(
        self, *, evaluation_source: StrategyEvaluationSource, evaluation_id: str
    ) -> StrategyEvaluationSubject | None:
        rows = self._rows(
            sa.select(strategy_evaluation_subject_table).where(
                strategy_evaluation_subject_table.c.evaluation_source == evaluation_source.value,
                strategy_evaluation_subject_table.c.evaluation_id == evaluation_id,
            )
        )
        return _subject_from_row(rows[0]) if rows else None

    def transition(self, *, transition_id: str) -> StrategyVersionTransition | None:
        rows = self._rows(
            sa.select(strategy_version_transition_table).where(
                strategy_version_transition_table.c.transition_id == transition_id
            )
        )
        return _transition_from_row(rows[0]) if rows else None

    def trial_provenance(self, *, trial_id: str) -> StrategyPaperTrialProvenance | None:
        rows = self._rows(
            sa.select(strategy_paper_trial_provenance_table).where(
                strategy_paper_trial_provenance_table.c.trial_id == trial_id
            )
        )
        return _trial_provenance_from_row(rows[0]) if rows else None

    def versions_for_strategy(self, *, strategy_id: str) -> tuple[StrategyVersionRecord, ...]:
        table = strategy_version_table
        return tuple(
            _version_from_row(row)
            for row in self._rows(
                sa.select(table)
                .where(table.c.strategy_id == strategy_id)
                .order_by(table.c.registry, table.c.version, table.c.definition_hash)
            )
        )

    def subjects_for_strategy(self, *, strategy_id: str) -> tuple[StrategyEvaluationSubject, ...]:
        table = strategy_evaluation_subject_table
        return tuple(
            _subject_from_row(row)
            for row in self._rows(
                sa.select(table)
                .where(table.c.strategy_id == strategy_id)
                .order_by(table.c.evaluation_source, table.c.evaluation_id)
            )
        )

    def transitions_for_strategy(
        self, *, strategy_id: str
    ) -> tuple[StrategyVersionTransition, ...]:
        table = strategy_version_transition_table
        return tuple(
            _transition_from_row(row)
            for row in self._rows(
                sa.select(table)
                .where(table.c.strategy_id == strategy_id)
                .order_by(table.c.transition_id)
            )
        )

    def trial_provenance_for_strategy(
        self, *, strategy_id: str
    ) -> tuple[StrategyPaperTrialProvenance, ...]:
        table = strategy_paper_trial_provenance_table
        return tuple(
            _trial_provenance_from_row(row)
            for row in self._rows(
                sa.select(table)
                .where(table.c.strategy_id == strategy_id)
                .order_by(table.c.trial_id)
            )
        )


__all__ = [
    "STRATEGY_VERSION_PROVENANCE_TABLES",
    "PostgresStrategyVersionProvenanceRepository",
    "StrategyVersionProvenanceError",
    "metadata",
]
