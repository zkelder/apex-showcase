from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from apex.adapters.kalshi_capture_fixtures import VirtualCaptureClock
from apex.adapters.kalshi_public import KalshiPublicEventNormalizer, KalshiPublicMarketNormalizer
from apex.adapters.kalshi_sweep_fixtures import KalshiFixtureOrderbookSweepClient
from apex.adapters.postgres import (
    postgres_autopilot_store,
    postgres_multi_strategy_shadow_store,
    postgres_strategy_shadow_store,
)
from apex.application.authorize_order import CampaignInitializationCommand
from apex.application.autopilot import StrategyAuthorityState
from apex.application.capture_public_orderbook_session import (
    BoundedOrderbookCaptureRequest,
    CaptureBoundedPublicOrderbookSession,
    CaptureMarketCandidate,
    InMemoryPublicOrderbookCaptureStore,
)
from apex.application.discover_public_events import CapturedPublicEventPage
from apex.application.market_dynamics_shadow import market_dynamics_strategy_descriptor
from apex.application.multi_strategy_shadow import (
    BuildEnrichedShadowMarketCohort,
    MultiStrategyInvocation,
    MultiStrategyShadowCycleCommand,
    RunMultiStrategyShadowCampaignCycle,
)
from apex.application.orderbook_microstructure_shadow import (
    orderbook_microstructure_strategy_descriptor,
)
from apex.application.strategy_shadow import (
    OPERATOR_THESIS_STRATEGY_ID,
    StrategyAuthorityCommand,
    build_market_consensus_baseline_prediction,
    production_shadow_strategy_descriptors,
)
from apex.domain.autopilot import CampaignPortfolioPolicy, EvidenceTier
from apex.domain.multi_strategy import ShadowMarketCohortConfiguration
from apex.domain.public_event import evaluate_event_consistency, link_event_markets
from apex.domain.public_market import PublicMarketDataQualityState
from apex.domain.values import ContentHash, Money, content_digest

pytestmark = pytest.mark.postgres

ROOT = Path(__file__).resolve().parents[2]
HEAD_REVISION = "0118_evidence_gap_recovery"
PRIOR_REVISION = "0016_operator_console"
NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)


def _config(connection: sa.Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(sa.text("SELECT version_num FROM alembic_version")))


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as connection:
        command.downgrade(_config(connection), revision)


def _upgrade_head(engine: Engine) -> None:
    with engine.begin() as connection:
        command.upgrade(_config(connection), "head")


def test_0017_empty_schema_downgrades_and_reupgrades(postgres_engine: Engine) -> None:
    assert _revision(postgres_engine) == HEAD_REVISION

    _downgrade(postgres_engine, PRIOR_REVISION)

    assert _revision(postgres_engine) == PRIOR_REVISION
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT to_regclass('shadow_prediction')")) is None
    _upgrade_head(postgres_engine)
    assert _revision(postgres_engine) == HEAD_REVISION


def test_retained_authority_evidence_blocks_0017_downgrade_atomically(
    postgres_engine: Engine,
) -> None:
    store = postgres_strategy_shadow_store(postgres_engine)
    decision = store.change_authority(
        StrategyAuthorityCommand(
            strategy_id=OPERATOR_THESIS_STRATEGY_ID,
            requested_authority=StrategyAuthorityState.PAUSED,
            operator_reason="focused downgrade guard proof",
            evidence_review_id="review-downgrade-guard",
            idempotency_key="authority-downgrade-guard",
            confirmation_text=None,
            requested_at=NOW,
        )
    )
    assert decision.resulting_authority is StrategyAuthorityState.PAUSED

    with pytest.raises(Exception, match="cannot downgrade 0019"):
        _downgrade(postgres_engine, PRIOR_REVISION)

    assert _revision(postgres_engine) == HEAD_REVISION
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM strategy_authority_decision")) == 1
        assert (
            connection.scalar(sa.text("SELECT to_regclass('shadow_prediction')"))
            == "shadow_prediction"
        )


def test_shared_exact_enrichment_and_orderbook_do_not_create_complement_evidence(
    postgres_engine: Engine,
    service,
    policy,
) -> None:
    clock = VirtualCaptureClock()
    client = KalshiFixtureOrderbookSweepClient(clock)
    ticker = "SWEEP-HIGH-NON-CANDIDATE"
    event_ticker = "SWEEP-EVENT-A"
    candidate = CaptureMarketCandidate(
        market_snapshot_id="list-market-snapshot-shared-enrichment",
        ingestion_run_id="list-discovery-shared-enrichment",
        provider_market_ticker=ticker,
        provider_event_ticker=event_ticker,
        status="open",
        observed_at=clock.wall_clock(),
        close_time=clock.wall_clock() + timedelta(hours=8),
        is_provisional=False,
        rule_content_hash=ContentHash(
            sha256(f"Synthetic exact rule for {ticker}".encode()).hexdigest()
        ),
        data_quality_state=PublicMarketDataQualityState.ELIGIBLE.value,
    )
    capture = CaptureBoundedPublicOrderbookSession(
        client=client,
        store=InMemoryPublicOrderbookCaptureStore(),
        wall_clock=clock.wall_clock,
        monotonic_clock=clock.monotonic_clock,
        sleeper=clock.sleep,
        normalizer=KalshiPublicMarketNormalizer(),
    )
    request = BoundedOrderbookCaptureRequest(
        session_id="shared-enrichment-persistence",
        market_ticker=ticker,
        observations=3,
        interval_seconds=10,
        maximum_depth=5,
        maximum_bundle_quantity="10",
        timeout_seconds=10,
    )
    attempt = capture.enrich_candidate(request=request, candidate=candidate)
    assert attempt.selected is not None
    refresh = attempt.selected
    event_raw = next(
        raw for raw in refresh.raw_responses if isinstance(raw.response.document.get("event"), dict)
    )
    event = event_raw.response.document["event"]
    assert isinstance(event, dict)
    event_snapshot = KalshiPublicEventNormalizer().normalize_events(
        raw_response_id=event_raw.raw_response_id,
        page=CapturedPublicEventPage(
            endpoint_identity=event_raw.response.endpoint_identity,
            request_cursor=None,
            received_at=event_raw.response.received_at,
            http_status=event_raw.response.http_status,
            response_content_type=event_raw.response.response_content_type,
            raw_payload=event_raw.response.raw_payload,
            response_cursor=None,
            document={"events": [event]},
        ),
    )[0]
    relationship = link_event_markets(event_snapshot, (refresh.snapshot,))[0]
    evaluation = evaluate_event_consistency(
        event_snapshot,
        relationships=(relationship,),
        markets=(refresh.snapshot,),
        evaluated_at=refresh.response.received_at,
        evaluator_version="public-event-consistency-v1",
    )
    store = postgres_multi_strategy_shadow_store(postgres_engine)
    enriched = store.persist_shared_enriched_market(
        session_id="multi-strategy-session-persistence",
        iteration_ordinal=0,
        list_market_snapshot_id=candidate.market_snapshot_id,
        refresh=refresh,
        event_snapshot=event_snapshot,
        relationship=relationship,
        evaluation=evaluation,
    )
    shared_book = capture.capture_shared_orderbook(
        refresh=refresh,
        maximum_depth=5,
        timeout_seconds=10,
        request_ordinal=10001,
    )
    store.append_shared_orderbook(
        raw=shared_book.raw,
        orderbook=shared_book.orderbook,
        levels=shared_book.levels,
    )
    campaign_id = "shared-enrichment-consensus"
    service.initialize_campaign(
        CampaignInitializationCommand(
            campaign_id=campaign_id,
            contribution=Money("1000"),
            authorized_loss=Money("500"),
            policy=policy,
            occurred_at=NOW,
        )
    )
    market = postgres_strategy_shadow_store(postgres_engine).retained_market(
        enriched.market_snapshot_id
    )
    prediction = build_market_consensus_baseline_prediction(
        market=market,
        campaign_id=campaign_id,
        decision_at=market.available_at,
    )
    assert postgres_strategy_shadow_store(postgres_engine).persist_prediction(prediction) is False

    portfolio = CampaignPortfolioPolicy(
        policy_version_id="shared-enrichment-portfolio-v1",
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
        configuration_hash=ContentHash(content_digest({"campaign": campaign_id})),
    )
    postgres_autopilot_store(postgres_engine).ensure_portfolio_policy(portfolio, recorded_at=NOW)
    configuration = ShadowMarketCohortConfiguration(selected_market_count=3)
    cohort = BuildEnrichedShadowMarketCohort.build(
        campaign_id=campaign_id,
        cycle_key="shared-enrichment-cycle",
        configuration=configuration,
        candidates=(enriched,),
        decision_at=market.available_at,
    )
    descriptors = {
        descriptor.strategy_id: descriptor
        for descriptor in production_shadow_strategy_descriptors()
    }
    microstructure = orderbook_microstructure_strategy_descriptor()
    dynamics = market_dynamics_strategy_descriptor()
    command = MultiStrategyShadowCycleCommand(
        campaign_id=campaign_id,
        idempotency_key="shared-enrichment-cycle",
        cohort_configuration=configuration,
        strategy_configuration_hashes=tuple(
            sorted(
                (
                    (
                        descriptors["market-consensus-baseline-v1"].strategy_id,
                        descriptors["market-consensus-baseline-v1"].configuration_hash.value,
                    ),
                    (microstructure.strategy_id, microstructure.configuration_hash.value),
                    (dynamics.strategy_id, dynamics.configuration_hash.value),
                )
            )
        ),
        maximum_predictions=100,
        maximum_opportunities=40,
        deadline_at=market.available_at + timedelta(seconds=30),
        operator_note="shared-enrichment persistence proof",
        requested_at=market.available_at,
    )
    result = RunMultiStrategyShadowCampaignCycle(store).run(
        command=command,
        cohort=cohort,
        invocations=(
            MultiStrategyInvocation(
                descriptor=descriptors["market-consensus-baseline-v1"],
                predictions=(prediction,),
                opportunities=(),
                feature_snapshot_ids=(),
                reason_codes=("RETAINED_MARKET_CONSENSUS_PREDICTIONS",),
            ),
            MultiStrategyInvocation(
                descriptor=microstructure,
                predictions=(),
                opportunities=(),
                feature_snapshot_ids=(),
                reason_codes=("INSUFFICIENT_RETAINED_OBSERVATIONS",),
            ),
            MultiStrategyInvocation(
                descriptor=dynamics,
                predictions=(),
                opportunities=(),
                feature_snapshot_ids=(),
                reason_codes=("INSUFFICIENT_RETAINED_OBSERVATIONS",),
            ),
        ),
    )

    assert enriched.market_ticker == ticker
    assert result.prediction_count == 1
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM normalized_market_snapshot")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM normalized_event_snapshot")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM event_market_relationship")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM market_orderbook_snapshot")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM binary_complement_evaluation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM shadow_prediction")) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM multi_strategy_shadow_cycle")) == 1


def test_multi_strategy_cycle_starts_with_a_running_shadow_campaign(
    postgres_engine: Engine,
    service,
    policy,
) -> None:
    campaign_id = "multi-strategy-cycle-start"
    service.initialize_campaign(
        CampaignInitializationCommand(
            campaign_id=campaign_id,
            contribution=Money("1000"),
            authorized_loss=Money("500"),
            policy=policy,
            occurred_at=NOW,
        )
    )
    portfolio = CampaignPortfolioPolicy(
        policy_version_id="multi-strategy-portfolio-v1",
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
        configuration_hash=ContentHash(content_digest({"campaign": campaign_id})),
    )
    postgres_autopilot_store(postgres_engine).ensure_portfolio_policy(portfolio, recorded_at=NOW)
    command = MultiStrategyShadowCycleCommand(
        campaign_id=campaign_id,
        idempotency_key="start-focused-cycle",
        cohort_configuration=ShadowMarketCohortConfiguration(selected_market_count=3),
        strategy_configuration_hashes=(
            ("kalshi-market-dynamics-v1", "a" * 64),
            ("kalshi-orderbook-microstructure-v1", "b" * 64),
            ("market-consensus-baseline-v1", "c" * 64),
        ),
        maximum_predictions=100,
        maximum_opportunities=40,
        deadline_at=NOW + timedelta(seconds=30),
        operator_note="focused cycle-start proof",
        requested_at=NOW,
    )
    store = postgres_multi_strategy_shadow_store(postgres_engine)

    cycle_id, _campaign, _policy, _snapshot, replay = store.start_multi_strategy_cycle(command)

    assert replay is False
    store.fail_multi_strategy_cycle(
        cycle_id,
        reason_code="FOCUSED_TEST_FINALIZATION",
        completed_at=NOW + timedelta(seconds=1),
    )
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT state FROM multi_strategy_shadow_cycle")) == "FAILED"
        )
