"""Broad U.S. equity data-quality, discovery, ranking, and SHADOW rules.

All outputs are research evidence.  They do not create or authorize financial
orders, positions, reservations, journals, broker access, PAPER, or LIVE state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from itertools import pairwise

from apex.domain.securities import SecuritiesBarEvidence
from apex.domain.values import (
    DecimalInput,
    canonical_decimal,
    content_digest,
    deterministic_id,
    parse_decimal,
)

BROAD_US_EQUITY_DATA_QUALITY_V1 = "BROAD_US_EQUITY_DATA_QUALITY_V1"
BROAD_EQUITY_DISCOVERY_FEATURES_V1 = "BROAD_EQUITY_DISCOVERY_FEATURES_V1"
BROAD_EQUITY_MARKET_REGIME_V1 = "BROAD_EQUITY_MARKET_REGIME_V1"
BROAD_EQUITY_OPPORTUNITY_RANKING_V1 = "BROAD_EQUITY_OPPORTUNITY_RANKING_V1"
BROAD_EQUITY_RESEARCH_PORTFOLIO_V1 = "BROAD_EQUITY_RESEARCH_PORTFOLIO_V1"
BROAD_EQUITY_DAILY_SHADOW_V1 = "BROAD_EQUITY_DAILY_SHADOW_V1"
BROAD_EQUITY_WARNING = "BROAD EQUITY DISCOVERY IS RESEARCH ONLY"
OUT_OF_DOMAIN_WARNING = "OUT_OF_DOMAIN_DIAGNOSTIC"


class BroadEquityError(ValueError):
    pass


class DataReadiness(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"
    BLOCKED_DATA = "BLOCKED_DATA"
    BLOCKED_IDENTITY = "BLOCKED_IDENTITY"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    PROVIDER_LIMITED = "PROVIDER_LIMITED"


class BroadMarketRegime(StrEnum):
    RISK_ON = "RISK_ON"
    MIXED = "MIXED"
    RISK_OFF = "RISK_OFF"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class BroadDataPolicy:
    minimum_last_price: Decimal
    minimum_completed_sessions: int
    preferred_sessions: int
    minimum_completeness: Decimal
    maximum_stale_sessions: int
    minimum_median_dollar_volume: Decimal
    minimum_recent_trading_days: int
    minimum_provider_coverage: Decimal
    canonical_hash: str

    @classmethod
    def create(
        cls,
        *,
        minimum_last_price: DecimalInput = "5",
        minimum_completed_sessions: int = 252,
        preferred_sessions: int = 504,
        minimum_completeness: DecimalInput = "0.98",
        maximum_stale_sessions: int = 1,
        minimum_median_dollar_volume: DecimalInput = "10000000",
        minimum_recent_trading_days: int = 20,
        minimum_provider_coverage: DecimalInput = "0.98",
    ) -> BroadDataPolicy:
        values = (
            parse_decimal(minimum_last_price, allow_zero=False),
            parse_decimal(minimum_completeness, allow_zero=False),
            parse_decimal(minimum_median_dollar_volume, allow_zero=False),
            parse_decimal(minimum_provider_coverage, allow_zero=False),
        )
        if (
            minimum_completed_sessions < 2
            or preferred_sessions < minimum_completed_sessions
            or maximum_stale_sessions < 0
            or minimum_recent_trading_days < 2
            or values[1] > 1
            or values[3] > 1
        ):
            raise BroadEquityError("BROAD_DATA_POLICY_INVALID")
        payload = (
            BROAD_US_EQUITY_DATA_QUALITY_V1,
            values,
            minimum_completed_sessions,
            preferred_sessions,
            maximum_stale_sessions,
            minimum_recent_trading_days,
        )
        return cls(
            values[0],
            minimum_completed_sessions,
            preferred_sessions,
            values[1],
            maximum_stale_sessions,
            values[2],
            minimum_recent_trading_days,
            values[3],
            content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class BroadDataCapturePlan:
    plan_id: str
    universe_id: str
    start_date: str
    end_date: str
    symbol_count: int
    batch_size: int
    expected_requests: int
    expected_sessions_per_symbol: int
    estimated_bar_count: int
    estimated_storage_bytes: int
    provider_capability: str
    limitations: tuple[str, ...]
    canonical_hash: str


def plan_broad_data_capture(
    *,
    universe_id: str,
    start_date: date,
    end_date: date,
    symbol_count: int,
    expected_sessions_per_symbol: int,
    batch_size: int = 50,
    provider_capability: str = "ALPACA_HISTORICAL_DAILY_BARS_GET_ONLY",
) -> BroadDataCapturePlan:
    if (
        not universe_id
        or start_date > end_date
        or symbol_count < 1
        or expected_sessions_per_symbol < 1
        or not 1 <= batch_size <= 200
    ):
        raise BroadEquityError("BROAD_DATA_PLAN_INVALID")
    requests = (symbol_count + batch_size - 1) // batch_size
    bars = symbol_count * expected_sessions_per_symbol
    payload = (
        universe_id,
        start_date.isoformat(),
        end_date.isoformat(),
        symbol_count,
        batch_size,
        requests,
        expected_sessions_per_symbol,
        provider_capability,
    )
    return BroadDataCapturePlan(
        deterministic_id("broad_equity_data_plan", payload),
        universe_id,
        start_date.isoformat(),
        end_date.isoformat(),
        symbol_count,
        batch_size,
        requests,
        expected_sessions_per_symbol,
        bars,
        bars * 240,
        provider_capability,
        (
            "COMPLETED_XNYS_DAILY_SESSIONS_ONLY",
            "FEED_COVERAGE_DEPENDS_ON_CONFIGURED_PROVIDER_ENTITLEMENT",
            "DRY_PLAN_PERFORMS_NO_PROVIDER_REQUEST",
        ),
        content_digest(payload),
    )


@dataclass(frozen=True, slots=True)
class SecurityDataQuality:
    canonical_security_id: str
    expected_sessions: int
    received_sessions: int
    missing_sessions: tuple[str, ...]
    duplicate_sessions: tuple[str, ...]
    malformed_ohlc: int
    nonpositive_values: int
    stale_sessions: int
    abrupt_symbol_discontinuity: bool
    extreme_unadjusted_discontinuity: bool
    corporate_action_dependency: bool
    provider_coverage: Decimal
    median_dollar_volume: Decimal | None
    readiness: DataReadiness
    blockers: tuple[str, ...]
    canonical_hash: str


def assess_security_data_quality(
    *,
    canonical_security_id: str,
    bars: tuple[SecuritiesBarEvidence, ...],
    expected_session_dates: tuple[str, ...],
    policy: BroadDataPolicy,
    identity_blocked: bool = False,
    provider_limited: bool = False,
) -> SecurityDataQuality:
    expected = set(expected_session_dates)
    sessions: dict[str, list[SecuritiesBarEvidence]] = {}
    malformed = 0
    nonpositive = 0
    for bar in bars:
        session = bar.period_start.date().isoformat()
        sessions.setdefault(session, []).append(bar)
        if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
            malformed += 1
        if min(bar.open, bar.high, bar.low, bar.close, bar.volume) <= 0:
            nonpositive += 1
    observed = set(sessions)
    missing = tuple(sorted(expected - observed))
    duplicates = tuple(sorted(key for key, values in sessions.items() if len(values) > 1))
    stale = _trailing_missing(expected_session_dates, observed)
    ordered = sorted(
        (values[0] for values in sessions.values()),
        key=lambda item: item.period_start,
    )
    discontinuities = tuple(
        current.close / prior.close for prior, current in pairwise(ordered) if prior.close > 0
    )
    extreme = any(value >= Decimal("1.5") or value <= Decimal("0.5") for value in discontinuities)
    abrupt = any(
        value >= Decimal("3") or value <= Decimal("0.33333333") for value in discontinuities
    )
    coverage = _exact(
        Decimal(len(expected.intersection(observed))) / Decimal(len(expected))
        if expected
        else Decimal()
    )
    dollar_volumes = sorted(bar.close * bar.volume for bar in ordered)
    median_volume = _median(dollar_volumes) if dollar_volumes else None
    blockers: list[str] = []
    if identity_blocked:
        blockers.append("BLOCKED_IDENTITY")
    if provider_limited:
        blockers.append("PROVIDER_LIMITED")
    if len(observed) < policy.minimum_completed_sessions:
        blockers.append("INSUFFICIENT_HISTORY")
    if coverage < policy.minimum_completeness:
        blockers.append("COMPLETENESS_BELOW_MINIMUM")
    if coverage < policy.minimum_provider_coverage:
        blockers.append("PROVIDER_COVERAGE_BELOW_MINIMUM")
    if duplicates:
        blockers.append("DUPLICATE_CANONICAL_BARS")
    if malformed:
        blockers.append("MALFORMED_OHLC")
    if nonpositive:
        blockers.append("NONPOSITIVE_BAR_VALUE")
    if stale > policy.maximum_stale_sessions:
        blockers.append("STALE_LAST_BAR")
    if ordered and ordered[-1].close < policy.minimum_last_price:
        blockers.append("LAST_PRICE_BELOW_MINIMUM")
    if median_volume is None or median_volume < policy.minimum_median_dollar_volume:
        blockers.append("MEDIAN_DOLLAR_VOLUME_BELOW_MINIMUM")
    if len(ordered[-policy.minimum_recent_trading_days :]) < policy.minimum_recent_trading_days:
        blockers.append("INSUFFICIENT_RECENT_TRADING_DAYS")
    if abrupt:
        blockers.append("ABRUPT_SYMBOL_DISCONTINUITY")
    if identity_blocked:
        readiness = DataReadiness.BLOCKED_IDENTITY
    elif provider_limited:
        readiness = DataReadiness.PROVIDER_LIMITED
    elif "INSUFFICIENT_HISTORY" in blockers:
        readiness = DataReadiness.INSUFFICIENT_HISTORY
    elif blockers:
        readiness = DataReadiness.BLOCKED_DATA
    elif len(observed) < policy.preferred_sessions:
        readiness = DataReadiness.PARTIAL
    else:
        readiness = DataReadiness.READY
    payload = (
        BROAD_US_EQUITY_DATA_QUALITY_V1,
        canonical_security_id,
        policy.canonical_hash,
        tuple(bar.content_hash for bar in ordered),
        expected_session_dates,
        tuple(sorted(blockers)),
    )
    return SecurityDataQuality(
        canonical_security_id,
        len(expected),
        len(observed),
        missing,
        duplicates,
        malformed,
        nonpositive,
        stale,
        abrupt,
        extreme,
        extreme,
        coverage,
        median_volume,
        readiness,
        tuple(sorted(blockers)),
        content_digest(payload),
    )


def _trailing_missing(expected: tuple[str, ...], observed: set[str]) -> int:
    count = 0
    for session in reversed(expected):
        if session in observed:
            break
        count += 1
    return count


def _median(values: list[Decimal]) -> Decimal:
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


@dataclass(frozen=True, slots=True)
class BroadEquityFeatures:
    canonical_security_id: str
    as_of_session: str
    values: tuple[tuple[str, Decimal | None], ...]
    warnings: tuple[str, ...]
    data_quality_hash: str
    canonical_hash: str

    def value(self, name: str) -> Decimal | None:
        return dict(self.values).get(name)


def calculate_broad_equity_features(
    *,
    canonical_security_id: str,
    bars: tuple[SecuritiesBarEvidence, ...],
    quality: SecurityDataQuality,
    filing_age_days: int | None = None,
    selected_fact_coverage: DecimalInput | None = None,
) -> BroadEquityFeatures:
    ordered = sorted(bars, key=lambda item: item.period_start)
    if len(ordered) < 20:
        raise BroadEquityError("BROAD_FEATURES_REQUIRE_20_SESSIONS")
    closes = [bar.close for bar in ordered]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    values: dict[str, Decimal | None] = {}
    for horizon in (5, 20, 60, 126):
        values[f"return_{horizon}"] = _horizon_return(closes, horizon)
    values["distance_ma_20"] = closes[-1] / _mean(closes[-20:]) - 1
    values["distance_ma_60"] = closes[-1] / _mean(closes[-60:]) - 1 if len(closes) >= 60 else None
    values["moving_average_slope"] = _mean(closes[-5:]) / _mean(closes[-20:-15]) - 1
    values["trend_consistency"] = Decimal(sum(value > 0 for value in returns[-60:])) / Decimal(
        min(60, len(returns))
    )
    values["short_horizon_reversal"] = -returns[-1]
    values["gap_magnitude"] = ordered[-1].open / ordered[-2].close - 1
    recent_high = max(bar.high for bar in ordered[-126:])
    recent_low = min(bar.low for bar in ordered[-126:])
    values["distance_recent_high"] = closes[-1] / recent_high - 1
    values["distance_recent_low"] = closes[-1] / recent_low - 1
    values["return_dislocation_z"] = _zscore(returns[-1], returns[-60:])
    volumes = [bar.volume for bar in ordered]
    values["volume_shock"] = volumes[-1] / _mean(volumes[-20:]) - 1
    values["realized_volatility_20"] = _standard_deviation(returns[-20:])
    negative_returns = [value for value in returns[-60:] if value < 0]
    values["downside_volatility_60"] = _standard_deviation(negative_returns)
    values["maximum_drawdown_126"] = _maximum_drawdown(closes[-126:])
    gaps = [ordered[i].open / ordered[i - 1].close - 1 for i in range(1, len(ordered))]
    values["gap_risk_60"] = max((abs(value) for value in gaps[-60:]), default=Decimal())
    values["tail_return_min_126"] = min(returns[-126:])
    values["median_dollar_volume"] = quality.median_dollar_volume
    recent_dollar_volume = [bar.close * bar.volume for bar in ordered[-20:]]
    values["recent_dollar_volume"] = _mean(recent_dollar_volume)
    values["volume_stability"] = _coefficient_stability(volumes[-60:])
    values["turnover_proxy"] = None
    volatility = values["realized_volatility_20"]
    values["liquidity_adjusted_volatility"] = (
        volatility / _decimal_sqrt(values["recent_dollar_volume"])
        if volatility is not None
        and values["recent_dollar_volume"] is not None
        and values["recent_dollar_volume"] > 0
        else None
    )
    values["latest_filing_age_days"] = (
        Decimal(filing_age_days) if filing_age_days is not None else None
    )
    values["selected_fact_coverage"] = (
        parse_decimal(selected_fact_coverage) if selected_fact_coverage is not None else None
    )
    warnings: list[str] = [OUT_OF_DOMAIN_WARNING]
    if filing_age_days is None:
        warnings.append("FUNDAMENTALS_UNAVAILABLE_NOT_FAVORABLE")
    elif filing_age_days > 180:
        warnings.append("STALE_FUNDAMENTAL_WARNING")
    if quality.median_dollar_volume is None:
        warnings.append("MISSING_LIQUIDITY")
    serialized = tuple(
        sorted(
            (name, _exact(value) if value is not None else None) for name, value in values.items()
        )
    )
    payload = (
        BROAD_EQUITY_DISCOVERY_FEATURES_V1,
        canonical_security_id,
        ordered[-1].period_start.date().isoformat(),
        serialized,
        tuple(sorted(warnings)),
        quality.canonical_hash,
    )
    return BroadEquityFeatures(
        canonical_security_id,
        ordered[-1].period_start.date().isoformat(),
        serialized,
        tuple(sorted(warnings)),
        quality.canonical_hash,
        content_digest(payload),
    )


def _horizon_return(closes: list[Decimal], horizon: int) -> Decimal | None:
    return closes[-1] / closes[-horizon - 1] - 1 if len(closes) > horizon else None


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal()) / Decimal(len(values))


def _standard_deviation(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    average = _mean(values)
    variance = sum(((value - average) ** 2 for value in values), Decimal()) / Decimal(len(values))
    return _decimal_sqrt(variance)


def _decimal_sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 18
        return +value.sqrt()


def _zscore(value: Decimal, history: list[Decimal]) -> Decimal | None:
    deviation = _standard_deviation(history)
    if deviation is None or deviation == 0:
        return None
    return (value - _mean(history)) / deviation


def _maximum_drawdown(closes: list[Decimal]) -> Decimal:
    peak = closes[0]
    maximum = Decimal()
    for close in closes:
        peak = max(peak, close)
        maximum = max(maximum, (peak - close) / peak)
    return maximum


def _coefficient_stability(values: list[Decimal]) -> Decimal | None:
    average = _mean(values)
    deviation = _standard_deviation(values)
    if deviation is None or average == 0:
        return None
    return max(Decimal(), 1 - deviation / average)


@dataclass(frozen=True, slots=True)
class BroadEquityRegimeEvidence:
    regime: BroadMarketRegime
    breadth: Decimal
    dispersion: Decimal
    average_return_20: Decimal
    evidence_count: int
    canonical_hash: str


def classify_broad_market_regime(
    features: tuple[BroadEquityFeatures, ...],
) -> BroadEquityRegimeEvidence:
    returns = [value for feature in features if (value := feature.value("return_20")) is not None]
    if not returns:
        regime = BroadMarketRegime.INSUFFICIENT_EVIDENCE
        breadth = dispersion = average = Decimal()
    else:
        breadth = _exact(Decimal(sum(value > 0 for value in returns)) / Decimal(len(returns)))
        average = _exact(_mean(returns))
        dispersion = _exact(_standard_deviation(returns) or Decimal())
        if breadth >= Decimal("0.6") and average > 0:
            regime = BroadMarketRegime.RISK_ON
        elif breadth <= Decimal("0.4") and average < 0:
            regime = BroadMarketRegime.RISK_OFF
        else:
            regime = BroadMarketRegime.MIXED
    payload = (BROAD_EQUITY_MARKET_REGIME_V1, regime, breadth, dispersion, average, len(returns))
    return BroadEquityRegimeEvidence(
        regime, breadth, dispersion, average, len(returns), content_digest(payload)
    )


@dataclass(frozen=True, slots=True)
class BroadEquityOpportunity:
    rank: int
    canonical_security_id: str
    score: Decimal
    percentiles: tuple[tuple[str, Decimal], ...]
    uncertainty: Decimal
    liquidity_state: str
    data_quality_state: DataReadiness
    evidence_maturity: str
    abstention_reasons: tuple[str, ...]
    feature_hash: str
    canonical_hash: str


def rank_broad_equity_opportunities(
    *,
    features: tuple[BroadEquityFeatures, ...],
    qualities: tuple[SecurityDataQuality, ...],
    regime: BroadEquityRegimeEvidence,
) -> tuple[BroadEquityOpportunity, ...]:
    quality_by_id = {quality.canonical_security_id: quality for quality in qualities}
    names = (
        "return_20",
        "return_60",
        "trend_consistency",
        "short_horizon_reversal",
        "realized_volatility_20",
        "maximum_drawdown_126",
        "median_dollar_volume",
    )
    percentiles = {
        name: _percentiles(
            tuple(
                (feature.canonical_security_id, value)
                for feature in features
                if (value := feature.value(name)) is not None
            )
        )
        for name in names
    }
    candidates: list[tuple[BroadEquityFeatures, Decimal, tuple[tuple[str, Decimal], ...]]] = []
    for feature in features:
        quality = quality_by_id.get(feature.canonical_security_id)
        if quality is None:
            raise BroadEquityError("BROAD_RANKING_QUALITY_MISSING")
        values = tuple(
            (name, percentiles[name].get(feature.canonical_security_id, Decimal()))
            for name in names
        )
        lookup = dict(values)
        score = (
            lookup["return_20"] * Decimal("0.18")
            + lookup["return_60"] * Decimal("0.18")
            + lookup["trend_consistency"] * Decimal("0.16")
            + lookup["short_horizon_reversal"] * Decimal("0.08")
            + (1 - lookup["realized_volatility_20"]) * Decimal("0.10")
            + (1 - lookup["maximum_drawdown_126"]) * Decimal("0.12")
            + lookup["median_dollar_volume"] * Decimal("0.12")
            + quality.provider_coverage * Decimal("0.06")
        )
        if regime.regime is BroadMarketRegime.RISK_OFF:
            score *= Decimal("0.8")
        candidates.append((feature, _exact(score), values))
    ranked: list[BroadEquityOpportunity] = []
    for rank, (feature, score, values) in enumerate(
        sorted(candidates, key=lambda item: (-item[1], item[0].canonical_security_id)),
        start=1,
    ):
        quality = quality_by_id[feature.canonical_security_id]
        abstentions = quality.blockers
        uncertainty = _exact(
            max(
                Decimal(),
                Decimal("1")
                - quality.provider_coverage
                + (Decimal("0.2") if OUT_OF_DOMAIN_WARNING in feature.warnings else Decimal()),
            )
        )
        payload = (
            BROAD_EQUITY_OPPORTUNITY_RANKING_V1,
            regime.canonical_hash,
            rank,
            feature.canonical_hash,
            score,
            values,
            uncertainty,
            abstentions,
        )
        ranked.append(
            BroadEquityOpportunity(
                rank,
                feature.canonical_security_id,
                score,
                values,
                uncertainty,
                "SUFFICIENT" if quality.median_dollar_volume is not None else "MISSING",
                quality.readiness,
                "TRANSPARENT_FEATURES_ONLY",
                abstentions,
                feature.canonical_hash,
                content_digest(payload),
            )
        )
    return tuple(ranked)


def _percentiles(values: tuple[tuple[str, Decimal], ...]) -> dict[str, Decimal]:
    ordered = sorted(values, key=lambda item: (item[1], item[0]))
    denominator = Decimal(max(1, len(ordered) - 1))
    return {
        security_id: _exact(Decimal(index) / denominator)
        for index, (security_id, _) in enumerate(ordered)
    }


def _exact(value: Decimal) -> Decimal:
    """Bound derived research values to the repository's authoritative scale."""
    return parse_decimal(
        value.quantize(Decimal("0.00000001")),
        allow_negative=True,
    )


@dataclass(frozen=True, slots=True)
class BroadResearchAllocation:
    canonical_security_id: str
    hypothetical_weight: Decimal
    authority_permitted_weight: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class BroadResearchPortfolio:
    portfolio_id: str
    ranking_hashes: tuple[str, ...]
    allocations: tuple[BroadResearchAllocation, ...]
    cash_weight: Decimal
    authority: str
    canonical_hash: str


def build_broad_research_portfolio(
    *,
    opportunities: tuple[BroadEquityOpportunity, ...],
    maximum_names: int = 5,
    per_name_cap: DecimalInput = "0.2",
    uncertainty_cap: DecimalInput = "0.5",
) -> BroadResearchPortfolio:
    cap = parse_decimal(per_name_cap, allow_zero=False)
    uncertainty_limit = parse_decimal(uncertainty_cap)
    if not 1 <= maximum_names <= 5 or cap > 1 or uncertainty_limit > 1:
        raise BroadEquityError("BROAD_PORTFOLIO_POLICY_INVALID")
    selected = [
        item
        for item in opportunities
        if not item.abstention_reasons and item.uncertainty <= uncertainty_limit
    ][:maximum_names]
    allocations = tuple(
        BroadResearchAllocation(
            item.canonical_security_id,
            cap,
            Decimal(),
            "RESEARCH_ONLY_NO_CAPITAL_AUTHORITY",
        )
        for item in selected
    )
    total = sum((item.hypothetical_weight for item in allocations), Decimal())
    if total > 1:
        raise BroadEquityError("BROAD_PORTFOLIO_ALLOCATION_EXCEEDS_ONE")
    cash = 1 - total
    payload = (
        BROAD_EQUITY_RESEARCH_PORTFOLIO_V1,
        tuple(item.canonical_hash for item in opportunities),
        allocations,
        cash,
        "RESEARCH_ONLY",
    )
    return BroadResearchPortfolio(
        deterministic_id("broad_equity_research_portfolio", payload),
        tuple(item.canonical_hash for item in opportunities),
        allocations,
        cash,
        "RESEARCH_ONLY",
        content_digest(payload),
    )


@dataclass(frozen=True, slots=True)
class BroadEquityDiscoveryRun:
    run_id: str
    universe_id: str
    data_capture_id: str
    regime: BroadEquityRegimeEvidence
    opportunities: tuple[BroadEquityOpportunity, ...]
    portfolio: BroadResearchPortfolio
    warnings: tuple[str, ...]
    canonical_hash: str


def build_broad_equity_discovery_run(
    *,
    universe_id: str,
    data_capture_id: str,
    regime: BroadEquityRegimeEvidence,
    opportunities: tuple[BroadEquityOpportunity, ...],
    portfolio: BroadResearchPortfolio,
) -> BroadEquityDiscoveryRun:
    payload = (
        universe_id,
        data_capture_id,
        regime.canonical_hash,
        tuple(item.canonical_hash for item in opportunities),
        portfolio.canonical_hash,
        (BROAD_EQUITY_WARNING, OUT_OF_DOMAIN_WARNING),
    )
    return BroadEquityDiscoveryRun(
        deterministic_id("broad_equity_discovery_run", payload),
        universe_id,
        data_capture_id,
        regime,
        opportunities,
        portfolio,
        (BROAD_EQUITY_WARNING, OUT_OF_DOMAIN_WARNING),
        content_digest(payload),
    )


@dataclass(frozen=True, slots=True)
class BroadEquityShadowPlan:
    plan_id: str
    universe_id: str
    data_capture_id: str
    discovery_run_id: str
    policy_hashes: tuple[str, ...]
    top_candidate_ids: tuple[str, ...]
    decision_at: datetime
    forward_horizon_sessions: int
    data_cutoff: datetime
    authority: str
    state: str
    outcome_requirements: tuple[str, ...]
    canonical_hash: str


def create_broad_equity_shadow_plan(
    *,
    discovery_run: BroadEquityDiscoveryRun,
    policy_hashes: tuple[str, ...],
    top_n: int,
    decision_at: datetime,
    forward_horizon_sessions: int,
    data_cutoff: datetime,
) -> BroadEquityShadowPlan:
    if (
        not 1 <= top_n <= 20
        or forward_horizon_sessions < 1
        or decision_at.tzinfo is None
        or data_cutoff.tzinfo is None
        or data_cutoff > decision_at
    ):
        raise BroadEquityError("BROAD_SHADOW_PLAN_INVALID")
    candidates = tuple(
        item.canonical_security_id
        for item in discovery_run.opportunities
        if not item.abstention_reasons
    )[:top_n]
    payload = (
        BROAD_EQUITY_DAILY_SHADOW_V1,
        discovery_run.canonical_hash,
        tuple(sorted(policy_hashes)),
        candidates,
        decision_at,
        forward_horizon_sessions,
        data_cutoff,
        "OBSERVATION_ONLY",
        "DRAFT",
    )
    return BroadEquityShadowPlan(
        deterministic_id("broad_equity_shadow_plan", payload),
        discovery_run.universe_id,
        discovery_run.data_capture_id,
        discovery_run.run_id,
        tuple(sorted(policy_hashes)),
        candidates,
        decision_at,
        forward_horizon_sessions,
        data_cutoff,
        "OBSERVATION_ONLY",
        "DRAFT",
        (
            "EXACT_FUTURE_XNYS_SESSION_CLOSES",
            "UNCHANGED_CANONICAL_SECURITY_IDENTITY",
            "NO_EXECUTION_OR_FILL_INTERPRETATION",
        ),
        content_digest(payload),
    )


def canonical_feature_report(features: BroadEquityFeatures) -> dict[str, str | None]:
    """Small stable presentation helper without financial calculations."""
    return {
        key: canonical_decimal(value) if value is not None else None
        for key, value in features.values
    }
