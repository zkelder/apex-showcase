from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from apex.application.broad_equity import (
    BroadEquityApplicationError,
    CreateBroadEquityShadowPlan,
    InMemoryBroadEquityRepository,
    RecordBroadEquityInclusion,
    RunBroadEquityDiscovery,
)
from apex.domain.broad_equity import (
    OUT_OF_DOMAIN_WARNING,
    BroadDataPolicy,
    BroadMarketRegime,
    DataReadiness,
    assess_security_data_quality,
    calculate_broad_equity_features,
    plan_broad_data_capture,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
)

NOW = datetime(2026, 7, 29, 22, tzinfo=UTC)


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
                source_identity="alpaca-fixture-receipt",
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
                canonical_security_id=instrument_id,
                bars=bars,
                quality=quality,
            )
        )
    return tuple(features), tuple(qualities)


def test_broad_capture_quality_discovery_and_shadow_are_research_only() -> None:
    data_plan = plan_broad_data_capture(
        universe_id="universe-1",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 7, 29),
        symbol_count=501,
        expected_sessions_per_symbol=130,
        batch_size=50,
    )
    assert data_plan.expected_requests == 11
    assert data_plan.estimated_bar_count == 65130
    assert "DRY_PLAN_PERFORMS_NO_PROVIDER_REQUEST" in data_plan.limitations

    features, qualities = _evidence()
    assert all(item.readiness is DataReadiness.READY for item in qualities)
    assert all(OUT_OF_DOMAIN_WARNING in item.warnings for item in features)

    repository = InMemoryBroadEquityRepository()
    service = RunBroadEquityDiscovery(repository)
    run = service.execute(
        universe_id="universe-1",
        data_capture_id="capture-1",
        features=features,
        qualities=qualities,
        idempotency_key="discovery-1",
    )

    assert run.regime.regime is BroadMarketRegime.RISK_ON
    assert run.opportunities[0].canonical_security_id == "acme"
    assert run.portfolio.authority == "RESEARCH_ONLY"
    assert all(item.authority_permitted_weight == 0 for item in run.portfolio.allocations)
    assert run.portfolio.cash_weight == Decimal("0.4")
    assert (
        service.execute(
            universe_id="universe-1",
            data_capture_id="capture-1",
            features=features,
            qualities=qualities,
            idempotency_key="discovery-1",
        )
        == run
    )

    shadow = CreateBroadEquityShadowPlan(repository).execute(
        discovery_run=run,
        policy_hashes=("feature-policy", "ranking-policy"),
        top_n=2,
        decision_at=NOW,
        forward_horizon_sessions=20,
        data_cutoff=NOW - timedelta(minutes=1),
        idempotency_key="shadow-1",
    )
    assert shadow.authority == "OBSERVATION_ONLY"
    assert shadow.state == "DRAFT"
    assert len(shadow.top_candidate_ids) == 2
    assert not hasattr(shadow, "orders")


def test_inclusion_exclusion_decisions_are_durable_and_idempotent() -> None:
    _features, qualities = _evidence()
    repository = InMemoryBroadEquityRepository()
    service = RecordBroadEquityInclusion(repository)

    retained = service.execute(
        capture_id="capture-2026-07-29", qualities=qualities, recorded_at=NOW
    )
    assert retained == qualities
    assert repository.qualities_for_capture("capture-2026-07-29") == qualities
    assert all(item.readiness is DataReadiness.READY for item in retained)

    # Exact replay with unchanged evidence is a free no-op, not a duplicate write.
    replay = service.execute(capture_id="capture-2026-07-29", qualities=qualities, recorded_at=NOW)
    assert replay == qualities
    assert repository.qualities_for_capture("capture-2026-07-29") == qualities

    changed_bars = _bars("acme", drift=99, count=130)
    changed_expected = tuple(bar.period_start.date().isoformat() for bar in changed_bars)
    changed_quality = assess_security_data_quality(
        canonical_security_id="acme",
        bars=changed_bars,
        expected_session_dates=changed_expected,
        policy=BroadDataPolicy.create(
            minimum_completed_sessions=120,
            preferred_sessions=130,
            minimum_median_dollar_volume="1000000",
            minimum_recent_trading_days=20,
        ),
    )
    with pytest.raises(
        BroadEquityApplicationError, match="BROAD_EQUITY_QUALITY_IDEMPOTENCY_CONFLICT"
    ):
        service.execute(
            capture_id="capture-2026-07-29", qualities=(changed_quality,), recorded_at=NOW
        )

    with pytest.raises(BroadEquityApplicationError, match="BROAD_EQUITY_CAPTURE_ID_REQUIRED"):
        service.execute(capture_id="", qualities=qualities, recorded_at=NOW)


def test_broad_quality_blocks_duplicates_and_replay_conflicts() -> None:
    bars = _bars("acme", drift=1, count=25)
    expected = tuple(bar.period_start.date().isoformat() for bar in bars)
    quality = assess_security_data_quality(
        canonical_security_id="acme",
        bars=(*bars, bars[-1]),
        expected_session_dates=expected,
        policy=BroadDataPolicy.create(
            minimum_completed_sessions=20,
            preferred_sessions=25,
            minimum_median_dollar_volume="100",
            minimum_recent_trading_days=20,
        ),
    )
    assert quality.readiness is DataReadiness.BLOCKED_DATA
    assert "DUPLICATE_CANONICAL_BARS" in quality.blockers

    features, qualities = _evidence()
    repository = InMemoryBroadEquityRepository()
    service = RunBroadEquityDiscovery(repository)
    service.execute(
        universe_id="u",
        data_capture_id="c",
        features=features,
        qualities=qualities,
        idempotency_key="same",
    )
    with pytest.raises(BroadEquityApplicationError, match="BROAD_DISCOVERY_IDEMPOTENCY_CONFLICT"):
        service.execute(
            universe_id="changed",
            data_capture_id="c",
            features=features,
            qualities=qualities,
            idempotency_key="same",
        )
