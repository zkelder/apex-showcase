from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection, Engine

from apex.adapters.kalshi_operating_acceptance import (
    ACCEPTANCE_ACCOUNT_ID,
    run_kalshi_paper_fixture_acceptance,
)
from apex.adapters.kalshi_operating_postgres import (
    PostgresKalshiOperatingRepository,
)
from apex.domain.kalshi_operating import (
    KalshiCapability,
    KalshiCurrentOpportunity,
    KalshiEvidenceClass,
    KalshiRefreshRecord,
    KalshiSide,
)

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)
HEAD_REVISION = "0104_securities_bridge_v1"


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def test_capability_refresh_and_opportunities_are_append_only_and_replayable(
    postgres_engine: Engine,
) -> None:
    repository = PostgresKalshiOperatingRepository(postgres_engine)
    capability = KalshiCapability.inspect(
        capability_id="postgres-kalshi-capability",
        inspected_at=NOW,
        public_discovery=True,
        public_orderbook=True,
        public_rules=True,
    )
    assert (
        repository.save_capability(capability=capability, idempotency_key="postgres-capability-key")
        is False
    )
    assert (
        repository.save_capability(capability=capability, idempotency_key="postgres-capability-key")
        is True
    )
    opportunity = KalshiCurrentOpportunity.create(
        opportunity_id="postgres-kalshi-opportunity",
        refresh_id="postgres-kalshi-refresh",
        evaluation_id="postgres-kalshi-evaluation",
        evidence_class=KalshiEvidenceClass.FIXTURE,
        market_ticker="KXPOSTGRES",
        event_ticker="KXPOSTGRES-EVENT",
        rule_hash="a" * 64,
        side=KalshiSide.YES,
        price="0.45",
        visible_liquidity=25,
        close_at=NOW + timedelta(hours=2),
        settlement_at=NOW + timedelta(hours=3),
        evidence_observed_at=NOW,
        as_of=NOW,
        supporting_evidence_ids=("market", "book", "fee"),
    )
    refresh = KalshiRefreshRecord.create(
        refresh_id="postgres-kalshi-refresh",
        as_of=NOW,
        evidence_class=KalshiEvidenceClass.FIXTURE,
        market_discovery_run_id="fixture-market-run",
        market_configuration_hash="b" * 64,
        complement_configuration_hash="c" * 64,
        page_count=1,
        market_count=1,
        markets_evaluated=1,
        opportunities=(opportunity,),
        evaluation_ids=(opportunity.evaluation_id,),
        provider_requests=0,
        request_bounds=(("MAX_MARKETS", 1),),
        checkpoints=("fixture-market-run", opportunity.evaluation_id),
        warnings=(),
        completed_at=NOW,
    )
    assert (
        repository.save_refresh(
            refresh=refresh,
            opportunities=(opportunity,),
            idempotency_key="postgres-refresh-key",
        )
        is False
    )
    assert (
        repository.save_refresh(
            refresh=refresh,
            opportunities=(opportunity,),
            idempotency_key="postgres-refresh-key",
        )
        is True
    )
    assert repository.capability(capability.capability_id) == capability
    assert repository.latest_capability() == capability
    assert repository.refresh(refresh.refresh_id) == refresh
    assert repository.latest_refresh() == refresh
    assert repository.opportunities_for_refresh(refresh.refresh_id) == (opportunity,)


def test_full_kalshi_paper_acceptance_round_trips_postgres(
    postgres_engine: Engine,
) -> None:
    repository = PostgresKalshiOperatingRepository(postgres_engine)
    report = run_kalshi_paper_fixture_acceptance(repository)
    retained = repository.state(ACCEPTANCE_ACCOUNT_ID)
    assert retained is not None
    assert retained.cash == Decimal("1005.42")
    assert retained.reserved_cash == Decimal("0.00")
    assert retained.canonical_hash == report["replay"]["state_hash"]
    assert len(retained.fills) == 1
    assert len(retained.settlements) == 1
    assert retained.reconciliations[-1].status.value == "CLEAN"


def test_retained_kalshi_evidence_blocks_0062_downgrade(
    postgres_engine: Engine,
) -> None:
    repository = PostgresKalshiOperatingRepository(postgres_engine)
    capability = KalshiCapability.inspect(
        capability_id="postgres-kalshi-downgrade-capability",
        inspected_at=NOW,
        public_discovery=True,
        public_orderbook=True,
        public_rules=True,
    )
    repository.save_capability(capability=capability, idempotency_key="postgres-downgrade-key")
    with postgres_engine.connect() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="cannot downgrade 0062"):
            command.downgrade(_config(connection), "0061_crypto_spot_paper")
        connection.rollback()
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0062_kalshi_operating_paper"
        )
