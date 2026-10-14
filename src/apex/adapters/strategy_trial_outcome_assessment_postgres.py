"""Immutable PostgreSQL evidence for Strategy PAPER Trial Outcome Assessment v1.

One append-only table, ``strategy_trial_outcome_assessment`` (migration
``0117``), one row per ``(trial_id, policy_version)``. Implements
``application.strategy_trial_outcome_assessment.
StrategyTrialOutcomeAssessmentRepository``. Every read re-derives the row's
identity and canonical hash from its own columns
(``verify_strategy_trial_outcome_assessment``), so a row that does not hash
to itself is never returned. No provider client, credential, or network
capability lives here, and no row carries an ``authority`` other than
``'NONE'``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.application.strategy_trial_outcome_assessment import (
    StrategyTrialOutcomeAssessmentResearchError,
)
from apex.domain.strategy_paper_trial_lifecycle import StrategyPaperTrialTerminalReason
from apex.domain.strategy_trial_outcome_assessment import (
    ExpectedReturnBasis,
    StrategyTrialOutcomeAssessment,
    StrategyTrialOutcomeStatus,
    TrialEconomicsState,
    verify_strategy_trial_outcome_assessment,
)
from apex.domain.strategy_version_provenance import (
    StrategyEvaluationSource,
    StrategyVersionRegistry,
    TrialProvenanceClass,
)

metadata = sa.MetaData()


def _numeric(name: str, *, nullable: bool) -> sa.Column[Decimal]:
    return sa.Column(name, sa.Numeric, nullable=nullable)


strategy_trial_outcome_assessment_table = sa.Table(
    "strategy_trial_outcome_assessment",
    metadata,
    sa.Column("assessment_id", sa.String(300), primary_key=True),
    sa.Column("policy_version", sa.String(60), nullable=False),
    sa.Column("status", sa.String(40), nullable=False),
    sa.Column("reason_codes", sa.Text(), nullable=False),
    sa.Column("trial_id", sa.String(300), nullable=False),
    sa.Column("trial_hash", sa.String(64), nullable=False),
    sa.Column("strategy_id", sa.String(300), nullable=False),
    sa.Column("strategy_version", sa.Integer, nullable=False),
    sa.Column("instrument_id", sa.String(60), nullable=False),
    sa.Column("account_id", sa.String(300), nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("trial_legacy_adopted", sa.Boolean, nullable=False),
    sa.Column("trial_policy_version", sa.String(60), nullable=False),
    sa.Column("horizon_days", sa.Integer, nullable=False),
    sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("closure_id", sa.String(300), nullable=False),
    sa.Column("closure_hash", sa.String(64), nullable=False),
    sa.Column("terminal_reason", sa.String(40), nullable=False),
    sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("provenance_class", sa.String(40), nullable=False),
    sa.Column("version_registry", sa.String(40), nullable=True),
    sa.Column("definition_hash", sa.String(64), nullable=True),
    sa.Column("provenance_hash", sa.String(64), nullable=True),
    sa.Column("transition_id", sa.String(300), nullable=True),
    sa.Column("transition_hash", sa.String(64), nullable=True),
    sa.Column("transition_kind", sa.String(40), nullable=True),
    sa.Column("gate_policy_version", sa.String(60), nullable=True),
    sa.Column("evaluation_source", sa.String(40), nullable=True),
    sa.Column("evaluation_id", sa.String(300), nullable=True),
    sa.Column("evaluation_hash", sa.String(64), nullable=True),
    sa.Column("evaluated_version", sa.Integer, nullable=True),
    sa.Column("evaluation_as_of", sa.DateTime(timezone=True), nullable=True),
    sa.Column("evaluation_kind", sa.String(40), nullable=True),
    sa.Column("evaluation_window_start", sa.DateTime(timezone=True), nullable=True),
    sa.Column("evaluation_window_end", sa.DateTime(timezone=True), nullable=True),
    sa.Column("evaluation_sample_size", sa.Integer, nullable=True),
    _numeric("evaluation_net_return", nullable=True),
    _numeric("evaluation_average_trade_return", nullable=True),
    sa.Column("expected_return_basis", sa.String(60), nullable=False),
    sa.Column("opening_attribution_id", sa.String(300), nullable=False),
    sa.Column("opening_attribution_hash", sa.String(64), nullable=False),
    sa.Column("closing_attribution_id", sa.String(300), nullable=True),
    sa.Column("closing_attribution_hash", sa.String(64), nullable=True),
    sa.Column("economics_state", sa.String(20), nullable=False),
    sa.Column("opening_fill_count", sa.Integer, nullable=False),
    _numeric("opening_base_quantity", nullable=False),
    _numeric("opening_quote_quantity", nullable=False),
    _numeric("opening_fees", nullable=False),
    _numeric("opening_cost", nullable=False),
    sa.Column("closing_fill_count", sa.Integer, nullable=False),
    _numeric("closing_base_quantity", nullable=False),
    _numeric("closing_quote_quantity", nullable=False),
    _numeric("closing_fees", nullable=False),
    _numeric("closing_net_proceeds", nullable=False),
    _numeric("cash_flow_pnl", nullable=True),
    _numeric("ledger_realized_pnl", nullable=True),
    _numeric("rounding_residual", nullable=True),
    _numeric("reconciliation_tolerance", nullable=False),
    _numeric("realized_return", nullable=True),
    sa.Column("realized_return_basis", sa.String(60), nullable=False),
    sa.Column("population_id", sa.String(300), nullable=True),
    sa.Column("population_members_hash", sa.String(64), nullable=True),
    sa.Column("population_size", sa.Integer, nullable=False),
    sa.Column("minimum_sample_size", sa.Integer, nullable=False),
    _numeric("population_mean_realized_return", nullable=True),
    sa.Column("population_positive_count", sa.Integer, nullable=True),
    _numeric("population_total_realized_pnl", nullable=True),
    sa.Column("canonical_hash", sa.String(64), nullable=False),
    sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("authority", sa.String(16), nullable=False, server_default="NONE"),
    sa.UniqueConstraint(
        "trial_id", "policy_version", name="uq_strategy_trial_outcome_assessment_trial_policy"
    ),
)

STRATEGY_TRIAL_OUTCOME_ASSESSMENT_TABLES = ("strategy_trial_outcome_assessment",)

_ROW_COLUMNS = tuple(
    column.name
    for column in strategy_trial_outcome_assessment_table.c
    if column.name != "assessed_at"
)


def _time(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise StrategyTrialOutcomeAssessmentResearchError(
            "STRATEGY_TRIAL_OUTCOME_ASSESSMENT_ROW_TIMESTAMP_INVALID"
        )
    return value.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _values(assessment: StrategyTrialOutcomeAssessment) -> dict[str, object]:
    values: dict[str, object] = {}
    for name in _ROW_COLUMNS:
        value = getattr(assessment, name)
        if name == "reason_codes":
            value = json.dumps(list(value), separators=(",", ":"))
        elif isinstance(value, Enum):
            value = value.value
        values[name] = value
    return values


def _assessment_from_row(row: RowMapping) -> StrategyTrialOutcomeAssessment:
    reason_codes = json.loads(str(row["reason_codes"]))
    return verify_strategy_trial_outcome_assessment(
        StrategyTrialOutcomeAssessment(
            assessment_id=row["assessment_id"],
            policy_version=row["policy_version"],
            status=StrategyTrialOutcomeStatus(row["status"]),
            reason_codes=tuple(str(item) for item in reason_codes),
            trial_id=row["trial_id"],
            trial_hash=row["trial_hash"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            instrument_id=row["instrument_id"],
            account_id=row["account_id"],
            generation=row["generation"],
            trial_legacy_adopted=row["trial_legacy_adopted"],
            trial_policy_version=row["trial_policy_version"],
            horizon_days=row["horizon_days"],
            opened_at=_time(row["opened_at"]),
            closure_id=row["closure_id"],
            closure_hash=row["closure_hash"],
            terminal_reason=StrategyPaperTrialTerminalReason(row["terminal_reason"]),
            closed_at=_time(row["closed_at"]),
            provenance_class=TrialProvenanceClass(row["provenance_class"]),
            version_registry=(
                None
                if row["version_registry"] is None
                else StrategyVersionRegistry(row["version_registry"])
            ),
            definition_hash=row["definition_hash"],
            provenance_hash=row["provenance_hash"],
            transition_id=row["transition_id"],
            transition_hash=row["transition_hash"],
            transition_kind=row["transition_kind"],
            gate_policy_version=row["gate_policy_version"],
            evaluation_source=(
                None
                if row["evaluation_source"] is None
                else StrategyEvaluationSource(row["evaluation_source"])
            ),
            evaluation_id=row["evaluation_id"],
            evaluation_hash=row["evaluation_hash"],
            evaluated_version=row["evaluated_version"],
            evaluation_as_of=_optional_time(row["evaluation_as_of"]),
            evaluation_kind=row["evaluation_kind"],
            evaluation_window_start=_optional_time(row["evaluation_window_start"]),
            evaluation_window_end=_optional_time(row["evaluation_window_end"]),
            evaluation_sample_size=row["evaluation_sample_size"],
            evaluation_net_return=row["evaluation_net_return"],
            evaluation_average_trade_return=row["evaluation_average_trade_return"],
            expected_return_basis=ExpectedReturnBasis(row["expected_return_basis"]),
            opening_attribution_id=row["opening_attribution_id"],
            opening_attribution_hash=row["opening_attribution_hash"],
            closing_attribution_id=row["closing_attribution_id"],
            closing_attribution_hash=row["closing_attribution_hash"],
            economics_state=TrialEconomicsState(row["economics_state"]),
            opening_fill_count=row["opening_fill_count"],
            opening_base_quantity=row["opening_base_quantity"],
            opening_quote_quantity=row["opening_quote_quantity"],
            opening_fees=row["opening_fees"],
            opening_cost=row["opening_cost"],
            closing_fill_count=row["closing_fill_count"],
            closing_base_quantity=row["closing_base_quantity"],
            closing_quote_quantity=row["closing_quote_quantity"],
            closing_fees=row["closing_fees"],
            closing_net_proceeds=row["closing_net_proceeds"],
            cash_flow_pnl=row["cash_flow_pnl"],
            ledger_realized_pnl=row["ledger_realized_pnl"],
            rounding_residual=row["rounding_residual"],
            reconciliation_tolerance=row["reconciliation_tolerance"],
            realized_return=row["realized_return"],
            realized_return_basis=row["realized_return_basis"],
            population_id=row["population_id"],
            population_members_hash=row["population_members_hash"],
            population_size=row["population_size"],
            minimum_sample_size=row["minimum_sample_size"],
            population_mean_realized_return=row["population_mean_realized_return"],
            population_positive_count=row["population_positive_count"],
            population_total_realized_pnl=row["population_total_realized_pnl"],
            canonical_hash=row["canonical_hash"],
            authority=row["authority"],
        )
    )


class PostgresStrategyTrialOutcomeAssessmentRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(
        self, *, assessment: StrategyTrialOutcomeAssessment, assessed_at: datetime
    ) -> bool:
        verify_strategy_trial_outcome_assessment(assessment)
        table = strategy_trial_outcome_assessment_table
        try:
            with self._engine.begin() as connection:
                existing = connection.execute(
                    sa.select(table.c.canonical_hash).where(
                        table.c.assessment_id == assessment.assessment_id
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if existing != assessment.canonical_hash:
                        raise StrategyTrialOutcomeAssessmentResearchError(
                            "STRATEGY_TRIAL_OUTCOME_ASSESSMENT_IDENTITY_CONFLICT"
                        )
                    return True
                connection.execute(
                    table.insert().values(**_values(assessment), assessed_at=assessed_at)
                )
            return False
        except IntegrityError as error:
            # A concurrent writer for the same identity, or a lineage row the
            # composite foreign keys reject. Either way nothing was written.
            with self._engine.connect() as connection:
                existing = connection.execute(
                    sa.select(table.c.canonical_hash).where(
                        table.c.assessment_id == assessment.assessment_id
                    )
                ).scalar_one_or_none()
            if existing == assessment.canonical_hash:
                return True
            raise StrategyTrialOutcomeAssessmentResearchError(
                "STRATEGY_TRIAL_OUTCOME_ASSESSMENT_PERSISTENCE_CONFLICT"
            ) from error

    def _rows(
        self, statement: sa.Select[tuple[object, ...]]
    ) -> tuple[StrategyTrialOutcomeAssessment, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_assessment_from_row(row) for row in rows)

    def get(self, *, assessment_id: str) -> StrategyTrialOutcomeAssessment | None:
        table = strategy_trial_outcome_assessment_table
        found = self._rows(sa.select(table).where(table.c.assessment_id == assessment_id))
        return found[0] if found else None

    def for_trial(self, *, trial_id: str) -> tuple[StrategyTrialOutcomeAssessment, ...]:
        table = strategy_trial_outcome_assessment_table
        return self._rows(
            sa.select(table).where(table.c.trial_id == trial_id).order_by(table.c.policy_version)
        )

    def assessed_trial_ids(self, *, policy_version: str) -> frozenset[str]:
        table = strategy_trial_outcome_assessment_table
        with self._engine.connect() as connection:
            return frozenset(
                str(value)
                for value in connection.execute(
                    sa.select(table.c.trial_id).where(table.c.policy_version == policy_version)
                ).scalars()
            )

    def assessments(self) -> tuple[StrategyTrialOutcomeAssessment, ...]:
        table = strategy_trial_outcome_assessment_table
        return self._rows(sa.select(table).order_by(table.c.closed_at, table.c.trial_id))

    def assessed_at(self, *, assessment_id: str) -> datetime | None:
        table = strategy_trial_outcome_assessment_table
        with self._engine.connect() as connection:
            value = connection.execute(
                sa.select(table.c.assessed_at).where(table.c.assessment_id == assessment_id)
            ).scalar_one_or_none()
        return None if value is None else _time(value)
