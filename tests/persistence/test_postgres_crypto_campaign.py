from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.coinbase_public import FixtureCoinbaseCandleSource
from apex.adapters.crypto_campaign_postgres import PostgresCryptoCampaignRepository
from apex.application.crypto_campaign import (
    RecordDatasetExposure,
    RegisterCryptoCampaign,
)
from apex.application.crypto_research import (
    CaptureCoinbaseHistory,
    MaterializeCryptoHistoricalSnapshot,
)
from apex.domain.crypto_campaign import (
    EARLIER_EXPOSED_SNAPSHOT_ID,
    FIXED_CAMPAIGN_ID,
    ExposureCategory,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 29, 20, tzinfo=UTC)
DATA_START = datetime(2026, 7, 20, tzinfo=UTC)
CAMPAIGN_TABLES = {
    "crypto_dataset_exposure",
    "crypto_research_campaign",
    "crypto_campaign_dataset",
    "crypto_campaign_candidate",
    "crypto_campaign_experiment",
    "crypto_campaign_event",
    "crypto_holdout_event",
}


def _config(connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def test_campaign_and_exposure_are_durable_immutable_and_block_downgrade(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= CAMPAIGN_TABLES
    repository = PostgresCryptoCampaignRepository(postgres_engine)
    capture = CaptureCoinbaseHistory(repository, FixtureCoinbaseCandleSource()).start(
        capture_id="campaign-persistence-development-capture",
        start=DATA_START,
        end=DATA_START + timedelta(days=2),
        requested_at=NOW,
    )
    snapshot = MaterializeCryptoHistoricalSnapshot(repository).execute(
        snapshot_id=EARLIER_EXPOSED_SNAPSHOT_ID,
        capture_id=capture.capture_id,
        created_at=NOW + timedelta(seconds=1),
    )
    exposure = RecordDatasetExposure(repository).execute(
        exposure_id="campaign-persistence-development-exposure",
        snapshot_id=snapshot.snapshot_id,
        purpose="RETAINED_DEVELOPMENT_USE",
        category=ExposureCategory.DEVELOPMENT_EXPOSED,
        first_known_exposure_at=NOW,
        recorded_at=NOW + timedelta(seconds=2),
        actor="persistence-test",
        reason="Retain irreversible exposure classification.",
        idempotency_key="campaign-persistence-exposure-key",
    )
    campaign = RegisterCryptoCampaign(repository).execute(
        campaign_id=FIXED_CAMPAIGN_ID,
        actor="persistence-test",
        created_at=NOW + timedelta(seconds=3),
        idempotency_key="campaign-persistence-register-key",
    )
    fresh = PostgresCryptoCampaignRepository(postgres_engine)
    assert fresh.exposure(exposure.exposure_id) == exposure
    assert fresh.campaign(campaign.campaign_id) == campaign
    assert not campaign.paper_eligible
    assert not campaign.live_eligible
    assert campaign.capital_authority == "NONE"

    with (
        pytest.raises(sa.exc.DBAPIError, match="immutable"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE crypto_dataset_exposure SET authority = 'PAPER' "
                "WHERE exposure_id = :identity"
            ),
            {"identity": exposure.exposure_id},
        )

    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception, match="cannot downgrade 0052 with retained crypto campaign evidence"
        ):
            command.downgrade(_config(connection), "0051_crypto_challenger")
        connection.rollback()
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0052_crypto_campaign"
        )
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 0
