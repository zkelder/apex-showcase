from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.domain.live_market_observation import (
    MAX_LIVE_OBSERVATION_ROUND_INSTRUMENTS,
    MAX_LIVE_OBSERVATION_UNIVERSE_INSTRUMENTS,
    ContemporaneousObservationDisposition,
    ExecutionObservationDependency,
    LiveMarketObservation,
    LiveMarketObservationError,
    LiveObservationCaptureRound,
    LiveObservationQuality,
    LiveObservationRoundStatus,
    LiveObservationType,
    compose_live_observation_round_instruments,
    evaluate_contemporaneous_observation,
    execution_required_live_instruments,
    live_observation_round_id,
)
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _observation(**overrides: object) -> LiveMarketObservation:
    defaults: dict[str, object] = {
        "round_id": "live-round-1",
        "venue": SharedVenue.CRYPTO,
        "provider": "COINBASE",
        "instrument_id": "BTC-USD",
        "observation_type": LiveObservationType.QUOTE,
        "captured_at": NOW,
        "bid_price": Decimal("100.00"),
        "ask_price": Decimal("100.02"),
        "evidence_class": EvidenceClass.HISTORICAL_PUBLIC,
        "provider_schema_version": "COINBASE_PUBLIC_PRODUCT_BOOK_V1",
        "source_identity": "COINBASE_PUBLIC_PRODUCT_BOOK",
        "quality": LiveObservationQuality.PASSED,
    }
    defaults.update(overrides)
    return LiveMarketObservation.create(**defaults)  # type: ignore[arg-type]


def test_observation_id_is_deterministic_from_round_instrument_and_type() -> None:
    first = _observation(bid_price=Decimal("99.00"))
    second = _observation(bid_price=Decimal("100.01"))
    assert first.observation_id == second.observation_id
    assert first.canonical_hash != second.canonical_hash


def test_observation_id_changes_with_round_instrument_or_type() -> None:
    base = _observation()
    assert base.observation_id != _observation(round_id="live-round-2").observation_id
    assert base.observation_id != _observation(instrument_id="ETH-USD").observation_id
    assert (
        base.observation_id
        != _observation(
            observation_type=LiveObservationType.TRADE,
            bid_price=None,
            ask_price=None,
            last_trade_price=Decimal("100.01"),
        ).observation_id
    )


def test_canonical_hash_excludes_captured_at() -> None:
    first = _observation(captured_at=NOW)
    second = _observation(captured_at=NOW + timedelta(minutes=5))
    assert first.canonical_hash == second.canonical_hash


def test_canonical_hash_changes_with_content() -> None:
    first = _observation(bid_price=Decimal("100.00"))
    second = _observation(bid_price=Decimal("100.01"))
    assert first.canonical_hash != second.canonical_hash


def test_authority_is_always_none() -> None:
    assert _observation().authority == "NONE"


def test_bid_exceeding_ask_is_rejected() -> None:
    with pytest.raises(LiveMarketObservationError, match="BID_EXCEEDS_ASK"):
        _observation(bid_price=Decimal("100.05"), ask_price=Decimal("100.00"))


@pytest.mark.parametrize(
    "field", ["bid_size", "ask_size", "bar_volume", "book_depth_bid", "book_depth_ask"]
)
def test_negative_size_fields_are_rejected(field: str) -> None:
    with pytest.raises(LiveMarketObservationError, match="NEGATIVE"):
        _observation(**{field: Decimal("-1")})


def test_bar_observation_requires_all_bar_fields() -> None:
    with pytest.raises(LiveMarketObservationError, match="BAR_FIELDS_INCOMPLETE"):
        _observation(
            observation_type=LiveObservationType.BAR,
            bid_price=None,
            ask_price=None,
            bar_open=Decimal("100"),
            bar_high=Decimal("101"),
        )


def test_bar_observation_accepts_complete_consistent_fields() -> None:
    value = _observation(
        observation_type=LiveObservationType.BAR,
        bid_price=None,
        ask_price=None,
        bar_open=Decimal("100"),
        bar_high=Decimal("102"),
        bar_low=Decimal("99"),
        bar_close=Decimal("101"),
        bar_volume=Decimal("50"),
        bar_interval_seconds=60,
    )
    assert value.bar_high == Decimal("102")


def test_bar_ohlc_inconsistency_is_rejected() -> None:
    with pytest.raises(LiveMarketObservationError, match="BAR_OHLC_INCONSISTENT"):
        _observation(
            observation_type=LiveObservationType.BAR,
            bid_price=None,
            ask_price=None,
            bar_open=Decimal("100"),
            bar_high=Decimal("100.5"),
            bar_low=Decimal("99"),
            bar_close=Decimal("101"),  # close above high
            bar_volume=Decimal("50"),
            bar_interval_seconds=60,
        )


def test_bar_interval_must_be_positive() -> None:
    with pytest.raises(LiveMarketObservationError, match="BAR_INTERVAL_INVALID"):
        _observation(
            observation_type=LiveObservationType.BAR,
            bid_price=None,
            ask_price=None,
            bar_open=Decimal("100"),
            bar_high=Decimal("101"),
            bar_low=Decimal("99"),
            bar_close=Decimal("100"),
            bar_volume=Decimal("50"),
            bar_interval_seconds=0,
        )


def test_failed_quality_requires_at_least_one_reason() -> None:
    with pytest.raises(LiveMarketObservationError, match="FAILED_QUALITY_REQUIRES_REASONS"):
        _observation(
            bid_price=None,
            ask_price=None,
            quality=LiveObservationQuality.FAILED,
            quality_reasons=(),
        )


def test_instrument_id_is_required() -> None:
    with pytest.raises(LiveMarketObservationError, match="INSTRUMENT_ID_REQUIRED"):
        _observation(instrument_id="")


def _round(**overrides: object) -> LiveObservationCaptureRound:
    defaults: dict[str, object] = {
        "plan_id": "live-plan-1",
        "venue": SharedVenue.CRYPTO,
        "provider": "COINBASE",
        "window_start": NOW,
        "requested_at": NOW,
        "completed_at": NOW,
        "status": LiveObservationRoundStatus.COMPLETED,
        "requested_instrument_count": 3,
        "succeeded_count": 3,
        "failed_count": 0,
        "evidence_class": EvidenceClass.HISTORICAL_PUBLIC,
    }
    defaults.update(overrides)
    return LiveObservationCaptureRound.create(**defaults)  # type: ignore[arg-type]


def test_round_id_is_deterministic_from_plan_and_window() -> None:
    assert live_observation_round_id(plan_id="p", window_start=NOW) == live_observation_round_id(
        plan_id="p", window_start=NOW
    )
    assert _round().round_id == live_observation_round_id(plan_id="live-plan-1", window_start=NOW)


def test_round_id_changes_with_window_start() -> None:
    first = live_observation_round_id(plan_id="p", window_start=NOW)
    second = live_observation_round_id(plan_id="p", window_start=NOW + timedelta(minutes=1))
    assert first != second


def test_round_rejects_negative_counts() -> None:
    with pytest.raises(LiveMarketObservationError, match="COUNT_NEGATIVE"):
        _round(succeeded_count=-1)


def test_round_rejects_counts_exceeding_requested() -> None:
    with pytest.raises(LiveMarketObservationError, match="COUNT_EXCEEDS_REQUESTED"):
        _round(requested_instrument_count=1, succeeded_count=1, failed_count=1)


def test_evaluate_contemporaneous_observation_absent_when_no_candidate() -> None:
    result = evaluate_contemporaneous_observation(
        candidate=None, as_of=NOW, max_staleness_seconds=60
    )
    assert result.disposition is ContemporaneousObservationDisposition.ABSENT
    assert result.observation is None


def test_evaluate_contemporaneous_observation_absent_when_candidate_failed_quality() -> None:
    failed = _observation(
        bid_price=None,
        ask_price=None,
        quality=LiveObservationQuality.FAILED,
        quality_reasons=("PROVIDER_OSError",),
    )
    result = evaluate_contemporaneous_observation(
        candidate=failed, as_of=NOW, max_staleness_seconds=60
    )
    assert result.disposition is ContemporaneousObservationDisposition.ABSENT
    assert result.observation is None


def test_evaluate_contemporaneous_observation_satisfied_when_fresh() -> None:
    candidate = _observation(captured_at=NOW)
    result = evaluate_contemporaneous_observation(
        candidate=candidate, as_of=NOW + timedelta(seconds=30), max_staleness_seconds=60
    )
    assert result.disposition is ContemporaneousObservationDisposition.SATISFIED
    assert result.observation == candidate
    assert result.staleness_seconds == 30


def test_evaluate_contemporaneous_observation_stale_when_too_old() -> None:
    candidate = _observation(captured_at=NOW)
    result = evaluate_contemporaneous_observation(
        candidate=candidate, as_of=NOW + timedelta(seconds=120), max_staleness_seconds=60
    )
    assert result.disposition is ContemporaneousObservationDisposition.STALE
    assert result.observation is None
    assert result.staleness_seconds == 120


def test_evaluate_contemporaneous_observation_rejects_future_timestamp() -> None:
    candidate = _observation(captured_at=NOW)
    with pytest.raises(LiveMarketObservationError, match="FUTURE_TIMESTAMP"):
        evaluate_contemporaneous_observation(
            candidate=candidate, as_of=NOW - timedelta(seconds=1), max_staleness_seconds=60
        )


def test_evaluate_contemporaneous_observation_rejects_invalid_staleness_bound() -> None:
    with pytest.raises(LiveMarketObservationError, match="STALENESS_BOUND_INVALID"):
        evaluate_contemporaneous_observation(candidate=None, as_of=NOW, max_staleness_seconds=0)


# ---- PAPER Trial Closure Readiness v1: execution-required instruments ----

_PRODUCTION_UNIVERSE_2026_09_22 = (
    "BTC-USDC",
    "ETH-USDC",
    "SOL-USD",
    "XRP-USD",
    "DOGE-USD",
    "ADA-USD",
    "ZEC-USDC",
    "LINK-USD",
    "AVAX-USD",
    "LTC-USD",
)


def test_required_instrument_is_observed_even_when_absent_from_the_universe() -> None:
    composed = compose_live_observation_round_instruments(
        required=("BTC-USD",), universe=_PRODUCTION_UNIVERSE_2026_09_22
    )
    assert composed[0] == "BTC-USD"
    # No universe product is displaced by the required instrument.
    assert composed[1:] == _PRODUCTION_UNIVERSE_2026_09_22
    assert len(composed) == 11


def test_required_instrument_already_in_universe_is_requested_exactly_once() -> None:
    universe = ("ETH-USD", "BTC-USD", "SOL-USD")
    composed = compose_live_observation_round_instruments(required=("BTC-USD",), universe=universe)
    assert composed == ("BTC-USD", "ETH-USD", "SOL-USD")
    assert len(composed) == len(set(composed))


def test_no_required_instruments_keeps_the_original_universe_round_exactly() -> None:
    universe = tuple(f"COIN{index}-USD" for index in range(15))
    composed = compose_live_observation_round_instruments(required=(), universe=universe)
    assert composed == universe[:MAX_LIVE_OBSERVATION_UNIVERSE_INSTRUMENTS]


def test_required_instruments_are_never_truncated_and_fail_loudly_past_the_bound() -> None:
    required = tuple(f"REQ{index}-USD" for index in range(MAX_LIVE_OBSERVATION_ROUND_INSTRUMENTS))
    composed = compose_live_observation_round_instruments(
        required=required, universe=_PRODUCTION_UNIVERSE_2026_09_22
    )
    assert composed == required  # universe yields; required never does
    with pytest.raises(LiveMarketObservationError, match="EXCEED_ROUND_BOUND"):
        compose_live_observation_round_instruments(
            required=(*required, "ONE-MORE-USD"), universe=()
        )


def test_execution_required_instruments_merge_dependencies_deterministically() -> None:
    required = execution_required_live_instruments(
        active_trial_instrument_ids=("BTC-USD",),
        open_order_instrument_ids=("BTC-USD",),
        open_position_instrument_ids=("BTC-USD", "ETH-USD"),
        placement_target_instrument_ids=("BTC-USD", "BTC-USD"),
    )
    assert [item.instrument_id for item in required] == ["BTC-USD", "ETH-USD"]
    assert required[0].dependencies == (
        ExecutionObservationDependency.ACTIVE_STRATEGY_PAPER_TRIAL,
        ExecutionObservationDependency.OPEN_PAPER_ORDER,
        ExecutionObservationDependency.OPEN_PAPER_POSITION,
        ExecutionObservationDependency.STRATEGY_PAPER_PLACEMENT_TARGET,
    )
    assert required[1].dependencies == (ExecutionObservationDependency.OPEN_PAPER_POSITION,)


def test_nothing_is_required_once_no_lifecycle_depends_on_an_instrument() -> None:
    assert (
        execution_required_live_instruments(
            active_trial_instrument_ids=(),
            open_order_instrument_ids=(),
            open_position_instrument_ids=(),
            placement_target_instrument_ids=(),
        )
        == ()
    )


def test_blank_required_instrument_is_rejected() -> None:
    with pytest.raises(LiveMarketObservationError, match="REQUIRED_INSTRUMENT_INVALID"):
        execution_required_live_instruments(
            active_trial_instrument_ids=(" BTC-USD",),
            open_order_instrument_ids=(),
            open_position_instrument_ids=(),
            placement_target_instrument_ids=(),
        )
