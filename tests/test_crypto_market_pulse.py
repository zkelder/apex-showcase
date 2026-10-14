from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apex.adapters.coinbase_universe_public import (
    FixtureCoinbaseUniverseSource,
    _parse_product_book,
)
from apex.application.crypto_market_pulse import (
    ExecuteCryptoMarketPulseRefresh,
    InMemoryCryptoMarketPulseRepository,
    RegisterCryptoRefreshPlan,
    RunCurrentCryptoRelativeStrength,
    RunCurrentCryptoReversalRegime,
    crypto_market_pulse_workspace,
)
from apex.application.crypto_universe import (
    CaptureCryptoCalibrationHistory,
    CaptureCryptoUniverseObservations,
    CapturePublicCryptoProducts,
    CryptoUniverseConflict,
    MaterializeCryptoUniverse,
)
from apex.domain.crypto_market_pulse import (
    CryptoRefreshPlan,
    OpportunityTransition,
    PersistenceClassification,
    PulseQuality,
    RefreshState,
    build_opportunity_lineage,
    completed_utc_day,
    daily_refresh_due,
    universe_refresh_due,
)
from apex.domain.crypto_universe import (
    CryptoDailyObservation,
    OpportunityStatus,
    metric,
    reversal_regime_score,
)
from apex.operator_api import app
from scripts.dev import parser

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
HISTORY_START = datetime(2026, 1, 1, tzinfo=UTC)
HISTORY_END = datetime(2026, 7, 1, tzinfo=UTC)


class CountingFixtureSource(FixtureCoinbaseUniverseSource):
    def __init__(self) -> None:
        self.observation_requests = 0
        self.book_requests = 0

    def capture_daily_observations(self, **kwargs: object):
        self.observation_requests += 1
        return super().capture_daily_observations(**kwargs)  # type: ignore[arg-type]

    def capture_product_book(self, **kwargs: object):
        self.book_requests += 1
        return super().capture_product_book(**kwargs)  # type: ignore[arg-type]


class PositiveFixtureSource(FixtureCoinbaseUniverseSource):
    def capture_daily_observations(self, **kwargs: object):
        page = super().capture_daily_observations(**kwargs)  # type: ignore[arg-type]
        if kwargs["product_id"] != "XRP-USD":
            return page
        return replace(
            page,
            candles=tuple(
                CryptoDailyObservation(
                    bucket_start=item.bucket_start,
                    close=Decimal(100 + index * 2),
                    volume=item.volume,
                )
                for index, item in enumerate(page.candles)
            ),
        )


class InterruptingFixtureSource(FixtureCoinbaseUniverseSource):
    def __init__(self) -> None:
        self.requests = 0

    def capture_daily_observations(self, **kwargs: object):
        self.requests += 1
        if self.requests == 2:
            raise KeyboardInterrupt
        return super().capture_daily_observations(**kwargs)  # type: ignore[arg-type]


class StaleFixtureSource(FixtureCoinbaseUniverseSource):
    def capture_daily_observations(self, **kwargs: object):
        page = super().capture_daily_observations(**kwargs)  # type: ignore[arg-type]
        return replace(page, candles=page.candles[:-2])


class MissingBookFixtureSource(FixtureCoinbaseUniverseSource):
    def capture_product_book(self, **kwargs: object):
        raise ValueError("FIXTURE_PUBLIC_BOOK_UNAVAILABLE")


class PartiallyFailingBookFixtureSource(FixtureCoinbaseUniverseSource):
    """Book capture fails for exactly one product; every other product's
    candle and book requests succeed normally -- the genuine-provider
    scenario where one route's orderbook GET fails while the rest of the
    bounded ten-asset cycle succeeds."""

    def __init__(self, *, failing_product_id: str) -> None:
        self._failing_product_id = failing_product_id

    def capture_product_book(self, **kwargs: object):
        if kwargs["product_id"] == self._failing_product_id:
            raise ValueError("FIXTURE_PUBLIC_BOOK_UNAVAILABLE_FOR_ONE_ASSET")
        return super().capture_product_book(**kwargs)  # type: ignore[arg-type]


def _repository() -> tuple[
    InMemoryCryptoMarketPulseRepository,
    FixtureCoinbaseUniverseSource,
    str,
]:
    repository = InMemoryCryptoMarketPulseRepository()
    source = FixtureCoinbaseUniverseSource()
    capture = CapturePublicCryptoProducts(repository, source).execute(
        capture_id="pulse-fixture-products",
        requested_at=NOW,
    )
    CaptureCryptoUniverseObservations(repository, source).execute(
        capture_id=capture.capture_id,
        interval_start=HISTORY_START,
        interval_end=HISTORY_END,
        requested_at=NOW,
    )
    snapshot = MaterializeCryptoUniverse(repository).execute(
        snapshot_id="pulse-fixture-universe",
        capture_id=capture.capture_id,
        created_at=NOW,
    )
    RegisterCryptoRefreshPlan(repository).execute(
        plan_id="pulse-fixture-plan",
        universe_snapshot_id=snapshot.snapshot_id,
        actor="fixture-operator",
        created_at=NOW,
    )
    return repository, source, snapshot.snapshot_id


def test_daily_and_weekly_due_rules_and_completed_day_normalization() -> None:
    as_of = datetime(2026, 8, 2, 19, 30, tzinfo=UTC)
    assert completed_utc_day(as_of) == datetime(2026, 8, 2, tzinfo=UTC)
    assert daily_refresh_due(as_of=as_of, completed_as_of_times=())
    assert not daily_refresh_due(
        as_of=as_of,
        completed_as_of_times=(datetime(2026, 8, 2, 1, tzinfo=UTC),),
    )
    assert not universe_refresh_due(
        universe_created_at=datetime(2026, 7, 27, tzinfo=UTC),
        as_of=as_of,
    )
    assert universe_refresh_due(
        universe_created_at=datetime(2026, 7, 25, tzinfo=UTC),
        as_of=as_of,
    )


def test_public_book_normalization_is_decimal_bounded_and_route_exact() -> None:
    raw = (
        b'{"pricebook":{"product_id":"BTC-USD","bids":['
        b'{"price":"99.99","size":"2"}],"asks":['
        b'{"price":"100.01","size":"3"}]}}'
    )
    bid, ask, midpoint, spread, bid_depth, ask_depth = _parse_product_book(
        raw, product_id="BTC-USD", maximum_depth=5
    )
    assert (bid, ask, midpoint) == (Decimal("99.99"), Decimal("100.01"), Decimal("100"))
    assert spread == Decimal("2.00000000")
    assert bid_depth == Decimal("199.98000000")
    assert ask_depth == Decimal("300.03000000")
    with pytest.raises(ValueError, match="ROUTE_MISMATCH"):
        _parse_product_book(raw, product_id="ETH-USD", maximum_depth=5)


def test_public_book_rejects_missing_bid_missing_ask_and_empty_sides() -> None:
    ask_only = (
        b'{"pricebook":{"product_id":"BTC-USD","bids":[],"asks":[{"price":"100.01","size":"3"}]}}'
    )
    with pytest.raises(ValueError, match="SIDE_MISSING"):
        _parse_product_book(ask_only, product_id="BTC-USD", maximum_depth=5)

    bid_only = (
        b'{"pricebook":{"product_id":"BTC-USD","bids":[{"price":"99.99","size":"2"}],"asks":[]}}'
    )
    with pytest.raises(ValueError, match="SIDE_MISSING"):
        _parse_product_book(bid_only, product_id="BTC-USD", maximum_depth=5)

    no_sides_key = b'{"pricebook":{"product_id":"BTC-USD"}}'
    with pytest.raises(ValueError, match="SIDE_MISSING"):
        _parse_product_book(no_sides_key, product_id="BTC-USD", maximum_depth=5)


def test_public_book_rejects_invalid_price_and_invalid_quantity() -> None:
    # Zero clears the underlying authoritative-Decimal parser (which permits
    # zero by default) and is instead rejected by _parse_product_book's own
    # ``price <= 0`` / ``size <= 0`` economic-validity check.
    zero_price = (
        b'{"pricebook":{"product_id":"BTC-USD","bids":[{"price":"0","size":"2"}],'
        b'"asks":[{"price":"100.01","size":"3"}]}}'
    )
    with pytest.raises(ValueError, match="LEVEL_INVALID"):
        _parse_product_book(zero_price, product_id="BTC-USD", maximum_depth=5)

    zero_quantity = (
        b'{"pricebook":{"product_id":"BTC-USD","bids":[{"price":"99.99","size":"0"}],'
        b'"asks":[{"price":"100.01","size":"3"}]}}'
    )
    with pytest.raises(ValueError, match="LEVEL_INVALID"):
        _parse_product_book(zero_quantity, product_id="BTC-USD", maximum_depth=5)

    # Negative values fail closed even earlier, at the shared authoritative
    # Decimal parser itself, before _parse_product_book's own economic check
    # ever runs -- still a rejection, just a different (equally exact) cause.
    negative_price = (
        b'{"pricebook":{"product_id":"BTC-USD","bids":[{"price":"-1","size":"2"}],'
        b'"asks":[{"price":"100.01","size":"3"}]}}'
    )
    with pytest.raises(ValueError, match="negative values are not permitted"):
        _parse_product_book(negative_price, product_id="BTC-USD", maximum_depth=5)

    negative_quantity = (
        b'{"pricebook":{"product_id":"BTC-USD","bids":[{"price":"99.99","size":"-2"}],'
        b'"asks":[{"price":"100.01","size":"3"}]}}'
    )
    with pytest.raises(ValueError, match="negative values are not permitted"):
        _parse_product_book(negative_quantity, product_id="BTC-USD", maximum_depth=5)


def test_public_book_rejects_crossed_book() -> None:
    crossed = (
        b'{"pricebook":{"product_id":"BTC-USD","bids":[{"price":"100.05","size":"2"}],'
        b'"asks":[{"price":"100.01","size":"3"}]}}'
    )
    with pytest.raises(ValueError, match="CROSSED"):
        _parse_product_book(crossed, product_id="BTC-USD", maximum_depth=5)


def test_refresh_is_bounded_idempotent_and_excludes_current_day() -> None:
    repository, _, _ = _repository()
    source = CountingFixtureSource()
    service = ExecuteCryptoMarketPulseRefresh(repository, source)
    first = service.execute(
        refresh_id="pulse-refresh-2026-07-30",
        plan_id="pulse-fixture-plan",
        as_of=datetime(2026, 7, 30, 15, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
    )
    request_count = source.observation_requests
    replay = service.execute(
        refresh_id="pulse-refresh-2026-07-30",
        plan_id="pulse-fixture-plan",
        as_of=datetime(2026, 7, 30, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 18, tzinfo=UTC),
    )
    assert first.state is RefreshState.COMPLETED
    assert replay.pulse == first.pulse
    assert source.observation_requests == request_count == 10
    assert first.pulse is not None
    assert len(first.pulse.eligible_assets) == 10
    assert len(set(first.pulse.eligible_assets)) == 10
    assert all(
        candle.bucket_start < first.refresh.as_of
        for item in first.checkpoints
        for candle in item.observation.candles  # type: ignore[union-attr]
    )
    assert all(item.best_bid is None and item.best_ask is None for item in first.checkpoints)


def test_v2_refresh_retains_bounded_public_book_economics_and_replays_exactly() -> None:
    repository, _, snapshot_id = _repository()
    RegisterCryptoRefreshPlan(repository).execute(
        plan_id="pulse-public-book-plan",
        universe_snapshot_id=snapshot_id,
        actor="fixture-operator",
        created_at=NOW,
        current_market_evidence=True,
    )
    source = CountingFixtureSource()
    service = ExecuteCryptoMarketPulseRefresh(repository, source)
    first = service.execute(
        refresh_id="pulse-public-book-refresh",
        plan_id="pulse-public-book-plan",
        as_of=datetime(2026, 7, 30, 15, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
    )
    replay = service.execute(
        refresh_id="pulse-public-book-refresh",
        plan_id="pulse-public-book-plan",
        as_of=datetime(2026, 7, 30, 23, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 23, tzinfo=UTC),
    )
    assert first.state is RefreshState.COMPLETED
    assert first.network_requests_performed == 20
    assert source.observation_requests == source.book_requests == 10
    assert replay.network_requests_performed == 0
    assert replay.checkpoints == first.checkpoints
    assert all(
        item.best_bid is not None
        and item.best_ask is not None
        and item.best_bid < item.best_ask
        and item.spread_bps is not None
        and item.spread_bps > 0
        and item.visible_depth is not None
        and item.visible_depth > 0
        and item.quote_raw_evidence_hash is not None
        and item.quote_limitation == "BOUNDED_PUBLIC_ORDER_BOOK_RETAINED"
        for item in first.checkpoints
    )
    assert first.pulse is not None
    assert "BOUNDED_PUBLIC_ORDER_BOOK_IS_VISIBLE_EVIDENCE_NOT_FILLABILITY" in (
        first.pulse.limitations
    )

    history = CaptureCryptoCalibrationHistory(repository, FixtureCoinbaseUniverseSource()).execute(
        history_id="pulse-current-calibration-history-v1",
        universe_snapshot_id=snapshot_id,
        interval_start=datetime(2024, 7, 30, tzinfo=UTC),
        interval_end=datetime(2026, 7, 30, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
    )
    assert history.request_count == 30

    current = RunCurrentCryptoRelativeStrength(repository).execute(
        run_id="current-relative-strength-v1",
        pulse_id=first.pulse.pulse_id,
        generated_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
    )
    replayed = RunCurrentCryptoRelativeStrength(repository).execute(
        run_id="current-relative-strength-v1",
        pulse_id=first.pulse.pulse_id,
        generated_at=datetime(2026, 7, 30, 20, tzinfo=UTC),
    )
    assert replayed.idempotent_replay
    assert replayed.run == current.run
    assert replayed.opportunities == current.opportunities
    assert len(current.opportunities) == 10
    assert current.run.source_pulse_id == first.pulse.pulse_id
    assert current.run.calibration is not None
    assert current.run.calibration.dataset_end < current.run.generated_at
    assert current.run.calibration.observation_ids == tuple(
        sorted(item.observation_id for item in history.observations)
    )
    assert len(current.run.calibration.asset_coverage) == 10
    assert all(
        item.accepted_buckets == 730
        and item.request_count == 3
        and item.coverage_status == "COMPLETE"
        for item in current.run.calibration.asset_coverage
    )
    assert all(
        bucket.modeled_round_trip_cost == Decimal("0.014")
        and (
            bucket.expected_gross_return is None
            or bucket.expected_net_return
            == bucket.expected_gross_return - bucket.modeled_round_trip_cost
        )
        for bucket in current.run.calibration.buckets
    )
    assert len(current.run.calibration.usable_common_dates) == 730
    assert current.run.calibration.decision_outcomes
    assert all(
        item.decision_at < item.outcome_at < current.run.generated_at
        for item in current.run.calibration.decision_outcomes
    )
    assert sum(bucket.sample_count for bucket in current.run.calibration.buckets) == sum(
        item.included_in_bucket for item in current.run.calibration.decision_outcomes
    )
    returns_90 = sorted(item.trailing_return_90d for item in current.run.results)
    assert all(value is not None for value in returns_90)
    expected_median = (returns_90[4] + returns_90[5]) / Decimal(2)  # type: ignore[operator]
    assert all(item.universe_median_90d == expected_median for item in current.run.results)
    for item in current.run.results:
        assert item.relative_strength == item.trailing_return_90d - expected_median  # type: ignore[operator]
        assert item.gross_research_score == metric(
            item.relative_strength
            + Decimal("0.25") * item.trailing_return_30d  # type: ignore[operator]
            - item.reversal_penalty  # type: ignore[operator]
            - item.volatility_penalty  # type: ignore[operator]
            - item.spread_penalty  # type: ignore[operator]
            - item.depth_penalty  # type: ignore[operator]
            - Decimal("0.014")
        )
    assert all(item.calibration_sample_count is not None for item in current.opportunities)
    assert all(
        item.expected_gross_return is not None
        or "CALIBRATION_SAMPLE_INSUFFICIENT" in item.rejection_reasons
        for item in current.opportunities
    )
    assert all(
        item.expected_gross_return is None
        or (
            item.calibration_sample_count is not None
            and item.calibration_sample_count >= 30
            and item.uncertainty_lower is not None
            and item.uncertainty_upper is not None
            and item.expected_net_return is not None
            and item.uncertainty_lower <= item.expected_net_return <= item.uncertainty_upper
        )
        for item in current.opportunities
    )
    assert all(item.required_capital is not None for item in current.opportunities)
    assert all(item.maximum_loss == item.required_capital for item in current.opportunities)

    reversal = RunCurrentCryptoReversalRegime(repository).execute(
        run_id="current-reversal-regime-v1",
        pulse_id=first.pulse.pulse_id,
        generated_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
    )
    reversal_replay = RunCurrentCryptoReversalRegime(repository).execute(
        run_id="current-reversal-regime-v1",
        pulse_id=first.pulse.pulse_id,
        generated_at=datetime(2026, 7, 30, 20, tzinfo=UTC),
    )
    assert reversal_replay.idempotent_replay
    assert reversal_replay.run == reversal.run
    assert reversal_replay.opportunities == reversal.opportunities
    assert reversal.run.method_id == "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1"
    assert reversal.run.calibration is not None
    assert reversal.run.calibration.outcome_horizons == (14, 30)
    assert reversal.run.calibration.dataset_end < reversal.run.generated_at
    assert all(
        decision.decision_at < decision.outcome_at < reversal.run.generated_at
        and decision.forward_gross_return_14d is not None
        and decision.forward_gross_return_30d is not None
        for decision in reversal.run.calibration.decision_outcomes
    )
    assert all(
        tuple(horizon.outcome_days for horizon in bucket.horizon_results) == (14, 30)
        for bucket in reversal.run.calibration.buckets
    )
    assert all(
        item.method_id == "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1"
        and item.hypothetical_holding_days == 14
        and item.capital_authority == "NONE"
        and not item.paper_eligible
        and not item.live_eligible
        for item in reversal.opportunities
    )
    for item in reversal.run.results:
        assert item.trailing_return_7d is not None
        assert item.trailing_return_90d is not None
        assert item.spread_penalty is not None
        assert item.depth_penalty is not None
        assert item.gross_research_score == reversal_regime_score(
            return_7d=item.trailing_return_7d,
            return_90d=item.trailing_return_90d,
            volatility_30d=item.realized_volatility,
            spread_penalty=item.spread_penalty,
            depth_penalty=item.depth_penalty,
            modeled_round_trip_cost=Decimal("0.014"),
        )
        if item.source_eligible:
            assert item.trailing_return_90d > 0
            assert item.trailing_return_7d < 0
            assert item.calibration_sample_count is not None
            assert item.calibration_sample_count >= 30
            assert item.uncertainty_lower is not None and item.uncertainty_lower > 0
        if item.trailing_return_90d <= 0:
            assert "ABSOLUTE_90_DAY_RETURN_NOT_POSITIVE" in item.source_rejection_reasons
        if item.trailing_return_7d >= 0:
            assert "SEVEN_DAY_RETURN_NOT_NEGATIVE" in item.source_rejection_reasons
    assert all(
        (horizon.sample_count >= 30)
        == (horizon.expected_net_return is not None and horizon.uncertainty_lower is not None)
        for bucket in reversal.run.calibration.buckets
        for horizon in bucket.horizon_results
    )
    assert all(not item.paper_eligible and not item.live_eligible for item in current.opportunities)


def test_v2_missing_public_book_fails_closed_without_financial_action() -> None:
    repository, _, snapshot_id = _repository()
    RegisterCryptoRefreshPlan(repository).execute(
        plan_id="pulse-missing-book-plan",
        universe_snapshot_id=snapshot_id,
        actor="fixture-operator",
        created_at=NOW,
        current_market_evidence=True,
    )
    result = ExecuteCryptoMarketPulseRefresh(repository, MissingBookFixtureSource()).execute(
        refresh_id="pulse-missing-book-refresh",
        plan_id="pulse-missing-book-plan",
        as_of=datetime(2026, 7, 30, 15, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
    )
    assert result.state is RefreshState.FAILED
    assert result.pulse is None
    assert all(
        "PUBLIC_ORDER_BOOK_ValueError" in item.quality_reasons
        and item.best_bid is None
        and item.visible_depth is None
        for item in result.checkpoints
    )


def test_v2_partial_book_provider_failure_fails_closed_for_the_whole_refresh() -> None:
    """A single asset's book-request failure among the ten tracked assets
    must not silently downgrade to a usable pulse built from the other nine
    good checkpoints: the refresh state must be the distinguishable
    ``PARTIAL`` (not ``FAILED`` -- nine of ten checkpoints individually
    passed), and, exactly like a total failure, it must retain zero pulse
    snapshot and zero Opportunities. Mixed evidence within one cycle is never
    usable for any asset, not just the one that failed."""

    repository, _, snapshot_id = _repository()
    RegisterCryptoRefreshPlan(repository).execute(
        plan_id="pulse-partial-book-plan",
        universe_snapshot_id=snapshot_id,
        actor="fixture-operator",
        created_at=NOW,
        current_market_evidence=True,
    )
    universe = repository.universe_snapshot(snapshot_id)
    assert universe is not None
    failing_product_id = universe.eligible_product_ids[3]
    source = PartiallyFailingBookFixtureSource(failing_product_id=failing_product_id)
    result = ExecuteCryptoMarketPulseRefresh(repository, source).execute(
        refresh_id="pulse-partial-book-refresh",
        plan_id="pulse-partial-book-plan",
        as_of=datetime(2026, 7, 30, 15, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 16, tzinfo=UTC),
    )
    assert result.state is RefreshState.PARTIAL
    assert result.pulse is None
    assert len(result.checkpoints) == 10
    failing = next(item for item in result.checkpoints if item.product_id == failing_product_id)
    assert failing.quality is PulseQuality.FAILED
    assert "PUBLIC_ORDER_BOOK_ValueError" in failing.quality_reasons
    assert failing.best_bid is None
    assert failing.visible_bid_depth is None and failing.visible_ask_depth is None
    # The observation candles for the failing asset were still captured and
    # retained -- only its book request failed -- proving the loop isolates
    # each asset's book-capture failure instead of discarding candle evidence
    # it already fetched successfully.
    assert failing.observation is not None
    passing = [item for item in result.checkpoints if item.product_id != failing_product_id]
    assert len(passing) == 9
    assert all(
        item.quality is PulseQuality.PASSED
        and item.best_bid is not None
        and item.best_ask is not None
        and item.visible_bid_depth is not None
        and item.visible_ask_depth is not None
        for item in passing
    )


def test_same_plan_as_of_conflict_and_snapshot_immutability() -> None:
    repository, source, _ = _repository()
    service = ExecuteCryptoMarketPulseRefresh(repository, source)
    result = service.execute(
        refresh_id="pulse-refresh-one",
        plan_id="pulse-fixture-plan",
        as_of=datetime(2026, 7, 30, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )
    with pytest.raises(CryptoUniverseConflict, match="CRYPTO_PULSE_PLAN_AS_OF_CONFLICT"):
        service.execute(
            refresh_id="pulse-refresh-conflict",
            plan_id="pulse-fixture-plan",
            as_of=datetime(2026, 7, 30, 23, tzinfo=UTC),
            requested_at=datetime(2026, 7, 30, 23, tzinfo=UTC),
        )
    assert result.pulse is not None
    with pytest.raises(CryptoUniverseConflict, match="CRYPTO_PULSE_IDENTITY_CONFLICT"):
        repository.save_pulse_snapshot(
            replace(result.pulse, manifest_hash="f" * 64, canonical_hash="e" * 64)
        )


def test_running_refresh_resumes_completed_checkpoint_and_stale_evidence_blocks() -> None:
    repository, _, _ = _repository()
    interrupted = InterruptingFixtureSource()
    service = ExecuteCryptoMarketPulseRefresh(repository, interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.execute(
            refresh_id="resumable-refresh",
            plan_id="pulse-fixture-plan",
            as_of=datetime(2026, 7, 30, tzinfo=UTC),
            requested_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
        )
    assert len(repository.pulse_checkpoints("resumable-refresh")) == 1
    resumed_source = CountingFixtureSource()
    resumed = ExecuteCryptoMarketPulseRefresh(repository, resumed_source).execute(
        refresh_id="resumable-refresh",
        plan_id="pulse-fixture-plan",
        as_of=datetime(2026, 7, 30, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 2, tzinfo=UTC),
    )
    assert resumed.state is RefreshState.COMPLETED
    assert resumed_source.observation_requests == 9

    stale_repository, _, _ = _repository()
    stale = ExecuteCryptoMarketPulseRefresh(stale_repository, StaleFixtureSource()).execute(
        refresh_id="stale-refresh",
        plan_id="pulse-fixture-plan",
        as_of=datetime(2026, 7, 30, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )
    assert stale.state is RefreshState.FAILED
    assert all("OBSERVATION_STALE" in item.quality_reasons for item in stale.checkpoints)
    assert stale.pulse is None


def test_three_complete_refreshes_create_persistent_positive_lineage_and_summary() -> None:
    repository, _, _ = _repository()
    source = PositiveFixtureSource()
    results = []
    for offset in range(3):
        as_of = datetime(2026, 7, 30, tzinfo=UTC) + timedelta(days=offset)
        results.append(
            ExecuteCryptoMarketPulseRefresh(repository, source).execute(
                refresh_id=f"pulse-refresh-{offset + 1}",
                plan_id="pulse-fixture-plan",
                as_of=as_of,
                requested_at=as_of + timedelta(hours=1),
            )
        )
    positives = tuple(
        item
        for item in results[-1].lineages
        if item.current_score is not None and item.current_score > 0
    )
    assert positives
    assert all(item.consecutive_positive_refreshes == 3 for item in positives)
    assert all(
        item.persistence is PersistenceClassification.PERSISTENT_POSITIVE for item in positives
    )
    assert results[-1].summary is not None
    assert results[-1].summary.positive_score_count == len(positives)
    assert results[-1].summary.market_breadth == Decimal(len(positives)) / Decimal(10)


def test_reversal_expiration_and_data_quality_block_are_explicit() -> None:
    repository, _, _ = _repository()
    source = PositiveFixtureSource()
    first = ExecuteCryptoMarketPulseRefresh(repository, source).execute(
        refresh_id="pulse-positive",
        plan_id="pulse-fixture-plan",
        as_of=datetime(2026, 7, 30, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )
    current = next(
        item for item in first.opportunities if item.status is OpportunityStatus.RESEARCH_ONLY
    )
    prior_lineage = next(
        item for item in first.lineages if item.canonical_asset_id == current.canonical_asset_id
    )
    rejected = replace(
        current,
        opportunity_id="rejected-next",
        generated_at=current.generated_at + timedelta(days=1),
        valid_until=current.valid_until + timedelta(days=1),
        gross_research_score=Decimal("-0.01"),
        status=OpportunityStatus.REJECTED,
        rejection_reasons=("NON_POSITIVE_NET_RESEARCH_SCORE",),
        canonical_hash="a" * 64,
    )
    reversal = build_opportunity_lineage(
        refresh_id="next-refresh",
        pulse_id="next-pulse",
        as_of=current.generated_at + timedelta(days=1),
        asset_id=current.canonical_asset_id,
        product_id=current.product_id,
        current=rejected,
        prior=current,
        prior_lineage=prior_lineage,
        quality_blocked=False,
    )
    assert reversal.transition is OpportunityTransition.REVERSED_TO_NON_POSITIVE
    assert reversal.persistence is PersistenceClassification.REVERSED
    assert "PRIOR_OPPORTUNITY_EXPIRED" in reversal.transition_reasons
    blocked = build_opportunity_lineage(
        refresh_id="blocked-refresh",
        pulse_id="blocked-pulse",
        as_of=current.generated_at + timedelta(days=1),
        asset_id="BLOCKED",
        product_id="BLOCKED-USD",
        current=None,
        prior=None,
        prior_lineage=None,
        quality_blocked=True,
    )
    assert blocked.persistence is PersistenceClassification.DATA_QUALITY_BLOCKED


def test_disabled_plan_skips_and_workspace_has_zero_authority() -> None:
    repository, source, snapshot_id = _repository()
    repository.save_refresh_plan(
        CryptoRefreshPlan.create(
            plan_id="disabled-plan",
            universe_snapshot_id=snapshot_id,
            actor="fixture-operator",
            created_at=NOW,
            enabled=False,
        )
    )
    result = ExecuteCryptoMarketPulseRefresh(repository, source).execute(
        refresh_id="disabled-refresh",
        plan_id="disabled-plan",
        as_of=datetime(2026, 7, 30, tzinfo=UTC),
        requested_at=datetime(2026, 7, 30, 1, tzinfo=UTC),
    )
    assert result.state is RefreshState.SKIPPED_NOT_DUE
    workspace = crypto_market_pulse_workspace(repository)
    assert workspace["authority"] == "NONE"
    assert workspace["orders"] == workspace["positions"] == workspace["journals"] == 0


def test_exact_id_commands_and_read_only_api_routes_exist() -> None:
    plan = parser().parse_args(
        [
            "crypto-pulse-plan-register",
            "--plan-id",
            "plan-v2",
            "--snapshot-id",
            "snapshot-v2",
            "--actor",
            "operator",
            "--current-market-evidence",
        ]
    )
    assert plan.current_market_evidence
    parsed = parser().parse_args(
        [
            "crypto-pulse-refresh-run",
            "--refresh-id",
            "refresh-v1",
            "--plan-id",
            "plan-v1",
            "--as-of",
            "2026-07-30T00:00:00Z",
            "--mode",
            "fixture",
        ]
    )
    assert parsed.refresh_id == "refresh-v1"
    routes = {
        (route.path, method) for route in app.routes for method in getattr(route, "methods", ())
    }
    assert ("/api/v1/crypto-research/pulse/plans/{plan_id}", "GET") in routes
    assert ("/api/v1/crypto-research/pulse/refreshes/{refresh_id}", "GET") in routes
    assert ("/api/v1/crypto-research/pulse/checkpoints/{checkpoint_id}", "GET") in routes
    assert ("/api/v1/crypto-research/pulse/snapshots/{pulse_id}", "GET") in routes
    assert ("/api/v1/crypto-research/pulse/lineages/{lineage_id}", "GET") in routes
    assert ("/api/v1/crypto-research/pulse/summaries/{summary_id}", "GET") in routes
