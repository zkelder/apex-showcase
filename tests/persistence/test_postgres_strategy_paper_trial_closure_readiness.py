"""PAPER Trial Closure Readiness v1: production-shaped proof of the ENTIRE
horizon-close lifecycle for the one real generation-1 BTC-USD strategy PAPER
trial, before its first genuine horizon event.

The fixture reproduces the real production trial STRUCTURALLY, from the real
domain/application/adapter code paths -- never by inserting rows by hand:

- the opening BUY is placed through the real ``ExecuteStrategyPaperTrial.
  place_order`` under the pre-0111 lifetime seed
  (``recurring_strategy_paper_trial_seed``), exactly as production placed it
  on 2026-08-30 -- so the authorization/order/trial ids, the opening
  timestamp (``2026-08-30T00:00:30.374926Z``), the filled quantity
  (``0.00012687`` BTC), the average cost (``78663.19854969``) and the horizon
  (``2026-09-29T00:00:30.374926Z``) all come out byte-identical to the
  retained production rows (asserted below, not assumed);
- the trial is then ADOPTED (``legacy_adopted=True``) by the real recurring
  task, exactly as production adopted it on 2026-09-03;
- every later step (horizon evaluation, closing SELL, fill, closure record,
  reconciliation, attribution, replay, cooldown) is driven only through the
  real ``_execute_strategy_paper_trial`` executor branch, with the exact
  ``as_of``/``executed_at`` shape the real supervised worker uses
  (``as_of`` = UTC midnight of the cycle day, ``executed_at`` = wall clock).

Runs against this worktree's own isolated, disposable Postgres (see
docs/WORKTREE_VERIFICATION.md). Price series are clearly-labeled fixtures
(never a claim about real BTC-USD history); every economic figure is
recomputed in-test from the domain's own documented rounding rules and
compared against the real retained production values where one exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from test_postgres_strategy_paper_trial_recurring import (
    PLAN_ID,
    PROMOTED_STRATEGY_VERSION,
    PULSE_PLAN_ID,
    _bootstrap_universe_and_candles,
    _Executors,
    _plan,
    _register_and_activate_plan,
    _seed_observation,
)

from apex.adapters import operating_cycle_tasks as operating_cycle_tasks_module
from apex.adapters.coinbase_universe_public import FixtureCoinbaseUniverseSource
from apex.adapters.live_observation_requirements import (
    PostgresExecutionObservationRequirements,
    execute_live_observer_iteration,
)
from apex.adapters.operating_cycle_postgres import PostgresOperatingCycleRepository
from apex.adapters.operating_cycle_tasks import (
    GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
    STRATEGY_PAPER_TRIAL_ACTOR,
)
from apex.application import strategy_paper_trial as strategy_paper_trial_module
from apex.application.crypto_universe import CapturedProductBook
from apex.application.live_market_observation import (
    MAX_LIVE_OBSERVATION_INSTRUMENTS_PER_ROUND,
    ExecuteLiveMarketObservationRound,
)
from apex.application.strategy_paper_trial import ExecuteStrategyPaperTrial
from apex.domain.crypto_paper import (
    CryptoPaperError,
    CryptoPaperOrder,
    CryptoPaperState,
    PaperOrderState,
    PaperReservationState,
    PaperSide,
    ledger_account_balances,
    ledger_contract_discrepancies,
)
from apex.domain.crypto_universe import CryptoUniverseSnapshot
from apex.domain.live_market_observation import (
    ExecutionObservationDependency,
    LiveMarketObservation,
    LiveObservationCaptureRound,
    LiveObservationQuality,
    LiveObservationRoundStatus,
    LiveObservationType,
    live_observation_round_id,
)
from apex.domain.operating_cycle import OperatingCyclePlan, OperatingTaskStatus
from apex.domain.performance_attribution import PaperEvidenceClass
from apex.domain.research_evidence import EvidenceClass
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_benchmark import (
    BENCHMARK_INSTRUMENT,
    canonical_strategy_benchmarks,
    simulate_strategy_backtest,
)
from apex.domain.strategy_definition import StrategyRuleKind
from apex.domain.strategy_evaluation import (
    evaluate_external_strategy,
    promote_to_paper_eligible,
)
from apex.domain.strategy_paper_trial import (
    build_strategy_opportunity_projection,
    price_evidence_from_live_observation,
    recurring_strategy_paper_trial_seed,
)
from apex.domain.strategy_paper_trial_lifecycle import (
    STRATEGY_PAPER_TRIAL_LIFECYCLE_POLICY_V1,
    StrategyPaperTrialRecord,
    StrategyPaperTrialTerminalReason,
    TrialHorizonState,
    evaluate_trial_horizon,
)

pytestmark = pytest.mark.postgres

# ---- The real production trial, field for field (read from the live
# ``internal_strategy_paper_trial`` row on 2026-09-26; see the module
# docstring). These are ASSERTED against what the real code paths produce.
PRODUCTION_STRATEGY_ID = "strategy_definition_f7afb672c33cff581951bfa6"
PRODUCTION_TRIAL_ID = "strategy_paper_trial_generation_5f0024234415d261eb976b31"
PRODUCTION_OPENING_AUTHORIZATION_ID = (
    "strategy_paper_trial_recurring_candidate_c8e3ebc6004804b4dec2d4c0-authorization"
)
PRODUCTION_OPENING_ORDER_ID = (
    "strategy_paper_trial_recurring_candidate_c8e3ebc6004804b4dec2d4c0-order"
)
PRODUCTION_OPENED_AT = datetime(2026, 8, 30, 0, 0, 30, 374926, tzinfo=UTC)
PRODUCTION_FILLED_AT = datetime(2026, 8, 30, 0, 4, 30, 814717, tzinfo=UTC)
PRODUCTION_HORIZON_AT = datetime(2026, 9, 29, 0, 0, 30, 374926, tzinfo=UTC)
PRODUCTION_BASE_QUANTITY = Decimal("0.00012687")
PRODUCTION_AVERAGE_COST = Decimal("78663.19854969")
PRODUCTION_CUMULATIVE_COST = Decimal("9.98")
PRODUCTION_FILL_PRICE = Decimal("78293.22")
PRODUCTION_CASH_AFTER_OPEN = Decimal("990.02")

# Decision-time midpoint chosen so the real preview arithmetic reproduces the
# production filled quantity exactly (ceil(78232.65 * 1.0015, 0.01) = 78350.00;
# floor(10 / (78350.00 * 1.006), 1e-8) = 0.00012687), and the fill-time
# midpoint that reproduces the production execution price exactly
# (ceil(78215.00 * 1.001, 0.01) = 78293.22). FIXTURE values, not history.
OPENING_DECISION_MID = Decimal("78232.65")
OPENING_FILL_MID = Decimal("78215.00")
CLOSE_DECISION_MID = Decimal("110000.00")
CLOSE_FILL_MID = Decimal("110100.00")

BOOTSTRAP_AT = datetime(2026, 8, 29, tzinfo=UTC)
OPENING_AS_OF = datetime(2026, 8, 30, tzinfo=UTC)
ADOPTION_AS_OF = datetime(2026, 9, 3, tzinfo=UTC)
ADOPTION_TICK_AT = datetime(2026, 9, 3, 12, 0, 31, tzinfo=UTC)
HORIZON_DAY_AS_OF = datetime(2026, 9, 29, tzinfo=UTC)
CLOSE_DAY_AS_OF = datetime(2026, 9, 30, tzinfo=UTC)
CLOSE_SUBMIT_TICK_AT = datetime(2026, 9, 30, 0, 0, 31, tzinfo=UTC)
CLOSE_FILL_TICK_AT = datetime(2026, 9, 30, 0, 5, 31, tzinfo=UTC)
CLOSE_REPLAY_TICK_AT = datetime(2026, 9, 30, 0, 10, 31, tzinfo=UTC)

CENT = Decimal("0.01")
SATOSHI = Decimal("0.00000001")


def _cycle_id(as_of: datetime) -> str:
    """Exactly the real supervised worker's day-scoped cycle identity."""

    return f"{PLAN_ID}-worker-{as_of.strftime('%Y%m%d')}"


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    return value.quantize(increment, rounding=ROUND_FLOOR)


def _ceil(value: Decimal, increment: Decimal) -> Decimal:
    return value.quantize(increment, rounding=ROUND_CEILING)


def _state(env: _Executors) -> CryptoPaperState:
    state = env.crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
    assert state is not None
    return state


def _order(state: CryptoPaperState, order_id: str) -> CryptoPaperOrder:
    return next(item for item in state.orders if item.order_id == order_id)


def _seed_failed_observation(env: _Executors, *, captured_at: datetime) -> LiveMarketObservation:
    """A genuine capture attempt that could not produce a usable quote --
    the exact shape ``ExecuteLiveMarketObservationRound`` retains when the
    provider fails for one instrument. ``evaluate_contemporaneous_observation``
    treats it as ABSENT, never as evidence."""

    plan_id = "closure-readiness-live-plan"
    round_id = live_observation_round_id(plan_id=plan_id, window_start=captured_at)
    if env.live_observation.capture_round(round_id) is None:
        env.live_observation.save_capture_round(
            LiveObservationCaptureRound.create(
                plan_id=plan_id,
                venue=SharedVenue.CRYPTO,
                provider="COINBASE",
                window_start=captured_at,
                requested_at=captured_at,
                completed_at=captured_at,
                status=LiveObservationRoundStatus.FAILED,
                requested_instrument_count=1,
                succeeded_count=0,
                failed_count=1,
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
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        provider_schema_version="TEST_V1",
        source_identity="TEST_SOURCE",
        quality=LiveObservationQuality.FAILED,
        quality_reasons=("PROVIDER_UNAVAILABLE",),
    )
    env.live_observation.save_observation(observation)
    return observation


class _ProductionShapedTrial:
    """Drive the real code paths to the exact retained production state:
    a legacy-seed BUY, filled, then adopted as generation 1 -- and keep the
    handles later scenarios need."""

    def __init__(self, env: _Executors) -> None:
        self.env = env
        _bootstrap_universe_and_candles(crypto_repo=env.crypto, requested_at=BOOTSTRAP_AT)
        self.plan: OperatingCyclePlan = _plan(created_at=BOOTSTRAP_AT)
        executor = env.executor
        executor._ensure_strategy_crypto_paper_account(plan=self.plan)

        # ---- The pre-0111 lifetime-seed opening BUY, exactly as production
        # placed it on 2026-08-30T00:00:30.374926Z: same catalog definition,
        # same promotion, same seed function, same application service.
        candles = executor._strategy_paper_candle_history(
            plan=self.plan, ingested_at=PRODUCTION_OPENED_AT
        )
        definition = next(
            item
            for item in canonical_strategy_benchmarks(created_at=PRODUCTION_OPENED_AT)
            if item.rule_kind is StrategyRuleKind.BUY_AND_HOLD
        )
        assert definition.strategy_id == PRODUCTION_STRATEGY_ID
        backtest = simulate_strategy_backtest(
            definition,
            candles,
            evidence_class=candles[0].evidence_class,
            evidence_provenance=candles[0].capture_id,
        )
        evaluation = evaluate_external_strategy(definition, backtest)
        promoted = promote_to_paper_eligible(
            definition, evaluation=evaluation, promoted_at=PRODUCTION_OPENED_AT
        )
        assert promoted.version == PROMOTED_STRATEGY_VERSION
        pulse_plan = env.crypto.refresh_plan(PULSE_PLAN_ID)
        assert pulse_plan is not None
        universe = env.crypto.universe_snapshot(pulse_plan.universe_snapshot_id)
        assert universe is not None
        contract = executor._crypto_contract(
            BENCHMARK_INSTRUMENT, universe=universe, executed_at=PRODUCTION_OPENED_AT
        )
        assert contract is not None
        decision_observation = _seed_observation(
            env.live_observation, price=OPENING_DECISION_MID, captured_at=OPENING_AS_OF
        )
        decision_evidence = price_evidence_from_live_observation(
            decision_observation, product_id=BENCHMARK_INSTRUMENT
        )
        seed = recurring_strategy_paper_trial_seed(
            strategy_id=promoted.strategy_id,
            strategy_version=promoted.version,
            instrument_id=BENCHMARK_INSTRUMENT,
        )
        projection = build_strategy_opportunity_projection(
            definition=promoted,
            evaluation=evaluation,
            reference_price=decision_evidence.price,
            generated_at=PRODUCTION_OPENED_AT,
            opportunity_ordinal=f"{_cycle_id(OPENING_AS_OF)}-recurring",
        )
        placement = ExecuteStrategyPaperTrial(env.crypto_paper).place_order(
            definition=promoted,
            projection=projection,
            contract=contract,
            universe=universe,
            account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
            price_evidence=decision_evidence,
            operating_cycle_enabled=True,
            executed_at=PRODUCTION_OPENED_AT,
            actor=STRATEGY_PAPER_TRIAL_ACTOR,
            idempotency_seed=seed,
        )
        assert placement.order is not None
        assert placement.order.order_id == PRODUCTION_OPENING_ORDER_ID
        assert placement.order.authorization_id == PRODUCTION_OPENING_AUTHORIZATION_ID
        assert placement.order.submitted_at == PRODUCTION_OPENED_AT
        assert placement.order.target_base_quantity == PRODUCTION_BASE_QUANTITY

        # ---- Its fill at 2026-08-30T00:04:30.814717Z, through the real
        # evidence-application service with the real fill-id convention.
        fill_observation = _seed_observation(
            env.live_observation, price=OPENING_FILL_MID, captured_at=PRODUCTION_FILLED_AT
        )
        applied = ExecuteStrategyPaperTrial(env.crypto_paper).apply_evidence(
            account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
            order_id=PRODUCTION_OPENING_ORDER_ID,
            fill_id=f"{PRODUCTION_OPENING_ORDER_ID}-fill-{_cycle_id(OPENING_AS_OF)}",
            evidence=price_evidence_from_live_observation(
                fill_observation, product_id=BENCHMARK_INSTRUMENT
            ),
            actor=STRATEGY_PAPER_TRIAL_ACTOR,
            idempotency_key=f"{PRODUCTION_OPENING_ORDER_ID}-fill-{_cycle_id(OPENING_AS_OF)}-key",
        )
        assert applied.value is not None
        assert applied.value.fill.price == PRODUCTION_FILL_PRICE
        position = applied.value.position
        assert position.state == "OPEN"
        assert position.base_quantity == PRODUCTION_BASE_QUANTITY
        assert position.average_cost == PRODUCTION_AVERAGE_COST
        assert position.cumulative_cost == PRODUCTION_CUMULATIVE_COST
        assert applied.value.account_state.cash == PRODUCTION_CASH_AFTER_OPEN

        # ---- Adoption by the real recurring task (production: 2026-09-03).
        _seed_observation(
            env.live_observation, price=Decimal("80000"), captured_at=ADOPTION_TICK_AT
        )
        adopted = self.tick(as_of=ADOPTION_AS_OF, executed_at=ADOPTION_TICK_AT)
        assert adopted.status is not OperatingTaskStatus.FAILED
        self.trial = self.slot()[0]
        assert self.trial.trial_id == PRODUCTION_TRIAL_ID
        assert self.trial.legacy_adopted is True
        assert self.trial.generation == 1
        assert self.trial.opened_at == PRODUCTION_OPENED_AT
        assert self.trial.horizon_target_at == PRODUCTION_HORIZON_AT
        assert self.trial.opening_authorization_id == PRODUCTION_OPENING_AUTHORIZATION_ID
        assert self.trial.opening_order_id == PRODUCTION_OPENING_ORDER_ID
        assert self.trial.opening_realized_pnl_baseline == Decimal(0)
        assert self.trial.policy_version == STRATEGY_PAPER_TRIAL_LIFECYCLE_POLICY_V1
        assert self.closure() is None
        self.close_order_id = f"{self.trial.trial_id}-close-order"
        self.close_authorization_id = f"{self.trial.trial_id}-close-authorization"

    def tick(self, *, as_of: datetime, executed_at: datetime):
        return self.env.run_cycle(
            cycle_id=_cycle_id(as_of), as_of=as_of, executed_at=executed_at, plan=self.plan
        )

    def slot(self) -> tuple[StrategyPaperTrialRecord, ...]:
        return self.env.strategy_paper_trials.for_slot(
            strategy_id=PRODUCTION_STRATEGY_ID,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
        )

    def closure(self):
        return self.env.strategy_paper_trial_closures.for_trial(trial_id=PRODUCTION_TRIAL_ID)

    def rejections(self, execution) -> tuple[str, ...]:
        prefix = f"STRATEGY_PAPER_REJECTED:{PRODUCTION_STRATEGY_ID}:"
        return tuple(
            item.removeprefix(prefix) for item in execution.warnings if item.startswith(prefix)
        )

    # ---- Convenience drivers for the horizon boundary.
    def submit_close(self):
        """The first tick of the first UTC day whose midnight ``as_of`` is at
        or after the horizon -- the exact production shape."""

        _seed_observation(
            self.env.live_observation,
            price=CLOSE_DECISION_MID,
            captured_at=CLOSE_SUBMIT_TICK_AT - timedelta(seconds=26),
        )
        return self.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_SUBMIT_TICK_AT)

    def fill_close(self):
        _seed_observation(
            self.env.live_observation,
            price=CLOSE_FILL_MID,
            captured_at=CLOSE_FILL_TICK_AT - timedelta(seconds=26),
        )
        return self.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_FILL_TICK_AT)


def _expected_close_economics() -> dict[str, Decimal]:
    """The domain's own documented SELL arithmetic (``domain.crypto_paper.
    apply_retained_evidence``), recomputed independently here so the test
    never merely echoes whatever the code produced."""

    execution_price = _floor(CLOSE_FILL_MID * (Decimal(1) - Decimal("0.001")), CENT)
    quote = _floor(PRODUCTION_BASE_QUANTITY * execution_price, CENT)
    fee = _floor(quote * Decimal("0.006"), CENT)
    cost_basis = _ceil(PRODUCTION_AVERAGE_COST * PRODUCTION_BASE_QUANTITY, SATOSHI)
    net_proceeds = quote - fee
    realized = net_proceeds - cost_basis
    return {
        "execution_price": execution_price,
        "quote": quote,
        "fee": fee,
        "cost_basis": cost_basis,
        "net_proceeds": net_proceeds,
        "realized": realized,
        "cash_after": PRODUCTION_CASH_AFTER_OPEN + net_proceeds,
    }


@pytest.fixture
def env(postgres_engine: sa.Engine) -> _Executors:
    return _Executors(postgres_engine)


class _RaiseOnce:
    """Simulates a process crash at one exact durable boundary: the first
    call raises (any work the real callee already committed BEFORE this
    boundary stays committed, exactly like a real crash), every later call
    delegates to the real implementation (the restarted worker)."""

    def __init__(self, original: Callable[..., Any]) -> None:
        self._original = original
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("SIMULATED_WORKER_CRASH")
        return self._original(*args, **kwargs)


def _raise_once_method(original: Callable[..., Any]) -> tuple[_RaiseOnce, Callable[..., Any]]:
    """``_RaiseOnce`` for an UNBOUND class method (the executor instantiates
    the service fresh on every tick, so the instance cannot be patched):
    a plain function keeps Python's normal ``self`` binding intact."""

    crash = _RaiseOnce(original)

    def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        return crash(self, *args, **kwargs)

    return crash, method


class TestProductionShapedFixture:
    def test_reproduces_the_retained_production_trial_byte_for_byte(self, env: _Executors) -> None:
        driven = _ProductionShapedTrial(env)
        assert driven.trial.canonical_hash == (
            "701cfaeea9939e9b1ae116d39249b0fc79d2399ef0e1bbc7529a663e5638e4a8"
        )
        state = _state(env)
        assert [item.order_id for item in state.orders] == [PRODUCTION_OPENING_ORDER_ID]
        assert _order(state, PRODUCTION_OPENING_ORDER_ID).state is PaperOrderState.FILLED


class TestHorizonBoundary:
    """Invariant 1: no closure before the exact trial horizon -- with the
    production worker's day-scoped ``as_of``."""

    def test_pure_horizon_rule_against_the_real_production_timestamps(self) -> None:
        trial = StrategyPaperTrialRecord(
            trial_id=PRODUCTION_TRIAL_ID,
            strategy_id=PRODUCTION_STRATEGY_ID,
            strategy_version=PROMOTED_STRATEGY_VERSION,
            instrument_id=BENCHMARK_INSTRUMENT,
            account_id=GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID,
            generation=1,
            opened_at=PRODUCTION_OPENED_AT,
            opening_authorization_id=PRODUCTION_OPENING_AUTHORIZATION_ID,
            opening_order_id=PRODUCTION_OPENING_ORDER_ID,
            opening_realized_pnl_baseline=Decimal(0),
            horizon_days=30,
            horizon_target_at=PRODUCTION_HORIZON_AT,
            cooldown_days=1,
            policy_version=STRATEGY_PAPER_TRIAL_LIFECYCLE_POLICY_V1,
            legacy_adopted=True,
            canonical_hash="0" * 64,
        )
        before = TrialHorizonState.BEFORE_HORIZON
        at_or_after = TrialHorizonState.AT_OR_AFTER_HORIZON
        # horizon - epsilon
        assert (
            evaluate_trial_horizon(
                trial=trial, as_of=PRODUCTION_HORIZON_AT - timedelta(microseconds=1)
            )
            is before
        )
        # exact horizon
        assert evaluate_trial_horizon(trial=trial, as_of=PRODUCTION_HORIZON_AT) is at_or_after
        # horizon + epsilon
        assert (
            evaluate_trial_horizon(
                trial=trial, as_of=PRODUCTION_HORIZON_AT + timedelta(microseconds=1)
            )
            is at_or_after
        )
        # The real worker only ever passes UTC midnight: 2026-09-29T00:00:00Z
        # is 30.374926 seconds BEFORE the horizon, so the whole of the
        # nominal horizon day is BEFORE_HORIZON; 2026-09-30T00:00:00Z is the
        # first cycle day at or after it.
        assert evaluate_trial_horizon(trial=trial, as_of=HORIZON_DAY_AS_OF) is before
        assert evaluate_trial_horizon(trial=trial, as_of=CLOSE_DAY_AS_OF) is at_or_after

    def test_no_close_is_submitted_on_the_nominal_horizon_day(self, env: _Executors) -> None:
        driven = _ProductionShapedTrial(env)
        # Fresh evidence on every tick -- proving the abstention is the
        # horizon rule alone, never a missing-evidence side effect.
        for executed_at in (
            datetime(2026, 9, 28, 23, 55, 31, tzinfo=UTC),
            datetime(2026, 9, 29, 0, 0, 29, tzinfo=UTC),
            datetime(2026, 9, 29, 0, 0, 31, tzinfo=UTC),  # wall clock past the horizon
            datetime(2026, 9, 29, 12, 0, 31, tzinfo=UTC),
            datetime(2026, 9, 29, 23, 55, 31, tzinfo=UTC),
        ):
            as_of = executed_at.replace(hour=0, minute=0, second=0, microsecond=0)
            _seed_observation(
                env.live_observation,
                price=CLOSE_DECISION_MID,
                captured_at=executed_at - timedelta(seconds=26),
            )
            execution = driven.tick(as_of=as_of, executed_at=executed_at)
            assert execution.status is not OperatingTaskStatus.FAILED
            state = _state(env)
            assert len(state.orders) == 1, executed_at
            assert not any(
                item.authorization_id == driven.close_authorization_id
                for item in state.authorizations
            ), executed_at
            assert driven.closure() is None
            assert "STRATEGY_PAPER_TRIALS_CLOSED:0" in execution.warnings


class TestNormalCompleteClose:
    def test_close_submits_fills_and_finalizes_with_exact_decimal_economics(
        self, env: _Executors
    ) -> None:
        driven = _ProductionShapedTrial(env)
        expected = _expected_close_economics()

        # ---- First tick of 2026-09-30 (UTC): the closing SELL is submitted.
        submitted = driven.submit_close()
        assert submitted.status is OperatingTaskStatus.COMPLETED
        assert driven.close_order_id in submitted.output_identities
        state = _state(env)
        assert [item.order_id for item in state.orders] == [
            PRODUCTION_OPENING_ORDER_ID,
            driven.close_order_id,
        ]
        close_order = _order(state, driven.close_order_id)
        assert close_order.side is PaperSide.SELL
        assert close_order.state is PaperOrderState.OPEN
        assert close_order.target_base_quantity == PRODUCTION_BASE_QUANTITY
        assert close_order.submitted_at == CLOSE_SUBMIT_TICK_AT
        assert close_order.valid_until == CLOSE_SUBMIT_TICK_AT + timedelta(hours=4)
        authorization = next(
            item
            for item in state.authorizations
            if item.authorization_id == driven.close_authorization_id
        )
        assert authorization.allowed_side is PaperSide.SELL
        assert authorization.maximum_base_size == PRODUCTION_BASE_QUANTITY
        assert authorization.maximum_quote_notional == _ceil(
            PRODUCTION_BASE_QUANTITY * CLOSE_DECISION_MID * Decimal("1.1"), CENT
        )
        assert authorization.live_authority == "NONE"
        reservation = next(
            item for item in state.reservations if item.order_id == driven.close_order_id
        )
        assert reservation.asset == "BTC"
        assert reservation.remaining_amount == PRODUCTION_BASE_QUANTITY
        assert reservation.state is PaperReservationState.HELD
        assert state.account_state.balance("BTC").reserved == PRODUCTION_BASE_QUANTITY
        assert driven.closure() is None  # never before the fill is real
        assert "STRATEGY_PAPER_TRIALS_CLOSED:0" in submitted.warnings

        # ---- Same tick delivered twice (duplicate task delivery): no
        # second SELL, byte-identical account state.
        replayed = driven.submit_close()
        assert len(_state(env).orders) == 2
        # Only the per-tick reconciliation row may advance on a replay --
        # assert on the economic rows directly, never on the whole hash.
        assert _order(_state(env), driven.close_order_id) == close_order
        assert _state(env).account_state == state.account_state
        assert driven.closure() is None
        assert "STRATEGY_PAPER_TRIALS_CLOSED:0" in replayed.warnings

        # ---- Next tick: fresh post-submission evidence fills the SELL and
        # the closure is finalized in the same dispatch (Phase F then T').
        filled = driven.fill_close()
        assert filled.status is OperatingTaskStatus.COMPLETED
        assert "STRATEGY_PAPER_FILLS_APPLIED:1" in filled.warnings
        assert "STRATEGY_PAPER_TRIALS_CLOSED:1" in filled.warnings
        assert "STRATEGY_PAPER_RECONCILIATION:CLEAN" in filled.warnings
        state = _state(env)
        close_order = _order(state, driven.close_order_id)
        assert close_order.state is PaperOrderState.FILLED
        assert close_order.filled_base_quantity == PRODUCTION_BASE_QUANTITY
        fill = next(item for item in state.fills if item.order_id == driven.close_order_id)
        assert fill.price == expected["execution_price"]
        assert fill.quote_quantity == expected["quote"]
        assert fill.fee == expected["fee"]
        assert fill.side is PaperSide.SELL
        position = next(item for item in state.positions if item.product_id == BENCHMARK_INSTRUMENT)
        assert position.state == "CLOSED"
        assert position.base_quantity == Decimal(0)
        assert position.cumulative_cost == Decimal(0)
        assert position.cumulative_sale_proceeds == expected["net_proceeds"]
        assert position.realized_paper_pnl == expected["realized"]
        assert state.account_state.cash == expected["cash_after"]
        assert state.account_state.realized_paper_pnl == expected["realized"]
        assert state.account_state.balance("BTC").total == Decimal(0)
        assert state.account_state.balance("BTC").reserved == Decimal(0)
        journal = next(item for item in state.journals if item.source_fill_id == fill.fill_id)
        debits = sum(item.amount for item in journal.postings if item.direction == "DEBIT")
        credits = sum(item.amount for item in journal.postings if item.direction == "CREDIT")
        assert debits == credits
        # Crypto PAPER Ledger Close Accounting Integrity v1: the ledger agrees
        # with the account/closure economics -- no fee is recognized twice.
        ledger = ledger_account_balances(state.journals)
        assert ledger["PAPER_CRYPTO_ASSET_COST"] == 0
        assert (
            -ledger["PAPER_REALIZED_PNL"] - ledger["PAPER_MODELED_FEE_EXPENSE"]
            == expected["realized"]
        )
        assert ledger["PAPER_MODELED_FEE_EXPENSE"] == state.account_state.modeled_fees
        assert (
            ledger_contract_discrepancies(
                account_state=state.account_state,
                fills=state.fills,
                positions=state.positions,
                journals=state.journals,
            )
            == ()
        )
        assert state.reconciliations[-1].status.value == "CLEAN"

        closure = driven.closure()
        assert closure is not None
        assert closure.closure_id == f"{PRODUCTION_TRIAL_ID}-closure"
        assert closure.terminal_reason is StrategyPaperTrialTerminalReason.TRIAL_HORIZON_EXIT
        assert closure.closing_order_id == driven.close_order_id
        assert closure.closing_authorization_id == driven.close_authorization_id
        assert closure.closed_at == CLOSE_FILL_TICK_AT
        assert closure.realized_paper_pnl == expected["realized"]
        assert closure.next_eligible_at == CLOSE_FILL_TICK_AT + timedelta(days=1)
        assert closure.authority == "NONE"
        assert closure.coverage_nominal_days == 32  # 2026-08-30 .. 2026-09-30 inclusive
        assert closure.coverage_covered_days < closure.coverage_nominal_days

        attributions = [
            item
            for item in env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
            if item.order_id == driven.close_order_id
        ]
        assert attributions
        assert any(item.position_state == "CLOSED" for item in attributions)
        assert all(item.live_authority == "NONE" for item in attributions)

        # ---- Invariant 8: a later tick after complete closure creates no
        # further SELL, fill, closure, or economic change.
        later = driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        assert later.status is not OperatingTaskStatus.FAILED
        assert "STRATEGY_PAPER_TRIALS_CLOSED:0" in later.warnings
        assert "STRATEGY_PAPER_FILLS_APPLIED:0" in later.warnings
        assert "STRATEGY_PAPER_TRIALS_CREATED:0" in later.warnings
        again = _state(env)
        assert len(again.orders) == 2
        assert len(again.fills) == 2
        assert again.account_state.cash == expected["cash_after"]
        assert driven.closure() == closure
        assert env.strategy_paper_trials.get(trial_id=PRODUCTION_TRIAL_ID) == driven.trial

        # ---- Cooldown on the next cycle day; the closed generation is never
        # reconsidered for closing and no second SELL can exist.
        _seed_observation(
            env.live_observation,
            price=CLOSE_FILL_MID,
            captured_at=datetime(2026, 10, 1, 0, 0, 5, tzinfo=UTC),
        )
        cooldown = driven.tick(
            as_of=datetime(2026, 10, 1, tzinfo=UTC),
            executed_at=datetime(2026, 10, 1, 0, 0, 31, tzinfo=UTC),
        )
        assert "COOLDOWN_NOT_YET_ELAPSED" in driven.rejections(cooldown)
        assert len(_state(env).orders) == 2
        assert driven.closure() == closure


class TestCrashRecoveryBoundaries:
    def test_crash_after_fill_before_closure_row_recovers_without_a_second_fill(
        self, env: _Executors, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driven = _ProductionShapedTrial(env)
        driven.submit_close()
        register = _RaiseOnce(env.executor._strategy_paper_trial_closures.register)
        monkeypatch.setattr(env.executor._strategy_paper_trial_closures, "register", register)
        with pytest.raises(RuntimeError, match="SIMULATED_WORKER_CRASH"):
            driven.fill_close()
        # The fill committed before the boundary; the closure did not.
        state = _state(env)
        assert _order(state, driven.close_order_id).state is PaperOrderState.FILLED
        assert len(state.fills) == 2
        assert driven.closure() is None

        recovered = driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        assert recovered.status is OperatingTaskStatus.COMPLETED
        assert "STRATEGY_PAPER_FILLS_APPLIED:0" in recovered.warnings
        assert "STRATEGY_PAPER_TRIALS_CLOSED:1" in recovered.warnings
        closure = driven.closure()
        assert closure is not None
        assert closure.closed_at == CLOSE_REPLAY_TICK_AT  # the tick that observed it
        assert closure.realized_paper_pnl == _expected_close_economics()["realized"]
        assert len(_state(env).fills) == 2
        assert register.calls == 2

    def test_crash_after_closure_row_before_attribution_recovers_idempotently(
        self, env: _Executors, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driven = _ProductionShapedTrial(env)
        driven.submit_close()
        crash, method = _raise_once_method(
            operating_cycle_tasks_module.MaterializePerformanceAttribution.execute
        )
        monkeypatch.setattr(
            operating_cycle_tasks_module.MaterializePerformanceAttribution, "execute", method
        )
        with pytest.raises(RuntimeError, match="SIMULATED_WORKER_CRASH"):
            driven.fill_close()
        closure = driven.closure()
        assert closure is not None  # committed before the boundary
        assert not [
            item
            for item in env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
            if item.order_id == driven.close_order_id and item.position_state == "CLOSED"
        ]

        recovered = driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        assert recovered.status is not OperatingTaskStatus.FAILED
        assert "STRATEGY_PAPER_TRIALS_CLOSED:0" in recovered.warnings
        assert driven.closure() == closure
        closed_attributions = [
            item
            for item in env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
            if item.order_id == driven.close_order_id and item.position_state == "CLOSED"
        ]
        assert len(closed_attributions) == 1
        assert len(_state(env).fills) == 2
        assert crash.calls == 2


class TestEvidenceFailsClosed:
    def test_stale_observation_defers_the_close_until_fresh_evidence(self, env: _Executors) -> None:
        driven = _ProductionShapedTrial(env)
        # Latest BTC-USD quote is 2 hours old at the close tick.
        _seed_observation(
            env.live_observation,
            price=CLOSE_DECISION_MID,
            captured_at=CLOSE_SUBMIT_TICK_AT - timedelta(hours=2),
        )
        stale = driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_SUBMIT_TICK_AT)
        assert "STALE_CONTEMPORANEOUS_EVIDENCE_FOR_CLOSE" in driven.rejections(stale)
        state = _state(env)
        assert len(state.orders) == 1
        assert not any(
            item.authorization_id == driven.close_authorization_id for item in state.authorizations
        )
        assert driven.closure() is None

        # Fresh evidence on a later tick -> the close proceeds normally.
        _seed_observation(
            env.live_observation,
            price=CLOSE_DECISION_MID,
            captured_at=CLOSE_FILL_TICK_AT - timedelta(seconds=26),
        )
        submitted = driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_FILL_TICK_AT)
        assert driven.close_order_id in submitted.output_identities
        assert _order(_state(env), driven.close_order_id).state is PaperOrderState.OPEN

    def test_failed_quality_observation_is_absent_evidence_not_a_price(
        self, env: _Executors
    ) -> None:
        driven = _ProductionShapedTrial(env)
        _seed_failed_observation(env, captured_at=CLOSE_SUBMIT_TICK_AT - timedelta(seconds=26))
        absent = driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_SUBMIT_TICK_AT)
        assert "ABSENT_CONTEMPORANEOUS_EVIDENCE_FOR_CLOSE" in driven.rejections(absent)
        assert len(_state(env).orders) == 1
        assert driven.closure() is None

    def test_no_fill_on_evidence_captured_before_submission(self, env: _Executors) -> None:
        driven = _ProductionShapedTrial(env)
        driven.submit_close()
        # A later tick with NO evidence newer than the submission: the SELL
        # stays OPEN, nothing fills, nothing closes.
        waiting = driven.tick(
            as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_SUBMIT_TICK_AT + timedelta(minutes=5)
        )
        assert "STRATEGY_PAPER_FILLS_APPLIED:0" in waiting.warnings
        assert _order(_state(env), driven.close_order_id).state is PaperOrderState.OPEN
        assert driven.closure() is None


class _CountingQuoteSource:
    """The real fixture Coinbase book source, counting every provider call
    per product -- so a test can prove no instrument is requested twice."""

    def __init__(self) -> None:
        self._inner = FixtureCoinbaseUniverseSource()
        self.evidence_class = self._inner.evidence_class
        self.calls: list[str] = []

    def capture_product_book(
        self, *, product_id: str, maximum_depth: int, requested_at: datetime
    ) -> CapturedProductBook:
        self.calls.append(product_id)
        return self._inner.capture_product_book(
            product_id=product_id, maximum_depth=maximum_depth, requested_at=requested_at
        )


class _Universes:
    def __init__(self, *snapshots: CryptoUniverseSnapshot) -> None:
        self._snapshots = snapshots

    def universe_snapshots(self) -> tuple[CryptoUniverseSnapshot, ...]:
        return self._snapshots


def _production_universes(env: _Executors) -> tuple[CryptoUniverseSnapshot, CryptoUniverseSnapshot]:
    """The pinned research universe (includes BTC-USD) and a later refresh
    that -- exactly like production's 2026-09-22 refresh -- selected
    BTC-USDC as BTC's representative route instead."""

    pulse_plan = env.crypto.refresh_plan(PULSE_PLAN_ID)
    assert pulse_plan is not None
    pinned = env.crypto.universe_snapshot(pulse_plan.universe_snapshot_id)
    assert pinned is not None and BENCHMARK_INSTRUMENT in pinned.eligible_product_ids
    switched = replace(
        pinned,
        snapshot_id=f"{pinned.snapshot_id}-btc-usdc-refresh",
        eligible_product_ids=tuple(
            "BTC-USDC" if item == BENCHMARK_INSTRUMENT else item
            for item in pinned.eligible_product_ids
        ),
    )
    return pinned, switched


def _observe(
    env: _Executors, universes: _Universes, source: _CountingQuoteSource, *, now: datetime
):
    return execute_live_observer_iteration(
        universe=universes,
        requirements=PostgresExecutionObservationRequirements(
            trials=env.strategy_paper_trials,
            crypto_paper=env.crypto_paper,
            operating=PostgresOperatingCycleRepository(env.engine),
        ),
        round_service=ExecuteLiveMarketObservationRound(env.live_observation, source),
        plan_id="closure-readiness-live-observer",
        now=now,
    )


class TestExecutionRequiredLiveObservation:
    """F1: the unattended observer keeps every instrument present PAPER
    execution depends on, whatever the research universe selects."""

    def test_btc_usd_stays_observed_after_universe_switches_to_btc_usdc(
        self, env: _Executors
    ) -> None:
        driven = _ProductionShapedTrial(env)
        assert driven.closure() is None
        _pinned, switched = _production_universes(env)
        assert BENCHMARK_INSTRUMENT not in switched.eligible_product_ids
        source = _CountingQuoteSource()
        now = CLOSE_SUBMIT_TICK_AT - timedelta(seconds=60)

        iteration = _observe(env, _Universes(_pinned, switched), source, now=now)

        assert iteration.requested_instrument_ids[0] == BENCHMARK_INSTRUMENT
        assert "BTC-USDC" in iteration.requested_instrument_ids
        # Every universe product is still requested -- none displaced.
        assert set(switched.eligible_product_ids[:10]) <= set(iteration.requested_instrument_ids)
        assert len(iteration.requested_instrument_ids) == len(
            set(iteration.requested_instrument_ids)
        )
        assert len(iteration.requested_instrument_ids) <= MAX_LIVE_OBSERVATION_INSTRUMENTS_PER_ROUND
        assert sorted(source.calls) == sorted(iteration.requested_instrument_ids)
        assert iteration.round_status == "COMPLETED"
        assert iteration.execution_required_missing == ()
        (required,) = iteration.execution_required
        assert required.instrument_id == BENCHMARK_INSTRUMENT
        assert required.dependencies == (
            ExecutionObservationDependency.ACTIVE_STRATEGY_PAPER_TRIAL,
            ExecutionObservationDependency.OPEN_PAPER_POSITION,
        )
        latest = env.live_observation.latest_observation(
            venue=SharedVenue.CRYPTO,
            instrument_id=BENCHMARK_INSTRUMENT,
            observation_type=LiveObservationType.QUOTE,
            as_of=now,
        )
        assert latest is not None
        assert latest.round_id == iteration.round_id
        assert latest.quality is LiveObservationQuality.PASSED
        assert all(
            item.venue is SharedVenue.CRYPTO
            for item in env.live_observation.observations_for_round(iteration.round_id or "")
        )

        # Same-minute replay: the retained round, zero further provider calls.
        calls_before = len(source.calls)
        replay = _observe(env, _Universes(_pinned, switched), source, now=now)
        assert replay.idempotent_replay is True
        assert replay.round_id == iteration.round_id
        assert len(source.calls) == calls_before

    def test_required_instrument_already_in_universe_adds_no_request(self, env: _Executors) -> None:
        _ProductionShapedTrial(env)
        pinned, _switched = _production_universes(env)
        source = _CountingQuoteSource()
        iteration = _observe(
            env, _Universes(pinned), source, now=CLOSE_SUBMIT_TICK_AT - timedelta(seconds=60)
        )
        assert set(iteration.requested_instrument_ids) == set(pinned.eligible_product_ids[:10])
        assert source.calls.count(BENCHMARK_INSTRUMENT) == 1
        assert len(source.calls) == len(set(source.calls))

    def test_btc_usd_is_released_once_no_execution_dependency_remains(
        self, env: _Executors
    ) -> None:
        driven = _ProductionShapedTrial(env)
        driven.submit_close()
        driven.fill_close()
        assert driven.closure() is not None
        _pinned, switched = _production_universes(env)
        source = _CountingQuoteSource()
        # Trial closed, position flat, no open order, PAPER activation never
        # persisted on any operating plan -> nothing requires BTC-USD.
        released = _observe(
            env, _Universes(switched), source, now=CLOSE_REPLAY_TICK_AT + timedelta(minutes=1)
        )
        assert released.execution_required == ()
        assert BENCHMARK_INSTRUMENT not in released.requested_instrument_ids
        assert released.requested_instrument_ids == switched.eligible_product_ids[:10]

        # Once PAPER activation is genuinely ENABLED, placing the next
        # generation depends on BTC-USD evidence again -- and only for that
        # declared reason.
        _register_and_activate_plan(
            PostgresOperatingCycleRepository(env.engine), created_at=BOOTSTRAP_AT
        )
        placement = _observe(
            env, _Universes(switched), source, now=CLOSE_REPLAY_TICK_AT + timedelta(minutes=2)
        )
        (required,) = placement.execution_required
        assert required.instrument_id == BENCHMARK_INSTRUMENT
        assert required.dependencies == (
            ExecutionObservationDependency.STRATEGY_PAPER_PLACEMENT_TARGET,
        )
        assert placement.requested_instrument_ids[0] == BENCHMARK_INSTRUMENT


# ---- F2/F3/F4: resumable close lifecycle and truthful attribution ----

ATTEMPT_2_SEED = f"{PRODUCTION_TRIAL_ID}-close-2"


def _seed_quote(
    env: _Executors, *, price: Decimal, captured_at: datetime, ask_depth: Decimal
) -> LiveMarketObservation:
    """``_seed_observation`` with an explicit displayed depth -- used to
    produce a genuine partial SELL fill (fill base = min(remaining, depth))."""

    plan_id = "closure-readiness-depth-plan"
    round_id = live_observation_round_id(plan_id=plan_id, window_start=captured_at)
    if env.live_observation.capture_round(round_id) is None:
        env.live_observation.save_capture_round(
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
        book_depth_ask=ask_depth,
        book_depth_bid=ask_depth,
        evidence_class=EvidenceClass.GENUINE_PUBLIC,
        provider_schema_version="TEST_V1",
        source_identity="TEST_SOURCE",
        quality=LiveObservationQuality.PASSED,
    )
    env.live_observation.save_observation(observation)
    return observation


def _tick_with_fresh_quote(driven: _ProductionShapedTrial, executed_at: datetime):
    _seed_observation(
        driven.env.live_observation,
        price=CLOSE_FILL_MID,
        captured_at=executed_at - timedelta(seconds=26),
    )
    as_of = executed_at.replace(hour=0, minute=0, second=0, microsecond=0)
    return driven.tick(as_of=as_of, executed_at=executed_at)


def _close_authorizations(state: CryptoPaperState) -> list[str]:
    return sorted(
        item.authorization_id
        for item in state.authorizations
        if item.authorization_id.startswith(f"{PRODUCTION_TRIAL_ID}-close")
    )


def _close_orders(state: CryptoPaperState) -> list[CryptoPaperOrder]:
    return [item for item in state.orders if item.side is PaperSide.SELL]


def _attributions_for(env: _Executors, order_id: str):
    return [
        item
        for item in env.performance.attributions(PaperEvidenceClass.GENUINE_PAPER)
        if item.order_id == order_id
    ]


def _assert_closed_exactly_once(driven: _ProductionShapedTrial, *, closing_order_id: str) -> None:
    closure = driven.closure()
    assert closure is not None
    assert closure.terminal_reason is StrategyPaperTrialTerminalReason.TRIAL_HORIZON_EXIT
    assert closure.closing_order_id == closing_order_id
    assert closure.closing_authorization_id == closing_order_id.removesuffix("-order") + (
        "-authorization"
    )
    assert closure.authority == "NONE"
    state = _state(driven.env)
    position = next(item for item in state.positions if item.product_id == BENCHMARK_INSTRUMENT)
    assert position.state == "CLOSED"
    assert position.base_quantity == Decimal(0)
    assert state.account_state.balance("BTC").total == Decimal(0)
    assert state.account_state.balance("BTC").reserved == Decimal(0)
    assert state.live_authority == "NONE"
    assert all(item.live_authority == "NONE" for item in state.authorizations)
    assert state.reconciliations[-1].status.value == "CLEAN"
    assert closure.realized_paper_pnl == position.realized_paper_pnl


class TestResumableCloseLifecycle:
    def test_crash_after_authorize_resumes_the_same_attempt_and_closes(
        self, env: _Executors, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driven = _ProductionShapedTrial(env)
        crash, method = _raise_once_method(
            strategy_paper_trial_module.ConstructCryptoPaperOrderIntent.execute
        )
        monkeypatch.setattr(
            strategy_paper_trial_module.ConstructCryptoPaperOrderIntent, "execute", method
        )
        with pytest.raises(RuntimeError, match="SIMULATED_WORKER_CRASH"):
            driven.submit_close()
        state = _state(env)
        assert _close_authorizations(state) == [driven.close_authorization_id]
        assert _close_orders(state) == []
        original_authorization = next(
            item
            for item in state.authorizations
            if item.authorization_id == driven.close_authorization_id
        )

        # Next tick (5 minutes later, authorization still valid): the SAME
        # attempt resumes -- never re-authorized, never a second attempt.
        resumed = _tick_with_fresh_quote(driven, CLOSE_FILL_TICK_AT)
        assert resumed.status is OperatingTaskStatus.COMPLETED
        state = _state(env)
        assert _close_authorizations(state) == [driven.close_authorization_id]
        assert (
            next(
                item
                for item in state.authorizations
                if item.authorization_id == driven.close_authorization_id
            )
            == original_authorization
        )
        assert [item.order_id for item in _close_orders(state)] == [driven.close_order_id]
        assert crash.calls == 2

        _tick_with_fresh_quote(driven, CLOSE_REPLAY_TICK_AT)
        _assert_closed_exactly_once(driven, closing_order_id=driven.close_order_id)
        assert len(_close_orders(_state(env))) == 1

    def test_crash_after_submit_never_creates_a_duplicate_sell(
        self, env: _Executors, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driven = _ProductionShapedTrial(env)
        original = strategy_paper_trial_module.SubmitCryptoPaperOrder.execute
        calls = {"count": 0}

        def submit_then_crash(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls["count"] += 1
            result = original(self, *args, **kwargs)  # SUBMIT commits ...
            if calls["count"] == 1:
                raise RuntimeError("SIMULATED_WORKER_CRASH")  # ... then the process dies
            return result

        monkeypatch.setattr(
            strategy_paper_trial_module.SubmitCryptoPaperOrder, "execute", submit_then_crash
        )
        with pytest.raises(RuntimeError, match="SIMULATED_WORKER_CRASH"):
            driven.submit_close()
        assert [item.order_id for item in _close_orders(_state(env))] == [driven.close_order_id]

        # Every later tick sees the order IN_FLIGHT: no resubmission.
        filled = _tick_with_fresh_quote(driven, CLOSE_FILL_TICK_AT)
        assert "STRATEGY_PAPER_FILLS_APPLIED:1" in filled.warnings
        assert calls["count"] == 1
        state = _state(env)
        assert [item.order_id for item in _close_orders(state)] == [driven.close_order_id]
        assert _close_authorizations(state) == [driven.close_authorization_id]
        _assert_closed_exactly_once(driven, closing_order_id=driven.close_order_id)

    def test_submission_rejection_retries_only_after_the_authorization_expires(
        self, env: _Executors, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driven = _ProductionShapedTrial(env)
        original = strategy_paper_trial_module.SubmitCryptoPaperOrder.execute
        calls = {"count": 0}

        def reject_once(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls["count"] += 1
            if calls["count"] == 1:
                raise CryptoPaperError("SIMULATED_SUBMISSION_REJECTED")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(
            strategy_paper_trial_module.SubmitCryptoPaperOrder, "execute", reject_once
        )
        rejected = driven.submit_close()
        assert any(
            item.startswith("CLOSE_PLACEMENT_FAILED:CryptoPaperError")
            for item in driven.rejections(rejected)
        )
        assert _close_orders(_state(env)) == []

        # Inside the attempt-1 authorization's hour: its preview has expired
        # (300s), so the attempt is dead -- but no second authority may
        # exist while the first is still valid.
        for minutes in (5, 30, 59):
            waiting = _tick_with_fresh_quote(
                driven, CLOSE_SUBMIT_TICK_AT + timedelta(minutes=minutes)
            )
            assert (
                "CLOSE_ATTEMPT_1_PREVIEW_EXPIRED_WITHOUT_ORDER_AWAITING_AUTHORIZATION_EXPIRY"
                in driven.rejections(waiting)
            )
            state = _state(env)
            assert _close_authorizations(state) == [driven.close_authorization_id]
            assert _close_orders(state) == []

        # At/after expiry: exactly one new attempt, deterministic identity.
        retry_at = CLOSE_SUBMIT_TICK_AT + timedelta(hours=1)
        retried = _tick_with_fresh_quote(driven, retry_at)
        assert f"{ATTEMPT_2_SEED}-order" in retried.output_identities
        state = _state(env)
        assert _close_authorizations(state) == sorted(
            [driven.close_authorization_id, f"{ATTEMPT_2_SEED}-authorization"]
        )
        (order,) = _close_orders(state)
        assert order.order_id == f"{ATTEMPT_2_SEED}-order"
        assert order.target_base_quantity == PRODUCTION_BASE_QUANTITY

        # Duplicate delivery of the retry tick: replay, no third attempt.
        _tick_with_fresh_quote(driven, retry_at)
        assert len(_close_orders(_state(env))) == 1
        assert len(_close_authorizations(_state(env))) == 2

        _tick_with_fresh_quote(driven, retry_at + timedelta(minutes=5))
        _assert_closed_exactly_once(driven, closing_order_id=f"{ATTEMPT_2_SEED}-order")

    def test_sell_expiring_unfilled_keeps_the_trial_open_but_retryable(
        self, env: _Executors
    ) -> None:
        driven = _ProductionShapedTrial(env)
        driven.submit_close()
        # No evidence newer than the submission for 4 hours (an observer
        # gap): the SELL can never fill and Phase F expires it.
        expire_at = CLOSE_SUBMIT_TICK_AT + timedelta(hours=4)
        expired = driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=expire_at)
        assert "STRATEGY_PAPER_ORDERS_EXPIRED:1" in expired.warnings
        assert "STRATEGY_PAPER_TRIALS_CLOSED:0" in expired.warnings
        state = _state(env)
        assert _order(state, driven.close_order_id).state is PaperOrderState.EXPIRED
        position = next(item for item in state.positions if item.product_id == BENCHMARK_INSTRUMENT)
        assert position.state == "OPEN"
        assert position.base_quantity == PRODUCTION_BASE_QUANTITY
        assert state.account_state.balance("BTC").reserved == Decimal(0)
        assert driven.closure() is None
        assert env.strategy_paper_trials.get(trial_id=PRODUCTION_TRIAL_ID) == driven.trial

        # F4: the expired SELL is never attributed as an economic close, and
        # the opening BUY is not closed by it either.
        expired_sell = _attributions_for(env, driven.close_order_id)
        assert expired_sell
        assert not any(item.position_state == "CLOSED" for item in expired_sell)
        latest_sell = max(
            expired_sell, key=lambda item: (item.materialized_at, item.attribution_id)
        )
        assert latest_sell.position_state == "EXPIRED"
        assert all(item.open_exposure is None for item in expired_sell)
        assert not any(
            item.position_state == "CLOSED"
            for item in _attributions_for(env, PRODUCTION_OPENING_ORDER_ID)
        )

        # Next tick with fresh evidence: attempt 2, sized by the still-open
        # position, fills on the tick after; attempt 1 stays EXPIRED.
        retry_at = expire_at + timedelta(minutes=5)
        _tick_with_fresh_quote(driven, retry_at)
        state = _state(env)
        assert [item.order_id for item in _close_orders(state)] == [
            driven.close_order_id,
            f"{ATTEMPT_2_SEED}-order",
        ]
        _tick_with_fresh_quote(driven, retry_at + timedelta(minutes=5))
        _assert_closed_exactly_once(driven, closing_order_id=f"{ATTEMPT_2_SEED}-order")
        assert _order(_state(env), driven.close_order_id).state is PaperOrderState.EXPIRED
        assert not any(
            item.position_state == "CLOSED"
            for item in _attributions_for(env, driven.close_order_id)
        )

    def test_partial_fill_then_expiry_retries_only_the_residual(self, env: _Executors) -> None:
        driven = _ProductionShapedTrial(env)
        driven.submit_close()
        # Residual 0.00010687 stays above the fixture contract's 0.0001
        # minimum base (production's BTC-USD minimum is 1 satoshi).
        partial_depth = Decimal("0.00002")
        _seed_quote(
            env,
            price=CLOSE_FILL_MID,
            captured_at=CLOSE_FILL_TICK_AT - timedelta(seconds=26),
            ask_depth=partial_depth,
        )
        driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_FILL_TICK_AT)
        state = _state(env)
        assert _order(state, driven.close_order_id).state is PaperOrderState.PARTIALLY_FILLED
        residual = PRODUCTION_BASE_QUANTITY - partial_depth
        position = next(item for item in state.positions if item.product_id == BENCHMARK_INSTRUMENT)
        assert position.base_quantity == residual
        assert driven.closure() is None

        driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_SUBMIT_TICK_AT + timedelta(hours=4))
        assert _order(_state(env), driven.close_order_id).state is PaperOrderState.EXPIRED
        assert driven.closure() is None

        retry_at = CLOSE_SUBMIT_TICK_AT + timedelta(hours=4, minutes=5)
        retried = _tick_with_fresh_quote(driven, retry_at)
        assert not driven.rejections(retried), driven.rejections(retried)
        retry = _order(_state(env), f"{ATTEMPT_2_SEED}-order")
        assert retry.target_base_quantity == residual
        _tick_with_fresh_quote(driven, retry_at + timedelta(minutes=5))
        _assert_closed_exactly_once(driven, closing_order_id=f"{ATTEMPT_2_SEED}-order")
        fills = [item for item in _state(env).fills if item.side is PaperSide.SELL]
        assert sum(item.base_quantity for item in fills) == PRODUCTION_BASE_QUANTITY


class TestClosedTrialAttribution:
    def test_eventual_close_supersedes_the_open_buy_attribution(self, env: _Executors) -> None:
        driven = _ProductionShapedTrial(env)
        opening_rows = _attributions_for(env, PRODUCTION_OPENING_ORDER_ID)
        assert len(opening_rows) == 1
        (historical,) = opening_rows
        assert historical.position_state == "FILLED"
        assert historical.open_exposure is not None

        driven.submit_close()
        driven.fill_close()
        closure = driven.closure()
        assert closure is not None

        rows = sorted(
            _attributions_for(env, PRODUCTION_OPENING_ORDER_ID),
            key=lambda item: (item.materialized_at, item.attribution_id),
        )
        # The historical OPEN-era row is retained unchanged (auditable) ...
        assert rows[0] == historical
        # ... and exactly one superseding row carries the closed outcome.
        assert len(rows) == 2
        superseding = rows[-1]
        assert superseding.position_state == "CLOSED"
        assert superseding.open_exposure is None
        assert superseding.source_hash != historical.source_hash
        closing_fill = next(
            item for item in _state(env).fills if item.order_id == driven.close_order_id
        )
        assert superseding.time_to_completion_seconds == int(
            (closing_fill.event_time - PRODUCTION_OPENED_AT).total_seconds()
        )
        assert superseding.materialized_at > historical.materialized_at
        assert superseding.live_authority == "NONE"

        # Replay after close: no further rows (stable identity).
        driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_REPLAY_TICK_AT)
        assert len(_attributions_for(env, PRODUCTION_OPENING_ORDER_ID)) == 2
        closing_rows = _attributions_for(env, driven.close_order_id)
        assert [item.position_state for item in closing_rows].count("CLOSED") == 1

    def test_unsellable_residual_never_mints_repeated_close_authority(
        self, env: _Executors
    ) -> None:
        driven = _ProductionShapedTrial(env)
        driven.submit_close()
        _seed_quote(
            env,
            price=CLOSE_FILL_MID,
            captured_at=CLOSE_FILL_TICK_AT - timedelta(seconds=26),
            ask_depth=Decimal("0.0001"),  # residual 0.00002687 < fixture minimum 0.0001
        )
        driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_FILL_TICK_AT)
        driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=CLOSE_SUBMIT_TICK_AT + timedelta(hours=4))
        for hours in (4.1, 5.2, 6.3):
            blocked = _tick_with_fresh_quote(driven, CLOSE_SUBMIT_TICK_AT + timedelta(hours=hours))
            assert "CLOSE_QUANTITY_BELOW_CONTRACT_MINIMUM_BASE" in driven.rejections(blocked)
        state = _state(env)
        assert _close_authorizations(state) == [driven.close_authorization_id]
        assert len(_close_orders(state)) == 1
        assert driven.closure() is None  # truthfully still open, never falsely closed

    def test_close_over_the_per_order_notional_cap_mints_no_authority(
        self, env: _Executors
    ) -> None:
        driven = _ProductionShapedTrial(env)
        # 0.00012687 BTC x 250,000 = $31.72 > MAX_ORDER_NOTIONAL ($25).
        for minutes in (0, 65, 130):
            executed_at = CLOSE_SUBMIT_TICK_AT + timedelta(minutes=minutes)
            _seed_observation(
                env.live_observation,
                price=Decimal("250000"),
                captured_at=executed_at - timedelta(seconds=26),
            )
            blocked = driven.tick(as_of=CLOSE_DAY_AS_OF, executed_at=executed_at)
            assert "CLOSE_NOTIONAL_EXCEEDS_MAX_ORDER_NOTIONAL" in driven.rejections(blocked)
        state = _state(env)
        assert _close_authorizations(state) == []
        assert _close_orders(state) == []
        assert driven.closure() is None


class _FailingRequirements:
    def required(self, *, at: datetime):
        raise ValueError("SIMULATED_REQUIREMENTS_HYDRATION_FAILURE")


def test_observer_degrades_to_universe_when_requirements_cannot_be_resolved(
    env: _Executors,
) -> None:
    _ProductionShapedTrial(env)
    _pinned, switched = _production_universes(env)
    source = _CountingQuoteSource()
    iteration = execute_live_observer_iteration(
        universe=_Universes(switched),
        requirements=_FailingRequirements(),
        round_service=ExecuteLiveMarketObservationRound(env.live_observation, source),
        plan_id="closure-readiness-live-observer",
        now=CLOSE_SUBMIT_TICK_AT - timedelta(seconds=60),
    )
    # Observation continues over the universe; the failure is explicit.
    assert iteration.requested_instrument_ids == switched.eligible_product_ids[:10]
    assert iteration.round_status == "COMPLETED"
    assert iteration.execution_required_resolution_error == (
        "ValueError:SIMULATED_REQUIREMENTS_HYDRATION_FAILURE"
    )
