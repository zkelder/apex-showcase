"""Pure shared PAPER performance attribution and strategy-health rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from apex.domain.values import content_digest

SHARED_PERFORMANCE_ATTRIBUTION_V1 = "SHARED_PERFORMANCE_ATTRIBUTION_V1"
STRATEGY_HEALTH_POLICY_V1 = "STRATEGY_HEALTH_POLICY_V1"
PAPER_PERFORMANCE_WARNING = (
    "PAPER PERFORMANCE IS SIMULATED OR EXTERNAL-PAPER EVIDENCE — NOT REAL EARNINGS"
)
_SCALE = Decimal("0.00000001")


class PerformanceAttributionError(ValueError):
    """Raised when attribution evidence is invalid, mixed, or conflicting."""


class PaperVenue(StrEnum):
    CRYPTO_INTERNAL_PAPER = "CRYPTO_INTERNAL_PAPER"
    KALSHI_INTERNAL_PAPER = "KALSHI_INTERNAL_PAPER"
    ALPACA_EXTERNAL_PAPER = "ALPACA_EXTERNAL_PAPER"


class PaperEvidenceClass(StrEnum):
    GENUINE_PAPER = "GENUINE_PAPER"
    FIXTURE_PAPER = "FIXTURE_PAPER"


class AttributionCategory(StrEnum):
    SIGNAL_OR_SELECTION = "SIGNAL_OR_SELECTION"
    MARKET_MOVEMENT = "MARKET_MOVEMENT"
    FEES = "FEES"
    SLIPPAGE = "SLIPPAGE"
    LIQUIDITY = "LIQUIDITY"
    PARTIAL_OR_NO_FILL = "PARTIAL_OR_NO_FILL"
    TIMING = "TIMING"
    CAPITAL_LOCKUP = "CAPITAL_LOCKUP"
    DATA_QUALITY = "DATA_QUALITY"
    RECONCILIATION = "RECONCILIATION"
    SETTLEMENT = "SETTLEMENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class StrategyHealthState(StrEnum):
    NO_OPERATIONAL_EVIDENCE = "NO_OPERATIONAL_EVIDENCE"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    RECONCILIATION_BLOCKED = "RECONCILIATION_BLOCKED"
    DATA_QUALITY_BLOCKED = "DATA_QUALITY_BLOCKED"


def _id(value: str, reason: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 300:
        raise PerformanceAttributionError(reason)
    return normalized


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PerformanceAttributionError(reason)
    return value.astimezone(UTC)


def _decimal(value: Decimal | None, reason: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PerformanceAttributionError(reason)
    return value.quantize(_SCALE, rounding=ROUND_HALF_EVEN)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator <= 0:
        return None
    return (numerator / denominator).quantize(_SCALE, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class StrategyHealthPolicy:
    policy_id: str
    minimum_trials: int
    minimum_fills: int
    minimum_settled_outcomes: int
    healthy_minimum_fill_rate: Decimal
    watch_minimum_fill_rate: Decimal
    maximum_fee_variance: Decimal
    maximum_slippage_variance: Decimal
    maximum_drawdown_for_watch: Decimal
    canonical_hash: str
    authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def v1(cls) -> StrategyHealthPolicy:
        values = (
            STRATEGY_HEALTH_POLICY_V1,
            3,
            2,
            2,
            Decimal("0.75"),
            Decimal("0.40"),
            Decimal("0.01"),
            Decimal("0.01"),
            Decimal("0.20"),
            "NONE",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class AttributionSourceSnapshot:
    """Venue-neutral read model; adapters retain venue semantics in explicit fields."""

    source_id: str
    source_hash: str
    venue: PaperVenue
    evidence_class: PaperEvidenceClass
    strategy_or_method_id: str
    strategy_or_method_version: str
    instrument_id: str
    side: str | None
    source_opportunity_id: str | None
    shared_projection_id: str | None
    allocation_decision_id: str | None
    paper_trial_proposal_id: str | None
    authorization_id: str | None
    order_id: str | None
    reservation_id: str | None
    reservation_original_amount: Decimal | None
    reservation_current_amount: Decimal | None
    reservation_consumed_amount: Decimal | None
    fill_ids: tuple[str, ...]
    position_id: str | None
    settlement_id: str | None
    resolution_fact_id: str | None
    context_at_consideration_id: str | None
    context_at_execution_id: str | None
    consideration_context: str | None
    execution_context: str | None
    intended_quantity: Decimal | None
    filled_quantity: Decimal | None
    intended_notional: Decimal | None
    filled_notional: Decimal | None
    expected_gross_return: Decimal | None
    realized_gross_return: Decimal | None
    expected_net_return: Decimal | None
    realized_net_return: Decimal | None
    estimated_fees: Decimal | None
    realized_fees: Decimal | None
    estimated_slippage: Decimal | None
    realized_slippage: Decimal | None
    modeled_maximum_loss: Decimal | None
    realized_loss: Decimal | None
    open_exposure: Decimal | None
    unrealized_paper_pnl: Decimal | None
    expected_lockup_seconds: int | None
    actual_lockup_seconds: int | None
    considered_at: datetime | None
    first_fill_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime | None
    position_state: str | None
    reconciliation_status: str | None
    reconciliation_issues: tuple[str, ...]
    opportunity_expiration_behavior: str | None
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AttributionComponent:
    component_id: str
    attribution_id: str
    category: AttributionCategory
    value: Decimal | None
    units: str
    explanation: str
    evidence_ids: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class SharedPerformanceAttribution:
    attribution_id: str
    version: int
    policy_id: str
    source_id: str
    source_hash: str
    venue: PaperVenue
    paper_state_boundary: str
    evidence_class: PaperEvidenceClass
    strategy_or_method_id: str
    strategy_or_method_version: str
    instrument_id: str
    side: str | None
    source_opportunity_id: str | None
    shared_projection_id: str | None
    allocation_decision_id: str | None
    paper_trial_proposal_id: str | None
    authorization_id: str | None
    order_id: str | None
    reservation_id: str | None
    reservation_original_amount: Decimal | None
    reservation_current_amount: Decimal | None
    reservation_consumed_amount: Decimal | None
    fill_ids: tuple[str, ...]
    position_id: str | None
    settlement_id: str | None
    resolution_fact_id: str | None
    context_at_consideration_id: str | None
    context_at_execution_id: str | None
    consideration_context: str | None
    execution_context: str | None
    expected_gross_return: Decimal | None
    realized_gross_return: Decimal | None
    expected_net_return: Decimal | None
    realized_net_return: Decimal | None
    estimated_fees: Decimal | None
    realized_fees: Decimal | None
    estimated_slippage: Decimal | None
    realized_slippage: Decimal | None
    intended_quantity: Decimal | None
    filled_quantity: Decimal | None
    fill_rate: Decimal | None
    intended_notional: Decimal | None
    filled_notional: Decimal | None
    modeled_maximum_loss: Decimal | None
    realized_loss: Decimal | None
    open_exposure: Decimal | None
    unrealized_paper_pnl: Decimal | None
    time_to_first_fill_seconds: int | None
    time_to_completion_seconds: int | None
    expected_lockup_seconds: int | None
    actual_lockup_seconds: int | None
    opportunity_expiration_behavior: str | None
    position_state: str | None
    reconciliation_status: str | None
    reconciliation_issues: tuple[str, ...]
    missing_stages: tuple[str, ...]
    component_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    materialized_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    financial_mutation_capability: str = "NONE"
    paper_authorization_created: bool = False
    live_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class StrategyHealthAssessment:
    health_id: str
    policy_id: str
    strategy_or_method_id: str
    strategy_or_method_version: str
    evidence_class: PaperEvidenceClass
    window_start: datetime | None
    window_end: datetime
    paper_trial_count: int
    fill_count: int
    settled_outcome_count: int
    expected_net_return: Decimal | None
    realized_net_return: Decimal | None
    fee_variance: Decimal | None
    slippage_variance: Decimal | None
    fill_rate: Decimal | None
    reconciliation_quality: Decimal | None
    maximum_drawdown: Decimal | None
    context_classifications: tuple[str, ...]
    warnings: tuple[str, ...]
    failed_thresholds: tuple[str, ...]
    state: StrategyHealthState
    operator_recommendation: str
    attribution_ids: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    automatic_action: bool = False
    live_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class PaperPerformanceSummary:
    summary_id: str
    scope_type: str
    scope_id: str
    paper_state_boundary: str
    evidence_class: PaperEvidenceClass
    as_of: datetime
    attribution_count: int
    allocated_hypothetical_capital: Decimal
    filled_capital: Decimal
    open_exposure: Decimal
    realized_paper_pnl: Decimal
    unrealized_paper_pnl: Decimal | None
    fees: Decimal
    slippage: Decimal
    reconciliation_issue_count: int
    settled_outcomes: int
    unresolved_outcomes: int
    limitations: tuple[str, ...]
    attribution_ids: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    live_authority: str = "NONE"


def materialize_attribution(
    *, attribution_id: str, source: AttributionSourceSnapshot, materialized_at: datetime
) -> tuple[SharedPerformanceAttribution, tuple[AttributionComponent, ...]]:
    identity = _id(attribution_id, "ATTRIBUTION_ID_INVALID")
    when = _time(materialized_at, "ATTRIBUTION_TIME_INVALID")
    if source.evidence_class not in PaperEvidenceClass:
        raise PerformanceAttributionError("ATTRIBUTION_EVIDENCE_CLASS_INVALID")
    decimals = {
        name: _decimal(getattr(source, name), f"ATTRIBUTION_{name.upper()}_INVALID")
        for name in (
            "intended_quantity",
            "filled_quantity",
            "intended_notional",
            "filled_notional",
            "reservation_original_amount",
            "reservation_current_amount",
            "reservation_consumed_amount",
            "expected_gross_return",
            "realized_gross_return",
            "expected_net_return",
            "realized_net_return",
            "estimated_fees",
            "realized_fees",
            "estimated_slippage",
            "realized_slippage",
            "modeled_maximum_loss",
            "realized_loss",
            "open_exposure",
            "unrealized_paper_pnl",
        )
    }
    fill_rate = (
        None
        if decimals["filled_quantity"] is None or decimals["intended_quantity"] is None
        else _ratio(decimals["filled_quantity"], decimals["intended_quantity"])
    )
    first_fill_seconds = (
        None
        if source.considered_at is None or source.first_fill_at is None
        else max(
            0,
            int(
                (
                    _time(source.first_fill_at, "ATTRIBUTION_FILL_TIME_INVALID")
                    - _time(source.considered_at, "ATTRIBUTION_CONSIDERATION_TIME_INVALID")
                ).total_seconds()
            ),
        )
    )
    completion_seconds = (
        None
        if source.considered_at is None or source.completed_at is None
        else max(
            0,
            int(
                (
                    _time(source.completed_at, "ATTRIBUTION_COMPLETION_TIME_INVALID")
                    - _time(source.considered_at, "ATTRIBUTION_CONSIDERATION_TIME_INVALID")
                ).total_seconds()
            ),
        )
    )
    stage_values = {
        "SOURCE_OPPORTUNITY": source.source_opportunity_id,
        "SHARED_OPPORTUNITY": source.shared_projection_id,
        "ALLOCATION_DECISION": source.allocation_decision_id,
        "PAPER_TRIAL_PROPOSAL": source.paper_trial_proposal_id,
        "AUTHORIZATION": source.authorization_id,
        "ORDER": source.order_id,
        "RESERVATION": source.reservation_id,
        "FILL": source.fill_ids or None,
        "POSITION": source.position_id,
        "SETTLEMENT": source.settlement_id,
        "CONTEXT_AT_CONSIDERATION": source.context_at_consideration_id,
        "CONTEXT_AT_EXECUTION": source.context_at_execution_id,
        "RECONCILIATION": source.reconciliation_status,
    }
    missing = tuple(name for name, value in stage_values.items() if not value)
    components: list[AttributionComponent] = []

    def add(
        category: AttributionCategory,
        value: Decimal | None,
        units: str,
        text: str,
        ids: tuple[str, ...] = (),
    ) -> None:
        values = (
            f"{identity}-{len(components) + 1:02d}-{category.value.lower()}",
            identity,
            category,
            value,
            units,
            text,
            ids,
            "NONE",
        )
        components.append(AttributionComponent(*values[:-1], content_digest(values), "NONE"))

    if source.reconciliation_issues or source.reconciliation_status in {
        "BLOCKED",
        "MATERIAL_DISCREPANCY",
        "SUBMISSION_AMBIGUOUS",
        "MANUAL_REVIEW_REQUIRED",
    }:
        add(
            AttributionCategory.RECONCILIATION,
            None,
            "STATUS",
            "Reconciliation requires attention: "
            f"{source.reconciliation_status or 'UNAVAILABLE'}; "
            f"issues={','.join(source.reconciliation_issues) or 'UNSPECIFIED'}.",
        )
    if fill_rate is not None and fill_rate < Decimal(1):
        add(
            AttributionCategory.PARTIAL_OR_NO_FILL,
            Decimal(1) - fill_rate,
            "FRACTION",
            f"Only {fill_rate} of intended quantity was filled.",
            source.fill_ids,
        )
    elif source.order_id is not None and not source.fill_ids:
        add(
            AttributionCategory.PARTIAL_OR_NO_FILL,
            Decimal(1),
            "FRACTION",
            "The retained order has no fill evidence.",
        )
    if decimals["estimated_fees"] is not None and decimals["realized_fees"] is not None:
        variance = decimals["realized_fees"] - decimals["estimated_fees"]
        add(
            AttributionCategory.FEES,
            variance,
            "MONEY",
            f"Realized minus estimated fees equals {variance}.",
        )
    if decimals["estimated_slippage"] is not None and decimals["realized_slippage"] is not None:
        variance = decimals["realized_slippage"] - decimals["estimated_slippage"]
        add(
            AttributionCategory.SLIPPAGE,
            variance,
            "MONEY",
            f"Realized minus estimated slippage equals {variance}.",
        )
    if (
        decimals["expected_gross_return"] is not None
        and decimals["realized_gross_return"] is not None
    ):
        variance = decimals["realized_gross_return"] - decimals["expected_gross_return"]
        add(
            AttributionCategory.MARKET_MOVEMENT,
            variance,
            "RETURN",
            f"Realized minus expected gross return equals {variance}; "
            "this is descriptive, not causal.",
        )
    if source.actual_lockup_seconds is not None and source.expected_lockup_seconds is not None:
        delta = Decimal(source.actual_lockup_seconds - source.expected_lockup_seconds)
        add(
            AttributionCategory.CAPITAL_LOCKUP,
            delta,
            "SECONDS",
            f"Actual minus expected capital lockup equals {delta} seconds.",
        )
    if first_fill_seconds is not None or completion_seconds is not None:
        add(
            AttributionCategory.TIMING,
            None if completion_seconds is None else Decimal(completion_seconds),
            "SECONDS",
            f"First fill seconds={first_fill_seconds}; completion seconds={completion_seconds}.",
        )
    if source.settlement_id is not None:
        add(
            AttributionCategory.SETTLEMENT,
            decimals["realized_net_return"],
            "RETURN",
            f"Settlement {source.settlement_id} is retained with exact resolution lineage "
            f"{source.resolution_fact_id or 'UNAVAILABLE'}.",
            (source.settlement_id,),
        )
    if source.context_at_consideration_id is None or source.source_opportunity_id is None:
        add(
            AttributionCategory.DATA_QUALITY,
            None,
            "STATUS",
            "Required Opportunity or consideration-context lineage is unavailable; "
            "no values were invented.",
        )
    if not source.fill_ids or decimals["realized_net_return"] is None:
        add(
            AttributionCategory.INSUFFICIENT_EVIDENCE,
            None,
            "STATUS",
            "Realized PAPER performance is unavailable until compatible fill and "
            "realized-outcome evidence exists.",
        )
    if not components:
        add(
            AttributionCategory.SIGNAL_OR_SELECTION,
            Decimal(0),
            "RETURN",
            "Expected and realized evidence matched within retained precision; "
            "no causal claim is made.",
        )

    boundary = (
        "EXTERNAL_PAPER_EVIDENCE"
        if source.venue is PaperVenue.ALPACA_EXTERNAL_PAPER
        else "APEX_INTERNAL_PAPER_STATE"
    )
    values = (
        identity,
        1,
        SHARED_PERFORMANCE_ATTRIBUTION_V1,
        source.source_id,
        source.source_hash,
        source.venue,
        boundary,
        source.evidence_class,
        source.strategy_or_method_id,
        source.strategy_or_method_version,
        source.instrument_id,
        source.side,
        source.source_opportunity_id,
        source.shared_projection_id,
        source.allocation_decision_id,
        source.paper_trial_proposal_id,
        source.authorization_id,
        source.order_id,
        source.reservation_id,
        decimals["reservation_original_amount"],
        decimals["reservation_current_amount"],
        decimals["reservation_consumed_amount"],
        source.fill_ids,
        source.position_id,
        source.settlement_id,
        source.resolution_fact_id,
        source.context_at_consideration_id,
        source.context_at_execution_id,
        source.consideration_context,
        source.execution_context,
        decimals["expected_gross_return"],
        decimals["realized_gross_return"],
        decimals["expected_net_return"],
        decimals["realized_net_return"],
        decimals["estimated_fees"],
        decimals["realized_fees"],
        decimals["estimated_slippage"],
        decimals["realized_slippage"],
        decimals["intended_quantity"],
        decimals["filled_quantity"],
        fill_rate,
        decimals["intended_notional"],
        decimals["filled_notional"],
        decimals["modeled_maximum_loss"],
        decimals["realized_loss"],
        decimals["open_exposure"],
        decimals["unrealized_paper_pnl"],
        first_fill_seconds,
        completion_seconds,
        source.expected_lockup_seconds,
        source.actual_lockup_seconds,
        source.opportunity_expiration_behavior,
        source.position_state,
        source.reconciliation_status,
        source.reconciliation_issues,
        missing,
        tuple(item.component_id for item in components),
        tuple(
            sorted(
                set(
                    (
                        *source.limitations,
                        PAPER_PERFORMANCE_WARNING,
                        "CONTEXT_ASSOCIATION_IS_DESCRIPTIVE_NOT_CAUSAL",
                    )
                )
            )
        ),
        when,
        "NONE",
        "NONE",
        False,
        "NONE",
    )
    record = SharedPerformanceAttribution(*values[:-4], content_digest(values), *values[-4:])
    return record, tuple(components)


def assess_strategy_health(
    *, health_id: str, attributions: tuple[SharedPerformanceAttribution, ...], as_of: datetime
) -> StrategyHealthAssessment:
    if not attributions:
        raise PerformanceAttributionError("STRATEGY_HEALTH_ATTRIBUTIONS_REQUIRED")
    first = attributions[0]
    if any(
        (item.strategy_or_method_id, item.strategy_or_method_version, item.evidence_class)
        != (first.strategy_or_method_id, first.strategy_or_method_version, first.evidence_class)
        for item in attributions
    ):
        raise PerformanceAttributionError("STRATEGY_HEALTH_EVIDENCE_MIXED")
    policy = StrategyHealthPolicy.v1()
    ordered = tuple(
        sorted(attributions, key=lambda item: (item.materialized_at, item.attribution_id))
    )
    fill_count = sum(bool(item.fill_ids) for item in ordered)
    settled = sum(
        item.settlement_id is not None or item.realized_net_return is not None for item in ordered
    )
    total_intended = sum((item.intended_quantity or Decimal(0) for item in ordered), Decimal(0))
    total_filled = sum((item.filled_quantity or Decimal(0) for item in ordered), Decimal(0))
    fill_rate = _ratio(total_filled, total_intended)
    expected = (
        sum(
            (item.expected_net_return for item in ordered if item.expected_net_return is not None),
            Decimal(0),
        )
        if any(item.expected_net_return is not None for item in ordered)
        else None
    )
    realized = (
        sum(
            (item.realized_net_return for item in ordered if item.realized_net_return is not None),
            Decimal(0),
        )
        if any(item.realized_net_return is not None for item in ordered)
        else None
    )
    fee_pairs = tuple(
        (item.estimated_fees, item.realized_fees)
        for item in ordered
        if item.estimated_fees is not None and item.realized_fees is not None
    )
    slippage_pairs = tuple(
        (item.estimated_slippage, item.realized_slippage)
        for item in ordered
        if item.estimated_slippage is not None and item.realized_slippage is not None
    )
    fee_variance = (
        None
        if not fee_pairs
        else _decimal(
            sum((realized - estimated for estimated, realized in fee_pairs), Decimal(0)),
            "HEALTH_FEE_VARIANCE_INVALID",
        )
    )
    slippage_variance = (
        None
        if not slippage_pairs
        else _decimal(
            sum((realized - estimated for estimated, realized in slippage_pairs), Decimal(0)),
            "HEALTH_SLIPPAGE_VARIANCE_INVALID",
        )
    )
    cumulative = Decimal(0)
    peak = Decimal(0)
    maximum_drawdown: Decimal | None = None
    for item in ordered:
        if item.realized_net_return is None:
            continue
        cumulative += item.realized_net_return
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        maximum_drawdown = drawdown if maximum_drawdown is None else max(maximum_drawdown, drawdown)
    reconciliation_count = sum(
        not item.reconciliation_issues and item.reconciliation_status in {"RECONCILED", "MATCHED"}
        for item in ordered
    )
    reconciliation_quality = _ratio(Decimal(reconciliation_count), Decimal(len(ordered)))
    contexts = tuple(
        sorted(
            {
                value
                for item in ordered
                for value in (item.consideration_context, item.execution_context)
                if value is not None
            }
        )
    )
    failures: list[str] = []
    warnings: list[str] = [PAPER_PERFORMANCE_WARNING, "HEALTH_NEVER_CHANGES_STRATEGY_AUTHORITY"]
    reconciliation_blocked = any(
        item.reconciliation_issues
        or item.reconciliation_status
        in {"BLOCKED", "MATERIAL_DISCREPANCY", "SUBMISSION_AMBIGUOUS", "MANUAL_REVIEW_REQUIRED"}
        for item in ordered
    )
    data_blocked = any(
        "SOURCE_OPPORTUNITY" in item.missing_stages
        or "CONTEXT_AT_CONSIDERATION" in item.missing_stages
        for item in ordered
    )
    if reconciliation_blocked:
        state = StrategyHealthState.RECONCILIATION_BLOCKED
        failures.append("RECONCILIATION_NOT_CLEAN")
    elif data_blocked and fill_count > 0:
        state = StrategyHealthState.DATA_QUALITY_BLOCKED
        failures.append("SOURCE_OR_CONTEXT_LINEAGE_INCOMPLETE")
    elif not ordered or all(item.order_id is None and not item.fill_ids for item in ordered):
        state = StrategyHealthState.NO_OPERATIONAL_EVIDENCE
    elif (
        len(ordered) < policy.minimum_trials
        or fill_count < policy.minimum_fills
        or settled < policy.minimum_settled_outcomes
    ):
        state = StrategyHealthState.INSUFFICIENT_SAMPLE
        failures.append("MINIMUM_SAMPLE_NOT_MET")
    else:
        if fill_rate is None or fill_rate < policy.watch_minimum_fill_rate:
            failures.append("FILL_RATE_DEGRADED")
        if fee_variance is not None and fee_variance > policy.maximum_fee_variance:
            failures.append("FEE_VARIANCE_EXCEEDED")
        if slippage_variance is not None and slippage_variance > policy.maximum_slippage_variance:
            failures.append("SLIPPAGE_VARIANCE_EXCEEDED")
        if realized is not None and realized < 0:
            failures.append("REALIZED_NET_RETURN_NEGATIVE")
        if maximum_drawdown is not None and maximum_drawdown > policy.maximum_drawdown_for_watch:
            failures.append("MAXIMUM_DRAWDOWN_EXCEEDED")
        state = (
            StrategyHealthState.DEGRADED
            if len(failures) >= 2
            else StrategyHealthState.WATCH
            if failures or fill_rate is None or fill_rate < policy.healthy_minimum_fill_rate
            else StrategyHealthState.HEALTHY
        )
    recommendation = {
        StrategyHealthState.NO_OPERATIONAL_EVIDENCE: (
            "RETAIN CURRENT AUTHORITY; AWAIT OPERATIONAL PAPER EVIDENCE"
        ),
        StrategyHealthState.INSUFFICIENT_SAMPLE: (
            "RETAIN CURRENT AUTHORITY; COLLECT MORE COMPATIBLE PAPER EVIDENCE"
        ),
        StrategyHealthState.HEALTHY: "RETAIN CURRENT AUTHORITY; CONTINUE OPERATOR REVIEW",
        StrategyHealthState.WATCH: (
            "REVIEW ATTRIBUTION COMPONENTS; DO NOT CHANGE AUTHORITY AUTOMATICALLY"
        ),
        StrategyHealthState.DEGRADED: "OPERATOR REVIEW REQUIRED; NO AUTOMATIC SUSPENSION",
        StrategyHealthState.RECONCILIATION_BLOCKED: (
            "RESOLVE RECONCILIATION BEFORE INTERPRETING PERFORMANCE"
        ),
        StrategyHealthState.DATA_QUALITY_BLOCKED: (
            "RESTORE EXACT LINEAGE BEFORE INTERPRETING PERFORMANCE"
        ),
    }[state]
    values = (
        _id(health_id, "STRATEGY_HEALTH_ID_INVALID"),
        policy.policy_id,
        first.strategy_or_method_id,
        first.strategy_or_method_version,
        first.evidence_class,
        ordered[0].materialized_at,
        _time(as_of, "STRATEGY_HEALTH_TIME_INVALID"),
        len(ordered),
        fill_count,
        settled,
        expected,
        realized,
        fee_variance,
        slippage_variance,
        fill_rate,
        reconciliation_quality,
        maximum_drawdown,
        contexts,
        tuple(warnings),
        tuple(sorted(failures)),
        state,
        recommendation,
        tuple(item.attribution_id for item in ordered),
        "NONE",
        False,
        "NONE",
    )
    return StrategyHealthAssessment(*values[:-3], content_digest(values), *values[-3:])


def summarize_performance(
    *,
    summary_id: str,
    scope_type: str,
    scope_id: str,
    attributions: tuple[SharedPerformanceAttribution, ...],
    evidence_class: PaperEvidenceClass,
    as_of: datetime,
) -> PaperPerformanceSummary:
    if any(item.evidence_class is not evidence_class for item in attributions):
        raise PerformanceAttributionError("PERFORMANCE_SUMMARY_EVIDENCE_MIXED")
    boundaries = {item.paper_state_boundary for item in attributions}
    boundary = (
        next(iter(boundaries))
        if len(boundaries) == 1
        else "SEPARATE_INTERNAL_AND_EXTERNAL_PAPER_SUBTOTALS"
    )
    realized_values = tuple(
        item.realized_net_return for item in attributions if item.realized_net_return is not None
    )
    unrealized_values = tuple(
        item.unrealized_paper_pnl for item in attributions if item.unrealized_paper_pnl is not None
    )
    limitations = {PAPER_PERFORMANCE_WARNING}
    if not attributions:
        limitations.add("NO_COMPATIBLE_PAPER_ATTRIBUTION_EVIDENCE")
    if not realized_values:
        limitations.add("NO_REALIZED_PAPER_PERFORMANCE")
    values = (
        _id(summary_id, "PERFORMANCE_SUMMARY_ID_INVALID"),
        _id(scope_type, "PERFORMANCE_SUMMARY_SCOPE_INVALID"),
        _id(scope_id, "PERFORMANCE_SUMMARY_SCOPE_ID_INVALID"),
        boundary,
        evidence_class,
        _time(as_of, "PERFORMANCE_SUMMARY_TIME_INVALID"),
        len(attributions),
        sum((item.intended_notional or Decimal(0) for item in attributions), Decimal(0)),
        sum((item.filled_notional or Decimal(0) for item in attributions), Decimal(0)),
        sum((item.open_exposure or Decimal(0) for item in attributions), Decimal(0)),
        sum(realized_values, Decimal(0)),
        None if not unrealized_values else sum(unrealized_values, Decimal(0)),
        sum((item.realized_fees or Decimal(0) for item in attributions), Decimal(0)),
        sum((item.realized_slippage or Decimal(0) for item in attributions), Decimal(0)),
        sum(bool(item.reconciliation_issues) for item in attributions),
        sum(item.settlement_id is not None for item in attributions),
        sum(
            item.settlement_id is None and (item.order_id is not None or bool(item.fill_ids))
            for item in attributions
        ),
        tuple(sorted(limitations)),
        tuple(item.attribution_id for item in attributions),
        "NONE",
        "NONE",
    )
    return PaperPerformanceSummary(*values[:-2], content_digest(values), *values[-2:])
