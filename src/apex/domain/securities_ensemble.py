"""Pure contracts for the research-only Securities ensemble and meta-strategy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from apex.domain.strategy_intelligence import MarketRegime, clamp, mean, q, stddev
from apex.domain.values import content_digest

SECURITIES_ENSEMBLE_COMPONENT_V1 = "SECURITIES_ENSEMBLE_COMPONENT_V1"
SECURITIES_COMPONENT_CREDIBILITY_V1 = "SECURITIES_COMPONENT_CREDIBILITY_V1"
SECURITIES_REGIME_AWARE_ENSEMBLE_V1 = "SECURITIES_REGIME_AWARE_ENSEMBLE_V1"
SECURITIES_ENSEMBLE_UNCERTAINTY_V1 = "SECURITIES_ENSEMBLE_UNCERTAINTY_V1"
SECURITIES_META_STRATEGY_ACTION_V1 = "SECURITIES_META_STRATEGY_ACTION_V1"
SECURITIES_META_PORTFOLIO_PLAN_V1 = "SECURITIES_META_PORTFOLIO_PLAN_V1"


class EnsembleComponentFamily(StrEnum):
    CASH = "CASH"
    BUY_AND_HOLD = "BUY_AND_HOLD"
    TIME_SERIES_MOMENTUM = "TIME_SERIES_MOMENTUM"
    CROSS_SECTIONAL_MOMENTUM = "CROSS_SECTIONAL_MOMENTUM"
    SHORT_HORIZON_MEAN_REVERSION = "SHORT_HORIZON_MEAN_REVERSION"
    VOLATILITY_TARGETED_LONG_ONLY = "VOLATILITY_TARGETED_LONG_ONLY"
    LOGISTIC_REGRESSION_RESEARCH = "LOGISTIC_REGRESSION_RESEARCH"
    HISTOGRAM_GRADIENT_BOOSTING_RESEARCH = "HISTOGRAM_GRADIENT_BOOSTING_RESEARCH"
    MARKET_RELATIVE_STRENGTH = "MARKET_RELATIVE_STRENGTH"
    STRATEGY_INTELLIGENCE_EXPECTED_OUTCOME = "STRATEGY_INTELLIGENCE_EXPECTED_OUTCOME"


class ComponentEligibility(StrEnum):
    ELIGIBLE_RESEARCH_INPUT = "ELIGIBLE_RESEARCH_INPUT"
    RESEARCH_ONLY_BLOCKED = "RESEARCH_ONLY_BLOCKED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    STALE = "STALE"
    INVALID_LINEAGE = "INVALID_LINEAGE"
    INCOMPARABLE = "INCOMPARABLE"


class MetaAction(StrEnum):
    OPEN_LONG = "OPEN_LONG"
    HOLD = "HOLD"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True, slots=True)
class EnsembleComponent:
    instrument_id: str
    session_id: str
    family: EnsembleComponentFamily
    forecast: Decimal | None
    probability_positive: Decimal | None
    expected_gross_return: Decimal | None
    expected_net_return: Decimal | None
    downside: Decimal | None
    uncertainty: Decimal | None
    regime_suitability: Decimal
    calibration_state: str
    evidence_quality: Decimal
    simulator_state: str
    integrity_state: str
    eligibility: ComponentEligibility
    reason_codes: tuple[str, ...]
    source_hashes: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class ComponentCredibility:
    component_hash: str
    raw_factors: tuple[tuple[str, Decimal], ...]
    penalties: tuple[tuple[str, Decimal], ...]
    credibility: Decimal
    ceiling: Decimal
    reason_codes: tuple[str, ...]
    policy_hash: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class EnsembleForecast:
    instrument_id: str
    session_id: str
    component_weights: tuple[tuple[str, Decimal], ...]
    cash_weight: Decimal
    probability_positive: Decimal
    expected_gross_return: Decimal
    expected_net_return: Decimal
    downside: Decimal
    uncertainty: Decimal
    disagreement: Decimal
    effective_components: Decimal
    regime: MarketRegime
    regime_confidence: Decimal
    excluded_reasons: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class MetaDecision:
    instrument_id: str
    session_id: str
    action: MetaAction
    prior_exposure: Decimal
    desired_exposure: Decimal
    expected_net_return: Decimal
    downside: Decimal
    uncertainty: Decimal
    binding_rules: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class MetaPortfolioPlan:
    session_id: str
    weights: tuple[tuple[str, Decimal], ...]
    cash_weight: Decimal
    gross_exposure: Decimal
    turnover: Decimal
    expected_net_return: Decimal
    expected_downside: Decimal
    uncertainty: Decimal
    binding_rules: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class ComponentCredibilityPolicy:
    policy_id: str = SECURITIES_COMPONENT_CREDIBILITY_V1

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    def score(self, component: EnsembleComponent) -> ComponentCredibility:
        if component.eligibility is ComponentEligibility.INVALID_LINEAGE:
            return ComponentCredibility(
                component.canonical_hash,
                (),
                (("invalid_lineage", Decimal(1)),),
                Decimal(),
                Decimal(),
                ("INVALID_LINEAGE",),
                self.canonical_hash,
                content_digest((component.canonical_hash, "invalid")),
            )
        factors = (
            ("evidence_quality", component.evidence_quality),
            ("regime_suitability", component.regime_suitability),
            ("certainty", Decimal(1) - (component.uncertainty or Decimal(1))),
        )
        ceiling = Decimal(1)
        penalties: list[tuple[str, Decimal]] = []
        if component.eligibility is ComponentEligibility.RESEARCH_ONLY_BLOCKED:
            ceiling = Decimal("0.20")
            penalties.append(("blocked_research", Decimal("0.80")))
        if component.simulator_state == "MATERIAL_MISMATCH":
            ceiling = min(ceiling, Decimal("0.35"))
            penalties.append(("simulator_mismatch", Decimal("0.15")))
        if (
            component.eligibility is not ComponentEligibility.ELIGIBLE_RESEARCH_INPUT
            and component.eligibility is not ComponentEligibility.RESEARCH_ONLY_BLOCKED
        ):
            ceiling = Decimal()
            penalties.append(("ineligible", Decimal(1)))
        score = min(
            ceiling,
            clamp(
                mean(tuple(value for _name, value in factors))
                - sum((value for _name, value in penalties), Decimal())
            ),
        )
        return ComponentCredibility(
            component.canonical_hash,
            factors,
            tuple(penalties),
            score,
            ceiling,
            tuple(name.upper() for name, _value in penalties),
            self.canonical_hash,
            content_digest(
                (component.canonical_hash, factors, penalties, score, self.canonical_hash)
            ),
        )


@dataclass(frozen=True, slots=True)
class RegimeAwareEnsemblePolicy:
    policy_id: str = SECURITIES_REGIME_AWARE_ENSEMBLE_V1

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    def combine(
        self,
        components: Sequence[EnsembleComponent],
        credibility: Sequence[ComponentCredibility],
        regime: MarketRegime,
        confidence: Decimal,
    ) -> EnsembleForecast:
        if not components or len(components) != len(credibility):
            raise ValueError("ENSEMBLE_COMPONENT_ALIGNMENT_INVALID")
        included = [
            (component, value)
            for component, value in zip(components, credibility, strict=True)
            if component.eligibility
            in {
                ComponentEligibility.ELIGIBLE_RESEARCH_INPUT,
                ComponentEligibility.RESEARCH_ONLY_BLOCKED,
            }
            and value.credibility > 0
            and component.forecast is not None
        ]
        if not included:
            raise ValueError("ENSEMBLE_NO_COMPONENTS")
        raw = [q(value.credibility * component.regime_suitability) for component, value in included]
        total = sum(raw, Decimal())
        weights = [q(value / total) for value in raw]
        probabilities = tuple(
            component.probability_positive or component.forecast or Decimal("0.5")
            for component, _value in included
        )
        nets = tuple(component.expected_net_return or Decimal() for component, _value in included)
        gross = tuple(
            component.expected_gross_return or Decimal() for component, _value in included
        )
        downside = tuple(component.downside or Decimal() for component, _value in included)
        dispersion = stddev(probabilities)
        blocked_weight = sum(
            (
                weight
                for weight, (component, _value) in zip(weights, included, strict=True)
                if component.eligibility is ComponentEligibility.RESEARCH_ONLY_BLOCKED
            ),
            Decimal(),
        )
        cash = clamp(dispersion + blocked_weight + (Decimal(1) - confidence) * Decimal("0.25"))
        scalar = Decimal(1) - cash
        effective = q(
            Decimal(1)
            / max(
                sum((weight * weight for weight in weights), Decimal("0.00000001")),
                Decimal("0.00000001"),
            )
        )
        instrument, session = included[0][0].instrument_id, included[0][0].session_id
        return EnsembleForecast(
            instrument,
            session,
            tuple(
                (component.family.value, q(weight * scalar))
                for weight, (component, _value) in zip(weights, included, strict=True)
            ),
            cash,
            q(
                sum(
                    (weight * value for weight, value in zip(weights, probabilities, strict=True)),
                    Decimal(),
                )
            ),
            q(
                sum(
                    (weight * value for weight, value in zip(weights, gross, strict=True)),
                    Decimal(),
                )
            ),
            q(
                sum(
                    (weight * value for weight, value in zip(weights, nets, strict=True)), Decimal()
                )
            ),
            q(
                sum(
                    (weight * value for weight, value in zip(weights, downside, strict=True)),
                    Decimal(),
                )
            ),
            clamp(dispersion + blocked_weight + (Decimal(1) / effective)),
            dispersion,
            effective,
            regime,
            confidence,
            tuple(
                sorted(
                    {reason for component, _value in included for reason in component.reason_codes}
                )
            ),
            content_digest((instrument, session, weights, cash, self.canonical_hash)),
        )


@dataclass(frozen=True, slots=True)
class MetaStrategyActionPolicy:
    policy_id: str = SECURITIES_META_STRATEGY_ACTION_V1
    maximum_uncertainty: Decimal = Decimal("0.35")
    maximum_exposure: Decimal = Decimal("0.10")

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    def decide(
        self,
        forecast: EnsembleForecast,
        *,
        prior_exposure: Decimal,
        transparent_support: bool,
        quality: Decimal,
    ) -> MetaDecision:
        rules: list[str] = []
        if forecast.expected_net_return <= 0:
            rules.append("EXPECTED_NET_NONPOSITIVE")
        if forecast.uncertainty > self.maximum_uncertainty:
            rules.append("UNCERTAINTY_LIMIT")
        if quality < Decimal("0.8"):
            rules.append("EVIDENCE_QUALITY_FAILED")
        if not transparent_support:
            rules.append("TRANSPARENT_SUPPORT_REQUIRED")
        if forecast.regime in {MarketRegime.HIGH_VOLATILITY_STRESS, MarketRegime.CORRELATION_SHOCK}:
            rules.append("ADVERSE_REGIME")
        action, desired = MetaAction.ABSTAIN, Decimal()
        if not rules:
            action, desired = (
                (MetaAction.OPEN_LONG, self.maximum_exposure)
                if prior_exposure == 0
                else (MetaAction.HOLD, prior_exposure)
            )
        elif prior_exposure > 0 and (
            "EXPECTED_NET_NONPOSITIVE" in rules or "ADVERSE_REGIME" in rules
        ):
            action, desired = MetaAction.EXIT, Decimal()
        return MetaDecision(
            forecast.instrument_id,
            forecast.session_id,
            action,
            prior_exposure,
            desired,
            forecast.expected_net_return,
            forecast.downside,
            forecast.uncertainty,
            tuple(rules),
            content_digest((forecast.canonical_hash, action, desired, rules, self.canonical_hash)),
        )


@dataclass(frozen=True, slots=True)
class MetaPortfolioPolicy:
    policy_id: str = SECURITIES_META_PORTFOLIO_PLAN_V1
    per_instrument_cap: Decimal = Decimal("0.10")

    @property
    def canonical_hash(self) -> str:
        return content_digest(self)

    def plan(
        self, decisions: Sequence[MetaDecision], forecasts: Sequence[EnsembleForecast]
    ) -> MetaPortfolioPlan:
        by_id = {item.instrument_id: item for item in forecasts}
        weights = tuple(
            sorted(
                (
                    (item.instrument_id, min(self.per_instrument_cap, item.desired_exposure))
                    for item in decisions
                    if item.desired_exposure > 0
                ),
                key=lambda item: item[0],
            )
        )
        gross = sum((value for _name, value in weights), Decimal())
        cash = q(Decimal(1) - gross)
        expected = sum(
            (by_id[name].expected_net_return * weight for name, weight in weights), Decimal()
        )
        downside = sum((by_id[name].downside * weight for name, weight in weights), Decimal())
        uncertainty = (
            mean(tuple(by_id[name].uncertainty for name, _weight in weights))
            if weights
            else Decimal(1)
        )
        session = decisions[0].session_id if decisions else "CASH"
        return MetaPortfolioPlan(
            session,
            weights,
            cash,
            gross,
            gross,
            q(expected),
            q(downside),
            uncertainty,
            ("LONG_ONLY", "NO_LEVERAGE", "CASH_REMAINDER"),
            content_digest((session, weights, cash, self.canonical_hash)),
        )
