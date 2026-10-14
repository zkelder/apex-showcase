"""Idempotent orchestration for durable Investment Mandate versions and evaluation.

This module performs no provider I/O.  It composes an already-persisted
mandate lineage, an already-persisted normalized portfolio snapshot
(``application.portfolio_state``), and an already-persisted
``SharedOpportunityProjection`` and hands them to the pure
``domain.investment_mandate.evaluate_mandate`` function.  It never submits
an order, creates a reservation, or grants execution authority.

Point-in-time correctness is the central concern here: evaluating a past
Opportunity must use the mandate version and portfolio snapshot that were
actually in effect at that time, never today's, unless the caller
explicitly asks to re-evaluate under today's mandate/portfolio (which is a
legitimate, separate operation -- simply pass today's timestamp).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.domain.investment_mandate import (
    DEFAULT_MANDATE_LINEAGE_ID,
    AutonomyRequirement,
    InvestmentMandate,
    InvestmentMandateError,
    MandateEvaluation,
    evaluate_mandate,
)
from apex.domain.portfolio_state import PortfolioPosition, PortfolioSnapshot
from apex.domain.shared_opportunity import SharedOpportunityProjection
from apex.domain.values import DecimalInput, RiskFraction


class InvestmentMandateRepository(Protocol):
    def mandate(self, mandate_id: str) -> InvestmentMandate | None: ...
    def latest_mandate_version(self, lineage_id: str) -> InvestmentMandate | None: ...
    def active_mandate(self, lineage_id: str, *, as_of: datetime) -> InvestmentMandate | None: ...
    def save_mandate(self, *, mandate: InvestmentMandate, idempotency_key: str) -> bool: ...
    def portfolio_snapshot(self, snapshot_id: str) -> PortfolioSnapshot | None: ...
    def portfolio_snapshot_active_as_of(self, as_of: datetime) -> PortfolioSnapshot | None: ...
    def portfolio_positions(self, snapshot_id: str) -> tuple[PortfolioPosition, ...]: ...
    def opportunity(self, projection_id: str) -> SharedOpportunityProjection | None: ...
    def save_evaluation(self, *, evaluation: MandateEvaluation, idempotency_key: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class MandateVersionResult:
    mandate: InvestmentMandate
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class MandateEvaluationResult:
    evaluation: MandateEvaluation
    idempotent_replay: bool


class CreateInvestmentMandate:
    """Author the next immutable version in a mandate lineage.

    The previous version is never mutated or deleted; a new version is
    simply appended with an incremented ``version`` and
    ``previous_mandate_id`` pointing at the prior one.
    """

    def __init__(self, repository: InvestmentMandateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        idempotency_key: str,
        lineage_id: str = DEFAULT_MANDATE_LINEAGE_ID,
        effective_at: datetime,
        created_at: datetime,
        total_capital_allocated: DecimalInput,
        permitted_venues: tuple[str, ...] = (),
        permitted_market_families: tuple[str, ...] = (),
        disallowed_market_families: tuple[str, ...] = (),
        maximum_aggregate_exposure_fraction: RiskFraction | DecimalInput,
        maximum_per_instrument_fraction: RiskFraction | DecimalInput,
        maximum_venue_concentration_fraction: RiskFraction | DecimalInput,
        maximum_market_family_concentration_fraction: RiskFraction | DecimalInput,
        leverage_permitted: bool = False,
        short_exposure_permitted: bool = False,
        options_permitted: bool = False,
        options_defined_risk_only: bool = True,
        minimum_liquidity_state: str = "SUFFICIENT",
        maximum_capital_lockup_days: int | None = None,
        autonomy_requirement: AutonomyRequirement = AutonomyRequirement.OPERATOR_APPROVAL_REQUIRED,
        notes: str = "",
    ) -> MandateVersionResult:
        latest = self._repository.latest_mandate_version(lineage_id)
        version = 1 if latest is None else latest.version + 1
        previous_mandate_id = None if latest is None else latest.mandate_id
        mandate = InvestmentMandate.create(
            lineage_id=lineage_id,
            version=version,
            previous_mandate_id=previous_mandate_id,
            effective_at=effective_at,
            created_at=created_at,
            total_capital_allocated=total_capital_allocated,
            permitted_venues=permitted_venues,
            permitted_market_families=permitted_market_families,
            disallowed_market_families=disallowed_market_families,
            maximum_aggregate_exposure_fraction=maximum_aggregate_exposure_fraction,
            maximum_per_instrument_fraction=maximum_per_instrument_fraction,
            maximum_venue_concentration_fraction=maximum_venue_concentration_fraction,
            maximum_market_family_concentration_fraction=maximum_market_family_concentration_fraction,
            leverage_permitted=leverage_permitted,
            short_exposure_permitted=short_exposure_permitted,
            options_permitted=options_permitted,
            options_defined_risk_only=options_defined_risk_only,
            minimum_liquidity_state=minimum_liquidity_state,
            maximum_capital_lockup_days=maximum_capital_lockup_days,
            autonomy_requirement=autonomy_requirement,
            notes=notes,
        )
        replay = self._repository.save_mandate(mandate=mandate, idempotency_key=idempotency_key)
        return MandateVersionResult(mandate, replay)


class EvaluateOpportunityAgainstMandate:
    """Evaluate one retained Opportunity against the mandate/portfolio truth in effect.

    Unless ``portfolio_snapshot_id`` is given explicitly, the portfolio
    snapshot used is the latest one whose ``as_of`` is not after
    ``evaluated_at`` -- never a later, "more current" snapshot.  Likewise
    the mandate used is always the version active as of ``evaluated_at``,
    never simply the latest version.  Re-evaluating an old Opportunity
    under today's mandate/portfolio is only possible by explicitly passing
    today's timestamp -- it is never the default.
    """

    def __init__(self, repository: InvestmentMandateRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        idempotency_key: str,
        opportunity_id: str,
        evaluated_at: datetime,
        mandate_lineage_id: str = DEFAULT_MANDATE_LINEAGE_ID,
        portfolio_snapshot_id: str | None = None,
    ) -> MandateEvaluationResult:
        mandate = self._repository.active_mandate(mandate_lineage_id, as_of=evaluated_at)
        if mandate is None:
            raise InvestmentMandateError("MANDATE_EVALUATION_NO_ACTIVE_MANDATE_FOR_LINEAGE")
        opportunity = self._repository.opportunity(opportunity_id)
        if opportunity is None:
            raise InvestmentMandateError("MANDATE_EVALUATION_OPPORTUNITY_NOT_FOUND")
        snapshot = (
            self._repository.portfolio_snapshot(portfolio_snapshot_id)
            if portfolio_snapshot_id is not None
            else self._repository.portfolio_snapshot_active_as_of(evaluated_at)
        )
        if snapshot is None:
            raise InvestmentMandateError("MANDATE_EVALUATION_NO_PORTFOLIO_SNAPSHOT_AVAILABLE")
        if snapshot.as_of > evaluated_at:
            raise InvestmentMandateError("MANDATE_EVALUATION_PORTFOLIO_SNAPSHOT_FROM_THE_FUTURE")
        positions = self._repository.portfolio_positions(snapshot.snapshot_id)
        evaluation = evaluate_mandate(
            mandate=mandate,
            portfolio=snapshot,
            positions=positions,
            opportunity=opportunity,
            evaluated_at=evaluated_at,
        )
        replay = self._repository.save_evaluation(
            evaluation=evaluation, idempotency_key=idempotency_key
        )
        return MandateEvaluationResult(evaluation, replay)
