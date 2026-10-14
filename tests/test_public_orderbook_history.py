from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from apex.adapters.kalshi_orderbook_history_fixtures import (
    MARKET_TICKER,
    temporal_orderbook_history_fixture,
)
from apex.application.build_orderbook_observation_history import (
    BuildOrderbookObservationHistory,
    InMemoryOrderbookObservationHistoryStore,
    OrderbookHistoryRequest,
    OrderbookObservationHistoryError,
)
from apex.domain.public_orderbook_history import OrderbookHistoryContinuityState


def _service():
    return BuildOrderbookObservationHistory(
        store=InMemoryOrderbookObservationHistoryStore(temporal_orderbook_history_fixture())
    )


def test_fixture_history_calculates_bounded_candidate_streaks_and_breaks() -> None:
    report = _service().build(OrderbookHistoryRequest(provider_market_ticker=MARKET_TICKER))

    assert report.analysis.included_observation_count == 10
    assert report.analysis.candidate_observation_count == 7
    assert report.analysis.longest_candidate_streak_observation_count == 3
    assert report.analysis.current_candidate_streak_observation_count == 0
    assert report.analysis.evidence_break_count == 1
    assert report.analysis.observation_gap_count == 1
    assert report.analysis.maximum_observation_gap == timedelta(minutes=7)
    assert report.observations[2].matched_quantity_delta == 2
    assert report.observations[3].conservative_net_lower_bound_delta == Decimal("-0.15")
    assert report.observations[4].continuity_state is OrderbookHistoryContinuityState.NOT_APPLICABLE
    assert (
        report.observations[6].continuity_state is OrderbookHistoryContinuityState.OBSERVATION_GAP
    )
    assert report.observations[7].continuity_state is OrderbookHistoryContinuityState.EVIDENCE_BREAK
    assert report.observations[9].weighted_yes_ask is None
    assert report.observations[9].conservative_net_lower_bound is None
    assert "EVALUATION_FEES_ELIMINATE_GROSS_GAP" in report.observations[4].reason_codes


def test_history_requires_three_exact_market_observations_and_replays() -> None:
    values = temporal_orderbook_history_fixture()
    service = BuildOrderbookObservationHistory(
        store=InMemoryOrderbookObservationHistoryStore(values[:2])
    )
    with pytest.raises(OrderbookObservationHistoryError, match="at least three"):
        service.build(OrderbookHistoryRequest(provider_market_ticker=MARKET_TICKER))

    store = InMemoryOrderbookObservationHistoryStore(values)
    service = BuildOrderbookObservationHistory(store=store)
    request = OrderbookHistoryRequest(provider_market_ticker=MARKET_TICKER, maximum_observations=10)
    first = service.build(request)
    replay = service.build(request)
    assert not first.idempotent_replay
    assert replay.idempotent_replay
