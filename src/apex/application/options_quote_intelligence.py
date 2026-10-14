"""Idempotent orchestration for genuine options quote-chain and volatility
intelligence.

Every service here is additive research-evidence orchestration only: no
Order, Strategy, Shared Opportunity, PAPER order, or LIVE authority is ever
created, and every persisted fact keeps ``authority = "NONE"``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from apex.application.historical_market_data import (
    HistoricalMarketDataApplicationError,
    HistoricalMarketDataRepository,
    HistoricalPointInTimeReplay,
)
from apex.application.real_securities_data import SecuritiesEvidencePort
from apex.domain.historical_market_data import (
    HistoricalClock,
    HistoricalDatasetSnapshot,
    rolling_volatility,
)
from apex.domain.options_quote_intelligence import (
    REALIZED_VOLATILITY_WINDOW_SESSIONS,
    OptionChainCapturePlan,
    OptionChainCaptureRun,
    OptionQuoteObservation,
    OptionsChainSnapshot,
    OptionsQuoteProviderCapabilitySnapshot,
    OptionsVolatilityResearchFeatures,
    annualize_realized_volatility,
    create_options_chain_snapshot,
)
from apex.domain.research_cohort import ResearchCohortPolicy, ResolvedCohortMember

REALIZED_VOLATILITY_SECONDARY_WINDOW_SESSIONS = 60


class OptionsQuoteIntelligenceApplicationError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Realized-volatility integration: wires the previously-disconnected
# ``create_options_chain_snapshot(realized_volatility_annualized=...)``
# input to the genuine multi-asset historical dataset already retained by
# ``application.historical_market_data`` -- never a new network capture.
#
# Declared matching policy (fixed here, before any underlying's IV-versus-RV
# result is reviewed, exactly so the window cannot be picked *because* it
# makes a given result look interesting):
#   * primary horizon = 20 trading sessions, matching the pre-existing
#     ``options_quote_intelligence.REALIZED_VOLATILITY_WINDOW_SESSIONS``
#     default that already flows into ``IvVsRealizedVolatility`` -- this is
#     the one horizon fed into each snapshot's single IV-versus-RV
#     comparison and into the cross-market research-feature projection;
#   * secondary horizon = 60 trading sessions, computed and reported
#     alongside as context (cross-sectional/validation reporting) but never
#     substituted for the primary comparison;
#   * a horizon nearest an individual contract's own DTE is *not*
#     implemented this milestone: ``IvVsRealizedVolatility`` is one scalar
#     per snapshot, not one per expiration, so a genuinely per-expiration
#     DTE-matched RV would need a broader redesign than this integration
#     milestone scopes -- retained explicitly as a limitation, not silently
#     skipped.
# The dataset snapshot used is resolved dynamically from the cohort's own
# genuine-multi-asset idempotency key (identical lookup
# ``application.genuine_multi_asset_history.resolve_genuine_multi_asset_
# registry`` already performs for its own, unrelated quality-gate purpose)
# -- never a hardcoded snapshot id -- so a later, fresher retained capture
# is picked up automatically without a code change.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RealizedVolatilityResolution:
    status: str
    window_sessions_primary: int
    realized_volatility_primary: Decimal | None
    secondary_window_sessions: int
    realized_volatility_secondary: Decimal | None
    dataset_snapshot_id: str | None
    point_in_time_bar_count: int
    latest_bar_date: str | None
    limitations: tuple[str, ...]


class ResolveRealizedVolatility:
    """Point-in-time realized volatility for one underlying, computed only
    from bars already retained in the genuine multi-asset historical
    dataset -- reusing ``HistoricalPointInTimeReplay``/``rolling_volatility``
    (the existing, reviewed, ``as_of``-gated primitives) exactly as-is, not
    a bespoke reimplementation. Every failure mode (no retained dataset,
    underlying not in its universe, insufficient trailing history) is
    reported explicitly; never silently defaulted to zero or dropped."""

    def __init__(
        self,
        *,
        historical_repository: HistoricalMarketDataRepository,
        evidence: SecuritiesEvidencePort,
        cohort_policy: ResearchCohortPolicy,
    ) -> None:
        self._historical_repository = historical_repository
        self._evidence = evidence
        self._cohort_policy = cohort_policy

    def _latest_genuine_dataset_snapshot(self) -> HistoricalDatasetSnapshot | None:
        # Mirrors ``application.genuine_multi_asset_history.
        # resolve_genuine_multi_asset_registry``'s own snapshot-resolution
        # lookup (same idempotency key, same "most recently retained wins"
        # rule) without importing that module's unrelated quality-gate/
        # cross-market-dataset dependencies just to reach one field.
        idempotency_key = f"genuine-multi-asset-cohort-v1:{self._cohort_policy.policy_id}"
        plan = self._historical_repository.plan_by_idempotency(idempotency_key)
        if plan is None:
            return None
        matches = tuple(
            item
            for item in self._historical_repository.list_snapshots()
            if item.plan_id == plan.plan_id
        )
        return matches[-1] if matches else None

    def resolve(self, *, stable_security_id: str, as_of: datetime) -> RealizedVolatilityResolution:
        snapshot = self._latest_genuine_dataset_snapshot()
        if snapshot is None:
            return RealizedVolatilityResolution(
                "HISTORICAL_DATASET_UNAVAILABLE",
                REALIZED_VOLATILITY_WINDOW_SESSIONS,
                None,
                REALIZED_VOLATILITY_SECONDARY_WINDOW_SESSIONS,
                None,
                None,
                0,
                None,
                ("NO_RETAINED_GENUINE_MULTI_ASSET_DATASET_SNAPSHOT",),
            )
        replay = HistoricalPointInTimeReplay(
            repository=self._historical_repository, evidence=self._evidence
        )
        clock = HistoricalClock(as_of=as_of)
        try:
            bars = replay.evidence_as_of(
                snapshot_id=snapshot.snapshot_id,
                stable_security_id=stable_security_id,
                clock=clock,
            )
        except HistoricalMarketDataApplicationError:
            return RealizedVolatilityResolution(
                "SECURITY_NOT_IN_RETAINED_UNIVERSE",
                REALIZED_VOLATILITY_WINDOW_SESSIONS,
                None,
                REALIZED_VOLATILITY_SECONDARY_WINDOW_SESSIONS,
                None,
                snapshot.snapshot_id,
                0,
                None,
                ("STABLE_SECURITY_ID_NOT_IN_GENUINE_DATASET_UNIVERSE",),
            )
        primary = rolling_volatility(bars, as_of=as_of, window=REALIZED_VOLATILITY_WINDOW_SESSIONS)
        secondary = rolling_volatility(
            bars, as_of=as_of, window=REALIZED_VOLATILITY_SECONDARY_WINDOW_SESSIONS
        )
        annualized_primary = annualize_realized_volatility(primary) if primary is not None else None
        annualized_secondary = (
            annualize_realized_volatility(secondary) if secondary is not None else None
        )
        latest_bar_date = max((bar.period_end.date() for bar in bars), default=None)
        limitations: list[str] = []
        if annualized_primary is None:
            limitations.append(
                "FEWER_THAN_"
                f"{REALIZED_VOLATILITY_WINDOW_SESSIONS + 1}"
                "_POINT_IN_TIME_SAFE_SESSIONS_RETAINED"
            )
        if annualized_secondary is None:
            limitations.append(
                "FEWER_THAN_"
                f"{REALIZED_VOLATILITY_SECONDARY_WINDOW_SESSIONS + 1}"
                "_POINT_IN_TIME_SAFE_SESSIONS_RETAINED_FOR_SECONDARY_WINDOW"
            )
        return RealizedVolatilityResolution(
            "COMPUTED" if annualized_primary is not None else "INSUFFICIENT_HISTORY",
            REALIZED_VOLATILITY_WINDOW_SESSIONS,
            annualized_primary,
            REALIZED_VOLATILITY_SECONDARY_WINDOW_SESSIONS,
            annualized_secondary,
            snapshot.snapshot_id,
            len(bars),
            latest_bar_date.isoformat() if latest_bar_date is not None else None,
            tuple(limitations),
        )


class OptionsQuoteIntelligenceRepository(Protocol):
    def save_capability(
        self, snapshot: OptionsQuoteProviderCapabilitySnapshot
    ) -> OptionsQuoteProviderCapabilitySnapshot: ...

    def latest_capability(self, provider: str) -> OptionsQuoteProviderCapabilitySnapshot | None: ...

    def capabilities(
        self, provider: str | None = None
    ) -> tuple[OptionsQuoteProviderCapabilitySnapshot, ...]: ...

    def save_plan(self, plan: OptionChainCapturePlan) -> OptionChainCapturePlan: ...

    def plan_by_id(self, plan_id: str) -> OptionChainCapturePlan | None: ...

    def latest_plan(self) -> OptionChainCapturePlan | None: ...

    def plans(self) -> tuple[OptionChainCapturePlan, ...]: ...

    def save_run(self, run: OptionChainCaptureRun) -> OptionChainCaptureRun: ...

    def run_by_id(self, run_id: str) -> OptionChainCaptureRun | None: ...

    def runs_for_plan(self, plan_id: str) -> tuple[OptionChainCaptureRun, ...]: ...

    def save_observations(
        self, observations: tuple[OptionQuoteObservation, ...]
    ) -> tuple[OptionQuoteObservation, ...]: ...

    def observation_by_id(self, observation_id: str) -> OptionQuoteObservation | None: ...

    def observations_by_ids(
        self, observation_ids: tuple[str, ...]
    ) -> tuple[OptionQuoteObservation, ...]: ...

    def save_snapshot(self, snapshot: OptionsChainSnapshot) -> OptionsChainSnapshot: ...

    def snapshot_by_id(self, snapshot_id: str) -> OptionsChainSnapshot | None: ...

    def snapshots_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionsChainSnapshot, ...]: ...

    def latest_snapshot_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> OptionsChainSnapshot | None: ...

    def save_research_features(
        self, features: OptionsVolatilityResearchFeatures
    ) -> OptionsVolatilityResearchFeatures: ...

    def research_features_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionsVolatilityResearchFeatures, ...]: ...

    def research_features_by_snapshot(
        self, snapshot_id: str
    ) -> OptionsVolatilityResearchFeatures | None: ...


class RecordOptionsQuoteProviderCapability:
    def __init__(self, repository: OptionsQuoteIntelligenceRepository) -> None:
        self._repository = repository

    def execute(
        self, *, capability: OptionsQuoteProviderCapabilitySnapshot
    ) -> OptionsQuoteProviderCapabilitySnapshot:
        return self._repository.save_capability(capability)


class CreateOptionChainCapturePlan:
    """Content-addressed: creating the identical plan twice returns the
    already-retained row rather than a duplicate."""

    def __init__(self, repository: OptionsQuoteIntelligenceRepository) -> None:
        self._repository = repository

    def execute(self, *, plan: OptionChainCapturePlan) -> OptionChainCapturePlan:
        existing = self._repository.plan_by_id(plan.plan_id)
        if existing is not None:
            return existing
        return self._repository.save_plan(plan)


class RecordOptionChainCaptureRun:
    def __init__(self, repository: OptionsQuoteIntelligenceRepository) -> None:
        self._repository = repository

    def execute(self, *, run: OptionChainCaptureRun) -> OptionChainCaptureRun:
        existing = self._repository.run_by_id(run.run_id)
        if existing is not None:
            return existing
        return self._repository.save_run(run)


@dataclass(frozen=True, slots=True)
class CreateOptionsChainSnapshotCommand:
    plan: OptionChainCapturePlan
    run_id: str
    member: ResolvedCohortMember
    underlying_reference_price: Decimal
    underlying_price_source: str
    captured_at: datetime
    observations: tuple[OptionQuoteObservation, ...]
    as_of: date
    provider: str
    source_receipt_ids: tuple[str, ...]
    realized_volatility_annualized: Decimal | None = None
    realized_volatility_status: str = "REALIZED_VOLATILITY_UNAVAILABLE"


class CreateOptionsChainSnapshot:
    """Resolves genuine forward-accumulated ATM-IV history for the IV-rank
    feature from every prior retained snapshot of the identical underlying
    -- never fabricated, and empty (``IV_HISTORY_INSUFFICIENT``) on an
    underlying's first-ever capture -- then builds and idempotently
    persists one immutable snapshot plus its cross-market research-feature
    projection."""

    def __init__(self, repository: OptionsQuoteIntelligenceRepository) -> None:
        self._repository = repository

    def execute(
        self, command: CreateOptionsChainSnapshotCommand
    ) -> tuple[OptionsChainSnapshot, OptionsVolatilityResearchFeatures]:
        prior_snapshots = self._repository.snapshots_for_underlying(
            command.member.stable_security_id
        )
        prior_atm_history = tuple(
            snap.volatility_surface.atm_points[0].atm_iv
            for snap in prior_snapshots
            if snap.volatility_surface.atm_points
            and snap.volatility_surface.atm_points[0].atm_iv is not None
        )
        snapshot = create_options_chain_snapshot(
            plan_id=command.plan.plan_id,
            run_id=command.run_id,
            underlying_canonical_security_id=command.member.stable_security_id,
            underlying_symbol=command.member.symbol,
            underlying_reference_price=command.underlying_reference_price,
            underlying_price_source=command.underlying_price_source,
            captured_at=command.captured_at,
            observations=command.observations,
            as_of=command.as_of,
            provider=command.provider,
            source_receipt_ids=command.source_receipt_ids,
            prior_atm_iv_history=prior_atm_history,
            realized_volatility_annualized=command.realized_volatility_annualized,
            realized_volatility_status=command.realized_volatility_status,
        )
        existing = self._repository.snapshot_by_id(snapshot.snapshot_id)
        if existing is not None:
            features = self._repository.research_features_by_snapshot(existing.snapshot_id)
            if features is None:
                features = OptionsVolatilityResearchFeatures.from_snapshot(existing)
                self._repository.save_research_features(features)
            return existing, features
        if command.observations:
            self._repository.save_observations(command.observations)
        self._repository.save_snapshot(snapshot)
        features = OptionsVolatilityResearchFeatures.from_snapshot(snapshot)
        self._repository.save_research_features(features)
        return snapshot, features


class InMemoryOptionsQuoteIntelligenceRepository:
    def __init__(self) -> None:
        self._capabilities: dict[str, OptionsQuoteProviderCapabilitySnapshot] = {}
        self._plans: dict[str, OptionChainCapturePlan] = {}
        self._runs: dict[str, OptionChainCaptureRun] = {}
        self._observations: dict[str, OptionQuoteObservation] = {}
        self._snapshots: dict[str, OptionsChainSnapshot] = {}
        self._features: dict[str, OptionsVolatilityResearchFeatures] = {}

    def save_capability(
        self, snapshot: OptionsQuoteProviderCapabilitySnapshot
    ) -> OptionsQuoteProviderCapabilitySnapshot:
        self._capabilities[snapshot.canonical_hash] = snapshot
        return snapshot

    def latest_capability(self, provider: str) -> OptionsQuoteProviderCapabilitySnapshot | None:
        matches = [
            item for item in self._capabilities.values() if item.capability.provider == provider
        ]
        return max(matches, key=lambda item: item.checked_at) if matches else None

    def capabilities(
        self, provider: str | None = None
    ) -> tuple[OptionsQuoteProviderCapabilitySnapshot, ...]:
        values: Iterable[OptionsQuoteProviderCapabilitySnapshot] = self._capabilities.values()
        if provider is not None:
            values = (item for item in values if item.capability.provider == provider)
        return tuple(sorted(values, key=lambda item: (item.checked_at, item.canonical_hash)))

    def save_plan(self, plan: OptionChainCapturePlan) -> OptionChainCapturePlan:
        self._plans[plan.plan_id] = plan
        return plan

    def plan_by_id(self, plan_id: str) -> OptionChainCapturePlan | None:
        return self._plans.get(plan_id)

    def latest_plan(self) -> OptionChainCapturePlan | None:
        return max(self._plans.values(), key=lambda item: item.created_at, default=None)

    def plans(self) -> tuple[OptionChainCapturePlan, ...]:
        return tuple(sorted(self._plans.values(), key=lambda item: item.created_at))

    def save_run(self, run: OptionChainCaptureRun) -> OptionChainCaptureRun:
        self._runs[run.run_id] = run
        return run

    def run_by_id(self, run_id: str) -> OptionChainCaptureRun | None:
        return self._runs.get(run_id)

    def runs_for_plan(self, plan_id: str) -> tuple[OptionChainCaptureRun, ...]:
        return tuple(
            sorted(
                (item for item in self._runs.values() if item.plan_id == plan_id),
                key=lambda item: item.requested_at,
            )
        )

    def save_observations(
        self, observations: tuple[OptionQuoteObservation, ...]
    ) -> tuple[OptionQuoteObservation, ...]:
        for observation in observations:
            self._observations[observation.observation_id] = observation
        return observations

    def observation_by_id(self, observation_id: str) -> OptionQuoteObservation | None:
        return self._observations.get(observation_id)

    def observations_by_ids(
        self, observation_ids: tuple[str, ...]
    ) -> tuple[OptionQuoteObservation, ...]:
        return tuple(
            self._observations[item] for item in observation_ids if item in self._observations
        )

    def save_snapshot(self, snapshot: OptionsChainSnapshot) -> OptionsChainSnapshot:
        self._snapshots[snapshot.snapshot_id] = snapshot
        return snapshot

    def snapshot_by_id(self, snapshot_id: str) -> OptionsChainSnapshot | None:
        return self._snapshots.get(snapshot_id)

    def snapshots_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionsChainSnapshot, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._snapshots.values()
                    if item.underlying_canonical_security_id == underlying_canonical_security_id
                ),
                key=lambda item: item.captured_at,
            )
        )

    def latest_snapshot_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> OptionsChainSnapshot | None:
        matches = self.snapshots_for_underlying(underlying_canonical_security_id)
        return matches[-1] if matches else None

    def save_research_features(
        self, features: OptionsVolatilityResearchFeatures
    ) -> OptionsVolatilityResearchFeatures:
        self._features[features.feature_id] = features
        return features

    def research_features_for_underlying(
        self, underlying_canonical_security_id: str
    ) -> tuple[OptionsVolatilityResearchFeatures, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._features.values()
                    if item.underlying_canonical_security_id == underlying_canonical_security_id
                ),
                key=lambda item: item.as_of,
            )
        )

    def research_features_by_snapshot(
        self, snapshot_id: str
    ) -> OptionsVolatilityResearchFeatures | None:
        return next(
            (item for item in self._features.values() if item.snapshot_id == snapshot_id), None
        )
