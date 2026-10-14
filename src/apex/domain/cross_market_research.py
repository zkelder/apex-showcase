"""Controlled cross-market feature, regime, and hypothesis research
(Cross-Market Feature, Regime, and Controlled Hypothesis Research v1).

This module is Apex's first *bounded, governed* research-agenda layer. It
does not discover strategies and does not create execution authority --
every retained record below is ``authority = "NONE"``. It exists to let
Apex ask a small, predeclared set of economically plausible cross-market
questions against genuinely retained history and report, honestly, how
many of them survive real methodology.

Built entirely on top of existing, unmodified foundations:

* ``domain.historical_market_data`` / ``domain.historical_replay`` for
  point-in-time gating (``assert_no_lookahead``), the shared cross-venue
  ``HistoricalObservationView`` shape, and the six equity regime-v1
  classifiers this module reuses directly for the equity and BTC regime
  dimensions below.
* ``domain.cross_market_alignment`` for venue-agnostic availability/age
  classification.
* ``domain.research_cohort`` for the genuine 93-member cohort's declared
  roles (``CohortRole``) and sectors -- large/small, growth/value,
  cyclical/defensive, and rates/commodity proxy groupings all come from
  that already-retained, performance-blind declaration, never invented
  here.
* ``domain.research_methodology`` for the entire governed trial lifecycle
  (phases, holdout-by-default governance, multiple-testing correction,
  robustness result/summary shapes, and the conclusion/promotion
  taxonomy) -- this module never redefines any of that vocabulary.

What is genuinely new here: a bounded feature/outcome catalog computed
once per agenda run and shared by every hypothesis; a small, additive
"regime v2" layer (bond/risk relationship and commodity context, plus
BTC trend/volatility reusing the identical generic classifiers); five
controlled hypothesis templates; deterministic (seeded, reproducible)
negative-control construction; and the agenda/proposal/knowledge/
contradiction/follow-up/quality-report records the milestone asks for.

Non-goals (deliberate): no free-form hypothesis text, no unbounded
parameter search, no causal language, no automatic Strategy/Opportunity/
PAPER/LIVE creation, no external-model authority.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum

from apex.domain.historical_replay import (
    CorrelationRegimeLabel,
    classify_breadth_strength,
    classify_correlation_regime,
    classify_dispersion,
    classify_risk_tone,
    classify_trend_strength,
    classify_volatility_regime,
)
from apex.domain.values import content_digest, deterministic_id

CROSS_MARKET_RESEARCH_V1 = "CONTROLLED_CROSS_MARKET_HYPOTHESIS_RESEARCH_V1"
RESEARCH_WARNING = (
    "CONTROLLED HYPOTHESIS RESEARCH EVIDENCE ONLY -- NO STRATEGY, PAPER, OR LIVE AUTHORITY"
)
CAUSAL_LANGUAGE_POLICY = (
    "Apex may conclude a relationship is historically associated with, conditionally "
    "predictive in this sample, or improves benchmark-relative discrimination. It may "
    "not conclude a relationship 'causes' an outcome; no method used by this milestone "
    "supports causal inference."
)


class CrossMarketResearchError(ValueError):
    """A controlled-research invariant was violated."""


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CrossMarketResearchError(f"{label}_MUST_BE_TIMEZONE_AWARE")
    return value


def _text(value: str, code: str, *, maximum: int = 500) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise CrossMarketResearchError(code)
    return normalized


def _bounded_decimal(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return value.quantize(Decimal("0.00000001"))


def _decimal_sqrt(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        if value < 0:
            raise CrossMarketResearchError("CROSS_MARKET_SQRT_NEGATIVE_INPUT")
        return +value.sqrt()


# ==========================================================================
# Section 1: research-agenda model (the budget controlling the search space)
# ==========================================================================


class MarketId(StrEnum):
    """Every market this milestone may touch -- deliberately four, not a
    provider/venue list. ``RATES_PROXY``/``COMMODITY_PROXY`` are ETF
    baskets traded on the same equity venue as ``US_EQUITY``; they are
    named as proxies precisely so no feature or conclusion can imply a
    genuine bond/commodity market observation (see module docstring)."""

    US_EQUITY = "US_EQUITY"
    RATES_PROXY = "RATES_PROXY"
    COMMODITY_PROXY = "COMMODITY_PROXY"
    BTC = "BTC"


class FeatureFamily(StrEnum):
    EQUITY_BREADTH = "EQUITY_BREADTH"
    EQUITY_DISPERSION = "EQUITY_DISPERSION"
    EQUITY_VOLATILITY = "EQUITY_VOLATILITY"
    EQUITY_TREND = "EQUITY_TREND"
    EQUITY_CORRELATION = "EQUITY_CORRELATION"
    EQUITY_STYLE_RELATIVE_STRENGTH = "EQUITY_STYLE_RELATIVE_STRENGTH"
    EQUITY_SECTOR_BREADTH = "EQUITY_SECTOR_BREADTH"
    RATES_PROXY_TREND = "RATES_PROXY_TREND"
    RATES_EQUITY_RELATIONSHIP = "RATES_EQUITY_RELATIONSHIP"
    COMMODITY_PROXY_TREND = "COMMODITY_PROXY_TREND"
    COMMODITY_EQUITY_RELATIONSHIP = "COMMODITY_EQUITY_RELATIONSHIP"
    BTC_MOMENTUM = "BTC_MOMENTUM"
    BTC_VOLATILITY = "BTC_VOLATILITY"
    BTC_EQUITY_RELATIONSHIP = "BTC_EQUITY_RELATIONSHIP"
    NEGATIVE_CONTROL = "NEGATIVE_CONTROL"


class OutcomeFamily(StrEnum):
    EQUITY_BROAD_RETURN = "EQUITY_BROAD_RETURN"
    EQUITY_BROAD_VOLATILITY = "EQUITY_BROAD_VOLATILITY"
    EQUITY_BROAD_DISPERSION = "EQUITY_BROAD_DISPERSION"
    RATES_PROXY_RETURN = "RATES_PROXY_RETURN"
    BTC_RETURN = "BTC_RETURN"
    BTC_VOLATILITY = "BTC_VOLATILITY"


class HypothesisTemplateId(StrEnum):
    """The only five shapes a hypothesis may take this milestone (section
    4). ``GeneratedHypothesis.template_id`` must be one of these; nothing
    -- deterministic generator or assisted seam alike -- may construct a
    free-form hypothesis."""

    PREDICTIVE_RELATIONSHIP_V1 = "PREDICTIVE_RELATIONSHIP_V1"
    CONDITIONAL_RELATIONSHIP_V1 = "CONDITIONAL_RELATIONSHIP_V1"
    CROSS_MARKET_RELATIONSHIP_V1 = "CROSS_MARKET_RELATIONSHIP_V1"
    INTERACTION_V1 = "INTERACTION_V1"
    PERSISTENCE_V1 = "PERSISTENCE_V1"
    # Added for Cross-Market Research v3 (``domain.corporate_market_research``):
    # three genuinely new template shapes no v1 member covers -- many
    # companies compared at one as_of (cross-sectional), a change in a
    # corporate feature between two periods (change-based), and an SEC
    # filing-availability event (filing-event). ``CONDITIONAL_RELATIONSHIP_V1``
    # and ``INTERACTION_V1`` above are reused verbatim by v3's own
    # conditional/interaction designs -- their meaning ("a feature behaves
    # differently under regime R" / "the X-Y relationship changes when Z is
    # present") is already generic, not price-feature-specific.
    CROSS_SECTIONAL_V1 = "CROSS_SECTIONAL_V1"
    CORPORATE_CHANGE_V1 = "CORPORATE_CHANGE_V1"
    FILING_EVENT_V1 = "FILING_EVENT_V1"


class HypothesisFamily(StrEnum):
    """The multiple-testing partition (section 7/16): each hypothesis
    belongs to exactly one family, and correction is applied within each
    family separately (a "clearly partitioned family" scheme)."""

    EQUITY_INTERNAL = "EQUITY_INTERNAL"
    EQUITY_BTC = "EQUITY_BTC"
    FIXED_INCOME_CONTEXT = "FIXED_INCOME_CONTEXT"
    COMMODITY_CONTEXT = "COMMODITY_CONTEXT"
    NEGATIVE_CONTROL = "NEGATIVE_CONTROL"


class DiscoveryStatus(StrEnum):
    """Section 11: discovery versus confirmation, propagated onto every
    conclusion built from a hypothesis carrying this status."""

    PREDECLARED = "PREDECLARED"
    DISCOVERED_THIS_AGENDA = "DISCOVERED_THIS_AGENDA"
    NEGATIVE_CONTROL = "NEGATIVE_CONTROL"


class ProposalSource(StrEnum):
    """Section 5: the only two controlled proposal mechanisms."""

    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"
    ASSISTED_MODEL_SEAM = "ASSISTED_MODEL_SEAM"


class HypothesisGenerationMethod(StrEnum):
    DETERMINISTIC_TEMPLATE_EXPANSION_V1 = "DETERMINISTIC_TEMPLATE_EXPANSION_V1"


NEGATIVE_CONTROL_KINDS = (
    "SHUFFLED_FEATURE",
    "LAG_DISPLACED_FEATURE",
    "ECONOMICALLY_UNRELATED_PAIR",
    "RANDOM_DATE_BASELINE",
    "PERSISTENCE_BASELINE",
    "UNCONDITIONAL_OUTCOME",
)


@dataclass(frozen=True, slots=True)
class ResearchAgenda:
    """The declared search-space budget (section 1). Registering an
    agenda is the *only* way its hypotheses may later be proposed --
    nothing here searches an unbounded Cartesian product of features x
    horizons x regimes x assets; every bound below is a hard ceiling the
    application layer must respect, checked again at proposal time."""

    agenda_id: str
    version: str
    objective: str
    allowed_markets: tuple[MarketId, ...]
    allowed_feature_families: tuple[FeatureFamily, ...]
    allowed_outcome_families: tuple[OutcomeFamily, ...]
    allowed_horizons_sessions: tuple[int, ...]
    allowed_templates: tuple[HypothesisTemplateId, ...]
    hypothesis_generation_method: HypothesisGenerationMethod
    maximum_hypothesis_count: int
    maximum_configurations_per_hypothesis: int
    maximum_total_trials: int
    train_validation_holdout_policy: str
    multiple_testing_family_policy: str
    robustness_requirements: tuple[str, ...]
    dataset_snapshot_id: str
    replay_run_id: str
    cross_market_dataset_id: str
    cohort_policy_id: str
    registered_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        version: str,
        objective: str,
        allowed_markets: tuple[MarketId | str, ...],
        allowed_feature_families: tuple[FeatureFamily | str, ...],
        allowed_outcome_families: tuple[OutcomeFamily | str, ...],
        allowed_horizons_sessions: tuple[int, ...],
        allowed_templates: tuple[HypothesisTemplateId | str, ...],
        hypothesis_generation_method: HypothesisGenerationMethod | str,
        maximum_hypothesis_count: int,
        maximum_configurations_per_hypothesis: int,
        maximum_total_trials: int,
        train_validation_holdout_policy: str,
        multiple_testing_family_policy: str,
        robustness_requirements: tuple[str, ...],
        dataset_snapshot_id: str,
        replay_run_id: str,
        cross_market_dataset_id: str,
        cohort_policy_id: str,
        registered_at: datetime,
    ) -> ResearchAgenda:
        if not (1 <= maximum_hypothesis_count <= 30):
            raise CrossMarketResearchError("AGENDA_HYPOTHESIS_COUNT_MUST_BE_SMALL_AND_BOUNDED")
        if not (1 <= maximum_configurations_per_hypothesis <= 4):
            raise CrossMarketResearchError("AGENDA_CONFIGURATIONS_PER_HYPOTHESIS_OUT_OF_BOUNDS")
        maximum_possible_trials = maximum_hypothesis_count * maximum_configurations_per_hypothesis
        if maximum_total_trials < maximum_possible_trials:
            raise CrossMarketResearchError("AGENDA_TOTAL_TRIAL_BUDGET_TOO_SMALL_FOR_ITS_OWN_BOUNDS")
        if maximum_total_trials > 2_000:
            raise CrossMarketResearchError("AGENDA_TOTAL_TRIAL_BUDGET_UNBOUNDED")
        markets = tuple(MarketId(item) for item in allowed_markets)
        if not markets:
            raise CrossMarketResearchError("AGENDA_REQUIRES_AT_LEAST_ONE_ALLOWED_MARKET")
        feature_families = tuple(FeatureFamily(item) for item in allowed_feature_families)
        outcome_families = tuple(OutcomeFamily(item) for item in allowed_outcome_families)
        if not feature_families or not outcome_families:
            raise CrossMarketResearchError("AGENDA_REQUIRES_FEATURE_AND_OUTCOME_FAMILIES")
        horizons = tuple(sorted(set(allowed_horizons_sessions)))
        if not horizons or len(horizons) > 5 or any(item < 1 for item in horizons):
            raise CrossMarketResearchError("AGENDA_HORIZON_SET_MUST_BE_SMALL_AND_POSITIVE")
        templates = tuple(HypothesisTemplateId(item) for item in allowed_templates)
        if not templates:
            raise CrossMarketResearchError("AGENDA_REQUIRES_AT_LEAST_ONE_TEMPLATE")
        method = HypothesisGenerationMethod(hypothesis_generation_method)
        if not dataset_snapshot_id or not replay_run_id or not cross_market_dataset_id:
            raise CrossMarketResearchError("AGENDA_DATASET_LINEAGE_REQUIRED")
        if not cohort_policy_id:
            raise CrossMarketResearchError("AGENDA_COHORT_POLICY_REQUIRED")
        requirements = tuple(sorted(set(robustness_requirements)))
        registered = _aware(registered_at, "AGENDA_REGISTERED_AT")
        payload = (
            CROSS_MARKET_RESEARCH_V1,
            _text(version, "AGENDA_VERSION_INVALID", maximum=80),
            _text(objective, "AGENDA_OBJECTIVE_INVALID", maximum=2_000),
            markets,
            feature_families,
            outcome_families,
            horizons,
            templates,
            method,
            maximum_hypothesis_count,
            maximum_configurations_per_hypothesis,
            maximum_total_trials,
            _text(
                train_validation_holdout_policy, "AGENDA_PERIOD_POLICY_TEXT_INVALID", maximum=200
            ),
            _text(
                multiple_testing_family_policy,
                "AGENDA_MULTIPLE_TESTING_POLICY_TEXT_INVALID",
                maximum=200,
            ),
            requirements,
            dataset_snapshot_id,
            replay_run_id,
            cross_market_dataset_id,
            cohort_policy_id,
        )
        return cls(
            agenda_id=deterministic_id("cross_market_research_agenda", payload),
            version=payload[1],
            objective=payload[2],
            allowed_markets=markets,
            allowed_feature_families=feature_families,
            allowed_outcome_families=outcome_families,
            allowed_horizons_sessions=horizons,
            allowed_templates=templates,
            hypothesis_generation_method=method,
            maximum_hypothesis_count=maximum_hypothesis_count,
            maximum_configurations_per_hypothesis=maximum_configurations_per_hypothesis,
            maximum_total_trials=maximum_total_trials,
            train_validation_holdout_policy=payload[12],
            multiple_testing_family_policy=payload[13],
            robustness_requirements=requirements,
            dataset_snapshot_id=dataset_snapshot_id,
            replay_run_id=replay_run_id,
            cross_market_dataset_id=cross_market_dataset_id,
            cohort_policy_id=cohort_policy_id,
            registered_at=registered,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 2: bounded feature/outcome catalog
# ==========================================================================


class FeatureId(StrEnum):
    """The entire feature library (section 2). Every member is a named
    attribute of ``CrossMarketObservation`` below -- there is no dynamic
    or free-form feature construction; a hypothesis may only reference a
    member of this enum (or, for negative controls, one of the synthetic
    ids in ``NEGATIVE_CONTROL_FEATURE_IDS``)."""

    EQUITY_RETURN_5 = "EQUITY_RETURN_5"
    EQUITY_RETURN_20 = "EQUITY_RETURN_20"
    EQUITY_RETURN_60 = "EQUITY_RETURN_60"
    EQUITY_RETURN_90 = "EQUITY_RETURN_90"
    EQUITY_RETURN_252 = "EQUITY_RETURN_252"
    EQUITY_BREADTH_FRACTION_ADVANCING = "EQUITY_BREADTH_FRACTION_ADVANCING"
    EQUITY_DISPERSION_20 = "EQUITY_DISPERSION_20"
    EQUITY_VOLATILITY_20 = "EQUITY_VOLATILITY_20"
    EQUITY_DRAWDOWN = "EQUITY_DRAWDOWN"
    EQUITY_AVERAGE_CORRELATION_60 = "EQUITY_AVERAGE_CORRELATION_60"
    EQUITY_LARGE_MINUS_SMALL_20 = "EQUITY_LARGE_MINUS_SMALL_20"
    EQUITY_GROWTH_MINUS_VALUE_20 = "EQUITY_GROWTH_MINUS_VALUE_20"
    EQUITY_CYCLICAL_MINUS_DEFENSIVE_20 = "EQUITY_CYCLICAL_MINUS_DEFENSIVE_20"
    EQUITY_SECTOR_BREADTH_20 = "EQUITY_SECTOR_BREADTH_20"
    EQUITY_SECTOR_DISPERSION_20 = "EQUITY_SECTOR_DISPERSION_20"
    RATES_PROXY_RETURN_20 = "RATES_PROXY_RETURN_20"
    RATES_EQUITY_CORRELATION_60 = "RATES_EQUITY_CORRELATION_60"
    COMMODITY_PROXY_RETURN_20 = "COMMODITY_PROXY_RETURN_20"
    COMMODITY_EQUITY_CORRELATION_60 = "COMMODITY_EQUITY_CORRELATION_60"
    BTC_RETURN_5 = "BTC_RETURN_5"
    BTC_RETURN_20 = "BTC_RETURN_20"
    BTC_RETURN_60 = "BTC_RETURN_60"
    BTC_RETURN_90 = "BTC_RETURN_90"
    BTC_VOLATILITY_20 = "BTC_VOLATILITY_20"
    BTC_DRAWDOWN = "BTC_DRAWDOWN"
    BTC_EQUITY_CORRELATION_60 = "BTC_EQUITY_CORRELATION_60"
    EQUITY_DAILY_RETURN_SIGN = "EQUITY_DAILY_RETURN_SIGN"
    ALWAYS_PRESENT_BASELINE = "ALWAYS_PRESENT_BASELINE"
    SHUFFLED_EQUITY_BREADTH = "SHUFFLED_EQUITY_BREADTH"
    LAG_DISPLACED_EQUITY_BREADTH = "LAG_DISPLACED_EQUITY_BREADTH"
    RANDOM_NOISE_BASELINE = "RANDOM_NOISE_BASELINE"
    # Categorical (regime-label-backed) conditions, used with
    # ``ConditionRule.LABEL_EQUALS`` by the CONDITIONAL/INTERACTION/
    # PERSISTENCE templates -- each maps to exactly one
    # ``CrossMarketRegimeDimension`` via ``REGIME_CONDITION_FEATURE_IDS``
    # below rather than a numeric threshold.
    EQUITY_TREND_REGIME_LABEL = "EQUITY_TREND_REGIME_LABEL"
    EQUITY_RISK_TONE_REGIME_LABEL = "EQUITY_RISK_TONE_REGIME_LABEL"
    EQUITY_VOLATILITY_REGIME_LABEL = "EQUITY_VOLATILITY_REGIME_LABEL"
    BOND_RISK_RELATIONSHIP_REGIME_LABEL = "BOND_RISK_RELATIONSHIP_REGIME_LABEL"


NEGATIVE_CONTROL_FEATURE_IDS = frozenset(
    {
        FeatureId.SHUFFLED_EQUITY_BREADTH,
        FeatureId.LAG_DISPLACED_EQUITY_BREADTH,
        FeatureId.RANDOM_NOISE_BASELINE,
        FeatureId.ALWAYS_PRESENT_BASELINE,
        FeatureId.EQUITY_DAILY_RETURN_SIGN,
    }
)


class OutcomeId(StrEnum):
    EQUITY_FORWARD_RETURN = "EQUITY_FORWARD_RETURN"
    EQUITY_FORWARD_VOLATILITY = "EQUITY_FORWARD_VOLATILITY"
    EQUITY_FORWARD_DISPERSION = "EQUITY_FORWARD_DISPERSION"
    RATES_PROXY_FORWARD_RETURN = "RATES_PROXY_FORWARD_RETURN"
    BTC_FORWARD_RETURN = "BTC_FORWARD_RETURN"
    BTC_FORWARD_VOLATILITY = "BTC_FORWARD_VOLATILITY"
    # Used only by the PERSISTENCE template: a 0/1 indicator of whether the
    # source regime/sign condition still holds ``horizon_sessions`` later
    # -- never one of the six genuine forward-market outcomes above.
    PERSISTENCE_INDICATOR = "PERSISTENCE_INDICATOR"


@dataclass(frozen=True, slots=True)
class CrossMarketObservation:
    """One as_of's entire genuine cross-market feature panel -- computed
    once per agenda run and reused by every hypothesis (section 24: "do
    not rebuild identical feature frames per hypothesis"). Every field is
    point-in-time-safe as of ``as_of``; ``None`` means the lookback was
    not yet available, never a fabricated value."""

    session_date: str
    as_of: datetime
    features: tuple[tuple[FeatureId, Decimal | None], ...]
    regime: CrossMarketRegimeSnapshot
    data_quality_flags: tuple[str, ...]

    def feature(self, feature_id: FeatureId) -> Decimal | None:
        for key, value in self.features:
            if key is feature_id:
                return value
        return None


@dataclass(frozen=True, slots=True)
class CrossMarketOutcomeSet:
    """The forward-looking counterpart of ``CrossMarketObservation``:
    genuinely future values as measured from ``session_date`` to
    ``session_date + horizon_sessions`` sessions later. These are never
    point-in-time-gated at ``session_date`` (an outcome is defined to look
    forward by construction, exactly like ``domain.historical_replay.
    OutcomeWindowDeclaration``); what *is* enforced is that no
    ``CrossMarketObservation`` feature ever reads one of these values."""

    session_date: str
    horizon_sessions: int
    outcomes: tuple[tuple[OutcomeId, Decimal | None], ...]
    complete: bool

    def outcome(self, outcome_id: OutcomeId) -> Decimal | None:
        for key, value in self.outcomes:
            if key is outcome_id:
                return value
        return None


# ==========================================================================
# Section 3: generic point-in-time series math over HistoricalObservationView
# (venue-agnostic by construction -- an equity bar, TLT, DBC, or a BTC
# candle all reach this code as the identical shared shape.)
# ==========================================================================


@dataclass(frozen=True, slots=True)
class DatedValue:
    session_date: str
    value: Decimal


def to_dated_closes(
    views: Sequence[object], *, event_at_attr: str = "event_at", close_attr: str = "close"
) -> tuple[DatedValue, ...]:
    """Reduces an already point-in-time-visible, chronologically sorted
    ``HistoricalObservationView`` sequence to one (date, close) pair per
    calendar day (last observation wins for that day -- there is at most
    one per venue's own daily bar/candle, so this is a no-op except for
    ordering safety)."""

    ordered = sorted(views, key=lambda item: getattr(item, event_at_attr))
    by_date: dict[str, Decimal] = {}
    for item in ordered:
        event_at = getattr(item, event_at_attr)
        by_date[event_at.date().isoformat()] = getattr(item, close_attr)
    return tuple(DatedValue(date, value) for date, value in sorted(by_date.items()))


def series_return(
    series: Sequence[DatedValue], *, as_of_date: str, window_sessions: int
) -> Decimal | None:
    """Simple return over the trailing ``window_sessions`` closes visible
    at (and including) ``as_of_date``. ``None`` when the lookback is not
    yet fully covered -- never silently shortened."""

    if window_sessions < 1:
        raise CrossMarketResearchError("SERIES_RETURN_WINDOW_INVALID")
    trimmed = tuple(item for item in series if item.session_date <= as_of_date)
    if len(trimmed) < window_sessions + 1:
        return None
    first, last = trimmed[-(window_sessions + 1)], trimmed[-1]
    if first.value == 0:
        return None
    with localcontext() as context:
        context.prec = 28
        return _bounded_decimal((last.value - first.value) / first.value)


def series_annualized_volatility(
    series: Sequence[DatedValue], *, as_of_date: str, window_sessions: int, sessions_per_year: int
) -> Decimal | None:
    """Population stdev of trailing daily simple returns, annualized by
    ``sessions_per_year`` (252 for equities/rates/commodity proxies, 365
    for BTC, matching each venue's own trading calendar)."""

    if window_sessions < 2:
        raise CrossMarketResearchError("SERIES_VOLATILITY_WINDOW_INVALID")
    trimmed = tuple(item for item in series if item.session_date <= as_of_date)
    if len(trimmed) < window_sessions + 1:
        return None
    tail = trimmed[-(window_sessions + 1) :]
    with localcontext() as context:
        context.prec = 28
        returns = [
            (tail[index].value - tail[index - 1].value) / tail[index - 1].value
            for index in range(1, len(tail))
            if tail[index - 1].value != 0
        ]
        if len(returns) < 2:
            return None
        mean = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / Decimal(
            len(returns)
        )
        annualized = _decimal_sqrt(variance) * _decimal_sqrt(Decimal(sessions_per_year))
    return _bounded_decimal(annualized)


def series_drawdown(series: Sequence[DatedValue], *, as_of_date: str) -> Decimal | None:
    """Maximum peak-to-trough drawdown over the entire visible series."""

    trimmed = tuple(item for item in series if item.session_date <= as_of_date)
    if not trimmed:
        return None
    peak = trimmed[0].value
    worst = Decimal(0)
    with localcontext() as context:
        context.prec = 28
        for item in trimmed:
            peak = max(peak, item.value)
            if peak > 0:
                worst = max(worst, (peak - item.value) / peak)
    return _bounded_decimal(worst)


def series_correlation(
    series_a: Sequence[DatedValue],
    series_b: Sequence[DatedValue],
    *,
    as_of_date: str,
    window_sessions: int,
) -> Decimal | None:
    """Pearson correlation of trailing daily simple returns between two
    series, aligned by exact matching calendar date. ``None`` when fewer
    than ``window_sessions`` aligned return pairs are available."""

    if window_sessions < 2:
        raise CrossMarketResearchError("SERIES_CORRELATION_WINDOW_INVALID")
    by_date_a = {
        item.session_date: item.value for item in series_a if item.session_date <= as_of_date
    }
    by_date_b = {
        item.session_date: item.value for item in series_b if item.session_date <= as_of_date
    }
    shared_dates = sorted(set(by_date_a) & set(by_date_b))
    if len(shared_dates) < window_sessions + 1:
        return None
    tail_dates = shared_dates[-(window_sessions + 1) :]
    with localcontext() as context:
        context.prec = 28
        returns_a = [
            (by_date_a[tail_dates[index]] - by_date_a[tail_dates[index - 1]])
            / by_date_a[tail_dates[index - 1]]
            for index in range(1, len(tail_dates))
            if by_date_a[tail_dates[index - 1]] != 0
        ]
        returns_b = [
            (by_date_b[tail_dates[index]] - by_date_b[tail_dates[index - 1]])
            / by_date_b[tail_dates[index - 1]]
            for index in range(1, len(tail_dates))
            if by_date_b[tail_dates[index - 1]] != 0
        ]
        if len(returns_a) != len(returns_b) or len(returns_a) < 2:
            return None
        mean_a = sum(returns_a, Decimal(0)) / Decimal(len(returns_a))
        mean_b = sum(returns_b, Decimal(0)) / Decimal(len(returns_b))
        covariance = sum(
            ((a - mean_a) * (b - mean_b) for a, b in zip(returns_a, returns_b, strict=True)),
            Decimal(0),
        )
        variance_a = sum(((a - mean_a) ** 2 for a in returns_a), Decimal(0))
        variance_b = sum(((b - mean_b) ** 2 for b in returns_b), Decimal(0))
        if variance_a <= 0 or variance_b <= 0:
            return None
        correlation = covariance / (_decimal_sqrt(variance_a) * _decimal_sqrt(variance_b))
    return _bounded_decimal(max(Decimal(-1), min(Decimal(1), correlation)))


def basket_return(
    member_series: Sequence[tuple[str, Sequence[DatedValue]]],
    *,
    as_of_date: str,
    window_sessions: int,
) -> Decimal | None:
    """Equal-weight mean of each member's own ``series_return`` -- the
    basket/relative-strength primitive every style/sector feature below
    reuses (large-vs-small, growth-vs-value, cyclical-vs-defensive,
    sector breadth). A member with an incomplete lookback is excluded
    from that date's basket rather than treated as a zero return."""

    values = [
        value
        for _label, series in member_series
        if (value := series_return(series, as_of_date=as_of_date, window_sessions=window_sessions))
        is not None
    ]
    if not values:
        return None
    return _bounded_decimal(sum(values, Decimal(0)) / Decimal(len(values)))


def cross_sectional_dispersion(values: Sequence[Decimal]) -> Decimal | None:
    """Population stdev across a cross-section of already-computed member
    values (e.g. every cohort member's own trailing return) -- the same
    "dispersion" concept ``domain.historical_replay.classify_dispersion``
    already labels, generalized to any cross-section, not only single-
    session returns."""

    if len(values) < 2:
        return None
    with localcontext() as context:
        context.prec = 28
        mean = sum(values, Decimal(0)) / Decimal(len(values))
        variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(len(values))
        return _bounded_decimal(_decimal_sqrt(variance))


def fraction_positive(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return _bounded_decimal(Decimal(sum(1 for value in values if value > 0)) / Decimal(len(values)))


# --------------------------------------------------------------------------
# Batch (whole-series) variants of the point functions above.
#
# Building one ``CrossMarketObservation`` per session date over ~1,500
# genuine trading days is exactly the "do not rebuild identical feature
# frames per hypothesis" case section 24 warns about, one level down: the
# *panel builder* (``application.cross_market_research``) still must not
# re-scan an entire multi-year series from the start for every single
# date. These batch functions compute the identical point-in-time-safe
# quantity as their single-``as_of`` counterparts above, but for every
# index of an already date-ordered, one-value-per-session array in one
# pass (or one bounded-window pass), so a full panel build stays linear
# (not quadratic) in the number of session dates. Callers are responsible
# for having already built ``values``/``values_a``/``values_b`` as a
# point-in-time-safe, gap-free, one-entry-per-session-date array (exactly
# what ``to_dated_closes`` over a point-in-time-visible view already
# guarantees) -- index ``i`` here only ever reads indices ``<= i``.
# --------------------------------------------------------------------------


def rolling_returns(
    values: Sequence[Decimal], *, window_sessions: int
) -> tuple[Decimal | None, ...]:
    if window_sessions < 1:
        raise CrossMarketResearchError("ROLLING_RETURNS_WINDOW_INVALID")
    results: list[Decimal | None] = []
    with localcontext() as context:
        context.prec = 28
        for index in range(len(values)):
            if index < window_sessions or values[index - window_sessions] == 0:
                results.append(None)
                continue
            base = values[index - window_sessions]
            results.append(_bounded_decimal((values[index] - base) / base))
    return tuple(results)


def _daily_returns(values: Sequence[Decimal]) -> tuple[Decimal | None, ...]:
    with localcontext() as context:
        context.prec = 28
        return tuple(
            None
            if index == 0 or values[index - 1] == 0
            else (values[index] - values[index - 1]) / values[index - 1]
            for index in range(len(values))
        )


def rolling_volatility_series(
    values: Sequence[Decimal], *, window_sessions: int, sessions_per_year: int
) -> tuple[Decimal | None, ...]:
    """Population stdev of trailing daily simple returns, annualized --
    the batch sibling of ``series_annualized_volatility``."""

    if window_sessions < 2:
        raise CrossMarketResearchError("ROLLING_VOLATILITY_WINDOW_INVALID")
    daily = _daily_returns(values)
    results: list[Decimal | None] = []
    with localcontext() as context:
        context.prec = 28
        for index in range(len(values)):
            tail = [
                item
                for item in daily[max(0, index - window_sessions + 1) : index + 1]
                if item is not None
            ]
            if index < window_sessions or len(tail) < window_sessions:
                results.append(None)
                continue
            mean = sum(tail, Decimal(0)) / Decimal(len(tail))
            variance = sum(((item - mean) ** 2 for item in tail), Decimal(0)) / Decimal(len(tail))
            results.append(
                _bounded_decimal(
                    _decimal_sqrt(variance) * _decimal_sqrt(Decimal(sessions_per_year))
                )
            )
    return tuple(results)


def rolling_drawdown_series(values: Sequence[Decimal]) -> tuple[Decimal | None, ...]:
    """The batch sibling of ``series_drawdown``: one running peak-to-
    trough drawdown per index, in one linear pass."""

    results: list[Decimal | None] = []
    peak: Decimal | None = None
    with localcontext() as context:
        context.prec = 28
        worst = Decimal(0)
        for value in values:
            peak = value if peak is None else max(peak, value)
            if peak > 0:
                worst = max(worst, (peak - value) / peak)
            results.append(_bounded_decimal(worst))
    return tuple(results)


def rolling_correlation_series(
    values_a: Sequence[Decimal], values_b: Sequence[Decimal], *, window_sessions: int
) -> tuple[Decimal | None, ...]:
    """The batch sibling of ``series_correlation``: both arrays must
    already be aligned one-per-index to the identical session-date axis
    (exactly what the panel builder's shared date axis guarantees)."""

    if window_sessions < 2:
        raise CrossMarketResearchError("ROLLING_CORRELATION_WINDOW_INVALID")
    if len(values_a) != len(values_b):
        raise CrossMarketResearchError("ROLLING_CORRELATION_SERIES_LENGTH_MISMATCH")
    daily_a = _daily_returns(values_a)
    daily_b = _daily_returns(values_b)
    results: list[Decimal | None] = []
    with localcontext() as context:
        context.prec = 28
        for index in range(len(values_a)):
            window_a = daily_a[max(0, index - window_sessions + 1) : index + 1]
            window_b = daily_b[max(0, index - window_sessions + 1) : index + 1]
            pairs = [
                (a, b)
                for a, b in zip(window_a, window_b, strict=True)
                if a is not None and b is not None
            ]
            if index < window_sessions or len(pairs) < window_sessions:
                results.append(None)
                continue
            mean_a = sum((a for a, _b in pairs), Decimal(0)) / Decimal(len(pairs))
            mean_b = sum((b for _a, b in pairs), Decimal(0)) / Decimal(len(pairs))
            covariance = sum(((a - mean_a) * (b - mean_b) for a, b in pairs), Decimal(0))
            variance_a = sum(((a - mean_a) ** 2 for a, _b in pairs), Decimal(0))
            variance_b = sum(((b - mean_b) ** 2 for _a, b in pairs), Decimal(0))
            if variance_a <= 0 or variance_b <= 0:
                results.append(None)
                continue
            correlation = covariance / (_decimal_sqrt(variance_a) * _decimal_sqrt(variance_b))
            results.append(_bounded_decimal(max(Decimal(-1), min(Decimal(1), correlation))))
    return tuple(results)


# ==========================================================================
# Section 4: regime intelligence v2
# ==========================================================================


class CrossMarketRegimeDimension(StrEnum):
    """Ten independently interpretable dimensions (section 3's minimum
    list, plus the equity risk-tone summary v1 already defined) -- never
    a Cartesian combination of them. Most reuse the identical regime-v1
    equity classifiers verbatim; ``BTC_TREND``/``BTC_VOLATILITY`` reuse
    the same generic threshold classifiers applied to BTC's own
    return/volatility; only ``BOND_RISK_RELATIONSHIP`` and
    ``COMMODITY_CONTEXT`` are genuinely new classification rules."""

    EQUITY_TREND = "EQUITY_TREND"
    EQUITY_BREADTH = "EQUITY_BREADTH"
    EQUITY_VOLATILITY = "EQUITY_VOLATILITY"
    EQUITY_DISPERSION = "EQUITY_DISPERSION"
    EQUITY_RISK_TONE = "EQUITY_RISK_TONE"
    CROSS_ASSET_CORRELATION = "CROSS_ASSET_CORRELATION"
    BTC_TREND = "BTC_TREND"
    BTC_VOLATILITY = "BTC_VOLATILITY"
    BOND_RISK_RELATIONSHIP = "BOND_RISK_RELATIONSHIP"
    COMMODITY_CONTEXT = "COMMODITY_CONTEXT"


class BondRiskRelationshipLabel(StrEnum):
    """Evidence-derived, never a named historical period: whether the
    rates proxy (TLT) moved *with* or *against* equities over the same
    trailing window, and how tightly (via the existing correlation-regime
    thresholds already applied to the rates/equity correlation feature)."""

    RATE_TAILWIND = "RATE_TAILWIND"
    RATE_HEADWIND = "RATE_HEADWIND"
    DECOUPLED = "DECOUPLED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class CommodityContextLabel(StrEnum):
    COMMODITY_TAILWIND = "COMMODITY_TAILWIND"
    COMMODITY_HEADWIND = "COMMODITY_HEADWIND"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


def classify_bond_risk_relationship(
    *, rates_proxy_return_20: Decimal | None, rates_equity_correlation_60: Decimal | None
) -> BondRiskRelationshipLabel:
    if rates_proxy_return_20 is None or rates_equity_correlation_60 is None:
        return BondRiskRelationshipLabel.INSUFFICIENT_EVIDENCE
    correlation_regime = classify_correlation_regime(abs(rates_equity_correlation_60))
    if correlation_regime is CorrelationRegimeLabel.LOW_CORRELATION:
        return BondRiskRelationshipLabel.DECOUPLED
    if rates_equity_correlation_60 > 0:
        return BondRiskRelationshipLabel.RATE_TAILWIND
    return BondRiskRelationshipLabel.RATE_HEADWIND


def classify_commodity_context(
    *, commodity_proxy_return_20: Decimal | None
) -> CommodityContextLabel:
    if commodity_proxy_return_20 is None:
        return CommodityContextLabel.INSUFFICIENT_EVIDENCE
    if commodity_proxy_return_20 >= Decimal("0.05"):
        return CommodityContextLabel.COMMODITY_TAILWIND
    if commodity_proxy_return_20 <= Decimal("-0.05"):
        return CommodityContextLabel.COMMODITY_HEADWIND
    return CommodityContextLabel.NEUTRAL


@dataclass(frozen=True, slots=True)
class CrossMarketRegimeClassification:
    dimension: CrossMarketRegimeDimension
    label: str
    evidence: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CrossMarketRegimeSnapshot:
    rule_version: str
    classifications: tuple[CrossMarketRegimeClassification, ...]

    def label_for(self, dimension: CrossMarketRegimeDimension) -> str:
        for item in self.classifications:
            if item.dimension is dimension:
                return item.label
        raise CrossMarketResearchError("CROSS_MARKET_REGIME_DIMENSION_NOT_CLASSIFIED")


CROSS_MARKET_REGIME_RULE_V2 = "CROSS_MARKET_REGIME_RULE_V2"


def _evidence(*pairs: tuple[str, Decimal | None]) -> tuple[tuple[str, str], ...]:
    return tuple((label, "UNAVAILABLE" if value is None else str(value)) for label, value in pairs)


def build_cross_market_regime_snapshot(
    *,
    equity_return_20: Decimal | None,
    equity_fraction_advancing: Decimal | None,
    equity_volatility_20: Decimal | None,
    equity_dispersion_20: Decimal | None,
    equity_average_correlation_60: Decimal | None,
    btc_return_20: Decimal | None,
    btc_volatility_20: Decimal | None,
    rates_proxy_return_20: Decimal | None,
    rates_equity_correlation_60: Decimal | None,
    commodity_proxy_return_20: Decimal | None,
) -> CrossMarketRegimeSnapshot:
    """Ten descriptive dimensions, each tagged with the exact evidence it
    was derived from (section 3: "retain feature evidence; threshold/
    policy version; label"). No hundreds-way combination is ever formed;
    callers read one dimension's label at a time via ``label_for``."""

    trend_label = classify_trend_strength(equity_return_20)
    breadth_label = classify_breadth_strength(equity_fraction_advancing)
    volatility_label = classify_volatility_regime(equity_volatility_20)
    dispersion_label = classify_dispersion(equity_dispersion_20)
    correlation_label = classify_correlation_regime(equity_average_correlation_60)
    tone_label = classify_risk_tone(
        breadth=breadth_label, volatility=volatility_label, trend=trend_label
    )
    btc_trend_label = classify_trend_strength(btc_return_20)
    btc_volatility_label = classify_volatility_regime(btc_volatility_20)
    bond_label = classify_bond_risk_relationship(
        rates_proxy_return_20=rates_proxy_return_20,
        rates_equity_correlation_60=rates_equity_correlation_60,
    )
    commodity_label = classify_commodity_context(
        commodity_proxy_return_20=commodity_proxy_return_20
    )
    classifications = (
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.EQUITY_TREND,
            trend_label.value,
            _evidence(("equity_return_20", equity_return_20)),
        ),
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.EQUITY_BREADTH,
            breadth_label.value,
            _evidence(("equity_fraction_advancing", equity_fraction_advancing)),
        ),
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.EQUITY_VOLATILITY,
            volatility_label.value,
            _evidence(("equity_volatility_20", equity_volatility_20)),
        ),
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.EQUITY_DISPERSION,
            dispersion_label.value,
            _evidence(("equity_dispersion_20", equity_dispersion_20)),
        ),
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.EQUITY_RISK_TONE,
            tone_label.value,
            _evidence(
                ("equity_return_20", equity_return_20),
                ("equity_fraction_advancing", equity_fraction_advancing),
                ("equity_volatility_20", equity_volatility_20),
            ),
        ),
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.CROSS_ASSET_CORRELATION,
            correlation_label.value,
            _evidence(("equity_average_correlation_60", equity_average_correlation_60)),
        ),
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.BTC_TREND,
            btc_trend_label.value,
            _evidence(("btc_return_20", btc_return_20)),
        ),
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.BTC_VOLATILITY,
            btc_volatility_label.value,
            _evidence(("btc_volatility_20", btc_volatility_20)),
        ),
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.BOND_RISK_RELATIONSHIP,
            bond_label.value,
            _evidence(
                ("rates_proxy_return_20", rates_proxy_return_20),
                ("rates_equity_correlation_60", rates_equity_correlation_60),
            ),
        ),
        CrossMarketRegimeClassification(
            CrossMarketRegimeDimension.COMMODITY_CONTEXT,
            commodity_label.value,
            _evidence(("commodity_proxy_return_20", commodity_proxy_return_20)),
        ),
    )
    return CrossMarketRegimeSnapshot(
        rule_version=CROSS_MARKET_REGIME_RULE_V2, classifications=classifications
    )


REGIME_CONDITION_FEATURE_IDS: dict[FeatureId, CrossMarketRegimeDimension] = {
    FeatureId.EQUITY_TREND_REGIME_LABEL: CrossMarketRegimeDimension.EQUITY_TREND,
    FeatureId.EQUITY_RISK_TONE_REGIME_LABEL: CrossMarketRegimeDimension.EQUITY_RISK_TONE,
    FeatureId.EQUITY_VOLATILITY_REGIME_LABEL: CrossMarketRegimeDimension.EQUITY_VOLATILITY,
    FeatureId.BOND_RISK_RELATIONSHIP_REGIME_LABEL: (
        CrossMarketRegimeDimension.BOND_RISK_RELATIONSHIP
    ),
}


# ==========================================================================
# Section 5: hypothesis templates and generated hypotheses
# ==========================================================================


class ConditionRule(StrEnum):
    """How a continuous feature is turned into a two-group split. Both
    rules are fixed a priori (never selected by trying several
    candidates and keeping the best) and, where a threshold is data-
    derived, it is derived *only* from the train period and then applied
    identically to validation/holdout/walk-forward -- never re-derived
    per period, which would leak information across the governed
    boundary."""

    ABOVE_TRAIN_MEDIAN = "ABOVE_TRAIN_MEDIAN"
    LABEL_EQUALS = "LABEL_EQUALS"


@dataclass(frozen=True, slots=True)
class GeneratedHypothesis:
    """One agenda-scoped, template-bound hypothesis (section 4). Every
    field below is retained verbatim on every trial/conclusion this
    hypothesis produces, so lineage back to its template and agenda is
    always exact."""

    hypothesis_id: str
    agenda_id: str
    template_id: HypothesisTemplateId
    template_version: str
    feature_id: FeatureId
    condition_rule: ConditionRule
    condition_label: str | None
    secondary_feature_id: FeatureId | None
    secondary_condition_rule: ConditionRule | None
    secondary_condition_label: str | None
    market_scope: tuple[MarketId, ...]
    outcome_id: OutcomeId
    horizon_sessions: int
    benchmark_kind: str
    family: HypothesisFamily
    rationale: str
    proposal_source: ProposalSource
    is_negative_control: bool
    negative_control_kind: str | None
    discovery_status: DiscoveryStatus
    ordinal: int
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        agenda: ResearchAgenda,
        template_id: HypothesisTemplateId | str,
        template_version: str,
        feature_id: FeatureId | str,
        condition_rule: ConditionRule | str,
        condition_label: str | None,
        secondary_feature_id: FeatureId | str | None,
        secondary_condition_rule: ConditionRule | str | None,
        secondary_condition_label: str | None,
        market_scope: tuple[MarketId | str, ...],
        outcome_id: OutcomeId | str,
        horizon_sessions: int,
        benchmark_kind: str,
        family: HypothesisFamily | str,
        rationale: str,
        proposal_source: ProposalSource | str,
        is_negative_control: bool,
        negative_control_kind: str | None,
        discovery_status: DiscoveryStatus | str,
        ordinal: int,
        created_at: datetime,
    ) -> GeneratedHypothesis:
        template = HypothesisTemplateId(template_id)
        if template not in agenda.allowed_templates:
            raise CrossMarketResearchError("HYPOTHESIS_TEMPLATE_NOT_APPROVED_BY_AGENDA")
        feature = FeatureId(feature_id)
        rule = ConditionRule(condition_rule)
        secondary = FeatureId(secondary_feature_id) if secondary_feature_id is not None else None
        secondary_rule = (
            ConditionRule(secondary_condition_rule)
            if secondary_condition_rule is not None
            else None
        )
        if template is HypothesisTemplateId.CONDITIONAL_RELATIONSHIP_V1 and secondary is None:
            raise CrossMarketResearchError("CONDITIONAL_TEMPLATE_REQUIRES_SECONDARY_FEATURE")
        if template is HypothesisTemplateId.INTERACTION_V1 and secondary is None:
            raise CrossMarketResearchError("INTERACTION_TEMPLATE_REQUIRES_SECONDARY_FEATURE")
        if (secondary is None) != (secondary_rule is None):
            raise CrossMarketResearchError("SECONDARY_FEATURE_AND_RULE_MUST_BE_DECLARED_TOGETHER")
        scope = tuple(MarketId(item) for item in market_scope)
        if not scope:
            raise CrossMarketResearchError("HYPOTHESIS_MARKET_SCOPE_REQUIRED")
        outcome = OutcomeId(outcome_id)
        if horizon_sessions not in agenda.allowed_horizons_sessions:
            raise CrossMarketResearchError("HYPOTHESIS_HORIZON_NOT_APPROVED_BY_AGENDA")
        family_value = HypothesisFamily(family)
        source = ProposalSource(proposal_source)
        if source is ProposalSource.ASSISTED_MODEL_SEAM:
            raise CrossMarketResearchError("ASSISTED_MODEL_PROPOSALS_ARE_DISABLED_THIS_MILESTONE")
        status = DiscoveryStatus(discovery_status)
        if is_negative_control != (status is DiscoveryStatus.NEGATIVE_CONTROL):
            raise CrossMarketResearchError("NEGATIVE_CONTROL_FLAG_MUST_MATCH_DISCOVERY_STATUS")
        if is_negative_control and negative_control_kind not in NEGATIVE_CONTROL_KINDS:
            raise CrossMarketResearchError("NEGATIVE_CONTROL_KIND_INVALID")
        if ordinal < 0:
            raise CrossMarketResearchError("HYPOTHESIS_ORDINAL_INVALID")
        created = _aware(created_at, "HYPOTHESIS_CREATED_AT")
        payload = (
            CROSS_MARKET_RESEARCH_V1,
            agenda.agenda_id,
            template,
            _text(template_version, "TEMPLATE_VERSION_INVALID", maximum=40),
            feature,
            rule,
            condition_label,
            secondary,
            secondary_rule,
            secondary_condition_label,
            scope,
            outcome,
            horizon_sessions,
            _text(benchmark_kind, "HYPOTHESIS_BENCHMARK_KIND_INVALID", maximum=80),
            family_value,
            _text(rationale, "HYPOTHESIS_RATIONALE_INVALID", maximum=1_000),
            source,
            is_negative_control,
            negative_control_kind,
            status,
            ordinal,
        )
        return cls(
            hypothesis_id=deterministic_id("cross_market_hypothesis", payload),
            agenda_id=agenda.agenda_id,
            template_id=template,
            template_version=payload[3],
            feature_id=feature,
            condition_rule=rule,
            condition_label=condition_label,
            secondary_feature_id=secondary,
            secondary_condition_rule=secondary_rule,
            secondary_condition_label=secondary_condition_label,
            market_scope=scope,
            outcome_id=outcome,
            horizon_sessions=horizon_sessions,
            benchmark_kind=payload[13],
            family=family_value,
            rationale=payload[15],
            proposal_source=source,
            is_negative_control=is_negative_control,
            negative_control_kind=negative_control_kind,
            discovery_status=status,
            ordinal=ordinal,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def deterministic_shuffle(series: Sequence[DatedValue], *, seed: int) -> tuple[DatedValue, ...]:
    """The one negative-control primitive with genuine randomness in it:
    reassigns the *values* of ``series`` to its own dates using a fixed,
    documented seed, so a shuffled negative control is exactly
    reproducible run to run while destroying the true date alignment
    between a feature and same-date evidence."""

    dates = [item.session_date for item in series]
    values = [item.value for item in series]
    random.Random(seed).shuffle(values)
    return tuple(DatedValue(date, value) for date, value in zip(dates, values, strict=True))


def lag_displace(series: Sequence[DatedValue], *, sessions: int) -> tuple[DatedValue, ...]:
    """Reassigns the value from ``sessions`` observations earlier onto
    each later date -- a deliberately wrong alignment (larger than any
    feature's real informative horizon in this agenda) used only for the
    lag-displaced negative control."""

    if sessions < 1:
        raise CrossMarketResearchError("LAG_DISPLACE_SESSIONS_INVALID")
    ordered = tuple(sorted(series, key=lambda item: item.session_date))
    return tuple(
        DatedValue(ordered[index].session_date, ordered[index - sessions].value)
        for index in range(sessions, len(ordered))
    )


def deterministic_noise_series(dates: Sequence[str], *, seed: int) -> tuple[DatedValue, ...]:
    """Pure seeded pseudo-random values in ``[0, 1)``, unrelated to any
    genuine market evidence -- the random-date/noise baseline."""

    generator = random.Random(seed)
    return tuple(
        DatedValue(date, _bounded_decimal(Decimal(str(generator.random())))) for date in dates
    )


# ==========================================================================
# Section 6: time-partition stability analysis
# ==========================================================================


class StabilityState(StrEnum):
    STABLE = "STABLE"
    REGIME_DEPENDENT = "REGIME_DEPENDENT"
    UNSTABLE = "UNSTABLE"
    CONCENTRATED_IN_ONE_PERIOD = "CONCENTRATED_IN_ONE_PERIOD"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class SubperiodResult:
    label: str
    start_date: str
    end_date: str
    sample_count: int
    mean: Decimal | None
    is_high_stress_evidence: bool


@dataclass(frozen=True, slots=True)
class StabilityReport:
    subperiods: tuple[SubperiodResult, ...]
    recent_holdout: SubperiodResult | None
    state: StabilityState
    evidence_note: str


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else (-1 if value < 0 else 0)


def derive_stability_state(subperiods: Sequence[SubperiodResult]) -> StabilityState:
    """Section 13's sign/effect classification, derived only from already-
    computed subperiod means -- never from a hardcoded historical label."""

    evaluable: list[tuple[SubperiodResult, Decimal]] = [
        (item, item.mean) for item in subperiods if item.mean is not None and item.sample_count >= 5
    ]
    if len(evaluable) < 2:
        return StabilityState.INSUFFICIENT_EVIDENCE
    signs = {_sign(mean) for _item, mean in evaluable}
    nonzero = [mean for _item, mean in evaluable if mean != 0]
    if len(nonzero) <= 1:
        return StabilityState.CONCENTRATED_IN_ONE_PERIOD
    if len(signs) == 1 and 0 not in signs:
        return StabilityState.STABLE
    stress = [mean for item, mean in evaluable if item.is_high_stress_evidence]
    non_stress = [mean for item, mean in evaluable if not item.is_high_stress_evidence]
    if stress and non_stress:
        stress_signs = {_sign(mean) for mean in stress if mean != 0}
        non_stress_signs = {_sign(mean) for mean in non_stress if mean != 0}
        if stress_signs and non_stress_signs and stress_signs != non_stress_signs:
            return StabilityState.REGIME_DEPENDENT
    return StabilityState.UNSTABLE


# ==========================================================================
# Section 17: research knowledge base
# ==========================================================================


class KnowledgeStatus(StrEnum):
    ACTIVE_RESEARCH = "ACTIVE_RESEARCH"
    SUPPORTED_FOR_FURTHER_STUDY = "SUPPORTED_FOR_FURTHER_STUDY"
    WEAK = "WEAK"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class ResearchKnowledgeEntry:
    """One append-only knowledge-base version for one hypothesis (section
    17). A new conclusion never overwrites a prior entry -- ``revision``
    and ``supersedes_entry_id`` make the full history explicit and
    queryable, mirroring ``ResearchGovernancePolicy``'s own revision
    pattern in ``domain.research_methodology``."""

    entry_id: str
    hypothesis_id: str
    conclusion_id: str
    status: KnowledgeStatus
    evidence_strength: str
    phase_reached: str
    markets: tuple[MarketId, ...]
    regime_dependence: str
    benchmark_kind: str
    holdout_result: str
    robustness_state: str
    cross_sectional_or_time_series: str
    limitations: tuple[str, ...]
    revision: int
    supersedes_entry_id: str | None
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        hypothesis_id: str,
        conclusion_id: str,
        status: KnowledgeStatus | str,
        evidence_strength: str,
        phase_reached: str,
        markets: tuple[MarketId | str, ...],
        regime_dependence: str,
        benchmark_kind: str,
        holdout_result: str,
        robustness_state: str,
        cross_sectional_or_time_series: str,
        limitations: tuple[str, ...],
        revision: int,
        supersedes_entry_id: str | None,
        created_at: datetime,
    ) -> ResearchKnowledgeEntry:
        if not hypothesis_id or not conclusion_id:
            raise CrossMarketResearchError("KNOWLEDGE_ENTRY_LINEAGE_REQUIRED")
        if revision < 1:
            raise CrossMarketResearchError("KNOWLEDGE_ENTRY_REVISION_INVALID")
        if (revision == 1) != (supersedes_entry_id is None):
            raise CrossMarketResearchError("KNOWLEDGE_ENTRY_REVISION_LINEAGE_INVALID")
        status_value = KnowledgeStatus(status)
        market_values = tuple(MarketId(item) for item in markets)
        limitation_values = tuple(sorted(set(limitations)))
        created = _aware(created_at, "KNOWLEDGE_ENTRY_CREATED_AT")
        payload = (
            CROSS_MARKET_RESEARCH_V1,
            hypothesis_id,
            conclusion_id,
            status_value,
            _text(evidence_strength, "KNOWLEDGE_EVIDENCE_STRENGTH_INVALID", maximum=200),
            _text(phase_reached, "KNOWLEDGE_PHASE_REACHED_INVALID", maximum=80),
            market_values,
            _text(regime_dependence, "KNOWLEDGE_REGIME_DEPENDENCE_INVALID", maximum=200),
            _text(benchmark_kind, "KNOWLEDGE_BENCHMARK_KIND_INVALID", maximum=80),
            _text(holdout_result, "KNOWLEDGE_HOLDOUT_RESULT_INVALID", maximum=200),
            _text(robustness_state, "KNOWLEDGE_ROBUSTNESS_STATE_INVALID", maximum=200),
            _text(
                cross_sectional_or_time_series,
                "KNOWLEDGE_EVIDENCE_SHAPE_INVALID",
                maximum=80,
            ),
            limitation_values,
            revision,
            supersedes_entry_id,
        )
        return cls(
            entry_id=deterministic_id("cross_market_knowledge_entry", payload),
            hypothesis_id=hypothesis_id,
            conclusion_id=conclusion_id,
            status=status_value,
            evidence_strength=payload[4],
            phase_reached=payload[5],
            markets=market_values,
            regime_dependence=payload[7],
            benchmark_kind=payload[8],
            holdout_result=payload[9],
            robustness_state=payload[10],
            cross_sectional_or_time_series=payload[11],
            limitations=limitation_values,
            revision=revision,
            supersedes_entry_id=supersedes_entry_id,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 18: contradiction handling
# ==========================================================================


@dataclass(frozen=True, slots=True)
class ResearchContradiction:
    """Two genuine retained knowledge-base entries whose conclusions
    conflict (section 18). Never averaged or silently resolved -- both
    sides, their shared evidence, and the regimes where each holds are
    all retained explicitly."""

    contradiction_id: str
    first_entry_id: str
    second_entry_id: str
    shared_evidence: tuple[str, ...]
    first_holds_when: str
    second_holds_when: str
    description: str
    resolved: bool
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        first_entry_id: str,
        second_entry_id: str,
        shared_evidence: tuple[str, ...],
        first_holds_when: str,
        second_holds_when: str,
        description: str,
        created_at: datetime,
    ) -> ResearchContradiction:
        if not first_entry_id or not second_entry_id or first_entry_id == second_entry_id:
            raise CrossMarketResearchError("CONTRADICTION_REQUIRES_TWO_DISTINCT_ENTRIES")
        ordered_pair = tuple(sorted((first_entry_id, second_entry_id)))
        # ``first_holds_when``/``second_holds_when`` describe *whichever*
        # entry the caller passed as first/second -- when canonicalizing
        # the entry-id pair swaps their order, the two "holds when" texts
        # must swap with them, or the retained record would describe the
        # wrong entry. This is what makes ``create`` genuinely order-
        # independent (identical contradiction regardless of which side a
        # caller names "first"), not merely the entry-id pair.
        swapped = ordered_pair != (first_entry_id, second_entry_id)
        ordered_first_holds_when = second_holds_when if swapped else first_holds_when
        ordered_second_holds_when = first_holds_when if swapped else second_holds_when
        created = _aware(created_at, "CONTRADICTION_CREATED_AT")
        payload = (
            CROSS_MARKET_RESEARCH_V1,
            ordered_pair,
            tuple(sorted(set(shared_evidence))),
            _text(ordered_first_holds_when, "CONTRADICTION_FIRST_HOLDS_WHEN_INVALID", maximum=300),
            _text(
                ordered_second_holds_when, "CONTRADICTION_SECOND_HOLDS_WHEN_INVALID", maximum=300
            ),
            _text(description, "CONTRADICTION_DESCRIPTION_INVALID", maximum=1_000),
        )
        return cls(
            contradiction_id=deterministic_id("cross_market_contradiction", payload),
            first_entry_id=ordered_pair[0],
            second_entry_id=ordered_pair[1],
            shared_evidence=payload[2],
            first_holds_when=payload[3],
            second_holds_when=payload[4],
            description=payload[5],
            resolved=False,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 19: controlled follow-up proposals
# ==========================================================================


class FollowUpKind(StrEnum):
    SPLIT_BY_REGIME = "SPLIT_BY_REGIME"
    ALTERNATE_HORIZON = "ALTERNATE_HORIZON"
    ADJACENT_LOOKBACK = "ADJACENT_LOOKBACK"
    ALTERNATE_MARKET_OUTCOME = "ALTERNATE_MARKET_OUTCOME"
    INVESTIGATE_ROBUSTNESS_FAILURE = "INVESTIGATE_ROBUSTNESS_FAILURE"


@dataclass(frozen=True, slots=True)
class FollowUpProposal:
    """Section 19: bounded, linked, budget-consuming, and never
    automatically executed. Retaining a follow-up proposal is not the
    same as registering or running the follow-up agenda it recommends."""

    follow_up_id: str
    parent_conclusion_id: str
    parent_hypothesis_id: str
    kind: FollowUpKind
    proposed_template_id: HypothesisTemplateId
    description: str
    requires_new_agenda: bool
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        parent_conclusion_id: str,
        parent_hypothesis_id: str,
        kind: FollowUpKind | str,
        proposed_template_id: HypothesisTemplateId | str,
        description: str,
        created_at: datetime,
    ) -> FollowUpProposal:
        if not parent_conclusion_id or not parent_hypothesis_id:
            raise CrossMarketResearchError("FOLLOW_UP_PARENT_LINEAGE_REQUIRED")
        kind_value = FollowUpKind(kind)
        template = HypothesisTemplateId(proposed_template_id)
        created = _aware(created_at, "FOLLOW_UP_CREATED_AT")
        payload = (
            CROSS_MARKET_RESEARCH_V1,
            parent_conclusion_id,
            parent_hypothesis_id,
            kind_value,
            template,
            _text(description, "FOLLOW_UP_DESCRIPTION_INVALID", maximum=1_000),
        )
        return cls(
            follow_up_id=deterministic_id("cross_market_follow_up", payload),
            parent_conclusion_id=parent_conclusion_id,
            parent_hypothesis_id=parent_hypothesis_id,
            kind=kind_value,
            proposed_template_id=template,
            description=payload[5],
            requires_new_agenda=True,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 9: trial-execution evidence (one bundle per hypothesis, tying
# together every research_methodology record its governed run produced)
# ==========================================================================


@dataclass(frozen=True, slots=True)
class HypothesisExecutionRecord:
    """The retained "did this hypothesis actually go through the full
    governed methodology stack" evidence (section 9): every phase's
    ``ResearchTrial`` id (``None`` only when that phase's sample was
    genuinely insufficient -- never fabricated), the shared
    ``ResearchGovernancePolicy``, the walk-forward fold trial ids, the
    robustness run, the eventual trial-family (multiple-testing) linkage,
    the stability report, and the final conclusion/promotion evaluation.
    Immutable and append-only, exactly like every other record here --
    resuming an agenda run never edits a prior hypothesis's execution
    record, it only ever adds the next one."""

    execution_id: str
    hypothesis_id: str
    agenda_id: str
    governance_policy_id: str
    exploratory_trial_id: str | None
    validation_trial_id: str | None
    holdout_trial_id: str | None
    walk_forward_trial_ids: tuple[str, ...]
    robustness_run_id: str | None
    trial_family_id: str | None
    conclusion_id: str | None
    promotion_evaluation_id: str | None
    stability: StabilityReport | None
    evidence_shape: str
    blockers: tuple[str, ...]
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        hypothesis_id: str,
        agenda_id: str,
        governance_policy_id: str,
        exploratory_trial_id: str | None,
        validation_trial_id: str | None,
        holdout_trial_id: str | None,
        walk_forward_trial_ids: tuple[str, ...],
        robustness_run_id: str | None,
        trial_family_id: str | None,
        conclusion_id: str | None,
        promotion_evaluation_id: str | None,
        stability: StabilityReport | None,
        evidence_shape: str,
        blockers: tuple[str, ...],
        created_at: datetime,
    ) -> HypothesisExecutionRecord:
        if not hypothesis_id or not agenda_id or not governance_policy_id:
            raise CrossMarketResearchError("EXECUTION_RECORD_LINEAGE_REQUIRED")
        created = _aware(created_at, "EXECUTION_RECORD_CREATED_AT")
        walk_ids = tuple(sorted(set(walk_forward_trial_ids)))
        block_values = tuple(sorted(set(blockers)))
        payload = (
            CROSS_MARKET_RESEARCH_V1,
            hypothesis_id,
            agenda_id,
            governance_policy_id,
            exploratory_trial_id,
            validation_trial_id,
            holdout_trial_id,
            walk_ids,
            robustness_run_id,
            trial_family_id,
            conclusion_id,
            promotion_evaluation_id,
            stability,
            _text(evidence_shape, "EXECUTION_RECORD_EVIDENCE_SHAPE_INVALID", maximum=80),
            block_values,
        )
        return cls(
            execution_id=deterministic_id("cross_market_hypothesis_execution", payload),
            hypothesis_id=hypothesis_id,
            agenda_id=agenda_id,
            governance_policy_id=governance_policy_id,
            exploratory_trial_id=exploratory_trial_id,
            validation_trial_id=validation_trial_id,
            holdout_trial_id=holdout_trial_id,
            walk_forward_trial_ids=walk_ids,
            robustness_run_id=robustness_run_id,
            trial_family_id=trial_family_id,
            conclusion_id=conclusion_id,
            promotion_evaluation_id=promotion_evaluation_id,
            stability=stability,
            evidence_shape=payload[13],
            blockers=block_values,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


# ==========================================================================
# Section 1 (continued): the one bounded mutable cursor this milestone
# needs (section: persistence -- "bounded mutable cursor only if
# necessary for agenda execution"). Every other record in this module is
# fully immutable; this is the single exception, exactly mirroring
# ``domain.research_methodology.WalkForwardRun``'s plan/cursor split.
# ==========================================================================


class AgendaRunStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class AgendaRun:
    agenda_run_id: str
    agenda_id: str
    hypothesis_ids: tuple[str, ...]
    cursor_ordinal: int
    completed_hypothesis_ids: tuple[str, ...]
    status: AgendaRunStatus
    failure_reason: str | None
    trials_executed: int
    created_at: datetime
    updated_at: datetime
    canonical_input_hash: str
    authority: str = "NONE"

    @classmethod
    def draft(
        cls, *, agenda: ResearchAgenda, hypothesis_ids: tuple[str, ...], created_at: datetime
    ) -> AgendaRun:
        if not hypothesis_ids:
            raise CrossMarketResearchError("AGENDA_RUN_REQUIRES_AT_LEAST_ONE_HYPOTHESIS")
        if len(hypothesis_ids) > agenda.maximum_hypothesis_count:
            raise CrossMarketResearchError("AGENDA_RUN_HYPOTHESIS_COUNT_EXCEEDS_AGENDA_BUDGET")
        created = _aware(created_at, "AGENDA_RUN_CREATED_AT")
        payload = (CROSS_MARKET_RESEARCH_V1, agenda.agenda_id, tuple(hypothesis_ids))
        return cls(
            agenda_run_id=deterministic_id("cross_market_agenda_run", payload),
            agenda_id=agenda.agenda_id,
            hypothesis_ids=tuple(hypothesis_ids),
            cursor_ordinal=0,
            completed_hypothesis_ids=(),
            status=AgendaRunStatus.NOT_STARTED,
            failure_reason=None,
            trials_executed=0,
            created_at=created,
            updated_at=created,
            canonical_input_hash=content_digest(payload),
        )

    def advance(
        self, *, hypothesis_id: str, trials_added: int, is_final: bool, at: datetime
    ) -> AgendaRun:
        if self.status in (AgendaRunStatus.COMPLETE, AgendaRunStatus.FAILED):
            raise CrossMarketResearchError("AGENDA_RUN_CANNOT_ADVANCE_FROM_TERMINAL_STATE")
        if trials_added < 0:
            raise CrossMarketResearchError("AGENDA_RUN_TRIALS_ADDED_INVALID")
        return replace(
            self,
            cursor_ordinal=self.cursor_ordinal + 1,
            completed_hypothesis_ids=(*self.completed_hypothesis_ids, hypothesis_id),
            status=AgendaRunStatus.COMPLETE if is_final else AgendaRunStatus.IN_PROGRESS,
            trials_executed=self.trials_executed + trials_added,
            updated_at=_aware(at, "AGENDA_RUN_UPDATED_AT"),
        )

    def fail(self, *, reason: str, at: datetime) -> AgendaRun:
        if self.status is AgendaRunStatus.COMPLETE:
            raise CrossMarketResearchError("AGENDA_RUN_ALREADY_COMPLETE")
        return replace(
            self,
            status=AgendaRunStatus.FAILED,
            failure_reason=_text(reason, "AGENDA_RUN_FAILURE_REASON_INVALID", maximum=500),
            updated_at=_aware(at, "AGENDA_RUN_UPDATED_AT"),
        )


# ==========================================================================
# Section 21: agenda-level research-quality report
# ==========================================================================


@dataclass(frozen=True, slots=True)
class AgendaQualityReport:
    """One retained, agenda-level answer to "how much should we trust
    what this agenda found?" (section 21)."""

    report_id: str
    agenda_id: str
    dataset_limitations: tuple[str, ...]
    provider_coverage_limitations: tuple[str, ...]
    panel_dependence_note: str
    multiple_testing_burden_note: str
    negative_control_behavior_note: str
    holdout_integrity_note: str
    conclusion_distribution: tuple[tuple[str, int], ...]
    hypotheses_proposed: int
    trials_run: int
    created_at: datetime
    canonical_hash: str
    authority: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        agenda_id: str,
        dataset_limitations: tuple[str, ...],
        provider_coverage_limitations: tuple[str, ...],
        panel_dependence_note: str,
        multiple_testing_burden_note: str,
        negative_control_behavior_note: str,
        holdout_integrity_note: str,
        conclusion_distribution: tuple[tuple[str, int], ...],
        hypotheses_proposed: int,
        trials_run: int,
        created_at: datetime,
    ) -> AgendaQualityReport:
        if not agenda_id:
            raise CrossMarketResearchError("QUALITY_REPORT_AGENDA_ID_REQUIRED")
        if hypotheses_proposed < 0 or trials_run < 0:
            raise CrossMarketResearchError("QUALITY_REPORT_COUNTS_INVALID")
        created = _aware(created_at, "QUALITY_REPORT_CREATED_AT")
        payload = (
            CROSS_MARKET_RESEARCH_V1,
            agenda_id,
            tuple(sorted(set(dataset_limitations))),
            tuple(sorted(set(provider_coverage_limitations))),
            _text(panel_dependence_note, "QUALITY_PANEL_DEPENDENCE_NOTE_INVALID", maximum=2_000),
            _text(
                multiple_testing_burden_note,
                "QUALITY_MULTIPLE_TESTING_NOTE_INVALID",
                maximum=2_000,
            ),
            _text(
                negative_control_behavior_note,
                "QUALITY_NEGATIVE_CONTROL_NOTE_INVALID",
                maximum=2_000,
            ),
            _text(holdout_integrity_note, "QUALITY_HOLDOUT_INTEGRITY_NOTE_INVALID", maximum=2_000),
            tuple(sorted(conclusion_distribution)),
            hypotheses_proposed,
            trials_run,
        )
        return cls(
            report_id=deterministic_id("cross_market_quality_report", payload),
            agenda_id=agenda_id,
            dataset_limitations=payload[2],
            provider_coverage_limitations=payload[3],
            panel_dependence_note=payload[4],
            multiple_testing_burden_note=payload[5],
            negative_control_behavior_note=payload[6],
            holdout_integrity_note=payload[7],
            conclusion_distribution=payload[8],
            hypotheses_proposed=hypotheses_proposed,
            trials_run=trials_run,
            created_at=created,
            canonical_hash=content_digest(payload),
        )


def cross_market_research_warnings() -> tuple[str, ...]:
    return (
        RESEARCH_WARNING,
        CAUSAL_LANGUAGE_POLICY,
        "PANEL_OBSERVATIONS_SHARE_MARKET_DATES_NOT_INDEPENDENT_SAMPLES",
        "DISCOVERED_HYPOTHESES_CANNOT_TREAT_THEIR_OWN_DISCOVERY_PERIOD_AS_CONFIRMATORY",
    )
