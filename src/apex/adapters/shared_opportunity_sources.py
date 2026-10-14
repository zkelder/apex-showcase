"""Local-only venue adapters for the shared Opportunity projection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from typing import cast

import sqlalchemy as sa
from sqlalchemy.engine import Engine, RowMapping

from apex.adapters.crypto_market_pulse_postgres import (
    PostgresCryptoMarketPulseRepository,
)
from apex.adapters.crypto_stateful_research_postgres import (
    PostgresCryptoStatefulResearchRepository,
)
from apex.adapters.options_structures_postgres import PostgresOptionsStructuresRepository
from apex.adapters.postgres import (
    binary_complement_evaluation,
    event_market_relationship,
    market_orderbook_level,
    market_orderbook_snapshot,
    normalized_event_snapshot,
    normalized_market_snapshot,
    series_fee_snapshot,
    threshold_bundle_evaluation,
    threshold_ladder_scan,
    threshold_market_relationship,
    threshold_matched_slice,
)
from apex.adapters.research_operations_postgres import PostgresResearchOperationsRepository
from apex.adapters.shared_opportunity_postgres import PostgresSharedOpportunityRepository
from apex.domain.crypto_market_pulse import PULSE_FRESHNESS_HOURS
from apex.domain.crypto_stateful_research import (
    CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
)
from apex.domain.crypto_universe import (
    CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
    CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
    CryptoDiscoveryRun,
    OpportunityStatus,
)
from apex.domain.options_structures import (
    OptionStructureCandidate,
    StructureQualityLabel,
    structure_initial_cash_flow,
)
from apex.domain.public_complement import SeriesFeeSnapshot
from apex.domain.public_event import EventEvidenceState, EventRelationshipState
from apex.domain.public_exhaustive import (
    KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1,
    ExhaustiveEventSetEvaluation,
    ExhaustiveEventSetInput,
    ExhaustiveEventSetLegInput,
    evaluate_exhaustive_event_set,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.public_threshold import KALSHI_RELATED_MARKET_CONSISTENCY_V1
from apex.domain.shared_opportunity import (
    EligibilityState,
    SharedOpportunityError,
    SharedOpportunityProjection,
    SharedVenue,
)
from apex.domain.values import ContentHash

KALSHI_SHARED_FRESHNESS_SECONDS = 300
# Securities Research -> Shared Opportunity Recurring Bridge v1.
SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1 = "SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1"
# Defined-risk options structures are captured once per XNYS trading session
# (``domain.options_research_operations.OptionsCaptureWindowPolicy``), never
# every ~5 minutes like this producer itself is re-invoked. A flat, generous,
# multi-day window -- mirroring ``KALSHI_SHARED_FRESHNESS_SECONDS``'s own
# role: a venue-specific freshness constant, not sourced from the source
# record itself, chosen to match this venue's own genuine evidence cadence --
# absorbs a normal weekend/holiday gap between one session's capture and the
# next without that session's still-representative economics prematurely
# reading STALE/EXPIRED merely because a weekend elapsed. 4 days comfortably
# spans a 3-day weekend plus one holiday Monday.
SECURITIES_STRUCTURE_SHARED_FRESHNESS_SECONDS = 4 * 24 * 3600
# Only genuinely tradeable-quality evidence becomes a candidate opportunity --
# reuses ``domain.options_structures.build_structure_quality``'s own existing
# quality judgment (never a new selection heuristic invented at this layer).
# UNUSABLE (stale/one-sided/zero-size quote data) is definitionally not a
# researchable opportunity. MARGINAL (wide relative spread or high fee
# burden -- explicitly quality-flagged) is deliberately excluded too: by
# ``build_structure_quality``'s own construction, a MARGINAL/UNUSABLE
# candidate's ``quality.reasons`` is the *only* place either weakness is
# recorded, and neither is a typed, honest field this shared layer's own
# LIQUIDITY/COST ranking components already model -- keeping the first
# bridge to genuinely research-grade evidence only avoids silently
# discarding that signal in this narrow producer, not looser or stricter
# than what the structure's own domain module already decided.
_SECURITIES_ACCEPTABLE_QUALITY_LABELS = frozenset(
    {StructureQualityLabel.HIGH_QUALITY, StructureQualityLabel.USABLE}
)
_SHARED_DECIMAL_QUANTUM = Decimal("0.00000001")


def _age_seconds(as_of: datetime, observed_at: datetime) -> int:
    if as_of.tzinfo is None or observed_at.tzinfo is None:
        raise SharedOpportunityError("SHARED_SOURCE_TIME_INVALID")
    return max(
        0,
        int((as_of.astimezone(UTC) - observed_at.astimezone(UTC)).total_seconds()),
    )


def _shared_decimal(value: Decimal) -> Decimal:
    """Bound retained provider scale for the versioned shared comparison."""

    if value == 0:
        return Decimal(0)
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent >= -8:
        return value
    return value.quantize(_SHARED_DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN)


def _canonical_db_decimal(value: Decimal) -> Decimal:
    """Remove database-only trailing scale without changing the retained value."""

    if value == 0:
        return Decimal(0)
    return value.normalize()


def _round_up(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_CEILING) * increment


def _securities_structure_projection(
    candidate: OptionStructureCandidate, *, as_of: datetime
) -> SharedOpportunityProjection:
    """Pure mapping from one retained, quality-accepted
    ``OptionStructureCandidate`` to one ``SharedOpportunityProjection``.
    Every venue-specific diagnostic is stuffed into ``source_details`` as a
    string, exactly as CRYPTO/KALSHI already do -- never a new typed field on
    the shared dataclass. ``source_details`` additionally doubles as the one
    already-retained record ``application.counterfactual_followup``'s
    SECURITIES follow-up bridge reads back from later (see
    ``_securities_structure_baseline_from_projection`` there) -- the keys
    used here are that reader's own required contract; changing a key name
    here without updating that reader would silently make every future
    SECURITIES follow-up observation ``BLOCKED_UNAVAILABLE``, not crash, but
    should not be done without updating both together."""

    economics = candidate.economics
    generated_at = candidate.evaluated_at
    valid_until = generated_at + timedelta(seconds=SECURITIES_STRUCTURE_SHARED_FRESHNESS_SECONDS)
    source_details = (
        ("underlying_symbol", candidate.underlying_symbol),
        ("underlying_canonical_security_id", candidate.underlying_canonical_security_id),
        ("structure_type", candidate.structure_type.value),
        ("expiration", candidate.expiration),
        ("dte_at_decision", str(candidate.dte)),
        ("long_strike", str(candidate.long_leg.strike)),
        ("short_strike", str(candidate.short_leg.strike)),
        ("multiplier", str(economics.multiplier)),
        ("quantity", str(candidate.quantity)),
        ("initial_cash_flow", str(structure_initial_cash_flow(economics))),
        ("underlying_reference_price_at_decision", str(candidate.underlying_reference_price)),
        ("source_chain_snapshot_id", candidate.source_chain_snapshot_id),
        ("breakeven", str(economics.breakeven)),
        ("maximum_profit", str(economics.maximum_profit)),
        ("reward_risk_ratio", str(economics.reward_risk_ratio)),
        ("quality_label", candidate.quality.label.value),
    )
    return SharedOpportunityProjection.create(
        projection_id=f"shared-securities-structure-{candidate.structure_id}",
        venue=SharedVenue.SECURITIES,
        source_record_type="OptionStructureCandidate",
        source_record_id=candidate.structure_id,
        source_canonical_hash=candidate.canonical_hash,
        strategy_or_method_id=SECURITIES_DEFINED_RISK_OPTIONS_STRUCTURE_V1,
        instrument_id=candidate.underlying_canonical_security_id,
        product_or_market_id=candidate.structure_id,
        direction=candidate.structure_type.value,
        generated_at=generated_at,
        valid_until=valid_until,
        evidence_class="GENUINE_PUBLIC",
        freshness_seconds=_age_seconds(as_of, generated_at),
        freshness_limit_seconds=SECURITIES_STRUCTURE_SHARED_FRESHNESS_SECONDS,
        expected_gross_return=None,
        expected_net_return=None,
        # Honestly absent: ``domain.options_structures`` computes descriptive
        # payoff geometry only and explicitly, permanently stamps every
        # retained structure ``EXPECTED_VALUE_UNAVAILABLE`` -- no probability
        # model exists to turn max-profit/max-loss/breakeven into a genuine
        # expected value. Fabricating one here would misrepresent proxy
        # geometry as a genuine economic edge; the ranking policy's own
        # genuine-vs-proxy gate (``GENUINE_ECONOMIC_MAGNITUDE_IDENTITIES``)
        # already treats ``None`` correctly (RESEARCH_ONLY tier, never
        # ACTIONABLE) without this producer needing to say anything more.
        comparable_value=None,
        comparable_value_identity=None,
        # No probability/uncertainty model either, for the identical reason.
        uncertainty=None,
        maximum_loss=economics.maximum_loss,
        modeled_fees=economics.fees.total_round_trip_cost,
        modeled_slippage=economics.slippage.total_slippage,
        costs_retained=True,
        # Only ``StructureQualityLabel.HIGH_QUALITY``/``USABLE`` candidates
        # ever reach this function (see ``securities_projections``) -- by
        # ``build_structure_quality``'s own construction, both are always
        # two-sided, within-freshness, non-zero-size quotes, which is
        # exactly what ``SUFFICIENT`` means for every other venue's own
        # LIQUIDITY component.
        liquidity_state="SUFFICIENT",
        liquidity_limitations=candidate.quality.reasons,
        required_hypothetical_capital=economics.capital_required,
        # Conservative: never propose allocating more than the one structure
        # itself actually requires -- the shared 25%-of-opportunity/50%-of-
        # venue budget caps in ``propose_hypothetical_allocation`` apply on
        # top of this regardless.
        maximum_sensible_research_allocation=economics.capital_required,
        source_maximum_allocation_fraction=None,
        expected_holding_days=candidate.dte,
        estimated_lockup_days=candidate.dte,
        supporting_evidence_ids=(
            candidate.structure_id,
            candidate.source_chain_snapshot_id,
            candidate.long_leg.observation_id,
            candidate.short_leg.observation_id,
        ),
        source_warnings=(),
        source_rejection_reasons=(),
        source_authority="NONE",
        projection_limitations=(
            "EXPECTED_VALUE_UNAVAILABLE_NO_PROBABILITY_MODEL",
            "RESEARCH_ONLY_NO_CAPITAL_OR_EXECUTION_AUTHORITY",
        ),
        source_details=source_details,
    )


class RetainedSharedOpportunitySource:
    """Read exact retained source rows without invoking any provider."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._crypto = PostgresCryptoMarketPulseRepository(engine)
        self._stateful_crypto = PostgresCryptoStatefulResearchRepository(engine)
        self._shared = PostgresSharedOpportunityRepository(engine)
        self._options_structures = PostgresOptionsStructuresRepository(engine)

    def _reuse_projection(
        self, projection: SharedOpportunityProjection
    ) -> SharedOpportunityProjection:
        retained = self._shared.projection(projection.projection_id)
        return retained or projection

    def strategy_library(self) -> tuple[dict[str, object], ...]:
        """Return current retained method records without creating new authority."""

        current_runs = tuple(
            sorted(
                (
                    item
                    for item in self._crypto.discovery_runs()
                    if item.method_id
                    in {
                        CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
                        CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
                    }
                ),
                key=lambda item: (item.generated_at, item.run_id),
            )
        )
        crypto_run = next(
            (
                item
                for item in reversed(current_runs)
                if item.method_id == CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1
            ),
            None,
        )
        reversal_run = next(
            (
                item
                for item in reversed(current_runs)
                if item.method_id == CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1
            ),
            None,
        )
        crypto_opportunities = (
            ()
            if crypto_run is None
            else tuple(
                item
                for item in self._crypto.opportunities()
                if item.discovery_run_id == crypto_run.run_id
            )
        )
        reversal_opportunities = (
            ()
            if reversal_run is None
            else tuple(
                item
                for item in self._crypto.opportunities()
                if item.discovery_run_id == reversal_run.run_id
            )
        )
        stateful_signal = next(iter(reversed(self._stateful_crypto.current_signals())), None)
        stateful_opportunities = (
            ()
            if stateful_signal is None
            else tuple(
                item
                for item in self._crypto.opportunities()
                if item.discovery_run_id == stateful_signal.signal_id
            )
        )
        with self._engine.connect() as connection:
            threshold_scan = (
                connection.execute(
                    sa.select(threshold_ladder_scan)
                    .where(
                        threshold_ladder_scan.c.state.in_(("COMPLETED", "PARTIAL")),
                        threshold_ladder_scan.c.completed_at.is_not(None),
                    )
                    .order_by(
                        threshold_ladder_scan.c.completed_at.desc(),
                        threshold_ladder_scan.c.scan_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            threshold_market_count = 0
            if threshold_scan is not None:
                market_tickers = sa.union_all(
                    sa.select(
                        threshold_market_relationship.c.broader_market_ticker.label("ticker")
                    ).where(threshold_market_relationship.c.scan_id == threshold_scan["scan_id"]),
                    sa.select(
                        threshold_market_relationship.c.narrower_market_ticker.label("ticker")
                    ).where(threshold_market_relationship.c.scan_id == threshold_scan["scan_id"]),
                ).subquery()
                threshold_market_count = int(
                    connection.scalar(
                        sa.select(sa.func.count(sa.distinct(market_tickers.c.ticker)))
                    )
                    or 0
                )
        calibration = None if crypto_run is None else crypto_run.calibration
        calibration_coverage = () if calibration is None else calibration.asset_coverage
        calibration_buckets = () if calibration is None else calibration.buckets
        reversal_calibration = None if reversal_run is None else reversal_run.calibration
        reversal_coverage = (
            () if reversal_calibration is None else reversal_calibration.asset_coverage
        )
        reversal_buckets = () if reversal_calibration is None else reversal_calibration.buckets
        exhaustive_evaluations = self._exhaustive_event_set_evaluations()
        shared_eligible_by_method: dict[str, int] = {}
        # PAPER Operations & Observability v1: a second, parallel fold over
        # the identical already-retained current shared set -- never a new
        # query or a new eligibility computation. `SharedOpportunityProjection
        # .paper_eligible` (domain.shared_opportunity) is already genuinely
        # computed by `_crypto_paper_readiness` at SHARED_OPPORTUNITY_REBUILD
        # time (see docs/PAPER_ELIGIBILITY_TRACE_2026-08-25.md); this method
        # simply stopped reading it and hardcoded False/0 below instead. This
        # fold restores the real per-method count the same way
        # shared_eligible_by_method already restores the real ELIGIBLE count.
        paper_eligible_by_method: dict[str, int] = {}
        shared_sets = self._shared.opportunity_sets()
        if shared_sets:
            current_shared = shared_sets[-1]
            decisions = {
                item.projection_id: item
                for item in self._shared.eligibility_decisions(current_shared.opportunity_set_id)
            }
            for projection_id in current_shared.projection_ids:
                projection = self._shared.projection(projection_id)
                decision = decisions.get(projection_id)
                if (
                    projection is not None
                    and decision is not None
                    and decision.state is EligibilityState.ELIGIBLE
                ):
                    shared_eligible_by_method[projection.strategy_or_method_id] = (
                        shared_eligible_by_method.get(projection.strategy_or_method_id, 0) + 1
                    )
                if projection is not None and projection.paper_eligible:
                    paper_eligible_by_method[projection.strategy_or_method_id] = (
                        paper_eligible_by_method.get(projection.strategy_or_method_id, 0) + 1
                    )
        library = (
            {
                "method_id": CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
                "version": 1,
                "venue": "CRYPTO",
                "mechanism": "CROSS_SECTIONAL_RELATIVE_STRENGTH",
                "record_type": "CryptoDiscoveryRun",
                "current_record_id": None if crypto_run is None else crypto_run.run_id,
                "source_record_id": None if crypto_run is None else crypto_run.source_pulse_id,
                "status": (
                    "NOT_RUN"
                    if crypto_run is None
                    else (
                        "CURRENT_WITH_ELIGIBLE_OUTPUTS"
                        if any(
                            item.status is OpportunityStatus.RESEARCH_ONLY
                            for item in crypto_opportunities
                        )
                        else "CURRENT_NO_ELIGIBLE_OUTPUTS"
                    )
                ),
                "result_count": len(crypto_opportunities),
                "eligible_result_count": sum(
                    item.status is OpportunityStatus.RESEARCH_ONLY for item in crypto_opportunities
                ),
                "evaluated_at": None if crypto_run is None else crypto_run.generated_at,
                "evidence_class": "GENUINE_PUBLIC_RETAINED_PULSE",
                "supported_instruments": "CURRENT_ELIGIBLE_CANONICAL_SPOT_UNIVERSE",
                "required_data": (
                    "30D_AND_90D_DAILY_CLOSES",
                    "CURRENT_PUBLIC_TOP_OF_BOOK_AND_DEPTH",
                    "PRODUCT_STATUS_PRECISION_AND_MINIMUMS",
                ),
                "latest_signal_time": None if crypto_run is None else crypto_run.generated_at,
                "current_opportunity_count": len(crypto_opportunities),
                "source_eligibility_count": sum(
                    item.status is OpportunityStatus.RESEARCH_ONLY for item in crypto_opportunities
                ),
                "shared_eligible_count": shared_eligible_by_method.get(
                    CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1, 0
                ),
                "paper_ready_count": paper_eligible_by_method.get(
                    CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1, 0
                ),
                "health_state": "INSUFFICIENT_PAPER_SAMPLE",
                "economics": (
                    "STRICTLY_PRIOR_WALK_FORWARD_EXPECTED_RETURN_WITH_CURRENT_SPREAD_DEPTH_"
                    "MINIMUM_SIZE_FEES_AND_SLIPPAGE"
                ),
                "coverage_summary": (
                    "NO_RETAINED_CALIBRATION"
                    if calibration is None
                    else (
                        f"{calibration.dataset_start.date().isoformat()}.."
                        f"{calibration.dataset_end.date().isoformat()} · "
                        f"{len(calibration_coverage)} assets · "
                        f"{sum(item.accepted_buckets for item in calibration_coverage)} buckets · "
                        f"{len(calibration.decision_outcomes)} decisions"
                    )
                ),
                "calibration_coverage": tuple(
                    {
                        "asset": item.canonical_asset_id,
                        "product_id": item.product_id,
                        "observation_id": item.observation_id,
                        "requested_start": item.requested_start,
                        "requested_end": item.requested_end,
                        "accepted_buckets": item.accepted_buckets,
                        "missing_buckets": item.missing_buckets,
                        "rejected_records": item.rejected_records,
                        "request_count": item.request_count,
                        "coverage_status": item.coverage_status,
                        "limitations": item.limitations,
                    }
                    for item in calibration_coverage
                ),
                "calibration_buckets": tuple(
                    {
                        "bucket": item.bucket,
                        "sample_count": item.sample_count,
                        "expected_gross_return": item.expected_gross_return,
                        "expected_net_return": item.expected_net_return,
                        "uncertainty_lower": item.uncertainty_lower,
                        "uncertainty_upper": item.uncertainty_upper,
                        "net_uncertainty_lower": item.net_uncertainty_lower,
                        "net_uncertainty_upper": item.net_uncertainty_upper,
                        "modeled_round_trip_cost": item.modeled_round_trip_cost,
                    }
                    for item in calibration_buckets
                ),
                "calibration_decision_count": (
                    0 if calibration is None else len(calibration.decision_outcomes)
                ),
                "calibration_included_outcome_count": (
                    0
                    if calibration is None
                    else sum(item.included_in_bucket for item in calibration.decision_outcomes)
                ),
                "limitations": (
                    "EXPECTED_RETURN_IS_NOT_A_GUARANTEE",
                    "OVERLAPPING_OUTCOMES_LIMIT_INDEPENDENCE",
                    "DISPLAYED_DEPTH_IS_NOT_FILLABILITY",
                ),
                "research_authority": "NONE",
                "paper_eligible": paper_eligible_by_method.get(
                    CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1, 0
                )
                > 0,
                "live_authority": "NONE",
            },
            {
                "method_id": CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
                "version": 1,
                "venue": "CRYPTO",
                "mechanism": "SHORT_HORIZON_REVERSAL_WITHIN_POSITIVE_90_DAY_REGIME",
                "record_type": "CryptoDiscoveryRun",
                "current_record_id": None if reversal_run is None else reversal_run.run_id,
                "source_record_id": (
                    None if reversal_run is None else reversal_run.source_pulse_id
                ),
                "status": (
                    "NOT_RUN"
                    if reversal_run is None
                    else (
                        "CURRENT_WITH_ELIGIBLE_OUTPUTS"
                        if any(
                            item.status is OpportunityStatus.RESEARCH_ONLY
                            for item in reversal_opportunities
                        )
                        else "CURRENT_NO_ELIGIBLE_OUTPUTS"
                    )
                ),
                "result_count": len(reversal_opportunities),
                "eligible_result_count": sum(
                    item.status is OpportunityStatus.RESEARCH_ONLY
                    for item in reversal_opportunities
                ),
                "evaluated_at": None if reversal_run is None else reversal_run.generated_at,
                "evidence_class": "GENUINE_PUBLIC_RETAINED_PULSE",
                "supported_instruments": "CURRENT_ELIGIBLE_CANONICAL_SPOT_UNIVERSE",
                "required_data": (
                    "7D_AND_90D_DAILY_CLOSES",
                    "30D_REALIZED_VOLATILITY",
                    "CURRENT_PUBLIC_SPREAD_DEPTH_STATUS_AND_FRESHNESS",
                ),
                "latest_signal_time": (None if reversal_run is None else reversal_run.generated_at),
                "current_opportunity_count": len(reversal_opportunities),
                "source_eligibility_count": sum(
                    item.status is OpportunityStatus.RESEARCH_ONLY
                    for item in reversal_opportunities
                ),
                "shared_eligible_count": shared_eligible_by_method.get(
                    CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1, 0
                ),
                "paper_ready_count": paper_eligible_by_method.get(
                    CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1, 0
                ),
                "health_state": "INSUFFICIENT_PAPER_SAMPLE",
                "economics": (
                    "STRICTLY_PRIOR_14D_AND_30D_WALK_FORWARD_OUTCOMES_WITH_95_PERCENT_"
                    "UNCERTAINTY_DOWNSIDE_COST_SPREAD_AND_DEPTH_EVIDENCE"
                ),
                "coverage_summary": (
                    "NO_RETAINED_CALIBRATION"
                    if reversal_calibration is None
                    else (
                        f"{reversal_calibration.dataset_start.date().isoformat()}.."
                        f"{reversal_calibration.dataset_end.date().isoformat()} · "
                        f"{len(reversal_coverage)} assets · "
                        f"{sum(item.accepted_buckets for item in reversal_coverage)} buckets · "
                        f"{len(reversal_calibration.decision_outcomes)} decisions"
                    )
                ),
                "calibration_coverage": tuple(
                    {
                        "asset": item.canonical_asset_id,
                        "product_id": item.product_id,
                        "observation_id": item.observation_id,
                        "requested_start": item.requested_start,
                        "requested_end": item.requested_end,
                        "accepted_buckets": item.accepted_buckets,
                        "missing_buckets": item.missing_buckets,
                        "rejected_records": item.rejected_records,
                        "request_count": item.request_count,
                        "coverage_status": item.coverage_status,
                        "limitations": item.limitations,
                    }
                    for item in reversal_coverage
                ),
                "calibration_buckets": tuple(
                    {
                        "bucket": item.bucket,
                        "horizons": tuple(
                            {
                                "outcome_days": horizon.outcome_days,
                                "sample_count": horizon.sample_count,
                                "expected_gross_return": horizon.expected_gross_return,
                                "expected_net_return": horizon.expected_net_return,
                                "uncertainty_lower": horizon.uncertainty_lower,
                                "uncertainty_upper": horizon.uncertainty_upper,
                                "downside_probability": horizon.downside_probability,
                                "downside_p05": horizon.downside_p05,
                                "maximum_observed_loss": horizon.maximum_observed_loss,
                            }
                            for horizon in item.horizon_results
                        ),
                    }
                    for item in reversal_buckets
                ),
                "calibration_decision_count": (
                    0
                    if reversal_calibration is None
                    else len(reversal_calibration.decision_outcomes)
                ),
                "calibration_included_outcome_count": (
                    0
                    if reversal_calibration is None
                    else sum(
                        item.included_in_bucket for item in reversal_calibration.decision_outcomes
                    )
                ),
                "limitations": (
                    "EXPECTED_RETURN_IS_NOT_A_GUARANTEE",
                    "HISTORICAL_BOOK_PENALTIES_UNAVAILABLE",
                    "OVERLAPPING_14D_AND_30D_OUTCOMES_LIMIT_INDEPENDENCE",
                    "DISPLAYED_DEPTH_IS_NOT_FILLABILITY",
                ),
                "research_authority": "NONE",
                "paper_eligible": paper_eligible_by_method.get(
                    CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1, 0
                )
                > 0,
                "live_authority": "NONE",
            },
            {
                "method_id": CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
                "version": 1,
                "venue": "CRYPTO",
                "mechanism": "STATEFUL_FIXED_PULLBACK_ROTATION_THROUGH_CASH",
                "record_type": "CryptoStatefulCurrentSignal",
                "current_record_id": (
                    None if stateful_signal is None else stateful_signal.signal_id
                ),
                "source_record_id": (
                    None if stateful_signal is None else stateful_signal.source_pulse_id
                ),
                "status": (
                    "NOT_RUN"
                    if stateful_signal is None
                    else (
                        "CURRENT_LONG_SIGNAL"
                        if stateful_signal.selected_product_id is not None
                        else "CURRENT_CASH"
                    )
                ),
                "result_count": len(stateful_opportunities),
                "eligible_result_count": sum(
                    item.status is OpportunityStatus.RESEARCH_ONLY
                    for item in stateful_opportunities
                ),
                "evaluated_at": (None if stateful_signal is None else stateful_signal.generated_at),
                "evidence_class": "GENUINE_PUBLIC_RETAINED_PULSE_AND_HISTORY",
                "supported_instruments": "CURRENT_ELIGIBLE_CANONICAL_SPOT_UNIVERSE",
                "required_data": (
                    "7D_AND_90D_DAILY_CLOSES",
                    "30D_REALIZED_VOLATILITY_AND_UNIVERSE_MEDIAN",
                    "CURRENT_PUBLIC_SPREAD_DEPTH_STATUS_AND_FRESHNESS",
                    "RETAINED_DAILY_OPEN_EXECUTION_EVIDENCE",
                ),
                "latest_signal_time": (
                    None if stateful_signal is None else stateful_signal.generated_at
                ),
                "current_opportunity_count": len(stateful_opportunities),
                "source_eligibility_count": sum(
                    item.status is OpportunityStatus.RESEARCH_ONLY
                    for item in stateful_opportunities
                ),
                "shared_eligible_count": shared_eligible_by_method.get(
                    CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1, 0
                ),
                "paper_ready_count": paper_eligible_by_method.get(
                    CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1, 0
                ),
                "health_state": "INSUFFICIENT_PAPER_SAMPLE",
                "economics": "EXPECTED_ECONOMICS_ONLY_FROM_LEAKAGE_SAFE_RETAINED_CALIBRATION",
                "current_state": None if stateful_signal is None else stateful_signal.state,
                "selected_asset": (
                    None if stateful_signal is None else stateful_signal.selected_asset_id
                ),
                "selected_product": (
                    None if stateful_signal is None else stateful_signal.selected_product_id
                ),
                "experiment_run_id": (
                    None if stateful_signal is None else stateful_signal.experiment_run_id
                ),
                "recommendation_id": (
                    None if stateful_signal is None else stateful_signal.recommendation_id
                ),
                "limitations": (
                    "ONE_GENUINE_EXPERIMENT_DOES_NOT_ESTABLISH_EXPECTED_RETURN",
                    "DISPLAYED_DEPTH_IS_NOT_FILLABILITY",
                    "NO_AUTOMATIC_PAPER_ACTION",
                ),
                "research_authority": "NONE",
                "paper_eligible": paper_eligible_by_method.get(
                    CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1, 0
                )
                > 0,
                "live_authority": "NONE",
            },
            {
                "method_id": KALSHI_RELATED_MARKET_CONSISTENCY_V1,
                "version": 1,
                "venue": "KALSHI",
                "mechanism": "RULE_VERIFIED_THRESHOLD_RELATIONSHIP_PACKAGE",
                "record_type": "ThresholdLadderScan",
                "current_record_id": (
                    None if threshold_scan is None else str(threshold_scan["scan_id"])
                ),
                "source_record_id": (
                    None
                    if threshold_scan is None
                    else str(threshold_scan["evidence_ingestion_run_id"])
                ),
                "status": (
                    "NOT_RUN"
                    if threshold_scan is None
                    else (
                        "CURRENT_WITH_CANDIDATES"
                        if int(threshold_scan["candidate_count"]) > 0
                        else "CURRENT_NO_CANDIDATES"
                    )
                ),
                "result_count": (
                    0 if threshold_scan is None else int(threshold_scan["evaluation_count"])
                ),
                "eligible_result_count": (
                    0 if threshold_scan is None else int(threshold_scan["candidate_count"])
                ),
                "evaluated_at": (
                    None if threshold_scan is None else threshold_scan["completed_at"]
                ),
                "evidence_class": "GENUINE_PUBLIC_RETAINED_THRESHOLD_LADDER",
                "supported_instruments": "RULE_VERIFIED_NUMERIC_THRESHOLD_MARKET_LADDERS",
                "required_data": (
                    "EXACT_EVENT_AND_MARKET_IDENTITIES",
                    "STRUCTURED_RULE_AND_SETTLEMENT_RELATIONSHIP",
                    "PUBLIC_ORDERBOOK_DEPTH_AND_SERIES_FEES",
                ),
                "latest_signal_time": (
                    None if threshold_scan is None else threshold_scan["completed_at"]
                ),
                "current_opportunity_count": (
                    0 if threshold_scan is None else int(threshold_scan["candidate_count"])
                ),
                "source_eligibility_count": (
                    0 if threshold_scan is None else int(threshold_scan["candidate_count"])
                ),
                "shared_eligible_count": shared_eligible_by_method.get(
                    KALSHI_RELATED_MARKET_CONSISTENCY_V1, 0
                ),
                "paper_ready_count": paper_eligible_by_method.get(
                    KALSHI_RELATED_MARKET_CONSISTENCY_V1, 0
                ),
                "health_state": "INSUFFICIENT_PAPER_SAMPLE",
                "economics": (
                    "MODEL_FREE_BROADER_YES_PLUS_NARROWER_NO_SETTLEMENT_FLOOR_WITH_"
                    "MATCHED_DEPTH_FEES_AND_ROUNDING"
                ),
                "scan_breadth_summary": (
                    "NO_RETAINED_SCAN"
                    if threshold_scan is None
                    else (
                        f"{threshold_market_count} markets · "
                        f"{threshold_scan['selected_event_count']} selected events · "
                        f"{threshold_scan['relationship_count']} relationships · "
                        f"{threshold_scan['evaluation_count']} evaluations"
                    )
                ),
                "scan_breadth": (
                    {}
                    if threshold_scan is None
                    else {
                        "requested_events": int(threshold_scan["requested_event_count"]),
                        "selected_events": int(threshold_scan["selected_event_count"]),
                        "markets_considered": threshold_market_count,
                        "maximum_markets_per_event": int(
                            threshold_scan["maximum_markets_per_event"]
                        ),
                        "relationships_retained": int(threshold_scan["relationship_count"]),
                        "evaluations_retained": int(threshold_scan["evaluation_count"]),
                        "candidates": int(threshold_scan["candidate_count"]),
                    }
                ),
                "limitations": (
                    "PROBABILITIES_NOT_INVENTED",
                    "MULTI_LEG_EXECUTION_IS_NOT_ATOMIC",
                    "DISPLAYED_DEPTH_IS_NOT_FILLABILITY",
                ),
                "research_authority": "NONE",
                "paper_eligible": paper_eligible_by_method.get(
                    KALSHI_RELATED_MARKET_CONSISTENCY_V1, 0
                )
                > 0,
                "live_authority": "NONE",
            },
            {
                "method_id": KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1,
                "version": 1,
                "venue": "KALSHI",
                "mechanism": "MODEL_FREE_EXHAUSTIVE_YES_COMPLETE_SET",
                "record_type": "ExhaustiveEventSetEvaluation",
                "current_record_id": (
                    None if threshold_scan is None else str(threshold_scan["scan_id"])
                ),
                "source_record_id": (
                    None
                    if threshold_scan is None
                    else str(threshold_scan["evidence_ingestion_run_id"])
                ),
                "status": (
                    "NOT_RUN"
                    if threshold_scan is None
                    else (
                        "CURRENT_WITH_CANDIDATES"
                        if any(item.source_eligible for item in exhaustive_evaluations)
                        else "CURRENT_NO_CANDIDATES"
                    )
                ),
                "result_count": len(exhaustive_evaluations),
                "eligible_result_count": sum(
                    item.source_eligible for item in exhaustive_evaluations
                ),
                "evaluated_at": (
                    None if threshold_scan is None else threshold_scan["completed_at"]
                ),
                "evidence_class": "GENUINE_PUBLIC_RETAINED_EVENT_SET",
                "supported_instruments": "PROVEN_EXHAUSTIVE_MUTUALLY_EXCLUSIVE_EVENT_SETS",
                "required_data": (
                    "EXACT_EVENT_MARKET_AND_RULE_IDENTITIES",
                    "VERIFIED_MUTUAL_EXCLUSIVITY_AND_EXHAUSTIVENESS",
                    "PUBLIC_YES_ASK_DEPTH_SERIES_FEES_AND_ROUNDING",
                ),
                "latest_signal_time": (
                    None if threshold_scan is None else threshold_scan["completed_at"]
                ),
                "current_opportunity_count": len(exhaustive_evaluations),
                "source_eligibility_count": sum(
                    item.source_eligible for item in exhaustive_evaluations
                ),
                "shared_eligible_count": shared_eligible_by_method.get(
                    KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1, 0
                ),
                "paper_ready_count": paper_eligible_by_method.get(
                    KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1, 0
                ),
                "health_state": "INSUFFICIENT_PAPER_SAMPLE",
                "economics": (
                    "ONE_YES_PER_PROVEN_COMPLETE_OUTCOME_SET_WITH_MATCHED_DEPTH_EXACT_FEES_"
                    "ROUNDING_AND_FIXED_SETTLEMENT_PAYOUT"
                ),
                "scan_breadth_summary": (
                    "NO_RETAINED_SCAN"
                    if threshold_scan is None
                    else (
                        f"{threshold_market_count} markets · "
                        f"{len(exhaustive_evaluations)} event sets · "
                        f"{sum(item.exhaustiveness_verified for item in exhaustive_evaluations)} "
                        "exhaustive · "
                        f"{sum(item.source_eligible for item in exhaustive_evaluations)} eligible"
                    )
                ),
                "event_set_results": tuple(
                    {
                        "event_ticker": item.event_ticker,
                        "market_count": len(item.legs),
                        "exhaustiveness_verified": item.exhaustiveness_verified,
                        "matched_quantity": item.matched_quantity,
                        "gross_package_cost": item.gross_package_cost,
                        "modeled_fees": item.modeled_fees,
                        "rounding_reserve": item.rounding_reserve,
                        "guaranteed_settlement_payout": item.guaranteed_settlement_payout,
                        "conservative_net_gap": item.conservative_net_gap,
                        "source_eligible": item.source_eligible,
                        "rejection_reasons": item.rejection_reasons,
                    }
                    for item in exhaustive_evaluations
                ),
                "limitations": (
                    "PROBABILITIES_NOT_INVENTED",
                    "EXHAUSTIVENESS_MUST_BE_PROVEN",
                    "MULTI_LEG_EXECUTION_IS_NOT_ATOMIC",
                    "DISPLAYED_DEPTH_IS_NOT_FILLABILITY",
                ),
                "research_authority": "NONE",
                "paper_eligible": paper_eligible_by_method.get(
                    KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1, 0
                )
                > 0,
                "live_authority": "NONE",
            },
        )
        research_operations = PostgresResearchOperationsRepository(self._engine)
        implementation = research_operations.first_method_implementation()
        method_runs = research_operations.first_method_runs()
        if implementation is None:
            return library
        current_method_run = method_runs[-1] if method_runs else None
        return (
            *library,
            {
                "method_id": implementation.method_id,
                "version": implementation.method_version,
                "venue": "CROSS_VENUE",
                "mechanism": "UNCHANGED_RETAINED_EVIDENCE_OPERATIONALIZATION",
                "record_type": "FirstGeneratedResearchMethodRun",
                "current_record_id": (
                    None if current_method_run is None else current_method_run.method_run_id
                ),
                "source_record_id": implementation.selection_id,
                "status": "IMPLEMENTED" if current_method_run is None else "CURRENT_COMPLETE",
                "result_count": 0 if current_method_run is None else 1,
                "eligible_result_count": (
                    0
                    if current_method_run is None
                    else len(current_method_run.paper_ready_proposal_ids)
                ),
                "evaluated_at": (
                    implementation.implemented_at
                    if current_method_run is None
                    else current_method_run.completed_at
                ),
                "evidence_class": "GENUINE_RETAINED_CURRENT_OPPORTUNITY",
                "supported_instruments": "EXISTING_CURRENT_CROSS_VENUE_ALLOCATION",
                "required_data": implementation.approved_data_ids,
                "current_opportunity_count": (
                    0 if current_method_run is None else current_method_run.source_opportunity_count
                ),
                "source_eligibility_count": (
                    0 if current_method_run is None else current_method_run.shared_eligible_count
                ),
                "shared_eligible_count": (
                    0 if current_method_run is None else current_method_run.shared_eligible_count
                ),
                "paper_ready_count": (
                    0
                    if current_method_run is None
                    else len(current_method_run.paper_ready_proposal_ids)
                ),
                "health_state": "INSUFFICIENT_PAPER_SAMPLE",
                "limitations": (
                    "NO_EXPECTED_RETURN_INFERRED",
                    "ZERO_ALLOCATION_IS_VALID",
                    "NO_PROVIDER_REQUESTS",
                ),
                "research_authority": "NONE",
                "paper_eligible": (
                    False
                    if current_method_run is None
                    else len(current_method_run.paper_ready_proposal_ids) > 0
                ),
                "live_authority": "NONE",
            },
        )

    def securities_projections(self, *, as_of: datetime) -> tuple[SharedOpportunityProjection, ...]:
        """Securities Research -> Shared Opportunity Recurring Bridge v1.

        Reads only already-retained ``OptionStructureCandidate`` evidence
        (``latest_structures_by_underlying``: the current trading session's
        structures per underlying, never the full historical backlog, never
        a live Alpaca request) -- exactly the same "read exact retained
        source rows without invoking any provider" contract this whole class
        promises for CRYPTO/KALSHI. Safe to call every ~5 minutes purely
        because it is a cheap, bounded local read of evidence an entirely
        separate, already trading-session-gated recurring task
        (``OPTIONS_RESEARCH_EVIDENCE_CAPTURE``) is solely responsible for
        refreshing -- this method itself never decides when new evidence is
        due, mirroring exactly how ``kalshi_projections`` never re-triggers a
        Kalshi refresh either.

        Only ``StructureQualityLabel.HIGH_QUALITY``/``USABLE`` candidates
        become projections (see ``_SECURITIES_ACCEPTABLE_QUALITY_LABELS``).
        Every projection is honestly ``comparable_value=None`` -- the source
        structure economics module (``domain.options_structures``) has no
        probability-of-profit or expected-value model and explicitly stamps
        every retained structure ``EXPECTED_VALUE_UNAVAILABLE`` by design
        (never a computed edge from payoff geometry alone) -- so every
        SECURITIES projection reaches Shared Opportunity honestly tiered
        RESEARCH_ONLY, never ACTIONABLE, until a future milestone adds a
        genuine, explicit probability model (see this module's own
        module-level docstring note, if any, and the milestone's own final
        report for the full rationale)."""

        candidates = self._options_structures.latest_structures_by_underlying()
        return tuple(
            self._reuse_projection(_securities_structure_projection(candidate, as_of=as_of))
            for candidate in candidates
            if candidate.quality.label in _SECURITIES_ACCEPTABLE_QUALITY_LABELS
        )

    def crypto_projections(
        self, *, pulse_id: str, as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        pulse = self._crypto.pulse_snapshot(pulse_id)
        if pulse is None:
            raise SharedOpportunityError("SHARED_CRYPTO_PULSE_NOT_FOUND")
        lineages = tuple(
            item for item in self._crypto.opportunity_lineages() if item.pulse_id == pulse_id
        )
        if len(lineages) != len(pulse.selected_routes):
            raise SharedOpportunityError("SHARED_CRYPTO_PULSE_LINEAGE_INCOMPLETE")
        checkpoints = {
            item.product_id: item for item in self._crypto.pulse_checkpoints(pulse.refresh_id)
        }
        values: list[SharedOpportunityProjection] = []
        for lineage in sorted(lineages, key=lambda item: item.canonical_asset_id):
            if lineage.current_opportunity_id is None:
                raise SharedOpportunityError("SHARED_CRYPTO_OPPORTUNITY_LINEAGE_MISSING")
            opportunity = self._crypto.opportunity(lineage.current_opportunity_id)
            if opportunity is None:
                raise SharedOpportunityError("SHARED_CRYPTO_OPPORTUNITY_NOT_FOUND")
            discovery = self._crypto.discovery_run(opportunity.discovery_run_id)
            universe = self._crypto.universe_snapshot(opportunity.universe_snapshot_id)
            checkpoint = checkpoints.get(opportunity.product_id)
            if discovery is None or universe is None or checkpoint is None:
                raise SharedOpportunityError("SHARED_CRYPTO_SOURCE_LINEAGE_MISSING")
            product = next(
                (
                    item
                    for item in self._crypto.products(universe.source_capture_id)
                    if item.product_id == opportunity.product_id
                ),
                None,
            )
            if product is None:
                raise SharedOpportunityError("SHARED_CRYPTO_PRODUCT_REFERENCE_MISSING")
            result = next(
                (item for item in discovery.results if item.product_id == opportunity.product_id),
                None,
            )
            if result is None:
                raise SharedOpportunityError("SHARED_CRYPTO_DISCOVERY_RESULT_MISSING")
            fee_ratio = opportunity.fee_bps_per_side * Decimal(2) / Decimal(10000)
            slippage_ratio = opportunity.slippage_bps_per_side * Decimal(2) / Decimal(10000)
            quote_complete = all(
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
            required_capital: Decimal | None = None
            maximum_loss: Decimal | None = None
            maximum_allocation: Decimal | None = None
            if quote_complete:
                assert checkpoint.best_ask is not None
                minimum_quantity = max(
                    product.quantity_increment,
                    product.minimum_quantity or Decimal(0),
                )
                minimum_notional = max(
                    product.minimum_notional or Decimal(0),
                    minimum_quantity * checkpoint.best_ask,
                )
                minimum_notional = _round_up(minimum_notional, product.price_increment)
                entry_cost_ratio = (
                    opportunity.fee_bps_per_side + opportunity.slippage_bps_per_side
                ) / Decimal(10000)
                required_capital = _shared_decimal(
                    minimum_notional * (Decimal(1) + entry_cost_ratio)
                )
                maximum_loss = required_capital
                maximum_allocation = _shared_decimal(checkpoint.visible_ask_depth or Decimal(0))
            liquidity_state = "UNAVAILABLE"
            if quote_complete and required_capital is not None:
                liquidity_state = (
                    "SUFFICIENT"
                    if (checkpoint.visible_ask_depth or Decimal(0)) >= required_capital
                    else "INSUFFICIENT"
                )
            freshness = (
                checkpoint.freshness_seconds or 0
                if checkpoint.quote_captured_at is None
                else _age_seconds(as_of, checkpoint.quote_captured_at)
            )
            values.append(
                self._reuse_projection(
                    SharedOpportunityProjection.create(
                        projection_id=f"shared-crypto-{opportunity.opportunity_id}",
                        venue=SharedVenue.CRYPTO,
                        source_record_type="CryptoResearchOpportunity",
                        source_record_id=opportunity.opportunity_id,
                        source_canonical_hash=opportunity.canonical_hash,
                        strategy_or_method_id=discovery.method_id,
                        instrument_id=opportunity.canonical_asset_id,
                        product_or_market_id=opportunity.product_id,
                        direction="HYPOTHETICAL_LONG_SPOT_RESEARCH",
                        generated_at=opportunity.generated_at,
                        valid_until=opportunity.valid_until,
                        evidence_class=universe.evidence_class.value,
                        freshness_seconds=freshness,
                        freshness_limit_seconds=PULSE_FRESHNESS_HOURS * 3600,
                        expected_gross_return=None,
                        expected_net_return=None,
                        comparable_value=opportunity.gross_research_score,
                        comparable_value_identity="CRYPTO_GROSS_RESEARCH_SCORE_NOT_EXPECTED_RETURN",
                        uncertainty=opportunity.volatility_estimate,
                        maximum_loss=maximum_loss,
                        modeled_fees=fee_ratio,
                        modeled_slippage=slippage_ratio,
                        costs_retained=True,
                        liquidity_state=liquidity_state,
                        liquidity_limitations=(
                            *opportunity.liquidity_limitations,
                            "DISPLAYED_PUBLIC_DEPTH_IS_NOT_FILLABILITY",
                        ),
                        required_hypothetical_capital=required_capital,
                        maximum_sensible_research_allocation=maximum_allocation,
                        source_maximum_allocation_fraction=opportunity.maximum_research_allocation,
                        expected_holding_days=opportunity.hypothetical_holding_days,
                        estimated_lockup_days=opportunity.hypothetical_holding_days,
                        supporting_evidence_ids=(
                            pulse.pulse_id,
                            pulse.universe_snapshot_id,
                            opportunity.discovery_run_id,
                            opportunity.supporting_observation_id,
                            lineage.lineage_id,
                            checkpoint.checkpoint_id,
                            *(
                                ()
                                if checkpoint.quote_raw_evidence_hash is None
                                else (checkpoint.quote_raw_evidence_hash,)
                            ),
                        ),
                        source_warnings=opportunity.warnings,
                        source_rejection_reasons=(
                            opportunity.rejection_reasons
                            if opportunity.status is OpportunityStatus.REJECTED
                            else ()
                        ),
                        source_authority=opportunity.capital_authority,
                        projection_limitations=(
                            "CRYPTO_SCORE_IS_NOT_EXPECTED_RETURN",
                            "SPOT_DOWNSIDE_ASSUMES_PRICE_CAN_FALL_TO_ZERO",
                            "DISPLAYED_PUBLIC_DEPTH_IS_NOT_FILLABILITY",
                            *(
                                ()
                                if quote_complete
                                else ("PUBLIC_TOP_OF_BOOK_AND_DEPTH_UNAVAILABLE",)
                            ),
                        ),
                        source_details=(
                            ("canonical_base_asset", opportunity.canonical_asset_id),
                            ("selected_provider_route", opportunity.product_id),
                            ("pulse_id", pulse.pulse_id),
                            ("universe_snapshot_id", pulse.universe_snapshot_id),
                            ("discovery_rank", str(opportunity.rank)),
                            ("discovery_score", str(opportunity.gross_research_score)),
                            ("trailing_return", str(result.trailing_return)),
                            ("realized_volatility", str(result.realized_volatility)),
                            ("risk_adjusted_return", str(result.risk_adjusted_return)),
                            ("liquidity_penalty", str(result.liquidity_penalty)),
                            ("estimated_round_trip_cost", str(result.estimated_round_trip_cost)),
                            ("persistence_classification", lineage.persistence.value),
                            ("pulse_quality", checkpoint.quality.value),
                            (
                                "best_bid",
                                "" if checkpoint.best_bid is None else str(checkpoint.best_bid),
                            ),
                            (
                                "best_ask",
                                "" if checkpoint.best_ask is None else str(checkpoint.best_ask),
                            ),
                            (
                                "midpoint",
                                "" if checkpoint.midpoint is None else str(checkpoint.midpoint),
                            ),
                            (
                                "spread_bps",
                                "" if checkpoint.spread_bps is None else str(checkpoint.spread_bps),
                            ),
                            (
                                "visible_bid_quote_depth",
                                (
                                    ""
                                    if checkpoint.visible_bid_depth is None
                                    else str(checkpoint.visible_bid_depth)
                                ),
                            ),
                            (
                                "visible_ask_quote_depth",
                                (
                                    ""
                                    if checkpoint.visible_ask_depth is None
                                    else str(checkpoint.visible_ask_depth)
                                ),
                            ),
                            ("base_increment", str(product.quantity_increment)),
                            ("quote_increment", str(product.price_increment)),
                            (
                                "minimum_quantity",
                                (
                                    ""
                                    if product.minimum_quantity is None
                                    else str(product.minimum_quantity)
                                ),
                            ),
                            (
                                "minimum_notional",
                                (
                                    ""
                                    if product.minimum_notional is None
                                    else str(product.minimum_notional)
                                ),
                            ),
                            (
                                "required_hypothetical_capital",
                                "" if required_capital is None else str(required_capital),
                            ),
                            (
                                "maximum_loss",
                                "" if maximum_loss is None else str(maximum_loss),
                            ),
                            ("quote_evidence_quality", "COMPLETE" if quote_complete else "MISSING"),
                        ),
                    )
                )
            )
        current_runs_by_method: dict[str, CryptoDiscoveryRun] = {}
        for current_run in sorted(
            (
                item
                for item in self._crypto.discovery_runs()
                if item.source_pulse_id == pulse_id
                and item.method_id
                in {
                    CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
                    CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
                }
            ),
            key=lambda item: (item.generated_at, item.run_id),
        ):
            current_runs_by_method[current_run.method_id] = current_run
        for method_id in (
            CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1,
            CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1,
        ):
            selected_run = current_runs_by_method.get(method_id)
            if selected_run is None:
                continue
            current_opportunities = tuple(
                sorted(
                    (
                        item
                        for item in self._crypto.opportunities()
                        if item.discovery_run_id == selected_run.run_id
                    ),
                    key=lambda item: (item.rank, item.canonical_asset_id),
                )
            )
            if len(current_opportunities) != len(selected_run.results):
                raise SharedOpportunityError("SHARED_CRYPTO_CURRENT_METHOD_LINEAGE_INCOMPLETE")
            result_by_product = {item.product_id: item for item in selected_run.results}
            for opportunity in current_opportunities:
                result = result_by_product.get(opportunity.product_id)
                checkpoint = checkpoints.get(opportunity.product_id)
                if result is None or checkpoint is None:
                    raise SharedOpportunityError("SHARED_CRYPTO_CURRENT_METHOD_SOURCE_MISSING")
                observed_at = checkpoint.quote_captured_at or checkpoint.status_observed_at
                uncertainty = (
                    None
                    if opportunity.uncertainty_lower is None
                    or opportunity.uncertainty_upper is None
                    else _shared_decimal(
                        opportunity.uncertainty_upper - opportunity.uncertainty_lower
                    )
                )
                fee_ratio = opportunity.fee_bps_per_side * Decimal(2) / Decimal(10000)
                slippage_ratio = opportunity.slippage_bps_per_side * Decimal(2) / Decimal(10000)
                capital = opportunity.required_capital
                visible_depth = opportunity.visible_ask_depth
                values.append(
                    self._reuse_projection(
                        SharedOpportunityProjection.create(
                            projection_id=f"shared-crypto-current-{opportunity.opportunity_id}",
                            venue=SharedVenue.CRYPTO,
                            source_record_type="CryptoResearchOpportunity",
                            source_record_id=opportunity.opportunity_id,
                            source_canonical_hash=opportunity.canonical_hash,
                            strategy_or_method_id=method_id,
                            instrument_id=opportunity.canonical_asset_id,
                            product_or_market_id=opportunity.product_id,
                            direction=(
                                "HYPOTHETICAL_LONG_SPOT_RELATIVE_STRENGTH"
                                if method_id == CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1
                                else "HYPOTHETICAL_LONG_SPOT_REVERSAL_WITHIN_POSITIVE_REGIME"
                            ),
                            generated_at=opportunity.generated_at,
                            valid_until=opportunity.valid_until,
                            evidence_class="GENUINE_PUBLIC",
                            freshness_seconds=_age_seconds(as_of, observed_at),
                            freshness_limit_seconds=PULSE_FRESHNESS_HOURS * 3600,
                            expected_gross_return=opportunity.expected_gross_return,
                            expected_net_return=opportunity.expected_net_return,
                            comparable_value=opportunity.expected_net_return,
                            comparable_value_identity=(
                                "STRICTLY_PRIOR_WALK_FORWARD_EXPECTED_NET_RETURN"
                                if opportunity.expected_net_return is not None
                                else None
                            ),
                            statistical_sample_size=opportunity.calibration_sample_count,
                            uncertainty=uncertainty,
                            maximum_loss=opportunity.maximum_loss,
                            modeled_fees=fee_ratio,
                            modeled_slippage=slippage_ratio,
                            costs_retained=True,
                            liquidity_state=(
                                "SUFFICIENT"
                                if capital is not None
                                and visible_depth is not None
                                and visible_depth >= capital
                                else "INSUFFICIENT"
                            ),
                            liquidity_limitations=opportunity.liquidity_limitations,
                            required_hypothetical_capital=capital,
                            maximum_sensible_research_allocation=(
                                opportunity.maximum_sensible_allocation
                            ),
                            source_maximum_allocation_fraction=(
                                opportunity.maximum_research_allocation
                            ),
                            expected_holding_days=opportunity.hypothetical_holding_days,
                            estimated_lockup_days=opportunity.hypothetical_holding_days,
                            supporting_evidence_ids=tuple(
                                value
                                for value in (
                                    pulse.pulse_id,
                                    selected_run.run_id,
                                    opportunity.supporting_observation_id,
                                    opportunity.supporting_checkpoint_id,
                                    opportunity.calibration_id,
                                )
                                if value is not None
                            ),
                            source_warnings=opportunity.warnings,
                            source_rejection_reasons=(
                                opportunity.rejection_reasons
                                if opportunity.status is OpportunityStatus.REJECTED
                                else ()
                            ),
                            source_authority="NONE",
                            projection_limitations=(
                                "EXPECTED_RETURN_IS_PRIOR_RETAINED_CALIBRATION_NOT_A_FORECAST_GUARANTEE",
                                "OVERLAPPING_CALIBRATION_OUTCOMES_LIMIT_INDEPENDENCE",
                                "DISPLAYED_PUBLIC_DEPTH_IS_NOT_FILLABILITY",
                                "SPOT_DOWNSIDE_ASSUMES_PRICE_CAN_FALL_TO_ZERO",
                            ),
                            source_details=(
                                ("strategy_version", str(opportunity.strategy_version)),
                                ("rank", str(opportunity.rank)),
                                ("score", str(opportunity.gross_research_score)),
                                ("return_7d", str(result.trailing_return_7d or "")),
                                ("return_30d", str(result.trailing_return_30d or "")),
                                ("return_90d", str(result.trailing_return_90d or "")),
                                ("universe_median_90d", str(result.universe_median_90d or "")),
                                ("relative_strength", str(result.relative_strength or "")),
                                ("calibration_bucket", str(result.calibration_bucket or "")),
                                (
                                    "calibration_sample_count",
                                    str(opportunity.calibration_sample_count or 0),
                                ),
                                (
                                    "uncertainty_lower",
                                    str(opportunity.uncertainty_lower or ""),
                                ),
                                (
                                    "uncertainty_upper",
                                    str(opportunity.uncertainty_upper or ""),
                                ),
                                ("current_spread_bps", str(opportunity.current_spread_bps or "")),
                                ("visible_ask_quote_depth", str(visible_depth or "")),
                                ("required_hypothetical_capital", str(capital or "")),
                                ("maximum_loss", str(opportunity.maximum_loss or "")),
                                (
                                    "downside_probability",
                                    str(opportunity.downside_probability or ""),
                                ),
                                ("downside_p05", str(opportunity.downside_p05 or "")),
                                (
                                    "maximum_observed_loss",
                                    str(opportunity.maximum_observed_loss or ""),
                                ),
                                *(
                                    (
                                        f"calibration_horizon_{horizon.outcome_days}d",
                                        "|".join(
                                            (
                                                f"n={horizon.sample_count}",
                                                f"gross={horizon.expected_gross_return}",
                                                f"net={horizon.expected_net_return}",
                                                f"lower={horizon.uncertainty_lower}",
                                                f"upper={horizon.uncertainty_upper}",
                                                f"downside_probability={horizon.downside_probability}",
                                                f"downside_p05={horizon.downside_p05}",
                                            )
                                        ),
                                    )
                                    for horizon in opportunity.calibration_horizons
                                ),
                                ("paper_eligible", "false"),
                                ("live_eligible", "false"),
                            ),
                        )
                    )
                )
        stateful_signal = next(
            (
                item
                for item in reversed(self._stateful_crypto.current_signals())
                if item.source_pulse_id == pulse_id
            ),
            None,
        )
        if stateful_signal is not None:
            stateful_opportunities = tuple(
                item
                for item in self._crypto.opportunities()
                if item.discovery_run_id == stateful_signal.signal_id
            )
            for opportunity in stateful_opportunities:
                checkpoint = checkpoints.get(opportunity.product_id)
                if checkpoint is None:
                    raise SharedOpportunityError("SHARED_CRYPTO_STATEFUL_CHECKPOINT_MISSING")
                observed_at = checkpoint.quote_captured_at or checkpoint.status_observed_at
                capital = opportunity.required_capital
                visible_depth = opportunity.visible_ask_depth
                values.append(
                    self._reuse_projection(
                        SharedOpportunityProjection.create(
                            projection_id=f"shared-crypto-current-{opportunity.opportunity_id}",
                            venue=SharedVenue.CRYPTO,
                            source_record_type="CryptoResearchOpportunity",
                            source_record_id=opportunity.opportunity_id,
                            source_canonical_hash=opportunity.canonical_hash,
                            strategy_or_method_id=(
                                CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1
                            ),
                            instrument_id=opportunity.canonical_asset_id,
                            product_or_market_id=opportunity.product_id,
                            direction="HYPOTHETICAL_LONG_SPOT_STATEFUL_PULLBACK_ROTATION",
                            generated_at=opportunity.generated_at,
                            valid_until=opportunity.valid_until,
                            evidence_class="GENUINE_PUBLIC",
                            freshness_seconds=_age_seconds(as_of, observed_at),
                            freshness_limit_seconds=PULSE_FRESHNESS_HOURS * 3600,
                            expected_gross_return=opportunity.expected_gross_return,
                            expected_net_return=opportunity.expected_net_return,
                            comparable_value=opportunity.expected_net_return,
                            comparable_value_identity=(
                                "STRICTLY_PRIOR_STATEFUL_WALK_FORWARD_EXPECTED_NET_RETURN"
                                if opportunity.expected_net_return is not None
                                else None
                            ),
                            statistical_sample_size=opportunity.calibration_sample_count,
                            uncertainty=(
                                None
                                if opportunity.uncertainty_lower is None
                                or opportunity.uncertainty_upper is None
                                else _shared_decimal(
                                    opportunity.uncertainty_upper - opportunity.uncertainty_lower
                                )
                            ),
                            maximum_loss=opportunity.maximum_loss,
                            modeled_fees=(
                                opportunity.fee_bps_per_side * Decimal(2) / Decimal(10000)
                            ),
                            modeled_slippage=(
                                opportunity.slippage_bps_per_side * Decimal(2) / Decimal(10000)
                            ),
                            costs_retained=True,
                            liquidity_state=(
                                "SUFFICIENT"
                                if capital is not None
                                and visible_depth is not None
                                and visible_depth >= capital
                                else "INSUFFICIENT"
                            ),
                            liquidity_limitations=opportunity.liquidity_limitations,
                            required_hypothetical_capital=capital,
                            maximum_sensible_research_allocation=(
                                opportunity.maximum_sensible_allocation
                            ),
                            source_maximum_allocation_fraction=(
                                opportunity.maximum_research_allocation
                            ),
                            expected_holding_days=opportunity.hypothetical_holding_days,
                            estimated_lockup_days=opportunity.hypothetical_holding_days,
                            supporting_evidence_ids=tuple(
                                value
                                for value in (
                                    pulse.pulse_id,
                                    stateful_signal.signal_id,
                                    stateful_signal.experiment_run_id,
                                    stateful_signal.recommendation_id,
                                    opportunity.supporting_observation_id,
                                    opportunity.supporting_checkpoint_id,
                                )
                                if value is not None
                            ),
                            source_warnings=opportunity.warnings,
                            source_rejection_reasons=(
                                opportunity.rejection_reasons
                                if opportunity.status is OpportunityStatus.REJECTED
                                else ()
                            ),
                            source_authority="NONE",
                            projection_limitations=(
                                "ONE_GENUINE_EXPERIMENT_DOES_NOT_ESTABLISH_EXPECTED_RETURN",
                                "DISPLAYED_PUBLIC_DEPTH_IS_NOT_FILLABILITY",
                                "SPOT_DOWNSIDE_ASSUMES_PRICE_CAN_FALL_TO_ZERO",
                            ),
                            source_details=(
                                ("strategy_version", str(opportunity.strategy_version)),
                                ("rank", str(opportunity.rank)),
                                ("score", str(opportunity.gross_research_score)),
                                ("return_7d", str(opportunity.trailing_return_7d or "")),
                                ("return_90d", str(opportunity.trailing_return_90d or "")),
                                (
                                    "volatility_30d",
                                    str(opportunity.realized_volatility_30d or ""),
                                ),
                                (
                                    "calibration_sample_count",
                                    str(opportunity.calibration_sample_count or 0),
                                ),
                                (
                                    "current_spread_bps",
                                    str(opportunity.current_spread_bps or ""),
                                ),
                                ("visible_ask_quote_depth", str(visible_depth or "")),
                                ("required_hypothetical_capital", str(capital or "")),
                                ("maximum_loss", str(opportunity.maximum_loss or "")),
                                ("paper_eligible", "false"),
                                ("live_eligible", "false"),
                            ),
                        )
                    )
                )
        return tuple(values)

    def _exhaustive_event_set_evaluations(
        self,
    ) -> tuple[ExhaustiveEventSetEvaluation, ...]:
        """Evaluate bounded complete sets from the latest retained threshold refresh."""

        with self._engine.connect() as connection:
            scan = (
                connection.execute(
                    sa.select(threshold_ladder_scan)
                    .where(
                        threshold_ladder_scan.c.state.in_(("COMPLETED", "PARTIAL")),
                        threshold_ladder_scan.c.completed_at.is_not(None),
                    )
                    .order_by(
                        threshold_ladder_scan.c.completed_at.desc(),
                        threshold_ladder_scan.c.scan_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if scan is None:
                return ()
            ingestion_run_id = str(scan["evidence_ingestion_run_id"])
            rows = tuple(
                connection.execute(
                    sa.select(
                        normalized_event_snapshot.c.event_snapshot_id,
                        normalized_event_snapshot.c.provider_event_ticker,
                        normalized_event_snapshot.c.mutually_exclusive,
                        normalized_event_snapshot.c.exhaustiveness_evidence,
                        normalized_event_snapshot.c.nested_market_count,
                        normalized_event_snapshot.c.settlement_source_hash,
                        normalized_event_snapshot.c.canonical_snapshot_hash.label(
                            "event_snapshot_hash"
                        ),
                        normalized_event_snapshot.c.observed_at.label("event_observed_at"),
                        event_market_relationship,
                        normalized_market_snapshot.c.provider_market_ticker,
                        normalized_market_snapshot.c.event_ticker,
                        normalized_market_snapshot.c.rules_primary,
                        normalized_market_snapshot.c.rules_secondary,
                        normalized_market_snapshot.c.status.label("market_status"),
                        normalized_market_snapshot.c.close_time,
                        normalized_market_snapshot.c.latest_expiration_time,
                        normalized_market_snapshot.c.rule_content_hash,
                        normalized_market_snapshot.c.observed_at.label("market_observed_at"),
                    )
                    .join(
                        event_market_relationship,
                        event_market_relationship.c.event_snapshot_id
                        == normalized_event_snapshot.c.event_snapshot_id,
                    )
                    .join(
                        normalized_market_snapshot,
                        normalized_market_snapshot.c.snapshot_id
                        == event_market_relationship.c.market_snapshot_id,
                    )
                    .where(normalized_event_snapshot.c.ingestion_run_id == ingestion_run_id)
                    .order_by(
                        normalized_event_snapshot.c.provider_event_ticker,
                        normalized_market_snapshot.c.provider_market_ticker,
                        normalized_event_snapshot.c.observed_at.desc(),
                    )
                ).mappings()
            )
            by_event: dict[str, list[RowMapping]] = {}
            for row in rows:
                event_ticker = row["provider_event_ticker"]
                market_ticker = row["provider_market_ticker"]
                if event_ticker is None or market_ticker is None:
                    continue
                by_event.setdefault(str(event_ticker), []).append(row)
            evaluations: list[ExhaustiveEventSetEvaluation] = []
            for event_ticker, event_rows in sorted(by_event.items())[:100]:
                event_snapshot_rows: dict[str, RowMapping] = {}
                market_rows: dict[str, RowMapping] = {}
                for row in event_rows:
                    event_snapshot_rows.setdefault(str(row["event_snapshot_id"]), row)
                    market_rows.setdefault(str(row["provider_market_ticker"]), row)
                legs: list[ExhaustiveEventSetLegInput] = []
                for market_ticker, row in sorted(market_rows.items()):
                    book = (
                        connection.execute(
                            sa.select(market_orderbook_snapshot)
                            .where(
                                market_orderbook_snapshot.c.ingestion_run_id == ingestion_run_id,
                                market_orderbook_snapshot.c.provider_market_ticker == market_ticker,
                            )
                            .order_by(
                                market_orderbook_snapshot.c.observed_at.desc(),
                                market_orderbook_snapshot.c.orderbook_snapshot_id.desc(),
                            )
                            .limit(1)
                        )
                        .mappings()
                        .one_or_none()
                    )
                    level = None
                    fee = None
                    if book is not None:
                        level = (
                            connection.execute(
                                sa.select(market_orderbook_level)
                                .where(
                                    market_orderbook_level.c.orderbook_snapshot_id
                                    == book["orderbook_snapshot_id"],
                                    market_orderbook_level.c.derived_ask_side == "YES",
                                )
                                .order_by(
                                    market_orderbook_level.c.derived_ask_price,
                                    market_orderbook_level.c.ordinal,
                                )
                                .limit(1)
                            )
                            .mappings()
                            .one_or_none()
                        )
                        fee_row = (
                            connection.execute(
                                sa.select(series_fee_snapshot).where(
                                    series_fee_snapshot.c.ingestion_run_id == ingestion_run_id,
                                    series_fee_snapshot.c.series_ticker == book["series_ticker"],
                                )
                            )
                            .mappings()
                            .one_or_none()
                        )
                        if fee_row is not None:
                            fee = SeriesFeeSnapshot(
                                fee_snapshot_id=str(fee_row["fee_snapshot_id"]),
                                ingestion_run_id=str(fee_row["ingestion_run_id"]),
                                raw_response_id=str(fee_row["raw_response_id"]),
                                provider=PublicMarketProvider(str(fee_row["provider"])),
                                series_ticker=str(fee_row["series_ticker"]),
                                observed_at=cast(datetime, fee_row["observed_at"]),
                                fee_type=cast(str | None, fee_row["fee_type"]),
                                fee_multiplier=(
                                    None
                                    if fee_row["fee_multiplier"] is None
                                    else _canonical_db_decimal(
                                        cast(Decimal, fee_row["fee_multiplier"])
                                    )
                                ),
                                effective_at=cast(datetime | None, fee_row["effective_at"]),
                                fee_model_identity=str(fee_row["fee_model_identity"]),
                                taker_coefficient=_canonical_db_decimal(
                                    cast(Decimal, fee_row["taker_coefficient"])
                                ),
                                maker_coefficient=_canonical_db_decimal(
                                    cast(Decimal, fee_row["maker_coefficient"])
                                ),
                                rounding_identity=str(fee_row["rounding_identity"]),
                                schedule_identity=str(fee_row["schedule_identity"]),
                                schedule_effective_date=str(fee_row["schedule_effective_date"]),
                                source_hash=ContentHash(str(fee_row["source_hash"])),
                            )
                    legs.append(
                        ExhaustiveEventSetLegInput(
                            market_snapshot_id=str(row["market_snapshot_id"]),
                            market_ticker=market_ticker,
                            event_ticker=str(row["event_ticker"] or event_ticker),
                            relationship_id=str(row["relationship_id"]),
                            relationship_state=EventRelationshipState(
                                str(row["relationship_state"])
                            ),
                            rule_content_hash=cast(str | None, row["rule_content_hash"]),
                            rules_primary=cast(str | None, row["rules_primary"]),
                            rules_secondary=cast(str | None, row["rules_secondary"]),
                            market_status=cast(str | None, row["market_status"]),
                            close_at=cast(datetime | None, row["close_time"]),
                            settlement_at=cast(datetime | None, row["latest_expiration_time"]),
                            orderbook_snapshot_id=(
                                None if book is None else str(book["orderbook_snapshot_id"])
                            ),
                            orderbook_hash=(
                                None if book is None else str(book["canonical_orderbook_hash"])
                            ),
                            orderbook_observed_at=(
                                None if book is None else cast(datetime, book["observed_at"])
                            ),
                            orderbook_quality=(
                                None if book is None else str(book["data_quality_state"])
                            ),
                            yes_ask=(
                                None
                                if level is None
                                else _canonical_db_decimal(
                                    cast(Decimal, level["derived_ask_price"])
                                )
                            ),
                            visible_yes_quantity=(
                                Decimal(0)
                                if level is None
                                else _canonical_db_decimal(cast(Decimal, level["quantity"]))
                            ),
                            fee=fee,
                        )
                    )
                event_values = tuple(event_snapshot_rows.values())
                evaluations.append(
                    evaluate_exhaustive_event_set(
                        ExhaustiveEventSetInput(
                            scan_id=str(scan["scan_id"]),
                            event_ticker=event_ticker,
                            event_snapshot_ids=tuple(
                                sorted(str(row["event_snapshot_id"]) for row in event_values)
                            ),
                            event_snapshot_hashes=tuple(
                                sorted(str(row["event_snapshot_hash"]) for row in event_values)
                            ),
                            mutually_exclusive_values=tuple(
                                cast(bool | None, row["mutually_exclusive"]) for row in event_values
                            ),
                            exhaustiveness_values=tuple(
                                EventEvidenceState(str(row["exhaustiveness_evidence"]))
                                for row in event_values
                            ),
                            nested_market_counts=tuple(
                                int(row["nested_market_count"]) for row in event_values
                            ),
                            settlement_source_hashes=tuple(
                                str(row["settlement_source_hash"]) for row in event_values
                            ),
                            legs=tuple(legs),
                        ),
                        evaluated_at=cast(datetime, scan["completed_at"]),
                    )
                )
        return tuple(evaluations)

    def exhaustive_event_set_evaluations(
        self,
    ) -> tuple[ExhaustiveEventSetEvaluation, ...]:
        """Expose deterministic retained-source evaluations to operating reports."""

        return self._exhaustive_event_set_evaluations()

    def _exhaustive_event_set_projections(
        self, *, as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        values: list[SharedOpportunityProjection] = []
        for evaluation in self._exhaustive_event_set_evaluations():
            cost = _shared_decimal(evaluation.total_modeled_cost)
            gap = _shared_decimal(evaluation.conservative_net_gap)
            comparable = None if cost <= 0 else _shared_decimal(gap / cost)
            lockup_days = (
                None
                if evaluation.settlement_at is None
                else max(
                    0,
                    int(
                        (evaluation.settlement_at - evaluation.evaluated_at).total_seconds() / 86400
                    ),
                )
            )
            leg_details = tuple(
                (
                    f"yes_leg_{ordinal}",
                    "|".join(
                        (
                            f"market={leg.market_ticker}",
                            f"ask={leg.yes_ask}",
                            f"visible_quantity={leg.visible_quantity}",
                            f"matched_quantity={leg.matched_quantity}",
                            f"cost={leg.acquisition_cost}",
                            f"fee={leg.modeled_fee}",
                            f"rounding={leg.rounding_reserve}",
                        )
                    ),
                )
                for ordinal, leg in enumerate(evaluation.legs, 1)
            )
            values.append(
                self._reuse_projection(
                    SharedOpportunityProjection.create(
                        projection_id=f"shared-kalshi-exhaustive-{evaluation.evaluation_id}",
                        venue=SharedVenue.KALSHI,
                        source_record_type="ExhaustiveEventSetEvaluation",
                        source_record_id=evaluation.evaluation_id,
                        source_canonical_hash=evaluation.canonical_hash,
                        strategy_or_method_id=KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1,
                        instrument_id=evaluation.event_ticker,
                        product_or_market_id=f"{evaluation.event_ticker}:COMPLETE_SET",
                        direction="BUY_ONE_YES_IN_EVERY_EXHAUSTIVE_OUTCOME",
                        generated_at=evaluation.evaluated_at,
                        valid_until=evaluation.expires_at,
                        evidence_class="GENUINE_PUBLIC",
                        freshness_seconds=_age_seconds(as_of, evaluation.evaluated_at),
                        freshness_limit_seconds=KALSHI_SHARED_FRESHNESS_SECONDS,
                        expected_gross_return=None,
                        expected_net_return=None,
                        comparable_value=comparable,
                        comparable_value_identity=(
                            None
                            if comparable is None
                            else "GUARANTEED_SETTLEMENT_NET_GAP_OVER_MODELED_COST"
                        ),
                        uncertainty=None,
                        maximum_loss=None if cost <= 0 else evaluation.maximum_loss,
                        modeled_fees=_shared_decimal(
                            evaluation.modeled_fees + evaluation.rounding_reserve
                        ),
                        modeled_slippage=None,
                        costs_retained=True,
                        liquidity_state=(
                            "SUFFICIENT" if evaluation.matched_quantity > 0 else "INSUFFICIENT"
                        ),
                        liquidity_limitations=(
                            "DISPLAYED_DEPTH_IS_NOT_FILLABILITY",
                            "MULTI_LEG_EXECUTION_IS_NOT_ATOMIC",
                            "NO_SLIPPAGE_MODEL_BEYOND_RETAINED_DEPTH",
                        ),
                        required_hypothetical_capital=None if cost <= 0 else cost,
                        maximum_sensible_research_allocation=None if cost <= 0 else cost,
                        source_maximum_allocation_fraction=None,
                        expected_holding_days=lockup_days,
                        estimated_lockup_days=lockup_days,
                        supporting_evidence_ids=evaluation.evidence_lineage,
                        source_warnings=(
                            "KALSHI_PUBLIC_EVIDENCE_ONLY",
                            "PROBABILITIES_NOT_INVENTED",
                            "COMPLETE_SET_GUARANTEE_REQUIRES_EXACT_RETAINED_RULES",
                            "NO_AUTOMATIC_PAPER_ACTION",
                        ),
                        source_rejection_reasons=(
                            () if evaluation.source_eligible else evaluation.rejection_reasons
                        ),
                        source_authority="NONE",
                        projection_limitations=(
                            "VISIBLE_ORDERBOOK_ONLY",
                            "MULTI_LEG_EXECUTION_RISK_REMAINS",
                            "NO_EXECUTION_AUTHORITY",
                        ),
                        source_details=(
                            ("method_version", "1"),
                            ("mechanism", "EXHAUSTIVE_YES_COMPLETE_SET"),
                            ("event_set_market_count", str(len(evaluation.legs))),
                            (
                                "mutually_exclusive_verified",
                                str(evaluation.mutually_exclusive_verified).lower(),
                            ),
                            (
                                "exhaustiveness_verified",
                                str(evaluation.exhaustiveness_verified).lower(),
                            ),
                            ("rules_verified", str(evaluation.rules_verified).lower()),
                            ("timing_verified", str(evaluation.timing_verified).lower()),
                            ("matched_quantity", str(evaluation.matched_quantity)),
                            ("gross_package_cost", str(evaluation.gross_package_cost)),
                            ("modeled_fees", str(evaluation.modeled_fees)),
                            ("rounding_reserve", str(evaluation.rounding_reserve)),
                            ("total_modeled_cost", str(evaluation.total_modeled_cost)),
                            (
                                "guaranteed_settlement_payout",
                                str(evaluation.guaranteed_settlement_payout),
                            ),
                            ("conservative_net_gap", str(evaluation.conservative_net_gap)),
                            ("maximum_loss", str(evaluation.maximum_loss)),
                            ("maximum_payoff", str(evaluation.maximum_payoff)),
                            ("close_at", str(evaluation.close_at or "")),
                            ("settlement_at", str(evaluation.settlement_at or "")),
                            ("paper_eligible", "false"),
                            ("live_eligible", "false"),
                            *leg_details,
                        ),
                    )
                )
            )
        return tuple(values)

    def _related_market_projections(
        self, *, as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        """Project the final round of the latest retained threshold-ladder scan."""

        broader_market = normalized_market_snapshot.alias("related_broader_market")
        narrower_market = normalized_market_snapshot.alias("related_narrower_market")
        broader_book = market_orderbook_snapshot.alias("related_broader_book")
        narrower_book = market_orderbook_snapshot.alias("related_narrower_book")
        with self._engine.connect() as connection:
            scan = (
                connection.execute(
                    sa.select(threshold_ladder_scan)
                    .where(
                        threshold_ladder_scan.c.state.in_(("COMPLETED", "PARTIAL")),
                        threshold_ladder_scan.c.completed_at.is_not(None),
                    )
                    .order_by(
                        threshold_ladder_scan.c.completed_at.desc(),
                        threshold_ladder_scan.c.scan_id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if scan is None:
                return ()
            rows = tuple(
                connection.execute(
                    sa.select(
                        threshold_bundle_evaluation,
                        threshold_market_relationship,
                        broader_market.c.title.label("broader_title"),
                        broader_market.c.status.label("broader_status"),
                        broader_market.c.close_time.label("broader_close_time"),
                        broader_market.c.latest_expiration_time.label("broader_expiration_time"),
                        broader_market.c.canonical_snapshot_hash.label("broader_market_hash"),
                        narrower_market.c.title.label("narrower_title"),
                        narrower_market.c.status.label("narrower_status"),
                        narrower_market.c.close_time.label("narrower_close_time"),
                        narrower_market.c.latest_expiration_time.label("narrower_expiration_time"),
                        narrower_market.c.canonical_snapshot_hash.label("narrower_market_hash"),
                        broader_book.c.canonical_orderbook_hash.label("broader_book_hash"),
                        broader_book.c.data_quality_state.label("broader_book_quality"),
                        narrower_book.c.canonical_orderbook_hash.label("narrower_book_hash"),
                        narrower_book.c.data_quality_state.label("narrower_book_quality"),
                    )
                    .join(
                        threshold_market_relationship,
                        threshold_market_relationship.c.relationship_id
                        == threshold_bundle_evaluation.c.relationship_id,
                    )
                    .join(
                        broader_market,
                        broader_market.c.snapshot_id
                        == threshold_market_relationship.c.broader_market_snapshot_id,
                    )
                    .join(
                        narrower_market,
                        narrower_market.c.snapshot_id
                        == threshold_market_relationship.c.narrower_market_snapshot_id,
                    )
                    .outerjoin(
                        broader_book,
                        broader_book.c.orderbook_snapshot_id
                        == threshold_bundle_evaluation.c.broader_orderbook_snapshot_id,
                    )
                    .outerjoin(
                        narrower_book,
                        narrower_book.c.orderbook_snapshot_id
                        == threshold_bundle_evaluation.c.narrower_orderbook_snapshot_id,
                    )
                    .where(threshold_bundle_evaluation.c.scan_id == scan["scan_id"])
                    .order_by(
                        threshold_bundle_evaluation.c.relationship_id,
                        threshold_bundle_evaluation.c.round_ordinal.desc(),
                    )
                ).mappings()
            )
            latest: dict[str, RowMapping] = {}
            for row in rows:
                latest.setdefault(str(row["relationship_id"]), row)
            values: list[SharedOpportunityProjection] = []
            for relationship_id, row in sorted(latest.items()):
                evaluation_id = str(row["evaluation_id"])
                slices = tuple(
                    connection.execute(
                        sa.select(threshold_matched_slice)
                        .where(threshold_matched_slice.c.evaluation_id == evaluation_id)
                        .order_by(threshold_matched_slice.c.ordinal)
                    ).mappings()
                )
                observed = tuple(
                    cast(datetime, value)
                    for value in (row["broader_observed_at"], row["narrower_observed_at"])
                    if value is not None
                )
                generated_at = max(observed) if observed else cast(datetime, row["created_at"])
                expiry_values = tuple(
                    cast(datetime, value)
                    for value in (
                        row["broader_expiration_time"] or row["broader_close_time"],
                        row["narrower_expiration_time"] or row["narrower_close_time"],
                    )
                    if value is not None
                )
                valid_until = min(expiry_values) if expiry_values else None
                lockup_days = (
                    None
                    if valid_until is None
                    else max(0, int((valid_until - generated_at).total_seconds() / 86400))
                )
                quantity = _shared_decimal(cast(Decimal, row["matched_quantity"]))
                cost = _shared_decimal(cast(Decimal, row["total_modeled_acquisition_cost"]))
                gross = _shared_decimal(cast(Decimal, row["gross_lower_bound_value"]))
                net = _shared_decimal(cast(Decimal, row["conservative_net_lower_bound"]))
                fees = _shared_decimal(
                    cast(Decimal, row["total_modeled_fee"])
                    + cast(Decimal, row["conservative_rounding_reserve"])
                )
                state = str(row["state"])
                relationship_state = str(row["state_1"])
                reasons = tuple(
                    sorted(
                        {
                            *(str(item) for item in row["reason_codes"]),
                            *(str(item) for item in row["reason_codes_1"]),
                        }
                    )
                )
                source_rejections = tuple(
                    reason
                    for reason in (
                        *(() if state == "CANDIDATE" else (f"SOURCE_STATE_{state}",)),
                        *(
                            ()
                            if relationship_state == "VERIFIED"
                            else (f"RELATIONSHIP_STATE_{relationship_state}",)
                        ),
                        *reasons,
                    )
                )
                comparable = None if cost <= 0 else _shared_decimal(net / cost)
                slice_details = tuple(
                    (
                        f"matched_slice_{item['ordinal']}",
                        "|".join(
                            (
                                f"quantity={item['quantity']}",
                                f"broader_yes_ask={item['broader_yes_ask']}",
                                f"narrower_no_ask={item['narrower_no_ask']}",
                                f"broader_fee={item['broader_fee']}",
                                f"narrower_fee={item['narrower_fee']}",
                                f"lower_bound={item['conservative_lower_bound']}",
                            )
                        ),
                    )
                    for item in slices
                )
                values.append(
                    self._reuse_projection(
                        SharedOpportunityProjection.create(
                            projection_id=f"shared-kalshi-related-{evaluation_id}",
                            venue=SharedVenue.KALSHI,
                            source_record_type="ThresholdBundleEvaluation",
                            source_record_id=evaluation_id,
                            source_canonical_hash=str(row["canonical_evaluation_hash"]),
                            strategy_or_method_id=KALSHI_RELATED_MARKET_CONSISTENCY_V1,
                            instrument_id=str(row["provider_event_ticker"]),
                            product_or_market_id=(
                                f"{row['broader_market_ticker']}+{row['narrower_market_ticker']}"
                            ),
                            direction="BUY_BROADER_YES_AND_NARROWER_NO",
                            generated_at=generated_at,
                            valid_until=valid_until,
                            evidence_class="GENUINE_PUBLIC",
                            freshness_seconds=_age_seconds(as_of, generated_at),
                            freshness_limit_seconds=KALSHI_SHARED_FRESHNESS_SECONDS,
                            expected_gross_return=None,
                            expected_net_return=None,
                            comparable_value=comparable,
                            comparable_value_identity=(
                                None
                                if comparable is None
                                else "CONSERVATIVE_MODEL_FREE_NET_LOWER_BOUND_OVER_COST"
                            ),
                            uncertainty=None,
                            maximum_loss=None if cost <= 0 else cost,
                            modeled_fees=fees,
                            modeled_slippage=None,
                            costs_retained=True,
                            liquidity_state=(
                                "SUFFICIENT"
                                if quantity > 0
                                and row["broader_book_quality"] == "VALID"
                                and row["narrower_book_quality"] == "VALID"
                                else "INSUFFICIENT"
                            ),
                            liquidity_limitations=(
                                "DISPLAYED_DEPTH_IS_NOT_FILLABILITY",
                                "NO_SLIPPAGE_MODEL_BEYOND_RETAINED_DEPTH",
                                "MULTI_LEG_EXECUTION_IS_NOT_ATOMIC",
                            ),
                            required_hypothetical_capital=None if cost <= 0 else cost,
                            maximum_sensible_research_allocation=None if cost <= 0 else cost,
                            source_maximum_allocation_fraction=None,
                            expected_holding_days=lockup_days,
                            estimated_lockup_days=lockup_days,
                            supporting_evidence_ids=tuple(
                                value
                                for value in (
                                    str(scan["scan_id"]),
                                    relationship_id,
                                    evaluation_id,
                                    str(row["broader_market_snapshot_id"]),
                                    str(row["narrower_market_snapshot_id"]),
                                    cast(str | None, row["broader_orderbook_snapshot_id"]),
                                    cast(str | None, row["narrower_orderbook_snapshot_id"]),
                                    cast(str | None, row["broader_fee_snapshot_id"]),
                                    cast(str | None, row["narrower_fee_snapshot_id"]),
                                    str(row["broader_market_hash"]),
                                    str(row["narrower_market_hash"]),
                                    cast(str | None, row["broader_book_hash"]),
                                    cast(str | None, row["narrower_book_hash"]),
                                    *(str(item["canonical_slice_hash"]) for item in slices),
                                )
                                if value
                            ),
                            source_warnings=(
                                "KALSHI_PUBLIC_EVIDENCE_ONLY",
                                "SETTLEMENT_RELATIONSHIP_IS_RULE_VERIFIED_NOT_PROBABILISTIC",
                                "DISPLAYED_DEPTH_DOES_NOT_PROVE_FILLABILITY",
                            ),
                            source_rejection_reasons=source_rejections,
                            source_authority="NONE",
                            projection_limitations=(
                                "EXPECTED_RETURN_AND_PROBABILITY_NOT_INVENTED",
                                "VISIBLE_ORDERBOOK_ONLY",
                                "MULTI_LEG_EXECUTION_RISK_REMAINS",
                                "NO_EXECUTION_AUTHORITY",
                            ),
                            source_details=(
                                ("scan_id", str(scan["scan_id"])),
                                ("relationship_id", relationship_id),
                                ("relationship_state", relationship_state),
                                ("threshold_direction", str(row["threshold_direction"] or "")),
                                ("broader_market", str(row["broader_market_ticker"])),
                                ("broader_title", str(row["broader_title"] or "")),
                                ("broader_strike", str(row["broader_strike"] or "")),
                                ("broader_side", "BUY_YES"),
                                (
                                    "broader_weighted_ask",
                                    str(row["broader_yes_weighted_ask"] or ""),
                                ),
                                ("broader_cost", str(row["broader_cost"])),
                                ("narrower_market", str(row["narrower_market_ticker"])),
                                ("narrower_title", str(row["narrower_title"] or "")),
                                ("narrower_strike", str(row["narrower_strike"] or "")),
                                ("narrower_side", "BUY_NO"),
                                (
                                    "narrower_weighted_ask",
                                    str(row["narrower_no_weighted_ask"] or ""),
                                ),
                                ("narrower_cost", str(row["narrower_cost"])),
                                ("matched_quantity", str(quantity)),
                                ("gross_acquisition_cost", str(row["gross_acquisition_cost"])),
                                ("gross_settlement_floor", str(gross)),
                                ("modeled_fees_and_rounding", str(fees)),
                                ("total_modeled_acquisition_cost", str(cost)),
                                ("conservative_net_lower_bound", str(net)),
                                ("maximum_loss", str(cost)),
                                ("maximum_payoff_floor", str(gross)),
                                ("estimated_capital_lockup_days", str(lockup_days or "")),
                                ("probability_model", "NONE"),
                                ("paper_eligible", "false"),
                                ("live_eligible", "false"),
                                *slice_details,
                            ),
                        )
                    )
                )
        return tuple(values)

    def kalshi_projections(
        self, *, evaluation_ids: tuple[str, ...], as_of: datetime
    ) -> tuple[SharedOpportunityProjection, ...]:
        if len(set(evaluation_ids)) != len(evaluation_ids):
            raise SharedOpportunityError("SHARED_KALSHI_EVALUATION_ID_DUPLICATE")
        values: list[SharedOpportunityProjection] = []
        with self._engine.connect() as connection:
            for evaluation_id in evaluation_ids:
                row = (
                    connection.execute(
                        sa.select(
                            binary_complement_evaluation,
                            normalized_market_snapshot.c.event_ticker,
                            normalized_market_snapshot.c.title,
                            normalized_market_snapshot.c.status.label("market_status"),
                            normalized_market_snapshot.c.close_time,
                            normalized_market_snapshot.c.latest_expiration_time,
                            normalized_market_snapshot.c.yes_bid,
                            normalized_market_snapshot.c.yes_ask,
                            normalized_market_snapshot.c.no_bid,
                            normalized_market_snapshot.c.no_ask,
                            normalized_market_snapshot.c.rule_content_hash,
                            normalized_market_snapshot.c.data_quality_state.label("market_quality"),
                            market_orderbook_snapshot.c.data_quality_state.label(
                                "orderbook_quality"
                            ),
                            market_orderbook_snapshot.c.canonical_orderbook_hash,
                        )
                        .join(
                            normalized_market_snapshot,
                            normalized_market_snapshot.c.snapshot_id
                            == binary_complement_evaluation.c.market_snapshot_id,
                        )
                        .join(
                            market_orderbook_snapshot,
                            market_orderbook_snapshot.c.orderbook_snapshot_id
                            == binary_complement_evaluation.c.orderbook_snapshot_id,
                        )
                        .where(binary_complement_evaluation.c.evaluation_id == evaluation_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise SharedOpportunityError("SHARED_KALSHI_EVALUATION_NOT_FOUND")
                evaluated_at = cast(datetime, row["evaluated_at"])
                close_time = cast(datetime | None, row["close_time"])
                expiry = cast(datetime | None, row["latest_expiration_time"]) or close_time
                quantity = _shared_decimal(cast(Decimal, row["matched_quantity"]))
                cost = _shared_decimal(cast(Decimal, row["total_modeled_acquisition_cost"]))
                fees = _shared_decimal(
                    cast(Decimal, row["modeled_taker_fees"])
                    + cast(Decimal, row["conservative_rounding_reserve"])
                )
                net = _shared_decimal(cast(Decimal, row["conservative_net_lower_bound"]))
                reasons = tuple(str(item) for item in row["reason_codes"])
                state = str(row["state"])
                lockup_days = (
                    None
                    if expiry is None
                    else max(0, int((expiry - evaluated_at).total_seconds() / 86400))
                )
                comparable = None if cost <= 0 else net / cost
                weighted_yes = cast(Decimal | None, row["weighted_yes_ask"])
                weighted_no = cast(Decimal | None, row["weighted_no_ask"])
                source_details = (
                    ("event_ticker", str(row["event_ticker"] or "")),
                    ("market_ticker", str(row["provider_market_ticker"])),
                    ("market_title", str(row["title"] or "")),
                    ("yes_side", "BUY_YES"),
                    ("no_side", "BUY_NO"),
                    ("weighted_yes_ask", "" if weighted_yes is None else str(weighted_yes)),
                    ("weighted_no_ask", "" if weighted_no is None else str(weighted_no)),
                    ("gross_settlement_value", str(row["gross_settlement_value"])),
                    ("current_yes_ask", "" if row["yes_ask"] is None else str(row["yes_ask"])),
                    ("current_no_ask", "" if row["no_ask"] is None else str(row["no_ask"])),
                    ("current_yes_bid", "" if row["yes_bid"] is None else str(row["yes_bid"])),
                    ("current_no_bid", "" if row["no_bid"] is None else str(row["no_bid"])),
                    ("visible_contract_count", str(quantity)),
                    ("gross_cost", str(cost)),
                    ("modeled_fees", str(fees)),
                    ("maximum_loss", str(cost)),
                    ("maximum_payoff", str(row["gross_settlement_value"])),
                    ("required_hypothetical_capital", str(cost)),
                    ("market_close", "" if close_time is None else close_time.isoformat()),
                    ("expiration", "" if expiry is None else expiry.isoformat()),
                    (
                        "estimated_capital_lockup_days",
                        "" if lockup_days is None else str(lockup_days),
                    ),
                    ("freshness_seconds", str(_age_seconds(as_of, evaluated_at))),
                    ("rule_content_hash", str(row["rule_content_hash"] or "")),
                    ("market_status", str(row["market_status"] or "")),
                    ("market_quality", str(row["market_quality"])),
                    ("orderbook_quality", str(row["orderbook_quality"])),
                )
                values.append(
                    self._reuse_projection(
                        SharedOpportunityProjection.create(
                            projection_id=f"shared-kalshi-{evaluation_id}",
                            venue=SharedVenue.KALSHI,
                            source_record_type="BinaryComplementEvaluation",
                            source_record_id=evaluation_id,
                            source_canonical_hash=str(row["canonical_evaluation_hash"]),
                            strategy_or_method_id=str(row["evaluator_version"]),
                            instrument_id=str(row["event_ticker"] or row["provider_market_ticker"]),
                            product_or_market_id=str(row["provider_market_ticker"]),
                            direction="BUY_YES_AND_NO_COMPLEMENT",
                            generated_at=evaluated_at,
                            valid_until=expiry,
                            evidence_class="GENUINE_PUBLIC",
                            freshness_seconds=_age_seconds(as_of, evaluated_at),
                            freshness_limit_seconds=KALSHI_SHARED_FRESHNESS_SECONDS,
                            expected_gross_return=None,
                            expected_net_return=None,
                            comparable_value=comparable,
                            comparable_value_identity=(
                                None
                                if comparable is None
                                else "CONSERVATIVE_NET_LOWER_BOUND_OVER_MODELED_COST"
                            ),
                            uncertainty=None,
                            maximum_loss=None if cost <= 0 else cost,
                            modeled_fees=fees,
                            modeled_slippage=None,
                            costs_retained=True,
                            liquidity_state=(
                                "SUFFICIENT"
                                if quantity > 0 and str(row["orderbook_quality"]) == "VALID"
                                else "INSUFFICIENT"
                            ),
                            liquidity_limitations=(
                                "DISPLAYED_DEPTH_IS_NOT_FILLABILITY",
                                "NO_SLIPPAGE_MODEL_BEYOND_RETAINED_DEPTH",
                            ),
                            required_hypothetical_capital=None if cost <= 0 else cost,
                            maximum_sensible_research_allocation=None if cost <= 0 else cost,
                            source_maximum_allocation_fraction=None,
                            expected_holding_days=lockup_days,
                            estimated_lockup_days=lockup_days,
                            supporting_evidence_ids=tuple(
                                value
                                for value in (
                                    str(row["ingestion_run_id"]),
                                    str(row["market_snapshot_id"]),
                                    str(row["orderbook_snapshot_id"]),
                                    cast(str | None, row["fee_snapshot_id"]),
                                    str(row["canonical_orderbook_hash"]),
                                )
                                if value
                            ),
                            source_warnings=(
                                "KALSHI_PUBLIC_EVIDENCE_ONLY",
                                "DISPLAYED_DEPTH_DOES_NOT_PROVE_FILLABILITY",
                            ),
                            source_rejection_reasons=(
                                () if state == "CANDIDATE" else (f"SOURCE_STATE_{state}", *reasons)
                            ),
                            source_authority="NONE",
                            projection_limitations=(
                                "EXPECTED_RETURN_AND_PROBABILITY_NOT_RETAINED",
                                "VISIBLE_ORDERBOOK_ONLY",
                                "NO_EXECUTION_AUTHORITY",
                            ),
                            source_details=source_details,
                        )
                    )
                )
        return (
            *values,
            *self._related_market_projections(as_of=as_of),
            *self._exhaustive_event_set_projections(as_of=as_of),
        )
