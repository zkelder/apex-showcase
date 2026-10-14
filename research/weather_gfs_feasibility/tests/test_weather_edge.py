from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from research.weather_gfs_feasibility.archive import SnapshotResult
from research.weather_gfs_feasibility.core import FeasibilityError
from research.weather_gfs_feasibility.edge import (
    EligibleMarket,
    PriceEvidence,
    ResearchRow,
    StructuredThreshold,
    _dataset_rows,
    _features,
    block_bootstrap,
    chronological_split,
    eligible_markets,
    fit_logistic,
    model_probability,
    parse_threshold,
    select_candle_at_or_before,
    select_model,
)
from research.weather_gfs_feasibility.kalshi import (
    HistoricalCandle,
    HistoricalMarket,
    _historical_candle,
    _historical_daily_high_market,
)


def _market(
    *, ticker: str = "KXHIGHLAX-25MAR01-T70", result: str | None = "yes"
) -> HistoricalMarket:
    return HistoricalMarket(
        ticker=ticker,
        event_ticker="KXHIGHLAX-25MAR01",
        series_ticker="KXHIGHLAX",
        contract_date=date(2025, 3, 1),
        close_time="2025-03-02T07:59:00Z",
        rule=(
            "If the highest temperature recorded in Los Angeles Airport, CA for March 01, 2025 "
            "as reported by the National Weather Service's Climatological Report (Daily), "
            "is greater "
            "than 70°, then the market resolves to Yes."
        ),
        rule_sha256="rule",
        result=result,
        status="finalized",
        source_response_sha256="market-source",
    )


def _row(*, day: date, lead: int, result: int = 1) -> ResearchRow:
    return ResearchRow(
        contract_date=day,
        ticker=f"KXHIGHLAX-{day.isoformat()}-{lead}",
        event_ticker=f"KXHIGHLAX-{day.isoformat()}",
        series_ticker="KXHIGHLAX",
        lead_hours=lead,
        threshold_kind="GREATER_THAN",
        threshold=Decimal("70"),
        result_yes=result,
        forecast_daily_maximum=Decimal("72"),
        signed_margin=Decimal("2"),
        market_probability=Decimal("0.60"),
        candle_end_time=datetime(2025, 2, 28, tzinfo=UTC),
        forecast_initialization=datetime(2025, 2, 28, tzinfo=UTC),
        forecast_source_hash="forecast",
        market_source_hash="market",
        rule_sha256="rule",
        binding_hash="binding",
    )


def test_threshold_parser_retains_interval_and_directional_strikes() -> None:
    assert parse_threshold("is greater than 70°") == StructuredThreshold(
        "GREATER_THAN", Decimal("70")
    )
    assert parse_threshold("is less than 63 degrees") == StructuredThreshold(
        "LESS_THAN", Decimal("63")
    )
    assert parse_threshold("is between 69-70°") == StructuredThreshold(
        "BETWEEN", Decimal("69"), Decimal("70")
    )
    with pytest.raises(FeasibilityError, match="supported"):
        parse_threshold("is exactly 70°")


def test_chronological_split_is_deterministic_and_keeps_event_dates_together() -> None:
    dates = [date(2025, 3, 1) + timedelta(days=index) for index in range(60)]
    first, second = chronological_split(dates), chronological_split(list(reversed(dates)))

    assert first == second
    assert len(first.training_dates) == 42
    assert len(first.testing_dates) == 18
    assert set(first.training_dates).isdisjoint(first.testing_dates)
    assert max(first.training_dates) < min(first.testing_dates)


def test_later_market_candle_is_never_selected() -> None:
    as_of = datetime(2025, 3, 1, 8, tzinfo=UTC)
    earlier = HistoricalCandle("ticker", as_of - timedelta(hours=1), "0.4", None, "earlier")
    later = HistoricalCandle("ticker", as_of + timedelta(hours=1), "0.8", None, "later")

    assert select_candle_at_or_before((earlier, later), as_of=as_of) == earlier
    assert select_candle_at_or_before((later,), as_of=as_of) is None


def test_duplicate_and_void_market_evidence_fail_closed() -> None:
    market = _market()
    with pytest.raises(FeasibilityError, match="duplicate"):
        eligible_markets((market, market), series_ticker="KXHIGHLAX")

    accepted, rejected = eligible_markets((_market(result="void"),), series_ticker="KXHIGHLAX")
    assert not accepted
    assert rejected["VOIDED_OR_DISPUTED_OUTCOME"] == 1


def test_exact_series_filter_is_retained_when_historical_row_omits_series_field() -> None:
    market = _market()
    raw = {
        "ticker": market.ticker,
        "event_ticker": market.event_ticker,
        "close_time": market.close_time,
        "rules_primary": market.rule,
        "result": market.result,
        "status": market.status,
    }

    retained = _historical_daily_high_market(
        raw,
        "source",
        requested_series_ticker="KXHIGHLAX",
    )

    assert retained is not None
    assert retained.series_ticker == "KXHIGHLAX"


def test_conflicting_per_row_series_lineage_fails_closed() -> None:
    market = _market()
    raw = {
        "ticker": market.ticker,
        "event_ticker": market.event_ticker,
        "series_ticker": "KXOTHER",
        "close_time": market.close_time,
        "rules_primary": market.rule,
        "result": market.result,
        "status": market.status,
    }

    with pytest.raises(FeasibilityError, match="conflicts"):
        _historical_daily_high_market(raw, "source", requested_series_ticker="KXHIGHLAX")


def test_historical_price_parser_preserves_decimal_dollars_and_quote_midpoint() -> None:
    direct = _historical_candle(
        "ticker",
        {"end_period_ts": 1740787200, "price": {"close": "0.6000"}, "volume": "3.00"},
        "source",
    )
    midpoint = _historical_candle(
        "ticker",
        {
            "end_period_ts": 1740787200,
            "price": {"close": None},
            "yes_bid": {"close": "0.20"},
            "yes_ask": {"close": "0.40"},
            "volume": "0.00",
        },
        "source",
    )

    assert direct is not None
    assert direct.probability == "0.6000"
    assert direct.price_field == "price.close"
    assert midpoint is not None
    assert midpoint.probability == "0.30"
    assert midpoint.price_field == "midpoint(yes_bid.close,yes_ask.close)"


def test_settlement_label_is_not_a_logistic_feature_or_model_choice_input() -> None:
    yes = _row(day=date(2025, 3, 1), lead=48, result=1)
    no = _row(day=date(2025, 3, 1), lead=48, result=0)

    assert _features(yes, model="B", mean=0.0, scale=1.0) == _features(
        no, model="B", mean=0.0, scale=1.0
    )
    training = tuple(
        _row(day=date(2025, 3, 1) + timedelta(days=index), lead=lead, result=index % 2)
        for index in range(30)
        for lead in (48, 24)
    )
    _, _, selected, rule = select_model(training)
    assert selected.name == "MODEL_C"
    assert "TRAINING_ROWS" in rule


def test_model_coefficients_do_not_depend_on_held_out_rows() -> None:
    training = tuple(
        _row(day=date(2025, 3, 1) + timedelta(days=index), lead=48, result=index % 2)
        for index in range(30)
    )
    held_out = _row(day=date(2026, 1, 1), lead=48, result=1)

    initial = fit_logistic(training, model="B")
    changed_held_out = _row(day=held_out.contract_date, lead=held_out.lead_hours, result=0)
    repeated = fit_logistic(training, model="B")

    assert initial == repeated
    assert model_probability(initial, held_out) == model_probability(initial, changed_held_out)


def test_block_bootstrap_resamples_whole_event_dates() -> None:
    rows = (
        _row(day=date(2025, 3, 1), lead=48),
        _row(day=date(2025, 3, 1), lead=24),
        _row(day=date(2025, 3, 2), lead=48, result=0),
        _row(day=date(2025, 3, 2), lead=24, result=0),
    )
    result = block_bootstrap(
        rows,
        (Decimal("0.7"), Decimal("0.7"), Decimal("0.3"), Decimal("0.3")),
        (Decimal("0.5"), Decimal("0.5"), Decimal("0.5"), Decimal("0.5")),
        repetitions=12,
        seed=3,
    )

    assert result["method"] == "event-date-block-bootstrap-percentile-v1"
    assert result["repetitions"] == 12


def test_missing_price_or_forecast_is_not_promoted_to_a_dataset_row() -> None:
    market = EligibleMarket(_market(), StructuredThreshold("GREATER_THAN", Decimal("70")))
    candle = HistoricalCandle(
        ticker=market.market.ticker,
        end_time=datetime(2025, 2, 28, tzinfo=UTC),
        probability="0.5",
        volume=None,
        source_response_sha256="candle",
    )
    price = PriceEvidence(market, 48, datetime(2025, 3, 1, tzinfo=UTC), candle)
    snapshot = SnapshotResult(
        contract_date=market.market.contract_date,
        lead_hours=48,
        as_of=datetime(2025, 3, 1, tzinfo=UTC),
        initialization=datetime(2025, 2, 28, tzinfo=UTC),
        complete=False,
        reason="missing field",
        fields=(),
        daily_maximum=None,
    )

    rows = _dataset_rows(
        dates=(market.market.contract_date,),
        markets={market.market.contract_date: (market,)},
        prices={(market.market.ticker, 48): price},
        snapshots={(market.market.contract_date, 48): snapshot},
        configuration_hash="binding",
    )

    assert rows == ()


def test_future_forecast_initialization_fails_closed() -> None:
    market = EligibleMarket(_market(), StructuredThreshold("GREATER_THAN", Decimal("70")))
    as_of = datetime(2025, 3, 1, tzinfo=UTC)
    price = PriceEvidence(
        market,
        48,
        as_of,
        HistoricalCandle(market.market.ticker, as_of, "0.5", None, "candle"),
    )
    future_snapshot = SnapshotResult(
        contract_date=market.market.contract_date,
        lead_hours=48,
        as_of=as_of,
        initialization=as_of + timedelta(hours=1),
        complete=True,
        reason=None,
        fields=(),
        daily_maximum=Decimal("72"),
    )

    with pytest.raises(FeasibilityError, match="future"):
        _dataset_rows(
            dates=(market.market.contract_date,),
            markets={market.market.contract_date: (market,)},
            prices={(market.market.ticker, 48): price},
            snapshots={(market.market.contract_date, 48): future_snapshot},
            configuration_hash="binding",
        )
