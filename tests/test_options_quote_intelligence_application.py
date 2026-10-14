"""Idempotent orchestration proofs for genuine options quote-chain and
volatility intelligence. No database or network -- every input is a
constructed fixture against the in-memory repository."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from apex.application.historical_market_data import InMemoryHistoricalMarketDataRepository
from apex.application.options_quote_intelligence import (
    CreateOptionChainCapturePlan,
    CreateOptionsChainSnapshot,
    CreateOptionsChainSnapshotCommand,
    InMemoryOptionsQuoteIntelligenceRepository,
    RecordOptionChainCaptureRun,
    RecordOptionsQuoteProviderCapability,
    ResolveRealizedVolatility,
)
from apex.domain.historical_market_data import (
    HistoricalCapturePlan,
    HistoricalCapturePolicy,
    HistoricalCaptureUniverseMember,
    HistoricalDatasetCompletenessState,
    HistoricalDatasetSnapshot,
)
from apex.domain.options_intelligence import (
    CapabilityState,
    ExerciseStyle,
    OptionContract,
    OptionProviderCapabilities,
    OptionType,
    SettlementStyle,
)
from apex.domain.options_quote_intelligence import (
    DEFAULT_OPTIONS_COHORT_SYMBOLS,
    CaptureRunStatus,
    IvRankStatus,
    OptionChainCaptureBounds,
    OptionChainCapturePlan,
    OptionChainCaptureRun,
    OptionQuoteObservation,
    OptionsQuoteProviderCapabilitySnapshot,
    annualize_realized_volatility,
)
from apex.domain.research_cohort import (
    GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    CohortRole,
    CohortSector,
    ResolvedCohortMember,
)
from apex.domain.securities import (
    SecuritiesAdjustmentState,
    SecuritiesBarEvidence,
    SecuritiesBarInterval,
)

NOW = datetime(2026, 8, 8, 15, 0, tzinfo=UTC)
UNDERLYING = "security-spy-stable"


def _member(symbol: str = "SPY") -> ResolvedCohortMember:
    return ResolvedCohortMember(
        stable_security_id=UNDERLYING,
        symbol=symbol,
        sector=CohortSector.BROAD_MARKET,
        roles=(CohortRole.BROAD_INDEX_ETF,),
        asset_class="ETF",
    )


def _members() -> tuple[ResolvedCohortMember, ...]:
    return tuple(
        sorted(
            (_member(symbol) for symbol in DEFAULT_OPTIONS_COHORT_SYMBOLS),
            key=lambda item: item.symbol,
        )
    )


def _plan(created_at: datetime = NOW) -> OptionChainCapturePlan:
    return OptionChainCapturePlan.create(
        cohort_policy_id="research_cohort_policy_test",
        cohort_policy_hash="0" * 64,
        members=_members(),
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        created_at=created_at,
    )


def _contract(
    strike: str, option_type: OptionType, expiration: str = "2026-09-18"
) -> OptionContract:
    return OptionContract.create(
        provider_contract_id=f"SPY{expiration.replace('-', '')}{option_type.value[0]}{strike}",
        underlying_canonical_security_id=UNDERLYING,
        standardized_symbol=None,
        option_type=option_type,
        strike=strike,
        expiration=expiration,
        exercise_style=ExerciseStyle.AMERICAN,
        settlement_style=SettlementStyle.PHYSICAL,
        multiplier="100",
        deliverable="100 SHARES",
        listing_exchange=None,
        state="ACTIVE",
        contract_size_state="STANDARD",
        adjustment_state="UNKNOWN",
        source_provider="ALPACA_OPTIONS_MARKET_DATA",
        reference_receipt_ids=("receipt-1",),
    )


def _observation(
    strike: str, option_type: OptionType, bid: str, ask: str, *, captured_at: datetime = NOW
) -> OptionQuoteObservation:
    contract = _contract(strike, option_type)
    return OptionQuoteObservation.create(
        contract=contract,
        expected_underlying_canonical_security_id=UNDERLYING,
        bid=bid,
        ask=ask,
        bid_size="10",
        ask_size="10",
        last_trade=str((Decimal(bid) + Decimal(ask)) / 2),
        trade_size="5",
        underlying_reference_price="500",
        volume="100",
        open_interest=None,
        provider_implied_volatility=None,
        provider_delta=None,
        provider_gamma=None,
        provider_theta=None,
        provider_vega=None,
        provider_rho=None,
        market_status="OPEN",
        quote_timestamp=captured_at,
        availability_timestamp=captured_at,
        ingestion_timestamp=captured_at,
        captured_at=captured_at,
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )


def _observations(captured_at: datetime = NOW) -> tuple[OptionQuoteObservation, ...]:
    return (
        _observation("480", OptionType.PUT, "1.10", "1.30", captured_at=captured_at),
        _observation("500", OptionType.PUT, "8.00", "8.20", captured_at=captured_at),
        _observation("500", OptionType.CALL, "9.00", "9.20", captured_at=captured_at),
        _observation("520", OptionType.CALL, "1.50", "1.70", captured_at=captured_at),
    )


# ---------------------------------------------------------------------------
# Capability
# ---------------------------------------------------------------------------


def test_recording_capability_persists_and_is_queryable_by_provider() -> None:
    repository = InMemoryOptionsQuoteIntelligenceRepository()
    capability = OptionProviderCapabilities.create(
        provider="ALPACA_OPTIONS_MARKET_DATA",
        contract_discovery=CapabilityState.UNKNOWN,
        quotes=CapabilityState.UNKNOWN,
        trades=CapabilityState.UNKNOWN,
        snapshots=CapabilityState.UNKNOWN,
        implied_volatility=CapabilityState.UNKNOWN,
        greeks=CapabilityState.UNKNOWN,
        open_interest=CapabilityState.UNKNOWN,
        volume=CapabilityState.UNKNOWN,
    )
    snapshot = OptionsQuoteProviderCapabilitySnapshot.create(
        checked_at=NOW,
        credentials_available=False,
        requests_made=0,
        capability=capability,
        unavailable_reason="ALPACA_CREDENTIALS_ABSENT",
    )
    RecordOptionsQuoteProviderCapability(repository).execute(capability=snapshot)
    latest = repository.latest_capability("ALPACA_OPTIONS_MARKET_DATA")
    assert latest is not None
    assert latest.credentials_available is False
    assert repository.latest_capability("SOME_OTHER_PROVIDER") is None


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def test_creating_the_identical_plan_twice_returns_the_retained_row_not_a_duplicate() -> None:
    repository = InMemoryOptionsQuoteIntelligenceRepository()
    service = CreateOptionChainCapturePlan(repository)
    first = service.execute(plan=_plan())
    second = service.execute(plan=_plan(created_at=NOW + timedelta(days=1)))
    assert first.plan_id == second.plan_id
    assert len(repository.plans()) == 1


def test_latest_plan_returns_the_most_recently_created() -> None:
    repository = InMemoryOptionsQuoteIntelligenceRepository()
    older = OptionChainCapturePlan.create(
        cohort_policy_id="policy-a",
        cohort_policy_hash="0" * 64,
        members=_members(),
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        created_at=NOW,
    )
    newer = OptionChainCapturePlan.create(
        cohort_policy_id="policy-b",
        cohort_policy_hash="1" * 64,
        members=_members(),
        unresolved_symbols=(),
        bounds=OptionChainCaptureBounds.create(),
        created_at=NOW + timedelta(hours=1),
    )
    repository.save_plan(older)
    repository.save_plan(newer)
    assert repository.latest_plan().plan_id == newer.plan_id


# ---------------------------------------------------------------------------
# Capture run
# ---------------------------------------------------------------------------


def test_recording_a_blocked_run_is_idempotent_by_run_id() -> None:
    repository = InMemoryOptionsQuoteIntelligenceRepository()
    run = OptionChainCaptureRun.create(
        plan_id=_plan().plan_id,
        requested_at=NOW,
        completed_at=NOW,
        status=CaptureRunStatus.BLOCKED_CREDENTIALS_ABSENT,
        underlyings_requested=19,
        underlyings_captured=0,
        provider_request_count=0,
        payload_bytes=0,
        contracts_considered=0,
        contracts_quoted=0,
        snapshot_ids=(),
        reason="ALPACA_CREDENTIALS_ABSENT",
    )
    service = RecordOptionChainCaptureRun(repository)
    first = service.execute(run=run)
    second = service.execute(run=run)
    assert first.run_id == second.run_id
    assert len(repository.runs_for_plan(_plan().plan_id)) == 1


# ---------------------------------------------------------------------------
# Snapshot creation: idempotent replay, forward IV-history accumulation
# ---------------------------------------------------------------------------


def test_snapshot_creation_is_idempotent_across_repeated_identical_evidence() -> None:
    repository = InMemoryOptionsQuoteIntelligenceRepository()
    plan = _plan()
    service = CreateOptionsChainSnapshot(repository)
    command = CreateOptionsChainSnapshotCommand(
        plan=plan,
        run_id="run-1",
        member=_member(),
        underlying_reference_price=Decimal("500"),
        underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
        captured_at=NOW,
        observations=_observations(),
        as_of=date(2026, 8, 8),
        provider="ALPACA_OPTIONS_MARKET_DATA",
        source_receipt_ids=("receipt-1",),
    )
    first_snapshot, first_features = service.execute(command)
    second_snapshot, second_features = service.execute(command)
    assert first_snapshot.snapshot_id == second_snapshot.snapshot_id
    assert first_features.feature_id == second_features.feature_id
    assert len(repository.snapshots_for_underlying(UNDERLYING)) == 1
    assert len(repository.research_features_for_underlying(UNDERLYING)) == 1
    # The observations backing the snapshot are retained exactly once too.
    assert repository.observation_by_id(first_snapshot.observation_ids[0]) is not None


def test_forward_iv_history_accumulates_genuinely_across_repeated_captures() -> None:
    repository = InMemoryOptionsQuoteIntelligenceRepository()
    plan = _plan()
    service = CreateOptionsChainSnapshot(repository)
    day1 = service.execute(
        CreateOptionsChainSnapshotCommand(
            plan=plan,
            run_id="run-1",
            member=_member(),
            underlying_reference_price=Decimal("500"),
            underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
            captured_at=NOW,
            observations=_observations(NOW),
            as_of=date(2026, 8, 8),
            provider="ALPACA_OPTIONS_MARKET_DATA",
            source_receipt_ids=("receipt-1",),
        )
    )[0]
    assert day1.volatility_surface.iv_rank.status is IvRankStatus.INSUFFICIENT_HISTORY
    assert day1.volatility_surface.iv_rank.retained_snapshot_count == 0

    day2_time = NOW + timedelta(days=1)
    day2 = service.execute(
        CreateOptionsChainSnapshotCommand(
            plan=plan,
            run_id="run-2",
            member=_member(),
            underlying_reference_price=Decimal("501"),
            underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
            captured_at=day2_time,
            observations=_observations(day2_time),
            as_of=date(2026, 8, 9),
            provider="ALPACA_OPTIONS_MARKET_DATA",
            source_receipt_ids=("receipt-2",),
        )
    )[0]
    assert day2.snapshot_id != day1.snapshot_id
    assert day2.volatility_surface.iv_rank.retained_snapshot_count == 1
    assert len(repository.snapshots_for_underlying(UNDERLYING)) == 2


def test_snapshot_creation_persists_zero_observations_honestly_for_an_empty_capture() -> None:
    repository = InMemoryOptionsQuoteIntelligenceRepository()
    plan = _plan()
    service = CreateOptionsChainSnapshot(repository)
    snapshot, features = service.execute(
        CreateOptionsChainSnapshotCommand(
            plan=plan,
            run_id="run-1",
            member=_member(),
            underlying_reference_price=Decimal("500"),
            underlying_price_source="ALPACA_LATEST_STOCK_SNAPSHOT_IEX",
            captured_at=NOW,
            observations=(),
            as_of=date(2026, 8, 8),
            provider="ALPACA_OPTIONS_MARKET_DATA",
            source_receipt_ids=("receipt-1",),
        )
    )
    assert snapshot.contracts_considered == 0
    assert snapshot.data_quality_status.value == "EMPTY"
    assert features.atm_iv_nearest_expiration is None
    assert repository.observation_by_id("anything") is None


def test_observations_by_ids_fetches_only_the_requested_rows_missing_ids_skipped() -> None:
    repository = InMemoryOptionsQuoteIntelligenceRepository()
    observations = _observations()
    repository.save_observations(observations)
    fetched = repository.observations_by_ids(
        (observations[0].observation_id, "nonexistent-id", observations[2].observation_id)
    )
    assert {item.observation_id for item in fetched} == {
        observations[0].observation_id,
        observations[2].observation_id,
    }
    assert repository.observations_by_ids(()) == ()


# ---------------------------------------------------------------------------
# Realized-volatility integration: wires the previously-disconnected
# ``realized_volatility_annualized`` input to the genuine multi-asset
# historical dataset, reusing ``HistoricalPointInTimeReplay``/
# ``rolling_volatility`` -- never a bespoke recalculation.
# ---------------------------------------------------------------------------

_RV_STABLE_ID = "apex_stable_security_rv_fixture"


def _compounding_closes(bar_count: int) -> list[Decimal]:
    """A strictly-alternating, *approximately* ``+1%/-1%`` simple-return
    close series from an arbitrary base: proper compounding (each close is
    the *previous* close times ``1.01``/``0.99``, quantized to 4 decimals
    -- the retained-evidence authoritative-decimal scale bound), so every
    consecutive return is ``+-0.01`` to within quantization noise -- unlike
    two fixed absolute price levels, which are not symmetric percentage
    moves. Population stdev of any even-length contiguous tail is
    therefore ``0.01`` to within a small, bounded tolerance (equal
    +/-0.01-ish counts, mean approximately zero)."""

    closes = [Decimal("100")]
    for index in range(1, bar_count):
        factor = Decimal("1.01") if index % 2 == 1 else Decimal("0.99")
        closes.append((closes[-1] * factor).quantize(Decimal("0.0001")))
    return closes


def _bar_with_close(instrument_id: str, day: str, close: Decimal) -> SecuritiesBarEvidence:
    period_start = datetime.fromisoformat(f"{day}T14:30:00+00:00")
    period_end = datetime.fromisoformat(f"{day}T21:00:00+00:00")
    return SecuritiesBarEvidence(
        instrument_id=instrument_id,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=period_start,
        period_end=period_end,
        available_at=period_end + timedelta(minutes=30),
        open=str(close),
        high=str(close + 1),
        low=str(close - 1),
        close=str(close),
        volume="1000",
        source_identity="FIXTURE",
    )


class _FakeEvidenceStore:
    def __init__(self) -> None:
        self.datasets: dict[str, tuple] = {}

    def get_dataset(self, batch_id: str) -> tuple:
        return self.datasets[batch_id]


def _rv_dataset(
    *, bar_count: int, stable_security_id: str = _RV_STABLE_ID
) -> tuple[InMemoryHistoricalMarketDataRepository, _FakeEvidenceStore]:
    """Retains one genuine-multi-asset-cohort dataset snapshot (the exact
    idempotency key ``ResolveRealizedVolatility`` looks up) covering one
    instrument with ``bar_count`` daily bars of alternating ``+-1%``
    returns, the last dated 2026-08-06 (two sessions before ``NOW``,
    mirroring the real retained dataset's own staleness)."""

    historical_repository = InMemoryHistoricalMarketDataRepository()
    dates = [
        (date(2026, 8, 6) - timedelta(days=bar_count - 1 - index)).isoformat()
        for index in range(bar_count)
    ]
    policy = HistoricalCapturePolicy.create(
        provider="ALPACA_MARKET_DATA_V2",
        start_date=dates[0],
        end_date=dates[-1],
        chunk_size=100,
        maximum_instruments=100,
        maximum_requests=100,
        maximum_bytes=50_000_000,
    )
    member = HistoricalCaptureUniverseMember.create(
        stable_security_id=stable_security_id, symbol_at_capture="SPY", asset_class="ETF"
    )
    plan = HistoricalCapturePlan.draft(
        policy=policy,
        universe=(member,),
        evidence_class="GENUINE_PUBLIC",
        operator_action_id="op-rv-fixture",
        idempotency_key=(
            f"genuine-multi-asset-cohort-v1:{GENUINE_MULTI_ASSET_COHORT_POLICY_V1.policy_id}"
        ),
        created_at=NOW,
    )
    historical_repository.save_plan(plan)
    closes = _compounding_closes(bar_count)
    bars = tuple(
        _bar_with_close(member.instrument_id, day, close)
        for day, close in zip(dates, closes, strict=True)
    )
    batch_id = f"batch-{plan.plan_id}"
    store = _FakeEvidenceStore()
    store.datasets[batch_id] = ((), (), bars, ())
    snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(batch_id,),
        source_operation_ids=("op-1",),
        observation_count=len(bars),
        completeness_state=HistoricalDatasetCompletenessState.COMPLETE,
        earliest_observation_date=dates[0],
        latest_observation_date=dates[-1],
        created_at=NOW,
    )
    historical_repository.save_snapshot(snapshot)
    return historical_repository, store


def test_resolve_realized_volatility_computes_both_horizons_from_retained_bars() -> None:
    historical_repository, evidence = _rv_dataset(bar_count=65)
    resolver = ResolveRealizedVolatility(
        historical_repository=historical_repository,
        evidence=evidence,
        cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    )
    result = resolver.resolve(stable_security_id=_RV_STABLE_ID, as_of=NOW)
    assert result.status == "COMPUTED"
    assert result.window_sessions_primary == 20
    assert result.secondary_window_sessions == 60
    expected = annualize_realized_volatility(Decimal("0.01"))
    tolerance = Decimal("0.001")
    assert abs(result.realized_volatility_primary - expected) < tolerance
    assert abs(result.realized_volatility_secondary - expected) < tolerance
    assert result.dataset_snapshot_id is not None
    assert result.latest_bar_date == "2026-08-06"
    assert result.limitations == ()


def test_resolve_realized_volatility_never_leaks_a_future_bar() -> None:
    """A bar dated after ``as_of`` would change the hand-verified ``0.01``
    result if it leaked in -- point-in-time gating must exclude it."""

    historical_repository, evidence = _rv_dataset(bar_count=65)
    # Inject one additional, wildly different future bar dated after NOW.
    plan = historical_repository.list_plans()[0]
    snapshot = historical_repository.list_snapshots()[0]
    batch_id = snapshot.source_batch_ids[0]
    _instruments, _sessions, bars, _actions = evidence.datasets[batch_id]
    future_bar = _bar_far_future(plan.universe[0].instrument_id)
    evidence.datasets[batch_id] = ((), (), (*bars, future_bar), ())

    resolver = ResolveRealizedVolatility(
        historical_repository=historical_repository,
        evidence=evidence,
        cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    )
    result = resolver.resolve(stable_security_id=_RV_STABLE_ID, as_of=NOW)
    expected = annualize_realized_volatility(Decimal("0.01"))
    assert abs(result.realized_volatility_primary - expected) < Decimal("0.001")
    assert result.latest_bar_date == "2026-08-06"


def _bar_far_future(instrument_id: str) -> SecuritiesBarEvidence:
    day = "2026-12-31"
    period_start = datetime.fromisoformat(f"{day}T14:30:00+00:00")
    period_end = datetime.fromisoformat(f"{day}T21:00:00+00:00")
    return SecuritiesBarEvidence(
        instrument_id=instrument_id,
        interval=SecuritiesBarInterval.ONE_DAY,
        adjustment_state=SecuritiesAdjustmentState.RAW,
        period_start=period_start,
        period_end=period_end,
        available_at=period_end + timedelta(minutes=30),
        open="9000",
        high="9100",
        low="8900",
        close="9000",
        volume="1000",
        source_identity="FIXTURE",
    )


def test_resolve_realized_volatility_reports_insufficient_history_honestly() -> None:
    historical_repository, evidence = _rv_dataset(bar_count=10)
    resolver = ResolveRealizedVolatility(
        historical_repository=historical_repository,
        evidence=evidence,
        cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    )
    result = resolver.resolve(stable_security_id=_RV_STABLE_ID, as_of=NOW)
    assert result.status == "INSUFFICIENT_HISTORY"
    assert result.realized_volatility_primary is None
    assert result.realized_volatility_secondary is None
    assert result.dataset_snapshot_id is not None  # the dataset was found; just too short
    assert any("POINT_IN_TIME_SAFE_SESSIONS_RETAINED" in item for item in result.limitations)


def test_resolve_realized_volatility_reports_no_dataset_honestly() -> None:
    resolver = ResolveRealizedVolatility(
        historical_repository=InMemoryHistoricalMarketDataRepository(),
        evidence=_FakeEvidenceStore(),
        cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    )
    result = resolver.resolve(stable_security_id=_RV_STABLE_ID, as_of=NOW)
    assert result.status == "HISTORICAL_DATASET_UNAVAILABLE"
    assert result.dataset_snapshot_id is None
    assert result.realized_volatility_primary is None


def test_resolve_realized_volatility_reports_security_not_in_universe_honestly() -> None:
    historical_repository, evidence = _rv_dataset(bar_count=65)
    resolver = ResolveRealizedVolatility(
        historical_repository=historical_repository,
        evidence=evidence,
        cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    )
    result = resolver.resolve(stable_security_id="not-a-member-of-this-universe", as_of=NOW)
    assert result.status == "SECURITY_NOT_IN_RETAINED_UNIVERSE"
    assert result.dataset_snapshot_id is not None
    assert result.realized_volatility_primary is None


def test_resolve_realized_volatility_picks_up_a_fresher_snapshot_automatically() -> None:
    """The dataset snapshot is resolved dynamically (latest retained for
    the cohort's idempotency key), never a hardcoded id: a second, later
    capture for the identical cohort is picked up without a code change."""

    historical_repository, evidence = _rv_dataset(bar_count=10)
    first_snapshot = historical_repository.list_snapshots()[0]
    plan = historical_repository.list_plans()[0]
    # A second, later snapshot against the *same* plan with more history.
    dates = [(date(2026, 8, 6) - timedelta(days=64 - index)).isoformat() for index in range(65)]
    closes = _compounding_closes(65)
    bars = tuple(
        _bar_with_close(plan.universe[0].instrument_id, day, close)
        for day, close in zip(dates, closes, strict=True)
    )
    batch_id = f"batch-{plan.plan_id}-later"
    evidence.datasets[batch_id] = ((), (), bars, ())
    later_snapshot = HistoricalDatasetSnapshot.create(
        plan=plan,
        source_batch_ids=(batch_id,),
        source_operation_ids=("op-2",),
        observation_count=len(bars),
        completeness_state=HistoricalDatasetCompletenessState.COMPLETE,
        earliest_observation_date=dates[0],
        latest_observation_date=dates[-1],
        created_at=NOW + timedelta(hours=1),
    )
    historical_repository.save_snapshot(later_snapshot)

    resolver = ResolveRealizedVolatility(
        historical_repository=historical_repository,
        evidence=evidence,
        cohort_policy=GENUINE_MULTI_ASSET_COHORT_POLICY_V1,
    )
    result = resolver.resolve(stable_security_id=_RV_STABLE_ID, as_of=NOW)
    assert result.dataset_snapshot_id == later_snapshot.snapshot_id
    assert result.dataset_snapshot_id != first_snapshot.snapshot_id
    assert result.status == "COMPUTED"
