from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.crypto_campaign_generic_postgres import (
    PostgresGenericCryptoCampaignRepository,
)
from apex.application.crypto_campaign import RegisterCryptoCampaign
from apex.application.crypto_campaign_generic import retained_strategy_compatibility
from apex.domain.crypto_campaign import FIXED_CAMPAIGN_ID

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def test_generic_definition_and_plan_are_immutable_without_budget_consumption(
    postgres_engine: Engine,
) -> None:
    repository = PostgresGenericCryptoCampaignRepository(postgres_engine)
    RegisterCryptoCampaign(repository).execute(
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="generic-persistence-test",
        created_at=NOW,
        idempotency_key="generic-persistence-campaign-v1",
    )
    projection = retained_strategy_compatibility()[3]

    assert repository.save_generic_definition(projection.definition) == (projection.definition)
    assert repository.save_generic_definition(projection.definition) == (projection.definition)
    assert repository.save_generic_plan(projection.plan) == projection.plan
    assert repository.save_generic_plan(projection.plan) == projection.plan

    fresh = PostgresGenericCryptoCampaignRepository(postgres_engine)
    assert fresh.generic_definition(projection.definition.definition_id) == (projection.definition)
    assert fresh.generic_plan(projection.plan.plan_id) == projection.plan
    assert fresh.campaign_candidates(FIXED_CAMPAIGN_ID) == ()
    assert fresh.campaign_experiments(FIXED_CAMPAIGN_ID) == ()

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE crypto_campaign_candidate_definition "
                "SET authority = 'PAPER' WHERE definition_id = :identity"
            ),
            {"identity": projection.definition.definition_id},
        )

    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0054 with retained generic crypto candidate evidence",
        ):
            command.downgrade(_config(connection), "0053_crypto_campaign_candidate_1")
        connection.rollback()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0054_crypto_candidate_generic"
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM crypto_campaign_candidate")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM crypto_campaign_experiment")) == 0
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0
