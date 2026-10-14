"""Orchestration-level proofs for the genuine multi-asset registry
(milestone section 14/15) against in-memory fakes -- no network, no DB."""

from __future__ import annotations

from datetime import UTC, datetime

from apex.application.genuine_multi_asset_history import (
    GenuineMultiAssetRegistry,
    InMemoryGenuineMultiAssetHistoryRepository,
    resolve_genuine_multi_asset_registry,
)
from apex.application.historical_market_data import InMemoryHistoricalMarketDataRepository
from apex.domain.cross_market_dataset import CrossMarketDataset
from apex.domain.genuine_data_quality_gate import GenuineCohortQualityGateReport
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
from apex.domain.research_cohort import GENUINE_MULTI_ASSET_COHORT_POLICY_V1, ResearchCohortPolicy

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def _small_policy() -> ResearchCohortPolicy:
    from apex.domain.research_cohort import CohortRole, CohortSector, CohortSecurityDeclaration

    member = CohortSecurityDeclaration(
        symbol="ZZZ", sector=CohortSector.TECHNOLOGY, roles=(CohortRole.LARGE_CAP_EQUITY,), note="x"
    )
    return ResearchCohortPolicy.create(
        version="TEST_V1", members=(member,), selection_methodology="m", rationale="r"
    )


def _seeded_historical_repository(
    policy: ResearchCohortPolicy,
) -> tuple[InMemoryHistoricalMarketDataRepository, HistoricalDatasetSnapshot]:
    repository = InMemoryHistoricalMarketDataRepository()
    capture_policy = HistoricalCapturePolicy.create(
        provider="ALPACA_MARKET_DATA_V2",
        start_date="2020-07-27",
        end_date="2026-08-06",
        chunk_size=10,
        maximum_instruments=10,
        maximum_requests=10,
        maximum_bytes=1_000_000,
    )
    universe = (
        HistoricalCaptureUniverseMember.create(
            stable_security_id="stable_ZZZ", symbol_at_capture="ZZZ", asset_class="COMMON_STOCK"
        ),
    )
    plan = HistoricalCapturePlan.draft(
        policy=capture_policy,
        universe=universe,
        evidence_class="GENUINE_PUBLIC",
        operator_action_id="test-plan",
        idempotency_key=f"genuine-multi-asset-cohort-v1:{policy.policy_id}",
        created_at=NOW,
    )
    repository.save_plan(plan)
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=("batch-1",),
        source_operation_ids=("op-1",),
        observation_count=100,
        completeness_state=HistoricalDatasetCompletenessState.COMPLETE,
        earliest_observation_date="2020-07-27",
        latest_observation_date="2026-08-06",
        created_at=NOW,
    )
    repository.save_snapshot(snapshot)
    return repository, snapshot


def test_registry_resolves_empty_when_no_plan_exists_yet() -> None:
    policy = _small_policy()
    registry = resolve_genuine_multi_asset_registry(
        cohort_policy=policy,
        historical_repository=InMemoryHistoricalMarketDataRepository(),
        genuine_repository=InMemoryGenuineMultiAssetHistoryRepository(),
    )
    assert registry.cohort_policy_id == policy.policy_id
    assert registry.cohort_member_count == 1
    assert registry.historical_capture_plan_id is None
    assert registry.equity_dataset_snapshot_id is None
    assert registry.replay_run_ids == ()


def test_registry_resolves_plan_and_snapshot_by_deterministic_idempotency_key() -> None:
    policy = _small_policy()
    historical_repository, snapshot = _seeded_historical_repository(policy)
    registry = resolve_genuine_multi_asset_registry(
        cohort_policy=policy,
        historical_repository=historical_repository,
        genuine_repository=InMemoryGenuineMultiAssetHistoryRepository(),
    )
    assert registry.equity_dataset_snapshot_id == snapshot.snapshot_id
    assert registry.equity_coverage_start == "2020-07-27"
    assert registry.equity_coverage_end == "2026-08-06"


def test_registry_scopes_quality_gate_and_cross_market_dataset_to_this_cohort_policy() -> None:
    policy = _small_policy()
    other_policy_id = "some-other-cohort-policy"
    historical_repository, snapshot = _seeded_historical_repository(policy)
    genuine_repository = InMemoryGenuineMultiAssetHistoryRepository()

    matching_report = GenuineCohortQualityGateReport.build(
        snapshot_id=snapshot.snapshot_id,
        cohort_policy_id=policy.policy_id,
        requested_symbols=("ZZZ",),
        assessments=(),
        point_in_time_limitations=(),
        source_hashes=(),
        created_at=NOW,
    )
    other_report = GenuineCohortQualityGateReport.build(
        snapshot_id=snapshot.snapshot_id,
        cohort_policy_id=other_policy_id,
        requested_symbols=("YYY",),
        assessments=(),
        point_in_time_limitations=(),
        source_hashes=(),
        created_at=NOW,
    )
    genuine_repository.save_quality_gate_report(matching_report)
    genuine_repository.save_quality_gate_report(other_report)

    matching_dataset = CrossMarketDataset.create(
        equity_dataset_snapshot_id=snapshot.snapshot_id,
        equity_dataset_snapshot_hash=snapshot.canonical_hash,
        equity_coverage_start="2020-07-27",
        equity_coverage_end="2026-08-06",
        cohort_policy_id=policy.policy_id,
        cohort_policy_hash=policy.canonical_hash,
        quality_gate_report_id=matching_report.report_id,
        quality_gate_report_hash=matching_report.canonical_hash,
        crypto_provider="COINBASE",
        crypto_product_id="BTC-USD",
        crypto_capture_ids=("btc-a",),
        crypto_coverage_start="2020-07-01",
        crypto_coverage_end="2026-06-30",
        tmc_component_symbols=("SPY", "TLT", "USO"),
        tmc_source_identity="HISTORICAL_REPLAY_RETAINED",
        adjustment_policy_id="ALPACA_IEX_RAW_NO_ADJUSTMENT_V1",
        missing_data_policy_id="FAIL_CLOSED_NO_FORWARD_FILL_V1",
        alignment_policy_id="CROSS_MARKET_ALIGNMENT_V1",
        point_in_time_limitations=(),
        created_at=NOW,
    )
    genuine_repository.save_cross_market_dataset(matching_dataset)

    registry = resolve_genuine_multi_asset_registry(
        cohort_policy=policy,
        historical_repository=historical_repository,
        genuine_repository=genuine_repository,
    )
    assert registry.quality_gate_report_id == matching_report.report_id
    assert registry.cross_market_dataset_id == matching_dataset.cross_market_dataset_id
    assert registry.common_overlap_start == "2020-07-27"


def test_registry_authority_is_always_none() -> None:
    registry = GenuineMultiAssetRegistry(
        cohort_policy_id="p",
        cohort_policy_version="v",
        cohort_member_count=1,
        historical_capture_plan_id=None,
        equity_dataset_snapshot_id=None,
        equity_coverage_start=None,
        equity_coverage_end=None,
        historical_data_quality_report_id=None,
        quality_gate_report_id=None,
        quality_gate_usable_count=None,
        quality_gate_usable_with_warnings_count=None,
        quality_gate_insufficient_count=None,
        quality_gate_rejected_count=None,
        cross_market_dataset_id=None,
        crypto_capture_ids=(),
        common_overlap_start=None,
        common_overlap_end=None,
        tmc_component_symbols=(),
        replay_run_ids=(),
        governance_policy_ids=(),
        conclusion_ids=(),
        promotion_evaluation_ids=(),
        walk_forward_run_ids=(),
        robustness_run_ids=(),
        trial_family_ids=(),
    )
    assert registry.authority == "NONE"


def test_genuine_cohort_v1_policy_id_is_reachable_from_the_real_module_constant() -> None:
    # A cheap guard against accidentally shadowing/renaming the real policy
    # this milestone's genuine capture was actually run against.
    assert GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id.startswith("research_cohort_policy_")
