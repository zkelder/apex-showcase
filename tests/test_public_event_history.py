from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from apex.adapters.kalshi_history_fixtures import temporal_history_fixture_runs
from apex.application.build_event_observation_history import (
    BuildEventObservationHistory,
    EventHistoryRequest,
    EventObservationHistoryError,
    InMemoryEventObservationHistoryStore,
)
from apex.domain.public_history import (
    EventHistoryContinuityState,
    EventHistoryObservationState,
)


def _service():
    runs = temporal_history_fixture_runs()
    return BuildEventObservationHistory(store=InMemoryEventObservationHistoryStore(runs)), runs


def test_exact_event_fixture_retains_absence_breaks_and_candidate_streaks() -> None:
    service, _ = _service()

    report = service.build(
        EventHistoryRequest(provider_event_ticker="HISTORY-EVENT", maximum_completed_runs=9)
    )
    analysis = report.analysis
    observations = report.observations

    assert analysis.included_completed_run_count == 9
    assert analysis.present_observation_count == 8
    assert analysis.absent_observation_count == 1
    assert analysis.candidate_observation_count == 6
    assert analysis.consistent_observation_count == 1
    assert analysis.ineligible_observation_count == 1
    assert analysis.evidence_break_count == 1
    assert analysis.current_candidate_streak_observation_count == 1
    assert analysis.longest_candidate_streak_observation_count == 3
    assert analysis.longest_candidate_streak_duration == timedelta(minutes=10)
    assert analysis.current_candidate_streak_duration == timedelta(0)
    assert analysis.maximum_observation_gap == timedelta(minutes=45)
    assert analysis.current_consistency_deviation == Decimal("0.14")
    assert analysis.maximum_compatible_consistency_deviation == Decimal("0.14")
    assert analysis.minimum_visible_size_current_streak == Decimal("14")
    assert observations[0].state is EventHistoryObservationState.ABSENT
    assert observations[5].continuity_state is EventHistoryContinuityState.EVIDENCE_BREAK
    assert "SETTLEMENT_SOURCE_CHANGED" in observations[5].reason_codes
    assert observations[8].continuity_state is EventHistoryContinuityState.OBSERVATION_GAP
    assert observations[8].comparison_transition_id == "fixture-history-transition-8"


def test_history_is_idempotent_and_remains_exact_ticker_only() -> None:
    service, _ = _service()
    request = EventHistoryRequest(provider_event_ticker="HISTORY-EVENT", maximum_completed_runs=9)

    first = service.build(request)
    replay = service.build(request)

    assert replay.idempotent_replay
    assert replay.analysis == first.analysis
    with pytest.raises(EventObservationHistoryError, match="exact requested ticker"):
        service.build(
            EventHistoryRequest(
                provider_event_ticker="OTHER-EVENT",
                maximum_completed_runs=9,
            )
        )


def test_rule_relationship_and_gap_break_candidate_continuity() -> None:
    _, runs = _service()
    changed = list(runs)
    evidence = changed[3].evidence
    assert evidence is not None
    changed[3] = replace(
        changed[3],
        evidence=replace(evidence, rule_evidence_hash=evidence.relationship_evidence_hash),
    )
    report = BuildEventObservationHistory(
        store=InMemoryEventObservationHistoryStore(tuple(changed))
    ).build(EventHistoryRequest(provider_event_ticker="HISTORY-EVENT", maximum_completed_runs=9))

    assert report.observations[3].continuity_state is EventHistoryContinuityState.EVIDENCE_BREAK
    assert "RULE_EVIDENCE_CHANGED" in report.observations[3].reason_codes
    assert report.analysis.longest_candidate_streak_observation_count == 2


def test_invalid_bounds_or_reversed_observation_order_create_no_history() -> None:
    _, runs = _service()
    store = InMemoryEventObservationHistoryStore(runs)
    service = BuildEventObservationHistory(store=store)
    with pytest.raises(EventObservationHistoryError, match="three completed"):
        service.build(
            EventHistoryRequest(
                provider_event_ticker="HISTORY-EVENT",
                first_ingestion_run_id="not-a-run",
                maximum_completed_runs=9,
            )
        )

    bad = list(runs)
    bad[4] = replace(bad[4], observed_start_at=bad[3].observed_end_at)
    with pytest.raises(EventObservationHistoryError, match="ordering"):
        BuildEventObservationHistory(store=InMemoryEventObservationHistoryStore(tuple(bad))).build(
            EventHistoryRequest(provider_event_ticker="HISTORY-EVENT", maximum_completed_runs=9)
        )
