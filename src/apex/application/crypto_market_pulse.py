"""Application services for bounded crypto market-pulse refreshes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

from apex.application.crypto_universe import (
    CryptoUniverseConflict,
    CryptoUniverseRepository,
    InMemoryCryptoUniverseRepository,
    PublicCryptoUniverseSource,
)
from apex.domain.crypto_market_pulse import (
    CRYPTO_MARKET_PULSE_WARNING,
    PULSE_MAX_BOOK_DEPTH,
    CryptoMarketChangeSummary,
    CryptoOpportunityLineage,
    CryptoPulseCheckpoint,
    CryptoPulseSnapshot,
    CryptoRefreshEvent,
    CryptoRefreshPlan,
    CryptoRefreshRun,
    PulseQuality,
    RefreshState,
    build_change_summary,
    build_opportunity_lineage,
    build_pulse_snapshot,
    completed_utc_day,
    daily_refresh_due,
)
from apex.domain.crypto_universe import (
    FEE_BPS_PER_SIDE,
    SLIPPAGE_BPS_PER_SIDE,
    CryptoCurrentMarketEvidence,
    CryptoDiscoveryRun,
    CryptoProductObservation,
    CryptoResearchOpportunity,
    CryptoUniverseSnapshot,
    build_opportunities,
    build_relative_strength_opportunities,
    build_reversal_regime_opportunities,
    run_discovery,
    run_relative_strength,
    run_reversal_regime,
)
from apex.domain.values import content_digest


class _Canonical(Protocol):
    @property
    def canonical_hash(self) -> str: ...


class CryptoMarketPulseRepository(CryptoUniverseRepository, Protocol):
    def refresh_plan(self, plan_id: str) -> CryptoRefreshPlan | None: ...
    def save_refresh_plan(self, plan: CryptoRefreshPlan) -> CryptoRefreshPlan: ...
    def refresh_plans(self) -> tuple[CryptoRefreshPlan, ...]: ...
    def refresh_run(self, refresh_id: str) -> CryptoRefreshRun | None: ...
    def refresh_for_plan_as_of(self, plan_id: str, as_of: datetime) -> CryptoRefreshRun | None: ...
    def save_refresh_run(self, refresh: CryptoRefreshRun) -> CryptoRefreshRun: ...
    def refresh_runs(self) -> tuple[CryptoRefreshRun, ...]: ...
    def refresh_events(self, refresh_id: str) -> tuple[CryptoRefreshEvent, ...]: ...
    def save_refresh_event(self, event: CryptoRefreshEvent) -> CryptoRefreshEvent: ...
    def pulse_checkpoint(self, checkpoint_id: str) -> CryptoPulseCheckpoint | None: ...
    def save_pulse_checkpoint(self, checkpoint: CryptoPulseCheckpoint) -> CryptoPulseCheckpoint: ...
    def pulse_checkpoints(self, refresh_id: str) -> tuple[CryptoPulseCheckpoint, ...]: ...
    def pulse_snapshot(self, pulse_id: str) -> CryptoPulseSnapshot | None: ...
    def save_pulse_snapshot(self, pulse: CryptoPulseSnapshot) -> CryptoPulseSnapshot: ...
    def pulse_snapshots(self) -> tuple[CryptoPulseSnapshot, ...]: ...
    def opportunity_lineage(self, lineage_id: str) -> CryptoOpportunityLineage | None: ...
    def save_opportunity_lineages(
        self, values: tuple[CryptoOpportunityLineage, ...]
    ) -> tuple[CryptoOpportunityLineage, ...]: ...
    def opportunity_lineages(self) -> tuple[CryptoOpportunityLineage, ...]: ...
    def market_change_summary(self, summary_id: str) -> CryptoMarketChangeSummary | None: ...
    def save_market_change_summary(
        self, summary: CryptoMarketChangeSummary
    ) -> CryptoMarketChangeSummary: ...
    def market_change_summaries(self) -> tuple[CryptoMarketChangeSummary, ...]: ...


class InMemoryCryptoMarketPulseRepository(InMemoryCryptoUniverseRepository):
    def __init__(self) -> None:
        super().__init__()
        self._pulse_plans: dict[str, CryptoRefreshPlan] = {}
        self._pulse_runs: dict[str, CryptoRefreshRun] = {}
        self._pulse_events: dict[str, CryptoRefreshEvent] = {}
        self._pulse_checkpoints: dict[str, CryptoPulseCheckpoint] = {}
        self._pulse_snapshots: dict[str, CryptoPulseSnapshot] = {}
        self._pulse_lineages: dict[str, CryptoOpportunityLineage] = {}
        self._pulse_summaries: dict[str, CryptoMarketChangeSummary] = {}

    @staticmethod
    def _save[T: _Canonical](values: dict[str, T], key: str, value: T, canonical_hash: str) -> T:
        prior = values.get(key)
        if prior is not None and prior.canonical_hash != canonical_hash:
            raise CryptoUniverseConflict("CRYPTO_PULSE_IDENTITY_CONFLICT")
        values[key] = prior or value
        return prior or value

    def refresh_plan(self, plan_id: str) -> CryptoRefreshPlan | None:
        return self._pulse_plans.get(plan_id)

    def save_refresh_plan(self, plan: CryptoRefreshPlan) -> CryptoRefreshPlan:
        return self._save(self._pulse_plans, plan.plan_id, plan, plan.canonical_hash)

    def refresh_plans(self) -> tuple[CryptoRefreshPlan, ...]:
        return tuple(sorted(self._pulse_plans.values(), key=lambda item: item.plan_id))

    def refresh_run(self, refresh_id: str) -> CryptoRefreshRun | None:
        return self._pulse_runs.get(refresh_id)

    def refresh_for_plan_as_of(self, plan_id: str, as_of: datetime) -> CryptoRefreshRun | None:
        target = completed_utc_day(as_of)
        return next(
            (
                item
                for item in self._pulse_runs.values()
                if item.plan_id == plan_id and item.as_of == target
            ),
            None,
        )

    def save_refresh_run(self, refresh: CryptoRefreshRun) -> CryptoRefreshRun:
        prior_slot = self.refresh_for_plan_as_of(refresh.plan_id, refresh.as_of)
        if prior_slot is not None and prior_slot.refresh_id != refresh.refresh_id:
            raise CryptoUniverseConflict("CRYPTO_PULSE_PLAN_AS_OF_CONFLICT")
        return self._save(self._pulse_runs, refresh.refresh_id, refresh, refresh.canonical_hash)

    def refresh_runs(self) -> tuple[CryptoRefreshRun, ...]:
        return tuple(
            sorted(self._pulse_runs.values(), key=lambda item: (item.as_of, item.refresh_id))
        )

    def refresh_events(self, refresh_id: str) -> tuple[CryptoRefreshEvent, ...]:
        return tuple(
            sorted(
                (item for item in self._pulse_events.values() if item.refresh_id == refresh_id),
                key=lambda item: item.ordinal,
            )
        )

    def save_refresh_event(self, event: CryptoRefreshEvent) -> CryptoRefreshEvent:
        return self._save(self._pulse_events, event.event_id, event, event.canonical_hash)

    def pulse_checkpoint(self, checkpoint_id: str) -> CryptoPulseCheckpoint | None:
        return self._pulse_checkpoints.get(checkpoint_id)

    def save_pulse_checkpoint(self, checkpoint: CryptoPulseCheckpoint) -> CryptoPulseCheckpoint:
        return self._save(
            self._pulse_checkpoints,
            checkpoint.checkpoint_id,
            checkpoint,
            checkpoint.canonical_hash,
        )

    def pulse_checkpoints(self, refresh_id: str) -> tuple[CryptoPulseCheckpoint, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._pulse_checkpoints.values()
                    if item.refresh_id == refresh_id
                ),
                key=lambda item: item.ordinal,
            )
        )

    def pulse_snapshot(self, pulse_id: str) -> CryptoPulseSnapshot | None:
        return self._pulse_snapshots.get(pulse_id)

    def save_pulse_snapshot(self, pulse: CryptoPulseSnapshot) -> CryptoPulseSnapshot:
        return self._save(self._pulse_snapshots, pulse.pulse_id, pulse, pulse.canonical_hash)

    def pulse_snapshots(self) -> tuple[CryptoPulseSnapshot, ...]:
        return tuple(
            sorted(self._pulse_snapshots.values(), key=lambda item: (item.as_of, item.pulse_id))
        )

    def opportunity_lineage(self, lineage_id: str) -> CryptoOpportunityLineage | None:
        return self._pulse_lineages.get(lineage_id)

    def save_opportunity_lineages(
        self, values: tuple[CryptoOpportunityLineage, ...]
    ) -> tuple[CryptoOpportunityLineage, ...]:
        return tuple(
            self._save(self._pulse_lineages, item.lineage_id, item, item.canonical_hash)
            for item in values
        )

    def opportunity_lineages(self) -> tuple[CryptoOpportunityLineage, ...]:
        return tuple(
            sorted(
                self._pulse_lineages.values(),
                key=lambda item: (item.as_of, item.canonical_asset_id),
            )
        )

    def market_change_summary(self, summary_id: str) -> CryptoMarketChangeSummary | None:
        return self._pulse_summaries.get(summary_id)

    def save_market_change_summary(
        self, summary: CryptoMarketChangeSummary
    ) -> CryptoMarketChangeSummary:
        return self._save(
            self._pulse_summaries, summary.summary_id, summary, summary.canonical_hash
        )

    def market_change_summaries(self) -> tuple[CryptoMarketChangeSummary, ...]:
        return tuple(sorted(self._pulse_summaries.values(), key=lambda item: item.summary_id))


@dataclass(frozen=True, slots=True)
class CryptoRefreshResult:
    refresh: CryptoRefreshRun
    state: RefreshState
    checkpoints: tuple[CryptoPulseCheckpoint, ...]
    pulse: CryptoPulseSnapshot | None
    opportunities: tuple[CryptoResearchOpportunity, ...]
    lineages: tuple[CryptoOpportunityLineage, ...]
    summary: CryptoMarketChangeSummary | None
    network_requests_performed: int
    raw_bytes: int
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CryptoRelativeStrengthResult:
    run: CryptoDiscoveryRun
    opportunities: tuple[CryptoResearchOpportunity, ...]
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class _CurrentCryptoMethodInputs:
    snapshot: CryptoUniverseSnapshot
    observations: tuple[CryptoProductObservation, ...]
    market: tuple[CryptoCurrentMarketEvidence, ...]
    calibration_observations: tuple[CryptoProductObservation, ...]


def _current_crypto_method_inputs(
    repository: CryptoMarketPulseRepository,
    *,
    pulse: CryptoPulseSnapshot,
    generated_at: datetime,
) -> _CurrentCryptoMethodInputs:
    snapshot = repository.universe_snapshot(pulse.universe_snapshot_id)
    if snapshot is None:
        raise CryptoUniverseConflict("CRYPTO_CURRENT_METHOD_UNIVERSE_NOT_FOUND")
    products = {item.product_id: item for item in repository.products(snapshot.source_capture_id)}
    observations: list[CryptoProductObservation] = []
    market: list[CryptoCurrentMarketEvidence] = []
    for checkpoint in repository.pulse_checkpoints(pulse.refresh_id):
        product = products.get(checkpoint.product_id)
        if product is None or checkpoint.observation is None:
            raise CryptoUniverseConflict("CRYPTO_CURRENT_METHOD_LINEAGE_INCOMPLETE")
        observations.append(checkpoint.observation)
        complete = (
            all(
                value is not None
                for value in (
                    checkpoint.best_bid,
                    checkpoint.best_ask,
                    checkpoint.midpoint,
                    checkpoint.spread_bps,
                    checkpoint.visible_bid_depth,
                    checkpoint.visible_ask_depth,
                    checkpoint.quote_captured_at,
                    checkpoint.quote_raw_evidence_hash,
                )
            )
            and checkpoint.quality is PulseQuality.PASSED
        )
        required_capital: Decimal | None = None
        maximum_loss: Decimal | None = None
        maximum_allocation: Decimal | None = None
        if complete:
            assert checkpoint.best_ask is not None
            minimum_quantity = max(
                product.quantity_increment,
                product.minimum_quantity or Decimal(0),
            )
            minimum_notional = max(
                product.minimum_notional or Decimal(0),
                minimum_quantity * checkpoint.best_ask,
            )
            minimum_notional = (minimum_notional / product.price_increment).to_integral_value(
                rounding=ROUND_CEILING
            ) * product.price_increment
            entry_cost = (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / Decimal(10000)
            required_capital = minimum_notional * (Decimal(1) + entry_cost)
            maximum_loss = required_capital
            maximum_allocation = checkpoint.visible_ask_depth
        observed_at = checkpoint.quote_captured_at or checkpoint.status_observed_at
        evidence_payload = (
            checkpoint.checkpoint_id,
            checkpoint.product_id,
            observed_at,
            checkpoint.product_status,
            checkpoint.spread_bps,
            checkpoint.visible_ask_depth,
            required_capital,
            maximum_loss,
            maximum_allocation,
            complete,
        )
        market.append(
            CryptoCurrentMarketEvidence(
                product_id=checkpoint.product_id,
                checkpoint_id=checkpoint.checkpoint_id,
                observed_at=observed_at,
                product_status=checkpoint.product_status,
                spread_bps=checkpoint.spread_bps,
                visible_ask_depth=checkpoint.visible_ask_depth,
                required_capital=required_capital,
                maximum_loss=maximum_loss,
                maximum_sensible_allocation=maximum_allocation,
                evidence_complete=complete,
                canonical_hash=content_digest(evidence_payload),
            )
        )
    calibration_observations: list[CryptoProductObservation] = []
    retained_observations = repository.observations()
    for product_id in snapshot.eligible_product_ids:
        compatible = tuple(
            item
            for item in retained_observations
            if item.product_id == product_id
            and item.source_capture_id == snapshot.source_capture_id
            and item.evidence_class is snapshot.evidence_class
            and item.interval_end <= generated_at
            and len(item.candles) >= 121
        )
        if not compatible:
            raise CryptoUniverseConflict("CRYPTO_CURRENT_METHOD_CALIBRATION_HISTORY_UNAVAILABLE")
        calibration_observations.append(
            max(
                compatible,
                key=lambda item: (
                    len(item.candles),
                    (item.interval_end - item.interval_start).days,
                    item.interval_end,
                    item.observation_id,
                ),
            )
        )
    return _CurrentCryptoMethodInputs(
        snapshot,
        tuple(observations),
        tuple(market),
        tuple(calibration_observations),
    )


def current_crypto_method_inputs(
    repository: CryptoMarketPulseRepository,
    *,
    pulse: CryptoPulseSnapshot,
    generated_at: datetime,
) -> _CurrentCryptoMethodInputs:
    """Expose the exact retained current-method input set for reusable methods."""

    return _current_crypto_method_inputs(repository, pulse=pulse, generated_at=generated_at)


class RunCurrentCryptoRelativeStrength:
    """Project one retained v2 pulse through the fixed current strategy."""

    def __init__(self, repository: CryptoMarketPulseRepository) -> None:
        self._repository = repository

    def execute(
        self, *, run_id: str, pulse_id: str, generated_at: datetime
    ) -> CryptoRelativeStrengthResult:
        pulse = self._repository.pulse_snapshot(pulse_id)
        if pulse is None:
            raise CryptoUniverseConflict("CRYPTO_RELATIVE_STRENGTH_PULSE_NOT_FOUND")
        prior = self._repository.discovery_run(run_id)
        if prior is not None:
            if prior.source_pulse_id != pulse_id:
                raise CryptoUniverseConflict("CRYPTO_RELATIVE_STRENGTH_RUN_IDENTITY_CONFLICT")
            return CryptoRelativeStrengthResult(
                prior,
                tuple(
                    sorted(
                        (
                            item
                            for item in self._repository.opportunities()
                            if item.discovery_run_id == run_id
                        ),
                        key=lambda item: (item.rank, item.product_id),
                    )
                ),
                True,
            )
        inputs = _current_crypto_method_inputs(
            self._repository, pulse=pulse, generated_at=generated_at
        )
        run = self._repository.save_discovery_run(
            run_relative_strength(
                run_id=run_id,
                source_pulse_id=pulse_id,
                snapshot=inputs.snapshot,
                observations=inputs.observations,
                current_market=inputs.market,
                generated_at=generated_at,
                calibration_observations=inputs.calibration_observations,
            )
        )
        opportunities = self._repository.save_opportunities(
            build_relative_strength_opportunities(run)
        )
        return CryptoRelativeStrengthResult(run, opportunities, False)


class RunCurrentCryptoReversalRegime:
    """Project one retained pulse through the fixed reversal-regime method."""

    def __init__(self, repository: CryptoMarketPulseRepository) -> None:
        self._repository = repository

    def execute(
        self, *, run_id: str, pulse_id: str, generated_at: datetime
    ) -> CryptoRelativeStrengthResult:
        pulse = self._repository.pulse_snapshot(pulse_id)
        if pulse is None:
            raise CryptoUniverseConflict("CRYPTO_REVERSAL_REGIME_PULSE_NOT_FOUND")
        prior = self._repository.discovery_run(run_id)
        if prior is not None:
            if prior.source_pulse_id != pulse_id:
                raise CryptoUniverseConflict("CRYPTO_REVERSAL_REGIME_RUN_IDENTITY_CONFLICT")
            return CryptoRelativeStrengthResult(
                prior,
                tuple(
                    sorted(
                        (
                            item
                            for item in self._repository.opportunities()
                            if item.discovery_run_id == run_id
                        ),
                        key=lambda item: (item.rank, item.product_id),
                    )
                ),
                True,
            )
        inputs = _current_crypto_method_inputs(
            self._repository, pulse=pulse, generated_at=generated_at
        )
        run = self._repository.save_discovery_run(
            run_reversal_regime(
                run_id=run_id,
                source_pulse_id=pulse_id,
                snapshot=inputs.snapshot,
                observations=inputs.observations,
                current_market=inputs.market,
                generated_at=generated_at,
                calibration_observations=inputs.calibration_observations,
            )
        )
        opportunities = self._repository.save_opportunities(
            build_reversal_regime_opportunities(run)
        )
        return CryptoRelativeStrengthResult(run, opportunities, False)


class RegisterCryptoRefreshPlan:
    def __init__(self, repository: CryptoMarketPulseRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        plan_id: str,
        universe_snapshot_id: str,
        actor: str,
        created_at: datetime,
        current_market_evidence: bool = False,
    ) -> CryptoRefreshPlan:
        universe = self._repository.universe_snapshot(universe_snapshot_id)
        if universe is None:
            raise CryptoUniverseConflict("CRYPTO_UNIVERSE_SNAPSHOT_NOT_FOUND")
        factory = (
            CryptoRefreshPlan.create_with_current_market_evidence
            if current_market_evidence
            else CryptoRefreshPlan.create
        )
        plan = factory(
            plan_id=plan_id,
            universe_snapshot_id=universe_snapshot_id,
            actor=actor,
            created_at=created_at,
        )
        return self._repository.save_refresh_plan(plan)


class ExecuteCryptoMarketPulseRefresh:
    def __init__(
        self,
        repository: CryptoMarketPulseRepository,
        source: PublicCryptoUniverseSource,
    ) -> None:
        self._repository = repository
        self._source = source

    def _retained_result(self, refresh: CryptoRefreshRun) -> CryptoRefreshResult:
        events = self._repository.refresh_events(refresh.refresh_id)
        state = events[-1].state if events else RefreshState.PLANNED
        checkpoints = self._repository.pulse_checkpoints(refresh.refresh_id)
        pulse = self._repository.pulse_snapshot(f"{refresh.refresh_id}-pulse")
        lineages = tuple(
            item
            for item in self._repository.opportunity_lineages()
            if item.refresh_id == refresh.refresh_id
        )
        opportunities = tuple(
            item
            for item in self._repository.opportunities()
            if item.discovery_run_id == f"{refresh.refresh_id}-discovery"
        )
        summary = self._repository.market_change_summary(f"{refresh.refresh_id}-change-summary")
        return CryptoRefreshResult(
            refresh,
            state,
            checkpoints,
            pulse,
            opportunities,
            lineages,
            summary,
            0,
            sum(item.raw_bytes for item in checkpoints),
            tuple(
                item.canonical_asset_id
                for item in checkpoints
                if item.quality is PulseQuality.FAILED
            ),
        )

    def execute(
        self,
        *,
        refresh_id: str,
        plan_id: str,
        as_of: datetime,
        requested_at: datetime,
    ) -> CryptoRefreshResult:
        plan = self._repository.refresh_plan(plan_id)
        if plan is None:
            raise CryptoUniverseConflict("CRYPTO_PULSE_PLAN_NOT_FOUND")
        target = completed_utc_day(as_of)
        occupied = self._repository.refresh_for_plan_as_of(plan_id, target)
        if occupied is not None:
            if occupied.refresh_id != refresh_id:
                raise CryptoUniverseConflict("CRYPTO_PULSE_PLAN_AS_OF_CONFLICT")
            events = self._repository.refresh_events(refresh_id)
            if events and events[-1].state in {
                RefreshState.COMPLETED,
                RefreshState.PARTIAL,
                RefreshState.FAILED,
                RefreshState.SKIPPED_NOT_DUE,
            }:
                return self._retained_result(occupied)
        universe = self._repository.universe_snapshot(plan.universe_snapshot_id)
        if universe is None:
            raise CryptoUniverseConflict("CRYPTO_UNIVERSE_SNAPSHOT_NOT_FOUND")
        if universe.evidence_class is not self._source.evidence_class:
            raise CryptoUniverseConflict("CRYPTO_PULSE_EVIDENCE_CLASS_CONFLICT")
        if occupied is not None:
            refresh = occupied
        else:
            refresh = CryptoRefreshRun.create(
                refresh_id=refresh_id,
                plan=plan,
                universe=universe,
                as_of=target,
                created_at=requested_at,
            )
            prior_exact = self._repository.refresh_run(refresh_id)
            if prior_exact is not None and prior_exact.canonical_hash != refresh.canonical_hash:
                raise CryptoUniverseConflict("CRYPTO_PULSE_REFRESH_IDENTITY_CONFLICT")
            refresh = self._repository.save_refresh_run(refresh)
        events = self._repository.refresh_events(refresh_id)
        if not events:
            self._repository.save_refresh_event(
                CryptoRefreshEvent.create(
                    refresh_id=refresh_id,
                    ordinal=1,
                    state=RefreshState.PLANNED,
                    occurred_at=requested_at,
                )
            )
            events = self._repository.refresh_events(refresh_id)
        completed_asofs = tuple(
            item.as_of
            for item in self._repository.refresh_runs()
            if item.refresh_id != refresh_id
            and self._repository.refresh_events(item.refresh_id)
            and self._repository.refresh_events(item.refresh_id)[-1].state is RefreshState.COMPLETED
        )
        if not plan.enabled or not daily_refresh_due(
            as_of=target, completed_as_of_times=completed_asofs
        ):
            self._repository.save_refresh_event(
                CryptoRefreshEvent.create(
                    refresh_id=refresh_id,
                    ordinal=2,
                    state=RefreshState.SKIPPED_NOT_DUE,
                    occurred_at=requested_at,
                    warnings=("DAILY_REFRESH_NOT_DUE",),
                )
            )
            return self._retained_result(refresh)
        if events[-1].state is RefreshState.PLANNED:
            self._repository.save_refresh_event(
                CryptoRefreshEvent.create(
                    refresh_id=refresh_id,
                    ordinal=2,
                    state=RefreshState.RUNNING,
                    occurred_at=requested_at,
                    warnings=(
                        ("UNIVERSE_REFRESH_DUE",)
                        if refresh.universe_refresh_due
                        else ("UNIVERSE_REFRESH_NOT_DUE_REUSED_EXACT_SNAPSHOT",)
                    ),
                )
            )
        products = {
            item.product_id: item for item in self._repository.products(universe.source_capture_id)
        }
        asset_by_route = {
            route: asset
            for asset, route in zip(
                universe.eligible_asset_ids,
                universe.eligible_product_ids,
                strict=True,
            )
        }
        requests = 0
        for ordinal, product_id in enumerate(refresh.request_product_ids, 1):
            asset_id = asset_by_route[product_id]
            checkpoint_id = f"{refresh_id}-{asset_id.lower()}-checkpoint"
            if self._repository.pulse_checkpoint(checkpoint_id) is not None:
                continue
            product = products.get(product_id)
            if product is None:
                checkpoint = CryptoPulseCheckpoint.create(
                    refresh_id=refresh_id,
                    ordinal=ordinal,
                    canonical_asset_id=asset_id,
                    product_id=product_id,
                    product_status="UNKNOWN",
                    status_observed_at=requested_at,
                    observation=None,
                    as_of=target,
                    raw_bytes=0,
                    runtime_milliseconds=0,
                    source_locator=None,
                    failure_reason="PRODUCT_REFERENCE_UNAVAILABLE",
                )
            else:
                try:
                    page = self._source.capture_daily_observations(
                        product_id=product_id,
                        interval_start=refresh.interval_start,
                        interval_end=refresh.interval_end,
                        requested_at=requested_at,
                    )
                    requests += 1
                    raw_hash = hashlib.sha256(page.raw_payload).hexdigest()
                    observation = CryptoProductObservation.create(
                        observation_id=f"{refresh_id}-{product_id.lower()}-observation",
                        source_capture_id=universe.source_capture_id,
                        product_id=product_id,
                        interval_start=refresh.interval_start,
                        interval_end=refresh.interval_end,
                        evidence_class=self._source.evidence_class,
                        candles=page.candles,
                        missing_buckets=page.missing_buckets,
                        duplicate_records=page.duplicate_records,
                        rejected_records=page.rejected_records,
                        request_count=1,
                        raw_evidence_hashes=(raw_hash,),
                        source_contract_version="COINBASE_ADVANCED_TRADE_PUBLIC_CANDLES_V1",
                        parser_version="COINBASE_PUBLIC_DAILY_CANDLE_PARSER_V1",
                        checkpoint_ordinal=ordinal,
                        completed_at=page.captured_at,
                        canonical_asset_id=asset_id,
                    )
                    if plan.version < 2:
                        checkpoint = CryptoPulseCheckpoint.create(
                            refresh_id=refresh_id,
                            ordinal=ordinal,
                            canonical_asset_id=asset_id,
                            product_id=product_id,
                            product_status=f"{product.trading_status}/{product.product_status}",
                            status_observed_at=product.captured_at,
                            observation=observation,
                            as_of=target,
                            raw_bytes=len(page.raw_payload),
                            runtime_milliseconds=page.runtime_milliseconds,
                            source_locator=page.source_locator,
                        )
                    else:
                        try:
                            book = self._source.capture_product_book(
                                product_id=product_id,
                                maximum_depth=PULSE_MAX_BOOK_DEPTH,
                                requested_at=requested_at,
                            )
                            requests += 1
                            checkpoint = CryptoPulseCheckpoint.create(
                                refresh_id=refresh_id,
                                ordinal=ordinal,
                                canonical_asset_id=asset_id,
                                product_id=product_id,
                                product_status=(
                                    f"{product.trading_status}/{product.product_status}"
                                ),
                                status_observed_at=book.captured_at,
                                observation=observation,
                                as_of=target,
                                raw_bytes=len(page.raw_payload) + len(book.raw_payload),
                                runtime_milliseconds=(
                                    page.runtime_milliseconds + book.runtime_milliseconds
                                ),
                                source_locator=(f"{page.source_locator}|{book.source_locator}"),
                                best_bid=book.best_bid,
                                best_ask=book.best_ask,
                                midpoint=book.midpoint,
                                spread_bps=book.spread_bps,
                                visible_bid_depth=book.visible_bid_depth,
                                visible_ask_depth=book.visible_ask_depth,
                                quote_captured_at=book.captured_at,
                                quote_raw_evidence_hash=hashlib.sha256(
                                    book.raw_payload
                                ).hexdigest(),
                            )
                        except (OSError, RuntimeError, ValueError) as error:
                            requests += 1
                            checkpoint = CryptoPulseCheckpoint.create(
                                refresh_id=refresh_id,
                                ordinal=ordinal,
                                canonical_asset_id=asset_id,
                                product_id=product_id,
                                product_status=(
                                    f"{product.trading_status}/{product.product_status}"
                                ),
                                status_observed_at=product.captured_at,
                                observation=observation,
                                as_of=target,
                                raw_bytes=len(page.raw_payload),
                                runtime_milliseconds=page.runtime_milliseconds,
                                source_locator=page.source_locator,
                                quote_failure_reason=f"PUBLIC_ORDER_BOOK_{type(error).__name__}",
                            )
                except (OSError, RuntimeError, ValueError) as error:
                    requests += 1
                    checkpoint = CryptoPulseCheckpoint.create(
                        refresh_id=refresh_id,
                        ordinal=ordinal,
                        canonical_asset_id=asset_id,
                        product_id=product_id,
                        product_status=f"{product.trading_status}/{product.product_status}",
                        status_observed_at=product.captured_at,
                        observation=None,
                        as_of=target,
                        raw_bytes=0,
                        runtime_milliseconds=0,
                        source_locator=None,
                        failure_reason=type(error).__name__,
                    )
            self._repository.save_pulse_checkpoint(checkpoint)
        checkpoints = self._repository.pulse_checkpoints(refresh_id)
        total_bytes = sum(item.raw_bytes for item in checkpoints)
        total_runtime = sum(item.runtime_milliseconds for item in checkpoints)
        failures = tuple(
            item.canonical_asset_id for item in checkpoints if item.quality is PulseQuality.FAILED
        )
        if (
            len(checkpoints) != len(refresh.request_product_ids)
            or total_bytes > refresh.maximum_bytes
            or total_runtime > refresh.maximum_runtime_milliseconds
            or requests > refresh.maximum_requests
        ):
            failures = tuple(sorted(set((*failures, "REFRESH_BOUND_EXCEEDED"))))
        if failures:
            final_state = (
                RefreshState.PARTIAL if len(failures) < len(checkpoints) else RefreshState.FAILED
            )
            self._repository.save_refresh_event(
                CryptoRefreshEvent.create(
                    refresh_id=refresh_id,
                    ordinal=3,
                    state=final_state,
                    occurred_at=requested_at,
                    warnings=failures,
                )
            )
            return self._retained_result(refresh)
        pulse = self._repository.save_pulse_snapshot(
            build_pulse_snapshot(
                pulse_id=f"{refresh_id}-pulse",
                refresh=refresh,
                checkpoints=checkpoints,
                ingested_at=max(
                    (
                        item.observation.completed_at
                        for item in checkpoints
                        if item.observation is not None
                    ),
                    default=requested_at,
                ),
            )
        )
        observations = tuple(
            item.observation for item in checkpoints if item.observation is not None
        )
        discovery = self._repository.save_discovery_run(
            run_discovery(
                run_id=f"{refresh_id}-discovery",
                snapshot=universe,
                observations=observations,
                generated_at=target,
            )
        )
        opportunities = self._repository.save_opportunities(build_opportunities(discovery))
        prior_pulses = tuple(
            item for item in self._repository.pulse_snapshots() if item.as_of < target
        )
        prior_pulse = prior_pulses[-1] if prior_pulses else None
        prior_lineages = self._repository.opportunity_lineages()
        all_opportunities = self._repository.opportunities()
        lineages: list[CryptoOpportunityLineage] = []
        current_by_asset = {item.canonical_asset_id: item for item in opportunities}
        for asset_id, product_id in pulse.selected_routes:
            current = current_by_asset.get(asset_id)
            prior_lineage = next(
                (
                    item
                    for item in reversed(prior_lineages)
                    if item.canonical_asset_id == asset_id and item.as_of < target
                ),
                None,
            )
            prior = next(
                (
                    item
                    for item in sorted(
                        all_opportunities,
                        key=lambda value: (value.generated_at, value.opportunity_id),
                        reverse=True,
                    )
                    if item.canonical_asset_id == asset_id
                    and current is not None
                    and item.opportunity_id != current.opportunity_id
                    and item.generated_at < target
                ),
                None,
            )
            lineages.append(
                build_opportunity_lineage(
                    refresh_id=refresh_id,
                    pulse_id=pulse.pulse_id,
                    as_of=target,
                    asset_id=asset_id,
                    product_id=product_id,
                    current=current,
                    prior=prior,
                    prior_lineage=prior_lineage,
                    quality_blocked=False,
                )
            )
        retained_lineages = self._repository.save_opportunity_lineages(tuple(lineages))
        summary = self._repository.save_market_change_summary(
            build_change_summary(
                refresh_id=refresh_id,
                pulse=pulse,
                prior_pulse=prior_pulse,
                lineages=retained_lineages,
            )
        )
        self._repository.save_refresh_event(
            CryptoRefreshEvent.create(
                refresh_id=refresh_id,
                ordinal=3,
                state=RefreshState.COMPLETED,
                occurred_at=requested_at,
                warnings=(CRYPTO_MARKET_PULSE_WARNING,),
            )
        )
        return CryptoRefreshResult(
            refresh,
            RefreshState.COMPLETED,
            checkpoints,
            pulse,
            opportunities,
            retained_lineages,
            summary,
            requests,
            total_bytes,
            (),
        )


def crypto_market_pulse_workspace(
    repository: CryptoMarketPulseRepository,
) -> dict[str, object]:
    plans = repository.refresh_plans()
    runs = repository.refresh_runs()
    current = runs[-1] if runs else None
    events = repository.refresh_events(current.refresh_id) if current else ()
    pulse = repository.pulse_snapshot(f"{current.refresh_id}-pulse") if current else None
    discovery = repository.discovery_run(f"{current.refresh_id}-discovery") if current else None
    lineages = repository.opportunity_lineages()
    summary = (
        repository.market_change_summary(f"{current.refresh_id}-change-summary")
        if current
        else None
    )
    return {
        "refresh_plan": plans[-1] if plans else None,
        "refresh_run": current,
        "refresh_state": events[-1].state if events else None,
        "checkpoints": repository.pulse_checkpoints(current.refresh_id) if current else (),
        "pulse_snapshot": pulse,
        "current_rankings": discovery.results if discovery else (),
        "opportunity_lineages": lineages,
        "persistence_classifications": tuple(
            (item.canonical_asset_id, item.persistence) for item in lineages
        ),
        "expired_opportunities": tuple(
            item.prior_opportunity_id
            for item in lineages
            if "PRIOR_OPPORTUNITY_EXPIRED" in item.transition_reasons
        ),
        "reversed_opportunities": tuple(
            item.current_opportunity_id for item in lineages if item.persistence.value == "REVERSED"
        ),
        "market_change_summary": summary,
        "empty_state": None if plans else "NO CRYPTO MARKET PULSE PLAN RETAINED",
        "quote_evidence_limitation": (
            "BOUNDED_PUBLIC_ORDER_BOOK_IS_VISIBLE_EVIDENCE_NOT_FILLABILITY"
            if current
            and all(
                item.best_bid is not None
                for item in repository.pulse_checkpoints(current.refresh_id)
            )
            else "PUBLIC_TOP_OF_BOOK_NOT_CAPTURED_BY_SUPPORTED_BOUNDARY"
        ),
        "authority": "NONE",
        "paper_eligible": False,
        "live_eligible": False,
        "capital_authority": "NONE",
        "broker_writes": 0,
        "orders": 0,
        "positions": 0,
        "reservations": 0,
        "fills": 0,
        "journals": 0,
    }
