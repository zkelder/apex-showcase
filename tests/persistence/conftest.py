from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    PostgresSettings,
    create_postgres_engine,
    postgres_uow_factory,
    recreate_test_database,
)
from apex.application.authorize_order import (
    CampaignInitializationCommand,
    DurableAuthorizationService,
    PersistAuthorizationInputsCommand,
)
from apex.domain.campaign import CampaignPolicy
from apex.domain.decisions import AllocationDecision, OpportunityProposal, Side
from apex.domain.market import (
    ContractRuleVersion,
    InformationSetBuilder,
    Instrument,
    MarketSnapshot,
    SourceRecord,
)
from apex.domain.values import (
    ContentHash,
    InformationCutoff,
    Money,
    Price,
    Quantity,
    RiskFraction,
    content_digest,
    deterministic_id,
)

ROOT = Path(__file__).resolve().parents[2]
BASE_TIME = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class AuthorizationArtifacts:
    inputs: PersistAuthorizationInputsCommand
    authorization_at: datetime


def _hash(label: str) -> ContentHash:
    return ContentHash(content_digest({"label": label}))


@pytest.fixture(scope="session")
def postgres_settings() -> PostgresSettings:
    return PostgresSettings.from_env().for_test()


@pytest.fixture(scope="session")
def postgres_engine(postgres_settings: PostgresSettings) -> Iterator[Engine]:
    base_settings = PostgresSettings.from_env()
    recreate_test_database(base_settings)
    previous_database = os.environ.get("APEX_POSTGRES_DB")
    os.environ["APEX_POSTGRES_DB"] = postgres_settings.database
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    finally:
        if previous_database is None:
            os.environ.pop("APEX_POSTGRES_DB", None)
        else:
            os.environ["APEX_POSTGRES_DB"] = previous_database
    engine = create_postgres_engine(postgres_settings)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(postgres_engine: Engine) -> Iterator[None]:
    # Migration tests can transition through old schemas that drop tables used
    # by preceding pooled sessions. Release those idle sessions first so the
    # single test process cannot deadlock its own reversible DDL.
    postgres_engine.dispose()
    with postgres_engine.connect() as connection:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        connection.commit()
    with postgres_engine.begin() as connection:
        # This is an explicitly recreated ephemeral test database. Discovering
        # the current-head tables prevents newer retained-evidence overlays from
        # leaking between tests while leaving real workspaces and alembic state
        # untouched.
        table_names = tuple(
            name for name in sa.inspect(connection).get_table_names() if name != "alembic_version"
        )
        quoted = ", ".join(
            connection.dialect.identifier_preparer.quote(name) for name in table_names
        )
        if quoted:
            # ``crypto_discovery_run``/``crypto_stateful_current_signal`` (as of
            # migration 0100) reject untagged TRUNCATE by default -- see that
            # migration's docstring. This reset is the one legitimate,
            # whole-database caller, so it explicitly opts in for the
            # duration of this transaction only (``SET LOCAL``).
            connection.exec_driver_sql("SET LOCAL apex.allow_producer_truncate = 'true'")
            connection.exec_driver_sql(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
    yield


@pytest.fixture
def service(postgres_engine: Engine) -> DurableAuthorizationService:
    return DurableAuthorizationService(postgres_uow_factory(postgres_engine))


@pytest.fixture
def uow_factory(postgres_engine: Engine):
    return postgres_uow_factory(postgres_engine)


@pytest.fixture
def policy() -> CampaignPolicy:
    payload = {
        "policy_version_id": "policy-persistence-v1",
        "profile": "PERSISTENCE_TEST",
        "requested_risk_fraction": "1",
        "max_trade_loss_fraction": "1",
        "max_open_loss_fraction": "1",
        "min_synthetic_edge": "0",
        "max_spread": "1",
        "max_capital_lockup_seconds": 86400,
    }
    return CampaignPolicy(
        policy_version_id=payload["policy_version_id"],
        profile=payload["profile"],
        requested_risk_fraction=payload["requested_risk_fraction"],
        max_trade_loss_fraction=payload["max_trade_loss_fraction"],
        max_open_loss_fraction=payload["max_open_loss_fraction"],
        min_synthetic_edge=payload["min_synthetic_edge"],
        max_spread=payload["max_spread"],
        max_capital_lockup_seconds=payload["max_capital_lockup_seconds"],
        content_hash=_hash("policy-domain-v1"),
    )


@pytest.fixture
def initialize_campaign(
    service: DurableAuthorizationService,
    policy: CampaignPolicy,
) -> Callable[..., CampaignInitializationCommand]:
    def initialize(
        campaign_id: str = "campaign-persistence",
        contribution: str = "1000",
        authorized_loss: str = "1000",
    ) -> CampaignInitializationCommand:
        value = CampaignInitializationCommand(
            campaign_id=campaign_id,
            contribution=Money(contribution),
            authorized_loss=Money(authorized_loss),
            policy=policy,
            occurred_at=BASE_TIME,
        )
        service.initialize_campaign(value)
        return value

    return initialize


@pytest.fixture
def make_artifacts() -> Callable[..., AuthorizationArtifacts]:
    def make(
        suffix: str,
        *,
        campaign_id: str = "campaign-persistence",
        maximum_loss: str = "100",
        authorization_at: datetime | None = None,
        market_age_seconds: int = 1,
    ) -> AuthorizationArtifacts:
        auth_at = authorization_at or BASE_TIME + timedelta(seconds=10)
        instrument = Instrument(
            instrument_id=f"instrument-{suffix}",
            category="POLITICS",
            payout_unit=Decimal("1"),
            price_tick=Decimal("0.01"),
            quantity_increment=Decimal("0.00000001"),
            yes_settlement_value=Decimal("1"),
            no_settlement_value=Decimal("0"),
            void_settlement_value=Decimal("0.5"),
        )
        rule = ContractRuleVersion(
            rule_version_id=f"rule-{suffix}",
            exact_text=f"Synthetic deterministic rule {suffix}",
            resolution_source="synthetic-test-source",
            effective_at=BASE_TIME - timedelta(days=1),
            close_at=BASE_TIME + timedelta(hours=1),
            determination_at=BASE_TIME + timedelta(hours=2),
            expected_settlement_at=BASE_TIME + timedelta(hours=3),
            content_hash=_hash(f"rule-{suffix}"),
        )
        market_source_id = f"market-{suffix}"
        record = SourceRecord(
            source_id=market_source_id,
            source_event_at=auth_at - timedelta(seconds=market_age_seconds + 1),
            available_at=auth_at - timedelta(seconds=market_age_seconds),
            sequence=1,
            schema_version="market-v1",
            content_hash=_hash(f"market-{suffix}"),
        )
        information_set = InformationSetBuilder.build(
            records=(record,),
            instrument=instrument,
            cutoff=InformationCutoff(auth_at - timedelta(seconds=market_age_seconds)),
            rule=rule,
            market=MarketSnapshot(
                yes_ask=Price("0.40", "0.01"),
                no_ask=Price("0.60", "0.01"),
                visible_yes_quantity=Quantity("1000", "0.00000001"),
            ),
        )
        proposal = OpportunityProposal(
            proposal_id=f"proposal-{suffix}",
            instrument_id=instrument.instrument_id,
            rule_version_id=rule.rule_version_id,
            information_set_id=information_set.information_set_id,
            strategy_version="strategy-persistence-v1",
            side=Side.YES,
            executable_ask=Price("0.40", "0.01"),
            fee_model_version="fee-v1",
            fee_per_unit=Money("0.01"),
            expected_net_value_per_unit=Decimal("0.10"),
            maximum_loss_per_unit=Money("1"),
            requested_risk_fraction=RiskFraction("1"),
            expires_at=BASE_TIME + timedelta(minutes=30),
            fingerprint=content_digest({"proposal": suffix}),
        )
        quantity = Quantity(maximum_loss, "0.00000001")
        maximum_loss_money = Money(maximum_loss)
        snapshot_hash = content_digest({"snapshot": suffix})
        allocation = AllocationDecision(
            allocation_id=deterministic_id(
                "allocation",
                {
                    "proposal_id": proposal.proposal_id,
                    "quantity": quantity,
                    "maximum_loss": maximum_loss_money,
                    "snapshot_hash": snapshot_hash,
                },
            ),
            accepted=True,
            proposal_id=proposal.proposal_id,
            quantity=quantity,
            maximum_loss=maximum_loss_money,
            snapshot_hash=snapshot_hash,
            reason_codes=("ALLOCATED",),
        )
        return AuthorizationArtifacts(
            inputs=PersistAuthorizationInputsCommand(
                campaign_id=campaign_id,
                proposal=proposal,
                allocation=allocation,
                information_set=information_set,
                market_observation_source_id=market_source_id,
                recorded_at=BASE_TIME + timedelta(seconds=1),
            ),
            authorization_at=auth_at,
        )

    return make
