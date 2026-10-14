from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.autopilot_fixture import fixture_paper_portfolio_registry
from apex.adapters.postgres import postgres_autopilot_store, postgres_paper_portfolio_store
from apex.application.autopilot import AutopilotCycleAuthority, AutopilotCycleCommand
from apex.application.evidence_review import PaperTrialLimits, PaperTrialUsage
from apex.application.paper_portfolio import PaperCycleCommand, RunAutopilotPaperCycle
from apex.domain.autopilot import CampaignPortfolioPolicy, EvidenceTier
from apex.domain.execution import ClassifiedRuleBoundSettlementFact
from apex.domain.market import SettlementOutcome
from apex.domain.paper import PaperExecutionModel, PaperExecutionPolicy, PaperUnfilledBehavior
from apex.domain.values import ContentHash, Money, RiskFraction, content_digest

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _paper_policy() -> PaperExecutionPolicy:
    payload = {"paper-policy": "fixture-v1"}
    return PaperExecutionPolicy(
        policy_version_id="fixture-paper-policy-v1",
        execution_model=PaperExecutionModel.IMMEDIATE_CROSS_V1,
        maximum_evidence_age_seconds=30,
        maximum_spread=Decimal("0.02"),
        maximum_orderbook_depth=5,
        maximum_visible_size_participation=RiskFraction("1"),
        slippage_reserve_per_contract=Money("0"),
        fee_model_version="fixture-autopilot-fee-v1",
        time_in_force_seconds=30,
        unfilled_behavior=PaperUnfilledBehavior.EXPIRE,
        configuration_version="fixture-paper-v1",
        configuration_hash=ContentHash(content_digest(payload)),
    )


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _portfolio(campaign_id: str) -> CampaignPortfolioPolicy:
    payload = {"campaign_id": campaign_id, "portfolio": "paper-v1"}
    return CampaignPortfolioPolicy(
        policy_version_id=f"{campaign_id}-paper-portfolio-v1",
        campaign_id=campaign_id,
        maximum_active_risk_fraction="1",
        maximum_per_opportunity_risk_fraction="0.2",
        maximum_per_strategy_risk_fraction="1",
        maximum_per_correlation_bucket_risk_fraction="1",
        maximum_simultaneous_reservations=4,
        maximum_simultaneous_open_positions=4,
        maximum_capital_lockup_seconds=3600,
        minimum_conservative_net_expected_value="0",
        minimum_evidence_tier=EvidenceTier.RESEARCH,
        allow_profit_compounding=False,
        configuration_hash=ContentHash(content_digest(payload)),
    )


def _command(campaign_id: str) -> PaperCycleCommand:
    return PaperCycleCommand(
        cycle=AutopilotCycleCommand(
            campaign_id=campaign_id,
            idempotency_key="paper-cycle-v1",
            maximum_strategies=2,
            maximum_proposals=4,
            deadline_at=NOW + timedelta(seconds=30),
            operator_note="focused paper persistence test",
            requested_at=NOW,
            authority=AutopilotCycleAuthority.PAPER,
        ),
        execution_policy=_paper_policy(),
    )


def _classified_fact(
    *,
    position: sa.RowMapping,
    classification: SettlementOutcome,
    recorded_at: datetime,
) -> ClassifiedRuleBoundSettlementFact:
    value = Decimal("1") if classification is SettlementOutcome.YES else Decimal("0")
    resolution_source = str(position["resolution_source"])
    rule_hash = ContentHash(str(position["rule_content_hash"]))
    raw_payload = (
        f"resolution:{classification.value};settlement_value={value};"
        f"instrument={position['instrument_id']};rule={position['rule_version_id']};"
        f"rule_hash={rule_hash.value};source={resolution_source}"
    )
    raw_hash = ContentHash(sha256(raw_payload.encode("utf-8")).hexdigest())
    evidence = {
        "fixture_id": f"paper-position-settlement-{classification.value.lower()}",
        "schema_version": "2",
        "source_id": f"paper-position-settlement-{classification.value.lower()}-source",
        "instrument_id": str(position["instrument_id"]),
        "rule_version_id": str(position["rule_version_id"]),
        "rule_content_hash": rule_hash.value,
        "resolution_source": resolution_source,
        "outcome_classification": classification,
        "source_event_at": recorded_at - timedelta(seconds=1),
        "available_at": recorded_at,
        "settlement_value": str(value),
        "raw_payload": raw_payload,
        "raw_payload_hash": raw_hash.value,
    }
    return ClassifiedRuleBoundSettlementFact(
        source_id=f"paper-position-settlement-{classification.value.lower()}-source",
        instrument_id=str(position["instrument_id"]),
        rule_version_id=str(position["rule_version_id"]),
        source_event_at=recorded_at - timedelta(seconds=1),
        available_at=recorded_at,
        settlement_value=value,
        content_hash=ContentHash(content_digest(evidence)),
        fixture_id=f"paper-position-settlement-{classification.value.lower()}",
        schema_version="2",
        settlement_value_text=str(value),
        resolution_source=resolution_source,
        rule_content_hash=rule_hash,
        outcome_classification=classification,
        raw_payload=raw_payload,
        raw_payload_hash=raw_hash,
    )


def test_paper_cycle_persists_exact_no_partial_and_full_fill_lifecycles(
    postgres_engine: Engine, service, policy, initialize_campaign
) -> None:
    campaign_id = "paper-portfolio-persistence"
    initialize_campaign(campaign_id=campaign_id)
    store = postgres_autopilot_store(postgres_engine)
    store.ensure_portfolio_policy(_portfolio(campaign_id), recorded_at=NOW)

    class TestTrialAuthority:
        def active_trial_for(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                authorization_id="persistence-test-paper-trial",
                limits=PaperTrialLimits(
                    paper_execution_policy_id=_paper_policy().policy_version_id,
                    paper_execution_policy_hash=_paper_policy().configuration_hash,
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

        def record_lineage(self, **_: object) -> None:
            return None

    class LegacyLifecycleExecutor:
        def __init__(self, delegate) -> None:
            self._delegate = delegate

        def ensure_execution_policy(self, *args: object, **kwargs: object) -> None:
            self._delegate.ensure_execution_policy(*args, **kwargs)

        def execute_approved(self, **kwargs: object) -> None:
            kwargs.pop("paper_trial_authorization_id")
            self._delegate.execute_approved(**kwargs)

    result = RunAutopilotPaperCycle(
        store,
        fixture_paper_portfolio_registry(),
        LegacyLifecycleExecutor(postgres_paper_portfolio_store(postgres_engine)),
        TestTrialAuthority(),
    ).run(_command(campaign_id))

    assert result.state.value == "PARTIAL"
    assert result.approved_count == 3
    assert result.rejected_count == 1
    with postgres_engine.connect() as connection:
        states = tuple(connection.scalars(sa.text('SELECT state FROM "order" ORDER BY state')))
        assert states == ("CANCELED", "EXPIRED", "FILLED")
        assert connection.scalar(sa.text("SELECT count(*) FROM paper_execution")) == 3
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 2
        assert connection.scalar(sa.text("SELECT count(*) FROM execution_event")) == 7
        evidence_id = str(
            connection.scalar(
                sa.text("SELECT evidence_id FROM paper_execution WHERE state = 'FULL_FILL' LIMIT 1")
            )
        )
    marks = postgres_paper_portfolio_store(postgres_engine).mark_open_positions(
        campaign_id=campaign_id,
        evidence_id=evidence_id,
        recorded_at=NOW + timedelta(seconds=2),
    )
    status = postgres_paper_portfolio_store(postgres_engine).portfolio_status(campaign_id)
    assert len(marks) == 1
    assert marks[0].conservative_unrealized_pnl == Decimal("-0.01")
    assert status["conservative_unrealized_paper_pnl"] == Decimal("-0.01")
    assert status["orders_by_state"] == {"CANCELED": 1, "EXPIRED": 1, "FILLED": 1}
    with postgres_engine.connect() as connection:
        position = (
            connection.execute(
                sa.text(
                    "SELECT position.*, proposal.resolution_source, proposal.rule_content_hash "
                    'FROM position JOIN "order" AS durable_order '
                    "ON durable_order.order_id = position.originating_order_id "
                    "JOIN opportunity_proposal AS proposal "
                    "ON proposal.proposal_id = durable_order.proposal_id "
                    "WHERE position.campaign_id = :campaign_id "
                    "AND position.settlement_state = 'OPEN' "
                    "AND durable_order.state = 'FILLED'"
                ),
                {"campaign_id": campaign_id},
            )
            .mappings()
            .one()
        )
    settled_at = NOW + timedelta(hours=1)
    settlement_id = postgres_paper_portfolio_store(postgres_engine).settle_position(
        campaign_id=campaign_id,
        position_id=str(position["position_id"]),
        fact=_classified_fact(
            position=position,
            classification=SettlementOutcome.YES,
            recorded_at=settled_at,
        ),
        void_term_content_hash=None,
        recorded_at=settled_at,
    )
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM settlement_fact")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM position_settlement")) == 1
        assert (
            connection.scalar(
                sa.text("SELECT settlement_state FROM position WHERE position_id = :position_id"),
                {"position_id": position["position_id"]},
            )
            == "SETTLED"
        )
        assert settlement_id.startswith("paper_position_settlement_")
        partial_position = (
            connection.execute(
                sa.text(
                    "SELECT position.*, proposal.resolution_source, proposal.rule_content_hash "
                    'FROM position JOIN "order" AS durable_order '
                    "ON durable_order.order_id = position.originating_order_id "
                    "JOIN opportunity_proposal AS proposal "
                    "ON proposal.proposal_id = durable_order.proposal_id "
                    "WHERE position.campaign_id = :campaign_id "
                    "AND position.settlement_state = 'OPEN' "
                    "AND durable_order.state = 'CANCELED'"
                ),
                {"campaign_id": campaign_id},
            )
            .mappings()
            .one()
        )
    no_settlement_id = postgres_paper_portfolio_store(postgres_engine).settle_position(
        campaign_id=campaign_id,
        position_id=str(partial_position["position_id"]),
        fact=_classified_fact(
            position=partial_position,
            classification=SettlementOutcome.NO,
            recorded_at=settled_at + timedelta(seconds=1),
        ),
        void_term_content_hash=None,
        recorded_at=settled_at + timedelta(seconds=1),
    )
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM settlement_fact")) == 2
        assert connection.scalar(sa.text("SELECT count(*) FROM position_settlement")) == 2
        assert no_settlement_id.startswith("paper_position_settlement_")


def test_0015_empty_downgrade_reupgrades_and_retained_paper_policy_blocks_atomically(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0014_autopilot_core")
        connection.commit()
    assert _revision(postgres_engine) == "0014_autopilot_core"
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
        connection.commit()
    assert _revision(postgres_engine) == "0118_evidence_gap_recovery"

    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0015_paper_portfolio")
        connection.commit()
    assert _revision(postgres_engine) == "0015_paper_portfolio"

    store = postgres_paper_portfolio_store(postgres_engine)
    store.ensure_execution_policy(_paper_policy(), recorded_at=NOW)
    schema_before = tuple(sorted(sa.inspect(postgres_engine).get_table_names()))
    with (
        pytest.raises(Exception, match="retained paper portfolio evidence"),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_config(connection), "0014_autopilot_core")
        connection.commit()
    assert _revision(postgres_engine) == "0015_paper_portfolio"
    assert tuple(sorted(sa.inspect(postgres_engine).get_table_names())) == schema_before
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM paper_execution_policy")) == 1
