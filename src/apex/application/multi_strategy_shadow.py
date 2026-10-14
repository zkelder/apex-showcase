"""Shared-cohort, multi-strategy SHADOW campaign evaluation.

The service is intentionally non-financial.  It consumes retained evidence and
the three already-governed strategies, compares their independent forecasts,
and records a hypothetical capital plan.  It never creates a reservation,
order, position, journal, or settlement.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from apex.application.autopilot import ShadowOpportunity, StrategyDescriptor
from apex.application.strategy_shadow import ShadowPrediction
from apex.domain.autopilot import CampaignPortfolioPolicy, CampaignPortfolioSnapshot
from apex.domain.campaign import Campaign, CampaignSnapshot, OperationalInvariants
from apex.domain.decisions import CapitalAllocator, RiskKernel
from apex.domain.multi_strategy import (
    CompatiblePredictionEvidence,
    CrossStrategyComparison,
    EnrichedShadowMarket,
    MultiStrategyCycleState,
    ShadowCapitalAllocationState,
    ShadowMarketCohortConfiguration,
    StrategyConflictResolution,
    StrategyConflictType,
    compare_prediction_evidence,
)
from apex.domain.values import (
    ContentHash,
    Money,
    Quantity,
    ValueValidationError,
    content_digest,
    deterministic_id,
)


class MultiStrategyShadowError(RuntimeError):
    """A bounded, non-financial multi-strategy evaluation cannot proceed."""


class MultiStrategyIdempotencyConflictError(MultiStrategyShadowError):
    """A cycle identity was reused with different immutable input."""


@dataclass(frozen=True, slots=True)
class MultiStrategyShadowCycleCommand:
    campaign_id: str
    idempotency_key: str
    cohort_configuration: ShadowMarketCohortConfiguration
    strategy_configuration_hashes: tuple[tuple[str, str], ...]
    maximum_predictions: int
    maximum_opportunities: int
    deadline_at: datetime
    operator_note: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if not self.campaign_id or len(self.campaign_id) > 160:
            raise ValueValidationError("campaign ID is invalid")
        if not self.idempotency_key or len(self.idempotency_key) > 160:
            raise ValueValidationError("idempotency key is invalid")
        if not 1 <= self.maximum_predictions <= 300:
            raise ValueValidationError("maximum predictions must be between one and 300")
        if not 1 <= self.maximum_opportunities <= 100:
            raise ValueValidationError("maximum opportunities must be between one and 100")
        if len(self.operator_note) > 500:
            raise ValueValidationError("operator note exceeds its bound")
        if tuple(sorted(self.strategy_configuration_hashes)) != self.strategy_configuration_hashes:
            raise ValueValidationError("strategy configuration hashes must be sorted")
        if len({item[0] for item in self.strategy_configuration_hashes}) != len(
            self.strategy_configuration_hashes
        ):
            raise ValueValidationError("strategy configuration identities must be unique")
        for strategy_id, configuration_hash in self.strategy_configuration_hashes:
            if not strategy_id or len(strategy_id) > 160 or len(configuration_hash) != 64:
                raise ValueValidationError("strategy configuration provenance is invalid")
        for label, value in (
            ("requested time", self.requested_at),
            ("cycle deadline", self.deadline_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueValidationError(f"{label} must be timezone-aware")
        requested = self.requested_at.astimezone(UTC)
        deadline = self.deadline_at.astimezone(UTC)
        if deadline <= requested:
            raise ValueValidationError("cycle deadline must follow requested time")
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "deadline_at", deadline)

    @property
    def request_hash(self) -> str:
        return content_digest(
            {
                "campaign_id": self.campaign_id,
                "idempotency_key": self.idempotency_key,
                "cohort_configuration_hash": self.cohort_configuration.canonical_hash.value,
                "strategy_configuration_hashes": self.strategy_configuration_hashes,
                "maximum_predictions": self.maximum_predictions,
                "maximum_opportunities": self.maximum_opportunities,
                "deadline_at": self.deadline_at,
                "operator_note": self.operator_note,
            }
        )


@dataclass(frozen=True, slots=True)
class CohortBuildResult:
    cohort_id: str
    configuration: ShadowMarketCohortConfiguration
    selected: tuple[EnrichedShadowMarket, ...]
    rejected_count: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SharedCohortEnrichmentResult:
    """Bounded exact-enrichment outcome before cohort diversity selection."""

    cohort: CohortBuildResult
    attempted_count: int
    successful_count: int
    rejection_counts: tuple[tuple[str, int], ...]


class BuildEnrichedShadowMarketCohort:
    """Own bounded exact enrichment and deterministic shared-cohort selection."""

    @staticmethod
    def enrich(
        *,
        campaign_id: str,
        cycle_key: str,
        configuration: ShadowMarketCohortConfiguration,
        candidates: Iterable[object],
        enrich_candidate: Callable[[object], tuple[EnrichedShadowMarket | None, tuple[str, ...]]],
        decision_at: datetime,
    ) -> SharedCohortEnrichmentResult:
        """Enrich list candidates before applying event/series diversity caps.

        The callback is the only component permitted to turn a provisional
        list-market record into an ``EnrichedShadowMarket``.  It must return
        exact event, series, and market lineage or a machine-readable rejection
        reason.  This keeps generic cohort construction independent of the
        complement-research evaluator.
        """

        enriched: list[EnrichedShadowMarket] = []
        rejections: dict[str, int] = {}
        attempted = 0
        for candidate in candidates:
            if attempted >= configuration.maximum_enrichments:
                break
            attempted += 1
            value, reason_codes = enrich_candidate(candidate)
            if value is not None:
                enriched.append(value)
                continue
            for reason in reason_codes or ("ENRICHMENT_REJECTED",):
                rejections[reason] = rejections.get(reason, 0) + 1
        cohort = BuildEnrichedShadowMarketCohort.build(
            campaign_id=campaign_id,
            cycle_key=cycle_key,
            configuration=configuration,
            candidates=enriched,
            decision_at=decision_at,
        )
        return SharedCohortEnrichmentResult(
            cohort=cohort,
            attempted_count=attempted,
            successful_count=len(enriched),
            rejection_counts=tuple(sorted(rejections.items())),
        )

    @staticmethod
    def build(
        *,
        campaign_id: str,
        cycle_key: str,
        configuration: ShadowMarketCohortConfiguration,
        candidates: Iterable[EnrichedShadowMarket],
        decision_at: datetime,
    ) -> CohortBuildResult:
        decided_at = decision_at.astimezone(UTC)
        ordered = tuple(
            sorted(
                candidates,
                key=lambda value: (
                    value.event_ticker,
                    value.series_ticker,
                    value.market_ticker,
                    value.market_snapshot_id,
                ),
            )
        )
        selected: list[EnrichedShadowMarket] = []
        selected_snapshot_ids: set[str] = set()
        event_counts: dict[str, int] = defaultdict(int)
        series_counts: dict[str, int] = defaultdict(int)
        rejected = 0
        for market in ordered[: configuration.maximum_enrichments]:
            if market.market_snapshot_id in selected_snapshot_ids:
                rejected += 1
                continue
            if decided_at - market.available_at > configuration.maximum_observation_age:
                rejected += 1
                continue
            if event_counts[market.event_ticker] >= configuration.maximum_markets_per_event:
                rejected += 1
                continue
            if series_counts[market.series_ticker] >= configuration.maximum_markets_per_series:
                rejected += 1
                continue
            selected.append(market)
            selected_snapshot_ids.add(market.market_snapshot_id)
            event_counts[market.event_ticker] += 1
            series_counts[market.series_ticker] += 1
            if len(selected) == configuration.selected_market_count:
                break
        payload = {
            "campaign_id": campaign_id,
            "cycle_key": cycle_key,
            "configuration_hash": configuration.canonical_hash.value,
            "selected": tuple(value.canonical_hash.value for value in selected),
        }
        return CohortBuildResult(
            cohort_id=deterministic_id("multi_strategy_shadow_cohort", payload),
            configuration=configuration,
            selected=tuple(selected),
            rejected_count=rejected + max(0, len(ordered) - configuration.maximum_enrichments),
            reason_codes=("EXACT_EVENT_SERIES_RULE_LINEAGE_REQUIRED",),
        )


@dataclass(frozen=True, slots=True)
class MultiStrategyInvocation:
    descriptor: StrategyDescriptor
    predictions: tuple[ShadowPrediction, ...]
    opportunities: tuple[ShadowOpportunity, ...]
    feature_snapshot_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    failed: bool = False
    prediction_feature_snapshot_ids: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.failed and (self.predictions or self.opportunities):
            raise ValueValidationError("failed strategy invocation cannot retain outputs")
        if len(self.feature_snapshot_ids) > 100:
            raise ValueValidationError("strategy feature evidence exceeds its bound")
        if len(
            {prediction_id for prediction_id, _feature_id in self.prediction_feature_snapshot_ids}
        ) != len(self.prediction_feature_snapshot_ids):
            raise ValueValidationError("prediction feature provenance must be unique")
        prediction_ids = {prediction.prediction_id for prediction in self.predictions}
        if any(
            prediction_id not in prediction_ids or not feature_id
            for prediction_id, feature_id in self.prediction_feature_snapshot_ids
        ):
            raise ValueValidationError(
                "prediction feature provenance must reference retained output"
            )
        if not self.reason_codes:
            raise ValueValidationError("strategy invocation requires reason codes")


@dataclass(frozen=True, slots=True)
class RetainedStrategyConflict:
    conflict_id: str
    cycle_id: str
    instrument_id: str
    proposal_ids: tuple[str, ...]
    strategy_ids: tuple[str, ...]
    conflict_type: StrategyConflictType
    resolution: StrategyConflictResolution
    reason_codes: tuple[str, ...]
    canonical_hash: ContentHash


@dataclass(frozen=True, slots=True)
class HypotheticalCapitalAllocation:
    allocation_id: str
    cycle_id: str
    proposal_id: str
    strategy_id: str
    instrument_group: str
    ranking_ordinal: int | None
    requested_risk: Decimal
    policy_permitted_risk: Decimal
    allocated_risk: Decimal
    allocated_quantity: Decimal
    unallocated_risk: Decimal
    state: ShadowCapitalAllocationState
    binding_constraints: tuple[str, ...]
    reason_codes: tuple[str, ...]
    canonical_input_hash: ContentHash
    canonical_result_hash: ContentHash


def _yes_probability(prediction: ShadowPrediction, *, conservative: bool) -> Decimal:
    values = dict(
        prediction.conservative_probabilities if conservative else prediction.point_probabilities
    )
    return values["YES"]


def _prediction_evidence(
    prediction: ShadowPrediction,
    *,
    cohort_by_snapshot: dict[str, EnrichedShadowMarket],
    feature_snapshot_id: str | None,
) -> CompatiblePredictionEvidence:
    cohort = cohort_by_snapshot.get(prediction.market_evidence_id)
    return CompatiblePredictionEvidence(
        prediction_id=prediction.prediction_id,
        strategy_id=prediction.strategy_id,
        strategy_version=prediction.strategy_version,
        market_ticker=prediction.instrument_id,
        event_ticker=prediction.event_id,
        decision_at=prediction.decision_at,
        market_yes_probability=dict(prediction.market_probabilities)["YES"],
        point_yes_probability=_yes_probability(prediction, conservative=False),
        conservative_yes_probability=_yes_probability(prediction, conservative=True),
        market_evidence_id=prediction.market_evidence_id,
        feature_snapshot_id=feature_snapshot_id,
        configuration_version=prediction.model_configuration_version,
        rule_content_hash=cohort.rule_content_hash if cohort is not None else None,
    )


def compare_independent_predictions(
    *,
    cycle_id: str,
    cohort: CohortBuildResult,
    invocations: Iterable[MultiStrategyInvocation],
    maximum_prediction_skew: timedelta = timedelta(minutes=5),
) -> tuple[CrossStrategyComparison, ...]:
    """Group only same exact retained market identity; never ensemble forecasts."""

    by_market: dict[str, list[CompatiblePredictionEvidence]] = defaultdict(list)
    cohort_by_snapshot = {value.market_snapshot_id: value for value in cohort.selected}
    for invocation in invocations:
        feature_by_prediction = dict(invocation.prediction_feature_snapshot_ids)
        for prediction in invocation.predictions:
            by_market[prediction.instrument_id].append(
                _prediction_evidence(
                    prediction,
                    cohort_by_snapshot=cohort_by_snapshot,
                    feature_snapshot_id=feature_by_prediction.get(prediction.prediction_id),
                )
            )
    return tuple(
        compare_prediction_evidence(
            cycle_id=cycle_id,
            values=tuple(values),
            maximum_prediction_skew=maximum_prediction_skew,
        )
        for _market, values in sorted(by_market.items())
    )


def detect_strategy_conflicts(
    *, cycle_id: str, opportunities: Iterable[ShadowOpportunity]
) -> tuple[RetainedStrategyConflict, ...]:
    grouped: dict[str, list[ShadowOpportunity]] = defaultdict(list)
    for opportunity in opportunities:
        grouped[opportunity.proposal.instrument_id].append(opportunity)
    conflicts: list[RetainedStrategyConflict] = []
    for instrument_id, values in sorted(grouped.items()):
        if len(values) < 2:
            continue
        ordered = tuple(sorted(values, key=lambda value: value.proposal.proposal_id))
        sides = {value.proposal.side for value in ordered}
        conflict_type = (
            StrategyConflictType.OPPOSITE_DIRECTION
            if len(sides) > 1
            else StrategyConflictType.SAME_DIRECTION_DUPLICATE
        )
        resolution = (
            StrategyConflictResolution.REJECT_BOTH_DIRECTIONAL_CONFLICT
            if conflict_type is StrategyConflictType.OPPOSITE_DIRECTION
            else StrategyConflictResolution.GROUP_FOR_CAP
        )
        reasons = (
            "REJECT_BOTH_DIRECTIONAL_CONFLICT"
            if conflict_type is StrategyConflictType.OPPOSITE_DIRECTION
            else "SAME_INSTRUMENT_EXPOSURE_GROUP_CAP_ONCE",
        )
        proposal_ids = tuple(value.proposal.proposal_id for value in ordered)
        strategy_ids = tuple(value.economics.strategy_id for value in ordered)
        payload = {
            "cycle_id": cycle_id,
            "instrument_id": instrument_id,
            "proposal_ids": proposal_ids,
            "strategies": strategy_ids,
            "conflict_type": conflict_type,
            "resolution": resolution,
        }
        conflicts.append(
            RetainedStrategyConflict(
                conflict_id=deterministic_id("multi_strategy_conflict", payload),
                cycle_id=cycle_id,
                instrument_id=instrument_id,
                proposal_ids=proposal_ids,
                strategy_ids=strategy_ids,
                conflict_type=conflict_type,
                resolution=resolution,
                reason_codes=reasons,
                canonical_hash=ContentHash(content_digest(payload)),
            )
        )
    return tuple(conflicts)


def _ranking_key(opportunity: ShadowOpportunity) -> tuple[object, ...]:
    economics = opportunity.economics
    visible = economics.visible_quantity.value if economics.visible_quantity else Decimal(-1)
    return (
        -economics.evidence_tier.rank,
        -economics.conservative_net_expected_value,
        -economics.capital_efficiency if economics.capital_efficiency is not None else Decimal(0),
        economics.capital_lockup_seconds,
        -visible,
        economics.strategy_id,
        opportunity.proposal.proposal_id,
    )


def allocate_hypothetical_shadow_capital(
    *,
    cycle_id: str,
    campaign: Campaign,
    portfolio_policy: CampaignPortfolioPolicy,
    portfolio_snapshot: CampaignPortfolioSnapshot,
    opportunities: Iterable[ShadowOpportunity],
    conflicts: Iterable[RetainedStrategyConflict],
    decision_at: datetime,
) -> tuple[HypotheticalCapitalAllocation, ...]:
    """Apply policy, allocator, and sealed risk checks without financial mutation."""

    conflict_by_proposal = {
        proposal_id: conflict
        for conflict in conflicts
        for proposal_id in conflict.proposal_ids
        if conflict.resolution is StrategyConflictResolution.REJECT_BOTH_DIRECTIONAL_CONFLICT
    }
    ranked = tuple(sorted(opportunities, key=_ranking_key))
    projected_risk = Decimal(0)
    projected_by_strategy: dict[str, Decimal] = defaultdict(Decimal)
    projected_by_bucket: dict[str, Decimal] = defaultdict(Decimal)
    allocated_by_instrument: dict[str, Decimal] = defaultdict(Decimal)
    output: list[HypotheticalCapitalAllocation] = []
    for ordinal, opportunity in enumerate(ranked, start=1):
        economics = opportunity.economics
        proposal = opportunity.proposal
        requested = economics.maximum_loss
        reasons: list[str] = []
        if proposal.proposal_id in conflict_by_proposal:
            reasons.append("REJECT_BOTH_DIRECTIONAL_CONFLICT")
        if economics.conservative_net_expected_value <= 0:
            reasons.append("NONPOSITIVE_CONSERVATIVE_NET_EXPECTED_VALUE")
        equity = portfolio_snapshot.ledger_equity
        available = min(
            portfolio_snapshot.remaining_loss_capacity - projected_risk,
            equity * portfolio_policy.maximum_active_risk_fraction.value
            - portfolio_snapshot.active_risk
            - projected_risk,
        )
        available = max(Decimal(0), available)
        per_opportunity_cap = equity * portfolio_policy.maximum_per_opportunity_risk_fraction.value
        strategy_cap = (
            equity * portfolio_policy.maximum_per_strategy_risk_fraction.value
            - projected_by_strategy[economics.strategy_id]
        )
        bucket_cap = min(
            (
                equity * portfolio_policy.maximum_per_correlation_bucket_risk_fraction.value
                - projected_by_bucket[bucket]
                for bucket in economics.correlation_buckets
            ),
            default=Decimal(0),
        )
        group_cap = per_opportunity_cap - allocated_by_instrument[proposal.instrument_id]
        permitted = max(
            Decimal(0),
            min(requested, available, per_opportunity_cap, strategy_cap, bucket_cap, group_cap),
        )
        if requested > per_opportunity_cap:
            reasons.append("PORTFOLIO_PER_OPPORTUNITY_CAP_EXCEEDED")
        if requested > strategy_cap:
            reasons.append("PORTFOLIO_STRATEGY_CAP_EXCEEDED")
        if requested > bucket_cap:
            reasons.append("PORTFOLIO_CORRELATION_CAP_EXCEEDED")
        if requested > group_cap:
            reasons.append("SAME_INSTRUMENT_GROUP_CAP_EXCEEDED")
        if requested > available:
            reasons.append("SHADOW_CAPACITY_EXHAUSTED")
        allocated = Decimal(0)
        quantity = Decimal(0)
        if not reasons and permitted > 0:
            visible = economics.visible_quantity or Quantity("1", "1")
            projected_snapshot = CampaignSnapshot(
                campaign=campaign,
                ledger_equity=Money(portfolio_snapshot.ledger_equity),
                settled_cash=Money(portfolio_snapshot.settled_cash),
                active_reservations=Money(portfolio_snapshot.active_risk + projected_risk),
            )
            allocation = CapitalAllocator.allocate(
                proposal,
                projected_snapshot,
                visible,
                campaign.policy,
                decision_at=economics.decision_at,
                expected_settlement_at=economics.capital_release_at,
            )
            risk = RiskKernel.evaluate(
                proposal,
                allocation,
                projected_snapshot,
                opportunity.information_set,
                campaign.policy,
                OperationalInvariants(),
                authorization_at=economics.decision_at,
                market_observation_source_id=opportunity.market_observation_source_id,
            )
            if allocation.accepted and risk.accepted:
                allocated = min(permitted, allocation.maximum_loss.amount)
                if allocated <= 0:
                    reasons.append("ZERO_HYPOTHETICAL_ALLOCATION")
                else:
                    quantity = allocation.quantity.value
                    projected_risk += allocated
                    projected_by_strategy[economics.strategy_id] += allocated
                    allocated_by_instrument[proposal.instrument_id] += allocated
                    for bucket in economics.correlation_buckets:
                        projected_by_bucket[bucket] += allocated
            else:
                reasons.extend(
                    allocation.reason_codes if not allocation.accepted else risk.reason_codes
                )
        state = (
            ShadowCapitalAllocationState.ADMITTED
            if allocated > 0
            else ShadowCapitalAllocationState.REJECTED
        )
        final_reasons = tuple(sorted(set(reasons or {"HYPOTHETICAL_SHADOW_ADMISSION"})))
        payload = {
            "cycle_id": cycle_id,
            "proposal_id": proposal.proposal_id,
            "rank": ordinal,
            "requested_risk": requested,
            "policy_permitted_risk": permitted,
            "reasons": final_reasons,
        }
        output.append(
            HypotheticalCapitalAllocation(
                allocation_id=deterministic_id("multi_strategy_shadow_allocation", payload),
                cycle_id=cycle_id,
                proposal_id=proposal.proposal_id,
                strategy_id=economics.strategy_id,
                instrument_group=proposal.instrument_id,
                ranking_ordinal=ordinal,
                requested_risk=requested,
                policy_permitted_risk=permitted,
                allocated_risk=allocated,
                allocated_quantity=quantity,
                unallocated_risk=max(Decimal(0), requested - allocated),
                state=state,
                binding_constraints=tuple(
                    reason
                    for reason in final_reasons
                    if reason.startswith("PORTFOLIO_")
                    or reason.startswith("SAME_")
                    or reason.startswith("SHADOW_")
                ),
                reason_codes=final_reasons,
                canonical_input_hash=ContentHash(content_digest(payload)),
                canonical_result_hash=ContentHash(
                    content_digest(
                        {
                            "payload": payload,
                            "allocated": allocated,
                            "quantity": quantity,
                            "state": state,
                        }
                    )
                ),
            )
        )
    return tuple(output)


@dataclass(frozen=True, slots=True)
class MultiStrategyShadowCycleResult:
    cycle_id: str
    campaign_id: str
    state: MultiStrategyCycleState
    idempotent_replay: bool
    cohort: CohortBuildResult
    invocations: tuple[MultiStrategyInvocation, ...]
    invocation_count: int
    prediction_count: int
    opportunity_count: int
    comparisons: tuple[CrossStrategyComparison, ...]
    conflicts: tuple[RetainedStrategyConflict, ...]
    allocations: tuple[HypotheticalCapitalAllocation, ...]
    remaining_shadow_capacity: Decimal
    reason_codes: tuple[str, ...]


class MultiStrategyShadowStore(Protocol):
    def start_multi_strategy_cycle(
        self, command: MultiStrategyShadowCycleCommand
    ) -> tuple[str, Campaign, CampaignPortfolioPolicy, CampaignPortfolioSnapshot, bool]: ...

    def complete_multi_strategy_cycle(self, result: MultiStrategyShadowCycleResult) -> None: ...

    def fail_multi_strategy_cycle(
        self, cycle_id: str, *, reason_code: str, completed_at: datetime
    ) -> None: ...

    def get_multi_strategy_cycle(
        self, cycle_id: str, command: MultiStrategyShadowCycleCommand
    ) -> MultiStrategyShadowCycleResult: ...


class RunMultiStrategyShadowCampaignCycle:
    """Persist bounded stages independently; provider calls belong outside this service."""

    def __init__(self, store: MultiStrategyShadowStore) -> None:
        self._store = store

    def run(
        self,
        *,
        command: MultiStrategyShadowCycleCommand,
        cohort: CohortBuildResult,
        invocations: tuple[MultiStrategyInvocation, ...],
    ) -> MultiStrategyShadowCycleResult:
        cycle_id, campaign, policy, snapshot, replay = self._store.start_multi_strategy_cycle(
            command
        )
        if replay:
            return self._store.get_multi_strategy_cycle(cycle_id, command)
        try:
            prediction_count = sum(len(item.predictions) for item in invocations)
            if prediction_count > command.maximum_predictions:
                raise MultiStrategyShadowError("maximum prediction bound exceeded")
            opportunities = tuple(
                opportunity
                for invocation in invocations
                for opportunity in invocation.opportunities
            )
            if len(opportunities) > command.maximum_opportunities:
                raise MultiStrategyShadowError("maximum opportunity bound exceeded")
            comparisons = compare_independent_predictions(
                cycle_id=cycle_id, cohort=cohort, invocations=invocations
            )
            conflicts = detect_strategy_conflicts(cycle_id=cycle_id, opportunities=opportunities)
            allocations = allocate_hypothetical_shadow_capital(
                cycle_id=cycle_id,
                campaign=campaign,
                portfolio_policy=policy,
                portfolio_snapshot=snapshot,
                opportunities=opportunities,
                conflicts=conflicts,
                decision_at=command.requested_at,
            )
            state = (
                MultiStrategyCycleState.PARTIAL
                if any(item.failed for item in invocations)
                else MultiStrategyCycleState.COMPLETED
            )
            result = MultiStrategyShadowCycleResult(
                cycle_id=cycle_id,
                campaign_id=command.campaign_id,
                state=state,
                idempotent_replay=False,
                cohort=cohort,
                invocations=invocations,
                invocation_count=len(invocations),
                prediction_count=prediction_count,
                opportunity_count=len(opportunities),
                comparisons=comparisons,
                conflicts=conflicts,
                allocations=allocations,
                remaining_shadow_capacity=max(
                    Decimal(0),
                    min(
                        snapshot.remaining_loss_capacity,
                        snapshot.ledger_equity * policy.maximum_active_risk_fraction.value
                        - snapshot.active_risk,
                    )
                    - sum((item.allocated_risk for item in allocations), Decimal(0)),
                ),
                reason_codes=("SHADOW_HYPOTHETICAL_NO_CAPITAL_RESERVED",),
            )
            self._store.complete_multi_strategy_cycle(result)
            return result
        except Exception:
            self._store.fail_multi_strategy_cycle(
                cycle_id,
                reason_code="MULTI_STRATEGY_CYCLE_FAILED",
                completed_at=command.requested_at,
            )
            raise
