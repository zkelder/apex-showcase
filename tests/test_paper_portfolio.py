from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.decisions import Side
from apex.domain.paper import (
    PaperExecutionEvidence,
    PaperExecutionModel,
    PaperExecutionPolicy,
    PaperFillState,
    PaperOrderbookLevel,
    PaperUnfilledBehavior,
    evaluate_conservative_mark,
    evaluate_immediate_cross,
)
from apex.domain.values import ContentHash, Money, Price, Quantity, RiskFraction, content_digest

NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)


def _hash(value: object) -> ContentHash:
    return ContentHash(content_digest(value))


def _policy(**overrides: object) -> PaperExecutionPolicy:
    values: dict[str, object] = {
        "policy_version_id": "paper-policy-v1",
        "execution_model": PaperExecutionModel.IMMEDIATE_CROSS_V1,
        "maximum_evidence_age_seconds": 30,
        "maximum_spread": Decimal("0.02"),
        "maximum_orderbook_depth": 2,
        "maximum_visible_size_participation": RiskFraction("0.5"),
        "slippage_reserve_per_contract": Money("0.01"),
        "fee_model_version": "paper-fee-v1",
        "time_in_force_seconds": 30,
        "unfilled_behavior": PaperUnfilledBehavior.EXPIRE,
        "configuration_version": "v1",
        "configuration_hash": _hash("paper-policy"),
    }
    values.update(overrides)
    return PaperExecutionPolicy(**values)  # type: ignore[arg-type]


def _level(price: str, quantity: str) -> PaperOrderbookLevel:
    return PaperOrderbookLevel(Price(price, "0.01"), Quantity(quantity, "1"))


def _evidence(**overrides: object) -> PaperExecutionEvidence:
    values: dict[str, object] = {
        "evidence_id": "paper-evidence-v1",
        "provider": "FIXTURE",
        "provider_market_ticker": "fixture-market",
        "provider_series_ticker": "fixture-series",
        "market_observation_source_id": "fixture-market-source",
        "instrument_id": "fixture-instrument",
        "rule_version_id": "fixture-rule-v1",
        "rule_content_hash": _hash("rule"),
        "fee_model_version": "paper-fee-v1",
        "observed_at": NOW - timedelta(seconds=1),
        "available_at": NOW,
        "yes_asks": (_level("0.50", "2"), _level("0.51", "2")),
        "no_asks": (_level("0.50", "2"),),
        "yes_bids": (_level("0.49", "4"),),
        "no_bids": (_level("0.49", "2"),),
        "evidence_references": ("fixture-evidence",),
        "content_hash": _hash("paper-evidence"),
    }
    values.update(overrides)
    return PaperExecutionEvidence(**values)  # type: ignore[arg-type]


def test_immediate_cross_caps_displayed_depth_and_participation_exactly() -> None:
    result = evaluate_immediate_cross(
        policy=_policy(),
        evidence=_evidence(),
        side=Side.YES,
        requested_quantity=Quantity("4", "1"),
        limit_price=Price("0.51", "0.01"),
        maximum_loss_per_unit=Money("1"),
        fee_per_unit=Money("0.02"),
        cycle_at=NOW,
    )

    assert result.state is PaperFillState.PARTIAL_FILL
    assert result.filled_quantity == Quantity("2", "1")
    assert result.unfilled_quantity == Quantity("2", "1")
    assert result.consideration == Money("1")
    assert result.fee == Money("0.04")
    assert result.slippage_reserve == Money("0.02")
    assert result.remaining_reservation_risk == Money("2")


def test_multilevel_average_that_cannot_be_one_tick_fill_fails_closed() -> None:
    result = evaluate_immediate_cross(
        policy=_policy(maximum_visible_size_participation=RiskFraction("1")),
        evidence=_evidence(),
        side=Side.YES,
        requested_quantity=Quantity("3", "1"),
        limit_price=Price("0.51", "0.01"),
        maximum_loss_per_unit=Money("1"),
        fee_per_unit=Money("0"),
        cycle_at=NOW,
    )

    assert result.state is PaperFillState.REJECTED
    assert result.filled_quantity == Quantity("0", "1")
    assert result.reason_codes == ("PAPER_VWAP_TICK_UNREPRESENTABLE",)


def test_stale_or_missing_bid_evidence_rejects_without_a_fill() -> None:
    stale = evaluate_immediate_cross(
        policy=_policy(),
        evidence=_evidence(
            observed_at=NOW - timedelta(seconds=32),
            available_at=NOW - timedelta(seconds=31),
        ),
        side=Side.YES,
        requested_quantity=Quantity("1", "1"),
        limit_price=Price("0.50", "0.01"),
        maximum_loss_per_unit=Money("1"),
        fee_per_unit=Money("0"),
        cycle_at=NOW,
    )
    missing_bid = evaluate_immediate_cross(
        policy=_policy(),
        evidence=_evidence(yes_bids=()),
        side=Side.YES,
        requested_quantity=Quantity("1", "1"),
        limit_price=Price("0.50", "0.01"),
        maximum_loss_per_unit=Money("1"),
        fee_per_unit=Money("0"),
        cycle_at=NOW,
    )

    assert stale.state is PaperFillState.REJECTED
    assert stale.filled_quantity == Quantity("0", "1")
    assert "PAPER_EVIDENCE_STALE" in stale.reason_codes
    assert missing_bid.state is PaperFillState.REJECTED
    assert "PAPER_SPREAD_UNAVAILABLE" in missing_bid.reason_codes


def test_conservative_mark_uses_only_visible_bid_depth_and_never_posts_pnl() -> None:
    mark = evaluate_conservative_mark(
        mark_id="paper-mark-v1",
        position_id="paper-position-v1",
        side=Side.YES,
        quantity=Quantity("3", "1"),
        retained_position_cost=Money("1.50"),
        evidence=_evidence(yes_bids=(_level("0.49", "2"),)),
        maximum_depth=2,
        close_fee_per_unit=Money("0.01"),
        slippage_reserve_per_contract=Money("0.01"),
        marked_at=NOW,
    )

    assert mark.supported_quantity == Quantity("2", "1")
    assert mark.unsupported_quantity == Quantity("1", "1")
    assert mark.conservative_liquidation_value == Money("0.98")
    assert mark.conservative_unrealized_pnl == Decimal("-0.56")


@pytest.mark.parametrize("maximum_depth", (0, 101))
def test_mark_rejects_unbounded_depth(maximum_depth: int) -> None:
    with pytest.raises(ValueError):
        evaluate_conservative_mark(
            mark_id="paper-mark-v1",
            position_id="paper-position-v1",
            side=Side.YES,
            quantity=Quantity("1", "1"),
            retained_position_cost=Money("0.50"),
            evidence=_evidence(),
            maximum_depth=maximum_depth,
            close_fee_per_unit=Money("0"),
            slippage_reserve_per_contract=Money("0"),
            marked_at=NOW,
        )


@pytest.mark.parametrize(
    "overrides",
    (
        {"maximum_visible_size_participation": RiskFraction("0")},
        {"unfilled_behavior": PaperUnfilledBehavior.CANCEL},
    ),
)
def test_initial_paper_policy_rejects_unsupported_execution_terms(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _policy(**overrides)
