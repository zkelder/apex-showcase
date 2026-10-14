"""Pure, reusable rules for one-position stateful Crypto research.

The module accepts retained daily open/close evidence and current public market
quality evidence.  It has no provider, persistence, PAPER, or financial
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum

from apex.domain.crypto_universe import (
    FEE_BPS_PER_SIDE,
    SLIPPAGE_BPS_PER_SIDE,
    CryptoCurrentMarketEvidence,
    CryptoProductObservation,
    CryptoResearchOpportunity,
    OpportunityStatus,
    metric,
    reversal_regime_score,
)
from apex.domain.research_operations import ResearchProposal
from apex.domain.values import content_digest, parse_decimal

CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1 = (
    "CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1"
)
CRYPTO_STATEFUL_RESEARCH_EXECUTOR_V1 = "CRYPTO_STATEFUL_RESEARCH_EXECUTOR_V1"
CRYPTO_STATEFUL_REFINEMENT_POLICY_V1 = "CRYPTO_STATEFUL_REFINEMENT_POLICY_V1"
CRYPTO_STATEFUL_PULLBACK_THRESHOLD = Decimal("-0.03")
CRYPTO_STATEFUL_MAXIMUM_HOLD_DAYS = 14
CRYPTO_STATEFUL_RESEARCH_NOTIONAL = Decimal("10000")
CRYPTO_STATEFUL_FRESHNESS_SECONDS = 36 * 3600
CRYPTO_STATEFUL_MINIMUM_DEPTH = Decimal("1000")


class CryptoStatefulResearchError(ValueError):
    """The bounded stateful research contract failed closed."""


class CryptoState(StrEnum):
    CASH = "CASH"
    LONG = "LONG"


class CryptoStatefulAction(StrEnum):
    CASH = "CASH"
    ENTER = "ENTER"
    HOLD = "HOLD"
    EXIT = "EXIT"


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None:
        raise CryptoStatefulResearchError(reason)
    return value.astimezone(UTC)


def _identity(value: str, reason: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 300:
        raise CryptoStatefulResearchError(reason)
    return normalized


def _decimal(value: object, reason: str, *, positive: bool = False) -> Decimal:
    try:
        parsed = parse_decimal(value, allow_negative=not positive)  # type: ignore[arg-type]
    except (TypeError, ValueError, ArithmeticError) as error:
        raise CryptoStatefulResearchError(reason) from error
    if positive and parsed <= 0:
        raise CryptoStatefulResearchError(reason)
    return parsed


def _median(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        raise CryptoStatefulResearchError("CRYPTO_STATEFUL_MEDIAN_INPUT_EMPTY")
    ordered = tuple(sorted(values))
    middle = len(ordered) // 2
    return metric(
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / Decimal(2)
    )


def _volatility(closes: tuple[Decimal, ...]) -> Decimal:
    if len(closes) != 31:
        raise CryptoStatefulResearchError("CRYPTO_STATEFUL_VOLATILITY_WINDOW_INVALID")
    returns = tuple(closes[index] / closes[index - 1] - 1 for index in range(1, 31))
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        mean = sum(returns, Decimal()) / Decimal(len(returns))
        variance = sum(((value - mean) ** 2 for value in returns), Decimal()) / Decimal(
            len(returns) - 1
        )
        return metric(variance.sqrt() * Decimal(365).sqrt())


@dataclass(frozen=True, slots=True)
class CryptoExecutionCandle:
    bucket_start: datetime
    open: Decimal
    close: Decimal
    canonical_hash: str

    @classmethod
    def create(
        cls, *, bucket_start: datetime, open_price: object, close_price: object
    ) -> CryptoExecutionCandle:
        timestamp = _time(bucket_start, "CRYPTO_EXECUTION_CANDLE_TIME_INVALID")
        if timestamp.hour or timestamp.minute or timestamp.second or timestamp.microsecond:
            raise CryptoStatefulResearchError("CRYPTO_EXECUTION_CANDLE_BUCKET_INVALID")
        opening = _decimal(open_price, "CRYPTO_EXECUTION_CANDLE_OPEN_INVALID", positive=True)
        close = _decimal(close_price, "CRYPTO_EXECUTION_CANDLE_CLOSE_INVALID", positive=True)
        values = (timestamp, opening, close)
        return cls(*values, content_digest(values))


@dataclass(frozen=True, slots=True)
class CryptoExecutionHistory:
    history_id: str
    canonical_asset_id: str
    product_id: str
    source_observation_id: str
    source_observation_hash: str
    interval_start: datetime
    interval_end: datetime
    candles: tuple[CryptoExecutionCandle, ...]
    raw_evidence_hashes: tuple[str, ...]
    request_count: int
    captured_at: datetime
    canonical_hash: str
    evidence_class: str = "HISTORICAL_PUBLIC"
    authority: str = "NONE"
    quality_ineligible_dates: tuple[datetime, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        history_id: str,
        canonical_asset_id: str,
        product_id: str,
        source_observation: CryptoProductObservation,
        candles: tuple[CryptoExecutionCandle, ...],
        raw_evidence_hashes: tuple[str, ...],
        request_count: int,
        captured_at: datetime,
    ) -> CryptoExecutionHistory:
        ordered = tuple(sorted(candles, key=lambda value: value.bucket_start))
        retained_close = {value.bucket_start: value.close for value in source_observation.candles}
        if (
            source_observation.product_id != product_id
            or source_observation.canonical_asset_id != canonical_asset_id
            or len(ordered) != len(source_observation.candles)
            or len({value.bucket_start for value in ordered}) != len(ordered)
            or any(retained_close.get(value.bucket_start) != value.close for value in ordered)
            or request_count != len(raw_evidence_hashes)
            or request_count < 1
            or any(len(value) != 64 for value in raw_evidence_hashes)
        ):
            raise CryptoStatefulResearchError("CRYPTO_EXECUTION_HISTORY_LINEAGE_INVALID")
        quality_ineligible_dates: tuple[datetime, ...] = ()
        values = (
            _identity(history_id, "CRYPTO_EXECUTION_HISTORY_ID_INVALID"),
            _identity(canonical_asset_id, "CRYPTO_EXECUTION_ASSET_ID_INVALID"),
            _identity(product_id, "CRYPTO_EXECUTION_PRODUCT_ID_INVALID"),
            source_observation.observation_id,
            source_observation.canonical_hash,
            source_observation.interval_start,
            source_observation.interval_end,
            ordered,
            raw_evidence_hashes,
            request_count,
            _time(captured_at, "CRYPTO_EXECUTION_HISTORY_CAPTURE_TIME_INVALID"),
            "HISTORICAL_PUBLIC",
            "NONE",
            quality_ineligible_dates,
        )
        return cls(*values[:-3], content_digest(values), *values[-3:])


@dataclass(frozen=True, slots=True)
class ResearchProposalRefinement:
    refinement_id: str
    original_proposal_id: str
    original_proposal_version: int
    original_proposal_hash: str
    refined_proposal_id: str
    refined_proposal_version: int
    refined_proposal_hash: str
    method_id: str
    material_distinct: bool
    distinctness_reasons: tuple[str, ...]
    compared_method_ids: tuple[str, ...]
    missing_original_semantics: tuple[str, ...]
    provider_request_limit: int
    refined_at: datetime
    canonical_hash: str
    policy_id: str = CRYPTO_STATEFUL_REFINEMENT_POLICY_V1
    authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        refinement_id: str,
        original: ResearchProposal,
        refined: ResearchProposal,
        provider_request_limit: int,
        refined_at: datetime,
    ) -> ResearchProposalRefinement:
        compared = (
            "CRYPTO_CROSS_SECTIONAL_DISCOVERY_V1",
            "CRYPTO_MULTI_ASSET_RELATIVE_STRENGTH_V1",
            "CRYPTO_MULTI_ASSET_REVERSAL_REGIME_V1",
            "BTC_USD_DAILY_TREND_VALIDATION_V1",
            "BTC_USD_MONTHLY_252_OBSERVATION_PRICE_TREND",
        )
        reasons = (
            "STATEFUL_RATHER_THAN_STATELESS",
            "MAXIMUM_ONE_ASSET_POSITION",
            "FIXED_THREE_PERCENT_SHORT_HORIZON_PULLBACK",
            "ELIGIBLE_UNIVERSE_RELATIVE_VOLATILITY_REGIME",
            "EXPLICIT_RECOVERY_REGIME_QUALITY_AND_TIME_EXITS",
            "ROTATION_REQUIRES_AN_INTERVENING_CASH_DECISION",
        )
        missing = (
            "EXACT_LOOKBACKS",
            "EXACT_COMPARATORS",
            "EXACT_ENTRY_THRESHOLD",
            "EXACT_EXIT_TRANSITIONS",
            "EXACT_MAXIMUM_HOLD",
        )
        material = (
            original.version == 1
            and refined.version == 2
            and refined.proposed_family == original.proposed_family
            and dict(refined.strategy_definition).get("method_id")
            == CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1
            and dict(refined.strategy_definition).get("pullback_threshold") == "-0.03"
            and dict(refined.strategy_definition).get("maximum_positions") == "1"
        )
        if not material:
            raise CryptoStatefulResearchError("CRYPTO_STATEFUL_REFINEMENT_NOT_MATERIAL")
        values = (
            _identity(refinement_id, "RESEARCH_REFINEMENT_ID_INVALID"),
            original.proposal_id,
            original.version,
            original.canonical_hash,
            refined.proposal_id,
            refined.version,
            refined.canonical_hash,
            CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
            True,
            reasons,
            compared,
            missing,
            provider_request_limit,
            _time(refined_at, "RESEARCH_REFINEMENT_TIME_INVALID"),
            CRYPTO_STATEFUL_REFINEMENT_POLICY_V1,
            "NONE",
            "NONE",
        )
        if not 0 <= provider_request_limit <= 30:
            raise CryptoStatefulResearchError("RESEARCH_REFINEMENT_PROVIDER_BOUND_INVALID")
        return cls(*values[:-3], content_digest(values), *values[-3:])


@dataclass(frozen=True, slots=True)
class CryptoStatefulMethodSpecification:
    specification_id: str
    proposal_id: str
    proposal_hash: str
    method_id: str
    version: int
    entry_lookbacks: tuple[int, ...]
    pullback_threshold: Decimal
    maximum_hold_days: int
    maximum_positions: int
    research_notional: Decimal
    fee_bps_per_side: Decimal
    slippage_bps_per_side: Decimal
    semantics: tuple[str, ...]
    implementation_id: str
    implemented_at: datetime
    canonical_hash: str
    authority: str = "NONE"
    live_authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        specification_id: str,
        proposal: ResearchProposal,
        implementation_id: str,
        implemented_at: datetime,
    ) -> CryptoStatefulMethodSpecification:
        definition = dict(proposal.strategy_definition)
        required = {
            "decision_cadence": "DAILY_COMPLETED",
            "execution": "NEXT_ELIGIBLE_DAILY_OPEN",
            "return_7d": "7_COMPLETED_DAYS",
            "volatility_30d": "30_COMPLETED_DAYS_ANNUALIZED",
            "return_90d": "90_COMPLETED_DAYS",
            "pullback_threshold": "-0.03",
            "maximum_hold_days": "14",
            "maximum_positions": "1",
            "rotation": "EXIT_TO_CASH_BEFORE_LATER_ENTRY",
        }
        if (
            proposal.version != 2
            or definition.get("method_id") != CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1
            or any(definition.get(key) != value for key, value in required.items())
        ):
            raise CryptoStatefulResearchError("CRYPTO_STATEFUL_SPECIFICATION_SEMANTICS_INVALID")
        semantics = (
            "CURRENT_ELIGIBLE_CANONICAL_SPOT_UNIVERSE_ONE_ROUTE_PER_ASSET",
            "LONG_ONE_ASSET_OR_CASH_NO_LEVERAGE_SHORT_OR_DERIVATIVES",
            "ENTRY_RETURN_90_STRICTLY_POSITIVE",
            "ENTRY_RETURN_7_LESS_THAN_OR_EQUAL_TO_NEGATIVE_0_03",
            "ENTRY_VOLATILITY_30_LESS_THAN_OR_EQUAL_TO_UNIVERSE_MEDIAN",
            "ENTRY_REVERSAL_REGIME_SCORE_STRICTLY_POSITIVE",
            "DETERMINISTIC_SCORE_ASSET_PRODUCT_RANKING",
            "NEXT_OPEN_EXECUTION_WITH_COMPLETED_INFORMATION_ONLY",
            "EXIT_RECOVERY_REGIME_VOLATILITY_QUALITY_OR_14_DAY_TIME_STOP",
            "NO_SAME_DECISION_DIRECT_ROTATION",
            "MISSING_REQUIRED_EVIDENCE_FAILS_CLOSED",
        )
        values = (
            _identity(specification_id, "CRYPTO_STATEFUL_SPECIFICATION_ID_INVALID"),
            proposal.proposal_id,
            proposal.canonical_hash,
            CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
            1,
            (7, 30, 90),
            CRYPTO_STATEFUL_PULLBACK_THRESHOLD,
            CRYPTO_STATEFUL_MAXIMUM_HOLD_DAYS,
            1,
            CRYPTO_STATEFUL_RESEARCH_NOTIONAL,
            FEE_BPS_PER_SIDE,
            SLIPPAGE_BPS_PER_SIDE,
            semantics,
            _identity(implementation_id, "CRYPTO_STATEFUL_IMPLEMENTATION_ID_INVALID"),
            _time(implemented_at, "CRYPTO_STATEFUL_IMPLEMENTATION_TIME_INVALID"),
            "NONE",
            "NONE",
        )
        return cls(*values[:-2], content_digest(values), *values[-2:])


@dataclass(frozen=True, slots=True)
class CryptoStatefulDecision:
    ordinal: int
    decision_at: datetime
    execution_at: datetime
    prior_state: CryptoState
    resulting_state: CryptoState
    action: CryptoStatefulAction
    prior_product_id: str | None
    selected_product_id: str | None
    selected_asset_id: str | None
    reasons: tuple[str, ...]
    holding_days: int
    return_7d: Decimal | None
    return_90d: Decimal | None
    volatility_30d: Decimal | None
    universe_median_volatility_30d: Decimal
    reversal_score: Decimal | None
    execution_open: Decimal | None
    modeled_fee: Decimal
    modeled_slippage: Decimal
    gross_equity: Decimal
    net_equity: Decimal
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CryptoStatefulExperimentMetrics:
    decision_count: int
    entry_count: int
    exit_count: int
    completed_holding_periods: tuple[int, ...]
    turnover: Decimal
    exposure_fraction: Decimal
    gross_return: Decimal
    net_return: Decimal
    fees: Decimal
    slippage: Decimal
    maximum_drawdown: Decimal
    realized_volatility: Decimal
    cash_return: Decimal
    equal_weight_return: Decimal
    relative_strength_comparison: Decimal | None
    reversal_regime_comparison: Decimal | None
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class CryptoStatefulExperimentRun:
    run_id: str
    proposal_id: str
    specification_id: str
    specification_hash: str
    dataset_id: str
    dataset_hash: str
    observation_ids: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    warmup_days: int
    decisions: tuple[CryptoStatefulDecision, ...]
    metrics: CryptoStatefulExperimentMetrics
    warnings: tuple[str, ...]
    result_hash: str
    canonical_hash: str
    completed_at: datetime
    authority: str = "NONE"
    paper_authority: str = "NONE"
    live_authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class CryptoStatefulCurrentSignal:
    signal_id: str
    method_id: str
    method_version: int
    specification_id: str
    experiment_run_id: str
    recommendation_id: str
    source_pulse_id: str
    universe_snapshot_id: str
    generated_at: datetime
    state: CryptoState
    selected_asset_id: str | None
    selected_product_id: str | None
    reasons: tuple[str, ...]
    return_7d: Decimal | None
    return_90d: Decimal | None
    volatility_30d: Decimal | None
    universe_median_volatility_30d: Decimal
    reversal_score: Decimal | None
    expected_gross_return: Decimal | None
    expected_net_return: Decimal | None
    uncertainty_lower: Decimal | None
    uncertainty_upper: Decimal | None
    calibration_sample_count: int
    source_opportunity_ids: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    paper_authority: str = "NONE"
    live_authority: str = "NONE"


def _features(
    histories: tuple[CryptoExecutionHistory, ...],
    date_index: int,
    common_dates: tuple[datetime, ...],
) -> dict[str, tuple[Decimal, Decimal, Decimal, Decimal]]:
    result: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    for history in histories:
        prices = {value.bucket_start: value.close for value in history.candles}
        current = prices[common_dates[date_index]]
        return_7 = metric(current / prices[common_dates[date_index - 7]] - 1)
        return_90 = metric(current / prices[common_dates[date_index - 90]] - 1)
        volatility = _volatility(
            tuple(prices[value] for value in common_dates[date_index - 30 : date_index + 1])
        )
        score = reversal_regime_score(
            return_7d=return_7,
            return_90d=return_90,
            volatility_30d=volatility,
            spread_penalty=Decimal(),
            depth_penalty=Decimal(),
            modeled_round_trip_cost=metric(
                Decimal(2) * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / Decimal(10000)
            ),
        )
        result[history.product_id] = (return_7, return_90, volatility, score)
    return result


def run_stateful_crypto_experiment(
    *,
    run_id: str,
    proposal_id: str,
    specification: CryptoStatefulMethodSpecification,
    dataset_id: str,
    histories: tuple[CryptoExecutionHistory, ...],
    relative_strength_comparison: Decimal | None,
    reversal_regime_comparison: Decimal | None,
    completed_at: datetime,
) -> CryptoStatefulExperimentRun:
    """Execute the fixed long-one-asset/cash state machine without look-ahead."""

    by_product = {value.product_id: value for value in histories}
    if len(by_product) != len(histories) or len(histories) < 3:
        raise CryptoStatefulResearchError("CRYPTO_STATEFUL_DATASET_UNIVERSE_INVALID")
    date_sets = tuple({value.bucket_start for value in history.candles} for history in histories)
    common_dates = tuple(sorted(set.intersection(*date_sets)))
    if len(common_dates) < 120:
        raise CryptoStatefulResearchError("CRYPTO_STATEFUL_DATASET_HISTORY_INSUFFICIENT")
    candle_by_product = {
        product_id: {value.bucket_start: value for value in history.candles}
        for product_id, history in by_product.items()
    }
    asset_by_product = {value.product_id: value.canonical_asset_id for value in histories}
    dataset_hash = content_digest(
        (dataset_id, tuple(sorted((value.history_id, value.canonical_hash) for value in histories)))
    )
    fee_rate = FEE_BPS_PER_SIDE / Decimal(10000)
    slippage_rate = SLIPPAGE_BPS_PER_SIDE / Decimal(10000)
    cash = CRYPTO_STATEFUL_RESEARCH_NOTIONAL
    gross_cash = CRYPTO_STATEFUL_RESEARCH_NOTIONAL
    quantity = Decimal()
    gross_quantity = Decimal()
    held_product: str | None = None
    entry_execution_at: datetime | None = None
    decisions: list[CryptoStatefulDecision] = []
    fees = Decimal()
    slippage = Decimal()
    turnover_notional = Decimal()
    exposure_days = 0
    holding_periods: list[int] = []
    equity_curve: list[Decimal] = [CRYPTO_STATEFUL_RESEARCH_NOTIONAL]
    entry_count = 0
    exit_count = 0
    for index in range(90, len(common_dates) - 1):
        decision_at = common_dates[index]
        execution_at = common_dates[index + 1]
        values = _features(histories, index, common_dates)
        median_vol = _median(tuple(item[2] for item in values.values()))
        prior_state = CryptoState.LONG if held_product is not None else CryptoState.CASH
        prior_product = held_product
        selected_product: str | None = held_product
        action = (
            CryptoStatefulAction.HOLD if held_product is not None else CryptoStatefulAction.CASH
        )
        reasons: tuple[str, ...]
        chosen_features: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
        holding_days = (
            0 if entry_execution_at is None else max(0, (execution_at - entry_execution_at).days)
        )
        if held_product is not None:
            held_features = values[held_product]
            quality_ineligible = decision_at in by_product[held_product].quality_ineligible_dates
            exit_reasons = tuple(
                reason
                for condition, reason in (
                    (held_features[0] >= 0, "SEVEN_DAY_RETURN_RECOVERED"),
                    (held_features[1] <= 0, "NINETY_DAY_REGIME_FAILED"),
                    (held_features[2] > median_vol, "RELATIVE_VOLATILITY_REGIME_FAILED"),
                    (quality_ineligible, "QUALITY_EVIDENCE_INELIGIBLE"),
                    (holding_days >= CRYPTO_STATEFUL_MAXIMUM_HOLD_DAYS, "FOURTEEN_DAY_TIME_STOP"),
                )
                if condition
            )
            chosen_features = held_features
            if exit_reasons:
                action = CryptoStatefulAction.EXIT
                selected_product = None
                reasons = exit_reasons
            else:
                reasons = ("ALL_HOLD_CONDITIONS_REMAIN_VALID",)
        else:
            eligible = tuple(
                sorted(
                    (
                        (product_id, asset_by_product[product_id], feature)
                        for product_id, feature in values.items()
                        if feature[1] > 0
                        and feature[0] <= CRYPTO_STATEFUL_PULLBACK_THRESHOLD
                        and feature[2] <= median_vol
                        and feature[3] > 0
                        and common_dates[index]
                        not in by_product[product_id].quality_ineligible_dates
                    ),
                    key=lambda item: (-item[2][3], item[1], item[0]),
                )
            )
            if eligible:
                selected_product, _, chosen_features = eligible[0]
                action = CryptoStatefulAction.ENTER
                reasons = ("HIGHEST_RANKED_ENTRY_ELIGIBLE_ASSET",)
            else:
                reasons = ("NO_ASSET_PASSED_ALL_ENTRY_PRECONDITIONS",)
        execution_open: Decimal | None = None
        event_fee = Decimal()
        event_slippage = Decimal()
        if action is CryptoStatefulAction.EXIT and held_product is not None:
            execution_open = candle_by_product[held_product][execution_at].open
            gross_proceeds = quantity * execution_open
            sell_price = execution_open * (Decimal(1) - slippage_rate)
            proceeds_after_slippage = quantity * sell_price
            event_slippage = metric(gross_proceeds - proceeds_after_slippage)
            event_fee = metric(proceeds_after_slippage * fee_rate)
            cash = proceeds_after_slippage - event_fee
            gross_cash = gross_quantity * execution_open
            turnover_notional += gross_proceeds
            fees += event_fee
            slippage += event_slippage
            holding_periods.append(holding_days)
            quantity = Decimal()
            gross_quantity = Decimal()
            held_product = None
            entry_execution_at = None
            exit_count += 1
        elif action is CryptoStatefulAction.ENTER and selected_product is not None:
            execution_open = candle_by_product[selected_product][execution_at].open
            buy_price = execution_open * (Decimal(1) + slippage_rate)
            quantity = cash / (buy_price * (Decimal(1) + fee_rate))
            gross_quantity = gross_cash / execution_open
            event_slippage = metric(quantity * (buy_price - execution_open))
            event_fee = metric(quantity * buy_price * fee_rate)
            turnover_notional += quantity * execution_open
            fees += event_fee
            slippage += event_slippage
            cash = Decimal()
            gross_cash = Decimal()
            held_product = selected_product
            entry_execution_at = execution_at
            entry_count += 1
        if held_product is not None:
            exposure_days += 1
            mark = candle_by_product[held_product][execution_at].open
            net_equity = quantity * mark
            gross_equity = gross_quantity * mark
        else:
            net_equity = cash
            gross_equity = gross_cash
        equity_curve.append(net_equity)
        resulting_state = CryptoState.LONG if held_product is not None else CryptoState.CASH
        selected_asset = None if selected_product is None else asset_by_product[selected_product]
        payload = (
            len(decisions) + 1,
            decision_at,
            execution_at,
            prior_state,
            resulting_state,
            action,
            prior_product,
            selected_product,
            selected_asset,
            reasons,
            holding_days,
            chosen_features,
            median_vol,
            execution_open,
            event_fee,
            event_slippage,
            metric(gross_equity),
            metric(net_equity),
        )
        decisions.append(
            CryptoStatefulDecision(
                ordinal=len(decisions) + 1,
                decision_at=decision_at,
                execution_at=execution_at,
                prior_state=prior_state,
                resulting_state=resulting_state,
                action=action,
                prior_product_id=prior_product,
                selected_product_id=selected_product,
                selected_asset_id=selected_asset,
                reasons=reasons,
                holding_days=holding_days,
                return_7d=None if chosen_features is None else chosen_features[0],
                return_90d=None if chosen_features is None else chosen_features[1],
                volatility_30d=None if chosen_features is None else chosen_features[2],
                universe_median_volatility_30d=median_vol,
                reversal_score=None if chosen_features is None else chosen_features[3],
                execution_open=execution_open,
                modeled_fee=event_fee,
                modeled_slippage=event_slippage,
                gross_equity=metric(gross_equity),
                net_equity=metric(net_equity),
                canonical_hash=content_digest(payload),
            )
        )
    final_net = equity_curve[-1]
    final_gross = decisions[-1].gross_equity
    running_peak = equity_curve[0]
    maximum_drawdown = Decimal()
    for equity in equity_curve:
        running_peak = max(running_peak, equity)
        maximum_drawdown = max(maximum_drawdown, (running_peak - equity) / running_peak)
    daily_returns = tuple(
        equity_curve[index] / equity_curve[index - 1] - 1
        for index in range(1, len(equity_curve))
        if equity_curve[index - 1] > 0
    )
    if len(daily_returns) >= 2:
        with localcontext() as context:
            context.prec = 50
            mean = sum(daily_returns, Decimal()) / Decimal(len(daily_returns))
            variance = sum(((value - mean) ** 2 for value in daily_returns), Decimal()) / Decimal(
                len(daily_returns) - 1
            )
            realized_volatility = metric(variance.sqrt() * Decimal(365).sqrt())
    else:
        realized_volatility = Decimal()
    first_execution = common_dates[91]
    final_execution = common_dates[-1]
    equal_weight = sum(
        candle_by_product[product_id][final_execution].open
        / candle_by_product[product_id][first_execution].open
        - 1
        for product_id in sorted(by_product)
    ) / Decimal(len(by_product))
    metrics_values = (
        len(decisions),
        entry_count,
        exit_count,
        tuple(holding_periods),
        metric(turnover_notional / CRYPTO_STATEFUL_RESEARCH_NOTIONAL),
        metric(Decimal(exposure_days) / Decimal(max(1, len(decisions)))),
        metric(final_gross / CRYPTO_STATEFUL_RESEARCH_NOTIONAL - 1),
        metric(final_net / CRYPTO_STATEFUL_RESEARCH_NOTIONAL - 1),
        metric(fees),
        metric(slippage),
        metric(maximum_drawdown),
        realized_volatility,
        Decimal(),
        metric(equal_weight),
        relative_strength_comparison,
        reversal_regime_comparison,
    )
    metrics = CryptoStatefulExperimentMetrics(*metrics_values, content_digest(metrics_values))
    warnings = (
        "STRICTLY_PRIOR_COMPLETED_SIGNALS_WITH_NEXT_OPEN_EXECUTION",
        "NO_FORWARD_FILL_OR_INTERPOLATION",
        "HISTORICAL_BOOK_DEPTH_IS_UNAVAILABLE_AND_NOT_INVENTED",
        "FIXED_FEES_AND_SLIPPAGE_RETAINED_SEPARATELY",
        "UNLIQUIDATED_WINDOW_END_POSITION_IS_MARKED_AT_FINAL_RETAINED_OPEN",
        "RESEARCH_ONLY_NO_CAPITAL_AUTHORITY",
    )
    result_hash = content_digest((tuple(value.canonical_hash for value in decisions), metrics))
    run_values = (
        _identity(run_id, "CRYPTO_STATEFUL_RUN_ID_INVALID"),
        proposal_id,
        specification.specification_id,
        specification.canonical_hash,
        _identity(dataset_id, "CRYPTO_STATEFUL_DATASET_ID_INVALID"),
        dataset_hash,
        tuple(
            value.source_observation_id
            for value in sorted(histories, key=lambda item: item.product_id)
        ),
        common_dates[90],
        common_dates[-1],
        90,
        tuple(decisions),
        metrics,
        warnings,
        result_hash,
        _time(completed_at, "CRYPTO_STATEFUL_RUN_TIME_INVALID"),
        "NONE",
        "NONE",
        "NONE",
    )
    return CryptoStatefulExperimentRun(
        *run_values[:-4], content_digest(run_values), *run_values[-4:]
    )


def current_stateful_signal(
    *,
    signal_id: str,
    specification: CryptoStatefulMethodSpecification,
    experiment: CryptoStatefulExperimentRun,
    recommendation_id: str,
    source_pulse_id: str,
    universe_snapshot_id: str,
    observations: tuple[CryptoProductObservation, ...],
    current_market: tuple[CryptoCurrentMarketEvidence, ...],
    generated_at: datetime,
) -> tuple[CryptoStatefulCurrentSignal, tuple[CryptoResearchOpportunity, ...]]:
    generated = _time(generated_at, "CRYPTO_STATEFUL_CURRENT_TIME_INVALID")
    observation_by_product = {value.product_id: value for value in observations}
    market_by_product = {value.product_id: value for value in current_market}
    if set(observation_by_product) != set(market_by_product) or len(observations) < 3:
        raise CryptoStatefulResearchError("CRYPTO_STATEFUL_CURRENT_LINEAGE_INCOMPLETE")
    raw_rows: list[tuple[str, str, Decimal, Decimal, Decimal, Decimal, tuple[str, ...]]] = []
    for product_id, observation in observation_by_product.items():
        if len(observation.candles) < 91:
            raise CryptoStatefulResearchError("CRYPTO_STATEFUL_CURRENT_HISTORY_INSUFFICIENT")
        closes = tuple(value.close for value in observation.candles)
        return_7 = metric(closes[-1] / closes[-8] - 1)
        return_90 = metric(closes[-1] / closes[-91] - 1)
        volatility = _volatility(closes[-31:])
        market = market_by_product[product_id]
        spread_penalty = metric(
            Decimal() if market.spread_bps is None else market.spread_bps / Decimal(10000)
        )
        depth_penalty = metric(
            Decimal("0.05")
            if market.visible_ask_depth is None
            else max(Decimal(), CRYPTO_STATEFUL_MINIMUM_DEPTH - market.visible_ask_depth)
            / CRYPTO_STATEFUL_MINIMUM_DEPTH
            * Decimal("0.05")
        )
        score = reversal_regime_score(
            return_7d=return_7,
            return_90d=return_90,
            volatility_30d=volatility,
            spread_penalty=spread_penalty,
            depth_penalty=depth_penalty,
            modeled_round_trip_cost=metric(
                Decimal(2) * (FEE_BPS_PER_SIDE + SLIPPAGE_BPS_PER_SIDE) / Decimal(10000)
            ),
        )
        reasons: list[str] = []
        if not market.evidence_complete:
            reasons.append("CURRENT_MARKET_EVIDENCE_INCOMPLETE")
        if market.product_status != "ACTIVE/ONLINE":
            reasons.append("CURRENT_PRODUCT_STATUS_INELIGIBLE")
        if (
            int((generated - market.observed_at).total_seconds())
            > CRYPTO_STATEFUL_FRESHNESS_SECONDS
        ):
            reasons.append("CURRENT_MARKET_EVIDENCE_STALE")
        if (
            market.required_capital is None
            or market.visible_ask_depth is None
            or market.visible_ask_depth
            < max(market.required_capital, CRYPTO_STATEFUL_MINIMUM_DEPTH)
        ):
            reasons.append("CURRENT_VISIBLE_DEPTH_INSUFFICIENT")
        raw_rows.append(
            (
                observation.canonical_asset_id,
                product_id,
                return_7,
                return_90,
                volatility,
                score,
                tuple(reasons),
            )
        )
    median_vol = _median(tuple(value[4] for value in raw_rows))
    eligible = tuple(
        sorted(
            (
                value
                for value in raw_rows
                if value[3] > 0
                and value[2] <= CRYPTO_STATEFUL_PULLBACK_THRESHOLD
                and value[4] <= median_vol
                and value[5] > 0
                and not value[6]
            ),
            key=lambda value: (-value[5], value[0], value[1]),
        )
    )
    selected = eligible[0] if eligible else None
    current_state = CryptoState.LONG if selected is not None else CryptoState.CASH
    signal_reasons = (
        ("HIGHEST_RANKED_ENTRY_ELIGIBLE_ASSET",)
        if selected is not None
        else ("NO_ASSET_PASSED_ALL_ENTRY_PRECONDITIONS",)
    )
    # A single genuine experiment is not enough to claim calibrated expected economics.
    expected_gross: Decimal | None = None
    expected_net: Decimal | None = None
    uncertainty_lower: Decimal | None = None
    uncertainty_upper: Decimal | None = None
    sample_count = len(experiment.metrics.completed_holding_periods)
    opportunities: list[CryptoResearchOpportunity] = []
    if selected is not None:
        asset, product_id, return_7, return_90, volatility, score, market_reasons = selected
        market = market_by_product[product_id]
        rejection_reasons = (
            "LEAKAGE_SAFE_CALIBRATION_SAMPLE_INSUFFICIENT",
            "EXPECTED_NET_ECONOMICS_UNAVAILABLE",
            *market_reasons,
        )
        opportunity_id = f"{signal_id}-{asset.lower()}-opportunity"
        payload = (
            opportunity_id,
            signal_id,
            product_id,
            generated,
            score,
            return_7,
            return_90,
            volatility,
            median_vol,
            rejection_reasons,
            experiment.canonical_hash,
            "NONE",
        )
        opportunities.append(
            CryptoResearchOpportunity(
                opportunity_id=opportunity_id,
                discovery_run_id=signal_id,
                universe_snapshot_id=universe_snapshot_id,
                product_id=product_id,
                generated_at=generated,
                valid_until=generated + timedelta(days=1),
                rank=1,
                supporting_observation_id=observation_by_product[product_id].observation_id,
                gross_research_score=score,
                volatility_estimate=volatility,
                fee_bps_per_side=FEE_BPS_PER_SIDE,
                slippage_bps_per_side=SLIPPAGE_BPS_PER_SIDE,
                liquidity_limitations=(
                    "DISPLAYED_PUBLIC_DEPTH_IS_NOT_FILLABILITY",
                    "SPOT_DOWNSIDE_ASSUMES_PRICE_CAN_FALL_TO_ZERO",
                ),
                hypothetical_holding_days=CRYPTO_STATEFUL_MAXIMUM_HOLD_DAYS,
                maximum_research_allocation=Decimal("0.10"),
                status=OpportunityStatus.REJECTED,
                warnings=(
                    "RESEARCH_ONLY_NO_CAPITAL_AUTHORITY",
                    "NOT_PAPER_ELIGIBLE",
                    "NOT_LIVE_ELIGIBLE",
                ),
                rejection_reasons=rejection_reasons,
                canonical_hash=content_digest(payload),
                canonical_asset_id=asset,
                strategy_version=1,
                expected_gross_return=None,
                expected_net_return=None,
                uncertainty_lower=None,
                uncertainty_upper=None,
                calibration_sample_count=sample_count,
                required_capital=market.required_capital,
                maximum_loss=market.maximum_loss,
                maximum_sensible_allocation=market.maximum_sensible_allocation,
                current_spread_bps=market.spread_bps,
                visible_ask_depth=market.visible_ask_depth,
                score_components=(
                    ("RETURN_7D", return_7),
                    ("RETURN_90D", return_90),
                    ("VOLATILITY_30D", volatility),
                    ("UNIVERSE_MEDIAN_VOLATILITY_30D", median_vol),
                    ("REVERSAL_REGIME_SCORE", score),
                ),
                calibration_id=experiment.run_id,
                supporting_checkpoint_id=market.checkpoint_id,
                method_id=CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
                trailing_return_7d=return_7,
                trailing_return_90d=return_90,
                realized_volatility_30d=volatility,
            )
        )
    signal_values = (
        _identity(signal_id, "CRYPTO_STATEFUL_CURRENT_SIGNAL_ID_INVALID"),
        CRYPTO_MULTI_ASSET_STATEFUL_PULLBACK_ROTATION_V1,
        1,
        specification.specification_id,
        experiment.run_id,
        recommendation_id,
        source_pulse_id,
        universe_snapshot_id,
        generated,
        current_state,
        None if selected is None else selected[0],
        None if selected is None else selected[1],
        signal_reasons,
        None if selected is None else selected[2],
        None if selected is None else selected[3],
        None if selected is None else selected[4],
        median_vol,
        None if selected is None else selected[5],
        expected_gross,
        expected_net,
        uncertainty_lower,
        uncertainty_upper,
        sample_count,
        tuple(value.opportunity_id for value in opportunities),
        "NONE",
        "NONE",
        "NONE",
    )
    signal = CryptoStatefulCurrentSignal(
        *signal_values[:-3], content_digest(signal_values), *signal_values[-3:]
    )
    return signal, tuple(opportunities)
