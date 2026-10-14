from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.domain.public_complement import (
    FEE_MODEL_IDENTITY,
    MAKER_COEFFICIENT,
    ROUNDING_IDENTITY,
    SCHEDULE_EFFECTIVE_DATE,
    SCHEDULE_IDENTITY,
    TAKER_COEFFICIENT,
    SeriesFeeSnapshot,
)
from apex.domain.public_event import EventEvidenceState, EventRelationshipState
from apex.domain.public_exhaustive import (
    KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1,
    ExhaustiveEventSetInput,
    ExhaustiveEventSetLegInput,
    evaluate_exhaustive_event_set,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import ContentHash

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
HASH = ContentHash("a" * 64)


def _fee(ordinal: int) -> SeriesFeeSnapshot:
    return SeriesFeeSnapshot(
        fee_snapshot_id=f"fee-{ordinal}",
        ingestion_run_id="bounded-scan",
        raw_response_id=f"fee-raw-{ordinal}",
        provider=PublicMarketProvider.KALSHI,
        series_ticker="KXCOMPLETE",
        observed_at=NOW - timedelta(seconds=10),
        fee_type="quadratic",
        fee_multiplier=Decimal("1"),
        effective_at=None,
        fee_model_identity=FEE_MODEL_IDENTITY,
        taker_coefficient=TAKER_COEFFICIENT,
        maker_coefficient=MAKER_COEFFICIENT,
        rounding_identity=ROUNDING_IDENTITY,
        schedule_identity=SCHEDULE_IDENTITY,
        schedule_effective_date=SCHEDULE_EFFECTIVE_DATE,
        source_hash=HASH,
    )


def _input() -> ExhaustiveEventSetInput:
    legs = tuple(
        ExhaustiveEventSetLegInput(
            market_snapshot_id=f"market-{ordinal}",
            market_ticker=f"KXCOMPLETE-{ordinal}",
            event_ticker="KXCOMPLETE",
            relationship_id=f"relationship-{ordinal}",
            relationship_state=EventRelationshipState.EXACT,
            rule_content_hash=str(HASH),
            rules_primary=f"Outcome {ordinal} resolves YES exactly when selected.",
            rules_secondary=None,
            market_status="open",
            close_at=NOW + timedelta(hours=1),
            settlement_at=NOW + timedelta(hours=2),
            orderbook_snapshot_id=f"book-{ordinal}",
            orderbook_hash=str(HASH),
            orderbook_observed_at=NOW - timedelta(seconds=10),
            orderbook_quality="VALID",
            yes_ask=Decimal("0.25"),
            visible_yes_quantity=Decimal("5"),
            fee=_fee(ordinal),
        )
        for ordinal in range(1, 4)
    )
    return ExhaustiveEventSetInput(
        scan_id="bounded-scan",
        event_ticker="KXCOMPLETE",
        event_snapshot_ids=("event-1",),
        event_snapshot_hashes=(str(HASH),),
        mutually_exclusive_values=(True,),
        exhaustiveness_values=(EventEvidenceState.VERIFIED,),
        nested_market_counts=(3,),
        settlement_source_hashes=(str(HASH),),
        legs=legs,
    )


def test_verified_complete_event_set_has_exact_model_free_economics_and_no_authority() -> None:
    first = evaluate_exhaustive_event_set(_input(), evaluated_at=NOW)
    replay = evaluate_exhaustive_event_set(_input(), evaluated_at=NOW)

    assert first == replay
    assert first.source_eligible
    assert first.rejection_reasons == ()
    assert first.matched_quantity == Decimal("5")
    assert first.gross_package_cost == Decimal("3.75")
    assert first.guaranteed_settlement_payout == Decimal("5")
    assert first.total_modeled_cost > first.gross_package_cost
    assert first.conservative_net_gap == (
        first.guaranteed_settlement_payout - first.total_modeled_cost
    )
    assert first.required_capital == first.maximum_loss == first.total_modeled_cost
    assert first.maximum_payoff == first.guaranteed_settlement_payout
    assert first.authority == "NONE"
    assert not first.paper_eligible
    assert first.live_authority == "NONE"
    assert KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1 in (
        KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1,
    )


def test_complete_set_fails_closed_on_unverified_coverage_ambiguous_rules_and_stale_book() -> None:
    value = _input()
    ambiguous = replace(
        value.legs[0],
        rules_secondary="Market may be void if none of the above occurs.",
        orderbook_observed_at=NOW - timedelta(minutes=10),
    )
    result = evaluate_exhaustive_event_set(
        replace(
            value,
            mutually_exclusive_values=(False,),
            exhaustiveness_values=(EventEvidenceState.UNKNOWN,),
            nested_market_counts=(4,),
            legs=(ambiguous, *value.legs[1:]),
        ),
        evaluated_at=NOW,
    )

    assert not result.source_eligible
    assert result.matched_quantity == 0
    assert {
        "MUTUAL_EXCLUSIVITY_NOT_VERIFIED",
        "COLLECTIVE_EXHAUSTIVENESS_NOT_VERIFIED",
        "EVENT_SET_COVERAGE_INCOMPLETE",
        "AMBIGUOUS_VOID_CANCELLATION_CONDITIONAL_OR_OTHER_RULE",
        "ORDERBOOK_EVIDENCE_STALE",
        "NO_MATCHED_VISIBLE_PACKAGE_QUANTITY",
    }.issubset(result.rejection_reasons)


def test_complete_set_rejects_nonpositive_net_gap_after_exact_fee_and_rounding() -> None:
    value = _input()
    expensive = tuple(replace(leg, yes_ask=Decimal("0.34")) for leg in value.legs)
    result = evaluate_exhaustive_event_set(
        replace(value, legs=expensive),
        evaluated_at=NOW,
    )

    assert not result.source_eligible
    assert result.gross_package_cost == Decimal("5.10")
    assert result.conservative_net_gap < 0
    assert "CONSERVATIVE_NET_GAP_NOT_POSITIVE" in result.rejection_reasons
