from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from apex.application.capture_public_orderbook_session import CaptureMarketCandidate
from apex.application.discover_public_markets import (
    PublicMarketIngestionState,
    PublicMarketScanSummary,
)
from apex.application.market_dynamics_shadow import RunMarketDynamicsShadowStrategy
from apex.application.shadow_operations import (
    AtomicShadowLaunchLifecycleError,
    AtomicShadowLaunchOperation,
    AtomicShadowLaunchState,
    AtomicShadowPlanExclusions,
    AtomicShadowPlanLaunchSummary,
    CreateShadowEvidencePlanCommand,
    DiscoverAndCreateShadowEvidencePlan,
    DiscoverAndCreateShadowEvidencePlanCommand,
    ExactShadowCohortEnrichment,
    ExactShadowCohortEnrichmentAttempt,
    ShadowEvidencePlanConfiguration,
    ShadowPlanConfigurationBindingError,
    fixed_strategy_configuration_bindings,
    fixed_strategy_configuration_hashes,
    resolve_plan_strategy_configurations,
    score_shadow_prediction_against_public_outcome,
)
from apex.application.strategy_shadow import RetainedMarketEvidence
from apex.domain.market_dynamics import MarketDynamicsFeatureSnapshot, MarketDynamicsStrategyConfig
from apex.domain.orderbook_microstructure import (
    PUBLISHED_FEE_CONFIGURATION_VERSION,
    OrderbookMicrostructureStrategyConfig,
    published_fee_microstructure_v2_configuration,
)
from apex.domain.public_complement import (
    FEE_MODEL_IDENTITY,
    MAKER_COEFFICIENT,
    ROUNDING_IDENTITY,
    SCHEDULE_EFFECTIVE_DATE,
    SCHEDULE_IDENTITY,
    TAKER_COEFFICIENT,
    OrderbookSide,
    PublicOrderbookLevel,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.published_fee_schedule import (
    KALSHI_FEE_SCHEDULE_SOURCE_SHA256,
    KALSHI_PUBLISHED_FEE_SCHEDULE,
)
from apex.domain.values import ContentHash, ValueValidationError, content_digest


def test_finite_shadow_plan_defaults_are_bounded_and_hash_stable() -> None:
    first = ShadowEvidencePlanConfiguration()
    second = ShadowEvidencePlanConfiguration()

    assert first.iterations == 12
    assert first.interval_seconds == 300
    assert first.market_count == 10
    assert first.canonical_hash == second.canonical_hash

    with pytest.raises(ValueValidationError, match="between three and 288"):
        ShadowEvidencePlanConfiguration(iterations=289)
    with pytest.raises(ValueValidationError, match="between 30 and 86400"):
        ShadowEvidencePlanConfiguration(interval_seconds=29)


def test_plan_command_identity_is_configuration_bound() -> None:
    timestamp = datetime(2031, 1, 1, tzinfo=UTC)
    first = CreateShadowEvidencePlanCommand(
        campaign_id="campaign-shadow-operations",
        name="Forward evidence",
        configuration=ShadowEvidencePlanConfiguration(),
        idempotency_key="forward-evidence-v1",
        requested_at=timestamp,
    )
    changed = CreateShadowEvidencePlanCommand(
        campaign_id="campaign-shadow-operations",
        name="Forward evidence",
        configuration=ShadowEvidencePlanConfiguration(market_count=7),
        idempotency_key="forward-evidence-v1",
        requested_at=timestamp,
    )

    assert first.canonical_request_hash != changed.canonical_request_hash


def test_explicit_plan_tickers_are_canonicalized_and_configuration_bound() -> None:
    configuration = ShadowEvidencePlanConfiguration(market_count=3)
    command = CreateShadowEvidencePlanCommand(
        campaign_id="campaign-shadow-operations",
        name="Explicit forward evidence",
        configuration=configuration,
        idempotency_key="explicit-forward-evidence-v1",
        requested_at=datetime(2031, 1, 1, tzinfo=UTC),
        requested_market_tickers=("KX-C", "KX-A", "KX-B"),
    )

    assert command.requested_market_tickers == ("KX-A", "KX-B", "KX-C")
    assert (
        command.canonical_request_hash
        != CreateShadowEvidencePlanCommand(
            campaign_id="campaign-shadow-operations",
            name="Explicit forward evidence",
            configuration=configuration,
            idempotency_key="explicit-forward-evidence-v1",
            requested_at=datetime(2031, 1, 1, tzinfo=UTC),
            requested_market_tickers=("KX-A", "KX-B", "KX-D"),
        ).canonical_request_hash
    )
    with pytest.raises(ValueValidationError, match="must be unique"):
        CreateShadowEvidencePlanCommand(
            campaign_id="campaign-shadow-operations",
            name="Explicit forward evidence",
            configuration=configuration,
            idempotency_key="explicit-forward-evidence-v1",
            requested_at=datetime(2031, 1, 1, tzinfo=UTC),
            requested_market_tickers=("KX-A", "KX-A", "KX-C"),
        )
    with pytest.raises(ValueValidationError, match="must equal"):
        CreateShadowEvidencePlanCommand(
            campaign_id="campaign-shadow-operations",
            name="Explicit forward evidence",
            configuration=configuration,
            idempotency_key="explicit-forward-evidence-v1",
            requested_at=datetime(2031, 1, 1, tzinfo=UTC),
            requested_market_tickers=("KX-A", "KX-B", "KX-C", "KX-D"),
        )


def test_plan_strategy_hashes_match_the_fixed_three_strategy_configuration() -> None:
    hashes = fixed_strategy_configuration_hashes(market_count=6)

    assert tuple(strategy_id for strategy_id, _hash in hashes) == (
        "kalshi-market-dynamics-v1",
        "kalshi-orderbook-microstructure-v1",
        "market-consensus-baseline-v1",
    )
    assert all(len(configuration_hash) == 64 for _strategy_id, configuration_hash in hashes)


def test_plan_strategy_bindings_reconstruct_exact_configuration_and_reject_tampering() -> None:
    bindings = fixed_strategy_configuration_bindings(market_count=6)
    configuration = ShadowEvidencePlanConfiguration(
        market_count=6,
        strategy_configuration_bindings=bindings,
    )

    rebuilt = resolve_plan_strategy_configurations(configuration)

    assert (
        rebuilt["kalshi-market-dynamics-v1"].canonical_hash.value
        == dict(configuration.strategy_configuration_hashes)["kalshi-market-dynamics-v1"]
    )
    tampered = replace(
        bindings[2], configuration_hash=content_digest({"tampered": "configuration"})
    )
    invalid = ShadowEvidencePlanConfiguration(
        market_count=6,
        strategy_configuration_bindings=(*bindings[:2], tampered),
    )

    with pytest.raises(ShadowPlanConfigurationBindingError, match="PLAN_CONFIGURATION_MISMATCH"):
        resolve_plan_strategy_configurations(invalid)


def test_v2_plan_binding_includes_complete_published_fee_contract_and_rejects_tampering() -> None:
    microstructure = published_fee_microstructure_v2_configuration(
        maximum_markets_per_invocation=6,
        minimum_observations_per_market=4,
    )
    bindings = fixed_strategy_configuration_bindings(
        market_count=6,
        microstructure_configuration=microstructure,
    )
    configuration = ShadowEvidencePlanConfiguration(
        market_count=6,
        strategy_configuration_bindings=bindings,
    )

    rebuilt = resolve_plan_strategy_configurations(configuration)

    rebuilt_microstructure = rebuilt["kalshi-orderbook-microstructure-v1"]
    assert isinstance(rebuilt_microstructure, OrderbookMicrostructureStrategyConfig)
    assert rebuilt_microstructure.canonical_hash == microstructure.canonical_hash
    micro_payload = bindings[1].payload
    assert micro_payload["published_fee_source_sha256"] == KALSHI_FEE_SCHEDULE_SOURCE_SHA256
    assert (
        micro_payload["published_fee_source_url"]
        == "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
    )
    tampered = replace(
        bindings[1],
        configuration_payload=tuple(
            (
                key,
                "https://kalshi.com/docs/other-fee-schedule.pdf"
                if key == "published_fee_source_url"
                else value,
            )
            for key, value in bindings[1].configuration_payload
        ),
    )
    invalid = ShadowEvidencePlanConfiguration(
        market_count=6,
        strategy_configuration_bindings=(bindings[0], tampered, bindings[2]),
    )

    with pytest.raises(ShadowPlanConfigurationBindingError, match="PLAN_CONFIGURATION_MISMATCH"):
        resolve_plan_strategy_configurations(invalid)


class _NoPredictionWrites:
    def persist_market_dynamics_feature(self, feature: object) -> bool:
        raise AssertionError("configuration mismatch must not persist a feature")

    def persist_prediction(self, prediction: object) -> bool:
        raise AssertionError("configuration mismatch must not persist a prediction")

    def link_market_dynamics_prediction(
        self, *, prediction: object, feature_snapshot_id: str
    ) -> bool:
        raise AssertionError("configuration mismatch must not link a prediction")


def test_configuration_mismatch_prevents_feature_prediction_and_opportunity_persistence() -> None:
    configuration = MarketDynamicsStrategyConfig(maximum_markets_per_invocation=3)
    feature = cast(
        MarketDynamicsFeatureSnapshot,
        SimpleNamespace(configuration_hash=ContentHash("0" * 64)),
    )

    result = RunMarketDynamicsShadowStrategy(_NoPredictionWrites()).run(
        campaign_id="fixture-campaign",
        market=cast(RetainedMarketEvidence, object()),
        feature=feature,
        configuration=configuration,
        decision_at=datetime(2031, 1, 1, tzinfo=UTC),
        expected_configuration_hash=configuration.canonical_hash,
        expected_configuration_version=configuration.configuration_version,
    )

    assert result.prediction is None
    assert result.opportunity is None
    assert result.reason_codes == ("PLAN_STRATEGY_CONFIGURATION_MISMATCH",)


def test_public_outcome_scoring_is_decimal_deterministic_and_nonfinancial() -> None:
    first = score_shadow_prediction_against_public_outcome(
        point_probabilities=(("NO", Decimal("0.30")), ("YES", Decimal("0.70"))),
        conservative_probabilities=(("NO", Decimal("0.40")), ("YES", Decimal("0.60"))),
        consensus_probabilities=(("NO", Decimal("0.50")), ("YES", Decimal("0.50"))),
        resolved_outcome="YES",
    )
    replay = score_shadow_prediction_against_public_outcome(
        point_probabilities=(("NO", Decimal("0.30")), ("YES", Decimal("0.70"))),
        conservative_probabilities=(("NO", Decimal("0.40")), ("YES", Decimal("0.60"))),
        consensus_probabilities=(("NO", Decimal("0.50")), ("YES", Decimal("0.50"))),
        resolved_outcome="YES",
    )

    assert first == replay
    assert first.point_brier_score < first.consensus_brier_score
    assert first.conservative_brier_score < first.consensus_brier_score
    assert first.point_direction_correct is True
    assert first.numerical_method == "Decimal.ln"
    assert first.numerical_precision == 50


class _AtomicLaunchStore:
    def __init__(self) -> None:
        self.plan: dict[str, object] | None = None
        self.request_hash: str | None = None
        self.configuration_hash: str | None = None
        self.exclusions = AtomicShadowPlanExclusions()
        self.resolve_error: Exception | None = None
        self.created: CreateShadowEvidencePlanCommand | None = None
        self.operation: AtomicShadowLaunchOperation | None = None

    def reserve_atomic_launch_operation(
        self,
        *,
        command: DiscoverAndCreateShadowEvidencePlanCommand,
        operation_id: str,
        operation_request_hash: str,
        exclusions: AtomicShadowPlanExclusions,
    ) -> AtomicShadowLaunchOperation:
        if self.operation is not None:
            if self.operation.operation_request_hash != operation_request_hash:
                raise RuntimeError("atomic shadow evidence plan identity conflicts")
            return self.operation
        if self.plan is not None:
            if self.configuration_hash != command.exclusion_configuration_hash:
                raise RuntimeError("atomic shadow evidence plan identity conflicts")
            raise AtomicShadowLaunchLifecycleError(
                AtomicShadowLaunchState.INTERRUPTED_AFTER_PROVIDER_IO
            )
        self.operation = AtomicShadowLaunchOperation(
            operation_id=operation_id,
            operation_request_hash=operation_request_hash,
            exclusions=exclusions,
            state=AtomicShadowLaunchState.RUNNING,
            created_now=True,
        )
        return self.operation

    def complete_atomic_no_plan_operation(
        self,
        *,
        command: DiscoverAndCreateShadowEvidencePlanCommand,
        operation_id: str,
        operation_request_hash: str,
        exclusions: AtomicShadowPlanExclusions,
        state: AtomicShadowLaunchState,
        summary: AtomicShadowPlanLaunchSummary,
    ) -> AtomicShadowLaunchOperation:
        assert state in {
            AtomicShadowLaunchState.NO_PLAN,
            AtomicShadowLaunchState.FAILED_AFTER_PROVIDER_IO,
        }
        assert self.operation is not None
        summary_dict = summary.as_dict()
        self.operation = AtomicShadowLaunchOperation(
            operation_id=operation_id,
            operation_request_hash=operation_request_hash,
            exclusions=exclusions,
            state=state,
            created_now=False,
            result_payload={
                "summary": summary_dict,
                "result_hash": content_digest(summary_dict),
            },
        )
        return self.operation

    def plan_for_atomic_launch(
        self, *, campaign_id: str, idempotency_key: str, exclusion_configuration_hash: str
    ) -> dict[str, object] | None:
        if self.plan is None:
            return None
        if self.configuration_hash != exclusion_configuration_hash:
            raise RuntimeError("atomic shadow evidence plan identity conflicts")
        return self.plan

    def resolve_atomic_launch_exclusions(
        self,
        *,
        campaign_id: str,
        prior_plan_ids: tuple[str, ...],
        exclude_prior_plan_events: bool,
        exclude_active_plan_markets: bool,
    ) -> AtomicShadowPlanExclusions:
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.exclusions

    def create_plan(
        self, command: CreateShadowEvidencePlanCommand
    ) -> tuple[dict[str, object], bool]:
        self.created = command
        self.request_hash = command.atomic_operation_request_hash
        self.configuration_hash = command.atomic_exclusion_configuration_hash
        self.plan = {
            "plan_id": "atomic-plan-1",
            "campaign_id": command.campaign_id,
            "state": "CREATED",
            "atomic_operation_request_hash": command.atomic_operation_request_hash,
            "atomic_exclusions": command.atomic_exclusions,
        }
        if command.atomic_terminal_summary is not None:
            summary = command.atomic_terminal_summary.as_dict()
            self.operation = AtomicShadowLaunchOperation(
                operation_id=command.atomic_terminal_summary.operation_id,
                operation_request_hash=command.atomic_terminal_summary.operation_request_hash,
                exclusions=cast(AtomicShadowPlanExclusions, command.atomic_exclusions),
                state=AtomicShadowLaunchState.CREATED_PLAN,
                created_now=False,
                result_payload={"summary": summary, "result_hash": content_digest(summary)},
                plan=self.plan,
            )
        return self.plan, False


def _atomic_command(
    *, timestamp: datetime, **changes: object
) -> DiscoverAndCreateShadowEvidencePlanCommand:
    values: dict[str, object] = {
        "campaign_id": "atomic-shadow-campaign",
        "name": "Atomic forward evidence",
        "window_start": timestamp + timedelta(hours=1),
        "window_end": timestamp + timedelta(days=10),
        "maximum_pages": 3,
        "maximum_inspected_markets": 20,
        "maximum_enrichments": 5,
        "target_markets": 3,
        "minimum_markets": 3,
        "minimum_events": 3,
        "maximum_markets_per_event": 1,
        "maximum_markets_per_series": 3,
        "iterations": 3,
        "interval_seconds": 30,
        "orderbook_depth": 5,
        "idempotency_key": "atomic-shadow-plan-v1",
        "decision_at": timestamp,
    }
    values.update(changes)
    return DiscoverAndCreateShadowEvidencePlanCommand(**values)  # type: ignore[arg-type]


def _candidate(ordinal: int, timestamp: datetime) -> CaptureMarketCandidate:
    return CaptureMarketCandidate(
        market_snapshot_id=f"list-snapshot-{ordinal}",
        ingestion_run_id="discovery-run",
        provider_market_ticker=f"KX-ATOMIC-{ordinal}",
        provider_event_ticker=f"KX-EVENT-{ordinal}",
        status="open",
        observed_at=timestamp,
        close_time=timestamp + timedelta(days=2),
        is_provisional=False,
        rule_content_hash=content_digest({"rule": ordinal}),
        data_quality_state="ELIGIBLE",
        volume_24h=Decimal(10 - ordinal),
    )


def _exact(candidate: CaptureMarketCandidate, timestamp: datetime) -> ExactShadowCohortEnrichment:
    def digest(label: str) -> str:
        return content_digest({"label": label, "ticker": candidate.provider_market_ticker})

    return ExactShadowCohortEnrichment(
        list_market_snapshot_id=candidate.market_snapshot_id,
        market_snapshot_id=f"exact-{candidate.market_snapshot_id}",
        event_snapshot_id=f"event-{candidate.provider_event_ticker}",
        event_raw_response_id=f"event-raw-{candidate.provider_market_ticker}",
        series_raw_response_id=f"series-raw-{candidate.provider_market_ticker}",
        exact_market_raw_response_id=f"market-raw-{candidate.provider_market_ticker}",
        fee_snapshot_id=f"fee-{candidate.provider_market_ticker}",
        provider="KALSHI",
        market_ticker=candidate.provider_market_ticker,
        event_ticker=candidate.provider_event_ticker,
        series_ticker=f"KX-SERIES-{candidate.provider_event_ticker}",
        rule_content_hash=digest("rule"),
        settlement_source_identity=digest("settlement-source"),
        lifecycle_state="open",
        binary_structure_identity=digest("binary"),
        observed_at=timestamp,
        available_at=timestamp,
        close_at=timestamp + timedelta(days=2),
        market_lineage_hash=digest("market"),
        event_lineage_hash=digest("event"),
        series_lineage_hash=digest("series"),
        fee_lineage_hash=digest("fee"),
        enriched_at=timestamp,
    )


def _v2_preflight_exact(
    candidate: CaptureMarketCandidate,
    timestamp: datetime,
    *,
    yes_quantity: Decimal = Decimal("2"),
    no_quantity: Decimal = Decimal("2"),
    yes_bid: Decimal = Decimal("0.45"),
    no_bid: Decimal = Decimal("0.45"),
    include_no_bid: bool = True,
    fee_type: str = KALSHI_PUBLISHED_FEE_SCHEDULE.provider_fee_type,
) -> ExactShadowCohortEnrichment:
    exact = _exact(candidate, timestamp)

    def digest(label: str) -> ContentHash:
        return ContentHash(
            content_digest({"label": label, "ticker": candidate.provider_market_ticker})
        )

    orderbook = PublicOrderbookSnapshot(
        orderbook_snapshot_id=f"book-{candidate.provider_market_ticker}",
        ingestion_run_id="discovery-run",
        raw_response_id=f"book-raw-{candidate.provider_market_ticker}",
        market_snapshot_id=exact.market_snapshot_id,
        provider=PublicMarketProvider.KALSHI,
        provider_market_ticker=exact.market_ticker,
        provider_event_ticker=exact.event_ticker,
        series_ticker=exact.series_ticker,
        observed_at=timestamp,
        maximum_depth=5,
        yes_bid_level_count=1,
        no_bid_level_count=int(include_no_bid),
        data_quality_state="VALID",
        canonical_source_hash=digest("book"),
    )
    levels: list[PublicOrderbookLevel] = [
        PublicOrderbookLevel(
            level_id=f"yes-{candidate.provider_market_ticker}",
            orderbook_snapshot_id=orderbook.orderbook_snapshot_id,
            side=OrderbookSide.YES_BID,
            ordinal=0,
            bid_price=yes_bid,
            quantity=yes_quantity,
            order_count=1,
            source_hash=digest("yes"),
        )
    ]
    if include_no_bid:
        levels.append(
            PublicOrderbookLevel(
                level_id=f"no-{candidate.provider_market_ticker}",
                orderbook_snapshot_id=orderbook.orderbook_snapshot_id,
                side=OrderbookSide.NO_BID,
                ordinal=0,
                bid_price=no_bid,
                quantity=no_quantity,
                order_count=1,
                source_hash=digest("no"),
            )
        )
    fee = SeriesFeeSnapshot(
        fee_snapshot_id=f"fee-{candidate.provider_market_ticker}",
        ingestion_run_id="discovery-run",
        raw_response_id=f"fee-raw-{candidate.provider_market_ticker}",
        provider=PublicMarketProvider.KALSHI,
        series_ticker=exact.series_ticker,
        observed_at=timestamp,
        fee_type=fee_type,
        fee_multiplier=Decimal("1"),
        effective_at=timestamp,
        fee_model_identity=FEE_MODEL_IDENTITY,
        taker_coefficient=TAKER_COEFFICIENT,
        maker_coefficient=MAKER_COEFFICIENT,
        rounding_identity=ROUNDING_IDENTITY,
        schedule_identity=SCHEDULE_IDENTITY,
        schedule_effective_date=SCHEDULE_EFFECTIVE_DATE,
        source_hash=digest("fee"),
    )
    return replace(
        exact,
        binary_structure_identity=content_digest(
            {"market_snapshot_id": exact.market_snapshot_id, "structure": "BINARY_YES_NO"}
        ),
        preflight_orderbook=orderbook,
        preflight_levels=tuple(levels),
        preflight_fee=fee,
    )


def test_atomic_launch_uses_one_exact_enrichment_pass_and_freezes_its_values() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    calls = {"discover": 0, "enrich": 0}
    candidates = tuple(_candidate(ordinal, now) for ordinal in range(3))

    def discover(_request: object) -> PublicMarketScanSummary:
        calls["discover"] += 1
        return PublicMarketScanSummary(
            ingestion_run_id="discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=2,
            market_count=20,
            eligible_count=3,
            ineligible_count=17,
            invalid_count=0,
            idempotent_replay=False,
        )

    def enrich(
        candidate: CaptureMarketCandidate, _operation_id: str
    ) -> ExactShadowCohortEnrichmentAttempt:
        calls["enrich"] += 1
        return ExactShadowCohortEnrichmentAttempt(
            selected=_exact(candidate, now),
            reason_codes=(),
            provider_get_count=3,
            event_enrichment_succeeded=True,
            series_enrichment_succeeded=True,
            exact_market_refresh_succeeded=True,
        )

    result = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=discover,
        select_candidates=lambda _run_id, _limit: candidates,
        enrich_candidate=enrich,
    ).run(_atomic_command(timestamp=now))

    assert result.plan is not None
    assert calls == {"discover": 1, "enrich": 3}
    assert store.created is not None
    assert store.created.requested_market_tickers == ()
    assert len(store.created.validated_cohort_candidates) == 3
    assert result.summary.provider_get_count == 11
    assert result.summary.provider_order_count == 0
    assert result.summary.financial_state_before == result.summary.financial_state_after == "NONE"


def test_atomic_launch_ranks_retained_list_quote_quality_before_exact_enrichment() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    candidates = (
        replace(_candidate(0, now), yes_bid=Decimal("0.20"), yes_ask=Decimal("0.90")),
        replace(
            _candidate(1, now),
            yes_bid=Decimal("0.49"),
            yes_ask=Decimal("0.51"),
            no_bid=Decimal("0.49"),
            no_ask=Decimal("0.51"),
        ),
        replace(
            _candidate(2, now),
            yes_bid=Decimal("0.10"),
            yes_ask=Decimal("0.90"),
            no_bid=Decimal("0.10"),
            no_ask=Decimal("0.90"),
        ),
    )
    enriched: list[str] = []

    def enrich(
        candidate: CaptureMarketCandidate, _operation_id: str
    ) -> ExactShadowCohortEnrichmentAttempt:
        enriched.append(candidate.provider_market_ticker)
        return ExactShadowCohortEnrichmentAttempt(
            selected=_exact(candidate, now),
            reason_codes=(),
            provider_get_count=3,
            event_enrichment_succeeded=True,
            series_enrichment_succeeded=True,
            exact_market_refresh_succeeded=True,
        )

    result = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: PublicMarketScanSummary(
            ingestion_run_id="discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=3,
            eligible_count=3,
            ineligible_count=0,
            invalid_count=0,
            idempotent_replay=False,
        ),
        select_candidates=lambda _run_id, _limit: candidates,
        enrich_candidate=enrich,
    ).run(_atomic_command(timestamp=now))

    assert result.plan is not None
    assert enriched == ["KX-ATOMIC-1", "KX-ATOMIC-2", "KX-ATOMIC-0"]
    assert result.summary.list_two_sided_quote_candidates == 2
    assert result.summary.list_incomplete_quote_candidates == 1
    assert dict(result.summary.list_spread_buckets) == {
        "ABOVE_MAXIMUM": 1,
        "AT_OR_BELOW_MAXIMUM": 1,
        "MISSING": 1,
    }


def test_atomic_ranking_does_not_spend_enrichment_budget_on_early_event_round_robin() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    command = _atomic_command(timestamp=now)
    candidates = (
        replace(
            _candidate(0, now),
            provider_event_ticker="KX-SHARED-EVENT",
            yes_bid=Decimal("0.49"),
            yes_ask=Decimal("0.51"),
            no_bid=Decimal("0.49"),
            no_ask=Decimal("0.51"),
        ),
        replace(
            _candidate(1, now),
            provider_event_ticker="KX-SHARED-EVENT",
            yes_bid=Decimal("0.40"),
            yes_ask=Decimal("0.60"),
            no_bid=Decimal("0.40"),
            no_ask=Decimal("0.60"),
        ),
        replace(
            _candidate(2, now),
            provider_event_ticker="KX-OTHER-EVENT",
            yes_bid=Decimal("0.10"),
            yes_ask=Decimal("0.90"),
            no_bid=Decimal("0.10"),
            no_ask=Decimal("0.90"),
        ),
    )

    ranked = DiscoverAndCreateShadowEvidencePlan._ranked_candidates(
        candidates=candidates,
        command=command,
    )

    assert [candidate.provider_market_ticker for candidate in ranked] == [
        "KX-ATOMIC-0",
        "KX-ATOMIC-1",
        "KX-ATOMIC-2",
    ]


def test_atomic_v2_launch_binds_the_requested_fee_configuration_and_one_preflight_per_market() -> (
    None
):
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    candidates = tuple(_candidate(ordinal, now) for ordinal in range(3))
    calls = {"enrichment": 0, "preflight": 0}

    def enrich(
        candidate: CaptureMarketCandidate, _operation_id: str
    ) -> ExactShadowCohortEnrichmentAttempt:
        calls["enrichment"] += 1
        calls["preflight"] += 1
        return ExactShadowCohortEnrichmentAttempt(
            selected=_v2_preflight_exact(candidate, now),
            reason_codes=(),
            provider_get_count=4,
            event_enrichment_succeeded=True,
            series_enrichment_succeeded=True,
            exact_market_refresh_succeeded=True,
            orderbook_preflight_attempted=True,
        )

    result = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: PublicMarketScanSummary(
            ingestion_run_id="discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=20,
            eligible_count=3,
            ineligible_count=17,
            invalid_count=0,
            idempotent_replay=False,
        ),
        select_candidates=lambda _run_id, _limit: candidates,
        enrich_candidate=enrich,
    ).run(
        _atomic_command(
            timestamp=now,
            maximum_orderbook_preflights=3,
            microstructure_configuration_version=PUBLISHED_FEE_CONFIGURATION_VERSION,
        )
    )

    assert result.plan is not None, result.summary.as_dict()
    assert calls == {"enrichment": 3, "preflight": 3}
    assert result.summary.orderbook_preflights_attempted == 3
    assert result.summary.fee_compatible_candidates == 3
    assert result.summary.two_sided_books == 3
    assert result.summary.spread_compatible_books == 3
    assert result.summary.visible_quantity_compatible_books == 3
    assert result.summary.schedule_time_compatible_books == 3
    assert result.summary.provider_order_count == 0
    assert store.created is not None
    micro_binding = store.created.configuration.strategy_configuration_bindings[1]
    assert micro_binding.configuration_version == PUBLISHED_FEE_CONFIGURATION_VERSION
    assert micro_binding.payload["published_fee_source_sha256"] == KALSHI_FEE_SCHEDULE_SOURCE_SHA256


def test_atomic_v2_proves_quadratic_alias_without_bypassing_exact_orderbook() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    candidates = tuple(
        replace(
            _candidate(ordinal, now),
            yes_bid=Decimal("0.45"),
            yes_ask=Decimal("0.55"),
            no_bid=Decimal("0.45"),
            no_ask=Decimal("0.55"),
        )
        for ordinal in range(3)
    )
    exacts = (
        _v2_preflight_exact(candidates[0], now, fee_type="quadratic"),
        _v2_preflight_exact(candidates[1], now, fee_type="quadratic"),
        _v2_preflight_exact(
            candidates[2],
            now,
            yes_bid=Decimal("0.10"),
            no_bid=Decimal("0.60"),
            fee_type="quadratic",
        ),
    )

    result = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: PublicMarketScanSummary(
            ingestion_run_id="discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=3,
            eligible_count=3,
            ineligible_count=0,
            invalid_count=0,
            idempotent_replay=False,
        ),
        select_candidates=lambda _run_id, _limit: candidates,
        enrich_candidate=lambda candidate, _operation_id: ExactShadowCohortEnrichmentAttempt(
            selected=exacts[int(candidate.provider_market_ticker.rsplit("-", 1)[1])],
            reason_codes=(),
            provider_get_count=4,
            event_enrichment_succeeded=True,
            series_enrichment_succeeded=True,
            exact_market_refresh_succeeded=True,
            orderbook_preflight_attempted=True,
        ),
    ).run(
        _atomic_command(
            timestamp=now,
            maximum_orderbook_preflights=3,
            microstructure_configuration_version=PUBLISHED_FEE_CONFIGURATION_VERSION,
        )
    )

    assert result.plan is None
    assert dict(result.summary.fee_applicability_states) == {"API_LABEL_ALIAS_PROVEN": 3}
    assert result.summary.fee_compatible_candidates == 3
    assert result.summary.fee_and_spread_compatible_candidates == 2
    assert result.summary.list_exact_spread_disagreement_count == 1
    assert dict(result.summary.enrichment_failures)["SPREAD_TOO_WIDE"] == 1


def test_atomic_v2_launch_reports_one_sided_wide_and_thin_preflight_rejections() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    candidates = tuple(_candidate(ordinal, now) for ordinal in range(3))
    preflights = (
        _v2_preflight_exact(candidates[0], now, include_no_bid=False),
        _v2_preflight_exact(candidates[1], now, yes_bid=Decimal("0.10"), no_bid=Decimal("0.60")),
        _v2_preflight_exact(candidates[2], now, yes_quantity=Decimal("0.5")),
    )

    result = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: PublicMarketScanSummary(
            ingestion_run_id="discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=20,
            eligible_count=3,
            ineligible_count=17,
            invalid_count=0,
            idempotent_replay=False,
        ),
        select_candidates=lambda _run_id, _limit: candidates,
        enrich_candidate=lambda candidate, _operation_id: ExactShadowCohortEnrichmentAttempt(
            selected=preflights[int(candidate.provider_market_ticker.rsplit("-", 1)[1])],
            reason_codes=(),
            provider_get_count=4,
            event_enrichment_succeeded=True,
            series_enrichment_succeeded=True,
            exact_market_refresh_succeeded=True,
            orderbook_preflight_attempted=True,
        ),
    ).run(
        _atomic_command(
            timestamp=now,
            maximum_orderbook_preflights=3,
            microstructure_configuration_version=PUBLISHED_FEE_CONFIGURATION_VERSION,
        )
    )

    assert result.plan is None
    assert result.summary.no_plan_reason == "MINIMUM_MARKET_COUNT_NOT_MET"
    assert result.summary.orderbook_preflights_attempted == 3
    assert result.summary.one_sided_books == 1
    assert dict(result.summary.enrichment_failures)["ONE_SIDED_BOOK"] == 1
    assert dict(result.summary.enrichment_failures)["SPREAD_TOO_WIDE"] == 1
    assert dict(result.summary.enrichment_failures)["INSUFFICIENT_VISIBLE_QUANTITY"] == 1


def test_atomic_launch_returns_a_complete_no_plan_summary_when_exact_validation_fails() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    candidate = _candidate(1, now)
    result = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: PublicMarketScanSummary(
            ingestion_run_id="discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=20,
            eligible_count=1,
            ineligible_count=19,
            invalid_count=0,
            idempotent_replay=False,
        ),
        select_candidates=lambda _run_id, _limit: (candidate,),
        enrich_candidate=lambda _candidate, _operation_id: ExactShadowCohortEnrichmentAttempt(
            selected=None,
            reason_codes=("EVENT_MARKET_LINEAGE_MISMATCH",),
            provider_get_count=1,
            event_enrichment_succeeded=False,
            series_enrichment_succeeded=False,
            exact_market_refresh_succeeded=False,
        ),
    ).run(_atomic_command(timestamp=now))

    assert result.plan is None
    assert result.summary.no_plan_reason == "MINIMUM_MARKET_COUNT_NOT_MET"
    assert result.summary.enrichment_failures == (("EVENT_MARKET_LINEAGE_MISMATCH", 1),)
    assert result.summary.provider_order_count == 0
    assert store.created is None


def test_atomic_no_plan_replay_uses_the_retained_terminal_result_without_provider_io() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    calls = {"discover": 0, "enrich": 0}
    command = _atomic_command(timestamp=now)

    def discover(_request: object) -> PublicMarketScanSummary:
        calls["discover"] += 1
        return PublicMarketScanSummary(
            ingestion_run_id="replay-discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=20,
            eligible_count=1,
            ineligible_count=19,
            invalid_count=0,
            idempotent_replay=False,
        )

    def enrich(
        _candidate: CaptureMarketCandidate, _operation_id: str
    ) -> ExactShadowCohortEnrichmentAttempt:
        calls["enrich"] += 1
        return ExactShadowCohortEnrichmentAttempt(
            selected=None,
            reason_codes=("EVENT_MARKET_LINEAGE_MISMATCH",),
            provider_get_count=1,
            event_enrichment_succeeded=False,
            series_enrichment_succeeded=False,
            exact_market_refresh_succeeded=False,
        )

    service = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=discover,
        select_candidates=lambda _run_id, _limit: (_candidate(1, now),),
        enrich_candidate=enrich,
    )

    first = service.run(command)
    replay = service.run(command)

    assert first.state is AtomicShadowLaunchState.NO_PLAN
    assert replay.state is AtomicShadowLaunchState.NO_PLAN
    assert first.result_hash == replay.result_hash
    assert replay.summary.idempotent_replay is True
    assert replay.summary.no_plan_reason == first.summary.no_plan_reason
    assert calls == {"discover": 1, "enrich": 1}
    assert store.created is None


def test_atomic_created_plan_replay_returns_the_same_terminal_result_without_provider_io() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    calls = {"discover": 0, "enrich": 0}
    candidates = tuple(_candidate(ordinal, now) for ordinal in range(3))
    command = _atomic_command(timestamp=now)

    def discover(_request: object) -> PublicMarketScanSummary:
        calls["discover"] += 1
        return PublicMarketScanSummary(
            ingestion_run_id="created-replay-discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=20,
            eligible_count=3,
            ineligible_count=17,
            invalid_count=0,
            idempotent_replay=False,
        )

    def enrich(
        candidate: CaptureMarketCandidate, _operation_id: str
    ) -> ExactShadowCohortEnrichmentAttempt:
        calls["enrich"] += 1
        return ExactShadowCohortEnrichmentAttempt(
            selected=_exact(candidate, now),
            reason_codes=(),
            provider_get_count=3,
            event_enrichment_succeeded=True,
            series_enrichment_succeeded=True,
            exact_market_refresh_succeeded=True,
        )

    service = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=discover,
        select_candidates=lambda _run_id, _limit: candidates,
        enrich_candidate=enrich,
    )

    first = service.run(command)
    replay = service.run(command)

    assert first.state is AtomicShadowLaunchState.CREATED_PLAN
    assert replay.state is AtomicShadowLaunchState.CREATED_PLAN
    assert replay.plan == first.plan
    assert replay.summary.idempotent_replay is True
    assert first.result_hash == replay.result_hash
    assert calls == {"discover": 1, "enrich": 3}


def test_atomic_provider_failure_is_retained_as_a_terminal_failure() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    command = _atomic_command(timestamp=now)
    service = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
        select_candidates=lambda _run_id, _limit: (),
        enrich_candidate=lambda _candidate, _operation_id: (_ for _ in ()).throw(
            AssertionError("enrichment must not run")
        ),
    )

    result = service.run(command)
    replay = service.run(command)

    assert result.state is AtomicShadowLaunchState.FAILED_AFTER_PROVIDER_IO
    assert result.summary.no_plan_reason == "PROVIDER_DISCOVERY_FAILED"
    assert replay.summary.idempotent_replay is True
    assert replay.result_hash == result.result_hash


def test_atomic_launch_replay_and_conflict_happen_before_provider_calls() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    command = _atomic_command(timestamp=now)
    store.configuration_hash = command.exclusion_configuration_hash
    store.request_hash = command.operation_request_hash(store.exclusions)
    store.plan = {
        "plan_id": "replayed-plan",
        "state": "CREATED",
        "atomic_operation_request_hash": store.request_hash,
        "atomic_exclusions": store.exclusions,
    }
    calls = 0
    service = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: (_ for _ in ()).throw(AssertionError("provider call")),
        select_candidates=lambda _run_id, _limit: (),
        enrich_candidate=lambda _candidate, _operation_id: ExactShadowCohortEnrichmentAttempt(
            selected=None,
            reason_codes=("SHOULD_NOT_RUN",),
            provider_get_count=1,
            event_enrichment_succeeded=False,
            series_enrichment_succeeded=False,
            exact_market_refresh_succeeded=False,
        ),
    )

    with pytest.raises(AtomicShadowLaunchLifecycleError, match="INTERRUPTED_AFTER_PROVIDER_IO"):
        service.run(command)

    assert calls == 0
    with pytest.raises(RuntimeError, match="identity conflicts"):
        service.run(_atomic_command(timestamp=now, name="different operation"))


def test_atomic_launch_rejects_contradictory_bounds_before_provider_calls() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueValidationError, match="minimum markets"):
        _atomic_command(timestamp=now, minimum_markets=4, target_markets=3)
    with pytest.raises(ValueValidationError, match="at most 30 days"):
        _atomic_command(timestamp=now, iterations=288, interval_seconds=86400)
    with pytest.raises(ValueValidationError, match="mandatory"):
        _atomic_command(
            timestamp=now,
            prior_plan_ids=("prior-plan",),
            exclude_prior_plan_markets=False,
        )


def test_atomic_launch_excludes_prior_and_active_cohorts_before_and_after_enrichment() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    store.exclusions = AtomicShadowPlanExclusions(
        supplied_prior_plan_ids=("prior-plan",),
        resolved_prior_plan_ids=("prior-plan",),
        prior_plan_market_tickers=("KX-ATOMIC-0",),
        prior_plan_event_tickers=("KX-EVENT-1",),
        active_plan_market_tickers=("KX-ATOMIC-2",),
        exclude_prior_plan_events=True,
    )
    candidates = tuple(_candidate(ordinal, now) for ordinal in range(8))
    enriched: list[str] = []

    def enrich(
        candidate: CaptureMarketCandidate, _operation_id: str
    ) -> ExactShadowCohortEnrichmentAttempt:
        enriched.append(candidate.provider_market_ticker)
        exact = _exact(candidate, now)
        ordinal = int(candidate.provider_market_ticker.rsplit("-", 1)[1])
        if ordinal == 3:
            exact = replace(exact, market_ticker="KX-ATOMIC-0")
        if ordinal == 4:
            exact = replace(exact, event_ticker="KX-EVENT-1")
        return ExactShadowCohortEnrichmentAttempt(
            selected=exact,
            reason_codes=(),
            provider_get_count=3,
            event_enrichment_succeeded=True,
            series_enrichment_succeeded=True,
            exact_market_refresh_succeeded=True,
        )

    result = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: PublicMarketScanSummary(
            ingestion_run_id="discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=20,
            eligible_count=8,
            ineligible_count=12,
            invalid_count=0,
            idempotent_replay=False,
        ),
        select_candidates=lambda _run_id, _limit: candidates,
        enrich_candidate=enrich,
    ).run(
        _atomic_command(
            timestamp=now,
            maximum_enrichments=5,
            prior_plan_ids=("prior-plan",),
            exclude_prior_plan_events=True,
        )
    )

    assert result.plan is not None
    assert enriched == ["KX-ATOMIC-3", "KX-ATOMIC-4", "KX-ATOMIC-5", "KX-ATOMIC-6", "KX-ATOMIC-7"]
    assert result.summary.candidates_excluded_before_enrichment_by_market == 2
    assert result.summary.candidates_excluded_before_enrichment_by_event == 1
    assert result.summary.candidates_excluded_after_enrichment_by_market == 1
    assert result.summary.candidates_excluded_after_enrichment_by_event == 1
    assert result.summary.selected_markets_overlapping_prior_plans == 0
    assert result.summary.selected_events_overlapping_prior_plans == 0
    assert result.summary.selected_markets_overlapping_active_plans == 0
    assert result.summary.provider_order_count == 0


def test_atomic_exclusion_resolution_failure_happens_before_provider_calls() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    store.resolve_error = ValueValidationError("atomic exclusion prior plan was not found")
    service = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: (_ for _ in ()).throw(AssertionError("provider call")),
        select_candidates=lambda _run_id, _limit: (),
        enrich_candidate=lambda _candidate, _operation_id: (_ for _ in ()).throw(
            AssertionError("enrichment call")
        ),
    )

    with pytest.raises(ValueValidationError, match="not found"):
        service.run(_atomic_command(timestamp=now, prior_plan_ids=("missing-plan",)))


def test_atomic_exclusion_configuration_changes_conflict_before_provider_calls() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    command = _atomic_command(timestamp=now, prior_plan_ids=("prior-plan",))
    store.configuration_hash = command.exclusion_configuration_hash
    store.request_hash = command.operation_request_hash(store.exclusions)
    store.plan = {
        "plan_id": "replayed-plan",
        "state": "CREATED",
        "atomic_operation_request_hash": store.request_hash,
        "atomic_exclusions": store.exclusions,
    }
    service = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: (_ for _ in ()).throw(AssertionError("provider call")),
        select_candidates=lambda _run_id, _limit: (),
        enrich_candidate=lambda _candidate, _operation_id: (_ for _ in ()).throw(
            AssertionError("enrichment call")
        ),
    )

    with pytest.raises(RuntimeError, match="identity conflicts"):
        service.run(
            _atomic_command(
                timestamp=now,
                prior_plan_ids=("different-prior-plan",),
                exclude_prior_plan_events=True,
            )
        )


def test_atomic_launch_groups_failures_and_enforces_event_and_series_caps() -> None:
    now = datetime(2031, 1, 1, tzinfo=UTC)
    store = _AtomicLaunchStore()
    candidates = tuple(_candidate(ordinal, now) for ordinal in range(6))
    event_series = (
        ("EVENT-0", "SERIES-0"),
        ("EVENT-1", "SERIES-0"),
        ("EVENT-2", "SERIES-0"),
        ("EVENT-3", "SERIES-1"),
        ("EVENT-4", "SERIES-1"),
        ("EVENT-5", "SERIES-2"),
    )

    def enrich(
        candidate: CaptureMarketCandidate, _operation_id: str
    ) -> ExactShadowCohortEnrichmentAttempt:
        event_ticker, series_ticker = event_series[
            int(candidate.provider_market_ticker.rsplit("-", 1)[1])
        ]
        return ExactShadowCohortEnrichmentAttempt(
            selected=replace(
                _exact(candidate, now), event_ticker=event_ticker, series_ticker=series_ticker
            ),
            reason_codes=(),
            provider_get_count=3,
            event_enrichment_succeeded=True,
            series_enrichment_succeeded=True,
            exact_market_refresh_succeeded=True,
        )

    result = DiscoverAndCreateShadowEvidencePlan(
        store=store,
        discover=lambda _request: PublicMarketScanSummary(
            ingestion_run_id="discovery-run",
            state=PublicMarketIngestionState.COMPLETED,
            page_count=1,
            market_count=20,
            eligible_count=6,
            ineligible_count=14,
            invalid_count=0,
            idempotent_replay=False,
        ),
        select_candidates=lambda _run_id, _limit: candidates,
        enrich_candidate=enrich,
    ).run(
        _atomic_command(
            timestamp=now,
            target_markets=5,
            maximum_enrichments=6,
            maximum_markets_per_event=1,
            maximum_markets_per_series=2,
        )
    )

    assert result.plan is not None
    assert result.summary.selected_count == 5
    assert result.summary.selected_event_count == 5
    assert result.summary.series_cap_exclusions >= 1
    assert len(store.created.validated_cohort_candidates) == 5  # type: ignore[union-attr]
