"""Pure, research-only Securities strategy-intelligence contracts.

This module intentionally has no execution, provider, campaign, or ledger
imports.  It turns retained historical evidence into reproducible research
decisions only; none of these values grants capital or deployment authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum

from apex.domain.values import content_digest

SECURITIES_STRATEGY_INTELLIGENCE_FEATURES_V1 = "SECURITIES_STRATEGY_INTELLIGENCE_FEATURES_V1"
SECURITIES_MARKET_REGIME_V1 = "SECURITIES_MARKET_REGIME_V1"
SECURITIES_EXPECTED_OUTCOME_V1 = "SECURITIES_EXPECTED_OUTCOME_V1"
SECURITIES_RESEARCH_ACTION_POLICY_V1 = "SECURITIES_RESEARCH_ACTION_POLICY_V1"
SECURITIES_RESEARCH_OPPORTUNITY_RANKING_V1 = "SECURITIES_RESEARCH_OPPORTUNITY_RANKING_V1"

_Q = Decimal("0.00000001")
_ONE = Decimal(1)
_ZERO = Decimal()


class StrategyIntelligenceError(ValueError):
    """A research decision violates a declared intelligence invariant."""


class MarketRegime(StrEnum):
    TRENDING_RISK_ON = "TRENDING_RISK_ON"
    TRENDING_DEFENSIVE = "TRENDING_DEFENSIVE"
    CHOPPY_LOW_VOLATILITY = "CHOPPY_LOW_VOLATILITY"
    HIGH_VOLATILITY_STRESS = "HIGH_VOLATILITY_STRESS"
    CORRELATION_SHOCK = "CORRELATION_SHOCK"
    MIXED_UNCERTAIN = "MIXED_UNCERTAIN"


class ResearchAction(StrEnum):
    OPEN_LONG = "OPEN_LONG"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True, slots=True)
class StrategyIntelligenceFeatures:
    """One instrument/session feature record with exact retained lineage."""

    instrument_id: str
    decision_session_id: str
    decision_session_date: str
    available_at: str
    values: tuple[tuple[str, Decimal], ...]
    source_row_ids: tuple[str, ...]
    source_lineage_hash: str
    feature_hash: str
    quality_warning_count: int
    current_identity_limited: bool
    fixed_universe_limited: bool

    def value(self, name: str) -> Decimal:
        lookup = dict(self.values)
        if name not in lookup:
            raise StrategyIntelligenceError(f"FEATURE_MISSING:{name}")
        return lookup[name]


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    decision_session_id: str
    regime: MarketRegime
    confidence: Decimal
    boundary_distance: Decimal
    measurements: tuple[tuple[str, Decimal], ...]
    reason_codes: tuple[str, ...]
    policy_hash: str
    source_lineage_hash: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class NormalizedForecast:
    instrument_id: str
    decision_session_id: str
    components: tuple[tuple[str, Decimal], ...]
    calibrated_probability: Decimal | None
    baseline_probability: Decimal | None
    model_integrity_state: str
    simulator_conformance_state: str
    blocked: bool
    source_lineage_hash: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class ExpectedOutcome:
    instrument_id: str
    decision_session_id: str
    probability_positive: Decimal | None
    expected_gross_return: Decimal | None
    expected_downside: Decimal | None
    downside_tail: Decimal | None
    uncertainty: Decimal | None
    estimated_cost: Decimal | None
    expected_net_return: Decimal | None
    probability_exceeds_cost: Decimal | None
    sample_size: int
    reason_codes: tuple[str, ...]
    policy_hash: str
    source_lineage_hash: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class ResearchDecision:
    instrument_id: str
    decision_session_id: str
    action: ResearchAction
    desired_exposure: Decimal
    regime_fit: Decimal
    agreement: Decimal
    binding_constraints: tuple[str, ...]
    reason_codes: tuple[str, ...]
    policy_hash: str
    source_lineage_hash: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class RankedOpportunity:
    instrument_id: str
    decision_session_id: str
    rank: int
    composite_score: Decimal
    components: tuple[tuple[str, Decimal], ...]
    decision: ResearchDecision
    is_cash_alternative: bool
    canonical_hash: str


def q(value: Decimal) -> Decimal:
    return value.quantize(_Q, rounding=ROUND_HALF_EVEN)


def clamp(value: Decimal, lower: Decimal = _ZERO, upper: Decimal = _ONE) -> Decimal:
    return q(min(upper, max(lower, value)))


def mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise StrategyIntelligenceError("MEAN_REQUIRES_VALUES")
    return q(sum(values, _ZERO) / Decimal(len(values)))


def stddev(values: Sequence[Decimal]) -> Decimal:
    if len(values) < 2:
        return _ZERO
    center = mean(values)
    with localcontext() as context:
        context.prec = 48
        variance = sum(((item - center) ** 2 for item in values), _ZERO) / Decimal(len(values))
        return q(variance.sqrt())


@dataclass(frozen=True, slots=True)
class MarketRegimePolicy:
    """Predeclared, deliberately broad regime thresholds."""

    policy_id: str = SECURITIES_MARKET_REGIME_V1
    risk_on_trend: Decimal = Decimal("0.02")
    stress_volatility: Decimal = Decimal("0.03")
    stress_drawdown: Decimal = Decimal("-0.08")
    correlation_shock: Decimal = Decimal("0.75")
    correlation_change: Decimal = Decimal("0.15")
    low_volatility: Decimal = Decimal("0.012")
    choppy_trend_band: Decimal = Decimal("0.01")

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    def classify(
        self, *, session_id: str, measurements: Mapping[str, Decimal], source_lineage_hash: str
    ) -> RegimeSnapshot:
        required = {
            "broad_trend_60",
            "breadth_20",
            "volatility_20",
            "drawdown_60",
            "average_correlation",
            "correlation_change",
            "return_dispersion",
        }
        if set(measurements) != required:
            raise StrategyIntelligenceError("REGIME_MEASUREMENT_CONTRACT_INVALID")
        trend = measurements["broad_trend_60"]
        breadth = measurements["breadth_20"]
        volatility = measurements["volatility_20"]
        drawdown = measurements["drawdown_60"]
        correlation = measurements["average_correlation"]
        correlation_delta = measurements["correlation_change"]
        reasons: list[str]
        distances: list[Decimal]
        if correlation >= self.correlation_shock and correlation_delta >= self.correlation_change:
            regime, reasons = MarketRegime.CORRELATION_SHOCK, ["CORRELATION_SHOCK_THRESHOLD"]
            distances = [
                correlation - self.correlation_shock,
                correlation_delta - self.correlation_change,
            ]
        elif volatility >= self.stress_volatility or drawdown <= self.stress_drawdown:
            regime, reasons = MarketRegime.HIGH_VOLATILITY_STRESS, ["STRESS_VOLATILITY_OR_DRAWDOWN"]
            distances = [volatility - self.stress_volatility, self.stress_drawdown - drawdown]
        elif trend >= self.risk_on_trend and breadth >= Decimal("0.55"):
            regime, reasons = MarketRegime.TRENDING_RISK_ON, ["POSITIVE_TREND_AND_BREADTH"]
            distances = [trend - self.risk_on_trend, breadth - Decimal("0.55")]
        elif trend <= -self.risk_on_trend and breadth <= Decimal("0.45"):
            regime, reasons = MarketRegime.TRENDING_DEFENSIVE, ["NEGATIVE_TREND_AND_BREADTH"]
            distances = [-self.risk_on_trend - trend, Decimal("0.45") - breadth]
        elif abs(trend) <= self.choppy_trend_band and volatility <= self.low_volatility:
            regime, reasons = MarketRegime.CHOPPY_LOW_VOLATILITY, ["LOW_TREND_LOW_VOLATILITY"]
            distances = [self.choppy_trend_band - abs(trend), self.low_volatility - volatility]
        else:
            regime, reasons = MarketRegime.MIXED_UNCERTAIN, ["MIXED_COMPONENTS"]
            distances = [abs(trend), abs(breadth - Decimal("0.5"))]
        boundary = q(min(abs(item) for item in distances))
        confidence = clamp(boundary / Decimal("0.10"))
        payload = {
            "session": session_id,
            "regime": regime,
            "measurements": tuple(sorted(measurements.items())),
            "reasons": tuple(reasons),
            "policy": self.canonical_hash,
            "lineage": source_lineage_hash,
        }
        return RegimeSnapshot(
            decision_session_id=session_id,
            regime=regime,
            confidence=confidence,
            boundary_distance=boundary,
            measurements=tuple(sorted(measurements.items())),
            reason_codes=tuple(reasons),
            policy_hash=self.canonical_hash,
            source_lineage_hash=source_lineage_hash,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ExpectedOutcomePolicy:
    policy_id: str = SECURITIES_EXPECTED_OUTCOME_V1
    minimum_sample_size: int = 20
    modeled_cost: Decimal = Decimal("0.0010")

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    def estimate(
        self,
        *,
        forecast: NormalizedForecast,
        prior_returns: Sequence[Decimal],
        source_lineage_hash: str,
    ) -> ExpectedOutcome:
        reasons: list[str] = []
        probability = forecast.calibrated_probability or forecast.baseline_probability
        if probability is None:
            reasons.append("FORECAST_UNAVAILABLE")
        if len(prior_returns) < self.minimum_sample_size:
            reasons.append("INSUFFICIENT_PRIOR_RETURN_EVIDENCE")
        if probability is None or len(prior_returns) < self.minimum_sample_size:
            return self._unavailable(forecast, reasons, source_lineage_hash)
        gains = tuple(item for item in prior_returns if item > _ZERO)
        losses = tuple(item for item in prior_returns if item <= _ZERO)
        if not gains or not losses:
            return self._unavailable(
                forecast, [*reasons, "ONE_SIDED_PRIOR_DISTRIBUTION"], source_lineage_hash
            )
        expected = q(probability * mean(gains) + (_ONE - probability) * mean(losses))
        downside = q(mean(losses))
        tail = q(min(sorted(losses)[: max(1, len(losses) // 5)]))
        component_values = tuple(value for _name, value in forecast.components)
        disagreement = stddev(component_values) if component_values else Decimal("0.5")
        uncertainty = clamp(
            disagreement + stddev(prior_returns) + Decimal(1) / Decimal(len(prior_returns))
        )
        exceeds = clamp(
            probability * clamp((mean(gains) - self.modeled_cost) / max(mean(gains), _Q))
        )
        payload = {
            "forecast": forecast.canonical_hash,
            "returns": tuple(prior_returns),
            "policy": self.canonical_hash,
            "lineage": source_lineage_hash,
        }
        return ExpectedOutcome(
            instrument_id=forecast.instrument_id,
            decision_session_id=forecast.decision_session_id,
            probability_positive=probability,
            expected_gross_return=expected,
            expected_downside=downside,
            downside_tail=tail,
            uncertainty=uncertainty,
            estimated_cost=self.modeled_cost,
            expected_net_return=q(expected - self.modeled_cost),
            probability_exceeds_cost=exceeds,
            sample_size=len(prior_returns),
            reason_codes=tuple(reasons),
            policy_hash=self.canonical_hash,
            source_lineage_hash=source_lineage_hash,
            canonical_hash=content_digest(payload),
        )

    def _unavailable(
        self, forecast: NormalizedForecast, reasons: Sequence[str], lineage: str
    ) -> ExpectedOutcome:
        payload = {
            "forecast": forecast.canonical_hash,
            "reasons": tuple(reasons),
            "policy": self.canonical_hash,
        }
        return ExpectedOutcome(
            instrument_id=forecast.instrument_id,
            decision_session_id=forecast.decision_session_id,
            probability_positive=None,
            expected_gross_return=None,
            expected_downside=None,
            downside_tail=None,
            uncertainty=None,
            estimated_cost=None,
            expected_net_return=None,
            probability_exceeds_cost=None,
            sample_size=0,
            reason_codes=tuple(reasons),
            policy_hash=self.canonical_hash,
            source_lineage_hash=lineage,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class ResearchActionPolicy:
    policy_id: str = SECURITIES_RESEARCH_ACTION_POLICY_V1
    minimum_survival_probability: Decimal = Decimal("0.55")
    maximum_uncertainty: Decimal = Decimal("0.35")
    maximum_exposure: Decimal = Decimal("0.10")

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    def decide(
        self,
        *,
        outcome: ExpectedOutcome,
        regime: RegimeSnapshot,
        agreement: Decimal,
        evidence_quality: Decimal,
        blocked: bool,
        current_exposure: Decimal,
        lineage: str,
    ) -> ResearchDecision:
        constraints: list[str] = []
        action = ResearchAction.ABSTAIN
        exposure = _ZERO
        if outcome.expected_net_return is None or outcome.uncertainty is None:
            constraints.append("INSUFFICIENT_EVIDENCE")
        if evidence_quality < Decimal("0.8"):
            constraints.append("EVIDENCE_QUALITY_FAILED")
        if outcome.expected_net_return is not None and outcome.expected_net_return <= _ZERO:
            constraints.append("EXPECTED_NET_RETURN_NONPOSITIVE")
        if outcome.uncertainty is not None and outcome.uncertainty > self.maximum_uncertainty:
            constraints.append("UNCERTAINTY_ABOVE_LIMIT")
        if (
            outcome.probability_exceeds_cost is not None
            and outcome.probability_exceeds_cost < self.minimum_survival_probability
        ):
            constraints.append("COST_SURVIVAL_PROBABILITY_LOW")
        if blocked:
            constraints.append("RESEARCH_ONLY_BLOCKED_MODEL")
        if regime.regime in {MarketRegime.HIGH_VOLATILITY_STRESS, MarketRegime.CORRELATION_SHOCK}:
            constraints.append("ADVERSE_REGIME")
        if not constraints:
            if current_exposure <= _ZERO:
                action = ResearchAction.OPEN_LONG
                exposure = self.maximum_exposure
            else:
                action = ResearchAction.HOLD
                exposure = min(self.maximum_exposure, current_exposure)
        elif current_exposure > _ZERO and (
            "EXPECTED_NET_RETURN_NONPOSITIVE" in constraints or "ADVERSE_REGIME" in constraints
        ):
            action = (
                ResearchAction.EXIT
                if outcome.expected_net_return is None or outcome.expected_net_return <= _ZERO
                else ResearchAction.REDUCE
            )
            exposure = _ZERO if action is ResearchAction.EXIT else q(current_exposure / Decimal(2))
        payload = {
            "outcome": outcome.canonical_hash,
            "regime": regime.canonical_hash,
            "constraints": tuple(constraints),
            "policy": self.canonical_hash,
            "lineage": lineage,
        }
        return ResearchDecision(
            instrument_id=outcome.instrument_id,
            decision_session_id=outcome.decision_session_id,
            action=action,
            desired_exposure=exposure,
            regime_fit=Decimal("0.25")
            if regime.regime
            in {MarketRegime.HIGH_VOLATILITY_STRESS, MarketRegime.CORRELATION_SHOCK}
            else Decimal("1"),
            agreement=clamp(agreement),
            binding_constraints=tuple(constraints),
            reason_codes=tuple(constraints) or ("OPEN_EVIDENCE_SUPPORTS_RESEARCH",),
            policy_hash=self.canonical_hash,
            source_lineage_hash=lineage,
            canonical_hash=content_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class OpportunityRankingPolicy:
    policy_id: str = SECURITIES_RESEARCH_OPPORTUNITY_RANKING_V1

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    def rank(
        self, *, outcomes: Sequence[ExpectedOutcome], decisions: Sequence[ResearchDecision]
    ) -> tuple[RankedOpportunity, ...]:
        by_id = {item.instrument_id: item for item in decisions}
        ranked: list[
            tuple[Decimal, ExpectedOutcome, ResearchDecision, tuple[tuple[str, Decimal], ...]]
        ] = []
        for outcome in outcomes:
            decision = by_id[outcome.instrument_id]
            net = outcome.expected_net_return or _ZERO
            survival = outcome.probability_exceeds_cost or _ZERO
            downside = abs(outcome.expected_downside or _ZERO)
            uncertainty = outcome.uncertainty or _ONE
            components = (
                ("expected_net_return", net),
                ("cost_survival_probability", survival),
                ("downside_penalty", -downside),
                ("uncertainty_penalty", -uncertainty),
                ("regime_fit", decision.regime_fit),
                ("agreement", decision.agreement),
            )
            score = q(
                net * Decimal("4")
                + survival * Decimal("0.3")
                - downside
                - uncertainty * Decimal("0.2")
                + decision.regime_fit * Decimal("0.05")
                + decision.agreement * Decimal("0.05")
            )
            ranked.append((score, outcome, decision, components))
        ranked.sort(key=lambda item: (-item[0], item[1].instrument_id))
        result = [
            RankedOpportunity(
                instrument_id=outcome.instrument_id,
                decision_session_id=outcome.decision_session_id,
                rank=index,
                composite_score=score,
                components=components,
                decision=decision,
                is_cash_alternative=False,
                canonical_hash=content_digest(
                    {
                        "score": score,
                        "outcome": outcome.canonical_hash,
                        "decision": decision.canonical_hash,
                        "policy": self.canonical_hash,
                    }
                ),
            )
            for index, (score, outcome, decision, components) in enumerate(ranked, 1)
        ]
        session = outcomes[0].decision_session_id if outcomes else "CASH"
        cash_decision = ResearchDecision(
            "CASH",
            session,
            ResearchAction.ABSTAIN,
            _ZERO,
            _ONE,
            _ONE,
            (),
            ("CASH_ALTERNATIVE",),
            self.canonical_hash,
            self.canonical_hash,
            content_digest({"cash": session, "policy": self.canonical_hash}),
        )
        result.append(
            RankedOpportunity(
                "CASH",
                session,
                len(result) + 1,
                _ZERO,
                (("cash_return", _ZERO),),
                cash_decision,
                True,
                content_digest({"cash": session, "policy": self.canonical_hash}),
            )
        )
        return tuple(result)
