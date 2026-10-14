"""Synthetic, redistributable temporal evidence for orderbook-history tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from apex.domain.public_complement import (
    FEE_MODEL_IDENTITY,
    MAKER_COEFFICIENT,
    ROUNDING_IDENTITY,
    SCHEDULE_EFFECTIVE_DATE,
    SCHEDULE_IDENTITY,
    TAKER_COEFFICIENT,
    BinaryComplementEvaluation,
    ComplementReasonCode,
    ComplementState,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.public_orderbook_history import OrderbookHistoryEvidence
from apex.domain.values import ContentHash, content_digest

MARKET_TICKER = "ORDERBOOK-HISTORY-MARKET"
SERIES_TICKER = "ORDERBOOK-HISTORY-SERIES"
BASE_TIME = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _evidence(
    ordinal: int,
    *,
    at: datetime,
    state: ComplementState,
    quantity: str,
    lower_bound: str,
    multiplier: str = "1",
    schedule: str = SCHEDULE_IDENTITY,
    rule: str = "rule-v1",
) -> OrderbookHistoryEvidence:
    book_id = f"fixture-history-orderbook-{ordinal}"
    fee = SeriesFeeSnapshot(
        fee_snapshot_id=f"fixture-history-fee-{ordinal}",
        ingestion_run_id=f"fixture-history-run-{ordinal}",
        raw_response_id=f"fixture-history-fee-raw-{ordinal}",
        provider=PublicMarketProvider.KALSHI,
        series_ticker=SERIES_TICKER,
        observed_at=at,
        fee_type="quadratic",
        fee_multiplier=Decimal(multiplier),
        effective_at=at,
        fee_model_identity=FEE_MODEL_IDENTITY,
        taker_coefficient=TAKER_COEFFICIENT,
        maker_coefficient=MAKER_COEFFICIENT,
        rounding_identity=ROUNDING_IDENTITY,
        schedule_identity=schedule,
        schedule_effective_date=SCHEDULE_EFFECTIVE_DATE,
        source_hash=_hash(("fee", ordinal)),
    )
    orderbook = PublicOrderbookSnapshot(
        orderbook_snapshot_id=book_id,
        ingestion_run_id=f"fixture-history-run-{ordinal}",
        raw_response_id=f"fixture-history-orderbook-raw-{ordinal}",
        market_snapshot_id=f"fixture-history-market-{ordinal}",
        provider=PublicMarketProvider.KALSHI,
        provider_market_ticker=MARKET_TICKER,
        provider_event_ticker="ORDERBOOK-HISTORY-EVENT",
        series_ticker=SERIES_TICKER,
        observed_at=at,
        maximum_depth=5,
        yes_bid_level_count=1,
        no_bid_level_count=1,
        data_quality_state="VALID" if state is not ComplementState.INVALID else "INVALID",
        canonical_source_hash=_hash(("book", ordinal)),
    )
    matched = Decimal(quantity)
    lower = Decimal(lower_bound)
    yes_fee = matched * Decimal("0.01744")
    no_fee = matched * Decimal("0.01744")
    modeled_fees = yes_fee + no_fee
    rounding_reserve = matched * Decimal("0.00112")
    gross_cost = matched - lower - modeled_fees - rounding_reserve
    candidate = state is ComplementState.CANDIDATE
    reason: tuple[ComplementReasonCode, ...]
    if candidate:
        reason = ()
    elif state is ComplementState.NO_CANDIDATE:
        reason = (ComplementReasonCode.FEES_ELIMINATE_GROSS_GAP,)
    else:
        reason = (ComplementReasonCode.ORDERBOOK_MISSING_SIDE,)
    evaluation = BinaryComplementEvaluation(
        evaluation_id=f"fixture-history-evaluation-{ordinal}",
        ingestion_run_id=orderbook.ingestion_run_id,
        orderbook_snapshot_id=book_id,
        market_snapshot_id=orderbook.market_snapshot_id,
        fee_snapshot_id=fee.fee_snapshot_id,
        provider=PublicMarketProvider.KALSHI,
        provider_market_ticker=MARKET_TICKER,
        evaluator_version="public-binary-complement-v1",
        evaluated_at=at,
        maximum_depth=5,
        maximum_bundle_quantity=Decimal("20"),
        rounding_identity=ROUNDING_IDENTITY,
        matched_quantity=matched,
        weighted_yes_ask=(Decimal("0.47") if matched else None),
        weighted_no_ask=(Decimal("0.47") if matched else None),
        gross_acquisition_cost=gross_cost,
        gross_settlement_value=matched,
        gross_complement_gap=matched - gross_cost,
        yes_modeled_taker_fee=yes_fee,
        no_modeled_taker_fee=no_fee,
        modeled_taker_fees=modeled_fees,
        conservative_rounding_reserve=rounding_reserve,
        total_modeled_acquisition_cost=matched - lower,
        conservative_net_lower_bound=lower,
        net_lower_bound_per_contract=(
            (lower / matched).quantize(Decimal("0.00000001"), rounding=ROUND_FLOOR)
            if matched
            else None
        ),
        state=state,
        reason_codes=reason,
        canonical_input_hash=_hash(("evaluation", ordinal, state)),
    )
    return OrderbookHistoryEvidence(
        orderbook=orderbook,
        evaluation=evaluation,
        fee=fee,
        rule_evidence_hash=_hash(rule),
    )


def temporal_orderbook_history_fixture() -> tuple[OrderbookHistoryEvidence, ...]:
    """Ten point-in-time observations covering continuity and evidence failures."""

    return (
        _evidence(
            0, at=BASE_TIME, state=ComplementState.NO_CANDIDATE, quantity="0", lower_bound="0"
        ),
        _evidence(
            1,
            at=BASE_TIME + timedelta(minutes=1),
            state=ComplementState.CANDIDATE,
            quantity="10",
            lower_bound="0.24",
        ),
        _evidence(
            2,
            at=BASE_TIME + timedelta(minutes=2),
            state=ComplementState.CANDIDATE,
            quantity="12",
            lower_bound="0.30",
        ),
        _evidence(
            3,
            at=BASE_TIME + timedelta(minutes=3),
            state=ComplementState.CANDIDATE,
            quantity="12",
            lower_bound="0.15",
        ),
        _evidence(
            4,
            at=BASE_TIME + timedelta(minutes=4),
            state=ComplementState.NO_CANDIDATE,
            quantity="0",
            lower_bound="0",
        ),
        _evidence(
            5,
            at=BASE_TIME + timedelta(minutes=5),
            state=ComplementState.CANDIDATE,
            quantity="8",
            lower_bound="0.12",
        ),
        _evidence(
            6,
            at=BASE_TIME + timedelta(minutes=12),
            state=ComplementState.CANDIDATE,
            quantity="8",
            lower_bound="0.11",
        ),
        _evidence(
            7,
            at=BASE_TIME + timedelta(minutes=13),
            state=ComplementState.CANDIDATE,
            quantity="9",
            lower_bound="0.14",
            multiplier="2",
        ),
        _evidence(
            8,
            at=BASE_TIME + timedelta(minutes=14),
            state=ComplementState.CANDIDATE,
            quantity="9",
            lower_bound="0.13",
            multiplier="2",
        ),
        _evidence(
            9,
            at=BASE_TIME + timedelta(minutes=15),
            state=ComplementState.INVALID,
            quantity="0",
            lower_bound="0",
            multiplier="2",
        ),
    )
