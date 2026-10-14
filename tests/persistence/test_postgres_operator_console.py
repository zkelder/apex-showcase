from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apex.adapters.postgres import postgres_operator_console_store
from apex.application.operator_console import (
    CreateMoonshotCampaign,
    CreateMoonshotCampaignCommand,
    OperatorMode,
)
from apex.domain.autopilot import EvidenceTier
from apex.domain.values import Money, RiskFraction

pytestmark = pytest.mark.postgres

NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)


def _command(*, key: str) -> CreateMoonshotCampaignCommand:
    return CreateMoonshotCampaignCommand(
        campaign_id="operator-console-persistence",
        name="Bounded local operator Campaign",
        initial_capital=Money("1000"),
        maximum_authorized_loss=Money("500"),
        starts_at=NOW,
        expires_at=NOW + timedelta(days=1),
        operating_mode=OperatorMode.PAPER,
        maximum_active_risk_fraction=RiskFraction("0.5"),
        maximum_per_opportunity_risk_fraction=RiskFraction("0.1"),
        maximum_per_strategy_risk_fraction=RiskFraction("0.25"),
        maximum_per_correlation_bucket_risk_fraction=RiskFraction("0.25"),
        maximum_simultaneous_reservations=4,
        maximum_simultaneous_positions=4,
        maximum_capital_lockup_seconds=3600,
        minimum_conservative_expected_value=Money("0"),
        minimum_evidence_tier=EvidenceTier.RESEARCH,
        allow_profit_compounding=False,
        operator_note="focused durable console test",
        idempotency_key=key,
    )


def test_operator_campaign_creation_is_idempotent_and_projects_existing_financial_state(
    postgres_engine,
) -> None:
    store = postgres_operator_console_store(postgres_engine)

    created = CreateMoonshotCampaign(store).create(_command(key="operator-create-v1"))
    replay = CreateMoonshotCampaign(store).create(_command(key="operator-create-v1"))

    assert created == {
        "campaign_id": "operator-console-persistence",
        "idempotent_replay": False,
    }
    assert replay == {
        "campaign_id": "operator-console-persistence",
        "idempotent_replay": True,
    }
    detail = store.campaign_detail("operator-console-persistence")
    assert detail is not None
    assert detail["operating_mode"] == "PAPER"
    assert detail["state"] == "RUNNING"
    assert detail["portfolio"]["contributions"] == Money("1000")
    assert detail["portfolio"]["remaining_campaign_loss_capacity"] == Money("500")
    assert len(store.ledger("operator-console-persistence", limit=20, offset=0)) == 2
    assert store.reservations("operator-console-persistence", limit=20, offset=0) == ()
    assert store.orders("operator-console-persistence", limit=20, offset=0) == ()
    assert store.positions("operator-console-persistence", limit=20, offset=0) == ()
    assert store.available_mark_evidence("operator-console-persistence") == ()
    assert (
        store.activity(campaign_id="operator-console-persistence", limit=20, offset=0)[0]["action"]
        == "CREATE_CAMPAIGN"
    )

    overview = store.overview()
    assert overview["funded_capital"] == Money("1000")
    assert overview["authorized_loss"] == Money("500")
    assert overview["open_positions"] == 0
