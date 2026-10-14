from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from apex.adapters.postgres import (
    market_dynamics_feature_snapshot,
    market_dynamics_prediction_feature,
    multi_strategy_shadow_cycle,
    multi_strategy_shadow_invocation,
    normalized_market_snapshot,
    operator_action,
    postgres_evidence_review_store,
    postgres_shadow_operations_store,
    postgres_strategy_shadow_store,
    provider_ingestion_run,
    public_outcome_fact,
    raw_provider_response,
    shadow_evidence_cohort_member,
    shadow_evidence_iteration,
    shadow_evidence_plan,
    shadow_forward_evaluation,
    shadow_prediction,
)
from apex.application.authorize_order import DurableAuthorizationService
from apex.application.autopilot import AutopilotIdempotencyConflictError
from apex.application.discover_public_markets import (
    PublicMarketIngestionState,
    PublicMarketScanSummary,
)
from apex.application.scan_public_binary_complements import CapturedKalshiPublicResponse
from apex.application.shadow_operations import (
    AtomicShadowLaunchState,
    AtomicShadowPlanExclusions,
    CreateShadowEvidencePlanCommand,
    DiscoverAndCreateShadowEvidencePlan,
    DiscoverAndCreateShadowEvidencePlanCommand,
    FrozenShadowEvidenceCohortMember,
    PublicOutcomeState,
    ShadowEvidenceIterationState,
    ShadowEvidencePlanConfiguration,
    ValidatedShadowCohortCandidate,
    fixed_strategy_configuration_bindings,
)
from apex.domain.orderbook_microstructure import published_fee_microstructure_v2_configuration
from apex.domain.published_fee_schedule import KALSHI_FEE_SCHEDULE_SOURCE_SHA256
from apex.domain.values import ValueValidationError, content_digest

pytestmark = pytest.mark.postgres


def _member(ordinal: int, now: datetime) -> FrozenShadowEvidenceCohortMember:
    def digest(label: str) -> str:
        return content_digest({"label": label, "ordinal": ordinal})

    return FrozenShadowEvidenceCohortMember(
        market_snapshot_id=f"shadow-plan-market-{ordinal}",
        provider="KALSHI",
        market_ticker=f"SHADOW-PLAN-{ordinal}",
        event_ticker=f"SHADOW-EVENT-{ordinal}",
        series_ticker=f"SHADOW-SERIES-{ordinal}",
        rule_content_hash=digest("rule"),
        lifecycle_state="open",
        observed_at=now,
        available_at=now,
        close_at=now + timedelta(days=1),
        market_lineage_hash=digest("market"),
        event_lineage_hash=digest("event"),
        series_lineage_hash=digest("series"),
        fee_lineage_hash=digest("fee"),
    )


def _validated_member(ordinal: int, now: datetime) -> ValidatedShadowCohortCandidate:
    member = _member(ordinal, now)
    return ValidatedShadowCohortCandidate(
        list_market_snapshot_id=f"list-{member.market_snapshot_id}",
        market_snapshot_id=member.market_snapshot_id,
        event_snapshot_id=f"event-snapshot-{ordinal}",
        event_raw_response_id=f"event-raw-{ordinal}",
        series_raw_response_id=f"series-raw-{ordinal}",
        exact_market_raw_response_id=f"market-raw-{ordinal}",
        fee_snapshot_id=f"fee-{ordinal}",
        provider=member.provider,
        market_ticker=member.market_ticker,
        event_ticker=member.event_ticker,
        series_ticker=member.series_ticker,
        rule_content_hash=member.rule_content_hash,
        settlement_source_identity=content_digest({"settlement": ordinal}),
        lifecycle_state=member.lifecycle_state,
        binary_structure_identity=content_digest({"binary": ordinal}),
        observed_at=member.observed_at,
        available_at=member.available_at,
        close_at=member.close_at,
        market_lineage_hash=member.market_lineage_hash,
        event_lineage_hash=member.event_lineage_hash,
        series_lineage_hash=member.series_lineage_hash,
        fee_lineage_hash=member.fee_lineage_hash,
        enriched_at=now,
    )


def _persist_plan_scoped_prediction(
    *,
    engine: Engine,
    campaign_id: str,
    plan_id: str | None,
    ordinal: int,
    strategy_id: str,
    market_ticker: str,
    event_ticker: str,
    opportunity_count: int,
    conflict_count: int,
    admitted_count: int,
    requested_risk: Decimal,
    allocated_risk: Decimal,
    now: datetime,
) -> str:
    """Persist the smallest complete immutable lineage for one plan-scoped prediction."""

    label = f"performance-scope-{plan_id}-{ordinal}"

    def digest(field: str) -> str:
        return content_digest({"label": label, "field": field})

    ingestion_run_id = f"{label}-ingestion"
    raw_response_id = f"{label}-raw"
    snapshot_id = f"{label}-market"
    prediction_id = f"{label}-prediction"
    cycle_id = f"{label}-cycle"
    raw_payload = b"{}"
    with engine.begin() as connection:
        connection.execute(
            provider_ingestion_run.insert().values(
                ingestion_run_id=ingestion_run_id,
                provider="KALSHI",
                capability="PUBLIC_REST_MARKET_DISCOVERY",
                started_at=now,
                completed_at=now,
                requested_status="open",
                page_size=10,
                maximum_pages=1,
                state="COMPLETED",
                page_count=1,
                market_count=1,
                application_version="test",
                schema_version="v1",
                canonical_configuration_hash=digest("ingestion"),
            )
        )
        connection.execute(
            raw_provider_response.insert().values(
                raw_response_id=raw_response_id,
                ingestion_run_id=ingestion_run_id,
                provider="KALSHI",
                endpoint_identity="KALSHI_GET_MARKETS",
                request_page_ordinal=1,
                request_cursor=None,
                received_at=now,
                http_status=200,
                response_content_type="application/json",
                raw_payload=raw_payload,
                raw_payload_sha256=sha256(raw_payload).hexdigest(),
                response_size=len(raw_payload),
                provider_response_cursor=None,
                schema_version="v1",
                created_at=now,
            )
        )
        connection.execute(
            normalized_market_snapshot.insert().values(
                snapshot_id=snapshot_id,
                ingestion_run_id=ingestion_run_id,
                raw_response_id=raw_response_id,
                provider="KALSHI",
                provider_market_ticker=market_ticker,
                event_ticker=event_ticker,
                title="retained plan-scope test market",
                subtitle=None,
                status="open",
                open_time=now,
                close_time=now + timedelta(days=1),
                latest_expiration_time=None,
                observed_at=now,
                yes_bid=Decimal("0.49"),
                yes_ask=Decimal("0.51"),
                no_bid=Decimal("0.49"),
                no_ask=Decimal("0.51"),
                yes_bid_size=Decimal("10"),
                yes_ask_size=Decimal("10"),
                volume=None,
                volume_24h=None,
                open_interest=None,
                liquidity=None,
                rules_primary="retained exact rule",
                rules_secondary=None,
                rule_content_hash=digest("rule"),
                provider_payload_ordinal=0,
                canonical_snapshot_hash=digest("snapshot"),
                data_quality_state="ELIGIBLE",
                created_at=now,
            )
        )
        connection.execute(
            shadow_prediction.insert().values(
                prediction_id=prediction_id,
                strategy_id=strategy_id,
                strategy_version="v1",
                campaign_id=campaign_id,
                cycle_id=None,
                invocation_id=None,
                proposal_id=None,
                provider="KALSHI",
                instrument_id=market_ticker,
                event_id=event_ticker,
                decision_at=now,
                resolution_at=now + timedelta(hours=1),
                outcomes_payload='["NO","YES"]',
                point_probabilities_payload=(
                    '[{"outcome_id":"NO","probability":"0.5"},'
                    '{"outcome_id":"YES","probability":"0.5"}]'
                ),
                conservative_probabilities_payload=(
                    '[{"outcome_id":"NO","probability":"0.5"},'
                    '{"outcome_id":"YES","probability":"0.5"}]'
                ),
                market_probabilities_payload=(
                    '[{"outcome_id":"NO","probability":"0.5"},'
                    '{"outcome_id":"YES","probability":"0.5"}]'
                ),
                market_evidence_id=snapshot_id,
                expected_fee=Decimal("0"),
                slippage_reserve=Decimal("0"),
                execution_uncertainty_reserve=Decimal("0"),
                evidence_tier="RESEARCH",
                evidence_references=[snapshot_id],
                thesis_summary=None,
                correlation_buckets=[],
                operator_note=None,
                model_configuration_version="test-v1",
                canonical_input_hash=digest("prediction-input"),
                canonical_prediction_hash=digest("prediction-result"),
                state="UNRESOLVED",
                created_at=now,
            )
        )
        connection.execute(
            multi_strategy_shadow_cycle.insert().values(
                cycle_id=cycle_id,
                campaign_id=campaign_id,
                idempotency_key=label,
                canonical_request_hash=digest("cycle-request"),
                cohort_configuration_hash=digest("cohort"),
                strategy_configuration_hashes_payload="{}",
                maximum_predictions=10,
                maximum_opportunities=10,
                deadline_at=now + timedelta(seconds=30),
                operator_note="focused plan-scope report test",
                started_at=now,
                completed_at=now,
                state="COMPLETED",
                cohort_id=None,
                selected_market_count=1,
                invocation_count=1,
                prediction_count=1,
                opportunity_count=opportunity_count,
                conflict_count=conflict_count,
                hypothetical_admission_count=admitted_count,
                hypothetical_rejection_count=0,
                requested_risk_total=requested_risk,
                allocated_risk_total=allocated_risk,
                remaining_shadow_capacity=Decimal("100"),
                canonical_result_hash=digest("cycle-result"),
                last_error_code=None,
                created_at=now,
            )
        )
        if plan_id is not None:
            connection.execute(
                shadow_evidence_iteration.insert().values(
                    iteration_id=f"{label}-iteration",
                    plan_id=plan_id,
                    ordinal=ordinal,
                    state="COMPLETED",
                    lease_fence=1,
                    scheduled_at=now,
                    started_at=now,
                    completed_at=now,
                    cycle_id=cycle_id,
                    cohort_size=1,
                    market_refresh_count=1,
                    orderbook_success_count=1,
                    orderbook_failure_count=0,
                    prediction_count=1,
                    comparison_count=1,
                    conflict_count=conflict_count,
                    hypothetical_admission_count=admitted_count,
                    hypothetical_rejection_count=0,
                    provider_request_count=2,
                    last_completed_stage="HYPOTHETICAL_ALLOCATION_COMPLETED",
                    reason_codes=["PUBLIC_GET_ONLY", "NO_FINANCIAL_STATE_CREATED"],
                    canonical_payload_hash=digest("iteration-payload"),
                    canonical_result_hash=digest("iteration-result"),
                    created_at=now,
                )
            )
        connection.execute(
            multi_strategy_shadow_invocation.insert().values(
                invocation_id=f"{label}-invocation",
                cycle_id=cycle_id,
                ordinal=0,
                strategy_id=strategy_id,
                strategy_version="v1",
                configuration_hash=digest("configuration"),
                state="COMPLETED",
                prediction_ids=[prediction_id],
                opportunity_ids=[],
                feature_snapshot_ids=[],
                reason_codes=["RETAINED_TEST_PREDICTION"],
                canonical_payload_hash=digest("invocation"),
                created_at=now,
            )
        )
        if strategy_id == "kalshi-market-dynamics-v1":
            configuration_hash = digest("configuration")
            feature_id = f"{label}-feature"
            connection.execute(
                market_dynamics_feature_snapshot.insert().values(
                    feature_snapshot_id=feature_id,
                    strategy_id=strategy_id,
                    strategy_version="v1",
                    market_snapshot_id=snapshot_id,
                    first_market_snapshot_id=snapshot_id,
                    latest_market_snapshot_id=snapshot_id,
                    provider_market_ticker=market_ticker,
                    provider_event_ticker=event_ticker,
                    series_ticker="test-series",
                    rule_content_hash=digest("rule"),
                    decision_at=now,
                    observation_count=0,
                    first_observed_at=None,
                    latest_observed_at=None,
                    history_duration_seconds=None,
                    maximum_adjacent_gap_seconds=None,
                    minimum_adjacent_gap_seconds=None,
                    earliest_midpoint=None,
                    latest_midpoint=None,
                    earliest_spread=None,
                    latest_spread=None,
                    window_momentum=None,
                    recent_momentum=None,
                    positive_move_count=0,
                    negative_move_count=0,
                    zero_move_count=0,
                    nonzero_move_count=0,
                    signed_directional_persistence=None,
                    normalized_window_momentum=None,
                    normalized_recent_momentum=None,
                    window_momentum_contribution=None,
                    recent_momentum_contribution=None,
                    directional_persistence_contribution=None,
                    raw_adjustment=None,
                    clamped_adjustment=None,
                    feature_state="INELIGIBLE",
                    reason_codes=["TEST_FEATURE"],
                    configuration_hash=configuration_hash,
                    canonical_input_hash=digest("feature-input"),
                    canonical_feature_hash=digest("feature-result"),
                    created_at=now,
                )
            )
            connection.execute(
                market_dynamics_prediction_feature.insert().values(
                    prediction_id=prediction_id,
                    feature_snapshot_id=feature_id,
                    proposal_id=None,
                    canonical_payload_hash=digest("feature-link"),
                    created_at=now,
                )
            )
    return prediction_id


def test_legacy_configuration_lineage_classifies_plan_mismatch_and_unplanned_complete(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("configuration-lineage-classification")
    postgres_strategy_shadow_store(postgres_engine).register_catalog()
    now = datetime(2031, 1, 1, tzinfo=UTC)
    plan, _replay = postgres_shadow_operations_store(postgres_engine).create_plan(
        CreateShadowEvidencePlanCommand(
            campaign_id=campaign.campaign_id,
            name="Exact configured evidence",
            configuration=ShadowEvidencePlanConfiguration(
                iterations=3,
                market_count=3,
                max_enrichments=3,
                strategy_configuration_bindings=fixed_strategy_configuration_bindings(
                    market_count=3
                ),
            ),
            idempotency_key="configuration-lineage-plan",
            requested_at=now,
        )
    )
    planned_prediction = _persist_plan_scoped_prediction(
        engine=postgres_engine,
        campaign_id=campaign.campaign_id,
        plan_id=str(plan["plan_id"]),
        ordinal=0,
        strategy_id="kalshi-market-dynamics-v1",
        market_ticker="KX-PLANNED-MISMATCH",
        event_ticker="KX-PLANNED-EVENT",
        opportunity_count=0,
        conflict_count=0,
        admitted_count=0,
        requested_risk=Decimal(0),
        allocated_risk=Decimal(0),
        now=now,
    )
    unplanned_prediction = _persist_plan_scoped_prediction(
        engine=postgres_engine,
        campaign_id=campaign.campaign_id,
        plan_id=None,
        ordinal=1,
        strategy_id="kalshi-market-dynamics-v1",
        market_ticker="KX-UNPLANNED-COMPLETE",
        event_ticker="KX-UNPLANNED-EVENT",
        opportunity_count=0,
        conflict_count=0,
        admitted_count=0,
        requested_risk=Decimal(0),
        allocated_risk=Decimal(0),
        now=now,
    )

    source = postgres_evidence_review_store(postgres_engine).evidence_source(
        strategy_id="kalshi-market-dynamics-v1",
        strategy_version="v1",
        cutoff_at=now + timedelta(minutes=1),
    )
    states = {item.prediction_id: item.state.value for item in source.configuration_lineage}

    assert states[planned_prediction] == "PLAN_CONFIGURATION_MISMATCH"
    assert states[unplanned_prediction] == "UNPLANNED_CONFIGURATION_COMPLETE"


def test_v2_fee_contract_plan_persists_source_bound_configuration_without_financial_mutation(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("published-fee-plan-persistence")
    configuration = ShadowEvidencePlanConfiguration(
        iterations=3,
        market_count=3,
        max_enrichments=3,
        strategy_configuration_bindings=fixed_strategy_configuration_bindings(
            market_count=3,
            microstructure_configuration=published_fee_microstructure_v2_configuration(
                maximum_markets_per_invocation=3,
                minimum_observations_per_market=4,
            ),
        ),
    )
    plan, replay = postgres_shadow_operations_store(postgres_engine).create_plan(
        CreateShadowEvidencePlanCommand(
            campaign_id=campaign.campaign_id,
            name="Published fee contract evidence",
            configuration=configuration,
            idempotency_key="published-fee-plan-v2",
            requested_at=datetime(2031, 1, 1, tzinfo=UTC),
        )
    )

    assert replay is False
    assert KALSHI_FEE_SCHEDULE_SOURCE_SHA256 in str(plan["strategy_configuration_hashes_payload"])
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 1


def test_finite_plan_freezes_once_and_terminal_iteration_never_changes_financial_state(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("shadow-operations-persistence")
    store = postgres_shadow_operations_store(postgres_engine)
    now = datetime(2031, 1, 1, tzinfo=UTC)
    command = CreateShadowEvidencePlanCommand(
        campaign_id=campaign.campaign_id,
        name="Finite evidence",
        configuration=ShadowEvidencePlanConfiguration(
            iterations=3,
            market_count=3,
            max_enrichments=3,
        ),
        idempotency_key="finite-plan-v1",
        requested_at=now,
    )

    plan, replay = store.create_plan(command)
    assert replay is False
    replay_plan, replay = store.create_plan(command)
    assert replay is True
    assert replay_plan["plan_id"] == plan["plan_id"]

    frozen = store.freeze_cohort(
        plan_id=str(plan["plan_id"]),
        members=tuple(_member(ordinal, now) for ordinal in range(3)),
        frozen_at=now,
    )
    assert len(frozen) == 3
    assert (
        store.freeze_cohort(
            plan_id=str(plan["plan_id"]),
            members=(_member(4, now),),
            frozen_at=now,
        )
        == frozen
    )

    acquired, fence = store.acquire_lease(
        plan_id=str(plan["plan_id"]), owner_id="worker-a", now=now
    )
    assert acquired is True
    assert fence == 1
    iteration = store.begin_due_iteration(
        plan_id=str(plan["plan_id"]), owner_id="worker-a", now=now
    )
    assert iteration is not None
    completed = store.complete_iteration(
        iteration_id=str(iteration["iteration_id"]),
        lease_fence=1,
        state=ShadowEvidenceIterationState.PARTIAL,
        completed_at=now + timedelta(seconds=1),
        cycle_id=None,
        cohort_size=3,
        market_refresh_count=3,
        orderbook_success_count=0,
        orderbook_failure_count=3,
        prediction_count=0,
        comparison_count=0,
        conflict_count=0,
        hypothetical_admission_count=0,
        hypothetical_rejection_count=0,
        provider_request_count=0,
        last_completed_stage="ITERATION_ORDERBOOKS_CAPTURED",
        reason_codes=("INSUFFICIENT_RETAINED_ORDERBOOK_HISTORY",),
    )
    assert completed["state"] == ShadowEvidenceIterationState.PARTIAL.value

    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(shadow_evidence_plan)) == 1
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(shadow_evidence_cohort_member))
            == 3
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(shadow_evidence_iteration))
            == 1
        )
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 1


def test_explicit_plan_ticker_selection_is_retained_in_the_immutable_operator_action(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("shadow-explicit-plan-selection")
    store = postgres_shadow_operations_store(postgres_engine)
    now = datetime(2031, 1, 1, tzinfo=UTC)
    plan, replay = store.create_plan(
        CreateShadowEvidencePlanCommand(
            campaign_id=campaign.campaign_id,
            name="Explicit retained cohort",
            configuration=ShadowEvidencePlanConfiguration(
                iterations=3, market_count=3, max_enrichments=3
            ),
            idempotency_key="explicit-plan-v1",
            requested_at=now,
            requested_market_tickers=("KX-C", "KX-A", "KX-B"),
        )
    )

    assert replay is False
    assert plan["requested_market_tickers"] == ("KX-A", "KX-B", "KX-C")
    retained = store.plan(str(plan["plan_id"]))
    assert retained is not None
    assert retained["requested_market_tickers"] == ("KX-A", "KX-B", "KX-C")
    with postgres_engine.connect() as connection:
        action = connection.execute(
            sa.select(operator_action.c.target_entity_ids, operator_action.c.reason_codes).where(
                operator_action.c.idempotency_key == "shadow-plan-create:explicit-plan-v1"
            )
        ).one()
    assert action.target_entity_ids == [
        f"PLAN:{plan['plan_id']}",
        "MARKET_TICKER:KX-A",
        "MARKET_TICKER:KX-B",
        "MARKET_TICKER:KX-C",
    ]
    assert "EXPLICIT_MARKET_TICKER_SELECTION" in action.reason_codes


def test_validated_cohort_plan_and_members_are_created_in_one_transaction(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("atomic-shadow-plan-persistence")
    store = postgres_shadow_operations_store(postgres_engine)
    now = datetime(2031, 1, 1, tzinfo=UTC)
    operation_hash = content_digest({"operation": "atomic-shadow-plan-persistence"})
    exclusions = AtomicShadowPlanExclusions()
    exclusion_configuration_hash = content_digest({"exclusion-configuration": "atomic"})
    command = CreateShadowEvidencePlanCommand(
        campaign_id=campaign.campaign_id,
        name="Validated atomic cohort",
        configuration=ShadowEvidencePlanConfiguration(
            iterations=3, market_count=3, max_enrichments=3
        ),
        idempotency_key="atomic-plan-v1",
        requested_at=now,
        validated_cohort_candidates=tuple(_validated_member(ordinal, now) for ordinal in range(3)),
        atomic_operation_request_hash=operation_hash,
        atomic_exclusion_configuration_hash=exclusion_configuration_hash,
        atomic_exclusions=exclusions,
    )

    plan, replay = store.create_plan(command)

    assert replay is False
    assert len(store.cohort(str(plan["plan_id"]))) == 3
    assert (
        store.plan_for_atomic_launch(
            campaign_id=campaign.campaign_id,
            idempotency_key="atomic-plan-v1",
            exclusion_configuration_hash=exclusion_configuration_hash,
        )
        is not None
    )
    with pytest.raises(AutopilotIdempotencyConflictError, match="identity conflicts"):
        store.plan_for_atomic_launch(
            campaign_id=campaign.campaign_id,
            idempotency_key="atomic-plan-v1",
            exclusion_configuration_hash=content_digest({"exclusion-configuration": "different"}),
        )
    with postgres_engine.connect() as connection:
        action = connection.execute(
            sa.select(operator_action.c.target_entity_ids, operator_action.c.reason_codes).where(
                operator_action.c.idempotency_key == "shadow-plan-create:atomic-plan-v1"
            )
        ).one()
        assert f"ATOMIC_OPERATION_REQUEST_HASH:{operation_hash}" in action.target_entity_ids
        assert (
            f"ATOMIC_EXCLUSION_CONFIGURATION_HASH:{exclusion_configuration_hash}"
            in action.target_entity_ids
        )
        assert f"ATOMIC_EXCLUSION_SET_HASH:{exclusions.canonical_hash}" in action.target_entity_ids
        assert "VALIDATED_ENRICHED_COHORT" in action.reason_codes
        assert "ATOMIC_COHORT_EXCLUSIONS" in action.reason_codes
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0


def test_atomic_no_plan_is_durable_and_replays_without_new_provider_evidence(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("atomic-no-plan-replay")
    store = postgres_shadow_operations_store(postgres_engine)
    now = datetime(2031, 1, 1, tzinfo=UTC)
    command = DiscoverAndCreateShadowEvidencePlanCommand(
        campaign_id=campaign.campaign_id,
        name="Durable no-plan replay",
        window_start=now + timedelta(hours=1),
        window_end=now + timedelta(days=2),
        maximum_pages=1,
        maximum_inspected_markets=20,
        maximum_enrichments=20,
        target_markets=3,
        minimum_markets=3,
        minimum_events=3,
        maximum_markets_per_event=1,
        maximum_markets_per_series=3,
        iterations=3,
        interval_seconds=30,
        orderbook_depth=5,
        idempotency_key="durable-no-plan-replay-v1",
        decision_at=now,
        maximum_orderbook_preflights=20,
    )
    calls = {"discover": 0}

    def discover(_request: object) -> PublicMarketScanSummary:
        calls["discover"] += 1
        return PublicMarketScanSummary(
            ingestion_run_id="durable-no-plan-discovery",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=20,
            eligible_count=0,
            ineligible_count=20,
            invalid_count=0,
            idempotent_replay=False,
        )

    operation = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=discover,
        select_candidates=lambda _run_id, _limit: (),
        enrich_candidate=lambda _candidate, _operation_id: pytest.fail(
            "no enrichment should occur for an empty retained list"
        ),
    )

    first = operation.run(command)
    replay = operation.run(command)

    assert first.state is AtomicShadowLaunchState.NO_PLAN
    assert replay.state is AtomicShadowLaunchState.NO_PLAN
    assert first.result_hash == replay.result_hash
    assert replay.summary.idempotent_replay is True
    assert calls == {"discover": 1}
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(raw_provider_response)) == 0
        assert connection.scalar(sa.select(sa.func.count()).select_from(shadow_evidence_plan)) == 0
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(operator_action)
                .where(
                    operator_action.c.campaign_id == campaign.campaign_id,
                    operator_action.c.idempotency_key.in_(
                        (
                            "shadow-plan-launch-start:durable-no-plan-replay-v1",
                            "shadow-plan-launch-terminal:durable-no-plan-replay-v1",
                        )
                    ),
                )
            )
            == 2
        )
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0


def test_atomic_exclusions_resolve_frozen_prior_and_active_plan_markets(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("atomic-exclusion-resolution")
    store = postgres_shadow_operations_store(postgres_engine)
    now = datetime(2031, 1, 1, tzinfo=UTC)

    def create(name: str, key: str, offset: int) -> dict[str, object]:
        plan, replay = store.create_plan(
            CreateShadowEvidencePlanCommand(
                campaign_id=campaign.campaign_id,
                name=name,
                configuration=ShadowEvidencePlanConfiguration(
                    iterations=3, market_count=3, max_enrichments=3
                ),
                idempotency_key=key,
                requested_at=now,
                validated_cohort_candidates=tuple(
                    _validated_member(offset + ordinal, now) for ordinal in range(3)
                ),
            )
        )
        assert replay is False
        return plan

    prior = create("Prior frozen cohort", "prior-frozen-v1", 0)
    store.control_plan(
        plan_id=str(prior["plan_id"]),
        action="cancel",
        idempotency_key="cancel-prior-frozen-v1",
        reason="finished fixture cohort",
        requested_at=now + timedelta(seconds=1),
    )
    create("Active frozen cohort", "active-frozen-v1", 10)

    exclusions = store.resolve_atomic_launch_exclusions(
        campaign_id=campaign.campaign_id,
        prior_plan_ids=(str(prior["plan_id"]),),
        exclude_prior_plan_events=True,
        exclude_active_plan_markets=True,
    )

    assert exclusions.resolved_prior_plan_ids == (str(prior["plan_id"]),)
    assert exclusions.prior_plan_market_tickers == (
        "SHADOW-PLAN-0",
        "SHADOW-PLAN-1",
        "SHADOW-PLAN-2",
    )
    assert exclusions.prior_plan_event_tickers == (
        "SHADOW-EVENT-0",
        "SHADOW-EVENT-1",
        "SHADOW-EVENT-2",
    )
    assert exclusions.active_plan_market_tickers == (
        "SHADOW-PLAN-10",
        "SHADOW-PLAN-11",
        "SHADOW-PLAN-12",
    )
    with pytest.raises(ValueValidationError, match="not found"):
        store.resolve_atomic_launch_exclusions(
            campaign_id=campaign.campaign_id,
            prior_plan_ids=("missing-plan",),
            exclude_prior_plan_events=True,
            exclude_active_plan_markets=True,
        )
    foreign_campaign = initialize_campaign("atomic-exclusion-foreign")
    foreign = postgres_shadow_operations_store(postgres_engine).create_plan(
        CreateShadowEvidencePlanCommand(
            campaign_id=foreign_campaign.campaign_id,
            name="Foreign frozen cohort",
            configuration=ShadowEvidencePlanConfiguration(
                iterations=3, market_count=3, max_enrichments=3
            ),
            idempotency_key="foreign-frozen-v1",
            requested_at=now,
            validated_cohort_candidates=tuple(
                _validated_member(30 + ordinal, now) for ordinal in range(3)
            ),
        )
    )[0]
    with pytest.raises(ValueValidationError, match="another campaign"):
        store.resolve_atomic_launch_exclusions(
            campaign_id=campaign.campaign_id,
            prior_plan_ids=(str(foreign["plan_id"]),),
            exclude_prior_plan_events=True,
            exclude_active_plan_markets=True,
        )
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0


def test_public_outcome_fact_is_not_settlement_and_is_idempotent(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("shadow-outcome-persistence")
    store = postgres_shadow_operations_store(postgres_engine)
    now = datetime(2031, 1, 1, tzinfo=UTC)
    fact, replay = store.record_public_outcome_fact(
        campaign_id=campaign.campaign_id,
        plan_id=None,
        market_ticker="SHADOW-OUTCOME-MARKET",
        raw_payload=b'{"market":{"result":"YES"}}',
        received_at=now,
        endpoint_identity="KALSHI_GET_MARKET",
        response_content_type="application/json",
        resolved_outcome="YES",
        state=PublicOutcomeState.RESOLVED,
        rule_content_hash=content_digest({"rule": "retained"}),
        reason_codes=("PUBLIC_EXACT_MARKET_GET",),
    )
    replay_fact, replay = store.record_public_outcome_fact(
        campaign_id=campaign.campaign_id,
        plan_id=None,
        market_ticker="SHADOW-OUTCOME-MARKET",
        raw_payload=b'{"market":{"result":"YES"}}',
        received_at=now,
        endpoint_identity="KALSHI_GET_MARKET",
        response_content_type="application/json",
        resolved_outcome="YES",
        state=PublicOutcomeState.RESOLVED,
        rule_content_hash=content_digest({"rule": "retained"}),
        reason_codes=("PUBLIC_EXACT_MARKET_GET",),
    )

    assert replay is True
    assert replay_fact["outcome_fact_id"] == fact["outcome_fact_id"]
    assert store.evaluate_public_outcome_fact(
        outcome_fact_id=str(fact["outcome_fact_id"]), evaluated_at=now + timedelta(seconds=1)
    ) == (0, 0)
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(public_outcome_fact)) == 1
        assert connection.scalar(sa.text("SELECT count(*) FROM settlement_fact")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position_settlement")) == 0


def test_reconciliation_preserves_invalid_fact_and_reuses_its_exact_market_raw_response(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    del service
    campaign = initialize_campaign("shadow-outcome-reconciliation-immutability")
    store = postgres_shadow_operations_store(postgres_engine)
    now = datetime(2031, 1, 1, tzinfo=UTC)
    raw_payload = b'{"market":{"ticker":"RECONCILE-MARKET","result":"YES"}}'
    rule_hash = content_digest({"rule": "reconciliation-retained"})
    invalid, replay = store.record_public_outcome_fact(
        campaign_id=campaign.campaign_id,
        plan_id=None,
        market_ticker="RECONCILE-MARKET",
        raw_payload=raw_payload,
        received_at=now,
        endpoint_identity="KALSHI_GET_MARKET",
        response_content_type="application/json",
        resolved_outcome=None,
        state=PublicOutcomeState.INVALID,
        rule_content_hash=rule_hash,
        reason_codes=("SERIES_IDENTITY_MISSING_FINAL_RESPONSE",),
        event_ticker="RECONCILE-EVENT",
        series_ticker="RECONCILE-SERIES",
    )
    assert replay is False
    event_raw_id = store.record_public_outcome_reconciliation_event_response(
        campaign_id=campaign.campaign_id,
        plan_id="reconciliation-plan",
        market_ticker="RECONCILE-MARKET",
        event_ticker="RECONCILE-EVENT",
        response=CapturedKalshiPublicResponse(
            endpoint_identity="KALSHI_GET_EVENTS",
            received_at=now + timedelta(seconds=1),
            http_status=200,
            response_content_type="application/json",
            raw_payload=b'{"event":{"event_ticker":"RECONCILE-EVENT","markets":[]}}',
            document={"event": {"event_ticker": "RECONCILE-EVENT", "markets": []}},
        ),
    )
    resolved, replay = store.record_public_outcome_fact(
        campaign_id=campaign.campaign_id,
        plan_id=None,
        market_ticker="RECONCILE-MARKET",
        raw_payload=raw_payload,
        received_at=now + timedelta(seconds=2),
        endpoint_identity="KALSHI_GET_MARKET",
        response_content_type="application/json",
        resolved_outcome="YES",
        state=PublicOutcomeState.RESOLVED,
        rule_content_hash=rule_hash,
        reason_codes=("PUBLIC_OUTCOME_IDENTITY_RECONCILIATION_V1",),
        event_ticker="RECONCILE-EVENT",
        series_ticker="RECONCILE-SERIES",
        reconciliation_raw_response_ids=(event_raw_id,),
    )
    replay_resolved, replay = store.record_public_outcome_fact(
        campaign_id=campaign.campaign_id,
        plan_id=None,
        market_ticker="RECONCILE-MARKET",
        raw_payload=raw_payload,
        received_at=now + timedelta(seconds=3),
        endpoint_identity="KALSHI_GET_MARKET",
        response_content_type="application/json",
        resolved_outcome="YES",
        state=PublicOutcomeState.RESOLVED,
        rule_content_hash=rule_hash,
        reason_codes=("PUBLIC_OUTCOME_IDENTITY_RECONCILIATION_V1",),
        event_ticker="RECONCILE-EVENT",
        series_ticker="RECONCILE-SERIES",
        reconciliation_raw_response_ids=(event_raw_id,),
    )

    assert replay is True
    assert invalid["outcome_fact_id"] != resolved["outcome_fact_id"]
    assert resolved["outcome_fact_id"] == replay_resolved["outcome_fact_id"]
    assert invalid["raw_response_id"] == resolved["raw_response_id"]
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(public_outcome_fact)) == 2
        assert connection.scalar(sa.select(sa.func.count()).select_from(raw_provider_response)) == 2
        assert connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")) == 0
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM settlement_fact")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 1


def test_performance_report_serializes_repeating_decimal_averages_at_score_precision(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    del service
    campaign = initialize_campaign("performance-repeating-decimal")
    store = postgres_shadow_operations_store(postgres_engine)
    postgres_strategy_shadow_store(postgres_engine).register_catalog()
    now = datetime(2031, 1, 1, tzinfo=UTC)
    plan, replay = store.create_plan(
        CreateShadowEvidencePlanCommand(
            campaign_id=campaign.campaign_id,
            name="Repeating decimal performance",
            configuration=ShadowEvidencePlanConfiguration(
                iterations=3, market_count=3, max_enrichments=3
            ),
            idempotency_key="performance-repeating-decimal-v1",
            requested_at=now,
        )
    )
    assert replay is False
    prediction_ids = tuple(
        _persist_plan_scoped_prediction(
            engine=postgres_engine,
            campaign_id=campaign.campaign_id,
            plan_id=str(plan["plan_id"]),
            ordinal=ordinal,
            strategy_id="market-consensus-baseline-v1",
            market_ticker=f"REPEATING-MARKET-{ordinal}",
            event_ticker=f"REPEATING-EVENT-{ordinal}",
            opportunity_count=0,
            conflict_count=0,
            admitted_count=0,
            requested_risk=Decimal(0),
            allocated_risk=Decimal(0),
            now=now,
        )
        for ordinal in range(3)
    )
    fact, replay = store.record_public_outcome_fact(
        campaign_id=campaign.campaign_id,
        plan_id=str(plan["plan_id"]),
        market_ticker="REPEATING-MARKET-0",
        raw_payload=b'{"market":{"result":"YES"}}',
        received_at=now + timedelta(seconds=1),
        endpoint_identity="KALSHI_GET_MARKET",
        response_content_type="application/json",
        resolved_outcome="YES",
        state=PublicOutcomeState.RESOLVED,
        rule_content_hash=content_digest({"rule": "repeating-decimal"}),
        reason_codes=("PUBLIC_EXACT_MARKET_GET",),
    )
    assert replay is False
    with postgres_engine.begin() as connection:
        for ordinal, prediction_id in enumerate(prediction_ids):
            score = Decimal(int(ordinal == 2))
            connection.execute(
                shadow_forward_evaluation.insert().values(
                    evaluation_id=f"repeating-decimal-evaluation-{ordinal}",
                    prediction_id=prediction_id,
                    outcome_fact_id=str(fact["outcome_fact_id"]),
                    campaign_id=campaign.campaign_id,
                    plan_id=str(plan["plan_id"]),
                    strategy_id="market-consensus-baseline-v1",
                    evaluated_at=now + timedelta(seconds=2),
                    resolved_outcome="YES",
                    point_brier_score=score,
                    point_log_loss=score,
                    conservative_brier_score=score,
                    conservative_log_loss=score,
                    consensus_brier_score=score,
                    consensus_log_loss=score,
                    point_direction_correct=ordinal > 0,
                    conservative_direction_correct=ordinal > 0,
                    point_brier_difference=score,
                    conservative_brier_difference=score,
                    point_log_loss_difference=score,
                    conservative_log_loss_difference=score,
                    numerical_method="Decimal.ln",
                    numerical_precision=28,
                    canonical_input_hash=content_digest({"input": ordinal}),
                    canonical_result_hash=content_digest({"result": ordinal}),
                    created_at=now + timedelta(seconds=2),
                )
            )

    report = store.performance_report(
        campaign_id=campaign.campaign_id,
        strategy_id=None,
        plan_id=str(plan["plan_id"]),
        requested_at=now + timedelta(seconds=3),
    )

    assert report["metrics"]["point_brier_score"] == Decimal("0.33333333")
    assert report["metrics"]["point_direction_accuracy"] == Decimal("0.66666667")


def test_performance_report_scopes_predictions_and_aggregates_to_the_requested_plan(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("plan-scoped-performance")
    other_campaign = initialize_campaign("plan-scoped-performance-foreign")
    store = postgres_shadow_operations_store(postgres_engine)
    postgres_strategy_shadow_store(postgres_engine).register_catalog()
    now = datetime(2031, 1, 1, tzinfo=UTC)

    def plan(name: str, key: str, campaign_id: str) -> dict[str, object]:
        value, replay = store.create_plan(
            CreateShadowEvidencePlanCommand(
                campaign_id=campaign_id,
                name=name,
                configuration=ShadowEvidencePlanConfiguration(
                    iterations=3, market_count=3, max_enrichments=3
                ),
                idempotency_key=key,
                requested_at=now,
            )
        )
        assert replay is False
        return value

    first = plan("First scoped plan", "performance-first", campaign.campaign_id)
    second = plan("Second scoped plan", "performance-second", campaign.campaign_id)
    foreign = plan("Foreign scoped plan", "performance-foreign", other_campaign.campaign_id)
    first_prediction = _persist_plan_scoped_prediction(
        engine=postgres_engine,
        campaign_id=campaign.campaign_id,
        plan_id=str(first["plan_id"]),
        ordinal=0,
        strategy_id="market-consensus-baseline-v1",
        market_ticker="PERFORMANCE-FIRST",
        event_ticker="PERFORMANCE-EVENT-FIRST",
        opportunity_count=1,
        conflict_count=0,
        admitted_count=1,
        requested_risk=Decimal("10"),
        allocated_risk=Decimal("5"),
        now=now,
    )
    second_prediction = _persist_plan_scoped_prediction(
        engine=postgres_engine,
        campaign_id=campaign.campaign_id,
        plan_id=str(second["plan_id"]),
        ordinal=0,
        strategy_id="market-consensus-baseline-v1",
        market_ticker="PERFORMANCE-SECOND",
        event_ticker="PERFORMANCE-EVENT-SECOND",
        opportunity_count=2,
        conflict_count=1,
        admitted_count=0,
        requested_risk=Decimal("20"),
        allocated_risk=Decimal("0"),
        now=now,
    )
    _persist_plan_scoped_prediction(
        engine=postgres_engine,
        campaign_id=campaign.campaign_id,
        plan_id=str(second["plan_id"]),
        ordinal=1,
        strategy_id="kalshi-market-dynamics-v1",
        market_ticker="PERFORMANCE-THIRD",
        event_ticker="PERFORMANCE-EVENT-THIRD",
        opportunity_count=3,
        conflict_count=0,
        admitted_count=1,
        requested_risk=Decimal("30"),
        allocated_risk=Decimal("15"),
        now=now,
    )
    fact, replay = store.record_public_outcome_fact(
        campaign_id=campaign.campaign_id,
        plan_id=str(second["plan_id"]),
        market_ticker="PERFORMANCE-SECOND",
        raw_payload=b'{"market":{"result":"YES"}}',
        received_at=now + timedelta(seconds=1),
        endpoint_identity="KALSHI_GET_MARKET",
        response_content_type="application/json",
        resolved_outcome="YES",
        state=PublicOutcomeState.RESOLVED,
        rule_content_hash=content_digest({"rule": "performance-second"}),
        reason_codes=("PUBLIC_EXACT_MARKET_GET",),
    )
    assert replay is False
    with postgres_engine.begin() as connection:
        connection.execute(
            shadow_forward_evaluation.insert().values(
                evaluation_id="performance-second-evaluation",
                prediction_id=second_prediction,
                outcome_fact_id=str(fact["outcome_fact_id"]),
                campaign_id=campaign.campaign_id,
                plan_id=str(second["plan_id"]),
                strategy_id="market-consensus-baseline-v1",
                evaluated_at=now + timedelta(seconds=2),
                resolved_outcome="YES",
                point_brier_score=Decimal("0.25"),
                point_log_loss=Decimal("0.69314718"),
                conservative_brier_score=Decimal("0.25"),
                conservative_log_loss=Decimal("0.69314718"),
                consensus_brier_score=Decimal("0.25"),
                consensus_log_loss=Decimal("0.69314718"),
                point_direction_correct=True,
                conservative_direction_correct=True,
                point_brier_difference=Decimal("0"),
                conservative_brier_difference=Decimal("0"),
                point_log_loss_difference=Decimal("0"),
                conservative_log_loss_difference=Decimal("0"),
                numerical_method="Decimal.ln",
                numerical_precision=28,
                canonical_input_hash=content_digest({"input": "performance-second"}),
                canonical_result_hash=content_digest({"result": "performance-second"}),
                created_at=now + timedelta(seconds=2),
            )
        )
    with postgres_engine.connect() as connection:
        financial_before = (
            connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")),
            connection.scalar(sa.text('SELECT count(*) FROM "order"')),
            connection.scalar(sa.text("SELECT count(*) FROM position")),
            connection.scalar(sa.text("SELECT count(*) FROM settlement_fact")),
            connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")),
        )

    campaign_report = store.performance_report(
        campaign_id=campaign.campaign_id,
        strategy_id=None,
        plan_id=None,
        requested_at=now + timedelta(seconds=3),
    )
    first_report = store.performance_report(
        campaign_id=campaign.campaign_id,
        strategy_id=None,
        plan_id=str(first["plan_id"]),
        requested_at=now + timedelta(seconds=4),
    )
    second_report = store.performance_report(
        campaign_id=campaign.campaign_id,
        strategy_id=None,
        plan_id=str(second["plan_id"]),
        requested_at=now + timedelta(seconds=5),
    )

    assert campaign_report["scope"] == "CAMPAIGN"
    assert campaign_report["metrics"]["prediction_count"] == 3
    assert first_report["scope"] == "PLAN"
    assert first_report["metrics"]["prediction_count"] == 1
    assert first_report["metrics"]["plan_prediction_count"] == 1
    assert first_report["metrics"]["campaign_prediction_count"] == 3
    assert first_report["metrics"]["resolved_prediction_count"] == 0
    assert first_report["metrics"]["distinct_market_count"] == 1
    assert first_report["metrics"]["distinct_event_count"] == 1
    assert first_report["metrics"]["opportunity_count"] == 1
    assert first_report["metrics"]["conflict_count"] == 0
    assert first_report["metrics"]["hypothetical_admission_count"] == 1
    assert first_report["metrics"]["requested_risk_total"] == Decimal("10")
    assert second_report["metrics"]["prediction_count"] == 2
    assert second_report["metrics"]["resolved_prediction_count"] == 1
    assert second_report["metrics"]["unresolved_prediction_count"] == 1
    assert second_report["metrics"]["distinct_market_count"] == 2
    assert second_report["metrics"]["distinct_event_count"] == 2
    assert second_report["metrics"]["opportunity_count"] == 5
    assert second_report["metrics"]["conflict_count"] == 1
    assert second_report["metrics"]["hypothetical_admission_count"] == 1
    assert second_report["metrics"]["requested_risk_total"] == Decimal("50")
    assert second_report["metrics"]["allocated_risk_total"] == Decimal("15")
    assert second_report["metrics"]["strategy_counts"] == (
        {
            "strategy_id": "kalshi-market-dynamics-v1",
            "strategy_version": "v1",
            "prediction_count": 1,
            "resolved_prediction_count": 0,
            "unresolved_prediction_count": 1,
        },
        {
            "strategy_id": "market-consensus-baseline-v1",
            "strategy_version": "v1",
            "prediction_count": 1,
            "resolved_prediction_count": 1,
            "unresolved_prediction_count": 0,
        },
    )
    with pytest.raises(ValueValidationError, match="was not found"):
        store.performance_report(
            campaign_id=campaign.campaign_id,
            strategy_id=None,
            plan_id="missing-plan",
            requested_at=now + timedelta(seconds=6),
        )
    with pytest.raises(ValueValidationError, match="another campaign"):
        store.performance_report(
            campaign_id=campaign.campaign_id,
            strategy_id=None,
            plan_id=str(foreign["plan_id"]),
            requested_at=now + timedelta(seconds=7),
        )
    with postgres_engine.connect() as connection:
        financial_after = (
            connection.scalar(sa.text("SELECT count(*) FROM risk_reservation")),
            connection.scalar(sa.text('SELECT count(*) FROM "order"')),
            connection.scalar(sa.text("SELECT count(*) FROM position")),
            connection.scalar(sa.text("SELECT count(*) FROM settlement_fact")),
            connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")),
        )
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            "0118_evidence_gap_recovery"
        )
    assert first_prediction != second_prediction
    assert financial_after == financial_before


def test_created_plan_can_cancel_with_a_reason_and_idempotent_audit(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("shadow-plan-cancel")
    store = postgres_shadow_operations_store(postgres_engine)
    now = datetime(2031, 1, 1, tzinfo=UTC)
    plan, _ = store.create_plan(
        CreateShadowEvidencePlanCommand(
            campaign_id=campaign.campaign_id,
            name="Cancel before first work",
            configuration=ShadowEvidencePlanConfiguration(
                iterations=3, market_count=3, max_enrichments=3
            ),
            idempotency_key="cancel-plan-v1",
            requested_at=now,
        )
    )

    canceled, replay = store.control_plan(
        plan_id=str(plan["plan_id"]),
        action="cancel",
        idempotency_key="cancel-plan-action-v1",
        reason="operator ended finite evidence collection",
        requested_at=now + timedelta(seconds=1),
    )
    assert replay is False
    assert canceled["state"] == "CANCELED"
    replay_value, replay = store.control_plan(
        plan_id=str(plan["plan_id"]),
        action="cancel",
        idempotency_key="cancel-plan-action-v1",
        reason="operator ended finite evidence collection",
        requested_at=now + timedelta(seconds=2),
    )
    assert replay is True
    assert replay_value["plan_id"] == plan["plan_id"]


def test_canceled_plan_keeps_its_terminal_state_while_a_fenced_iteration_recovers(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    campaign = initialize_campaign("shadow-plan-canceled-worker-recovery")
    store = postgres_shadow_operations_store(postgres_engine)
    now = datetime(2031, 1, 1, tzinfo=UTC)
    plan, _ = store.create_plan(
        CreateShadowEvidencePlanCommand(
            campaign_id=campaign.campaign_id,
            name="Canceled after a fenced start",
            configuration=ShadowEvidencePlanConfiguration(
                iterations=3, market_count=3, max_enrichments=3
            ),
            idempotency_key="cancel-plan-worker-recovery-v1",
            requested_at=now,
        )
    )
    plan_id = str(plan["plan_id"])
    acquired, fence = store.acquire_lease(plan_id=plan_id, owner_id="worker-a", now=now)
    assert acquired is True
    assert fence == 1
    assert store.begin_due_iteration(plan_id=plan_id, owner_id="worker-a", now=now) is not None

    canceled, _ = store.control_plan(
        plan_id=plan_id,
        action="cancel",
        idempotency_key="cancel-plan-worker-recovery-action-v1",
        reason="foreground worker interrupted after it started",
        requested_at=now + timedelta(seconds=1),
    )
    assert canceled["state"] == "CANCELED"
    assert store.interrupt_owned_iteration(
        plan_id=plan_id,
        owner_id="worker-a",
        fence=1,
        interrupted_at=now + timedelta(seconds=2),
        reason_code="LOCAL_FOREGROUND_WORKER_INTERRUPTED",
    )
    assert store.release_lease(
        plan_id=plan_id,
        owner_id="worker-a",
        fence=1,
        released_at=now + timedelta(seconds=2),
    )

    retained = store.plan(plan_id)
    assert retained is not None
    assert retained["state"] == "CANCELED"
    iterations = store.iterations(plan_id)
    assert len(iterations) == 1
    assert iterations[0]["state"] == ShadowEvidenceIterationState.INTERRUPTED.value
    with postgres_engine.connect() as connection:
        assert connection.scalar(sa.text('SELECT count(*) FROM "order"')) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM position")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM ledger_journal")) == 1


def test_fixture_governed_strategy_carries_no_per_prediction_configuration_lineage(
    postgres_engine: Engine,
    service: DurableAuthorizationService,
    initialize_campaign,
) -> None:
    """Directly persisted fixture forecasts must not be labelled lineage-MISSING."""

    from apex.adapters.autopilot_fixture import (
        GOVERNED_EVIDENCE_FIXTURE_STRATEGY_ID,
        fixture_governed_evidence_registry,
        fixture_governed_forward_prediction,
    )
    from apex.adapters.postgres import (
        normalized_market_snapshot,
        postgres_autopilot_store,
    )
    from apex.adapters.postgres import (
        postgres_strategy_shadow_store as shadow_store_factory,
    )

    campaign = initialize_campaign("fixture-governed-lineage")
    registry = fixture_governed_evidence_registry()
    postgres_autopilot_store(postgres_engine).register_descriptors(registry.descriptors)
    now = datetime(2031, 2, 1, tzinfo=UTC)
    postgres_strategy_shadow_store(postgres_engine).register_catalog()
    neighbour_id = _persist_plan_scoped_prediction(
        engine=postgres_engine,
        campaign_id=campaign.campaign_id,
        plan_id=None,
        ordinal=0,
        strategy_id="kalshi-market-dynamics-v1",
        market_ticker="KX-FIXTURE-GOVERNED",
        event_ticker="KX-FIXTURE-GOVERNED-EVENT",
        opportunity_count=0,
        conflict_count=0,
        admitted_count=0,
        requested_risk=Decimal(0),
        allocated_risk=Decimal(0),
        now=now,
    )
    with postgres_engine.connect() as connection:
        snapshot_id = connection.execute(
            sa.select(normalized_market_snapshot.c.snapshot_id).limit(1)
        ).scalar_one()
    prediction, _outcome = fixture_governed_forward_prediction(
        campaign_id=campaign.campaign_id,
        market_snapshot_id=str(snapshot_id),
        market_ticker="KX-FIXTURE-GOVERNED",
        event_ticker="KX-FIXTURE-GOVERNED-EVENT",
        decision_at=now,
        ordinal=0,
    )
    shadow_store_factory(postgres_engine).persist_prediction(prediction)
    descriptor = registry.descriptors[0]

    source = postgres_evidence_review_store(postgres_engine).evidence_source(
        strategy_id=GOVERNED_EVIDENCE_FIXTURE_STRATEGY_ID,
        strategy_version=descriptor.version,
        cutoff_at=now + timedelta(minutes=1),
    )

    assert len(source.predictions) == 1
    assert source.configuration_lineage == ()
    assert neighbour_id  # lineage-bearing strategies are unaffected
