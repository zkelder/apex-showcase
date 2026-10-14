"""Pure, authority-free rules for comparing retained venue Opportunities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from typing import cast

from apex.domain.performance_attribution import StrategyHealthAssessment, StrategyHealthState
from apex.domain.values import content_digest, parse_decimal

SHARED_OPPORTUNITY_PROJECTION_V1 = "SHARED_OPPORTUNITY_PROJECTION_V1"
SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1 = "SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1"
SHARED_OPPORTUNITY_PRIORITY_POLICY_V1 = "SHARED_OPPORTUNITY_PRIORITY_POLICY_V1"
SHARED_RESEARCH_ALLOCATION_POLICY_V1 = "SHARED_RESEARCH_ALLOCATION_POLICY_V1"
CRYPTO_SHARED_PAPER_ELIGIBILITY_POLICY_V1 = "CRYPTO_SHARED_PAPER_ELIGIBILITY_POLICY_V1"
SHARED_OPPORTUNITY_WARNING = "SHARED OPPORTUNITY RESEARCH ONLY — NO CAPITAL OR EXECUTION AUTHORITY"
HYPOTHETICAL_BUDGET = Decimal("1000")
MAX_OPPORTUNITY_FRACTION = Decimal("0.25")
MAX_VENUE_FRACTION = Decimal("0.50")
# v1 (original nine components) summed to 1.00 with FRESHNESS at 0.10. Evidence-Weighted
# Cross-Venue Opportunity Ranking v1 adds STATISTICAL_QUALIFICATION and funds it by halving
# FRESHNESS: freshness is already a hard ELIGIBLE/REJECTED gate (EVIDENCE_STALE) before an
# opportunity ever reaches this function, so by construction every component here already
# cleared the freshness bar -- it is a real but second-order, non-economic signal, unlike
# how much genuine walk-forward evidence backs the return estimate itself.
COMPONENT_WEIGHTS = (
    ("COMPARABLE_VALUE", Decimal("0.25")),
    ("DOWNSIDE", Decimal("0.15")),
    ("COST", Decimal("0.15")),
    ("LIQUIDITY", Decimal("0.10")),
    ("LOCKUP", Decimal("0.10")),
    ("FRESHNESS", Decimal("0.05")),
    ("EVIDENCE", Decimal("0.05")),
    ("UNCERTAINTY", Decimal("0.05")),
    ("SOURCE_HEALTH", Decimal("0.05")),
    ("STATISTICAL_QUALIFICATION", Decimal("0.05")),
)
# comparable_value_identity tags that the venue source adapters already, deliberately, attach
# only when comparable_value is a genuine economic-value estimate: a strictly-prior walk-forward
# expected NET return (crypto), or a model-free guaranteed conservative net-lower-bound-over-cost
# (Kalshi -- weaker statistically than a probability-weighted EV in one sense, but stronger in
# another: it is a deterministic worst-case guarantee, not an average). Anything else -- None, a
# composite/discovery score explicitly labeled "...NOT_EXPECTED_RETURN", a fixture tag, or any
# future unrecognized identity -- is conservatively treated as proxy magnitude, never genuine EV.
# Fails closed by construction: a newly added source that forgets to register its identity here
# only ever under-ranks itself into RESEARCH_ONLY, never over-ranks into ACTIONABLE.
GENUINE_ECONOMIC_MAGNITUDE_IDENTITIES = frozenset(
    {
        "STRICTLY_PRIOR_WALK_FORWARD_EXPECTED_NET_RETURN",
        "STRICTLY_PRIOR_STATEFUL_WALK_FORWARD_EXPECTED_NET_RETURN",
        "GUARANTEED_SETTLEMENT_NET_GAP_OVER_MODELED_COST",
        "CONSERVATIVE_MODEL_FREE_NET_LOWER_BOUND_OVER_COST",
        "CONSERVATIVE_NET_LOWER_BOUND_OVER_MODELED_COST",
    }
)
# Mirrors apex.domain.crypto_universe.RELATIVE_STRENGTH_MINIMUM_SAMPLES (=30), the existing
# fail-closed minimum-sample gate a rank-bucket must already clear before it can ever produce a
# genuine expected_net_return at all. Deliberately not imported: shared_opportunity.py is the
# cross-venue layer sitting above every vertical and currently has zero vertical-specific domain
# imports; importing one crypto-only constant here would create the exact asymmetric-venue
# coupling this milestone is trying to remove, not add. Keep this value in sync with the crypto
# constant by hand if that policy ever changes.
STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES = 30
# Saturation point of the statistical-qualification ramp: sample counts at/above this are treated
# as "abundant" evidence and score the same. Not a gate (the minimum above is the gate) -- just
# where the smooth 0.5->1.0 ramp tops out. Chosen as 5x the minimum: a round, conservative
# multiple, not fit to any particular retained dataset.
STATISTICAL_QUALIFICATION_SATURATION_SAMPLES = 150
# Opportunity-cost floor (design improvement #5): the minimum horizon-normalized (per-day),
# genuine economic magnitude required to enter the ACTIONABLE priority tier. Deliberately zero
# ("must be strictly better than doing nothing with the capital"), not an invented live-trading
# profitability bar -- Apex has no retained evidence to justify a nonzero live threshold yet.
# Versioned so a future milestone can tighten it without redesigning the tiering mechanism.
OPPORTUNITY_COST_FLOOR_V1 = Decimal("0")
# "FIXTURE" is deliberately excluded: eligibility is a genuine-evidence gate, and synthetic test
# data must never be able to reach it, matching the same exclusion counterfactual_followup.py's
# FOLLOWUP_ELIGIBLE_EVIDENCE_CLASSES already makes for the identical reason (the 2026-08-16
# DOGE-USD contamination fix). No retained production projection has ever carried evidence_class
# "FIXTURE" -- confirmed against the live database prior to this change -- so this tightens a
# theoretical gap rather than changing any genuine opportunity's disposition.
PERMITTED_EVIDENCE_CLASSES = frozenset({"HISTORICAL_PUBLIC", "GENUINE_PUBLIC"})
DISQUALIFYING_WARNINGS = frozenset(
    {
        "SOURCE_DATA_INVALID",
        "SOURCE_LINEAGE_INVALID",
        "LIQUIDITY_INSUFFICIENT",
        "UNKNOWN_SOURCE_AUTHORITY",
    }
)
_QUANTUM = Decimal("0.00000001")


class SharedOpportunityError(ValueError):
    """Raised when retained shared evidence is invalid or conflicts."""


class SharedVenue(StrEnum):
    CRYPTO = "CRYPTO"
    KALSHI = "KALSHI"
    # Securities Research -> Shared Opportunity Recurring Bridge v1. Backed by
    # already-retained ``domain.options_structures.OptionStructureCandidate``
    # evidence only -- never a live provider call from this layer. See
    # ``adapters.shared_opportunity_sources.RetainedSharedOpportunitySource.
    # securities_projections`` for the sole producer.
    SECURITIES = "SECURITIES"


class EligibilityState(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    BLOCKED_MISSING_EVIDENCE = "BLOCKED_MISSING_EVIDENCE"


class PriorityTier(StrEnum):
    """Orthogonal to rank/score: ACTIONABLE opportunities cleared the opportunity-cost floor
    on genuine economic magnitude; RESEARCH_ONLY opportunities remain fully visible and ranked
    but have not (proxy magnitude, missing magnitude, or at/below the floor). Tier never reorders
    or deletes -- it is additional explanation, not a filter."""

    ACTIONABLE = "ACTIONABLE"
    RESEARCH_ONLY = "RESEARCH_ONLY"


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None:
        raise SharedOpportunityError(reason)
    return value.astimezone(UTC)


def _identity(value: str, reason: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 300:
        raise SharedOpportunityError(reason)
    return normalized


def _decimal(value: object | None, reason: str, *, nonnegative: bool = False) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = parse_decimal(cast(str | int | Decimal, value), allow_negative=not nonnegative)
    except (TypeError, ValueError, ArithmeticError) as error:
        raise SharedOpportunityError(reason) from error
    if nonnegative and parsed < 0:
        raise SharedOpportunityError(reason)
    return parsed


def _bounded(value: Decimal) -> Decimal:
    return min(Decimal(1), max(Decimal(0), value)).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _crypto_paper_readiness(
    *,
    venue: SharedVenue,
    evidence_class: str,
    freshness_seconds: int,
    freshness_limit_seconds: int,
    costs_retained: bool,
    modeled_fees: Decimal | None,
    modeled_slippage: Decimal | None,
    maximum_loss: Decimal | None,
    required_hypothetical_capital: Decimal | None,
    liquidity_state: str,
    supporting_evidence_ids: tuple[str, ...],
    source_warnings: tuple[str, ...],
    source_rejection_reasons: tuple[str, ...],
    source_authority: str,
) -> tuple[bool, tuple[str, ...]]:
    """Canonical, deterministic internal-PAPER-consideration readiness for a CRYPTO
    ``SharedOpportunityProjection``, computed once from retained construction
    evidence only (never a live account, universe, or operator activation state,
    which stay execution-time concerns re-checked every cycle by
    ``evaluate_paper_eligibility`` in ``domain/crypto_paper.py``).  Kalshi
    projections are always scoped out here and keep gating PAPER consideration
    through their own ``KalshiPaperEligibility`` policy instead, so this function
    never changes Kalshi's observable behavior."""

    if venue is not SharedVenue.CRYPTO:
        return False, ("PAPER_ELIGIBILITY_SCOPED_TO_CRYPTO_VENUE",)
    reasons: list[str] = []
    if evidence_class not in PERMITTED_EVIDENCE_CLASSES:
        reasons.append("EVIDENCE_CLASS_NOT_PERMITTED")
    if freshness_seconds > freshness_limit_seconds:
        reasons.append("EVIDENCE_STALE")
    if not supporting_evidence_ids:
        reasons.append("EVIDENCE_LINEAGE_INCOMPLETE")
    if source_rejection_reasons:
        reasons.append("SOURCE_POLICY_REJECTED")
    if not costs_retained:
        reasons.append("EXPECTED_COSTS_MISSING")
    if modeled_fees is None:
        reasons.append("FEE_EVIDENCE_MISSING")
    if modeled_slippage is None:
        reasons.append("SLIPPAGE_EVIDENCE_MISSING")
    if maximum_loss is None:
        reasons.append("MAXIMUM_LOSS_MISSING")
    if required_hypothetical_capital is None or required_hypothetical_capital <= 0:
        reasons.append("REQUIRED_CAPITAL_INVALID_OR_MISSING")
    if liquidity_state != "SUFFICIENT":
        reasons.append("LIQUIDITY_NOT_SUFFICIENT")
    if any(value in DISQUALIFYING_WARNINGS for value in source_warnings):
        reasons.append("DISQUALIFYING_WARNING_PRESENT")
    if source_authority != "NONE":
        reasons.append("SOURCE_AUTHORITY_NOT_RESEARCH_ONLY")
    if reasons:
        return False, tuple(sorted(set(reasons)))
    return True, ("PAPER_ELIGIBLE_UNDER_CANONICAL_SHARED_POLICY",)


@dataclass(frozen=True, slots=True)
class SharedOpportunityProjection:
    projection_id: str
    version: int
    venue: SharedVenue
    source_record_type: str
    source_record_id: str
    source_canonical_hash: str
    strategy_or_method_id: str
    instrument_id: str
    product_or_market_id: str
    direction: str
    generated_at: datetime
    valid_until: datetime | None
    evidence_class: str
    freshness_seconds: int
    freshness_limit_seconds: int
    expected_gross_return: Decimal | None
    expected_net_return: Decimal | None
    comparable_value: Decimal | None
    comparable_value_identity: str | None
    uncertainty: Decimal | None
    maximum_loss: Decimal | None
    modeled_fees: Decimal | None
    modeled_slippage: Decimal | None
    costs_retained: bool
    liquidity_state: str
    liquidity_limitations: tuple[str, ...]
    required_hypothetical_capital: Decimal | None
    maximum_sensible_research_allocation: Decimal | None
    source_maximum_allocation_fraction: Decimal | None
    expected_holding_days: int | None
    estimated_lockup_days: int | None
    supporting_evidence_ids: tuple[str, ...]
    source_warnings: tuple[str, ...]
    source_rejection_reasons: tuple[str, ...]
    source_authority: str
    projection_limitations: tuple[str, ...]
    source_details: tuple[tuple[str, str], ...]
    canonical_hash: str
    authority: str = "NONE"
    research_only: bool = True
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    financial_authority: str = "NONE"
    paper_eligibility_reasons: tuple[str, ...] = ()
    # Count of independent walk-forward decision samples backing calibration_bucket's
    # expected_net_return (e.g. CryptoCalibrationBucket.sample_count), when the source method
    # computes one. None for proxy/composite-score methods and for Kalshi's model-free guarantees
    # (a genuinely different, non-statistical, rule-verified form of rigor -- never fabricated
    # from source_details string parsing, never treated as zero). Added by Evidence-Weighted
    # Cross-Venue Opportunity Ranking v1; absent on projections retained before this change.
    statistical_sample_size: int | None = None

    @classmethod
    def create(
        cls,
        *,
        projection_id: str,
        venue: SharedVenue,
        source_record_type: str,
        source_record_id: str,
        source_canonical_hash: str,
        strategy_or_method_id: str,
        instrument_id: str,
        product_or_market_id: str,
        direction: str,
        generated_at: datetime,
        valid_until: datetime | None,
        evidence_class: str,
        freshness_seconds: int,
        freshness_limit_seconds: int,
        expected_gross_return: object | None = None,
        expected_net_return: object | None = None,
        comparable_value: object | None = None,
        comparable_value_identity: str | None = None,
        uncertainty: object | None = None,
        maximum_loss: object | None = None,
        modeled_fees: object | None = None,
        modeled_slippage: object | None = None,
        costs_retained: bool,
        liquidity_state: str,
        liquidity_limitations: tuple[str, ...],
        required_hypothetical_capital: object | None,
        maximum_sensible_research_allocation: object | None,
        source_maximum_allocation_fraction: object | None,
        expected_holding_days: int | None,
        estimated_lockup_days: int | None,
        supporting_evidence_ids: tuple[str, ...],
        source_warnings: tuple[str, ...],
        source_rejection_reasons: tuple[str, ...],
        source_authority: str,
        projection_limitations: tuple[str, ...],
        source_details: tuple[tuple[str, str], ...],
        statistical_sample_size: int | None = None,
    ) -> SharedOpportunityProjection:
        generated = _time(generated_at, "SHARED_OPPORTUNITY_GENERATED_TIME_INVALID")
        expiry = (
            None if valid_until is None else _time(valid_until, "SHARED_OPPORTUNITY_EXPIRY_INVALID")
        )
        if freshness_seconds < 0 or freshness_limit_seconds <= 0:
            raise SharedOpportunityError("SHARED_OPPORTUNITY_FRESHNESS_INVALID")
        if expected_holding_days is not None and expected_holding_days < 0:
            raise SharedOpportunityError("SHARED_OPPORTUNITY_HOLDING_INVALID")
        if estimated_lockup_days is not None and estimated_lockup_days < 0:
            raise SharedOpportunityError("SHARED_OPPORTUNITY_LOCKUP_INVALID")
        if statistical_sample_size is not None and statistical_sample_size < 0:
            raise SharedOpportunityError("SHARED_OPPORTUNITY_SAMPLE_SIZE_INVALID")
        resolved_maximum_loss = _decimal(
            maximum_loss, "SHARED_OPPORTUNITY_MAXIMUM_LOSS_INVALID", nonnegative=True
        )
        resolved_modeled_fees = _decimal(
            modeled_fees, "SHARED_OPPORTUNITY_FEES_INVALID", nonnegative=True
        )
        resolved_modeled_slippage = _decimal(
            modeled_slippage, "SHARED_OPPORTUNITY_SLIPPAGE_INVALID", nonnegative=True
        )
        resolved_required_capital = _decimal(
            required_hypothetical_capital,
            "SHARED_OPPORTUNITY_REQUIRED_CAPITAL_INVALID",
            nonnegative=True,
        )
        resolved_liquidity_state = _identity(
            liquidity_state, "SHARED_OPPORTUNITY_LIQUIDITY_INVALID"
        )
        resolved_evidence_ids = tuple(sorted(set(supporting_evidence_ids)))
        resolved_warnings = tuple(sorted(set(source_warnings)))
        resolved_rejection_reasons = tuple(sorted(set(source_rejection_reasons)))
        paper_eligible, paper_eligibility_reasons = _crypto_paper_readiness(
            venue=venue,
            evidence_class=evidence_class,
            freshness_seconds=freshness_seconds,
            freshness_limit_seconds=freshness_limit_seconds,
            costs_retained=costs_retained,
            modeled_fees=resolved_modeled_fees,
            modeled_slippage=resolved_modeled_slippage,
            maximum_loss=resolved_maximum_loss,
            required_hypothetical_capital=resolved_required_capital,
            liquidity_state=resolved_liquidity_state,
            supporting_evidence_ids=resolved_evidence_ids,
            source_warnings=resolved_warnings,
            source_rejection_reasons=resolved_rejection_reasons,
            source_authority=source_authority,
        )
        values = (
            _identity(projection_id, "SHARED_OPPORTUNITY_PROJECTION_ID_INVALID"),
            1,
            venue,
            _identity(source_record_type, "SHARED_OPPORTUNITY_SOURCE_TYPE_INVALID"),
            _identity(source_record_id, "SHARED_OPPORTUNITY_SOURCE_ID_INVALID"),
            _identity(source_canonical_hash, "SHARED_OPPORTUNITY_SOURCE_HASH_INVALID"),
            _identity(strategy_or_method_id, "SHARED_OPPORTUNITY_METHOD_INVALID"),
            _identity(instrument_id, "SHARED_OPPORTUNITY_INSTRUMENT_INVALID"),
            _identity(product_or_market_id, "SHARED_OPPORTUNITY_MARKET_INVALID"),
            _identity(direction, "SHARED_OPPORTUNITY_DIRECTION_INVALID"),
            generated,
            expiry,
            _identity(evidence_class, "SHARED_OPPORTUNITY_EVIDENCE_CLASS_INVALID"),
            freshness_seconds,
            freshness_limit_seconds,
            _decimal(expected_gross_return, "SHARED_OPPORTUNITY_GROSS_RETURN_INVALID"),
            _decimal(expected_net_return, "SHARED_OPPORTUNITY_NET_RETURN_INVALID"),
            _decimal(comparable_value, "SHARED_OPPORTUNITY_COMPARABLE_VALUE_INVALID"),
            comparable_value_identity,
            _decimal(uncertainty, "SHARED_OPPORTUNITY_UNCERTAINTY_INVALID", nonnegative=True),
            resolved_maximum_loss,
            resolved_modeled_fees,
            resolved_modeled_slippage,
            costs_retained,
            resolved_liquidity_state,
            tuple(sorted(set(liquidity_limitations))),
            resolved_required_capital,
            _decimal(
                maximum_sensible_research_allocation,
                "SHARED_OPPORTUNITY_MAXIMUM_ALLOCATION_INVALID",
                nonnegative=True,
            ),
            _decimal(
                source_maximum_allocation_fraction,
                "SHARED_OPPORTUNITY_SOURCE_ALLOCATION_INVALID",
                nonnegative=True,
            ),
            expected_holding_days,
            estimated_lockup_days,
            resolved_evidence_ids,
            resolved_warnings,
            resolved_rejection_reasons,
            source_authority,
            tuple(sorted(set(projection_limitations))),
            tuple(sorted(set(source_details))),
            "NONE",
            True,
            paper_eligible,
            False,
            "NONE",
            "NONE",
            paper_eligibility_reasons,
            statistical_sample_size,
        )
        return cls(*values[:-8], content_digest(values), *values[-8:])


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    decision_id: str
    projection_id: str
    policy_id: str
    evaluated_at: datetime
    state: EligibilityState
    reasons: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"


def evaluate_eligibility(
    projection: SharedOpportunityProjection, *, evaluated_at: datetime
) -> EligibilityDecision:
    now = _time(evaluated_at, "SHARED_ELIGIBILITY_TIME_INVALID")
    reasons: list[str] = []
    missing: list[str] = []
    if not projection.source_canonical_hash:
        missing.append("SOURCE_IMMUTABLE_HASH_MISSING")
    if projection.evidence_class not in PERMITTED_EVIDENCE_CLASSES:
        reasons.append("EVIDENCE_CLASS_NOT_PERMITTED")
    if projection.source_rejection_reasons:
        reasons.append("SOURCE_POLICY_REJECTED")
        reasons.extend(f"SOURCE:{value}" for value in projection.source_rejection_reasons)
    expired = projection.valid_until is not None and now >= projection.valid_until
    if expired:
        reasons.append("OPPORTUNITY_EXPIRED")
    if projection.freshness_seconds > projection.freshness_limit_seconds:
        reasons.append("EVIDENCE_STALE")
    if not projection.costs_retained:
        missing.append("EXPECTED_COSTS_MISSING")
    if projection.maximum_loss is None:
        missing.append("MAXIMUM_LOSS_MISSING")
    if projection.required_hypothetical_capital is None:
        missing.append("REQUIRED_CAPITAL_MISSING")
    if projection.liquidity_state in {"INSUFFICIENT", "INVALID", "UNAVAILABLE"}:
        reasons.append("LIQUIDITY_INSUFFICIENT")
    reasons.extend(
        f"DISQUALIFYING_WARNING:{value}"
        for value in projection.source_warnings
        if value in DISQUALIFYING_WARNINGS
    )
    if projection.source_authority != "NONE":
        reasons.append("SOURCE_AUTHORITY_NOT_RESEARCH_ONLY")
    if expired:
        state = EligibilityState.EXPIRED
    elif missing:
        state = EligibilityState.BLOCKED_MISSING_EVIDENCE
    elif reasons:
        state = EligibilityState.REJECTED
    else:
        state = EligibilityState.ELIGIBLE
        reasons.append("ELIGIBLE_FOR_RESEARCH_COMPARISON_ONLY")
    all_reasons = tuple(sorted(set((*reasons, *missing))))
    decision_id = f"{projection.projection_id}-eligibility-v1"
    payload = (
        decision_id,
        projection.projection_id,
        SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1,
        now,
        state,
        all_reasons,
        "NONE",
    )
    return EligibilityDecision(*payload[:-1], canonical_hash=content_digest(payload))


@dataclass(frozen=True, slots=True)
class PriorityComponent:
    name: str
    raw_value: Decimal | None
    normalized_value: Decimal
    weight: Decimal
    weighted_value: Decimal
    missing_behavior: str | None
    explanation: str


@dataclass(frozen=True, slots=True)
class PriorityResult:
    priority_id: str
    projection_id: str
    policy_id: str
    score: Decimal
    rank: int
    tie_break_key: tuple[str, str, str]
    components: tuple[PriorityComponent, ...]
    explanation: str
    canonical_hash: str
    authority: str = "NONE"
    # Opportunity-cost floor classification (design improvement #5): orthogonal to score/rank,
    # never reorders or hides anything. Absent on results retained before this milestone;
    # PostgresSharedOpportunityRepository defaults missing values to the conservative
    # RESEARCH_ONLY/() reading, never to ACTIONABLE.
    tier: PriorityTier = PriorityTier.RESEARCH_ONLY
    tier_reasons: tuple[str, ...] = ()


def _component(
    name: str,
    raw: Decimal | None,
    normalized: Decimal,
    weight: Decimal,
    *,
    missing: str | None = None,
    detail: str | None = None,
) -> PriorityComponent:
    bounded = _bounded(normalized)
    weighted = (bounded * weight).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
    explanation = f"{name} normalized={bounded} weight={weight} contribution={weighted}"
    if missing:
        explanation += f" missing_behavior={missing}"
    if detail:
        explanation += f" detail={detail}"
    return PriorityComponent(name, raw, bounded, weight, weighted, missing, explanation)


def _effective_weights(excluded_names: frozenset[str]) -> dict[str, Decimal]:
    """Deterministically redistribute an excluded component's base weight proportionally across
    every other component so ``sum(weights) == 1`` always, exactly -- never fabricate a
    perfect/neutral value for the excluded component itself (see ``_source_health``)."""

    base = dict(COMPONENT_WEIGHTS)
    if not excluded_names:
        return base
    included_names = [name for name, _ in COMPONENT_WEIGHTS if name not in excluded_names]
    if not included_names:
        raise SharedOpportunityError("SHARED_PRIORITY_ALL_COMPONENTS_EXCLUDED")
    excluded_total = sum((base[name] for name in excluded_names), Decimal(0))
    denominator = Decimal(1) - excluded_total
    weights: dict[str, Decimal] = dict.fromkeys(excluded_names, Decimal(0))
    with localcontext() as context:
        context.prec = 28
        for name in included_names[:-1]:
            weights[name] = (base[name] / denominator).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
    # The last included component (fixed, deterministic iteration order) absorbs the exact
    # Decimal-rounding residual so the total is always exactly 1, never 0.99999999-ish.
    weights[included_names[-1]] = Decimal(1) - sum(weights.values(), Decimal(0))
    return weights


def _economic_magnitude_signal(
    item: SharedOpportunityProjection,
) -> tuple[Decimal | None, str | None]:
    """Genuine-vs-proxy gate + horizon normalization for one opportunity's economic magnitude.

    Returns ``(horizon_normalized_per_day_value, None)`` only when ``comparable_value`` is a
    genuine economic-value estimate (per ``comparable_value_identity``, never inferred from the
    field name alone) *and* its horizon is known. Otherwise returns ``(None, exclusion_reason)``.
    A ``None`` result must never be treated as favorable -- callers fall back to a conservative
    neutral value and exclude the opportunity from any venue percentile peer population.
    """

    if item.comparable_value is None:
        return None, "NO_COMPARABLE_VALUE"
    if item.comparable_value_identity not in GENUINE_ECONOMIC_MAGNITUDE_IDENTITIES:
        return None, "PROXY_MAGNITUDE_NOT_GENUINE_EV"
    if item.expected_holding_days is None:
        return None, "HORIZON_UNKNOWN_NOT_PERCENTILE_COMPARABLE"
    # Floor at one day: 0-day ("same day") horizons are a genuine, known value, not missing
    # evidence, but dividing by zero (or a near-zero day count) would blow the per-day rate up
    # arbitrarily; flooring at 1 conservatively UNDERSTATES a genuinely fast opportunity's rate
    # rather than fabricating an inflated one.
    effective_days = max(item.expected_holding_days, 1)
    with localcontext() as context:
        context.prec = 28
        return item.comparable_value / Decimal(effective_days), None


def _venue_percentile(target_id: str, population: Mapping[str, Decimal]) -> Decimal:
    """Tie-aware fractional percentile rank of ``target_id`` within ``population``
    (``(below + tied / 2) / n``), in ``[0, 1]``. Mirrors the exact formula and tie convention of
    the established ``apex.domain.historical_market_data.cross_sectional_rank`` primitive (not
    imported directly: this cross-venue layer intentionally has zero per-vertical domain
    dependencies). Caller is responsible for point-in-time and genuine-vs-proxy filtering before
    building ``population`` -- this function has no time axis or magnitude-provenance opinion."""

    target = population[target_id]
    below = sum(1 for value in population.values() if value < target)
    tied = sum(1 for value in population.values() if value == target)
    with localcontext() as context:
        context.prec = 28
        return (Decimal(below) + Decimal(tied) / Decimal(2)) / Decimal(len(population))


def _statistical_qualification(sample_size: int | None) -> tuple[Decimal, str | None]:
    """Smooth, saturating ramp on independent walk-forward decision samples backing a genuine
    expected-return estimate. Deliberately reuses ``STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES``
    (== crypto_universe.RELATIVE_STRENGTH_MINIMUM_SAMPLES) as its floor rather than inventing a
    new threshold. ``None`` (proxy methods; Kalshi's model-free, rule-verified -- not statistical
    -- guarantees) is conservative-neutral, never penalized as if unqualified and never rewarded
    as if abundant."""

    if sample_size is None:
        return Decimal("0.5"), "NEUTRAL_0.5_NO_SAMPLE_SIZE_CONCEPT_FOR_THIS_METHOD"
    if sample_size < STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES:
        # Defensive only: the current upstream calibration already fails closed below this
        # minimum before a genuine expected_net_return can exist at all (see design.md survey).
        return Decimal("0.5"), "NEUTRAL_0.5_BELOW_ESTABLISHED_MINIMUM_SAMPLE"
    span = Decimal(
        STATISTICAL_QUALIFICATION_SATURATION_SAMPLES - STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES
    )
    progress = Decimal(
        min(sample_size, STATISTICAL_QUALIFICATION_SATURATION_SAMPLES)
        - STATISTICAL_QUALIFICATION_MINIMUM_SAMPLES
    )
    with localcontext() as context:
        context.prec = 28
        return Decimal("0.5") + Decimal("0.5") * progress / span, None


def _effective_slippage(item: SharedOpportunityProjection) -> Decimal:
    """Conservative stand-in for a missing ``modeled_slippage``: never zero (that would be a
    favorable fabrication -- see acceptance criterion on missingness), never an invented external
    constant either. When slippage genuinely was not separately modeled for this venue/method
    (100% of retained Kalshi opportunities today, by design: their acquisition cost is already a
    matched-depth execution price, not a quote plus a separately modeled bps markup -- see
    ``public_complement``/``public_exhaustive``/``public_threshold``), assume it costs at least
    as much as this SAME opportunity's own known fee rate, so missing slippage can never score
    more favorably than an explicit slippage figure at least as large as the fee rate."""

    if item.modeled_slippage is not None:
        return item.modeled_slippage
    return item.modeled_fees or Decimal(0)


_SOURCE_HEALTH_STATE_SCORES: dict[StrategyHealthState, Decimal] = {
    StrategyHealthState.HEALTHY: Decimal("1"),
    StrategyHealthState.WATCH: Decimal("0.5"),
    StrategyHealthState.DEGRADED: Decimal("0.15"),
    StrategyHealthState.RECONCILIATION_BLOCKED: Decimal("0.05"),
    StrategyHealthState.DATA_QUALITY_BLOCKED: Decimal("0.05"),
}
_SOURCE_HEALTH_NO_SAMPLE_STATES = frozenset(
    {StrategyHealthState.NO_OPERATIONAL_EVIDENCE, StrategyHealthState.INSUFFICIENT_SAMPLE}
)


def _source_health(assessment: StrategyHealthAssessment | None) -> tuple[Decimal | None, str]:
    """Returns ``(score, explanation)``. ``score`` is ``None`` iff the component must be
    EXCLUDED (weight deterministically redistributed by ``_effective_weights``, never
    fabricated as a fake perfect/neutral value): no retained assessment exists for this
    opportunity's method, or the retained assessment has not itself cleared
    ``StrategyHealthPolicy``'s own minimum-sample gate yet."""

    if assessment is None:
        return None, "EXCLUDED_NO_RETAINED_HEALTH_ASSESSMENT_FOR_METHOD"
    if assessment.state in _SOURCE_HEALTH_NO_SAMPLE_STATES:
        return None, f"EXCLUDED_NO_MEANINGFUL_SAMPLE:{assessment.state.value}"
    return (
        _SOURCE_HEALTH_STATE_SCORES[assessment.state],
        f"GENUINE_HEALTH_STATE:{assessment.state.value}",
    )


def prioritize(
    projections: tuple[SharedOpportunityProjection, ...],
    decisions: tuple[EligibilityDecision, ...],
    *,
    opportunity_set_id: str,
    health_by_method: Mapping[str, StrategyHealthAssessment] | None = None,
) -> tuple[PriorityResult, ...]:
    eligible_ids = {
        item.projection_id for item in decisions if item.state is EligibilityState.ELIGIBLE
    }
    health_lookup = health_by_method or {}
    eligible_items = tuple(item for item in projections if item.projection_id in eligible_ids)

    # Pass 1: build the contemporaneous, same-venue peer population for percentile
    # normalization. Population == exactly the opportunities passed into *this* call (this
    # function is pure and has no other data source), so it is point-in-time correct by
    # construction -- no later evidence, no future opportunity state, can ever leak in. Only
    # genuine (non-proxy), horizon-normalizable magnitudes participate: mixing proxy scores or
    # un-normalized raw magnitudes into the pool would defeat the purpose of normalizing them.
    magnitude_by_id: dict[str, Decimal] = {}
    magnitude_exclusion_by_id: dict[str, str] = {}
    population_by_venue: dict[SharedVenue, dict[str, Decimal]] = {
        venue: {} for venue in SharedVenue
    }
    for item in eligible_items:
        value, exclusion_reason = _economic_magnitude_signal(item)
        if value is None:
            assert exclusion_reason is not None
            magnitude_exclusion_by_id[item.projection_id] = exclusion_reason
            continue
        magnitude_by_id[item.projection_id] = value
        population_by_venue[item.venue][item.projection_id] = value

    staged: list[
        tuple[
            SharedOpportunityProjection,
            tuple[PriorityComponent, ...],
            Decimal,
            PriorityTier,
            tuple[str, ...],
        ]
    ] = []
    for item in eligible_items:
        capital = item.required_hypothetical_capital or Decimal(1)
        per_day_value = magnitude_by_id.get(item.projection_id)
        venue_population = population_by_venue[item.venue]
        magnitude_available = per_day_value is not None

        if per_day_value is None:
            comparable_normalized = Decimal("0.5")
            comparable_missing: str | None = magnitude_exclusion_by_id[item.projection_id]
        elif len(venue_population) < 2:
            comparable_normalized = Decimal("0.5")
            comparable_missing = "INSUFFICIENT_VENUE_POPULATION_FOR_PERCENTILE"
        else:
            comparable_normalized = _venue_percentile(item.projection_id, venue_population)
            comparable_missing = None
        comparable_detail = (
            f"raw_comparable_value={item.comparable_value}; "
            f"economic_magnitude_available={magnitude_available}; "
            f"horizon_days={item.expected_holding_days}; "
            f"horizon_normalized_per_day={'n/a' if per_day_value is None else per_day_value}; "
            f"venue={item.venue.value}; venue_percentile_population_size={len(venue_population)}"
        )

        health_score, health_explanation = _source_health(
            health_lookup.get(item.strategy_or_method_id)
        )
        excluded_components = frozenset({"SOURCE_HEALTH"}) if health_score is None else frozenset()
        weights = _effective_weights(excluded_components)

        statistical_score, statistical_missing = _statistical_qualification(
            item.statistical_sample_size
        )

        components: tuple[PriorityComponent, ...] = (
            _component(
                "COMPARABLE_VALUE",
                item.comparable_value,
                comparable_normalized,
                weights["COMPARABLE_VALUE"],
                missing=comparable_missing,
                detail=comparable_detail,
            ),
            _component(
                "DOWNSIDE",
                item.maximum_loss,
                Decimal("0.5")
                if item.maximum_loss is None
                else Decimal(1) - item.maximum_loss / capital,
                weights["DOWNSIDE"],
                missing="NEUTRAL_0.5_MAXIMUM_LOSS_MISSING" if item.maximum_loss is None else None,
            ),
            _component(
                "COST",
                None
                if item.modeled_fees is None
                else item.modeled_fees + _effective_slippage(item),
                (
                    Decimal("0.5")
                    if item.modeled_fees is None
                    else Decimal(1) - (item.modeled_fees + _effective_slippage(item)) / capital
                ),
                weights["COST"],
                missing="NEUTRAL_0.5_FEE_EVIDENCE_MISSING" if item.modeled_fees is None else None,
                detail=(
                    None
                    if item.modeled_fees is None or item.modeled_slippage is not None
                    else "modeled_slippage missing -- no separate slippage line for this"
                    " venue/method (e.g. Kalshi's acquisition cost is already a matched-depth"
                    " execution price, not a quote plus a separately modeled bps markup);"
                    " conservatively assumed to cost at least as much as this opportunity's own"
                    f" known fee rate ({item.modeled_fees}) rather than zero"
                ),
            ),
            _component(
                "LIQUIDITY",
                None,
                Decimal(1) if item.liquidity_state == "SUFFICIENT" else Decimal("0.5"),
                weights["LIQUIDITY"],
            ),
            _component(
                "LOCKUP",
                None if item.estimated_lockup_days is None else Decimal(item.estimated_lockup_days),
                Decimal("0.5")
                if item.estimated_lockup_days is None
                else Decimal(1) - Decimal(item.estimated_lockup_days) / Decimal(365),
                weights["LOCKUP"],
                missing="NEUTRAL_0.5" if item.estimated_lockup_days is None else None,
            ),
            _component(
                "FRESHNESS",
                Decimal(item.freshness_seconds),
                Decimal(1)
                - Decimal(item.freshness_seconds) / Decimal(item.freshness_limit_seconds),
                weights["FRESHNESS"],
            ),
            _component(
                "EVIDENCE",
                None,
                Decimal(1) if item.evidence_class == "GENUINE_PUBLIC" else Decimal("0.75"),
                weights["EVIDENCE"],
            ),
            _component(
                "UNCERTAINTY",
                item.uncertainty,
                Decimal("0.5") if item.uncertainty is None else Decimal(1) - item.uncertainty,
                weights["UNCERTAINTY"],
                missing="NEUTRAL_0.5" if item.uncertainty is None else None,
            ),
            _component(
                "SOURCE_HEALTH",
                None,
                Decimal(0) if health_score is None else health_score,
                weights["SOURCE_HEALTH"],
                missing=health_explanation if health_score is None else None,
            ),
            _component(
                "STATISTICAL_QUALIFICATION",
                (
                    None
                    if item.statistical_sample_size is None
                    else Decimal(item.statistical_sample_size)
                ),
                statistical_score,
                weights["STATISTICAL_QUALIFICATION"],
                missing=statistical_missing,
            ),
        )
        assert sum((component.weight for component in components), Decimal(0)) == Decimal(1)
        score = sum((component.weighted_value for component in components), Decimal(0))

        tier_reasons: list[str] = []
        if not magnitude_available:
            tier = PriorityTier.RESEARCH_ONLY
            tier_reasons.append(
                f"NO_GENUINE_ECONOMIC_MAGNITUDE:{magnitude_exclusion_by_id[item.projection_id]}"
            )
        elif cast(Decimal, per_day_value) <= OPPORTUNITY_COST_FLOOR_V1:
            tier = PriorityTier.RESEARCH_ONLY
            tier_reasons.append(
                f"AT_OR_BELOW_OPPORTUNITY_COST_FLOOR_V1:per_day={per_day_value}"
                f":floor={OPPORTUNITY_COST_FLOOR_V1}"
            )
        else:
            tier = PriorityTier.ACTIONABLE
            tier_reasons.append(
                f"ABOVE_OPPORTUNITY_COST_FLOOR_V1:per_day={per_day_value}"
                f":floor={OPPORTUNITY_COST_FLOOR_V1}"
            )

        staged.append(
            (
                item,
                components,
                score.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN),
                tier,
                tuple(tier_reasons),
            )
        )
    staged.sort(
        key=lambda row: (
            -row[2],
            row[0].venue.value,
            row[0].source_record_id,
            row[0].projection_id,
        )
    )
    results: list[PriorityResult] = []
    for rank, (
        staged_projection,
        staged_components,
        staged_score,
        staged_tier,
        staged_tier_reasons,
    ) in enumerate(staged, 1):
        # Scoped by opportunity_set_id, exactly like explanation_id below in
        # build_opportunity_set: priority_id must be unique per (set, projection),
        # never per projection alone. A projection genuinely persists unchanged
        # across many days (_reuse_projection reuses it by design -- SECURITIES
        # structures are the common case), but priority is a fresh, set-relative
        # ranking recomputed every day; an un-scoped id collides with the row
        # already committed under the FIRST opportunity_set_id that ever ranked
        # this projection, raising SHARED_OPPORTUNITY_PERSISTENCE_CONFLICT out of
        # save_opportunity_bundle's plain (no reuse-check) priority insert the
        # moment the same projection is ranked a second time under a new set.
        priority_id = f"{opportunity_set_id}-{staged_projection.projection_id}-priority-v1"
        tie_key = (
            staged_projection.venue.value,
            staged_projection.source_record_id,
            staged_projection.projection_id,
        )
        largest_components = ",".join(
            value.name
            for value in sorted(
                staged_components,
                key=lambda value: (-value.weighted_value, value.name),
            )[:3]
        )
        explanation = (
            f"rank={rank}; score={staged_score}; tier={staged_tier.value}; "
            f"deterministic_tie_break={tie_key}; largest_components={largest_components}"
        )
        priority_payload = (
            priority_id,
            staged_projection.projection_id,
            SHARED_OPPORTUNITY_PRIORITY_POLICY_V1,
            staged_score,
            rank,
            tie_key,
            staged_components,
            explanation,
            "NONE",
            staged_tier,
            staged_tier_reasons,
        )
        results.append(
            PriorityResult(
                priority_id=priority_id,
                projection_id=staged_projection.projection_id,
                policy_id=SHARED_OPPORTUNITY_PRIORITY_POLICY_V1,
                score=staged_score,
                rank=rank,
                tie_break_key=tie_key,
                components=staged_components,
                explanation=explanation,
                canonical_hash=content_digest(priority_payload),
                authority="NONE",
                tier=staged_tier,
                tier_reasons=staged_tier_reasons,
            )
        )
    return tuple(results)


@dataclass(frozen=True, slots=True)
class OpportunityExplanation:
    explanation_id: str
    opportunity_set_id: str
    projection_id: str
    eligibility_state: EligibilityState
    text: str
    limiting_missing_evidence: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"


@dataclass(frozen=True, slots=True)
class SharedOpportunitySet:
    opportunity_set_id: str
    created_at: datetime
    projection_ids: tuple[str, ...]
    eligibility_decision_ids: tuple[str, ...]
    priority_result_ids: tuple[str, ...]
    ranked_projection_ids: tuple[str, ...]
    tie_groups: tuple[tuple[str, ...], ...]
    expired_projection_ids: tuple[str, ...]
    unavailable_projection_ids: tuple[str, ...]
    manifest_hash: str
    policy_ids: tuple[str, str]
    limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"


def build_opportunity_set(
    *,
    opportunity_set_id: str,
    created_at: datetime,
    projections: tuple[SharedOpportunityProjection, ...],
    health_by_method: Mapping[str, StrategyHealthAssessment] | None = None,
) -> tuple[
    SharedOpportunitySet,
    tuple[EligibilityDecision, ...],
    tuple[PriorityResult, ...],
    tuple[OpportunityExplanation, ...],
]:
    timestamp = _time(created_at, "SHARED_OPPORTUNITY_SET_TIME_INVALID")
    set_id = _identity(opportunity_set_id, "SHARED_OPPORTUNITY_SET_ID_INVALID")
    ordered = tuple(sorted(projections, key=lambda item: item.projection_id))
    decisions = tuple(evaluate_eligibility(item, evaluated_at=timestamp) for item in ordered)
    priorities = prioritize(
        ordered, decisions, opportunity_set_id=set_id, health_by_method=health_by_method
    )
    score_groups: dict[Decimal, list[str]] = {}
    for priority in priorities:
        score_groups.setdefault(priority.score, []).append(priority.projection_id)
    ties = tuple(
        tuple(values) for _, values in sorted(score_groups.items(), reverse=True) if len(values) > 1
    )
    manifest = content_digest(tuple((item.projection_id, item.canonical_hash) for item in ordered))
    explanations: list[OpportunityExplanation] = []
    by_priority = {item.projection_id: item for item in priorities}
    for decision in decisions:
        selected_priority = by_priority.get(decision.projection_id)
        missing = tuple(reason for reason in decision.reasons if reason.endswith("_MISSING"))
        if selected_priority:
            text = (
                f"Eligible for research comparison only; {selected_priority.explanation}. "
                "Allocation is hypothetical and policy-capped."
            )
        else:
            text = (
                f"Not prioritized: state={decision.state.value}; "
                f"reasons={','.join(decision.reasons) or 'none'}; no hypothetical allocation."
            )
        explanation_id = f"{set_id}-{decision.projection_id}-explanation"
        explanation_payload = (
            explanation_id,
            set_id,
            decision.projection_id,
            decision.state,
            text,
            missing,
            "NONE",
        )
        explanations.append(
            OpportunityExplanation(
                explanation_id=explanation_id,
                opportunity_set_id=set_id,
                projection_id=decision.projection_id,
                eligibility_state=decision.state,
                text=text,
                limiting_missing_evidence=missing,
                canonical_hash=content_digest(explanation_payload),
                authority="NONE",
            )
        )
    set_payload = (
        set_id,
        timestamp,
        tuple(item.projection_id for item in ordered),
        tuple(item.decision_id for item in decisions),
        tuple(item.priority_id for item in priorities),
        tuple(item.projection_id for item in priorities),
        ties,
        tuple(item.projection_id for item in decisions if item.state is EligibilityState.EXPIRED),
        tuple(
            item.projection_id
            for item in decisions
            if item.state is EligibilityState.BLOCKED_MISSING_EVIDENCE
        ),
        manifest,
        (
            SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1,
            SHARED_OPPORTUNITY_PRIORITY_POLICY_V1,
        ),
        (
            "VENUE_ECONOMICS_REMAIN_SOURCE_SPECIFIC",
            "MISSING_VALUES_ARE_NOT_ZERO",
            "TOTAL_MARKET_CONTEXT_NOT_ATTACHED",
            SHARED_OPPORTUNITY_WARNING,
        ),
        "NONE",
        False,
        False,
        "NONE",
    )
    opportunity_set = SharedOpportunitySet(
        opportunity_set_id=set_id,
        created_at=timestamp,
        projection_ids=tuple(item.projection_id for item in ordered),
        eligibility_decision_ids=tuple(item.decision_id for item in decisions),
        priority_result_ids=tuple(item.priority_id for item in priorities),
        ranked_projection_ids=tuple(item.projection_id for item in priorities),
        tie_groups=ties,
        expired_projection_ids=tuple(
            item.projection_id for item in decisions if item.state is EligibilityState.EXPIRED
        ),
        unavailable_projection_ids=tuple(
            item.projection_id
            for item in decisions
            if item.state is EligibilityState.BLOCKED_MISSING_EVIDENCE
        ),
        manifest_hash=manifest,
        policy_ids=(
            SHARED_OPPORTUNITY_ELIGIBILITY_POLICY_V1,
            SHARED_OPPORTUNITY_PRIORITY_POLICY_V1,
        ),
        limitations=(
            "VENUE_ECONOMICS_REMAIN_SOURCE_SPECIFIC",
            "MISSING_VALUES_ARE_NOT_ZERO",
            "TOTAL_MARKET_CONTEXT_NOT_ATTACHED",
            SHARED_OPPORTUNITY_WARNING,
        ),
        canonical_hash=content_digest(set_payload),
        authority="NONE",
        paper_eligible=False,
        live_eligible=False,
        capital_authority="NONE",
    )
    return opportunity_set, decisions, priorities, tuple(explanations)


@dataclass(frozen=True, slots=True)
class HypotheticalAllocation:
    projection_id: str
    venue: SharedVenue
    amount: Decimal
    modeled_downside: Decimal
    modeled_costs: Decimal
    estimated_lockup_days: int | None
    explanation: str


@dataclass(frozen=True, slots=True)
class HypotheticalAllocationProposal:
    proposal_id: str
    opportunity_set_id: str
    priority_policy_id: str
    hypothetical_budget: Decimal
    allocations: tuple[HypotheticalAllocation, ...]
    unallocated_cash: Decimal
    venue_totals: tuple[tuple[str, Decimal], ...]
    modeled_downside: Decimal
    modeled_costs: Decimal
    estimated_lockup_days: int | None
    warnings: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    account_capital: bool = False
    reservation: bool = False
    order: bool = False
    paper_eligible: bool = False
    live_eligible: bool = False
    financial_authority: str = "NONE"


def propose_hypothetical_allocation(
    *,
    proposal_id: str,
    opportunity_set: SharedOpportunitySet,
    projections: tuple[SharedOpportunityProjection, ...],
    priorities: tuple[PriorityResult, ...],
    budget: Decimal = HYPOTHETICAL_BUDGET,
) -> HypotheticalAllocationProposal:
    if budget != HYPOTHETICAL_BUDGET:
        raise SharedOpportunityError("SHARED_ALLOCATION_BUDGET_MUST_EQUAL_1000")
    by_id = {item.projection_id: item for item in projections}
    remaining = budget
    venue_totals: dict[SharedVenue, Decimal] = {venue: Decimal(0) for venue in SharedVenue}
    allocations: list[HypotheticalAllocation] = []
    rejection_reasons: list[str] = []
    for priority in priorities:
        item = by_id[priority.projection_id]
        cap = min(
            budget * MAX_OPPORTUNITY_FRACTION,
            budget * MAX_VENUE_FRACTION - venue_totals[item.venue],
            remaining,
            item.maximum_sensible_research_allocation or Decimal(0),
        )
        required = item.required_hypothetical_capital or Decimal(0)
        if cap <= 0 or cap < required:
            rejection_reasons.append(f"{item.projection_id}:ALLOCATION_CAP_BELOW_REQUIRED_CAPITAL")
            continue
        ratio = cap / required
        downside = ((item.maximum_loss or Decimal(0)) * ratio).quantize(
            _QUANTUM, rounding=ROUND_HALF_EVEN
        )
        costs = (
            ((item.modeled_fees or Decimal(0)) + (item.modeled_slippage or Decimal(0))) * ratio
        ).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
        allocations.append(
            HypotheticalAllocation(
                item.projection_id,
                item.venue,
                cap,
                downside,
                costs,
                item.estimated_lockup_days,
                f"rank={priority.rank}; capped by 25% opportunity, 50% venue, "
                "retained maximum, and remaining cash",
            )
        )
        venue_totals[item.venue] += cap
        remaining -= cap
    lockups = tuple(
        item.estimated_lockup_days for item in allocations if item.estimated_lockup_days is not None
    )
    values = (
        _identity(proposal_id, "SHARED_ALLOCATION_ID_INVALID"),
        opportunity_set.opportunity_set_id,
        SHARED_OPPORTUNITY_PRIORITY_POLICY_V1,
        budget,
        tuple(allocations),
        remaining,
        tuple(
            (venue.value, venue_totals[venue]) for venue in SharedVenue if venue_totals[venue] > 0
        ),
        sum((item.modeled_downside for item in allocations), Decimal(0)),
        sum((item.modeled_costs for item in allocations), Decimal(0)),
        max(lockups) if lockups else None,
        (
            "HYPOTHETICAL RESEARCH ONLY",
            "NOT ACCOUNT CAPITAL",
            "NOT A RESERVATION",
            "NOT AN ORDER",
            "NOT PAPER-ELIGIBLE",
            "NOT LIVE-ELIGIBLE",
            SHARED_OPPORTUNITY_WARNING,
        ),
        tuple(rejection_reasons),
        "NONE",
        False,
        False,
        False,
        False,
        False,
        "NONE",
    )
    return HypotheticalAllocationProposal(*values[:-7], content_digest(values), *values[-7:])
