from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.adapters.autopilot_fixture import (
    FixtureAutopilotStrategy,
    fixture_autopilot_registry,
    fixture_paper_portfolio_registry,
)
from apex.application.autopilot import (
    AutopilotCycleAuthority,
    AutopilotCycleCommand,
    CampaignControlError,
    InMemoryAutopilotStore,
    RunAutopilotShadowCycle,
    StrategyRegistry,
    governed_strategy_descriptors,
)
from apex.application.evidence_review import PaperTrialLimits, PaperTrialUsage
from apex.application.paper_portfolio import PaperCycleCommand, RunAutopilotPaperCycle
from apex.domain.autopilot import (
    CampaignOperatingState,
    CampaignPortfolioPolicy,
    CampaignPortfolioSnapshot,
    EvidenceTier,
    OpportunityEconomics,
    OpportunityOutcome,
    OpportunityValidity,
    ShadowDecisionState,
)
from apex.domain.campaign import Campaign, CampaignPolicy
from apex.domain.paper import PaperExecutionModel, PaperExecutionPolicy, PaperUnfilledBehavior
from apex.domain.values import (
    ContentHash,
    Money,
    Quantity,
    RiskFraction,
    ValueValidationError,
    content_digest,
)

NOW = datetime(2026, 7, 21, 18, 0, tzinfo=UTC)


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _economics(**overrides: object) -> OpportunityEconomics:
    values: dict[str, object] = {
        "proposal_id": "autopilot-proposal",
        "strategy_id": "autopilot-strategy",
        "strategy_version": "v1",
        "instrument_id": "instrument",
        "decision_at": NOW,
        "capital_release_at": NOW + timedelta(hours=1),
        "outcomes": (
            OpportunityOutcome(
                outcome_id="YES",
                point_probability="0.75",
                conservative_probability="0.60",
                payout="200",
            ),
            OpportunityOutcome(
                outcome_id="NO",
                point_probability="0.25",
                conservative_probability="0.40",
                payout="0",
            ),
        ),
        "acquisition_cost": "100",
        "expected_fee": "5",
        "slippage_reserve": "2",
        "execution_uncertainty_reserve": "3",
        "visible_quantity": Quantity("10", "1"),
        "requested_risk_fraction": "0.5",
        "evidence_tier": EvidenceTier.STRONG,
        "evidence_references": ("evidence-a",),
        "correlation_buckets": ("macro",),
        "configuration_version": "v1",
        "configuration_hash": _hash("economics"),
    }
    values.update(overrides)
    return OpportunityEconomics(**values)  # type: ignore[arg-type]


def _campaign() -> Campaign:
    policy_payload = {"policy": "autopilot"}
    policy = CampaignPolicy(
        policy_version_id="campaign-policy-v1",
        profile="MOONSHOT_SHADOW",
        requested_risk_fraction="1",
        max_trade_loss_fraction="1",
        max_open_loss_fraction="1",
        min_synthetic_edge="0",
        max_spread="1",
        max_capital_lockup_seconds=86_400,
        content_hash=_hash(policy_payload),
    )
    return Campaign(
        campaign_id="autopilot-campaign",
        contribution=Money("1000"),
        authorized_loss=Money("500"),
        policy=policy,
    )


def _portfolio_policy(*, bucket_cap: str = "0.5") -> CampaignPortfolioPolicy:
    payload = {"policy": "autopilot-portfolio"}
    return CampaignPortfolioPolicy(
        policy_version_id="autopilot-portfolio-v1",
        campaign_id="autopilot-campaign",
        maximum_active_risk_fraction="0.5",
        maximum_per_opportunity_risk_fraction="0.2",
        maximum_per_strategy_risk_fraction="0.5",
        maximum_per_correlation_bucket_risk_fraction=bucket_cap,
        maximum_simultaneous_reservations=2,
        maximum_simultaneous_open_positions=2,
        maximum_capital_lockup_seconds=3600,
        minimum_conservative_net_expected_value="0",
        minimum_evidence_tier=EvidenceTier.RESEARCH,
        allow_profit_compounding=False,
        configuration_hash=_hash(payload),
    )


def _snapshot() -> CampaignPortfolioSnapshot:
    payload = {
        "campaign_id": "autopilot-campaign",
        "funded_capital": Decimal("1000"),
        "settled_cash": Decimal("1000"),
        "ledger_equity": Decimal("1000"),
        "realized_trading_pnl": Decimal("0"),
        "fees": Decimal("0"),
        "remaining_loss_capacity": Decimal("500"),
        "current_reservation_risk": Decimal("0"),
        "current_open_position_risk": Decimal("0"),
        "active_risk_by_strategy": (),
        "active_risk_by_correlation_bucket": (),
        "reservation_count": 0,
        "open_position_count": 0,
        "earliest_capital_release_at": None,
        "latest_capital_release_at": None,
    }
    return CampaignPortfolioSnapshot(**payload, snapshot_hash=content_digest(payload))


def _command(key: str = "cycle-v1") -> AutopilotCycleCommand:
    return AutopilotCycleCommand(
        campaign_id="autopilot-campaign",
        idempotency_key=key,
        maximum_strategies=2,
        maximum_proposals=3,
        deadline_at=NOW + timedelta(seconds=30),
        operator_note="focused test",
        requested_at=NOW,
    )


def _paper_execution_policy() -> PaperExecutionPolicy:
    payload = {"policy": "paper-execution"}
    return PaperExecutionPolicy(
        policy_version_id="paper-execution-policy-v1",
        execution_model=PaperExecutionModel.IMMEDIATE_CROSS_V1,
        maximum_evidence_age_seconds=30,
        maximum_spread=Decimal("0.02"),
        maximum_orderbook_depth=5,
        maximum_visible_size_participation=RiskFraction("1"),
        slippage_reserve_per_contract=Money("0"),
        fee_model_version="fixture-autopilot-fee-v1",
        time_in_force_seconds=30,
        unfilled_behavior=PaperUnfilledBehavior.EXPIRE,
        configuration_version="v1",
        configuration_hash=_hash(payload),
    )


def test_opportunity_economics_retains_exact_bounded_math() -> None:
    economics = _economics()

    assert economics.point_expected_payout == Decimal("150")
    assert economics.conservative_expected_payout == Decimal("120")
    assert economics.total_modeled_acquisition_cost == Decimal("110")
    assert economics.point_net_expected_value == Decimal("40")
    assert economics.conservative_net_expected_value == Decimal("10")
    assert economics.minimum_payout == Decimal(0)
    assert economics.maximum_payout == Decimal("200")
    assert economics.maximum_loss == Decimal("110")
    assert economics.maximum_upside == Decimal("90")
    assert economics.capital_efficiency == Decimal("0.09090909090909090909090909091")


@pytest.mark.parametrize(
    "overrides",
    (
        {"acquisition_cost": 0.1},
        {
            "outcomes": (
                OpportunityOutcome(
                    outcome_id="YES",
                    point_probability="1",
                    conservative_probability="1",
                    payout="1",
                ),
            )
        },
        {
            "outcomes": (
                OpportunityOutcome(
                    outcome_id="YES",
                    point_probability="0.5",
                    conservative_probability="0.5",
                    payout="1",
                ),
                OpportunityOutcome(
                    outcome_id="YES",
                    point_probability="0.5",
                    conservative_probability="0.5",
                    payout="0",
                ),
            )
        },
        {"correlation_buckets": ("z", "a")},
        {"evidence_references": ()},
    ),
)
def test_opportunity_economics_fails_closed_for_malformed_inputs(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueValidationError):
        _economics(**overrides)


def test_governed_registry_is_static_with_only_controlled_shadow_strategies() -> None:
    descriptors = governed_strategy_descriptors()
    assert {value.strategy_id for value in descriptors} == {
        "gfs-weather-research",
        "kalshi-same-market-complement",
        "kalshi-threshold-ladder",
        "kalshi-orderbook-microstructure-v1",
        "kalshi-market-dynamics-v1",
        "operator-thesis-v1",
        "market-consensus-baseline-v1",
    }
    assert {value.strategy_id for value in descriptors if value.runnable} == {
        "operator-thesis-v1",
        "market-consensus-baseline-v1",
        "kalshi-orderbook-microstructure-v1",
        "kalshi-market-dynamics-v1",
    }
    assert all(
        not value.runnable
        for value in descriptors
        if value.strategy_id
        in {
            "gfs-weather-research",
            "kalshi-same-market-complement",
            "kalshi-threshold-ladder",
        }
    )
    assert StrategyRegistry(()).shadow_strategies(10) == ()
    with pytest.raises(ValueValidationError, match="duplicate strategy ID"):
        StrategyRegistry((FixtureAutopilotStrategy(), FixtureAutopilotStrategy()))


def test_fixture_shadow_cycle_is_deterministic_idempotent_and_nonfinancial() -> None:
    store = InMemoryAutopilotStore(
        campaign=_campaign(), portfolio_policy=_portfolio_policy(), snapshot=_snapshot()
    )
    service = RunAutopilotShadowCycle(store, fixture_autopilot_registry())

    first = service.run(_command())
    replay = service.run(_command())

    assert first.state.value == "PARTIAL"
    assert first.invocation_count == 2
    assert first.approved_count == 1
    assert first.rejected_count == 2
    assert replay.idempotent_replay is True
    assert replay.approved_count == 1
    assert first.assessments[0].decision is ShadowDecisionState.APPROVED
    assert any(
        item.validity is OpportunityValidity.REJECTED_NONPOSITIVE_CONSERVATIVE_VALUE
        for item in first.assessments
    )
    assert any(
        item.validity is OpportunityValidity.REJECTED_PORTFOLIO for item in first.assessments
    )
    assert store._snapshot.active_risk == 0


def test_paper_replay_reissues_only_the_same_approved_execution_requests() -> None:
    class RecordingExecutor:
        def __init__(self) -> None:
            self.approved_assessment_ids: list[str] = []

        def ensure_execution_policy(
            self, policy: PaperExecutionPolicy, *, recorded_at: datetime
        ) -> None:
            _ = policy, recorded_at

        def execute_approved(self, *, assessment, **_: object) -> None:
            self.approved_assessment_ids.append(assessment.assessment_id)

    class RecordingTrialAuthority:
        def __init__(self) -> None:
            self.lineage: list[tuple[str, str]] = []

        def active_trial_for(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                authorization_id="fixture-paper-recovery-trial",
                limits=PaperTrialLimits(
                    paper_execution_policy_id="fixture-paper-policy-v1",
                    paper_execution_policy_hash=_paper_execution_policy().configuration_hash,
                    starts_at=NOW - timedelta(seconds=1),
                    expires_at=NOW + timedelta(hours=1),
                    maximum_paper_cycles=10,
                    maximum_approved_opportunities=10,
                    maximum_paper_orders=10,
                    maximum_simultaneous_open_positions=10,
                    maximum_per_opportunity_risk_fraction=RiskFraction("1"),
                    maximum_strategy_active_risk_fraction=RiskFraction("1"),
                    maximum_cumulative_authorized_risk=Money("1000"),
                    maximum_cumulative_realized_paper_loss=Money("1000"),
                    maximum_paper_drawdown=Money("1000"),
                    maximum_capital_lockup_seconds=3600,
                    allowed_instrument_class="BOUNDED_PREDICTION_MARKET",
                    allowed_provider="FIXTURE",
                ),
                usage=PaperTrialUsage(),
            )

        def record_lineage(self, *, entity_type: str, entity_id: str, **_: object) -> None:
            self.lineage.append((entity_type, entity_id))

    store = InMemoryAutopilotStore(
        campaign=_campaign(), portfolio_policy=_portfolio_policy(), snapshot=_snapshot()
    )
    executor = RecordingExecutor()
    trial_authority = RecordingTrialAuthority()
    command = PaperCycleCommand(
        cycle=AutopilotCycleCommand(
            campaign_id="autopilot-campaign",
            idempotency_key="paper-recovery-v1",
            maximum_strategies=2,
            maximum_proposals=4,
            deadline_at=NOW + timedelta(seconds=30),
            operator_note="focused replay recovery",
            requested_at=NOW,
            authority=AutopilotCycleAuthority.PAPER,
        ),
        execution_policy=_paper_execution_policy(),
    )
    service = RunAutopilotPaperCycle(
        store, fixture_paper_portfolio_registry(), executor, trial_authority
    )

    first = service.run(command)
    replay = service.run(command)

    assert first.approved_count == 2
    assert replay.idempotent_replay is True
    assert len(executor.approved_assessment_ids) == 4
    assert executor.approved_assessment_ids[:2] == executor.approved_assessment_ids[2:]
    assert {entity_type for entity_type, _ in trial_authority.lineage} == {
        "AUTOPILOT_CYCLE",
        "OPPORTUNITY_ASSESSMENT",
    }


def test_pause_and_kill_block_future_shadow_cycles() -> None:
    store = InMemoryAutopilotStore(
        campaign=_campaign(), portfolio_policy=_portfolio_policy(), snapshot=_snapshot()
    )
    service = RunAutopilotShadowCycle(store, fixture_autopilot_registry())
    store.control(CampaignOperatingState.PAUSED)
    with pytest.raises(CampaignControlError, match="PAUSED"):
        service.run(_command("paused"))
    store.control(CampaignOperatingState.RUNNING)
    store.control(CampaignOperatingState.KILLED)
    with pytest.raises(CampaignControlError, match="KILLED"):
        service.run(_command("killed"))
    with pytest.raises(CampaignControlError, match="cannot be resumed"):
        store.control(CampaignOperatingState.RUNNING)


def test_correlation_cap_rejects_an_otherwise_valid_shadow_opportunity() -> None:
    store = InMemoryAutopilotStore(
        campaign=_campaign(),
        portfolio_policy=_portfolio_policy(bucket_cap="0.05"),
        snapshot=_snapshot(),
    )
    result = RunAutopilotShadowCycle(store, fixture_autopilot_registry()).run(_command())

    assert result.approved_count == 0
    assert any(
        "PORTFOLIO_CORRELATION_CAP_EXCEEDED" in assessment.reason_codes
        for assessment in result.assessments
    )
