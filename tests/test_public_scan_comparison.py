from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from apex.adapters.kalshi_comparison_fixtures import temporal_fixture_scans
from apex.application.compare_public_scans import (
    ComparePublicMarketScans,
    InMemoryPublicScanComparisonStore,
    PublicScanComparisonError,
)
from apex.domain.public_comparison import (
    EventConsistencyTransitionState,
    EvidenceCompatibilityState,
    MarketSnapshotDeltaState,
)


def _service():
    baseline, current = temporal_fixture_scans()
    return (
        ComparePublicMarketScans(store=InMemoryPublicScanComparisonStore((baseline, current))),
        baseline,
        current,
    )


def test_fixture_comparison_uses_exact_tickers_and_preserves_missing_values() -> None:
    service, baseline, current = _service()

    report = service.compare(
        baseline_ingestion_run_id=baseline.ingestion_run_id,
        current_ingestion_run_id=current.ingestion_run_id,
    )

    values = {value.provider_market_ticker: value for value in report.market_deltas}
    assert report.comparison.matched_market_count == 6
    assert values["ADDED"].state is MarketSnapshotDeltaState.ADDED
    assert values["REMOVED"].state is MarketSnapshotDeltaState.REMOVED
    assert values["UNCHANGED"].state is MarketSnapshotDeltaState.UNCHANGED
    assert values["WIDENED"].yes_spread_delta == Decimal("0.07")
    assert values["TIGHTENED"].yes_spread_delta == Decimal("-0.08")
    assert values["MISSING_TO_AVAILABLE"].yes_ask_delta is None
    assert values["AVAILABLE_TO_MISSING"].yes_ask_delta is None
    assert values["RULE_CHANGED"].rule_compatibility is EvidenceCompatibilityState.CHANGED
    assert values["RULE_CHANGED"].yes_bid_delta is None


def test_candidate_transition_states_and_ranking_are_deterministic() -> None:
    service, baseline, current = _service()

    first = service.compare(
        baseline_ingestion_run_id=baseline.ingestion_run_id,
        current_ingestion_run_id=current.ingestion_run_id,
    )
    replay = service.compare(
        baseline_ingestion_run_id=baseline.ingestion_run_id,
        current_ingestion_run_id=current.ingestion_run_id,
    )

    transitions = {value.provider_event_ticker: value for value in first.event_transitions}
    assert transitions["PERSIST"].state is EventConsistencyTransitionState.PERSISTING_CANDIDATE
    assert transitions["NEW"].state is EventConsistencyTransitionState.NEW_CANDIDATE
    assert transitions["RESOLVED"].state is EventConsistencyTransitionState.RESOLVED_CANDIDATE
    assert transitions["INELIGIBLE"].state is EventConsistencyTransitionState.BECAME_INELIGIBLE
    assert transitions["INVALID"].state is EventConsistencyTransitionState.BECAME_INVALID
    assert transitions["SOURCE_CHANGED"].state is EventConsistencyTransitionState.NEW_CANDIDATE
    assert transitions["SOURCE_CHANGED"].settlement_source_compatibility is (
        EvidenceCompatibilityState.CHANGED
    )
    assert tuple(value.provider_event_ticker for value in first.ranked_priorities) == (
        "PERSIST",
        "SOURCE_CHANGED",
        "NEW",
    )
    assert replay.idempotent_replay
    assert replay.comparison == first.comparison


def test_reversed_or_overlapping_scan_order_is_rejected_without_comparison() -> None:
    service, baseline, current = _service()

    with pytest.raises(PublicScanComparisonError, match="precede"):
        service.compare(
            baseline_ingestion_run_id=current.ingestion_run_id,
            current_ingestion_run_id=baseline.ingestion_run_id,
        )

    overlapping_current = replace(
        current,
        ingestion_run_id="fixture-market-overlap",
        observed_start_at=baseline.observed_end_at,
        observed_end_at=baseline.observed_end_at + timedelta(minutes=1),
    )
    overlapping = ComparePublicMarketScans(
        store=InMemoryPublicScanComparisonStore((baseline, overlapping_current))
    )
    with pytest.raises(PublicScanComparisonError, match="precede"):
        overlapping.compare(
            baseline_ingestion_run_id=baseline.ingestion_run_id,
            current_ingestion_run_id=overlapping_current.ingestion_run_id,
        )
