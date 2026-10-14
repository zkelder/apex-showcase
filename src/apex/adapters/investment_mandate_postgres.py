"""Immutable PostgreSQL evidence for durable Investment Mandate versions and evaluations.

Composes ``PostgresPortfolioSnapshotRepository`` (normalized portfolio truth)
and ``PostgresSharedOpportunityRepository`` (retained Opportunities) to
implement ``application.investment_mandate.InvestmentMandateRepository``.
No provider client, credential, or network capability lives here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from apex.adapters.portfolio_state_postgres import PostgresPortfolioSnapshotRepository
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.domain.investment_mandate import (
    AutonomyRequirement,
    InvestmentMandate,
    InvestmentMandateError,
    MandateConstraintCheck,
    MandateDecisionState,
    MandateEvaluation,
)
from apex.domain.portfolio_state import PortfolioPosition, PortfolioSnapshot, PortfolioVenue
from apex.domain.shared_opportunity import SharedOpportunityProjection
from apex.domain.values import RiskFraction, canonical_json_bytes

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


mandate_table = _evidence_table(
    "investment_mandate",
    "mandate_id",
    sa.Column("lineage_id", sa.String(300), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column(
        "previous_mandate_id",
        sa.String(300),
        sa.ForeignKey("investment_mandate.mandate_id"),
        nullable=True,
    ),
    sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
    sa.UniqueConstraint("lineage_id", "version", name="uq_investment_mandate_lineage_version"),
)
evaluation_table = _evidence_table(
    "mandate_evaluation",
    "evaluation_id",
    sa.Column(
        "mandate_id", sa.String(300), sa.ForeignKey("investment_mandate.mandate_id"), nullable=False
    ),
    sa.Column(
        "portfolio_snapshot_id",
        sa.String(300),
        sa.ForeignKey("portfolio_snapshot.snapshot_id"),
        nullable=False,
    ),
    sa.Column(
        "opportunity_id",
        sa.String(300),
        sa.ForeignKey("shared_opportunity_projection.projection_id"),
        nullable=False,
    ),
    sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("state", sa.String(20), nullable=False),
    sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
)

INVESTMENT_MANDATE_IMMUTABLE_TABLES = tuple(metadata.tables)
INVESTMENT_MANDATE_TABLES = INVESTMENT_MANDATE_IMMUTABLE_TABLES


def _payload(value: object) -> str:
    return canonical_json_bytes(value).decode()


def _data(row: RowMapping) -> dict[str, Any]:
    value = json.loads(str(row["payload"]))
    if not isinstance(value, dict):
        raise InvestmentMandateError("MANDATE_RETAINED_PAYLOAD_INVALID")
    return value


def _time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvestmentMandateError("MANDATE_RETAINED_COLLECTION_INVALID")
    return tuple(str(item) for item in value)


def _optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _fraction(value: object) -> RiskFraction:
    if not isinstance(value, dict):
        raise InvestmentMandateError("MANDATE_RETAINED_FRACTION_INVALID")
    return RiskFraction(str(value["value"]))


def _mandate(row: RowMapping) -> InvestmentMandate:
    value = _data(row)
    return InvestmentMandate(
        mandate_id=str(value["mandate_id"]),
        lineage_id=str(value["lineage_id"]),
        version=int(value["version"]),
        previous_mandate_id=(
            None if value["previous_mandate_id"] is None else str(value["previous_mandate_id"])
        ),
        effective_at=_time(value["effective_at"]),
        created_at=_time(value["created_at"]),
        total_capital_allocated=Decimal(str(value["total_capital_allocated"])),
        permitted_venues=_strings(value["permitted_venues"]),
        permitted_market_families=_strings(value["permitted_market_families"]),
        disallowed_market_families=_strings(value["disallowed_market_families"]),
        maximum_aggregate_exposure_fraction=_fraction(value["maximum_aggregate_exposure_fraction"]),
        maximum_per_instrument_fraction=_fraction(value["maximum_per_instrument_fraction"]),
        maximum_venue_concentration_fraction=_fraction(
            value["maximum_venue_concentration_fraction"]
        ),
        maximum_market_family_concentration_fraction=_fraction(
            value["maximum_market_family_concentration_fraction"]
        ),
        leverage_permitted=bool(value["leverage_permitted"]),
        short_exposure_permitted=bool(value["short_exposure_permitted"]),
        options_permitted=bool(value["options_permitted"]),
        options_defined_risk_only=bool(value["options_defined_risk_only"]),
        minimum_liquidity_state=str(value["minimum_liquidity_state"]),
        maximum_capital_lockup_days=(
            None
            if value["maximum_capital_lockup_days"] is None
            else int(value["maximum_capital_lockup_days"])
        ),
        autonomy_requirement=AutonomyRequirement(value["autonomy_requirement"]),
        notes=str(value["notes"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


def _constraint_check(value: dict[str, Any]) -> MandateConstraintCheck:
    return MandateConstraintCheck(
        name=str(value["name"]),
        satisfied=bool(value["satisfied"]),
        binding=bool(value["binding"]),
        limit=None if value["limit"] is None else str(value["limit"]),
        observed=None if value["observed"] is None else str(value["observed"]),
        detail=str(value["detail"]),
    )


def _evaluation(row: RowMapping) -> MandateEvaluation:
    value = _data(row)
    return MandateEvaluation(
        evaluation_id=str(value["evaluation_id"]),
        mandate_id=str(value["mandate_id"]),
        mandate_lineage_id=str(value["mandate_lineage_id"]),
        mandate_version=int(value["mandate_version"]),
        mandate_hash=str(value["mandate_hash"]),
        portfolio_snapshot_id=str(value["portfolio_snapshot_id"]),
        portfolio_snapshot_hash=str(value["portfolio_snapshot_hash"]),
        opportunity_id=str(value["opportunity_id"]),
        opportunity_hash=str(value["opportunity_hash"]),
        venue=PortfolioVenue(value["venue"]),
        market_family=str(value["market_family"]),
        evaluated_at=_time(value["evaluated_at"]),
        state=MandateDecisionState(value["state"]),
        reason_codes=_strings(value["reason_codes"]),
        unknown_facts=_strings(value["unknown_facts"]),
        explanation=str(value["explanation"]),
        constraints_evaluated=tuple(
            _constraint_check(item) for item in value["constraints_evaluated"]
        ),
        required_capital=_optional_decimal(value["required_capital"]),
        permitted_allocation_amount=_optional_decimal(value["permitted_allocation_amount"]),
        warnings=_strings(value["warnings"]),
        canonical_hash=str(value["canonical_hash"]),
        authority="NONE",
    )


class PostgresInvestmentMandateRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._portfolio = PostgresPortfolioSnapshotRepository(engine)
        self._shared_opportunity = PostgresSharedOpportunityRepository(engine)

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

    def mandate(self, mandate_id: str) -> InvestmentMandate | None:
        row = self._one(sa.select(mandate_table).where(mandate_table.c.mandate_id == mandate_id))
        return None if row is None else _mandate(row)

    def latest_mandate_version(self, lineage_id: str) -> InvestmentMandate | None:
        row = self._one(
            sa.select(mandate_table)
            .where(mandate_table.c.lineage_id == lineage_id)
            .order_by(mandate_table.c.version.desc())
            .limit(1)
        )
        return None if row is None else _mandate(row)

    def active_mandate(self, lineage_id: str, *, as_of: datetime) -> InvestmentMandate | None:
        row = self._one(
            sa.select(mandate_table)
            .where(mandate_table.c.lineage_id == lineage_id, mandate_table.c.effective_at <= as_of)
            .order_by(mandate_table.c.version.desc())
            .limit(1)
        )
        return None if row is None else _mandate(row)

    def save_mandate(self, *, mandate: InvestmentMandate, idempotency_key: str) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = self._existing_hash(
                    connection, mandate_table, "mandate_id", mandate.mandate_id
                )
                if existing is not None:
                    retained_key = connection.scalar(
                        sa.select(mandate_table.c.idempotency_key).where(
                            mandate_table.c.mandate_id == mandate.mandate_id
                        )
                    )
                    if existing != mandate.canonical_hash or retained_key != idempotency_key:
                        raise InvestmentMandateError("MANDATE_IDENTITY_CONFLICT")
                    return True
                if connection.scalar(
                    sa.select(mandate_table.c.mandate_id).where(
                        mandate_table.c.idempotency_key == idempotency_key
                    )
                ):
                    raise InvestmentMandateError("MANDATE_IDEMPOTENCY_CONFLICT")
                if connection.scalar(
                    sa.select(mandate_table.c.mandate_id).where(
                        mandate_table.c.lineage_id == mandate.lineage_id,
                        mandate_table.c.version == mandate.version,
                    )
                ):
                    raise InvestmentMandateError("MANDATE_LINEAGE_VERSION_CONFLICT")
                connection.execute(
                    mandate_table.insert().values(
                        mandate_id=mandate.mandate_id,
                        lineage_id=mandate.lineage_id,
                        version=mandate.version,
                        previous_mandate_id=mandate.previous_mandate_id,
                        effective_at=mandate.effective_at,
                        created_at=mandate.created_at,
                        idempotency_key=idempotency_key,
                        payload=_payload(mandate),
                        canonical_hash=mandate.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise InvestmentMandateError("MANDATE_PERSISTENCE_CONFLICT") from error

    def portfolio_snapshot(self, snapshot_id: str) -> PortfolioSnapshot | None:
        return self._portfolio.portfolio_snapshot(snapshot_id)

    def portfolio_snapshot_active_as_of(self, as_of: datetime) -> PortfolioSnapshot | None:
        return self._portfolio.portfolio_snapshot_active_as_of(as_of)

    def portfolio_positions(self, snapshot_id: str) -> tuple[PortfolioPosition, ...]:
        return self._portfolio.portfolio_positions(snapshot_id)

    def opportunity(self, projection_id: str) -> SharedOpportunityProjection | None:
        return self._shared_opportunity.projection(projection_id)

    def evaluation(self, evaluation_id: str) -> MandateEvaluation | None:
        row = self._one(
            sa.select(evaluation_table).where(evaluation_table.c.evaluation_id == evaluation_id)
        )
        return None if row is None else _evaluation(row)

    def evaluations_for_mandate(self, mandate_id: str) -> tuple[MandateEvaluation, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(evaluation_table)
                .where(evaluation_table.c.mandate_id == mandate_id)
                .order_by(evaluation_table.c.evaluated_at, evaluation_table.c.evaluation_id)
            ).mappings()
            return tuple(_evaluation(row) for row in rows)

    def all_evaluations(self) -> tuple[MandateEvaluation, ...]:
        """Every retained evaluation across every mandate lineage/version,
        oldest first.  Read-only; added for the Counterfactual Decision
        Ledger (``domain.counterfactual_ledger``), which must be able to
        normalize every genuinely-retained mandate decision without first
        knowing which mandate_id(s) produced them."""

        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(evaluation_table).order_by(
                    evaluation_table.c.evaluated_at, evaluation_table.c.evaluation_id
                )
            ).mappings()
            return tuple(_evaluation(row) for row in rows)

    def save_evaluation(self, *, evaluation: MandateEvaluation, idempotency_key: str) -> bool:
        try:
            with self._engine.begin() as connection:
                existing = self._existing_hash(
                    connection, evaluation_table, "evaluation_id", evaluation.evaluation_id
                )
                if existing is not None:
                    retained_key = connection.scalar(
                        sa.select(evaluation_table.c.idempotency_key).where(
                            evaluation_table.c.evaluation_id == evaluation.evaluation_id
                        )
                    )
                    if existing != evaluation.canonical_hash or retained_key != idempotency_key:
                        raise InvestmentMandateError("MANDATE_EVALUATION_IDENTITY_CONFLICT")
                    return True
                if connection.scalar(
                    sa.select(evaluation_table.c.evaluation_id).where(
                        evaluation_table.c.idempotency_key == idempotency_key
                    )
                ):
                    raise InvestmentMandateError("MANDATE_EVALUATION_IDEMPOTENCY_CONFLICT")
                connection.execute(
                    evaluation_table.insert().values(
                        evaluation_id=evaluation.evaluation_id,
                        mandate_id=evaluation.mandate_id,
                        portfolio_snapshot_id=evaluation.portfolio_snapshot_id,
                        opportunity_id=evaluation.opportunity_id,
                        evaluated_at=evaluation.evaluated_at,
                        state=evaluation.state.value,
                        idempotency_key=idempotency_key,
                        payload=_payload(evaluation),
                        canonical_hash=evaluation.canonical_hash,
                        authority="NONE",
                    )
                )
            return False
        except IntegrityError as error:
            raise InvestmentMandateError("MANDATE_EVALUATION_PERSISTENCE_CONFLICT") from error

    def workspace(self) -> dict[str, object]:
        with self._engine.connect() as connection:
            lineages = (
                connection.execute(sa.select(mandate_table.c.lineage_id).distinct()).scalars().all()
            )
        active: dict[str, object] = {}
        now = datetime.now(tz=UTC)
        for lineage_id in sorted(str(item) for item in lineages):
            current = self.active_mandate(lineage_id, as_of=now)
            active[lineage_id] = current
        latest_snapshot = self._portfolio.latest_portfolio_snapshot()
        return {
            "mandate_lineages": tuple(sorted(str(item) for item in lineages)),
            "active_mandates": active,
            "latest_portfolio_snapshot_id": (
                None if latest_snapshot is None else latest_snapshot.snapshot_id
            ),
            "authority": "NONE",
        }
