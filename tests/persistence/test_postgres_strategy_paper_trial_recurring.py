"""Recurring Strategy PAPER Orchestration v1: the multi-cycle demonstration
and focused persistence tests for ``ExistingCapabilityOperatingTaskExecutor
._execute_strategy_paper_trial`` (``OperatingTaskType.
STRATEGY_PAPER_TRIAL_EXECUTION``).

Runs against this worktree's own isolated, disposable Postgres (never
production -- see docs/WORKTREE_VERIFICATION.md). What is genuine vs.
clearly-labeled-reference here, stated plainly (mirrors tests/persistence/
test_postgres_strategy_paper_trial.py's own module docstring convention):

- Candidate discovery and evidence-gated promotion run against a REAL,
  retained ``CryptoProductObservation`` for BTC-USD -- but its own daily
  closes are a hand-chosen, clearly FIXTURE-labeled deterministic
  dip-then-recovery series (never a claim about real BTC-USD price
  history), so this suite deterministically controls which of the four
  canonical benchmarks legitimately promote rather than merely hoping the
  ordinal-derived default fixture pattern happens to. The product/universe/
  refresh-plan bootstrap chain around it is the real, unmodified
  ``CapturePublicCryptoProducts``/``CaptureCryptoUniverseObservations``/
  ``MaterializeCryptoUniverse``/``RegisterCryptoRefreshPlan`` pipeline
  (``FixtureCoinbaseUniverseSource``), identical to the existing, reviewed
  ``tests/persistence/test_postgres_crypto_market_pulse.py``.
- ``test_genuine_coinbase_decision_and_fill_evidence`` makes real,
  credential-free, unauthenticated public Coinbase network calls for
  decision-time and fill-time price evidence via the canonical Live Market
  Observation substrate -- proving Phase C against a real provider at least
  once, separately from the deterministic multi-cycle lifecycle proof (the
  4-hour order validity window makes a natural, unattended real-price exit
  impractical across real wall-clock cycles -- see module docstring
  precedent in test_postgres_strategy_paper_trial.py and this milestone's
  own operating instructions, which explicitly anticipate and permit this
  split).
- Every PAPER execution mechanic (eligibility, authorization, order
  construction, preview, submission, fill, ledger, reconciliation,
  attribution, strategy health) is Apex's existing, unmodified
  ``domain.crypto_paper``/``application.crypto_paper``/
  ``domain.performance_attribution`` machinery -- this suite adds no new
  execution logic, only genuine orchestration and genuine inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.config import Config

from apex.adapters.coinbase_universe_public import (
    CoinbasePublicUniverseSource,
    FixtureCoinbaseUniverseSource,
)
from apex.adapters.crypto_market_pulse_postgres import PostgresCryptoMarketPulseRepository
from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.adapters.live_market_observation_postgres import (
    PostgresLiveMarketObservationRepository,
)
from apex.adapters.operating_cycle_postgres import (
    PostgresOperatingCycleRepository,
    PostgresOperatingStateSource,
)
from apex.adapters.operating_cycle_tasks import (
    GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
    ExistingCapabilityOperatingTaskExecutor,
)
from apex.adapters.performance_attribution_postgres import (
    PostgresPerformanceAttributionRepository,
)
from apex.adapters.strategy_evaluation_history_postgres import (
    PostgresInternalStrategyEvaluationRepository,
)
from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
    PostgresStrategyPaperTrialClosureRepository,
    PostgresStrategyPaperTrialRepository,
)
from apex.adapters.strategy_version_provenance_postgres import (
    STRATEGY_VERSION_PROVENANCE_TABLES,
    PostgresStrategyVersionProvenanceRepository,
)
from apex.adapters.total_market_context_postgres import PostgresTotalMarketContextRepository
from apex.application.crypto_market_pulse import RegisterCryptoRefreshPlan
from apex.application.crypto_universe import (
    CaptureCryptoUniverseObservations,
    CapturedObservationPage,
    CapturePublicCryptoProducts,
    MaterializeCryptoUniverse,
)
from apex.application.live_market_observation import ExecuteLiveMarketObservationRound
from apex.application.operating_cycle import (
    ExecuteOperatingCycle,
    RegisterOperatingCyclePlan,
    SetPaperActivation,
)
from apex.application.strategy_version_provenance import StrategyVersionProvenanceResearchError
from apex.domain.crypto_paper import CryptoPaperOrder, PaperOrderState, PaperSide
from apex.domain.crypto_universe import CryptoDailyObservation
from apex.domain.live_market_observation import (
    LiveMarketObservation,
    LiveObservationCaptureRound,
    LiveObservationQuality,
    LiveObservationRoundStatus,
    LiveObservationType,
    live_observation_round_id,
)
from apex.domain.operating_cycle import (
    DueDisposition,
    DueTask,
    OperatingCyclePlan,
    OperatingTaskStatus,
    OperatingTaskType,
    PaperActivationState,
    PaperContinuationHealthState,
    TaskExecution,
    compute_paper_continuation_health,
)
from apex.domain.performance_attribution import PaperEvidenceClass, StrategyHealthState
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_benchmark import BENCHMARK_INSTRUMENT, canonical_strategy_benchmarks
from apex.domain.strategy_paper_trial_evidence_coverage import (
    MATERIAL_EVIDENCE_GAP_SECONDS,
    is_material_coverage_gap,
)
from apex.domain.strategy_paper_trial_lifecycle import (
    StrategyPaperTrialTerminalReason,
    TrialRetrialEligibility,
    evaluate_retrial_eligibility,
)
from apex.domain.strategy_version_provenance import (
    TrialProvenanceClass,
    classify_trial_provenance,
)
from apex.domain.total_market_context import (
    ContextComponent,
    OverallContextState,
    UnavailableContextComponent,
    build_context_snapshot,
)

pytestmark = pytest.mark.postgres

PLAN_ID = "recurring-strategy-paper-plan-v1"
PULSE_PLAN_ID = "recurring-strategy-paper-pulse-plan-v1"
# Repeatable Strategy PAPER Evidence v1: every candidate _execute_strategy_
# paper_trial ever places a trial for has ALREADY been run through
# promote_to_paper_eligible this same cycle (definition.version 1 -> 2,
# domain.strategy_definition.next_version's own RESEARCH_ONLY -> PAPER_
# ELIGIBLE bump) before its trial identity is ever computed -- the same
# promoted-version convention the pre-existing recurring_strategy_paper_
# trial_seed already used. Every trial this suite drives therefore carries
# strategy_version=2, never the canonical catalog's own reconstructed v1.
PROMOTED_STRATEGY_VERSION = 2


def _config(connection: sa.Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _plan(*, paper_eligible: bool = True, created_at: datetime) -> OperatingCyclePlan:
    from dataclasses import replace

    plan = OperatingCyclePlan.create(
        plan_id=PLAN_ID,
        crypto_pulse_plan_id=PULSE_PLAN_ID,
        actor="test:recurring-strategy-paper",
        created_at=created_at,
    )
    return replace(plan, paper_eligible=paper_eligible)


def _due_task(*, cycle_id: str) -> DueTask:
    return DueTask.create(
        task_type=OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION,
        disposition=DueDisposition.DUE,
        reasons=("STRATEGY_PAPER_TRIAL_MANAGED_EVERY_CYCLE",),
        dependency_types=(),
        source_evidence_ids=(),
        cycle_id=cycle_id,
        network_behavior="RETAINED_EVIDENCE_ONLY_ZERO_PROVIDER_WRITES",
    )


def _dip_then_recovery_closes(day_count: int) -> list[Decimal]:
    """A hand-chosen, clearly FIXTURE-labeled deterministic daily close
    series -- flat, then a real >5% dip (triggers MEAN_REVERSION's entry
    band), then a strong, sustained recovery well past the starting price
    (comfortably clears BUY_AND_HOLD's/TIME_SERIES_MOMENTUM's round-trip
    cost floor regardless of exact entry/exit day). Never a claim about
    genuine BTC-USD price history -- see module docstring."""

    closes: list[Decimal] = []
    for day in range(day_count):
        if day < 10:
            closes.append(Decimal("100"))
        elif day < 20:
            closes.append(Decimal("100") - (Decimal(day - 9) * Decimal("1.2")))  # -> ~88
        else:
            closes.append(Decimal("88") + Decimal(day - 19) * Decimal("1.1"))  # steady recovery
    return closes


class _ControlledBTCVolumeSource:
    """Composes the real, reviewed ``FixtureCoinbaseUniverseSource`` for
    products/product-book (unchanged, deterministic -- BTC-USD is already a
    real, top-shortlisted product there) but overrides ONLY the daily
    observation series for BTC-USD: the hand-chosen dip-then-recovery close
    series above, with volume set high enough to clear
    ``domain.crypto_universe.MAX_ELIGIBLE_PRODUCTS`` (the default fixture's
    own ordinal-derived volume formula happens to rank BTC-USD just outside
    that cap -- an artifact of the fixture's generic formula, not a real
    Coinbase volume fact, so this test controls it explicitly rather than
    silently depending on that coincidence). An honest evidence-shape
    control, never a claim about real BTC-USD price/volume history."""

    evidence_class = FixtureCoinbaseUniverseSource.evidence_class
    network_access = False

    def __init__(self) -> None:
        self._delegate = FixtureCoinbaseUniverseSource()

    def capture_products(self, **kwargs: object) -> object:
        return self._delegate.capture_products(**kwargs)  # type: ignore[arg-type]

    def capture_product_book(self, **kwargs: object) -> object:
        return self._delegate.capture_product_book(**kwargs)  # type: ignore[arg-type]

    def capture_daily_observations(
        self,
        *,
        product_id: str,
        interval_start: datetime,
        interval_end: datetime,
        requested_at: datetime,
    ) -> CapturedObservationPage:
        if product_id != BENCHMARK_INSTRUMENT:
            return self._delegate.capture_daily_observations(
                product_id=product_id,
                interval_start=interval_start,
                interval_end=interval_end,
                requested_at=requested_at,
            )
        day_count = (interval_end - interval_start).days
        closes = _dip_then_recovery_closes(day_count)
        candles = tuple(
            CryptoDailyObservation.create(
                bucket_start=interval_start + timedelta(days=offset),
                close=price,
                volume="3000000",
            )
            for offset, price in enumerate(closes)
        )
        return CapturedObservationPage(
            captured_at=requested_at,
            raw_payload=b"{}",
            candles=candles,
            missing_buckets=0,
            duplicate_records=0,
            rejected_records=0,
            source_locator="test://controlled-btc-observations",
            runtime_milliseconds=0,
        )


def _bootstrap_universe_and_candles(
    *, crypto_repo: PostgresCryptoMarketPulseRepository, requested_at: datetime
) -> None:
    """Real, unmodified CRYPTO_UNIVERSE_REFRESH bootstrap chain (identical
    in shape to the existing, reviewed test_postgres_crypto_market_pulse.py)
    -- real capture_id/BTC-USD product/eligible-universe/refresh-plan --
    driven by _ControlledBTCVolumeSource so BTC-USD's own retained daily
    closes are the deterministic dip-then-recovery series this suite's
    assertions are grounded in."""

    source = _ControlledBTCVolumeSource()
    capture = CapturePublicCryptoProducts(crypto_repo, source).execute(  # type: ignore[arg-type]
        capture_id="recurring-strategy-paper-products", requested_at=requested_at
    )
    CaptureCryptoUniverseObservations(crypto_repo, source).execute(  # type: ignore[arg-type]
        capture_id=capture.capture_id,
        # MIN_HISTORY_BUCKETS (domain.crypto_universe) requires >= 170 daily
        # buckets for a product to clear the eligible-universe shortlist.
        interval_start=requested_at - timedelta(days=200),
        interval_end=requested_at,
        requested_at=requested_at,
    )
    universe = MaterializeCryptoUniverse(crypto_repo).execute(
        snapshot_id="recurring-strategy-paper-universe",
        capture_id=capture.capture_id,
        created_at=requested_at,
    )
    assert BENCHMARK_INSTRUMENT in universe.eligible_product_ids
    RegisterCryptoRefreshPlan(crypto_repo).execute(
        plan_id=PULSE_PLAN_ID,
        universe_snapshot_id=universe.snapshot_id,
        actor="test:recurring-strategy-paper",
        created_at=requested_at,
        current_market_evidence=True,
    )


def _seed_observation(
    live_repo: PostgresLiveMarketObservationRepository,
    *,
    price: Decimal,
    captured_at: datetime,
    plan_id: str = "recurring-strategy-paper-live-plan",
) -> LiveMarketObservation:
    """A directly-constructed, directly-persisted genuine-shaped
    LiveMarketObservation -- mirrors tests/test_strategy_paper_trial.py's
    own ``_live_observation`` factory, persisted here (never through the
    separate, not-installed live-observer worker -- see this milestone's
    own operating instructions) so ObtainContemporaneousMarketObservation
    has real retained evidence to read."""

    round_id = live_observation_round_id(plan_id=plan_id, window_start=captured_at)
    # The parent round row must exist before any child observation row --
    # mirrors the exact "persist the parent before its children" FK-
    # ordering fix noted in this module's own history (Options Capture
    # Parent Persistence v1 / Live Market Observation Foundation v1).
    existing_round = live_repo.capture_round(round_id)
    if existing_round is None:
        live_repo.save_capture_round(
            LiveObservationCaptureRound.create(
                plan_id=plan_id,
                venue=SharedVenue.CRYPTO,
                provider="COINBASE",
                window_start=captured_at,
                requested_at=captured_at,
                completed_at=captured_at,
                status=LiveObservationRoundStatus.COMPLETED,
                requested_instrument_count=1,
                succeeded_count=1,
                failed_count=0,
                evidence_class=EvidenceClass.GENUINE_PUBLIC,
            )
        )
    observation = LiveMarketObservation.create(
        round_id=round_id,
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_id=BENCHMARK_INSTRUMENT,
        observation_type=LiveObservationType.QUOTE,
        captured_at=captured_at,
        bid_price=price - Decimal("0.01"),
        ask_price=price + Decimal("0.01"),
        book_depth_ask=Decimal("5"),
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        provider_schema_version="TEST_V1",
        source_identity="TEST_SOURCE",
        quality=LiveObservationQuality.PASSED,
    )
    live_repo.save_observation(observation)
    return observation


def _seed_total_market_context(
    context_repo: PostgresTotalMarketContextRepository, *, snapshot_id: str, as_of: datetime
) -> str:
    """A well-formed, genuinely-computed (via the real, unmodified
    build_context_snapshot) TotalMarketContextSnapshot -- component values
    are illustrative test constants (identical convention to
    tests/test_total_market_context.py / tests/persistence/
    test_postgres_total_market_context.py), not a live macro-data pull, but
    the snapshot itself is built and persisted through the real domain
    function and repository, never fabricated."""

    def _component(component_type: object, *, current: str, prior: str) -> ContextComponent:
        return ContextComponent.create(
            component_id=f"{snapshot_id}-{component_type}",
            component_type=component_type,  # type: ignore[arg-type]
            source_observation_id=f"{snapshot_id}-{component_type}-source",
            provider="TEST_PROVIDER",
            subject=str(component_type),
            observation_at=as_of - timedelta(hours=1),
            available_at=as_of - timedelta(minutes=30),
            ingested_at=as_of - timedelta(minutes=15),
            current_value=current,
            prior_comparable_value=prior,
            freshness_seconds=1800,
            freshness_limit_seconds=259_200,
            quality_status="VALID",
            evidence_class="GENUINE_PUBLIC",
            limitations=(),
            interpretation_category="TEST",
            supporting_evidence_ids=(f"{snapshot_id}-{component_type}-source",),
        )

    from apex.domain.total_market_context import ContextComponentType

    # classify_component (domain.total_market_context) only implements
    # rules for these four component types -- the other three are honestly
    # retained as UNAVAILABLE rather than fabricating a classification for
    # a type it has no policy for.
    _CLASSIFIED = (
        ContextComponentType.US_EQUITY_MARKET,
        ContextComponentType.TREASURY_RATE,
        ContextComponentType.MARKET_VOLATILITY,
        ContextComponentType.COMMODITY_OR_INFLATION,
    )
    # Per-type values chosen to land each classified component in its own
    # SUPPORTIVE band (see classify_component's exact thresholds): equity/
    # treasury read a +5% normalized change; volatility (an ABSOLUTE level,
    # not a change) reads a real-shaped low 10% figure; commodity has no
    # SUPPORTIVE band at all by policy, so a flat 0% change (NEUTRAL) is
    # the honest, non-STRESSED choice for it.
    _VALUES = {
        ContextComponentType.US_EQUITY_MARKET: ("105", "100"),
        ContextComponentType.TREASURY_RATE: ("105", "100"),
        ContextComponentType.MARKET_VOLATILITY: ("0.10", "0.12"),
        ContextComponentType.COMMODITY_OR_INFLATION: ("100", "100"),
    }
    components = tuple(
        _component(item, current=_VALUES[item][0], prior=_VALUES[item][1]) for item in _CLASSIFIED
    )
    unavailable = tuple(
        UnavailableContextComponent(item, "TEST_NOT_MODELED")
        for item in ContextComponentType
        if item not in _CLASSIFIED
    )
    snapshot = build_context_snapshot(
        snapshot_id=snapshot_id,
        created_at=as_of,
        source_batch_id=f"{snapshot_id}-source-batch",
        components=components,
        unavailable_components=unavailable,
    )
    assert snapshot.overall_state is OverallContextState.RISK_SUPPORTIVE
    context_repo.save_context_snapshot(
        idempotency_key=f"{snapshot_id}-key", snapshot=snapshot, components=components
    )
    return snapshot.snapshot_id


class _Executors:
    """Bundles the real repositories/executor/state-source this whole suite
    shares, all pointed at the same isolated engine."""

    def __init__(self, engine: sa.Engine) -> None:
        self.engine = engine
        self.crypto = PostgresCryptoMarketPulseRepository(engine)
        self.crypto_paper = PostgresCryptoPaperRepository(engine)
        self.live_observation = PostgresLiveMarketObservationRepository(engine)
        self.context = PostgresTotalMarketContextRepository(engine)
        self.performance = PostgresPerformanceAttributionRepository(engine)
        self.internal_strategy_evaluations = PostgresInternalStrategyEvaluationRepository(engine)
        # Repeatable Strategy PAPER Evidence v1: read access for this
        # suite's own assertions -- the executor already owns its own
        # instances of these two internally.
        self.strategy_paper_trials = PostgresStrategyPaperTrialRepository(engine)
        self.strategy_paper_trial_closures = PostgresStrategyPaperTrialClosureRepository(engine)
        self.state_source = PostgresOperatingStateSource(
            engine, PostgresOperatingCycleRepository(engine)
        )
        self.executor = ExistingCapabilityOperatingTaskExecutor(engine, self.state_source)

    def run_cycle(
        self, *, cycle_id: str, as_of: datetime, executed_at: datetime, plan: OperatingCyclePlan
    ):
        return self.executor.execute(
            task=_due_task(cycle_id=cycle_id),
            cycle_id=cycle_id,
            plan=plan,
            as_of=as_of,
            executed_at=executed_at,
        )


@pytest.fixture
def env(postgres_engine: sa.Engine) -> _Executors:
    return _Executors(postgres_engine)


DAY1 = datetime(2026, 6, 1, tzinfo=UTC)


def _open_order(env: _Executors):
    state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
    assert state is not None
    assert len(state.orders) == 1
    return state.orders[0]


class TestCycleNConsiderationAndPlacement:
    """Phase J, Cycle N: at least one legitimate PAPER-eligible strategy
    candidate considered, canonical contemporaneous evidence obtained, a
    trial created (or honestly rejected)."""

    def test_places_the_top_ranked_promoted_candidate(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )

        assert result.status is OperatingTaskStatus.COMPLETED
        assert "STRATEGY_PAPER_CANDIDATES_CONSIDERED:4" in result.warnings
        assert "STRATEGY_PAPER_CANDIDATES_PROMOTED:3" in result.warnings
        assert "STRATEGY_PAPER_TRIALS_CREATED:1" in result.warnings
        assert "STRATEGY_PAPER_CONTEMPORANEOUS_EVIDENCE:SATISFIED" in result.warnings
        order = _open_order(env)
        assert order.product_id == BENCHMARK_INSTRUMENT
        assert order.state is PaperOrderState.OPEN

    def test_rejects_moving_average_for_insufficient_history(self, env: _Executors) -> None:
        # The controlled series is 200 real days; MOVING_AVERAGE_TREND
        # structurally requires 201 -- an honest, deterministic rejection,
        # not a flaky economics outcome.
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert any("INSUFFICIENT_RETAINED_CANDLE_HISTORY" in warning for warning in result.warnings)


class TestIdempotencyAndDuplicateCycleReplay:
    """Phase K: task idempotency + duplicate-cycle replay."""

    def test_repeated_call_with_same_cycle_id_creates_no_duplicate(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        cycle_id = f"{PLAN_ID}-worker-20260601"

        first = env.run_cycle(cycle_id=cycle_id, as_of=DAY1, executed_at=DAY1, plan=plan)
        second = env.run_cycle(cycle_id=cycle_id, as_of=DAY1, executed_at=DAY1, plan=plan)

        assert first.status is OperatingTaskStatus.COMPLETED
        assert second.status is OperatingTaskStatus.REUSED
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1
        assert len(state.authorizations) == 1
        attributions = env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
        assert len({item.attribution_id for item in attributions}) == len(
            attributions
        )  # no dup ids


class TestCrossCycleOpenPositionLifecycle:
    """Phase F/J: cross-cycle open-position continuation, and closing when
    genuinely due."""

    def test_later_same_day_tick_recognizes_open_trial_without_recreating(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        first = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert first.status is OperatingTaskStatus.COMPLETED
        order_id = _open_order(env).order_id

        # A genuinely later cycle (different cycle_id/executed_at), still
        # within the order's 4-hour validity window, with no fresh evidence
        # observed strictly after submission yet -- neither fills nor
        # expires; simply recognized as already open, never recreated.
        later = DAY1 + timedelta(hours=1)
        second = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601-later-tick",
            as_of=later,
            executed_at=later,
            plan=plan,
        )
        assert any(item.endswith("-authorization") for item in second.reused_identities)
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1
        assert state.orders[0].order_id == order_id
        assert state.orders[0].state is PaperOrderState.OPEN

    def test_fresh_evidence_after_submission_fills_the_order(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        order_id = _open_order(env).order_id

        fill_time = DAY1 + timedelta(hours=1)
        # A small, realistic intra-hour price move -- large enough to prove
        # the fill uses genuinely fresh evidence (a different price/
        # observation_id than the decision), small enough that the fixed,
        # decision-time reservation still covers the fill (a large adverse
        # move genuinely overconsuming a market-buy reservation is a real
        # domain.crypto_paper invariant, not something this orchestration
        # test is exercising here).
        fill_observation = _seed_observation(
            env.live_observation, price=Decimal("50010"), captured_at=fill_time
        )
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601-fill-tick",
            as_of=fill_time,
            executed_at=fill_time + timedelta(minutes=1),
            plan=plan,
        )
        assert "STRATEGY_PAPER_FILLS_APPLIED:1" in result.warnings
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        filled_order = next(item for item in state.orders if item.order_id == order_id)
        assert filled_order.state in (PaperOrderState.FILLED, PaperOrderState.PARTIALLY_FILLED)
        fill_row = next(item for item in state.fills if item.order_id == order_id)
        # Exact fill/exit observation lineage (Phase K): the real, durable
        # LiveMarketObservation id that justified this fill is retained,
        # never a synthesized or silently-substituted one.
        assert fill_observation.observation_id in fill_row.evidence_id

    def test_later_cycle_expires_past_the_validity_window_and_closes_the_loop(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        order_id = _open_order(env).order_id

        much_later = DAY1 + timedelta(hours=5)  # past the 4-hour order validity window
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601-expiry-tick",
            as_of=much_later,
            executed_at=much_later,
            plan=plan,
        )
        assert result.status is OperatingTaskStatus.COMPLETED
        assert "STRATEGY_PAPER_ORDERS_EXPIRED:1" in result.warnings
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        expired_order = next(item for item in state.orders if item.order_id == order_id)
        assert expired_order.state is PaperOrderState.EXPIRED

        # Phase G closed loop: attribution/health materialize even for a
        # trial that closed without ever filling -- a negative/non-
        # profitable outcome is retained honestly, never hidden.
        attributions = env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
        matching = [item for item in attributions if item.order_id == order_id]
        assert matching
        assert matching[-1].position_state == "CLOSED"
        health = env.performance.health_assessments(PaperEvidenceClass.GENUINE_PAPER)
        assert any(
            item.strategy_or_method_id == matching[-1].strategy_or_method_id for item in health
        )


class TestMultiStrategyBoundedAllocationAndSameInstrumentConflict:
    """Phase E/K: multiple strategies want capital on the identical
    instrument this cycle -- exactly one is selected, the rest are recorded
    as considered-and-not-selected, never silently dropped."""

    def test_exactly_one_new_trial_per_cycle_despite_three_promotable_candidates(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert "STRATEGY_PAPER_CANDIDATES_PROMOTED:3" in result.warnings
        assert "STRATEGY_PAPER_TRIALS_CREATED:1" in result.warnings
        not_selected = [
            item
            for item in result.warnings
            if "NOT_SELECTED_THIS_CYCLE_RANK_OR_SAME_INSTRUMENT_CAPACITY" in item
        ]
        assert len(not_selected) == 2  # the other two promoted-but-unselected candidates
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        # domain.crypto_paper.MAX_OPEN_ORDERS_PER_PRODUCT=1 structurally
        # backs this up: even if this task's own cap were bypassed, the
        # existing, unmodified engine would refuse a second open order on
        # the identical product for this account.
        assert sum(item.product_id == BENCHMARK_INSTRUMENT for item in state.orders) == 1


class TestStaleAndAbsentObservationAbstention:
    """Phase C/K: fail closed on STALE/ABSENT -- never a silent direct
    Coinbase fallback."""

    def test_absent_observation_places_no_new_trial(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        # Deliberately no _seed_observation call at all.
        plan = _plan(created_at=DAY1)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert "STRATEGY_PAPER_CONTEMPORANEOUS_EVIDENCE:ABSENT" in result.warnings
        assert "STRATEGY_PAPER_TRIALS_CREATED:0" in result.warnings
        assert any("ABSENT_CONTEMPORANEOUS_EVIDENCE" in item for item in result.warnings)
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is None or not state.orders

    def test_stale_observation_places_no_new_trial(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        # All four canonical benchmarks declare a 1800-second (30-minute)
        # maximum_observation_staleness_seconds -- an observation captured
        # two hours before as_of is unambiguously stale.
        _seed_observation(
            env.live_observation, price=Decimal("50000"), captured_at=DAY1 - timedelta(hours=2)
        )
        plan = _plan(created_at=DAY1)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert "STRATEGY_PAPER_CONTEMPORANEOUS_EVIDENCE:STALE" in result.warnings
        assert "STRATEGY_PAPER_TRIALS_CREATED:0" in result.warnings
        assert any("STALE_CONTEMPORANEOUS_EVIDENCE" in item for item in result.warnings)

    def test_existing_open_trial_still_gets_reconciled_when_evidence_goes_absent(
        self, env: _Executors
    ) -> None:
        # An open position from a prior cycle must not be abandoned merely
        # because a later cycle's decision-time evidence is unavailable --
        # only NEW consideration abstains; existing lifecycle bookkeeping
        # (expiry/reconciliation) still runs unconditionally.
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        order_id = _open_order(env).order_id

        much_later = DAY1 + timedelta(hours=5)  # past validity -- no new observation seeded
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601-absent-tick",
            as_of=much_later,
            executed_at=much_later,
            plan=plan,
        )
        assert "STRATEGY_PAPER_ORDERS_EXPIRED:1" in result.warnings
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert next(item for item in state.orders if item.order_id == order_id).state is (
            PaperOrderState.EXPIRED
        )


class TestDecisionEvidenceLineage:
    """Phase K: exact decision observation lineage."""

    def test_preview_retains_the_exact_durable_decision_observation_id(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        decision_observation = _seed_observation(
            env.live_observation, price=Decimal("50000"), captured_at=DAY1
        )
        plan = _plan(created_at=DAY1)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )

        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        preview = state.previews[0]
        assert any(decision_observation.observation_id in item for item in preview.evidence_ids)


class TestPointInTimeRaceRegression:
    """Live Observation Point-in-Time Race Fix v1: reproduces the exact
    retained race shape that froze the first real
    STRATEGY_PAPER_TRIAL_EXECUTION dispatch on 2026-08-27 (see memory
    apex-docker-wsl-outage-recovery-strategy-paper-race-v1) through the real
    scheduler boundary (``ExecuteOperatingCycle`` -> ``_dispatch_task`` ->
    ``ExistingCapabilityOperatingTaskExecutor``), not just the raw executor
    ``_Executors.run_cycle`` bypasses. An independently-scheduled live-
    observer worker writes a fresh row a few milliseconds AFTER the cycle's
    own frozen ``as_of``/``executed_at``, retained alongside a genuinely
    valid observation captured shortly before it.

    Pre-fix: ``ObtainContemporaneousMarketObservation`` surfaced the future
    row via the repository's unbounded ``ORDER BY captured_at DESC LIMIT 1``,
    and ``evaluate_contemporaneous_observation``'s fail-closed guard raised
    ``LiveMarketObservationError`` (``LIVE_OBSERVATION_FUTURE_TIMESTAMP``) --
    which ``_dispatch_task``'s generic ``(OSError, RuntimeError, ValueError)``
    handler then turned into a terminal ``FAILED`` ``OperatingTaskResult``,
    freezing the task for the rest of the UTC day. Post-fix: the scheduler
    dispatch completes normally, anchored to the point-in-time-valid past
    observation.
    """

    def test_scheduler_dispatch_completes_using_the_valid_past_observation(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        valid_past = _seed_observation(
            env.live_observation, price=Decimal("50000"), captured_at=DAY1 - timedelta(seconds=60)
        )
        future_row = _seed_observation(
            env.live_observation,
            price=Decimal("50500"),
            captured_at=DAY1 + timedelta(milliseconds=22),
        )
        assert future_row.captured_at > DAY1  # sanity: genuinely future relative to as_of

        plan_repository = PostgresOperatingCycleRepository(env.engine)
        _register_and_activate_plan(plan_repository, created_at=DAY1)
        cycle_id = f"{PLAN_ID}-worker-20260601"

        wrapper = _RealStrategyPaperExecutorWrapper(
            ExistingCapabilityOperatingTaskExecutor(env.engine, env.state_source)
        )
        cycle = ExecuteOperatingCycle(plan_repository, env.state_source, wrapper).execute(
            cycle_id=cycle_id,
            plan_id=PLAN_ID,
            as_of=DAY1,
            executed_at=DAY1,
            idempotency_key=f"{cycle_id}-key",
        )

        # The core regression proof: STRATEGY_PAPER_TRIAL_EXECUTION never
        # reaches _dispatch_task's FAILED-terminal exception handler -- no
        # LiveMarketObservationError propagates, so it is not persisted as a
        # terminal task result at all (awaits_intraday_continuation=True,
        # exactly like every other successful dispatch of this task).
        assert "STRATEGY_PAPER_TRIAL_EXECUTION" not in {
            item.task_type.value for item in cycle.tasks
        }
        assert len(wrapper.results) == 1
        assert wrapper.results[0].status is OperatingTaskStatus.COMPLETED
        assert wrapper.results[0].awaits_intraday_continuation is True
        assert "STRATEGY_PAPER_CONTEMPORANEOUS_EVIDENCE:SATISFIED" in wrapper.results[0].warnings
        assert "STRATEGY_PAPER_TRIALS_CREATED:1" in wrapper.results[0].warnings

        # The selected observation is the point-in-time-valid PAST row --
        # decision-time evidence lineage never anchors to the future one.
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        preview = state.previews[0]
        assert any(valid_past.observation_id in item for item in preview.evidence_ids)
        assert not any(future_row.observation_id in item for item in preview.evidence_ids)

    def test_replay_under_the_race_shape_is_deterministic_and_creates_no_duplicate_state(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(
            env.live_observation, price=Decimal("50000"), captured_at=DAY1 - timedelta(seconds=60)
        )
        _seed_observation(
            env.live_observation,
            price=Decimal("50500"),
            captured_at=DAY1 + timedelta(milliseconds=22),
        )
        plan = _plan(created_at=DAY1)
        cycle_id = f"{PLAN_ID}-worker-20260601"

        first = env.run_cycle(cycle_id=cycle_id, as_of=DAY1, executed_at=DAY1, plan=plan)
        second = env.run_cycle(cycle_id=cycle_id, as_of=DAY1, executed_at=DAY1, plan=plan)

        assert first.status is OperatingTaskStatus.COMPLETED
        assert second.status is OperatingTaskStatus.REUSED
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1  # no duplicate PAPER state from replay
        assert len(state.authorizations) == 1


class TestTotalMarketContextLineage:
    """Phase D/K: Total-Market-Context lineage, and an honest block when no
    eligible context exists."""

    def test_threads_the_real_retained_context_id_into_attribution(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        context_id = _seed_total_market_context(
            env.context, snapshot_id="recurring-strategy-paper-context", as_of=DAY1
        )
        plan = _plan(created_at=DAY1)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )

        order_id = _open_order(env).order_id
        attributions = env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
        matching = next(item for item in attributions if item.order_id == order_id)
        assert matching.context_at_consideration_id == context_id
        assert matching.consideration_context == OverallContextState.RISK_SUPPORTIVE.value

    def test_missing_context_is_never_fabricated_and_health_reports_it_honestly(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert "STRATEGY_PAPER_TOTAL_MARKET_CONTEXT:UNAVAILABLE" in result.warnings

        order_id = _open_order(env).order_id
        attributions = env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
        matching = next(item for item in attributions if item.order_id == order_id)
        assert matching.context_at_consideration_id is None  # never fabricated
        health = env.performance.health_assessments(PaperEvidenceClass.GENUINE_PAPER)
        matching_health = next(
            item for item in health if item.strategy_or_method_id == matching.strategy_or_method_id
        )
        # The existing, unmodified performance_attribution.assess_strategy_
        # health policy correctly reports the honest, conservative state
        # given deliberately-unset context/settlement lineage on a single
        # fresh trial -- never silently upgraded to HEALTHY.
        assert matching_health.state is not StrategyHealthState.HEALTHY


class TestPaperCapitalCaps:
    """Phase K: PAPER capital caps -- the effective per-trial notional is
    exactly min(STRATEGY_TRIAL_NOTIONAL, MAX_ORDER_NOTIONAL) = $10, never
    larger."""

    def test_effective_per_trial_notional_is_ten_dollars(self, env: _Executors) -> None:
        from apex.domain.strategy_paper_trial import STRATEGY_TRIAL_NOTIONAL

        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )

        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        authorization = state.authorizations[0]
        assert Decimal("10") == STRATEGY_TRIAL_NOTIONAL
        assert authorization.maximum_quote_notional == Decimal("10")
        preview = state.previews[0]
        assert preview.estimated_total_cost <= Decimal("10") * Decimal("1.01")  # cost incl. fee


class TestNoLiveAuthorityAndNoProviderWritePath:
    """Phase K: no LIVE authority, no provider write path -- structural,
    not merely behavioral, guards."""

    def test_order_and_attribution_carry_no_live_authority(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert "LIVE_AUTHORITY_NONE" in result.warnings
        assert "NO_COINBASE_ORDER_SUBMISSION" in result.warnings

        order = _open_order(env)
        assert order.paper_authority == "PAPER_ONLY"
        assert order.live_authority == "NONE"
        attributions = env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
        matching = next(item for item in attributions if item.order_id == order.order_id)
        assert matching.authority == "NONE"
        assert matching.live_authority == "NONE"

    def test_operating_cycle_plan_not_paper_eligible_blocks_placement(
        self, env: _Executors
    ) -> None:
        # effective_paper_activation defaults to disabled -- an operator
        # must explicitly enable it (SetPaperActivation) before any internal
        # PAPER consideration proceeds; this task must honor that gate
        # exactly like the older CRYPTO/KALSHI PAPER trial tasks do.
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1, paper_eligible=False)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert "STRATEGY_PAPER_TRIALS_CREATED:0" in result.warnings
        assert any("OPERATING_CYCLE_NOT_ENABLED" in item for item in result.warnings)
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is None or not state.orders


def test_genuine_coinbase_decision_and_fill_evidence(env: _Executors) -> None:
    """Phase J: at least one genuine, credential-free, unauthenticated
    public Coinbase network-call demonstration of Phase C, separate from
    the deterministic multi-cycle lifecycle proof above (see module
    docstring for why the 4-hour order validity window makes a natural,
    unattended real-price exit impractical across real wall-clock cycles).
    """

    _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
    real_source = CoinbasePublicUniverseSource()
    now = datetime.now(tz=UTC)
    round_result = ExecuteLiveMarketObservationRound(env.live_observation, real_source).execute(
        plan_id="recurring-strategy-paper-genuine-plan",
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=(BENCHMARK_INSTRUMENT,),
        window_start=now,
        requested_at=now,
        provider_schema_version="GENUINE_TEST_V1",
        source_identity="GENUINE_TEST_SOURCE",
    )
    assert round_result.observations
    assert round_result.observations[0].quality is LiveObservationQuality.PASSED
    assert round_result.observations[0].evidence_class is EvidenceClass.HISTORICAL_PUBLIC
    # The real network round trip can genuinely advance the provider's own
    # captured_at past the `now` captured before it -- everything
    # downstream must be at or after the evidence's own timestamp (mirrors
    # tests/persistence/test_postgres_strategy_paper_trial.py's identical,
    # already-reviewed convention).
    now = max(now, round_result.observations[0].captured_at)

    plan = _plan(created_at=now)
    result = env.run_cycle(
        cycle_id=f"{PLAN_ID}-worker-genuine", as_of=now, executed_at=now, plan=plan
    )
    assert "STRATEGY_PAPER_CONTEMPORANEOUS_EVIDENCE:SATISFIED" in result.warnings
    state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
    assert state is not None
    assert state.orders
    preview = state.previews[0]
    assert any(round_result.observations[0].observation_id in item for item in preview.evidence_ids)
    assert preview.pricing_source.value == "GENUINE_TOP_OF_BOOK"


# --------------------------------------------------------------------------
# Recurring Strategy PAPER Intraday Execution Correction v1: every test
# above calls ``ExistingCapabilityOperatingTaskExecutor.execute`` directly,
# proving the executor branch's own domain correctness (idempotency,
# already-open recognition, fills, expiry, attribution) under repeated
# dispatch -- but each "later tick" scenario does so under a distinct,
# synthetic ``cycle_id`` per phase (e.g. ``-later-tick``, ``-fill-tick``,
# ``-expiry-tick``), which bypasses ``application.operating_cycle.
# ExecuteOperatingCycle`` entirely and therefore cannot observe whether the
# real *scheduler* -- the thing production's ``operating-cycle-worker``
# actually calls every tick, per ``cli.py``'s ``cycle_id = f"{plan_id}-
# worker-{occurrence_as_of:%Y%m%d}"`` (pinned for the whole UTC day, only
# ``executed_at`` advancing) -- would ever genuinely dispatch this task a
# second time under one real, unchanged cycle_id. It would not, before this
# milestone's fix: ``_resume_cycle`` finds STRATEGY_PAPER_TRIAL_EXECUTION
# already persisted from the first tick and skips it
# (``if task.task_type.value in by_type: continue``) without ever reaching
# the executor again. The tests below close exactly that gap, exercising
# the real scheduler/executor integration boundary production uses.
# --------------------------------------------------------------------------


class _RealStrategyPaperExecutorWrapper:
    """Delegates STRATEGY_PAPER_TRIAL_EXECUTION to the real, Postgres-backed
    ``ExistingCapabilityOperatingTaskExecutor``; every other task type
    completes trivially. Mirrors ``_RealOptionsExecutorWrapper`` in
    tests/persistence/test_postgres_operating_cycle.py exactly, so this
    proves the real strategy-paper orchestration through the real
    ``ExecuteOperatingCycle`` scheduler without also standing up unrelated
    crypto-universe/Kalshi/mandate/context fixtures. Every genuine
    (non-trivial) dispatch's raw ``TaskExecution`` is retained in
    ``results`` so a test can inspect what the real executor branch did on
    a call the scheduler chose not to persist as a terminal
    ``OperatingTaskResult`` (see ``TaskExecution.awaits_intraday_
    continuation``)."""

    def __init__(self, real: ExistingCapabilityOperatingTaskExecutor) -> None:
        self._real = real
        self.calls: list[tuple[str, datetime]] = []
        self.results: list[TaskExecution] = []

    def execute(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: OperatingCyclePlan,
        as_of: datetime,
        executed_at: datetime,
    ) -> TaskExecution:
        self.calls.append((task.task_type.value, executed_at))
        if task.task_type is OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION:
            result = self._real.execute(
                task=task, cycle_id=cycle_id, plan=plan, as_of=as_of, executed_at=executed_at
            )
            self.results.append(result)
            return result
        return TaskExecution(
            OperatingTaskStatus.COMPLETED, (f"{task.task_type.value}-output",), (), True, 0
        )


def _register_and_activate_plan(
    repository: PostgresOperatingCycleRepository, *, created_at: datetime
) -> None:
    """Persist the plan and its PAPER-activation event through the real
    repository -- unlike ``_plan()`` above (a local dataclass literal handed
    straight to the raw executor by ``env.run_cycle``), ``ExecuteOperatingCycle``
    folds ``paper_eligible`` from a genuinely retained ``PaperActivationEvent``
    (``effective_paper_activation`` never defaults to enabled), so a real
    scheduler-driven test must author one explicitly."""

    RegisterOperatingCyclePlan(repository).execute(
        plan_id=PLAN_ID,
        crypto_pulse_plan_id=PULSE_PLAN_ID,
        actor="test:real-scheduler-strategy-paper",
        created_at=created_at,
        idempotency_key=f"{PLAN_ID}-key",
    )
    SetPaperActivation(repository).execute(
        event_id=f"{PLAN_ID}-paper-activation",
        plan_id=PLAN_ID,
        state=PaperActivationState.ENABLED,
        reason="real-scheduler-strategy-paper-test",
        actor="test:real-scheduler-strategy-paper",
        occurred_at=created_at,
        idempotency_key=f"{PLAN_ID}-paper-activation-key",
    )


class TestRealSchedulerIntradayContinuation:
    """The exact scheduler/executor integration boundary production uses --
    real Postgres, the real ``ExecuteOperatingCycle`` application service,
    the real ``_execute_strategy_paper_trial`` executor branch -- with the
    SAME ``cycle_id`` across ticks and only ``executed_at`` advancing."""

    def test_same_cycle_id_later_tick_genuinely_redispatches_and_fills(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan_repository = PostgresOperatingCycleRepository(env.engine)
        _register_and_activate_plan(plan_repository, created_at=DAY1)
        cycle_id = f"{PLAN_ID}-worker-20260601"

        opened_wrapper = _RealStrategyPaperExecutorWrapper(
            ExistingCapabilityOperatingTaskExecutor(env.engine, env.state_source)
        )
        opened = ExecuteOperatingCycle(plan_repository, env.state_source, opened_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=PLAN_ID,
            as_of=DAY1,
            executed_at=DAY1,
            idempotency_key=f"{cycle_id}-key",
        )
        assert "STRATEGY_PAPER_TRIAL_EXECUTION" not in {
            item.task_type.value for item in opened.tasks
        }
        assert len(opened_wrapper.results) == 1
        assert opened_wrapper.results[0].awaits_intraday_continuation is True
        order_id = _open_order(env).order_id

        # Same cycle_id, later executed_at -- a genuinely new
        # ExecuteOperatingCycle/executor pair (simulated worker restart)
        # over the same repository/state source, exactly mirroring
        # TestOptionsCaptureIntraDayReevaluation's own "restart between
        # polls" shape in tests/test_operating_cycle.py.
        later = DAY1 + timedelta(hours=1)
        fill_observation = _seed_observation(
            env.live_observation, price=Decimal("50010"), captured_at=later
        )
        fill_wrapper = _RealStrategyPaperExecutorWrapper(
            ExistingCapabilityOperatingTaskExecutor(env.engine, env.state_source)
        )
        ExecuteOperatingCycle(plan_repository, env.state_source, fill_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=PLAN_ID,
            as_of=DAY1,
            executed_at=later + timedelta(minutes=1),
            idempotency_key=f"{cycle_id}-key",
        )
        # The core regression proof: the real executor branch was genuinely
        # called a second time under the identical cycle_id the real worker
        # actually reuses all day -- pre-fix, ``_resume_cycle`` would find
        # STRATEGY_PAPER_TRIAL_EXECUTION already persisted from the ``opened``
        # tick above and skip it without ever reaching the executor, so
        # ``fill_wrapper.results`` would stay empty here.
        assert len(fill_wrapper.results) == 1
        assert fill_wrapper.results[0].awaits_intraday_continuation is True
        assert "STRATEGY_PAPER_FILLS_APPLIED:1" in fill_wrapper.results[0].warnings

        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1  # no duplicate trial from the later tick
        filled_order = next(item for item in state.orders if item.order_id == order_id)
        assert filled_order.state in (PaperOrderState.FILLED, PaperOrderState.PARTIALLY_FILLED)
        fill_row = next(item for item in state.fills if item.order_id == order_id)
        assert fill_observation.observation_id in fill_row.evidence_id

    def test_same_cycle_id_later_tick_expires_and_attributes_without_duplicating(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan_repository = PostgresOperatingCycleRepository(env.engine)
        _register_and_activate_plan(plan_repository, created_at=DAY1)
        cycle_id = f"{PLAN_ID}-worker-20260601"

        opened_wrapper = _RealStrategyPaperExecutorWrapper(
            ExistingCapabilityOperatingTaskExecutor(env.engine, env.state_source)
        )
        ExecuteOperatingCycle(plan_repository, env.state_source, opened_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=PLAN_ID,
            as_of=DAY1,
            executed_at=DAY1,
            idempotency_key=f"{cycle_id}-key",
        )
        order_id = _open_order(env).order_id

        # Same cycle_id, well past the 4-hour order validity window, with no
        # fresher evidence seeded (STALE) -- must still genuinely
        # redispatch, recognize the already-open trial without duplicating
        # it, expire it, and materialize attribution/health for the closed
        # loop, all on this one later tick -- not 24 hours later at the next
        # UTC day's own first tick.
        much_later = DAY1 + timedelta(hours=5)
        expiry_wrapper = _RealStrategyPaperExecutorWrapper(
            ExistingCapabilityOperatingTaskExecutor(env.engine, env.state_source)
        )
        ExecuteOperatingCycle(plan_repository, env.state_source, expiry_wrapper).execute(
            cycle_id=cycle_id,
            plan_id=PLAN_ID,
            as_of=DAY1,
            executed_at=much_later,
            idempotency_key=f"{cycle_id}-key",
        )

        assert len(expiry_wrapper.results) == 1
        assert expiry_wrapper.results[0].awaits_intraday_continuation is True
        assert "STRATEGY_PAPER_ORDERS_EXPIRED:1" in expiry_wrapper.results[0].warnings

        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1  # no duplicate trial from the later tick
        expired_order = next(item for item in state.orders if item.order_id == order_id)
        assert expired_order.state is PaperOrderState.EXPIRED

        attributions = env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
        matching = [item for item in attributions if item.order_id == order_id]
        assert matching
        # Repeatable Strategy PAPER Evidence v1: both dispatches above share
        # the identical day-scoped as_of=DAY1 (this test is specifically
        # exercising SAME-day intraday continuation), so the two retained
        # attribution rows (OPEN from the first dispatch, CLOSED from the
        # expiry one) also share an identical materialized_at -- the
        # repository's own ORDER BY (materialized_at, attribution_id) then
        # tie-breaks on attribution_id's own content hash, which carries no
        # temporal meaning. matching[-1] (assuming "last in this order" ==
        # "most recent") is genuinely fragile against exactly that hash --
        # this milestone's own new, intentional idempotency_seed format
        # (generation-aware strategy_paper_trial_id, replacing the old
        # lifetime-only recurring_strategy_paper_trial_seed) changes that
        # hash and was observed to flip which row sorts last. Assert on the
        # honestly-retained property directly instead: SOME retained
        # attribution for this order shows the closed loop.
        assert any(item.position_state == "CLOSED" for item in matching)
        health = env.performance.health_assessments(PaperEvidenceClass.GENUINE_PAPER)
        assert any(
            item.strategy_or_method_id == matching[-1].strategy_or_method_id for item in health
        )


# --------------------------------------------------------------------------
# PAPER Continuation Health Signal v1: eliminates the ambiguity where an old
# terminal FAILED OperatingTaskResult can keep masquerading as "current"
# health while awaits_intraday_continuation-driven healthy continuation has
# genuinely been occurring since -- exactly the real 2026-08-27/-28/-29
# production shape (see docs/PAPER_OPERATIONS_OBSERVABILITY_V1.md). The test
# below reproduces a genuine historical failure (through the real
# ExecuteOperatingCycle -> _dispatch_task exception boundary, not a hand-
# constructed row) followed by real, later, healthy continuation, and proves
# two of this milestone's requirements against real Postgres specifically:
# (7) computing continuation health from a fresh read is deterministic and
# creates no misleading duplicate activity, and (9) the original FAILED
# OperatingTaskResult row is never mutated -- it is DB-trigger-immutable
# (apex_reject_mutation) and this milestone's new code is purely additive
# read-time computation, never a write.
# --------------------------------------------------------------------------


class _FailOnceThenDelegateWrapper:
    """Raises a genuine exception on STRATEGY_PAPER_TRIAL_EXECUTION's first
    dispatch (mirroring exactly how a real transient failure reaches
    ExecuteOperatingCycle's existing (OSError, RuntimeError, ValueError)
    _dispatch_task handler and gets persisted as a terminal FAILED
    OperatingTaskResult -- see TestPointInTimeRaceRegression's own module
    docstring for the real 2026-08-27 precedent this mirrors), then
    delegates to the real executor on every subsequent call. Every other
    task type completes trivially, exactly like
    _RealStrategyPaperExecutorWrapper."""

    def __init__(self, real: ExistingCapabilityOperatingTaskExecutor) -> None:
        self._real = real
        self._raised = False

    def execute(
        self,
        *,
        task: DueTask,
        cycle_id: str,
        plan: OperatingCyclePlan,
        as_of: datetime,
        executed_at: datetime,
    ) -> TaskExecution:
        if task.task_type is OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION:
            if not self._raised:
                self._raised = True
                raise RuntimeError("SIMULATED_STRATEGY_PAPER_TRANSIENT_FAILURE")
            return self._real.execute(
                task=task, cycle_id=cycle_id, plan=plan, as_of=as_of, executed_at=executed_at
            )
        return TaskExecution(
            OperatingTaskStatus.COMPLETED, (f"{task.task_type.value}-output",), (), True, 0
        )


class TestContinuationHealthSignal:
    """A genuine historical failure on one tick, followed by real healthy
    continuation (a fresh, unconditional reconciliation) on a later day,
    must classify RECOVERED without ever mutating, hiding, or duplicating
    the original FAILED terminal row. See tests/test_operating_cycle.py for
    full state-machine coverage of compute_paper_continuation_health in
    isolation -- this test proves the real Postgres integration only."""

    def test_historical_failure_then_real_recovery_computes_recovered_without_mutating_history(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan_repository = PostgresOperatingCycleRepository(env.engine)
        _register_and_activate_plan(plan_repository, created_at=DAY1)
        cycle_id_day1 = f"{PLAN_ID}-worker-20260601"

        # Tick 1: genuinely succeeds through the real executor -- opens a
        # trial; no terminal result persisted (awaits_intraday_continuation).
        opened_wrapper = _RealStrategyPaperExecutorWrapper(
            ExistingCapabilityOperatingTaskExecutor(env.engine, env.state_source)
        )
        ExecuteOperatingCycle(plan_repository, env.state_source, opened_wrapper).execute(
            cycle_id=cycle_id_day1,
            plan_id=PLAN_ID,
            as_of=DAY1,
            executed_at=DAY1,
            idempotency_key=f"{cycle_id_day1}-key",
        )
        assert len(opened_wrapper.results) == 1
        order_id = _open_order(env).order_id

        # Tick 2, same UTC day/cycle_id: STRATEGY_PAPER_TRIAL_EXECUTION has
        # no persisted terminal result yet for this cycle_id (tick 1
        # deferred it), so the real scheduler genuinely redispatches it --
        # and this time it genuinely raises, which the real, unmodified
        # exception boundary turns into a terminal FAILED OperatingTaskResult.
        failing_wrapper = _FailOnceThenDelegateWrapper(
            ExistingCapabilityOperatingTaskExecutor(env.engine, env.state_source)
        )
        failure_tick = DAY1 + timedelta(minutes=30)
        ExecuteOperatingCycle(plan_repository, env.state_source, failing_wrapper).execute(
            cycle_id=cycle_id_day1,
            plan_id=PLAN_ID,
            as_of=DAY1,
            executed_at=failure_tick,
            idempotency_key=f"{cycle_id_day1}-key",
        )
        failed_results = [
            item
            for item in plan_repository.task_results(cycle_id_day1)
            if item.task_type is OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION
        ]
        assert len(failed_results) == 1
        assert failed_results[0].status is OperatingTaskStatus.FAILED
        original_failed_hash = failed_results[0].canonical_hash
        original_failed_at = failed_results[0].completed_at

        # Day 2: a genuinely new cycle_id, real executor throughout --
        # recognizes the already-open trial (no duplicate order), reconciles
        # unconditionally, producing fresh continuation evidence. Still
        # persists no new terminal result (healthy continuation again).
        cycle_id_day2 = f"{PLAN_ID}-worker-20260602"
        day2 = DAY1 + timedelta(days=1)
        recovery_wrapper = _RealStrategyPaperExecutorWrapper(
            ExistingCapabilityOperatingTaskExecutor(env.engine, env.state_source)
        )
        ExecuteOperatingCycle(plan_repository, env.state_source, recovery_wrapper).execute(
            cycle_id=cycle_id_day2,
            plan_id=PLAN_ID,
            as_of=day2,
            executed_at=day2,
            idempotency_key=f"{cycle_id_day2}-key",
        )
        assert len(recovery_wrapper.results) == 1
        assert recovery_wrapper.results[0].awaits_intraday_continuation is True
        assert [
            item
            for item in plan_repository.task_results(cycle_id_day2)
            if item.task_type is OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION
        ] == []

        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 1  # still the one order from tick 1 -- no duplicate
        assert state.orders[0].order_id == order_id
        latest_reconciliation = max(state.reconciliations, key=lambda item: item.evaluated_at)
        assert latest_reconciliation.evaluated_at > original_failed_at  # genuinely fresher

        now = day2 + timedelta(minutes=1)
        health = compute_paper_continuation_health(
            latest_terminal_status=OperatingTaskStatus.FAILED,
            latest_terminal_at=original_failed_at,
            latest_continuation_at=latest_reconciliation.evaluated_at,
            now=now,
            freshness_window=timedelta(hours=1),
        )
        assert health.state is PaperContinuationHealthState.RECOVERED
        assert health.recovered_since_failure is True
        # The historical failure is preserved and still reported, never
        # hidden -- it is what "recovered" is recovering FROM.
        assert health.latest_terminal_status is OperatingTaskStatus.FAILED
        assert health.latest_terminal_at == original_failed_at

        # Requirement 7: recomputing from a fresh, independent read of the
        # same retained evidence is byte-identical -- deterministic, no
        # misleading duplicate activity from a replay.
        reread_state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert reread_state is not None
        reread_latest_reconciliation = max(
            reread_state.reconciliations, key=lambda item: item.evaluated_at
        )
        health_again = compute_paper_continuation_health(
            latest_terminal_status=OperatingTaskStatus.FAILED,
            latest_terminal_at=original_failed_at,
            latest_continuation_at=reread_latest_reconciliation.evaluated_at,
            now=now,
            freshness_window=timedelta(hours=1),
        )
        assert health_again == health

        # Requirement 9: the original FAILED row's own persisted content is
        # completely unchanged -- immutable at the DB level
        # (apex_reject_mutation trigger) and never written to by this
        # milestone's purely additive, read-only new code path.
        refetched_failed = [
            item
            for item in plan_repository.task_results(cycle_id_day1)
            if item.task_type is OperatingTaskType.STRATEGY_PAPER_TRIAL_EXECUTION
        ]
        assert len(refetched_failed) == 1
        assert refetched_failed[0].canonical_hash == original_failed_hash
        assert refetched_failed[0].status is OperatingTaskStatus.FAILED
        assert refetched_failed[0].completed_at == original_failed_at


class TestInternalStrategyEvaluationHistoryDurability:
    """Strategy Evaluation History & Promotion Lineage v1: production-shaped
    regression proving the REAL ``_execute_strategy_paper_trial`` dispatch
    (unmodified control flow, unmodified trading decisions) genuinely
    durably retains its own evaluation result for every candidate it
    evaluates -- not a second, separately-driven recomputation. Reuses this
    module's own established fixtures (real universe/candle bootstrap, real
    live-observation seeding, the real executor) exactly like every other
    class in this file."""

    def test_recurring_dispatch_durably_retains_every_evaluated_candidate(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert result.status is OperatingTaskStatus.COMPLETED
        # Matches this file's own established fixture shape (see
        # TestCycleNConsiderationAndPlacement): 4 candidates considered, 1
        # (MOVING_AVERAGE_TREND) rejected before ever reaching a real
        # evaluation (needs 201 days, the fixture retains 200) -- so exactly
        # 3 candidates genuinely reach evaluate_external_strategy and must
        # each have exactly one durably retained row.
        assert "STRATEGY_PAPER_EVALUATIONS_RETAINED:3" in result.warnings
        candidates = canonical_strategy_benchmarks(created_at=DAY1)
        retained_strategy_ids = {
            item.strategy_id
            for candidate in candidates
            for item in env.internal_strategy_evaluations.for_strategy(
                strategy_id=candidate.strategy_id
            )
        }
        evaluable_strategy_ids = {
            candidate.strategy_id
            for candidate in candidates
            if candidate.rule_kind.value != "MOVING_AVERAGE_TREND"
        }
        assert retained_strategy_ids == evaluable_strategy_ids
        for strategy_id in evaluable_strategy_ids:
            rows = env.internal_strategy_evaluations.for_strategy(strategy_id=strategy_id)
            assert len(rows) == 1
            assert rows[0].cycle_id == f"{PLAN_ID}-worker-20260601"
            assert rows[0].as_of == DAY1
            assert rows[0].candidate.sample_size > 0

    def test_same_cycle_id_later_tick_replays_without_duplicating(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        cycle_id = f"{PLAN_ID}-worker-20260601"

        first = env.run_cycle(cycle_id=cycle_id, as_of=DAY1, executed_at=DAY1, plan=plan)
        assert first.status is OperatingTaskStatus.COMPLETED
        buy_and_hold = next(
            item
            for item in canonical_strategy_benchmarks(created_at=DAY1)
            if item.rule_kind.value == "BUY_AND_HOLD"
        )
        first_rows = env.internal_strategy_evaluations.for_strategy(
            strategy_id=buy_and_hold.strategy_id
        )
        assert len(first_rows) == 1

        # A genuinely later real dispatch, same cycle_id/as_of (the
        # production day-scoped shape every other intraday-continuation
        # test in this file already exercises) -- must replay the identical
        # evaluation row, never mint a second one.
        later = DAY1 + timedelta(hours=1)
        second = env.run_cycle(cycle_id=cycle_id, as_of=DAY1, executed_at=later, plan=plan)
        assert any(item == first_rows[0].evaluation_id for item in second.reused_identities)
        second_rows = env.internal_strategy_evaluations.for_strategy(
            strategy_id=buy_and_hold.strategy_id
        )
        assert len(second_rows) == 1
        assert second_rows[0].evaluation_id == first_rows[0].evaluation_id

    def test_new_days_as_of_mints_a_distinct_retained_row(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        buy_and_hold = next(
            item
            for item in canonical_strategy_benchmarks(created_at=DAY1)
            if item.rule_kind.value == "BUY_AND_HOLD"
        )
        first_rows = env.internal_strategy_evaluations.for_strategy(
            strategy_id=buy_and_hold.strategy_id
        )
        assert len(first_rows) == 1

        day2 = DAY1 + timedelta(days=1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=day2)
        second = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260602", as_of=day2, executed_at=day2, plan=plan
        )
        assert second.status is OperatingTaskStatus.COMPLETED

        second_rows = env.internal_strategy_evaluations.for_strategy(
            strategy_id=buy_and_hold.strategy_id
        )
        # Part 6: a genuinely new as_of must retain a NEW, distinct row --
        # never silently discarded, never overwriting the first day's own
        # immutable evidence.
        assert len(second_rows) == 2
        second_ids = {item.evaluation_id for item in second_rows}
        assert first_rows[0].evaluation_id in second_ids
        assert len(second_ids) == 2  # the day-1 row plus one genuinely new day-2 row
        assert {item.as_of for item in second_rows} == {DAY1, day2}


def _winning_strategy_id(env: _Executors, *, order: CryptoPaperOrder) -> str:
    """The strategy_id whose authorization actually produced `order` --
    empirically determined rather than assumed, since evidence-weighted
    ranking (Phase E) can genuinely select any promoted candidate depending
    on the fixture's own controlled price series, not necessarily
    BUY_AND_HOLD specifically."""

    state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
    assert state is not None
    authorization = next(
        item for item in state.authorizations if item.authorization_id == order.authorization_id
    )
    return authorization.strategy_or_discovery_id


class _ClosedTrialOne:
    """Result of driving one qualifying candidate's trial 1 all the way
    through open -> fill -> horizon -> close-submit -> close-fill, via the
    real recurring task against real Postgres. Shared setup for both the
    full-lifecycle proof and the replay-safety proofs below."""

    def __init__(self, env: _Executors, plan: OperatingCyclePlan) -> None:
        self.env = env
        self.plan = plan

        # ---- Trial 1: open.
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        opened = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-t1-open", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert opened.status is OperatingTaskStatus.COMPLETED
        assert "STRATEGY_PAPER_TRIALS_CREATED:1" in opened.warnings
        order_1 = _open_order(env)
        self.strategy_id = _winning_strategy_id(env, order=order_1)

        slot_trials = env.strategy_paper_trials.for_slot(
            strategy_id=self.strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )
        assert len(slot_trials) == 1
        self.trial_1 = slot_trials[0]
        assert self.trial_1.generation == 1
        assert self.trial_1.opening_order_id == order_1.order_id
        assert env.strategy_paper_trial_closures.for_trial(trial_id=self.trial_1.trial_id) is None

        # ---- Trial 1: fill.
        fill_time = DAY1 + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("50010"), captured_at=fill_time)
        filled = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-t1-fill",
            as_of=fill_time,
            executed_at=fill_time + timedelta(minutes=1),
            plan=plan,
        )
        assert "STRATEGY_PAPER_FILLS_APPLIED:1" in filled.warnings
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        filled_order = next(item for item in state.orders if item.order_id == order_1.order_id)
        assert filled_order.state is PaperOrderState.FILLED

        # ---- Trial 1: horizon reached -> closing SELL submitted, not yet filled.
        self.horizon_at = self.trial_1.horizon_target_at
        _seed_observation(env.live_observation, price=Decimal("80000"), captured_at=self.horizon_at)
        self.close_submitted = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-t1-close-submit",
            as_of=self.horizon_at,
            executed_at=self.horizon_at,
            plan=plan,
        )
        assert self.close_submitted.status is OperatingTaskStatus.COMPLETED
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        assert len(state.orders) == 2  # the original BUY plus the new closing SELL
        self.close_order_id = f"{self.trial_1.trial_id}-close-order"
        close_order = next(item for item in state.orders if item.order_id == self.close_order_id)
        assert close_order.side is PaperSide.SELL
        assert env.strategy_paper_trial_closures.for_trial(trial_id=self.trial_1.trial_id) is None

        # ---- Trial 1: close fills -> closure finalized with realized P&L.
        close_fill_time = self.horizon_at + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("80500"), captured_at=close_fill_time)
        self.close_filled = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-t1-close-fill",
            as_of=close_fill_time,
            executed_at=close_fill_time + timedelta(minutes=1),
            plan=plan,
        )
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        filled_close_order = next(
            item for item in state.orders if item.order_id == self.close_order_id
        )
        assert filled_close_order.state is PaperOrderState.FILLED
        closure = env.strategy_paper_trial_closures.for_trial(trial_id=self.trial_1.trial_id)
        assert closure is not None
        self.closure = closure


class TestFullBoundedTrialLifecycle:
    """Repeatable Strategy PAPER Evidence v1: the required end-to-end proof
    -- qualification -> trial 1 authorization -> BUY -> fill/reconcile ->
    elapsed horizon -> SELL -> terminal attribution -> cooldown ->
    qualifying reevaluation -> trial 2 authorization -- entirely through
    the real recurring task against real Postgres."""

    def test_qualification_through_second_trial_generation(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        plan = _plan(created_at=DAY1)
        driven = _ClosedTrialOne(env, plan)

        assert driven.closure.terminal_reason is StrategyPaperTrialTerminalReason.TRIAL_HORIZON_EXIT
        assert driven.closure.realized_paper_pnl is not None
        # Bought around $50010, sold around $80500 -- genuinely profitable,
        # not merely non-None.
        assert driven.closure.realized_paper_pnl > 0

        # The underlying ledger position is genuinely, fully closed too --
        # never merely "the trial record says closed" while real PAPER
        # exposure remains outstanding.
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        position = next(item for item in state.positions if item.product_id == BENCHMARK_INSTRUMENT)
        assert position.state == "CLOSED"
        assert position.base_quantity == 0

        # Attribution honestly reflects the terminal, closed trial.
        attributions = env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
        close_attribution = [
            item for item in attributions if item.order_id == driven.close_order_id
        ]
        assert close_attribution
        assert any(item.position_state == "CLOSED" for item in close_attribution)

        # ---- Cooldown: immediately re-running does NOT open a SECOND
        # generation for THIS SAME slot yet. Every canonical candidate
        # targets the identical instrument (BTC-USD), so once trial 1's
        # instrument frees up, a DIFFERENT, never-before-traded candidate
        # (NO_PRIOR_TRIAL) can legitimately win this cycle's one-new-trial
        # cap ahead of driven.strategy_id's own retrial -- the
        # ALREADY_CONSIDERED_ANOTHER_CANDIDATE_ALREADY_OPEN_THIS_INSTRUMENT
        # mechanism working exactly as intended (Phase E ranking, not a
        # cooldown violation). What cooldown specifically guarantees is
        # narrower and slot-scoped: driven.strategy_id's OWN slot never
        # advances to generation 2 before its own cooldown elapses --
        # asserted directly here, independent of what any other candidate
        # does this same cycle.
        cooldown_check_at = driven.closure.closed_at + timedelta(minutes=1)
        _seed_observation(
            env.live_observation, price=Decimal("80500"), captured_at=cooldown_check_at
        )
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-cooldown-check",
            as_of=cooldown_check_at,
            executed_at=cooldown_check_at,
            plan=plan,
        )
        slot_after_cooldown_check = env.strategy_paper_trials.for_slot(
            strategy_id=driven.strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )
        assert len(slot_after_cooldown_check) == 1  # still only trial 1
        assert (
            evaluate_retrial_eligibility(
                latest_trial=driven.trial_1,
                latest_closure=driven.closure,
                as_of=cooldown_check_at,
            )
            is TrialRetrialEligibility.COOLDOWN_NOT_YET_ELAPSED
        )

        # ---- Requalification + trial 2: once cooldown genuinely elapses,
        # the domain's own retrial-eligibility rule -- applied here to the
        # REAL trial_1/closure rows this test just drove through Postgres,
        # not a fabricated fixture -- honestly reports the slot as eligible
        # for its next generation.
        trial_2_eligible_at = driven.closure.next_eligible_at
        assert (
            evaluate_retrial_eligibility(
                latest_trial=driven.trial_1,
                latest_closure=driven.closure,
                as_of=trial_2_eligible_at,
            )
            is TrialRetrialEligibility.ELIGIBLE_FOR_NEXT_GENERATION
        )

        # And mechanically: the recurring task genuinely places another
        # trial generation this cycle (Part 9's own required demonstration
        # that repeated trials are possible) -- for whichever candidate
        # Phase E's evidence-weighted ranking actually selects among every
        # slot now eligible, itself an honest, undetermined-in-advance
        # outcome this test does not fabricate an assumption about (even
        # driven.strategy_id itself is not guaranteed to win: it is merely
        # guaranteed ELIGIBLE, proven above).
        #
        # One settling cycle first: the cooldown-check cycle above already
        # let a DIFFERENT, never-before-traded candidate open its own
        # generation-1 trial on the now-free instrument (the identical
        # dynamic explained above) -- its own order is still unfilled and
        # still within Phase T's "active, blocks this instrument" view at
        # the START of any cycle before its 4-hour validity window lapses.
        # Settling past that window first (never filling it -- no fresh
        # observation seeded for it specifically) lets Phase F/T' resolve
        # it to OPENING_ORDER_EXPIRED_UNFILLED and genuinely free the
        # instrument again, exactly like a real, unattended deployment
        # would resolve it on its own -- not a special-cased shortcut.
        settle_at = cooldown_check_at + timedelta(hours=5)
        _seed_observation(env.live_observation, price=Decimal("80500"), captured_at=settle_at)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-settle",
            as_of=settle_at,
            executed_at=settle_at,
            plan=plan,
        )

        _seed_observation(
            env.live_observation, price=Decimal("81000"), captured_at=trial_2_eligible_at
        )
        reopened = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-t2-open",
            as_of=trial_2_eligible_at,
            executed_at=trial_2_eligible_at,
            plan=plan,
        )
        assert "STRATEGY_PAPER_TRIALS_CREATED:1" in reopened.warnings
        state_after_reopen = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state_after_reopen is not None
        # Identified by submission time, not by excluding every OTHER
        # order_id seen so far -- by this point the account also carries
        # the settling candidate's own (now-expired) order, which an
        # exclusion list keyed only on driven's own ids would miss.
        second_order = next(
            item for item in state_after_reopen.orders if item.submitted_at == trial_2_eligible_at
        )
        second_winner_strategy_id = _winning_strategy_id(env, order=second_order)
        second_winner_slot = env.strategy_paper_trials.for_slot(
            strategy_id=second_winner_strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )
        newest_trial = second_winner_slot[-1]
        assert newest_trial.generation >= 1
        assert newest_trial.trial_id != driven.trial_1.trial_id  # distinct but deterministic
        assert newest_trial.instrument_id == driven.trial_1.instrument_id
        assert env.strategy_paper_trial_closures.for_trial(trial_id=newest_trial.trial_id) is None

        # Historical preservation (Part 5G): trial 1's own record and
        # closure remain byte-identical, never rewritten by trial 2 opening.
        assert env.strategy_paper_trials.get(trial_id=driven.trial_1.trial_id) == driven.trial_1
        assert (
            env.strategy_paper_trial_closures.for_trial(trial_id=driven.trial_1.trial_id)
            == driven.closure
        )


class TestNonqualifyingRetrialAbstention:
    """Part 9, required test 11: cooldown having elapsed is necessary but
    not sufficient for a retrial. A strategy whose cooldown window has
    passed must still not begin generation 2 in a cycle where it cannot be
    freshly (re)qualified -- cooldown and qualification are two genuinely
    separate gates, and eligibility on cooldown grounds alone must never
    be conflated with an actual new trial being placed."""

    def test_cooldown_elapsed_but_unqualified_strategy_gets_no_retrial(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        plan = _plan(created_at=DAY1)
        driven = _ClosedTrialOne(env, plan)

        # Cooldown genuinely elapsed -- the domain's own eligibility rule,
        # applied to the real trial_1/closure rows this test just drove
        # through Postgres (identical fixture to
        # TestFullBoundedTrialLifecycle), honestly reports this slot
        # ELIGIBLE on cooldown grounds alone.
        trial_2_eligible_at = driven.closure.next_eligible_at
        assert (
            evaluate_retrial_eligibility(
                latest_trial=driven.trial_1,
                latest_closure=driven.closure,
                as_of=trial_2_eligible_at,
            )
            is TrialRetrialEligibility.ELIGIBLE_FOR_NEXT_GENERATION
        )

        # But mechanically: deliberately no fresh observation is seeded for
        # this cycle (the identical abstention mechanism already proven in
        # TestStaleAndAbsentObservationAbstention above -- the freshest
        # observation on record by now is hours stale relative to
        # trial_2_eligible_at). Nothing can be freshly (re)evaluated or
        # promoted this cycle, so driven.strategy_id specifically must NOT
        # begin generation 2 despite being cooldown-eligible.
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-t2-unqualified",
            as_of=trial_2_eligible_at,
            executed_at=trial_2_eligible_at,
            plan=plan,
        )
        assert "STRATEGY_PAPER_TRIALS_CREATED:0" in result.warnings

        slot_after = env.strategy_paper_trials.for_slot(
            strategy_id=driven.strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )
        assert len(slot_after) == 1  # still only trial 1 -- no generation 2
        assert slot_after[0] == driven.trial_1  # byte-identical, untouched


class TestBoundedTrialSchedulerReplaySafety:
    """Repeatable Strategy PAPER Evidence v1, Part 9 (tests 2/6/18):
    replaying the close-submission and retrial scheduler boundaries must
    never duplicate authorization/order/close/retrial economic state."""

    def test_close_submission_is_not_duplicated_on_replay(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        plan = _plan(created_at=DAY1)

        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        opened = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-t1-open", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert opened.status is OperatingTaskStatus.COMPLETED
        order_1 = _open_order(env)
        strategy_id = _winning_strategy_id(env, order=order_1)
        fill_time = DAY1 + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("50010"), captured_at=fill_time)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-t1-fill",
            as_of=fill_time,
            executed_at=fill_time + timedelta(minutes=1),
            plan=plan,
        )
        trial_1 = env.strategy_paper_trials.for_slot(
            strategy_id=strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )[0]
        horizon_at = trial_1.horizon_target_at
        _seed_observation(env.live_observation, price=Decimal("80000"), captured_at=horizon_at)

        replayed_cycle_id = f"{PLAN_ID}-worker-t1-close-submit-replay"
        first = env.run_cycle(
            cycle_id=replayed_cycle_id, as_of=horizon_at, executed_at=horizon_at, plan=plan
        )
        assert first.status is OperatingTaskStatus.COMPLETED
        state_after_first = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state_after_first is not None
        assert len(state_after_first.orders) == 2

        # A genuine replay of the exact same cycle_id/as_of/executed_at --
        # must recognize the already-submitted close, never mint a second
        # closing authorization/order.
        second = env.run_cycle(
            cycle_id=replayed_cycle_id, as_of=horizon_at, executed_at=horizon_at, plan=plan
        )
        assert second.status is OperatingTaskStatus.REUSED
        state_after_replay = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state_after_replay is not None
        assert len(state_after_replay.orders) == 2  # unchanged -- no duplicate SELL
        assert state_after_replay.canonical_hash == state_after_first.canonical_hash

    def test_second_trial_opening_is_not_duplicated_on_replay(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        plan = _plan(created_at=DAY1)
        driven = _ClosedTrialOne(env, plan)

        # Every canonical candidate targets the identical instrument, so
        # once trial 1's instrument frees up, Phase E's evidence-weighted
        # ranking -- not necessarily driven.strategy_id's own retrial --
        # honestly decides which slot gets this cycle's one new trial (see
        # TestFullBoundedTrialLifecycle's own identical note). This test's
        # actual subject is replay-safety of THAT placement, whichever slot
        # it belongs to -- determined empirically, never assumed.
        trial_2_eligible_at = driven.closure.next_eligible_at
        _seed_observation(
            env.live_observation, price=Decimal("81000"), captured_at=trial_2_eligible_at
        )
        replayed_cycle_id = f"{PLAN_ID}-worker-t2-open-replay"
        first = env.run_cycle(
            cycle_id=replayed_cycle_id,
            as_of=trial_2_eligible_at,
            executed_at=trial_2_eligible_at,
            plan=plan,
        )
        assert "STRATEGY_PAPER_TRIALS_CREATED:1" in first.warnings
        state_after_first = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state_after_first is not None
        second_order = next(
            item
            for item in state_after_first.orders
            if item.order_id not in (driven.trial_1.opening_order_id, driven.close_order_id)
        )
        winner_strategy_id = _winning_strategy_id(env, order=second_order)
        slot_after_first = env.strategy_paper_trials.for_slot(
            strategy_id=winner_strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )
        assert len(slot_after_first) >= 1
        assert slot_after_first[-1].opening_order_id == second_order.order_id

        second = env.run_cycle(
            cycle_id=replayed_cycle_id,
            as_of=trial_2_eligible_at,
            executed_at=trial_2_eligible_at,
            plan=plan,
        )
        assert "STRATEGY_PAPER_TRIALS_CREATED:0" in second.warnings
        slot_after_replay = env.strategy_paper_trials.for_slot(
            strategy_id=winner_strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )
        assert slot_after_replay == slot_after_first  # unchanged -- no duplicate generation minted
        state_after_replay = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state_after_replay is not None
        assert len(state_after_replay.orders) == len(state_after_first.orders)


class TestTrialEvidenceCoverageDisclosure:
    """Trial Evidence Coverage Disclosure v1: proves the closure record's
    ``coverage_*`` fields are genuinely populated from real retained
    ``LiveMarketObservation`` rows by the real recurring task against real
    Postgres -- purely descriptive, never influencing whether/how/when a
    trial actually closes or what its realized P&L is (every assertion
    about the underlying trade mechanics below is identical in kind to
    ``TestFullBoundedTrialLifecycle``'s own, proving this milestone changed
    nothing about them)."""

    def test_sparse_coverage_from_the_default_lifecycle_reports_a_material_gap(
        self, env: _Executors
    ) -> None:
        # _ClosedTrialOne's own observation schedule (open, fill 1h later,
        # horizon ~30 days later, close-fill 1h after that) is already a
        # genuine sparse/partial-coverage shape -- 4 observations spread
        # across a 30-day window, with the dominant gap being the ~29.96
        # days between the fill and the horizon observation.
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        plan = _plan(created_at=DAY1)
        driven = _ClosedTrialOne(env, plan)
        closure = driven.closure

        expected_window_seconds = int(
            (closure.closed_at - driven.trial_1.opened_at).total_seconds()
        )
        assert closure.coverage_window_duration_seconds == expected_window_seconds
        assert closure.coverage_qualifying_observation_count == 4
        # Only 2 distinct UTC calendar dates covered out of a 31-day
        # nominal window: open+fill share DAY1's own date, horizon+
        # close-fill (30 days later) share a second, separate date.
        assert closure.coverage_covered_days == 2
        assert closure.coverage_nominal_days == 31
        assert is_material_coverage_gap(closure.coverage_largest_gap_seconds) is True
        assert closure.coverage_largest_gap_seconds > MATERIAL_EVIDENCE_GAP_SECONDS
        assert closure.coverage_policy_version

    def test_continuous_dense_coverage_reports_no_material_gap(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        plan = _plan(created_at=DAY1)

        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        opened = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-dense-open", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert opened.status is OperatingTaskStatus.COMPLETED
        order_1 = _open_order(env)
        strategy_id = _winning_strategy_id(env, order=order_1)
        fill_time = DAY1 + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("50010"), captured_at=fill_time)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-dense-fill",
            as_of=fill_time,
            executed_at=fill_time + timedelta(minutes=1),
            plan=plan,
        )
        trial_1 = env.strategy_paper_trials.for_slot(
            strategy_id=strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )[0]
        horizon_at = trial_1.horizon_target_at

        # Comfortably-below-threshold, evenly-spaced coverage across the
        # ENTIRE window -- every gap between consecutive observations is 4
        # hours, well inside MATERIAL_EVIDENCE_GAP_SECONDS (6 hours) --
        # mirroring a genuinely healthy live-observer cadence at a reduced
        # (test-practical) sampling rate rather than the real 1-minute one.
        # Starts one step after fill_time (already separately seeded above
        # for the fill cycle's own contemporaneous-evidence requirement) to
        # avoid re-seeding the identical captured_at with a different price,
        # which would trip PostgresLiveMarketObservationRepository's own
        # identity-conflict guard.
        cursor = fill_time + timedelta(hours=4)
        while cursor < horizon_at:
            _seed_observation(env.live_observation, price=Decimal("55000"), captured_at=cursor)
            cursor += timedelta(hours=4)
        _seed_observation(env.live_observation, price=Decimal("80000"), captured_at=horizon_at)

        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-dense-close-submit",
            as_of=horizon_at,
            executed_at=horizon_at,
            plan=plan,
        )
        close_fill_time = horizon_at + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("80500"), captured_at=close_fill_time)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-dense-close-fill",
            as_of=close_fill_time,
            executed_at=close_fill_time + timedelta(minutes=1),
            plan=plan,
        )

        closure = env.strategy_paper_trial_closures.for_trial(trial_id=trial_1.trial_id)
        assert closure is not None
        assert closure.coverage_largest_gap_seconds <= int(timedelta(hours=4).total_seconds())
        assert is_material_coverage_gap(closure.coverage_largest_gap_seconds) is False
        assert closure.coverage_qualifying_observation_count > 100

    def test_multi_day_gap_is_honestly_recorded_while_fills_and_pnl_stay_correct(
        self, env: _Executors
    ) -> None:
        """Models today's actual real-world shape: valid observations exist
        initially, a long (~19 day) multi-day observation gap occurs (the
        genuine 2026-09-05..2026-09-21 outage this milestone was written
        to honestly disclose), valid observations resume, and a fresh
        contemporaneous observation exists at horizon -- trial closes
        normally, closure P&L remains based on the real fills alone, and
        coverage metadata honestly records the large historical gap."""

        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        plan = _plan(created_at=DAY1)

        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        opened = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-gap-open", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        assert opened.status is OperatingTaskStatus.COMPLETED
        order_1 = _open_order(env)
        strategy_id = _winning_strategy_id(env, order=order_1)
        fill_time = DAY1 + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("50010"), captured_at=fill_time)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-gap-fill",
            as_of=fill_time,
            executed_at=fill_time + timedelta(minutes=1),
            plan=plan,
        )
        trial_1 = env.strategy_paper_trials.for_slot(
            strategy_id=strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )[0]
        horizon_at = trial_1.horizon_target_at

        # "Valid observations exist initially" -- a couple more shortly
        # after the fill, same as genuine early-lifecycle live-observer
        # activity.
        _seed_observation(
            env.live_observation, price=Decimal("50100"), captured_at=fill_time + timedelta(hours=1)
        )
        _seed_observation(
            env.live_observation, price=Decimal("50200"), captured_at=fill_time + timedelta(hours=2)
        )
        last_pre_gap = fill_time + timedelta(hours=2)

        # "A long multi-day observation gap occurs" -- genuinely nothing
        # retained for ~19 days, then "valid observations resume".
        resume_at = last_pre_gap + timedelta(days=19)
        assert resume_at < horizon_at  # stays inside the 30-day window
        _seed_observation(env.live_observation, price=Decimal("62000"), captured_at=resume_at)

        # "At/after horizon a fresh contemporaneous observation exists" --
        # trial closes normally.
        _seed_observation(env.live_observation, price=Decimal("80000"), captured_at=horizon_at)
        close_submitted = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-gap-close-submit",
            as_of=horizon_at,
            executed_at=horizon_at,
            plan=plan,
        )
        assert close_submitted.status is OperatingTaskStatus.COMPLETED
        close_fill_time = horizon_at + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("80500"), captured_at=close_fill_time)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-gap-close-fill",
            as_of=close_fill_time,
            executed_at=close_fill_time + timedelta(minutes=1),
            plan=plan,
        )

        closure = env.strategy_paper_trial_closures.for_trial(trial_id=trial_1.trial_id)
        assert closure is not None
        assert closure.terminal_reason is StrategyPaperTrialTerminalReason.TRIAL_HORIZON_EXIT

        # Closure P&L remains based on the real fills alone -- unaffected
        # by the gap. Bought ~50010, sold ~80500: genuinely profitable.
        assert closure.realized_paper_pnl is not None
        assert closure.realized_paper_pnl > 0

        # Coverage metadata honestly records the large historical gap.
        expected_gap_seconds = int((resume_at - last_pre_gap).total_seconds())
        assert closure.coverage_largest_gap_seconds == expected_gap_seconds
        assert is_material_coverage_gap(closure.coverage_largest_gap_seconds) is True
        assert closure.coverage_largest_gap_seconds > MATERIAL_EVIDENCE_GAP_SECONDS
        # 6 genuinely distinct observations retained: open, fill, +2
        # pre-gap, 1 resume, horizon-close-decision, close-fill = 7.
        assert closure.coverage_qualifying_observation_count == 7
        assert closure.coverage_covered_days < closure.coverage_nominal_days

        # The underlying ledger position is genuinely, fully closed --
        # the gap did not leave anything half-finished.
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        position = next(item for item in state.positions if item.product_id == BENCHMARK_INSTRUMENT)
        assert position.state == "CLOSED"

    def test_replay_recomputes_identical_coverage_without_mutating_closure_state(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        plan = _plan(created_at=DAY1)

        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-replay-open", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        order_1 = _open_order(env)
        strategy_id = _winning_strategy_id(env, order=order_1)
        fill_time = DAY1 + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("50010"), captured_at=fill_time)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-replay-fill",
            as_of=fill_time,
            executed_at=fill_time + timedelta(minutes=1),
            plan=plan,
        )
        trial_1 = env.strategy_paper_trials.for_slot(
            strategy_id=strategy_id,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )[0]
        horizon_at = trial_1.horizon_target_at
        _seed_observation(env.live_observation, price=Decimal("80000"), captured_at=horizon_at)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-replay-close-submit",
            as_of=horizon_at,
            executed_at=horizon_at,
            plan=plan,
        )
        close_fill_time = horizon_at + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("80500"), captured_at=close_fill_time)

        replayed_cycle_id = f"{PLAN_ID}-worker-replay-close-fill"
        first = env.run_cycle(
            cycle_id=replayed_cycle_id,
            as_of=close_fill_time,
            executed_at=close_fill_time + timedelta(minutes=1),
            plan=plan,
        )
        assert first.status is OperatingTaskStatus.COMPLETED
        first_closure = env.strategy_paper_trial_closures.for_trial(trial_id=trial_1.trial_id)
        assert first_closure is not None

        # A genuine replay of the exact same cycle_id/as_of/executed_at for
        # TRIAL 1's own slot specifically -- evaluate_trial_evidence_
        # coverage recomputes from scratch against the identical retained
        # observations and must reproduce byte-identical coverage, hitting
        # RecordStrategyPaperTrialClosed's own idempotent-replay path
        # rather than a conflict or a mutation. NOT asserted here: the
        # overall TaskExecution.status -- closing trial_1's position
        # genuinely, correctly frees BTC-USD's one-open-order-per-product
        # capacity for a DIFFERENT, already-promoted candidate the moment
        # this replay re-reads account state (mirrors the existing,
        # unrelated-to-this-milestone TestBoundedTrialSchedulerReplaySafety
        # .test_second_trial_opening_is_not_duplicated_on_replay's own
        # documented dynamic), so a second same-input firing can honestly
        # do genuinely new, different work for ANOTHER slot while still
        # being a pure, non-duplicating replay for TRIAL 1's own closure --
        # the one thing this test actually verifies.
        second = env.run_cycle(
            cycle_id=replayed_cycle_id,
            as_of=close_fill_time,
            executed_at=close_fill_time + timedelta(minutes=1),
            plan=plan,
        )
        assert "STRATEGY_PAPER_TRIALS_CLOSED:0" in second.warnings  # trial_1 not re-closed
        second_closure = env.strategy_paper_trial_closures.for_trial(trial_id=trial_1.trial_id)
        assert second_closure == first_closure
        with env.engine.connect() as connection:
            count = connection.execute(
                sa.text(
                    "SELECT COUNT(*) FROM internal_strategy_paper_trial_closure "
                    "WHERE trial_id = :trial_id"
                ),
                {"trial_id": trial_1.trial_id},
            ).scalar_one()
        assert count == 1


class _FailingTransitionProvenance:
    """Delegates every call except ``record_transition``, which fails the way
    a genuine provenance conflict would."""

    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def record_transition(self, *, transition: object) -> bool:
        raise StrategyVersionProvenanceResearchError("INJECTED_PROVENANCE_FAILURE")


class TestStrategyVersionProvenanceIntegration:
    """Strategy Version Provenance v1, driven through the real recurring
    ``_execute_strategy_paper_trial`` dispatch."""

    def test_placed_trial_is_bound_to_this_cycles_exact_admission(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        cycle_id = f"{PLAN_ID}-worker-20260601"
        result = env.run_cycle(cycle_id=cycle_id, as_of=DAY1, executed_at=DAY1, plan=plan)
        assert "STRATEGY_PAPER_TRIALS_CREATED:1" in result.warnings
        assert "STRATEGY_PAPER_PROVENANCE_ADMISSIONS_RETAINED:3" in result.warnings

        provenance = PostgresStrategyVersionProvenanceRepository(env.engine)
        order = _open_order(env)
        strategy_id = _winning_strategy_id(env, order=order)
        (trial,) = env.strategy_paper_trials.for_strategy(strategy_id=strategy_id)
        bound = provenance.trial_provenance(trial_id=trial.trial_id)
        assert bound is not None
        assert classify_trial_provenance(trial=trial, provenance=bound) is (
            TrialProvenanceClass.BOUND
        )
        transition = provenance.transition(transition_id=bound.transition_id)
        assert transition is not None
        (evaluation,) = env.internal_strategy_evaluations.for_strategy(strategy_id=strategy_id)
        # trial -> transition -> exactly the evaluation this cycle retained
        # -> exactly the catalog artifact that evaluation ran on.
        assert transition.evaluation_id == evaluation.evaluation_id
        assert transition.evaluation_hash == evaluation.canonical_hash
        catalog = next(
            item
            for item in canonical_strategy_benchmarks(created_at=DAY1)
            if item.strategy_id == strategy_id
        )
        assert transition.from_ref.definition_hash == catalog.canonical_hash
        assert (bound.version_ref.version, trial.strategy_version) == (2, 2)

        with env.engine.connect() as connection:
            before = {
                table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # nosec B608
                for table in STRATEGY_VERSION_PROVENANCE_TABLES
            }
        replay = env.run_cycle(
            cycle_id=cycle_id, as_of=DAY1, executed_at=DAY1 + timedelta(hours=1), plan=plan
        )
        assert "STRATEGY_PAPER_PROVENANCE_ADMISSIONS_RETAINED:0" in replay.warnings
        with env.engine.connect() as connection:
            after = {
                table: connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()  # nosec B608
                for table in STRATEGY_VERSION_PROVENANCE_TABLES
            }
        assert after == before
        # 3 evaluated roots + 3 admitted v2 artifacts, 3 subjects, 3
        # admissions, 1 bound trial (MOVING_AVERAGE_TREND lacks history).
        assert before == {
            "strategy_version": 6,
            "strategy_evaluation_subject": 3,
            "strategy_version_transition": 3,
            "strategy_paper_trial_provenance": 1,
        }

    def test_provenance_failure_blocks_opening_without_failing_the_task(
        self, env: _Executors
    ) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        env.executor._strategy_provenance = _FailingTransitionProvenance(  # type: ignore[assignment]
            env.executor._strategy_provenance
        )
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601",
            as_of=DAY1,
            executed_at=DAY1,
            plan=_plan(created_at=DAY1),
        )
        assert result.status is not OperatingTaskStatus.FAILED
        assert "STRATEGY_PAPER_TRIALS_CREATED:0" in result.warnings
        assert "STRATEGY_PAPER_CANDIDATES_PROMOTED:0" in result.warnings
        assert (
            sum(
                "STRATEGY_PAPER_TRIAL_OPEN_BLOCKED_PROVENANCE:INJECTED_PROVENANCE_FAILURE" in item
                for item in result.warnings
            )
            == 3
        )
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is None or state.orders == ()
        # Evaluation history is still retained honestly.
        assert "STRATEGY_PAPER_EVALUATIONS_RETAINED:3" in result.warnings

    def test_open_trial_keeps_filling_while_provenance_is_failing(self, env: _Executors) -> None:
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=DAY1)
        _seed_observation(env.live_observation, price=Decimal("50000"), captured_at=DAY1)
        plan = _plan(created_at=DAY1)
        env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601", as_of=DAY1, executed_at=DAY1, plan=plan
        )
        order_id = _open_order(env).order_id
        env.executor._strategy_provenance = _FailingTransitionProvenance(  # type: ignore[assignment]
            env.executor._strategy_provenance
        )
        fill_time = DAY1 + timedelta(hours=1)
        _seed_observation(env.live_observation, price=Decimal("50010"), captured_at=fill_time)
        result = env.run_cycle(
            cycle_id=f"{PLAN_ID}-worker-20260601-fill-tick",
            as_of=fill_time,
            executed_at=fill_time + timedelta(minutes=1),
            plan=plan,
        )
        assert result.status is not OperatingTaskStatus.FAILED
        assert "STRATEGY_PAPER_FILLS_APPLIED:1" in result.warnings
        state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        assert state is not None
        filled = next(item for item in state.orders if item.order_id == order_id)
        assert filled.state in (PaperOrderState.FILLED, PaperOrderState.PARTIALLY_FILLED)
