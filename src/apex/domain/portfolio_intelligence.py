"""Research-only cross-vertical portfolio normalization and conservative ranking."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import content_digest, deterministic_id, parse_decimal

APEX_PORTFOLIO_OPPORTUNITY_V1 = "APEX_PORTFOLIO_OPPORTUNITY_V1"
APEX_OPPORTUNITY_MATURITY_V1 = "APEX_OPPORTUNITY_MATURITY_V1"
APEX_RESEARCH_CAPITAL_UTILITY_V1 = "APEX_RESEARCH_CAPITAL_UTILITY_V1"
APEX_CROSS_MARKET_RANKING_V1 = "APEX_CROSS_MARKET_RANKING_V1"
APEX_RESEARCH_PORTFOLIO_INTELLIGENCE_V1 = "APEX_RESEARCH_PORTFOLIO_INTELLIGENCE_V1"
APEX_CROSS_STRATEGY_DEPENDENCY_V1 = "APEX_CROSS_STRATEGY_DEPENDENCY_V1"
APEX_PORTFOLIO_FUTURE_HANDOFF_V1 = "APEX_PORTFOLIO_FUTURE_HANDOFF_V1"


class OpportunitySource(StrEnum):
    SECURITIES_META_STRATEGY = "SECURITIES_META_STRATEGY"
    SECURITIES_BASELINE = "SECURITIES_BASELINE"
    SECURITIES_RESEARCH_MODEL = "SECURITIES_RESEARCH_MODEL"
    EVENT_MARKET = "EVENT_MARKET"
    CASH = "CASH"


class OpportunityMaturity(StrEnum):
    HYPOTHESIS = "HYPOTHESIS"
    HISTORICAL_RESEARCH = "HISTORICAL_RESEARCH"
    INTEGRITY_BLOCKED = "INTEGRITY_BLOCKED"
    FORWARD_SHADOW_EARLY = "FORWARD_SHADOW_EARLY"
    FORWARD_SHADOW_MATURE = "FORWARD_SHADOW_MATURE"
    PAPER_ELIGIBLE_FOR_REVIEW = "PAPER_ELIGIBLE_FOR_REVIEW"
    PAPER_ACTIVE = "PAPER_ACTIVE"
    LIVE_ELIGIBLE_FOR_REVIEW = "LIVE_ELIGIBLE_FOR_REVIEW"
    LIVE_ACTIVE = "LIVE_ACTIVE"
    REVOKED = "REVOKED"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


class AuthorityCeiling(StrEnum):
    NONE = "NONE"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    SHADOW_ONLY = "SHADOW_ONLY"
    PAPER_REVIEW_ONLY = "PAPER_REVIEW_ONLY"
    PAPER = "PAPER"
    LIVE_REVIEW_ONLY = "LIVE_REVIEW_ONLY"
    LIVE = "LIVE"


class PortfolioScenario(StrEnum):
    BASE = "BASE"
    COST_STRESS = "COST_STRESS"
    VOLATILITY_STRESS = "VOLATILITY_STRESS"
    CORRELATION_SHOCK = "CORRELATION_SHOCK"
    LIQUIDITY_STRESS = "LIQUIDITY_STRESS"
    EVENT_LOCKUP_STRESS = "EVENT_LOCKUP_STRESS"
    PROVIDER_OUTAGE = "PROVIDER_OUTAGE"
    MODEL_DEGRADATION = "MODEL_DEGRADATION"
    ALL_RISK_OFF = "ALL_RISK_OFF"


@dataclass(frozen=True, slots=True)
class PortfolioDependency:
    """Conservative cross-opportunity dependency evidence, never a price correlation claim."""

    dependency_id: str
    left_opportunity_id: str
    right_opportunity_id: str
    dependency_penalty: Decimal
    reason_code: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class NonExecutablePortfolioInstruction:
    """Future PAPER handoff evidence that is explicitly not executable in this milestone."""

    instruction_id: str
    opportunity_id: str
    desired_allocation: Decimal
    permitted_allocation: Decimal
    maturity: OpportunityMaturity
    authority_ceiling: AuthorityCeiling
    blockers: tuple[str, ...]
    state: str
    executable: bool
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class PortfolioOpportunity:
    opportunity_id: str
    source: OpportunitySource
    source_id: str
    instrument_id: str
    expected_net_return: Decimal
    positive_probability: Decimal
    downside: Decimal
    uncertainty: Decimal
    liquidity: Decimal
    maturity: OpportunityMaturity
    authority_ceiling: AuthorityCeiling
    blockers: tuple[str, ...]
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        source: OpportunitySource,
        source_id: str,
        instrument_id: str,
        expected_net_return: str,
        positive_probability: str,
        downside: str,
        uncertainty: str,
        liquidity: str,
        maturity: OpportunityMaturity,
        authority_ceiling: AuthorityCeiling,
        blockers: tuple[str, ...] = (),
    ) -> PortfolioOpportunity:
        values = tuple(
            parse_decimal(item, allow_negative=True)
            for item in (
                expected_net_return,
                positive_probability,
                downside,
                uncertainty,
                liquidity,
            )
        )
        if not Decimal() <= values[1] <= Decimal(1) or not Decimal() <= values[3] <= Decimal(1):
            raise ValueError("PORTFOLIO_PROBABILITY_OR_UNCERTAINTY_INVALID")
        payload = (
            APEX_PORTFOLIO_OPPORTUNITY_V1,
            source,
            source_id,
            instrument_id,
            values,
            maturity,
            authority_ceiling,
            tuple(sorted(blockers)),
        )
        return cls(
            deterministic_id("apex_portfolio_opportunity", payload),
            source,
            source_id,
            instrument_id,
            values[0],
            values[1],
            values[2],
            values[3],
            values[4],
            maturity,
            authority_ceiling,
            tuple(sorted(blockers)),
            content_digest(payload),
        )

    @property
    def research_utility(self) -> Decimal:
        if self.source is OpportunitySource.CASH:
            return Decimal("0.0001")
        return max(
            Decimal(),
            self.expected_net_return * self.positive_probability
            - abs(self.downside) * self.uncertainty,
        )

    @property
    def authority_adjusted_utility(self) -> Decimal:
        if self.source is OpportunitySource.CASH:
            return self.research_utility
        if self.blockers or self.authority_ceiling in {
            AuthorityCeiling.NONE,
            AuthorityCeiling.RESEARCH_ONLY,
            AuthorityCeiling.SHADOW_ONLY,
        }:
            return Decimal()
        return self.research_utility


@dataclass(frozen=True, slots=True)
class PortfolioIntelligenceRun:
    run_id: str
    opportunities: tuple[PortfolioOpportunity, ...]
    desired_allocations: tuple[tuple[str, Decimal], ...]
    permitted_allocations: tuple[tuple[str, Decimal], ...]
    canonical_hash: str
    dependencies: tuple[PortfolioDependency, ...] = ()
    instructions: tuple[NonExecutablePortfolioInstruction, ...] = ()


def conservative_dependencies(
    opportunities: tuple[PortfolioOpportunity, ...],
) -> tuple[PortfolioDependency, ...]:
    """Retain an explicit penalty whenever compatible dependency is unknown."""
    dependencies: list[PortfolioDependency] = []
    for index, left in enumerate(opportunities):
        for right in opportunities[index + 1 :]:
            if OpportunitySource.CASH in {left.source, right.source}:
                penalty, reason = Decimal(), "CASH_NON_DEPENDENT"
            elif left.instrument_id == right.instrument_id:
                penalty, reason = Decimal(1), "SHARED_INSTRUMENT"
            elif left.source_id == right.source_id:
                penalty, reason = Decimal("0.8"), "SHARED_MODEL_OR_EVIDENCE_SOURCE"
            else:
                penalty, reason = Decimal("0.5"), "UNKNOWN_DEPENDENCY_PENALTY"
            payload = (
                APEX_CROSS_STRATEGY_DEPENDENCY_V1,
                left.opportunity_id,
                right.opportunity_id,
                penalty,
                reason,
            )
            dependencies.append(
                PortfolioDependency(
                    deterministic_id("apex_portfolio_dependency", payload),
                    left.opportunity_id,
                    right.opportunity_id,
                    penalty,
                    reason,
                    content_digest(payload),
                )
            )
    return tuple(dependencies)


def build_portfolio_intelligence_run(
    *, opportunities: tuple[PortfolioOpportunity, ...], idempotency_key: str
) -> PortfolioIntelligenceRun:
    if not idempotency_key or not opportunities:
        raise ValueError("PORTFOLIO_RUN_INPUT_REQUIRED")
    ranked = tuple(
        sorted(
            opportunities,
            key=lambda item: (item.research_utility, item.opportunity_id),
            reverse=True,
        )
    )
    permitted = tuple(
        sorted(
            opportunities,
            key=lambda item: (item.authority_adjusted_utility, item.opportunity_id),
            reverse=True,
        )
    )
    cash = next((item for item in opportunities if item.source is OpportunitySource.CASH), None)
    if cash is None:
        raise ValueError("PORTFOLIO_CASH_OPPORTUNITY_REQUIRED")
    desired_winner = ranked[0]
    desired = (
        ((desired_winner.opportunity_id, Decimal(1)),)
        if desired_winner.research_utility > 0
        else ((cash.opportunity_id, Decimal(1)),)
    )
    permitted_winner = permitted[0]
    allowed = (
        permitted_winner.authority_adjusted_utility > 0
        and permitted_winner.source is not OpportunitySource.CASH
    )
    allocations = (
        ((permitted_winner.opportunity_id, Decimal(1)),)
        if allowed
        else ((cash.opportunity_id, Decimal(1)),)
    )
    dependencies = conservative_dependencies(opportunities)
    desired_by_id = dict(desired)
    permitted_by_id = dict(allocations)
    instructions = tuple(
        _non_executable_instruction(
            item=item,
            desired_allocation=desired_by_id.get(item.opportunity_id, Decimal()),
            permitted_allocation=permitted_by_id.get(item.opportunity_id, Decimal()),
        )
        for item in opportunities
        if item.source is not OpportunitySource.CASH
    )
    payload = (
        APEX_RESEARCH_PORTFOLIO_INTELLIGENCE_V1,
        tuple(item.canonical_hash for item in opportunities),
        tuple(item.canonical_hash for item in dependencies),
        desired,
        allocations,
        tuple(item.canonical_hash for item in instructions),
        idempotency_key,
    )
    return PortfolioIntelligenceRun(
        deterministic_id("apex_portfolio_intelligence_run", payload),
        opportunities,
        desired,
        allocations,
        content_digest(payload),
        dependencies,
        instructions,
    )


def _non_executable_instruction(
    *,
    item: PortfolioOpportunity,
    desired_allocation: Decimal,
    permitted_allocation: Decimal,
) -> NonExecutablePortfolioInstruction:
    payload = (
        APEX_PORTFOLIO_FUTURE_HANDOFF_V1,
        item.opportunity_id,
        desired_allocation,
        permitted_allocation,
        item.maturity,
        item.authority_ceiling,
        item.blockers,
        "RESEARCH_ONLY",
        False,
    )
    return NonExecutablePortfolioInstruction(
        deterministic_id("apex_portfolio_instruction", payload),
        item.opportunity_id,
        desired_allocation,
        permitted_allocation,
        item.maturity,
        item.authority_ceiling,
        item.blockers,
        "RESEARCH_ONLY",
        False,
        content_digest(payload),
    )


def scenario_allocations(
    *, run: PortfolioIntelligenceRun, scenario: PortfolioScenario
) -> tuple[tuple[str, Decimal], ...]:
    """Return a conservative research allocation; scenarios never grant authority."""
    cash = next(item for item in run.opportunities if item.source is OpportunitySource.CASH)
    if scenario in {
        PortfolioScenario.ALL_RISK_OFF,
        PortfolioScenario.PROVIDER_OUTAGE,
        PortfolioScenario.MODEL_DEGRADATION,
    }:
        return ((cash.opportunity_id, Decimal(1)),)
    return run.permitted_allocations
