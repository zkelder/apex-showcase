"""Immutable PostgreSQL evidence for Repeatable Strategy PAPER Evidence v1.

Two append-only tables, never mutated, never deleted:
``internal_strategy_paper_trial`` (one row per genuinely distinct
``domain.strategy_paper_trial_lifecycle.StrategyPaperTrialRecord``) and
``internal_strategy_paper_trial_closure`` (one row per genuinely distinct
``domain.strategy_paper_trial_lifecycle.StrategyPaperTrialClosureRecord``,
UNIQUE on ``trial_id`` -- see migration ``0111`` for why that constraint is
the load-bearing "cannot close a trial twice" guarantee). Implements
``application.strategy_paper_trial_lifecycle``'s two repository Protocols.
No provider client, credential, or network capability lives here, and no
row this module writes ever carries an ``authority`` other than ``'NONE'``.

Mirrors ``adapters.strategy_evaluation_history_postgres.py``'s own
"idempotent insert-or-conflict, payload-free typed columns, round-trip
through the domain constructor" idiom throughout -- deliberately reused
rather than re-derived. Unlike that sibling module, every field here is a
plain scalar (no nested ``StrategyEvaluationResult``/JSON payload column is
needed), so there is no separate ``payload`` text column at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.application.strategy_paper_trial_lifecycle import (
    StrategyPaperTrialLifecycleResearchError,
)
from apex.domain.strategy_paper_trial_lifecycle import (
    StrategyPaperTrialClosureRecord,
    StrategyPaperTrialLifecycleError,
    StrategyPaperTrialRecord,
    StrategyPaperTrialTerminalReason,
)

metadata = sa.MetaData()

internal_strategy_paper_trial_table = sa.Table(
    "internal_strategy_paper_trial",
    metadata,
    sa.Column("trial_id", sa.String(300), primary_key=True),
    sa.Column("strategy_id", sa.String(300), nullable=False),
    sa.Column("strategy_version", sa.Integer, nullable=False),
    sa.Column("instrument_id", sa.String(60), nullable=False),
    sa.Column("account_id", sa.String(300), nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("opening_authorization_id", sa.String(300), nullable=False),
    sa.Column("opening_order_id", sa.String(300), nullable=False),
    sa.Column("opening_realized_pnl_baseline", sa.Numeric, nullable=False),
    sa.Column("horizon_days", sa.Integer, nullable=False),
    sa.Column("horizon_target_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("cooldown_days", sa.Integer, nullable=False),
    sa.Column("policy_version", sa.String(60), nullable=False),
    sa.Column("legacy_adopted", sa.Boolean, nullable=False),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
)

internal_strategy_paper_trial_closure_table = sa.Table(
    "internal_strategy_paper_trial_closure",
    metadata,
    sa.Column("closure_id", sa.String(300), primary_key=True),
    sa.Column("trial_id", sa.String(300), nullable=False, unique=True),
    sa.Column("strategy_id", sa.String(300), nullable=False),
    sa.Column("instrument_id", sa.String(60), nullable=False),
    sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("terminal_reason", sa.String(40), nullable=False),
    sa.Column("closing_authorization_id", sa.String(300), nullable=False),
    sa.Column("closing_order_id", sa.String(300), nullable=False),
    sa.Column("realized_paper_pnl", sa.Numeric, nullable=True),
    sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=False),
    # Trial Evidence Coverage Disclosure v1 (migration 0112) -- additive,
    # nullable columns; see domain.strategy_paper_trial_evidence_coverage
    # for what each one means. Nullable purely per that milestone's own
    # additive-migration convention -- application code always populates
    # every one of them together (see build_strategy_paper_trial_closure_
    # record), never a partial subset.
    sa.Column("coverage_window_duration_seconds", sa.Integer, nullable=True),
    sa.Column("coverage_nominal_days", sa.Integer, nullable=True),
    sa.Column("coverage_covered_days", sa.Integer, nullable=True),
    sa.Column("coverage_qualifying_observation_count", sa.Integer, nullable=True),
    sa.Column("coverage_largest_gap_seconds", sa.Integer, nullable=True),
    sa.Column("coverage_policy_version", sa.String(60), nullable=True),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    sa.ForeignKeyConstraint(
        ["trial_id"],
        ["internal_strategy_paper_trial.trial_id"],
        name="fk_internal_strategy_paper_trial_closure_trial",
    ),
)

STRATEGY_PAPER_TRIAL_LIFECYCLE_TABLES = (
    "internal_strategy_paper_trial",
    "internal_strategy_paper_trial_closure",
)


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _required_int(value: object, *, column: str) -> int:
    """Every ``coverage_*`` column is nullable at the DB level (Trial
    Evidence Coverage Disclosure v1's own additive-migration convention --
    see the table definition above), but application code always writes
    every one of them together, never a partial subset -- so a retained
    row with any of them ``NULL`` or a genuinely wrong type is a malformed
    row, not a legitimate legacy shape to silently tolerate. Fails closed
    (an ``isinstance`` check, never a bare ``int(value)``/``bool(value)``
    conversion -- ``bool("false")`` is ``True``, so blindly coercing an
    unexpected type could silently fabricate the wrong disclosure)."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise StrategyPaperTrialLifecycleError(
            f"STRATEGY_PAPER_TRIAL_CLOSURE_COVERAGE_FIELD_INVALID:{column}"
        )
    return value


def _required_str(value: object, *, column: str) -> str:
    if not isinstance(value, str) or not value:
        raise StrategyPaperTrialLifecycleError(
            f"STRATEGY_PAPER_TRIAL_CLOSURE_COVERAGE_FIELD_INVALID:{column}"
        )
    return value


def _trial_from_row(row: RowMapping) -> StrategyPaperTrialRecord:
    return StrategyPaperTrialRecord(
        trial_id=str(row["trial_id"]),
        strategy_id=str(row["strategy_id"]),
        strategy_version=int(row["strategy_version"]),
        instrument_id=str(row["instrument_id"]),
        account_id=str(row["account_id"]),
        generation=int(row["generation"]),
        opened_at=_time(row["opened_at"]),
        opening_authorization_id=str(row["opening_authorization_id"]),
        opening_order_id=str(row["opening_order_id"]),
        opening_realized_pnl_baseline=Decimal(str(row["opening_realized_pnl_baseline"])),
        horizon_days=int(row["horizon_days"]),
        horizon_target_at=_time(row["horizon_target_at"]),
        cooldown_days=int(row["cooldown_days"]),
        policy_version=str(row["policy_version"]),
        legacy_adopted=bool(row["legacy_adopted"]),
        canonical_hash=str(row["canonical_hash"]),
        authority=str(row["authority"]),
    )


def _closure_from_row(row: RowMapping) -> StrategyPaperTrialClosureRecord:
    return StrategyPaperTrialClosureRecord(
        closure_id=str(row["closure_id"]),
        trial_id=str(row["trial_id"]),
        strategy_id=str(row["strategy_id"]),
        instrument_id=str(row["instrument_id"]),
        closed_at=_time(row["closed_at"]),
        terminal_reason=StrategyPaperTrialTerminalReason(str(row["terminal_reason"])),
        closing_authorization_id=str(row["closing_authorization_id"]),
        closing_order_id=str(row["closing_order_id"]),
        realized_paper_pnl=_optional_decimal(row["realized_paper_pnl"]),
        next_eligible_at=_time(row["next_eligible_at"]),
        coverage_window_duration_seconds=_required_int(
            row["coverage_window_duration_seconds"], column="coverage_window_duration_seconds"
        ),
        coverage_nominal_days=_required_int(
            row["coverage_nominal_days"], column="coverage_nominal_days"
        ),
        coverage_covered_days=_required_int(
            row["coverage_covered_days"], column="coverage_covered_days"
        ),
        coverage_qualifying_observation_count=_required_int(
            row["coverage_qualifying_observation_count"],
            column="coverage_qualifying_observation_count",
        ),
        coverage_largest_gap_seconds=_required_int(
            row["coverage_largest_gap_seconds"], column="coverage_largest_gap_seconds"
        ),
        coverage_policy_version=_required_str(
            row["coverage_policy_version"], column="coverage_policy_version"
        ),
        canonical_hash=str(row["canonical_hash"]),
        authority=str(row["authority"]),
    )


class PostgresStrategyPaperTrialRepository:
    """Implements ``application.strategy_paper_trial_lifecycle.
    StrategyPaperTrialRepository`` against real PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(self, *, record: StrategyPaperTrialRecord) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = connection.execute(
                    sa.select(internal_strategy_paper_trial_table.c.canonical_hash).where(
                        internal_strategy_paper_trial_table.c.trial_id == record.trial_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != record.canonical_hash:
                        raise StrategyPaperTrialLifecycleResearchError(
                            "STRATEGY_PAPER_TRIAL_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    internal_strategy_paper_trial_table.insert().values(
                        trial_id=record.trial_id,
                        strategy_id=record.strategy_id,
                        strategy_version=record.strategy_version,
                        instrument_id=record.instrument_id,
                        account_id=record.account_id,
                        generation=record.generation,
                        opened_at=record.opened_at,
                        opening_authorization_id=record.opening_authorization_id,
                        opening_order_id=record.opening_order_id,
                        opening_realized_pnl_baseline=record.opening_realized_pnl_baseline,
                        horizon_days=record.horizon_days,
                        horizon_target_at=record.horizon_target_at,
                        cooldown_days=record.cooldown_days,
                        policy_version=record.policy_version,
                        legacy_adopted=record.legacy_adopted,
                        canonical_hash=record.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise StrategyPaperTrialLifecycleResearchError(
                "STRATEGY_PAPER_TRIAL_PERSISTENCE_CONFLICT"
            ) from error

    def get(self, *, trial_id: str) -> StrategyPaperTrialRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(internal_strategy_paper_trial_table).where(
                        internal_strategy_paper_trial_table.c.trial_id == trial_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _trial_from_row(row)

    def for_slot(
        self, *, strategy_id: str, strategy_version: int, instrument_id: str
    ) -> tuple[StrategyPaperTrialRecord, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(internal_strategy_paper_trial_table)
                .where(
                    internal_strategy_paper_trial_table.c.strategy_id == strategy_id,
                    internal_strategy_paper_trial_table.c.strategy_version == strategy_version,
                    internal_strategy_paper_trial_table.c.instrument_id == instrument_id,
                )
                .order_by(internal_strategy_paper_trial_table.c.generation)
            ).mappings()
            return tuple(_trial_from_row(row) for row in rows)

    def for_strategy(self, *, strategy_id: str) -> tuple[StrategyPaperTrialRecord, ...]:
        """Every retained trial for one strategy_id, across versions and
        instruments (Strategy Version Provenance v1 inspection)."""

        table = internal_strategy_paper_trial_table
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(table)
                .where(table.c.strategy_id == strategy_id)
                .order_by(
                    table.c.strategy_version,
                    table.c.instrument_id,
                    table.c.generation,
                )
            ).mappings()
            return tuple(_trial_from_row(row) for row in rows)

    def active_trials(self) -> tuple[StrategyPaperTrialRecord, ...]:
        """PAPER Trial Closure Readiness v1: every retained trial with no
        closure row -- by construction (UNIQUE(trial_id) on the closure
        table) exactly the trials whose lifecycle is still ACTIVE. Read by
        the live observer to keep every active trial's instrument observed."""

        trial = internal_strategy_paper_trial_table
        closure = internal_strategy_paper_trial_closure_table
        closed = sa.select(closure.c.trial_id).where(closure.c.trial_id == trial.c.trial_id)
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(trial).where(~sa.exists(closed)).order_by(trial.c.trial_id)
            ).mappings()
            return tuple(_trial_from_row(row) for row in rows)


class PostgresStrategyPaperTrialClosureRepository:
    """Implements ``application.strategy_paper_trial_lifecycle.
    StrategyPaperTrialClosureRepository`` against real PostgreSQL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(self, *, record: StrategyPaperTrialClosureRecord) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = connection.execute(
                    sa.select(internal_strategy_paper_trial_closure_table.c.canonical_hash).where(
                        internal_strategy_paper_trial_closure_table.c.closure_id
                        == record.closure_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != record.canonical_hash:
                        raise StrategyPaperTrialLifecycleResearchError(
                            "STRATEGY_PAPER_TRIAL_CLOSURE_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    internal_strategy_paper_trial_closure_table.insert().values(
                        closure_id=record.closure_id,
                        trial_id=record.trial_id,
                        strategy_id=record.strategy_id,
                        instrument_id=record.instrument_id,
                        closed_at=record.closed_at,
                        terminal_reason=record.terminal_reason.value,
                        closing_authorization_id=record.closing_authorization_id,
                        closing_order_id=record.closing_order_id,
                        realized_paper_pnl=record.realized_paper_pnl,
                        next_eligible_at=record.next_eligible_at,
                        coverage_window_duration_seconds=record.coverage_window_duration_seconds,
                        coverage_nominal_days=record.coverage_nominal_days,
                        coverage_covered_days=record.coverage_covered_days,
                        coverage_qualifying_observation_count=(
                            record.coverage_qualifying_observation_count
                        ),
                        coverage_largest_gap_seconds=record.coverage_largest_gap_seconds,
                        coverage_policy_version=record.coverage_policy_version,
                        canonical_hash=record.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            # The table's own UNIQUE(trial_id) constraint is the DB-level
            # backstop for "at most one closure per trial" -- a genuinely
            # concurrent second-closure attempt lands here even though
            # application.strategy_paper_trial_lifecycle.
            # RecordStrategyPaperTrialClosed already checks this first.
            raise StrategyPaperTrialLifecycleResearchError(
                "STRATEGY_PAPER_TRIAL_CLOSURE_PERSISTENCE_CONFLICT"
            ) from error

    def get(self, *, closure_id: str) -> StrategyPaperTrialClosureRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(internal_strategy_paper_trial_closure_table).where(
                        internal_strategy_paper_trial_closure_table.c.closure_id == closure_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _closure_from_row(row)

    def for_trial(self, *, trial_id: str) -> StrategyPaperTrialClosureRecord | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(internal_strategy_paper_trial_closure_table).where(
                        internal_strategy_paper_trial_closure_table.c.trial_id == trial_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _closure_from_row(row)

    def for_strategy(self, *, strategy_id: str) -> tuple[StrategyPaperTrialClosureRecord, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(internal_strategy_paper_trial_closure_table)
                .where(internal_strategy_paper_trial_closure_table.c.strategy_id == strategy_id)
                .order_by(internal_strategy_paper_trial_closure_table.c.closed_at)
            ).mappings()
            return tuple(_closure_from_row(row) for row in rows)

    def all_closures(self) -> tuple[StrategyPaperTrialClosureRecord, ...]:
        """Every retained closure, oldest first (Strategy PAPER Trial Outcome
        Assessment v1 discovery). ``trial_id`` breaks a ``closed_at`` tie."""

        table = internal_strategy_paper_trial_closure_table
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(table).order_by(table.c.closed_at, table.c.trial_id)
            ).mappings()
            return tuple(_closure_from_row(row) for row in rows)
