"""Synthetic, no-network completed scans for temporal comparison checks."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from apex.domain.public_comparison import (
    EventScanObservation,
    MarketScanObservation,
    PersistedPublicScan,
)
from apex.domain.public_event import (
    ConsistencyEvaluation,
    ConsistencyEvaluationState,
    EventEvidenceState,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash, content_digest


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _evaluation(
    *,
    event_snapshot_id: str,
    state: ConsistencyEvaluationState,
    yes_ask_sum: str | None,
    coverage: int = 2,
    missing: int = 0,
) -> ConsistencyEvaluation:
    value = Decimal(yes_ask_sum) if yes_ask_sum is not None else None
    return ConsistencyEvaluation(
        evaluation_id=f"fixture-evaluation-{event_snapshot_id}",
        event_snapshot_id=event_snapshot_id,
        evaluator_version="fixture-v1",
        evaluated_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        mutually_exclusive_evidence=EventEvidenceState.VERIFIED,
        exhaustiveness_evidence=EventEvidenceState.VERIFIED,
        quote_coverage_count=coverage,
        missing_quote_count=missing,
        yes_ask_sum=value,
        yes_bid_sum=value,
        no_ask_sum=value,
        no_bid_sum=value,
        minimum_visible_executable_size=Decimal("10") if missing == 0 else None,
        settlement_source_compatibility=EventEvidenceState.VERIFIED,
        rule_compatibility=EventEvidenceState.VERIFIED,
        close_time_compatibility=EventEvidenceState.VERIFIED,
        state=state,
        reason_codes=(),
        canonical_input_hash=_hash((event_snapshot_id, state, value, coverage, missing)),
    )


def _market(
    *,
    run: str,
    ticker: str,
    observed_at: datetime,
    yes_bid: str = "0.48",
    yes_ask: str | None = "0.50",
    rule: str = "rule-v1",
    status: str = "open",
) -> MarketScanObservation:
    return MarketScanObservation(
        snapshot_id=f"fixture-market-{run}-{ticker}",
        provider=PublicMarketProvider.KALSHI,
        provider_market_ticker=ticker,
        observed_at=observed_at,
        status=status,
        rule_content_hash=_hash(rule),
        yes_bid=Decimal(yes_bid),
        yes_ask=Decimal(yes_ask) if yes_ask is not None else None,
        no_bid=Decimal("0.50"),
        no_ask=Decimal("0.52"),
        yes_bid_size=Decimal("10"),
        yes_ask_size=Decimal("10"),
        volume=Decimal("100"),
        open_interest=Decimal("25"),
    )


def _event(
    *,
    run: str,
    ticker: str,
    observed_at: datetime,
    state: ConsistencyEvaluationState,
    yes_ask_sum: str | None,
    source: str = "source-v1",
    rule: str = "event-rule-v1",
    missing: int = 0,
) -> EventScanObservation:
    snapshot_id = f"fixture-event-{run}-{ticker}"
    return EventScanObservation(
        event_snapshot_id=snapshot_id,
        provider=PublicMarketProvider.KALSHI,
        provider_event_ticker=ticker,
        observed_at=observed_at,
        settlement_source_hash=_hash(source),
        relationship_evidence_hash=_hash(rule),
        evaluation=_evaluation(
            event_snapshot_id=snapshot_id,
            state=state,
            yes_ask_sum=yes_ask_sum,
            missing=missing,
        ),
    )


def temporal_fixture_scans() -> tuple[PersistedPublicScan, PersistedPublicScan]:
    """Return two non-overlapping synthetic scans covering temporal edge cases."""

    baseline_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
    current_at = datetime(2026, 7, 20, 12, 10, tzinfo=UTC)
    baseline = PersistedPublicScan(
        ingestion_run_id="fixture-market-baseline",
        provider=PublicMarketProvider.KALSHI,
        state="COMPLETED",
        observed_start_at=baseline_at,
        observed_end_at=baseline_at,
        markets=(
            _market(run="baseline", ticker="UNCHANGED", observed_at=baseline_at),
            _market(run="baseline", ticker="WIDENED", observed_at=baseline_at),
            _market(
                run="baseline",
                ticker="TIGHTENED",
                observed_at=baseline_at,
                yes_bid="0.45",
                yes_ask="0.55",
            ),
            _market(
                run="baseline", ticker="MISSING_TO_AVAILABLE", observed_at=baseline_at, yes_ask=None
            ),
            _market(run="baseline", ticker="AVAILABLE_TO_MISSING", observed_at=baseline_at),
            _market(run="baseline", ticker="RULE_CHANGED", observed_at=baseline_at),
            _market(run="baseline", ticker="REMOVED", observed_at=baseline_at),
        ),
        events=(
            _event(
                run="baseline",
                ticker="PERSIST",
                observed_at=baseline_at,
                state=ConsistencyEvaluationState.CANDIDATE,
                yes_ask_sum="0.90",
            ),
            _event(
                run="baseline",
                ticker="RESOLVED",
                observed_at=baseline_at,
                state=ConsistencyEvaluationState.CANDIDATE,
                yes_ask_sum="0.90",
            ),
            _event(
                run="baseline",
                ticker="INELIGIBLE",
                observed_at=baseline_at,
                state=ConsistencyEvaluationState.CANDIDATE,
                yes_ask_sum="0.90",
            ),
            _event(
                run="baseline",
                ticker="INVALID",
                observed_at=baseline_at,
                state=ConsistencyEvaluationState.CANDIDATE,
                yes_ask_sum="0.90",
            ),
            _event(
                run="baseline",
                ticker="SOURCE_CHANGED",
                observed_at=baseline_at,
                state=ConsistencyEvaluationState.CANDIDATE,
                yes_ask_sum="0.90",
            ),
        ),
    )
    current = PersistedPublicScan(
        ingestion_run_id="fixture-market-current",
        provider=PublicMarketProvider.KALSHI,
        state="COMPLETED",
        observed_start_at=current_at,
        observed_end_at=current_at,
        markets=(
            _market(run="current", ticker="UNCHANGED", observed_at=current_at),
            _market(
                run="current",
                ticker="WIDENED",
                observed_at=current_at,
                yes_bid="0.46",
                yes_ask="0.55",
            ),
            _market(
                run="current",
                ticker="TIGHTENED",
                observed_at=current_at,
                yes_bid="0.48",
                yes_ask="0.50",
            ),
            _market(run="current", ticker="MISSING_TO_AVAILABLE", observed_at=current_at),
            _market(
                run="current", ticker="AVAILABLE_TO_MISSING", observed_at=current_at, yes_ask=None
            ),
            _market(run="current", ticker="RULE_CHANGED", observed_at=current_at, rule="rule-v2"),
            _market(run="current", ticker="ADDED", observed_at=current_at),
        ),
        events=(
            _event(
                run="current",
                ticker="PERSIST",
                observed_at=current_at,
                state=ConsistencyEvaluationState.CANDIDATE,
                yes_ask_sum="0.88",
            ),
            _event(
                run="current",
                ticker="NEW",
                observed_at=current_at,
                state=ConsistencyEvaluationState.CANDIDATE,
                yes_ask_sum="0.91",
            ),
            _event(
                run="current",
                ticker="RESOLVED",
                observed_at=current_at,
                state=ConsistencyEvaluationState.CONSISTENT,
                yes_ask_sum="1.00",
            ),
            _event(
                run="current",
                ticker="INELIGIBLE",
                observed_at=current_at,
                state=ConsistencyEvaluationState.INELIGIBLE,
                yes_ask_sum=None,
                missing=1,
            ),
            _event(
                run="current",
                ticker="INVALID",
                observed_at=current_at,
                state=ConsistencyEvaluationState.INVALID,
                yes_ask_sum=None,
            ),
            _event(
                run="current",
                ticker="SOURCE_CHANGED",
                observed_at=current_at,
                state=ConsistencyEvaluationState.CANDIDATE,
                yes_ask_sum="0.89",
                source="source-v2",
            ),
        ),
    )
    return baseline, current
