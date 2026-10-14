"""PAPER Trial Closure Readiness v1: resolve, from retained PostgreSQL state,
the instruments present PAPER execution depends on for contemporaneous
``LiveMarketObservation`` evidence -- so the unattended live observer keeps
observing them regardless of which representative routes the latest
RESEARCH universe happens to select (see ``domain.live_market_observation.
execution_required_live_instruments`` for the invariant and the 2026-09-22
BTC-USD -> BTC-USDC incident that motivated it).

Read-only. Makes no provider request, writes nothing, and grants no
authority. Only lifecycles that genuinely consume live observations are
sources -- today exactly the recurring strategy PAPER path
(``adapters.operating_cycle_tasks._execute_strategy_paper_trial``: placement,
horizon close, and fill evidence all read the canonical contemporaneous
observation), on its own dedicated account:

* every ACTIVE strategy PAPER trial (no closure row) -> its instrument;
* every OPEN/PARTIALLY_FILLED order on the strategy PAPER account (exactly
  the orders that task's Phase F fills/expires from live evidence);
* every OPEN position with a positive base quantity on that account;
* the strategy catalog's placement targets, but ONLY while PAPER activation
  is effectively ENABLED on at least one retained operating plan (placing a
  new generation requires SATISFIED contemporaneous evidence; with
  activation disabled no placement can happen, so nothing is required).

An instrument none of these presently needs is not retained -- the observer
is never permanently forced to keep an instrument merely because it was
once traded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from apex.adapters.crypto_paper_postgres import PostgresCryptoPaperRepository
from apex.adapters.operating_cycle_postgres import PostgresOperatingCycleRepository
from apex.adapters.operating_cycle_tasks import GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID
from apex.adapters.strategy_paper_trial_lifecycle_postgres import (
    PostgresStrategyPaperTrialRepository,
)
from apex.application.live_market_observation import (
    LIVE_OBSERVATION_MAX_BOOK_DEPTH,
    ExecuteLiveMarketObservationRound,
)
from apex.domain.crypto_paper import PaperOrderState
from apex.domain.crypto_universe import CryptoUniverseSnapshot
from apex.domain.live_market_observation import (
    ExecutionRequiredInstrument,
    LiveObservationQuality,
    compose_live_observation_round_instruments,
    execution_required_live_instruments,
)
from apex.domain.operating_cycle import effective_paper_activation
from apex.domain.shared_opportunity import SharedVenue
from apex.domain.strategy_benchmark import canonical_strategy_benchmarks

_EVIDENCE_CONSUMING_ORDER_STATES = frozenset(
    {PaperOrderState.OPEN, PaperOrderState.PARTIALLY_FILLED}
)


class PostgresExecutionObservationRequirements:
    def __init__(
        self,
        *,
        trials: PostgresStrategyPaperTrialRepository,
        crypto_paper: PostgresCryptoPaperRepository,
        operating: PostgresOperatingCycleRepository,
    ) -> None:
        self._trials = trials
        self._crypto_paper = crypto_paper
        self._operating = operating

    def strategy_paper_placement_enabled(self) -> bool:
        return any(
            effective_paper_activation(self._operating.paper_activation_events(plan.plan_id))
            for plan in self._operating.plans()
        )

    def required(self, *, at: datetime) -> tuple[ExecutionRequiredInstrument, ...]:
        active_trials = tuple(item.instrument_id for item in self._trials.active_trials())
        state = self._crypto_paper.state(GENUINE_STRATEGY_CRYPTO_PAPER_ACCOUNT_ID)
        open_orders: tuple[str, ...] = ()
        open_positions: tuple[str, ...] = ()
        if state is not None:
            open_orders = tuple(
                item.product_id
                for item in state.orders
                if item.state in _EVIDENCE_CONSUMING_ORDER_STATES
            )
            open_positions = tuple(
                item.product_id
                for item in state.positions
                if item.state == "OPEN" and item.base_quantity > 0
            )
        placement_targets: tuple[str, ...] = ()
        if self.strategy_paper_placement_enabled():
            placement_targets = tuple(
                instrument_id
                for definition in canonical_strategy_benchmarks(created_at=at)
                for instrument_id in definition.instrument_ids
            )
        return execution_required_live_instruments(
            active_trial_instrument_ids=active_trials,
            open_order_instrument_ids=open_orders,
            open_position_instrument_ids=open_positions,
            placement_target_instrument_ids=placement_targets,
        )


class _UniverseSnapshots(Protocol):
    def universe_snapshots(self) -> tuple[CryptoUniverseSnapshot, ...]: ...


class _RequiredInstruments(Protocol):
    def required(self, *, at: datetime) -> tuple[ExecutionRequiredInstrument, ...]: ...


@dataclass(frozen=True, slots=True)
class LiveObserverIteration:
    """One observer iteration's outcome. ``execution_required_missing`` lists
    every execution-required instrument with no PASSED observation in this
    round -- always empty when ``round_status`` is ``COMPLETED``, since every
    required instrument is part of the requested set."""

    round_id: str | None
    window_start: datetime
    round_status: str
    requested_instrument_ids: tuple[str, ...]
    succeeded_count: int
    failed_count: int
    idempotent_replay: bool
    execution_required: tuple[ExecutionRequiredInstrument, ...]
    execution_required_missing: tuple[str, ...]
    execution_required_resolution_error: str | None = None


def execute_live_observer_iteration(
    *,
    universe: _UniverseSnapshots,
    requirements: _RequiredInstruments,
    round_service: ExecuteLiveMarketObservationRound,
    plan_id: str,
    now: datetime,
) -> LiveObserverIteration:
    window_start = now.replace(second=0, microsecond=0)
    snapshots = universe.universe_snapshots()
    universe_ids = snapshots[-1].eligible_product_ids if snapshots else ()
    # Resolving execution requirements reads more retained state than the
    # universe alone. If that ever fails, observation must degrade to the
    # research universe -- never stop entirely -- and say so loudly.
    resolution_error: str | None = None
    try:
        required = requirements.required(at=now)
        instrument_ids = compose_live_observation_round_instruments(
            required=tuple(item.instrument_id for item in required), universe=universe_ids
        )
    except Exception as error:  # degrade, never halt observation
        resolution_error = f"{type(error).__name__}:{error}"
        required = ()
        instrument_ids = compose_live_observation_round_instruments(
            required=(), universe=universe_ids
        )
    if not instrument_ids:
        return LiveObserverIteration(
            round_id=None,
            window_start=window_start,
            round_status="SKIPPED_NO_UNIVERSE",
            requested_instrument_ids=(),
            succeeded_count=0,
            failed_count=0,
            idempotent_replay=False,
            execution_required=required,
            execution_required_missing=(),
            execution_required_resolution_error=resolution_error,
        )
    result = round_service.execute(
        plan_id=plan_id,
        venue=SharedVenue.CRYPTO,
        provider="COINBASE",
        instrument_ids=instrument_ids,
        window_start=window_start,
        requested_at=now,
        provider_schema_version="COINBASE_ADVANCED_TRADE_PUBLIC_PRODUCT_BOOK_V1",
        source_identity="COINBASE_PUBLIC_PRODUCT_BOOK",
        maximum_book_depth=LIVE_OBSERVATION_MAX_BOOK_DEPTH,
    )
    observed = {
        item.instrument_id
        for item in result.observations
        if item.quality is LiveObservationQuality.PASSED
    }
    return LiveObserverIteration(
        round_id=result.round.round_id,
        window_start=window_start,
        round_status=result.round.status.value,
        requested_instrument_ids=instrument_ids,
        succeeded_count=result.round.succeeded_count,
        failed_count=result.round.failed_count,
        idempotent_replay=result.idempotent_replay,
        execution_required=required,
        execution_required_missing=tuple(
            item.instrument_id for item in required if item.instrument_id not in observed
        ),
        execution_required_resolution_error=resolution_error,
    )
