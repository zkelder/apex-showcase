from apex.application.portfolio_intelligence import (
    InMemoryPortfolioIntelligenceRepository,
    PortfolioIntelligenceError,
    RunPortfolioIntelligence,
    command_center,
)
from apex.domain.portfolio_intelligence import (
    AuthorityCeiling,
    OpportunityMaturity,
    OpportunitySource,
    PortfolioOpportunity,
    PortfolioScenario,
    build_portfolio_intelligence_run,
    scenario_allocations,
)


def test_integrity_blocked_opportunity_retains_research_utility_but_cash_is_permitted() -> None:
    blocked = PortfolioOpportunity.create(
        source=OpportunitySource.SECURITIES_META_STRATEGY,
        source_id="ensemble",
        instrument_id="SPY",
        expected_net_return="0.02",
        positive_probability="0.7",
        downside="-0.01",
        uncertainty="0.1",
        liquidity="1",
        maturity=OpportunityMaturity.INTEGRITY_BLOCKED,
        authority_ceiling=AuthorityCeiling.RESEARCH_ONLY,
        blockers=("PBO_ABOVE_MAXIMUM",),
    )
    cash = PortfolioOpportunity.create(
        source=OpportunitySource.CASH,
        source_id="cash",
        instrument_id="USD",
        expected_net_return="0",
        positive_probability="1",
        downside="0",
        uncertainty="0",
        liquidity="1",
        maturity=OpportunityMaturity.FORWARD_SHADOW_EARLY,
        authority_ceiling=AuthorityCeiling.RESEARCH_ONLY,
    )
    run = build_portfolio_intelligence_run(opportunities=(blocked, cash), idempotency_key="run")

    assert blocked.research_utility > 0
    assert blocked.authority_adjusted_utility == 0
    assert run.desired_allocations == ((blocked.opportunity_id, 1),)
    assert run.permitted_allocations == ((cash.opportunity_id, 1),)
    assert scenario_allocations(run=run, scenario=PortfolioScenario.ALL_RISK_OFF) == (
        (cash.opportunity_id, 1),
    )

    service = RunPortfolioIntelligence(InMemoryPortfolioIntelligenceRepository())
    first = service.execute(opportunities=(blocked, cash), idempotency_key="exact")
    assert service.execute(opportunities=(blocked, cash), idempotency_key="exact") == first
    assert (
        command_center(portfolio=first, shadow={"authority": "OBSERVATION_ONLY"})[
            "financial_authority"
        ]
        == "NONE"
    )
    try:
        service.execute(opportunities=(cash, blocked), idempotency_key="exact")
    except PortfolioIntelligenceError:
        pass
    else:
        raise AssertionError("conflicting replay must fail")


def test_dependency_evidence_and_future_handoffs_are_conservative_and_non_executable() -> None:
    first = PortfolioOpportunity.create(
        source=OpportunitySource.SECURITIES_META_STRATEGY,
        source_id="ensemble-a",
        instrument_id="SPY",
        expected_net_return="0.02",
        positive_probability="0.7",
        downside="-0.01",
        uncertainty="0.1",
        liquidity="1",
        maturity=OpportunityMaturity.HISTORICAL_RESEARCH,
        authority_ceiling=AuthorityCeiling.RESEARCH_ONLY,
    )
    second = PortfolioOpportunity.create(
        source=OpportunitySource.EVENT_MARKET,
        source_id="event-a",
        instrument_id="event-contract",
        expected_net_return="0.01",
        positive_probability="0.6",
        downside="-0.02",
        uncertainty="0.2",
        liquidity="1",
        maturity=OpportunityMaturity.HYPOTHESIS,
        authority_ceiling=AuthorityCeiling.NONE,
    )
    cash = PortfolioOpportunity.create(
        source=OpportunitySource.CASH,
        source_id="cash",
        instrument_id="USD",
        expected_net_return="0",
        positive_probability="1",
        downside="0",
        uncertainty="0",
        liquidity="1",
        maturity=OpportunityMaturity.FORWARD_SHADOW_EARLY,
        authority_ceiling=AuthorityCeiling.RESEARCH_ONLY,
    )

    run = build_portfolio_intelligence_run(
        opportunities=(first, second, cash), idempotency_key="dependency-evidence"
    )

    assert any(
        dependency.reason_code == "UNKNOWN_DEPENDENCY_PENALTY" and dependency.dependency_penalty > 0
        for dependency in run.dependencies
    )
    assert run.instructions
    assert all(instruction.state == "RESEARCH_ONLY" for instruction in run.instructions)
    assert all(not instruction.executable for instruction in run.instructions)
