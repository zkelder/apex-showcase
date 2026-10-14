"""Idempotent application orchestration for broad-equity research."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from apex.domain.broad_equity import (
    BroadEquityDiscoveryRun,
    BroadEquityFeatures,
    BroadEquityShadowPlan,
    SecurityDataQuality,
    build_broad_equity_discovery_run,
    build_broad_research_portfolio,
    classify_broad_market_regime,
    create_broad_equity_shadow_plan,
    rank_broad_equity_opportunities,
)


class BroadEquityApplicationError(RuntimeError):
    pass


class BroadEquityRepository(Protocol):
    def discovery_by_key(self, key: str) -> BroadEquityDiscoveryRun | None: ...
    def save_discovery(self, key: str, run: BroadEquityDiscoveryRun) -> BroadEquityDiscoveryRun: ...
    def discovery(self, run_id: str) -> BroadEquityDiscoveryRun | None: ...
    def shadow_by_key(self, key: str) -> BroadEquityShadowPlan | None: ...
    def save_shadow(self, key: str, plan: BroadEquityShadowPlan) -> BroadEquityShadowPlan: ...
    def shadow(self, plan_id: str) -> BroadEquityShadowPlan | None: ...

    def save_quality(
        self,
        capture_id: str,
        qualities: tuple[SecurityDataQuality, ...],
        *,
        recorded_at: datetime,
    ) -> tuple[SecurityDataQuality, ...]: ...

    def qualities_for_capture(self, capture_id: str) -> tuple[SecurityDataQuality, ...]: ...


class RunBroadEquityDiscovery:
    def __init__(self, repository: BroadEquityRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        universe_id: str,
        data_capture_id: str,
        features: tuple[BroadEquityFeatures, ...],
        qualities: tuple[SecurityDataQuality, ...],
        idempotency_key: str,
    ) -> BroadEquityDiscoveryRun:
        regime = classify_broad_market_regime(features)
        opportunities = rank_broad_equity_opportunities(
            features=features, qualities=qualities, regime=regime
        )
        candidate = build_broad_equity_discovery_run(
            universe_id=universe_id,
            data_capture_id=data_capture_id,
            regime=regime,
            opportunities=opportunities,
            portfolio=build_broad_research_portfolio(opportunities=opportunities),
        )
        prior = self._repository.discovery_by_key(idempotency_key)
        if prior is not None:
            if prior.canonical_hash != candidate.canonical_hash:
                raise BroadEquityApplicationError("BROAD_DISCOVERY_IDEMPOTENCY_CONFLICT")
            return prior
        return self._repository.save_discovery(idempotency_key, candidate)


class CreateBroadEquityShadowPlan:
    def __init__(self, repository: BroadEquityRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        discovery_run: BroadEquityDiscoveryRun,
        policy_hashes: tuple[str, ...],
        top_n: int,
        decision_at: datetime,
        forward_horizon_sessions: int,
        data_cutoff: datetime,
        idempotency_key: str,
    ) -> BroadEquityShadowPlan:
        candidate = create_broad_equity_shadow_plan(
            discovery_run=discovery_run,
            policy_hashes=policy_hashes,
            top_n=top_n,
            decision_at=decision_at,
            forward_horizon_sessions=forward_horizon_sessions,
            data_cutoff=data_cutoff,
        )
        prior = self._repository.shadow_by_key(idempotency_key)
        if prior is not None:
            if prior.canonical_hash != candidate.canonical_hash:
                raise BroadEquityApplicationError("BROAD_SHADOW_IDEMPOTENCY_CONFLICT")
            return prior
        return self._repository.save_shadow(idempotency_key, candidate)


class RecordBroadEquityInclusion:
    """Durably record inclusion/exclusion decisions for one bounded data-capture.

    ``assess_security_data_quality`` is pure per-security domain logic; this
    service is the idempotent, repository-backed boundary that retains its
    result as evidence grouped under one exact ``capture_id``.
    """

    def __init__(self, repository: BroadEquityRepository) -> None:
        self._repository = repository

    def execute(
        self,
        *,
        capture_id: str,
        qualities: tuple[SecurityDataQuality, ...],
        recorded_at: datetime,
    ) -> tuple[SecurityDataQuality, ...]:
        if not capture_id:
            raise BroadEquityApplicationError("BROAD_EQUITY_CAPTURE_ID_REQUIRED")
        existing = self._repository.qualities_for_capture(capture_id)
        existing_hashes = {item.canonical_hash for item in existing}
        candidate_hashes = {item.canonical_hash for item in qualities}
        if existing and existing_hashes != candidate_hashes:
            raise BroadEquityApplicationError("BROAD_EQUITY_QUALITY_IDEMPOTENCY_CONFLICT")
        if existing:
            return existing
        return self._repository.save_quality(capture_id, qualities, recorded_at=recorded_at)


class InMemoryBroadEquityRepository:
    def __init__(self) -> None:
        self._discoveries: dict[str, BroadEquityDiscoveryRun] = {}
        self._shadows: dict[str, BroadEquityShadowPlan] = {}
        self._qualities: dict[str, tuple[SecurityDataQuality, ...]] = {}

    def discovery_by_key(self, key: str) -> BroadEquityDiscoveryRun | None:
        return self._discoveries.get(key)

    def save_discovery(self, key: str, run: BroadEquityDiscoveryRun) -> BroadEquityDiscoveryRun:
        self._discoveries[key] = run
        return run

    def discovery(self, run_id: str) -> BroadEquityDiscoveryRun | None:
        return next((item for item in self._discoveries.values() if item.run_id == run_id), None)

    def shadow_by_key(self, key: str) -> BroadEquityShadowPlan | None:
        return self._shadows.get(key)

    def save_shadow(self, key: str, plan: BroadEquityShadowPlan) -> BroadEquityShadowPlan:
        self._shadows[key] = plan
        return plan

    def shadow(self, plan_id: str) -> BroadEquityShadowPlan | None:
        return next((item for item in self._shadows.values() if item.plan_id == plan_id), None)

    def save_quality(
        self,
        capture_id: str,
        qualities: tuple[SecurityDataQuality, ...],
        *,
        recorded_at: datetime,
    ) -> tuple[SecurityDataQuality, ...]:
        self._qualities[capture_id] = qualities
        return qualities

    def qualities_for_capture(self, capture_id: str) -> tuple[SecurityDataQuality, ...]:
        return self._qualities.get(capture_id, ())
