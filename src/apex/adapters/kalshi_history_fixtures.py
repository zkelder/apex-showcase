"""Synthetic no-network exact-event observations for retained-history checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.domain.public_event import (
    ConsistencyEvaluation,
    ConsistencyEvaluationState,
    EventEvidenceState,
)
from apex.domain.public_history import EventHistoryEvidence, EventHistoryRun
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, content_digest


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _evidence(
    *,
    ordinal: int,
    state: ConsistencyEvaluationState,
    yes_ask_sum: str | None,
    coverage: int,
    size: str | None,
    rule: str = "rule-v1",
    source: str = "source-v1",
    relationship: str = "relationship-v1",
    evaluator_version: str = "fixture-v1",
    observed_at: datetime | None = None,
) -> EventHistoryEvidence:
    observed_at = observed_at or (
        datetime(2026, 7, 20, 12, 0, tzinfo=UTC) + timedelta(minutes=5 * ordinal)
    )
    snapshot_id = f"fixture-history-event-{ordinal}"
    value = Decimal(yes_ask_sum) if yes_ask_sum is not None else None
    evaluation = ConsistencyEvaluation(
        evaluation_id=f"fixture-history-evaluation-{ordinal}",
        event_snapshot_id=snapshot_id,
        evaluator_version=evaluator_version,
        evaluated_at=observed_at,
        mutually_exclusive_evidence=EventEvidenceState.VERIFIED,
        exhaustiveness_evidence=EventEvidenceState.VERIFIED,
        quote_coverage_count=coverage,
        missing_quote_count=0 if yes_ask_sum is not None else 1,
        yes_ask_sum=value,
        yes_bid_sum=value,
        no_ask_sum=value,
        no_bid_sum=value,
        minimum_visible_executable_size=Decimal(size) if size is not None else None,
        settlement_source_compatibility=EventEvidenceState.VERIFIED,
        rule_compatibility=EventEvidenceState.VERIFIED,
        close_time_compatibility=EventEvidenceState.VERIFIED,
        state=state,
        reason_codes=(),
        canonical_input_hash=_hash((ordinal, state, value, coverage, size, rule, source)),
    )
    return EventHistoryEvidence(
        event_snapshot_id=snapshot_id,
        provider=PublicMarketProvider.KALSHI,
        provider_event_ticker="HISTORY-EVENT",
        observed_at=observed_at,
        settlement_source_hash=_hash(source),
        rule_evidence_hash=_hash(rule),
        relationship_evidence_hash=_hash(relationship),
        evaluation=evaluation,
    )


def temporal_history_fixture_runs() -> tuple[EventHistoryRun, ...]:
    """Nine bounded runs with absence, evidence change, and an observation gap."""

    base = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    event_by_ordinal = {
        1: _evidence(
            ordinal=1,
            state=ConsistencyEvaluationState.INELIGIBLE,
            yes_ask_sum=None,
            coverage=1,
            size=None,
        ),
        2: _evidence(
            ordinal=2,
            state=ConsistencyEvaluationState.CANDIDATE,
            yes_ask_sum="0.90",
            coverage=2,
            size="10",
        ),
        3: _evidence(
            ordinal=3,
            state=ConsistencyEvaluationState.CANDIDATE,
            yes_ask_sum="0.88",
            coverage=3,
            size="8",
        ),
        4: _evidence(
            ordinal=4,
            state=ConsistencyEvaluationState.CANDIDATE,
            yes_ask_sum="0.92",
            coverage=4,
            size="12",
        ),
        5: _evidence(
            ordinal=5,
            state=ConsistencyEvaluationState.CANDIDATE,
            yes_ask_sum="0.89",
            coverage=4,
            size="11",
            source="source-v2",
        ),
        6: _evidence(
            ordinal=6,
            state=ConsistencyEvaluationState.CANDIDATE,
            yes_ask_sum="0.87",
            coverage=4,
            size="13",
            source="source-v2",
        ),
        7: _evidence(
            ordinal=7,
            state=ConsistencyEvaluationState.CONSISTENT,
            yes_ask_sum="1.00",
            coverage=4,
            size="13",
            source="source-v2",
        ),
        8: _evidence(
            ordinal=8,
            state=ConsistencyEvaluationState.CANDIDATE,
            yes_ask_sum="0.86",
            coverage=4,
            size="14",
            source="source-v2",
            observed_at=base + timedelta(minutes=80),
        ),
    }
    values: list[EventHistoryRun] = []
    for ordinal in range(9):
        start = base + timedelta(minutes=5 * ordinal)
        if ordinal == 8:
            start += timedelta(minutes=40)
        values.append(
            EventHistoryRun(
                ingestion_run_id=f"fixture-history-run-{ordinal}",
                provider=PublicMarketProvider.KALSHI,
                state="COMPLETED",
                observed_start_at=start,
                observed_end_at=start,
                evidence=event_by_ordinal.get(ordinal),
                comparison_transition_id=(
                    f"fixture-history-transition-{ordinal}" if ordinal > 0 else None
                ),
            )
        )
    return tuple(values)
