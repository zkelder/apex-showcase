from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.broad_equity_postgres import PostgresBroadEquityRepository
from apex.application.broad_equity import (
    BroadEquityApplicationError,
    CreateBroadEquityShadowPlan,
    RecordBroadEquityInclusion,
    RunBroadEquityDiscovery,
)
from apex.domain.broad_equity import (
    BroadDataPolicy,
    DataReadiness,
    assess_security_data_quality,
    calculate_broad_equity_features,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
)

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 29, 22, tzinfo=UTC)
BROAD_EQUITY_TABLES = {
    "broad_equity_security_quality",
    "broad_equity_discovery_run",
    "broad_equity_shadow_plan",
}


def _config(connection: object) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _bars(instrument_id: str, *, drift: int, count: int = 130) -> tuple[SecuritiesBarEvidence, ...]:
    bars = []
    for index in range(count):
        start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        close = Decimal("50") + Decimal(index * drift) / Decimal("10")
        bars.append(
            SecuritiesBarEvidence(
                instrument_id=instrument_id,
                interval=SecuritiesBarInterval.ONE_DAY,
                adjustment_state=SecuritiesAdjustmentState.SPLIT_ADJUSTED,
                period_start=start,
                period_end=start + timedelta(hours=6),
                available_at=start + timedelta(hours=7),
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume="1000000",
                source_identity="fixture-receipt",
            )
        )
    return tuple(bars)


def _evidence() -> tuple[tuple[object, ...], tuple[object, ...]]:
    expected = tuple(
        (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)).date().isoformat()
        for index in range(130)
    )
    policy = BroadDataPolicy.create(
        minimum_completed_sessions=120,
        preferred_sessions=130,
        minimum_median_dollar_volume="1000000",
        minimum_recent_trading_days=20,
    )
    qualities = []
    features = []
    for instrument_id, drift in (("acme", 2), ("beta", 1), ("gamma", -1)):
        bars = _bars(instrument_id, drift=drift)
        quality = assess_security_data_quality(
            canonical_security_id=instrument_id,
            bars=bars,
            expected_session_dates=expected,
            policy=policy,
        )
        qualities.append(quality)
        features.append(
            calculate_broad_equity_features(
                canonical_security_id=instrument_id, bars=bars, quality=quality
            )
        )
    return tuple(features), tuple(qualities)


def test_clean_database_is_at_current_head_with_broad_equity_tables(
    postgres_engine: Engine,
) -> None:
    assert set(sa.inspect(postgres_engine).get_table_names()) >= BROAD_EQUITY_TABLES
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0118_evidence_gap_recovery"
        )


def test_inclusion_discovery_and_shadow_plan_survive_a_fresh_repository_instance(
    postgres_engine: Engine,
) -> None:
    features, qualities = _evidence()
    repository = PostgresBroadEquityRepository(postgres_engine)

    retained_qualities = RecordBroadEquityInclusion(repository).execute(
        capture_id="capture-persistence-2026-07-29", qualities=qualities, recorded_at=NOW
    )
    assert retained_qualities == qualities

    run = RunBroadEquityDiscovery(repository).execute(
        universe_id="universe-1",
        data_capture_id="capture-persistence-2026-07-29",
        features=features,
        qualities=qualities,
        idempotency_key="discovery-persistence-1",
    )
    plan = CreateBroadEquityShadowPlan(repository).execute(
        discovery_run=run,
        policy_hashes=("feature-policy", "ranking-policy"),
        top_n=2,
        decision_at=NOW,
        forward_horizon_sessions=20,
        data_cutoff=NOW - timedelta(minutes=1),
        idempotency_key="shadow-persistence-1",
    )

    fresh = PostgresBroadEquityRepository(postgres_engine)
    assert fresh.qualities_for_capture("capture-persistence-2026-07-29") == qualities
    assert all(item.readiness is DataReadiness.READY for item in qualities)
    assert fresh.discovery_by_key("discovery-persistence-1") == run
    assert fresh.discovery(run.run_id) == run
    assert fresh.shadow_by_key("shadow-persistence-1") == plan
    assert fresh.shadow(plan.plan_id) == plan

    with pytest.raises(
        BroadEquityApplicationError, match="BROAD_EQUITY_QUALITY_IDEMPOTENCY_CONFLICT"
    ):
        RecordBroadEquityInclusion(fresh).execute(
            capture_id="capture-persistence-2026-07-29", qualities=(qualities[0],), recorded_at=NOW
        )
    with pytest.raises(BroadEquityApplicationError, match="BROAD_DISCOVERY_IDEMPOTENCY_CONFLICT"):
        RunBroadEquityDiscovery(fresh).execute(
            universe_id="changed",
            data_capture_id="capture-persistence-2026-07-29",
            features=features,
            qualities=qualities,
            idempotency_key="discovery-persistence-1",
        )


def test_retained_broad_equity_evidence_blocks_destructive_0075_downgrade(
    postgres_engine: Engine,
) -> None:
    _features, qualities = _evidence()
    repository = PostgresBroadEquityRepository(postgres_engine)
    RecordBroadEquityInclusion(repository).execute(
        capture_id="capture-downgrade-guard", qualities=qualities, recorded_at=NOW
    )
    with postgres_engine.connect() as connection:
        with pytest.raises(
            Exception,
            match="cannot downgrade 0075 with retained broad_equity_security_quality evidence",
        ):
            command.downgrade(_config(connection), "0074_paper_activation")
        connection.rollback()
    # Alembic commits each revision's downgrade separately: 0076's own
    # downgrade (unrelated to this broad_equity guard) already succeeded and
    # committed before 0075's guard fired and rolled back only its own step,
    # so the database genuinely rests at 0075 here -- not back at head yet.
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
            == "0075_broad_market_research"
        )
    with postgres_engine.connect() as connection:
        command.upgrade(_config(connection), "head")
