"""Pure deterministic rules for authority-free cross-venue PAPER trial planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from apex.domain.shared_opportunity import (
    EligibilityDecision,
    EligibilityState,
    PriorityResult,
    SharedOpportunityProjection,
    SharedOpportunitySet,
    SharedVenue,
)
from apex.domain.total_market_context import (
    ContextEnrichedOpportunitySet,
    OverallContextState,
    TotalMarketContextSnapshot,
)
from apex.domain.values import content_digest

CROSS_VENUE_ALLOCATION_POLICY_V1 = "CROSS_VENUE_ALLOCATION_POLICY_V1"
CROSS_VENUE_ALLOCATION_WARNING = (
    "CROSS-VENUE ALLOCATION IS HYPOTHETICAL — NO CAPITAL, ORDER, OR EXECUTION AUTHORITY"
)
TOTAL_CAPITAL = Decimal("1000")
MINIMUM_CASH_FRACTION = Decimal("0.25")
MAXIMUM_OPPORTUNITY_FRACTION = Decimal("0.20")
MAXIMUM_STRATEGY_FRACTION = Decimal("0.25")
MAXIMUM_INSTRUMENT_FRACTION = Decimal("0.25")
MAXIMUM_VENUE_FRACTION = Decimal("0.40")
MAXIMUM_LONG_LOCKUP_FRACTION = Decimal("0.25")
MAXIMUM_DOWNSIDE_FRACTION = Decimal("0.10")
LONG_LOCKUP_DAYS = 30
_MONEY = Decimal("0.00000001")
_CONTEXT_CASH_FRACTIONS = {
    OverallContextState.RISK_SUPPORTIVE: Decimal("0.25"),
    OverallContextState.MIXED: Decimal("0.35"),
    OverallContextState.RISK_CAUTIONARY: Decimal("0.50"),
    OverallContextState.MARKET_STRESS: Decimal("0.75"),
    OverallContextState.INSUFFICIENT_CONTEXT: Decimal("1.00"),
}


class CrossVenueAllocationError(ValueError):
    """Raised when allocation evidence is incomplete, unsafe, or conflicting."""


def _id(value: str, reason: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 300:
        raise CrossVenueAllocationError(reason)
    return normalized


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None:
        raise CrossVenueAllocationError(reason)
    return value.astimezone(UTC)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY, rounding=ROUND_DOWN)


@dataclass(frozen=True, slots=True)
class CrossVenueAllocationPolicy:
    policy_id: str
    total_hypothetical_capital: Decimal
    minimum_retained_cash_fraction: Decimal
    maximum_opportunity_fraction: Decimal
    maximum_strategy_fraction: Decimal
    maximum_instrument_fraction: Decimal
    maximum_venue_fraction: Decimal
    maximum_long_lockup_fraction: Decimal
    maximum_aggregate_downside_fraction: Decimal
    long_lockup_days: int
    context_cash_fractions: tuple[tuple[str, Decimal], ...]
    exclusions: tuple[str, ...]
    allocation_method: str
    canonical_hash: str
    authority: str = "NONE"
    paper_authorization: bool = False
    order_authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def v1(cls) -> CrossVenueAllocationPolicy:
        values = (
            CROSS_VENUE_ALLOCATION_POLICY_V1,
            TOTAL_CAPITAL,
            MINIMUM_CASH_FRACTION,
            MAXIMUM_OPPORTUNITY_FRACTION,
            MAXIMUM_STRATEGY_FRACTION,
            MAXIMUM_INSTRUMENT_FRACTION,
            MAXIMUM_VENUE_FRACTION,
            MAXIMUM_LONG_LOCKUP_FRACTION,
            MAXIMUM_DOWNSIDE_FRACTION,
            LONG_LOCKUP_DAYS,
            tuple((state.value, _CONTEXT_CASH_FRACTIONS[state]) for state in OverallContextState),
            (
                "EXCLUDE_NON_ELIGIBLE_SHARED_OPPORTUNITIES",
                "EXCLUDE_STALE_EXPIRED_OR_MISSING_EVIDENCE",
                "EXCLUDE_UNAVAILABLE_VENUE_PAPER_BOUNDARY",
                "NEVER_EXCEED_SOURCE_MAXIMUM",
            ),
            "DETERMINISTIC_RANK_ORDER_INCREMENTAL_V1",
            "NONE",
            False,
            "NONE",
            "NONE",
        )
        return cls(*values[:-4], content_digest(values), *values[-4:])


@dataclass(frozen=True, slots=True)
class VenuePaperReadiness:
    venue: SharedVenue
    available: bool
    capability_id: str | None
    account_id: str | None
    reason_codes: tuple[str, ...]
    authority: str = "PAPER_ONLY"
    live_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class AllocationConstraintExplanation:
    explanation_id: str
    decision_id: str
    projection_id: str
    allocated: bool
    binding_constraints: tuple[str, ...]
    excluded_reasons: tuple[str, ...]
    text: str
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CrossVenueAllocationLine:
    line_id: str
    decision_id: str
    projection_id: str
    source_projection_hash: str
    venue: SharedVenue
    strategy_or_method_id: str
    instrument_id: str
    direction: str
    rank: int
    priority_score: Decimal
    amount: Decimal
    modeled_downside: Decimal
    modeled_costs: Decimal
    estimated_lockup_days: int | None
    source_maximum: Decimal
    binding_constraints: tuple[str, ...]
    explanation_id: str
    canonical_hash: str
    authority: str = "NONE"
    reservation: bool = False
    order: bool = False
    live_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class PaperTrialProposal:
    proposal_id: str
    decision_id: str
    allocation_line_id: str
    source_projection_id: str
    source_projection_hash: str
    venue: SharedVenue
    instrument_id: str
    side: str
    maximum_notional: Decimal
    maximum_modeled_risk: Decimal
    paper_account_id: str
    paper_capability_id: str
    paper_eligibility: str
    operator_authorization_required: bool
    operator_authorization_id: str | None
    expires_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    authorization: bool = False
    reservation: bool = False
    order: bool = False
    live_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CrossVenueAllocationDecision:
    decision_id: str
    version: int
    policy_id: str
    policy_hash: str
    opportunity_set_id: str
    opportunity_set_hash: str
    enriched_set_id: str
    enriched_set_hash: str
    context_snapshot_id: str
    context_snapshot_hash: str
    context_state: OverallContextState
    decided_at: datetime
    considered_projection_ids: tuple[str, ...]
    eligible_projection_ids: tuple[str, ...]
    excluded_projection_ids: tuple[str, ...]
    allocation_line_ids: tuple[str, ...]
    explanation_ids: tuple[str, ...]
    paper_trial_proposal_ids: tuple[str, ...]
    hypothetical_capital: Decimal
    retained_cash: Decimal
    venue_totals: tuple[tuple[str, Decimal], ...]
    modeled_downside: Decimal
    modeled_costs: Decimal
    long_lockup_capital: Decimal
    concentration: tuple[tuple[str, Decimal], ...]
    context_effects: tuple[str, ...]
    warnings: tuple[str, ...]
    source_manifest_hash: str
    canonical_hash: str
    authority: str = "NONE"
    capital_authority: str = "NONE"
    paper_authorization: bool = False
    reservation: bool = False
    order: bool = False
    live_authority: str = "NONE"
    financial_mutation_capability: str = "NONE"


def allocate_cross_venue(
    *,
    decision_id: str,
    opportunity_set: SharedOpportunitySet,
    enriched_set: ContextEnrichedOpportunitySet,
    context: TotalMarketContextSnapshot,
    projections: tuple[SharedOpportunityProjection, ...],
    eligibility: tuple[EligibilityDecision, ...],
    priorities: tuple[PriorityResult, ...],
    paper_readiness: tuple[VenuePaperReadiness, ...],
    decided_at: datetime,
) -> tuple[
    CrossVenueAllocationDecision,
    tuple[CrossVenueAllocationLine, ...],
    tuple[AllocationConstraintExplanation, ...],
    tuple[PaperTrialProposal, ...],
]:
    """Allocate incrementally in retained rank order without optimization or execution."""

    policy = CrossVenueAllocationPolicy.v1()
    decision_identity = _id(decision_id, "CROSS_VENUE_DECISION_ID_INVALID")
    now = _time(decided_at, "CROSS_VENUE_DECISION_TIME_INVALID")
    if (
        enriched_set.original_opportunity_set_id != opportunity_set.opportunity_set_id
        or enriched_set.original_opportunity_set_hash != opportunity_set.canonical_hash
        or enriched_set.context_snapshot_id != context.snapshot_id
    ):
        raise CrossVenueAllocationError("CROSS_VENUE_SOURCE_LINEAGE_MISMATCH")
    by_projection = {item.projection_id: item for item in projections}
    if tuple(sorted(by_projection)) != tuple(sorted(opportunity_set.projection_ids)):
        raise CrossVenueAllocationError("CROSS_VENUE_PROJECTION_LINEAGE_INCOMPLETE")
    decision_by_projection = {item.projection_id: item for item in eligibility}
    readiness = {item.venue: item for item in paper_readiness}
    if len(readiness) != len(paper_readiness):
        raise CrossVenueAllocationError("CROSS_VENUE_READINESS_DUPLICATE")

    cash_fraction = _CONTEXT_CASH_FRACTIONS[context.overall_state]
    retained_cash_floor = _money(policy.total_hypothetical_capital * cash_fraction)
    investable = policy.total_hypothetical_capital - retained_cash_floor
    venue_cap = _money(policy.total_hypothetical_capital * policy.maximum_venue_fraction)
    opportunity_cap = _money(
        policy.total_hypothetical_capital * policy.maximum_opportunity_fraction
    )
    strategy_cap = _money(policy.total_hypothetical_capital * policy.maximum_strategy_fraction)
    instrument_cap = _money(policy.total_hypothetical_capital * policy.maximum_instrument_fraction)
    downside_cap = _money(
        policy.total_hypothetical_capital * policy.maximum_aggregate_downside_fraction
    )
    long_lockup_cap = _money(
        policy.total_hypothetical_capital * policy.maximum_long_lockup_fraction
    )
    venue_totals = {venue: Decimal(0) for venue in SharedVenue}
    strategy_totals: dict[str, Decimal] = {}
    instrument_totals: dict[str, Decimal] = {}
    total_allocated = Decimal(0)
    total_downside = Decimal(0)
    total_costs = Decimal(0)
    locked = Decimal(0)
    eligible_ids: list[str] = []
    excluded_ids: list[str] = []
    lines: list[CrossVenueAllocationLine] = []
    explanations: list[AllocationConstraintExplanation] = []
    proposals: list[PaperTrialProposal] = []

    for priority in sorted(priorities, key=lambda item: (item.rank, item.tie_break_key)):
        projection = by_projection[priority.projection_id]
        shared_decision = decision_by_projection.get(projection.projection_id)
        boundary = readiness.get(projection.venue)
        excluded: list[str] = []
        if shared_decision is None or shared_decision.state is not EligibilityState.ELIGIBLE:
            excluded.append(
                "SHARED_OPPORTUNITY_NOT_ELIGIBLE"
                if shared_decision is None
                else f"SHARED_OPPORTUNITY_{shared_decision.state.value}"
            )
        if projection.valid_until is not None and now >= projection.valid_until:
            excluded.append("OPPORTUNITY_EXPIRED")
        if now < projection.generated_at:
            excluded.append("EVIDENCE_NOT_YET_AVAILABLE")
        elapsed_seconds = max(0, int((now - projection.generated_at).total_seconds()))
        if projection.freshness_seconds + elapsed_seconds > projection.freshness_limit_seconds:
            excluded.append("EVIDENCE_STALE")
        if (
            projection.maximum_loss is None
            or projection.required_hypothetical_capital is None
            or not projection.costs_retained
        ):
            excluded.append("ALLOCATION_EVIDENCE_MISSING")
        if boundary is None or not boundary.available:
            excluded.append("VENUE_PAPER_BOUNDARY_UNAVAILABLE")
        elif boundary.account_id is None:
            excluded.append("VENUE_PAPER_ACCOUNT_UNAVAILABLE")
        elif boundary.capability_id is None:
            excluded.append("VENUE_PAPER_CAPABILITY_UNAVAILABLE")
        if excluded:
            excluded_ids.append(projection.projection_id)
            explanation_id = f"{decision_identity}-{projection.projection_id}-explanation"
            reasons = tuple(sorted(set((*excluded, *(boundary.reason_codes if boundary else ())))))
            excluded_payload = (
                explanation_id,
                decision_identity,
                projection.projection_id,
                False,
                (),
                reasons,
                f"Zero allocation: {','.join(reasons)}.",
                "NONE",
            )
            explanations.append(
                AllocationConstraintExplanation(
                    *excluded_payload[:-1], content_digest(excluded_payload), "NONE"
                )
            )
            continue

        eligible_ids.append(projection.projection_id)
        required = projection.required_hypothetical_capital or Decimal(0)
        source_caps = [projection.maximum_sensible_research_allocation or Decimal(0)]
        if projection.source_maximum_allocation_fraction is not None:
            source_caps.append(
                _money(
                    policy.total_hypothetical_capital
                    * projection.source_maximum_allocation_fraction
                )
            )
        source_maximum = min(source_caps)
        caps: dict[str, Decimal] = {
            "RETAINED_CASH_FLOOR": investable - total_allocated,
            "OPPORTUNITY_CAP": opportunity_cap,
            "VENUE_CAP": venue_cap - venue_totals[projection.venue],
            "STRATEGY_CAP": strategy_cap
            - strategy_totals.get(projection.strategy_or_method_id, Decimal(0)),
            "INSTRUMENT_CAP": instrument_cap
            - instrument_totals.get(projection.instrument_id, Decimal(0)),
            "SOURCE_MAXIMUM": source_maximum,
        }
        is_long_lockup = (
            projection.estimated_lockup_days is not None
            and projection.estimated_lockup_days > policy.long_lockup_days
        )
        if is_long_lockup:
            caps["LONG_LOCKUP_CAP"] = long_lockup_cap - locked
        downside_rate = (
            Decimal(0) if required <= 0 else (projection.maximum_loss or Decimal(0)) / required
        )
        if downside_rate > 0:
            caps["AGGREGATE_DOWNSIDE_CAP"] = _money((downside_cap - total_downside) / downside_rate)
        amount = _money(max(Decimal(0), min(caps.values())))
        binding = tuple(sorted(name for name, value in caps.items() if _money(value) == amount))
        if amount <= 0 or amount < required:
            excluded_ids.append(projection.projection_id)
            reasons = ("CONSTRAINT_CAP_BELOW_REQUIRED_CAPITAL",)
            explanation_id = f"{decision_identity}-{projection.projection_id}-explanation"
            text = (
                f"Zero allocation at rank {priority.rank}: required={required}; "
                f"available={amount}; binding={','.join(binding)}."
            )
            constrained_payload = (
                explanation_id,
                decision_identity,
                projection.projection_id,
                False,
                binding,
                reasons,
                text,
                "NONE",
            )
            explanations.append(
                AllocationConstraintExplanation(
                    *constrained_payload[:-1], content_digest(constrained_payload), "NONE"
                )
            )
            continue

        downside = _money(amount * downside_rate)
        cost_rate = (
            Decimal(0)
            if required <= 0
            else (
                (projection.modeled_fees or Decimal(0))
                + (projection.modeled_slippage or Decimal(0))
            )
            / required
        )
        costs = _money(amount * cost_rate)
        explanation_id = f"{decision_identity}-{projection.projection_id}-explanation"
        line_id = f"{decision_identity}-{projection.projection_id}-line"
        text = (
            f"Allocated {amount} at retained rank {priority.rank}; "
            f"binding={','.join(binding)}; context={context.overall_state.value}; "
            f"venue={projection.venue.value}."
        )
        explanation_payload = (
            explanation_id,
            decision_identity,
            projection.projection_id,
            True,
            binding,
            (),
            text,
            "NONE",
        )
        explanations.append(
            AllocationConstraintExplanation(
                *explanation_payload[:-1], content_digest(explanation_payload), "NONE"
            )
        )
        line_values = (
            line_id,
            decision_identity,
            projection.projection_id,
            projection.canonical_hash,
            projection.venue,
            projection.strategy_or_method_id,
            projection.instrument_id,
            projection.direction,
            priority.rank,
            priority.score,
            amount,
            downside,
            costs,
            projection.estimated_lockup_days,
            source_maximum,
            binding,
            explanation_id,
            "NONE",
            False,
            False,
            "NONE",
        )
        line = CrossVenueAllocationLine(
            *line_values[:-4], content_digest(line_values), *line_values[-4:]
        )
        lines.append(line)
        assert boundary is not None and boundary.account_id is not None
        assert boundary.capability_id is not None
        expiry = min(
            now + timedelta(hours=24),
            projection.valid_until or now + timedelta(hours=24),
        )
        proposal_values = (
            f"{line_id}-paper-trial-proposal",
            decision_identity,
            line_id,
            projection.projection_id,
            projection.canonical_hash,
            projection.venue,
            projection.instrument_id,
            projection.direction,
            amount,
            downside,
            boundary.account_id,
            boundary.capability_id,
            "ELIGIBLE_FOR_EXPLICIT_PAPER_WORKFLOW_ONLY",
            True,
            None,
            expiry,
            "NONE",
            False,
            False,
            False,
            "NONE",
        )
        proposals.append(
            PaperTrialProposal(
                *proposal_values[:-5], content_digest(proposal_values), *proposal_values[-5:]
            )
        )
        total_allocated += amount
        total_downside += downside
        total_costs += costs
        venue_totals[projection.venue] += amount
        strategy_totals[projection.strategy_or_method_id] = (
            strategy_totals.get(projection.strategy_or_method_id, Decimal(0)) + amount
        )
        instrument_totals[projection.instrument_id] = (
            instrument_totals.get(projection.instrument_id, Decimal(0)) + amount
        )
        if is_long_lockup:
            locked += amount

    considered = tuple(opportunity_set.projection_ids)
    ranked_considered = {item.projection_id for item in priorities}
    for projection_id in considered:
        if projection_id in ranked_considered:
            continue
        excluded_ids.append(projection_id)
        shared_decision = decision_by_projection.get(projection_id)
        reasons = (
            ("SHARED_OPPORTUNITY_DECISION_MISSING",)
            if shared_decision is None
            else tuple(shared_decision.reasons)
        )
        explanation_id = f"{decision_identity}-{projection_id}-explanation"
        unranked_payload = (
            explanation_id,
            decision_identity,
            projection_id,
            False,
            (),
            reasons,
            f"Zero allocation: {','.join(reasons)}.",
            "NONE",
        )
        explanations.append(
            AllocationConstraintExplanation(
                *unranked_payload[:-1], content_digest(unranked_payload), "NONE"
            )
        )

    retained_cash = _money(policy.total_hypothetical_capital - total_allocated)
    context_effects = (
        f"CONTEXT_STATE:{context.overall_state.value}",
        f"RETAINED_CASH_FRACTION:{cash_fraction}",
        "CONTEXT_CHANGES_LIMITS_NOT_SOURCE_ELIGIBILITY_OR_PRIORITY",
        "CONTEXT_IS_EXPLANATORY_NOT_PREDICTIVE",
    )
    source_manifest = content_digest(
        (
            opportunity_set.canonical_hash,
            enriched_set.canonical_hash,
            context.canonical_hash,
            tuple((item.projection_id, item.canonical_hash) for item in projections),
            tuple(
                (item.venue.value, item.capability_id, item.account_id, item.available)
                for item in paper_readiness
            ),
        )
    )
    decision_values = (
        decision_identity,
        1,
        policy.policy_id,
        policy.canonical_hash,
        opportunity_set.opportunity_set_id,
        opportunity_set.canonical_hash,
        enriched_set.enriched_set_id,
        enriched_set.canonical_hash,
        context.snapshot_id,
        context.canonical_hash,
        context.overall_state,
        now,
        considered,
        tuple(eligible_ids),
        tuple(sorted(set(excluded_ids))),
        tuple(item.line_id for item in lines),
        tuple(item.explanation_id for item in explanations),
        tuple(item.proposal_id for item in proposals),
        policy.total_hypothetical_capital,
        retained_cash,
        tuple((venue.value, _money(venue_totals[venue])) for venue in SharedVenue),
        _money(total_downside),
        _money(total_costs),
        _money(locked),
        tuple(
            sorted(
                (
                    *((f"STRATEGY:{key}", _money(value)) for key, value in strategy_totals.items()),
                    *(
                        (f"INSTRUMENT:{key}", _money(value))
                        for key, value in instrument_totals.items()
                    ),
                )
            )
        ),
        context_effects,
        (
            CROSS_VENUE_ALLOCATION_WARNING,
            "PAPER_TRIAL_PROPOSALS_REQUIRE_SEPARATE_EXPLICIT_OPERATOR_AUTHORIZATION",
            "NO_PROVIDER_OR_BROKER_REQUEST",
            "LIVE_AUTHORITY_NONE",
        ),
        source_manifest,
        "NONE",
        "NONE",
        False,
        False,
        False,
        "NONE",
        "NONE",
    )
    decision = CrossVenueAllocationDecision(
        *decision_values[:-7], content_digest(decision_values), *decision_values[-7:]
    )
    return decision, tuple(lines), tuple(explanations), tuple(proposals)
