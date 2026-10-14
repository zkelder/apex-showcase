"""Research-only Portfolio Intelligence and Command Center aggregation."""

from __future__ import annotations

from typing import Protocol

from apex.domain.portfolio_intelligence import (
    PortfolioIntelligenceRun,
    PortfolioOpportunity,
    PortfolioScenario,
    build_portfolio_intelligence_run,
    conservative_dependencies,
    scenario_allocations,
)


class PortfolioIntelligenceError(RuntimeError):
    pass


class PortfolioIntelligenceRepository(Protocol):
    def run_by_key(self, key: str) -> PortfolioIntelligenceRun | None: ...
    def save_run(self, key: str, run: PortfolioIntelligenceRun) -> PortfolioIntelligenceRun: ...


class RunPortfolioIntelligence:
    def __init__(self, repository: PortfolioIntelligenceRepository) -> None:
        self._repository = repository

    def execute(
        self, *, opportunities: tuple[PortfolioOpportunity, ...], idempotency_key: str
    ) -> PortfolioIntelligenceRun:
        candidate = build_portfolio_intelligence_run(
            opportunities=opportunities, idempotency_key=idempotency_key
        )
        old = self._repository.run_by_key(idempotency_key)
        if old is not None:
            if old.canonical_hash != candidate.canonical_hash:
                raise PortfolioIntelligenceError("PORTFOLIO_INTELLIGENCE_IDEMPOTENCY_CONFLICT")
            return old
        return self._repository.save_run(idempotency_key, candidate)


def command_center(
    *, portfolio: PortfolioIntelligenceRun, shadow: dict[str, object]
) -> dict[str, object]:
    """Operator read model; deliberately exposes no execution controls."""
    dependencies = portfolio.dependencies or conservative_dependencies(portfolio.opportunities)
    return {
        "warnings": (
            "PORTFOLIO INTELLIGENCE IS RESEARCH ONLY",
            "RESEARCH ATTRACTIVENESS IS NOT CAPITAL AUTHORITY",
            "CASH AND ABSTENTION ARE VALID DECISIONS",
        ),
        "shadow_operations": shadow,
        "ranked_opportunities": tuple(
            {
                "opportunity_id": item.opportunity_id,
                "source": item.source.value,
                "instrument": item.instrument_id,
                "research_utility": item.research_utility,
                "authority_adjusted_utility": item.authority_adjusted_utility,
                "maturity": item.maturity.value,
                "authority_ceiling": item.authority_ceiling.value,
                "blockers": item.blockers,
            }
            for item in sorted(
                portfolio.opportunities, key=lambda x: x.research_utility, reverse=True
            )
        ),
        "research_desired_allocations": portfolio.desired_allocations,
        "authority_permitted_allocations": portfolio.permitted_allocations,
        "dependency_matrix": dependencies,
        "non_executable_instructions": portfolio.instructions,
        "scenarios": {
            scenario.value: scenario_allocations(run=portfolio, scenario=scenario)
            for scenario in PortfolioScenario
        },
        "financial_authority": "NONE",
    }


class InMemoryPortfolioIntelligenceRepository:
    def __init__(self) -> None:
        self._runs: dict[str, PortfolioIntelligenceRun] = {}

    def run_by_key(self, key: str) -> PortfolioIntelligenceRun | None:
        return self._runs.get(key)

    def save_run(self, key: str, run: PortfolioIntelligenceRun) -> PortfolioIntelligenceRun:
        self._runs[key] = run
        return run
