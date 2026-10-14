from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.autopilot_fixture import fixture_autopilot_registry
from apex.adapters.postgres import postgres_autopilot_store
from apex.application.authorize_order import (
    CampaignInitializationCommand,
    DurableAuthorizationService,
)
from apex.application.autopilot import (
    AutopilotCycleCommand,
    CampaignControlError,
    RunAutopilotShadowCycle,
)
from apex.domain.autopilot import CampaignPortfolioPolicy, EvidenceTier
from apex.domain.values import ContentHash, Money, content_digest

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 21, 18, 0, tzinfo=UTC)


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _policy(campaign_id: str) -> CampaignPortfolioPolicy:
    payload = {"campaign_id": campaign_id, "portfolio": "test-v1"}
    return CampaignPortfolioPolicy(
        policy_version_id=f"{campaign_id}-portfolio-v1",
        campaign_id=campaign_id,
        maximum_active_risk_fraction="0.5",
        maximum_per_opportunity_risk_fraction="0.2",
        maximum_per_strategy_risk_fraction="0.5",
        maximum_per_correlation_bucket_risk_fraction="0.5",
        maximum_simultaneous_reservations=2,
        maximum_simultaneous_open_positions=2,
        maximum_capital_lockup_seconds=3600,
        minimum_conservative_net_expected_value="0",
        minimum_evidence_tier=EvidenceTier.RESEARCH,
        allow_profit_compounding=False,
        configuration_hash=ContentHash(content_digest(payload)),
    )


def _initialize_autopilot_campaign(
    service: DurableAuthorizationService,
    policy,
    campaign_id: str,
) -> None:
    service.initialize_campaign(
        CampaignInitializationCommand(
            campaign_id=campaign_id,
            contribution=Money("1000"),
            authorized_loss=Money("500"),
            policy=policy,
            occurred_at=NOW,
        )
    )


def _command(campaign_id: str, key: str = "cycle-v1") -> AutopilotCycleCommand:
    return AutopilotCycleCommand(
        campaign_id=campaign_id,
        idempotency_key=key,
        maximum_strategies=2,
        maximum_proposals=3,
        deadline_at=NOW + timedelta(seconds=30),
        operator_note="focused persistence test",
        requested_at=NOW,
    )


def _financial_counts(engine: Engine, campaign_id: str) -> tuple[int, int, int, int, int]:
    with engine.connect() as connection:
        return tuple(
            int(connection.scalar(sa.text(statement), {"campaign_id": campaign_id}) or 0)
            for statement in (
                "SELECT count(*) FROM ledger_journal WHERE campaign_id = :campaign_id",
                "SELECT count(*) FROM risk_decision WHERE campaign_id = :campaign_id",
                "SELECT count(*) FROM risk_reservation WHERE campaign_id = :campaign_id",
                'SELECT count(*) FROM "order" WHERE campaign_id = :campaign_id',
                "SELECT count(*) FROM position WHERE campaign_id = :campaign_id",
            )
        )


def test_shadow_cycle_persists_assessments_without_financial_or_execution_mutation(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    policy,
) -> None:
    campaign_id = "autopilot-shadow-persistence"
    _initialize_autopilot_campaign(service, policy, campaign_id)
    store = postgres_autopilot_store(postgres_engine)
    store.ensure_portfolio_policy(_policy(campaign_id), recorded_at=NOW)
    before = _financial_counts(postgres_engine, campaign_id)

    result = RunAutopilotShadowCycle(store, fixture_autopilot_registry()).run(_command(campaign_id))
    replay = RunAutopilotShadowCycle(store, fixture_autopilot_registry()).run(_command(campaign_id))

    assert result.state.value == "PARTIAL"
    assert result.invocation_count == 2
    assert result.approved_count == 1
    assert result.rejected_count == 2
    assert replay.idempotent_replay is True
    assert replay.approved_count == 1
    assert _financial_counts(postgres_engine, campaign_id) == before
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM autopilot_cycle")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM autopilot_strategy_invocation")) == 2
        assert connection.scalar(sa.text("SELECT count(*) FROM opportunity_economics")) == 3
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM autopilot_opportunity_assessment")) == 3
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM shadow_portfolio_decision")) == 3


def test_durable_pause_resume_kill_and_control_idempotency(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    policy,
) -> None:
    campaign_id = "autopilot-controls"
    _initialize_autopilot_campaign(service, policy, campaign_id)
    store = postgres_autopilot_store(postgres_engine)
    store.ensure_portfolio_policy(_policy(campaign_id), recorded_at=NOW)

    paused = store.control(
        campaign_id=campaign_id,
        action="PAUSE",
        reason="operator pause",
        idempotency_key="pause-v1",
        occurred_at=NOW,
    )
    replay = store.control(
        campaign_id=campaign_id,
        action="PAUSE",
        reason="operator pause",
        idempotency_key="pause-v1",
        occurred_at=NOW,
    )
    assert paused.value == replay.value == "PAUSED"
    with pytest.raises(CampaignControlError, match="PAUSED"):
        RunAutopilotShadowCycle(store, fixture_autopilot_registry()).run(_command(campaign_id))
    assert (
        store.control(
            campaign_id=campaign_id,
            action="RESUME",
            reason="operator resume",
            idempotency_key="resume-v1",
            occurred_at=NOW + timedelta(seconds=1),
        ).value
        == "RUNNING"
    )
    assert (
        store.control(
            campaign_id=campaign_id,
            action="KILL",
            reason="operator kill",
            idempotency_key="kill-v1",
            occurred_at=NOW + timedelta(seconds=2),
        ).value
        == "KILLED"
    )
    with pytest.raises(CampaignControlError, match="KILLED"):
        RunAutopilotShadowCycle(store, fixture_autopilot_registry()).run(
            _command(campaign_id, "after-kill")
        )
    with pytest.raises(CampaignControlError, match="cannot be resumed"):
        store.control(
            campaign_id=campaign_id,
            action="RESUME",
            reason="unsafe resume",
            idempotency_key="resume-after-kill-v1",
            occurred_at=NOW + timedelta(seconds=3),
        )


def test_concurrent_shadow_cycles_retain_consistent_capacity_snapshots(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    policy,
) -> None:
    campaign_id = "autopilot-concurrent-shadow"
    _initialize_autopilot_campaign(service, policy, campaign_id)
    store = postgres_autopilot_store(postgres_engine)
    store.ensure_portfolio_policy(_policy(campaign_id), recorded_at=NOW)
    registry = fixture_autopilot_registry()
    store.register_descriptors(registry.descriptors)
    before = _financial_counts(postgres_engine, campaign_id)

    def run(key: str):
        return RunAutopilotShadowCycle(store, registry).run(_command(campaign_id, key))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(run, ("concurrent-a", "concurrent-b")))

    assert {result.approved_count for result in results} == {1}
    assert _financial_counts(postgres_engine, campaign_id) == before
    with postgres_engine.connect() as connection:
        hashes = tuple(
            connection.scalars(
                sa.text(
                    "SELECT portfolio_snapshot_hash FROM autopilot_cycle "
                    "WHERE campaign_id = :campaign_id ORDER BY cycle_id"
                ),
                {"campaign_id": campaign_id},
            )
        )
    assert len(hashes) == 2
    assert len(set(hashes)) == 1


def test_0014_empty_downgrade_reupgrades_and_retained_evidence_blocks_atomically(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    policy,
) -> None:
    with postgres_engine.connect() as connection:
        command.downgrade(_config(connection), "0013_threshold_ladder")
        connection.commit()
    assert _revision(postgres_engine) == "0013_threshold_ladder"
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "0014_autopilot_core")
        connection.commit()
    assert _revision(postgres_engine) == "0014_autopilot_core"

    campaign_id = "autopilot-downgrade-guard"
    _initialize_autopilot_campaign(service, policy, campaign_id)
    store = postgres_autopilot_store(postgres_engine)
    store.ensure_portfolio_policy(_policy(campaign_id), recorded_at=NOW)
    status_before = _financial_counts(postgres_engine, campaign_id)
    schema_before = tuple(sorted(sa.inspect(postgres_engine).get_table_names()))
    with (
        pytest.raises(Exception, match="retained Autopilot shadow evidence"),
        postgres_engine.connect() as connection,
    ):
        command.downgrade(_config(connection), "0013_threshold_ladder")
        connection.commit()
    assert _revision(postgres_engine) == "0014_autopilot_core"
    assert tuple(sorted(sa.inspect(postgres_engine).get_table_names())) == schema_before
    assert _financial_counts(postgres_engine, campaign_id) == status_before
