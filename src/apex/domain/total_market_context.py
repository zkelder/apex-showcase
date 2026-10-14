"""Pure Total Market context projections and Opportunity attachments.

Context is retained explanatory evidence only.  It never changes Opportunity
eligibility, priority, allocation, or any financial authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import cast

from apex.domain.shared_opportunity import SharedOpportunityProjection, SharedVenue
from apex.domain.values import content_digest, deterministic_id, parse_decimal

TOTAL_MARKET_CONTEXT_COMPONENT_V1 = "TOTAL_MARKET_CONTEXT_COMPONENT_V1"
TOTAL_MARKET_CONTEXT_SNAPSHOT_V1 = "TOTAL_MARKET_CONTEXT_SNAPSHOT_V1"
TOTAL_MARKET_CONTEXT_POLICY_V1 = "TOTAL_MARKET_CONTEXT_POLICY_V1"
TOTAL_MARKET_CONTEXT_ATTACHMENT_V1 = "TOTAL_MARKET_CONTEXT_ATTACHMENT_V1"
CONTEXT_ENRICHED_OPPORTUNITY_SET_V1 = "CONTEXT_ENRICHED_OPPORTUNITY_SET_V1"
TOTAL_MARKET_CONTEXT_WARNING = (
    "TOTAL MARKET CONTEXT IS EXPLANATORY EVIDENCE — NOT A RETURN PREDICTION OR TRADING RULE"
)
COMPONENT_FRESHNESS_LIMIT_SECONDS = 259_200
MAX_COMPONENT_OBSERVATION_SKEW_SECONDS = 86_400
MINIMUM_OVERALL_COMPONENTS = 2
_QUANTUM = Decimal("0.00000001")


class TotalMarketContextError(ValueError):
    """Raised when context evidence is invalid or conflicts."""


class ContextComponentType(StrEnum):
    US_EQUITY_MARKET = "US_EQUITY_MARKET"
    TREASURY_RATE = "TREASURY_RATE"
    YIELD_CURVE = "YIELD_CURVE"
    MARKET_VOLATILITY = "MARKET_VOLATILITY"
    US_DOLLAR = "US_DOLLAR"
    COMMODITY_OR_INFLATION = "COMMODITY_OR_INFLATION"
    MACRO_EVENT_PROXIMITY = "MACRO_EVENT_PROXIMITY"


class ContextComponentState(StrEnum):
    SUPPORTIVE = "SUPPORTIVE"
    NEUTRAL = "NEUTRAL"
    CAUTIONARY = "CAUTIONARY"
    STRESSED = "STRESSED"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    QUALITY_BLOCKED = "QUALITY_BLOCKED"


class OverallContextState(StrEnum):
    RISK_SUPPORTIVE = "RISK_SUPPORTIVE"
    MIXED = "MIXED"
    RISK_CAUTIONARY = "RISK_CAUTIONARY"
    MARKET_STRESS = "MARKET_STRESS"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"


class ApplicabilityState(StrEnum):
    RELEVANT = "RELEVANT"
    IRRELEVANT = "IRRELEVANT"
    UNAVAILABLE = "UNAVAILABLE"


def _time(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None:
        raise TotalMarketContextError(reason)
    return value.astimezone(UTC)


def _identity(value: str, reason: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 300:
        raise TotalMarketContextError(reason)
    return normalized


def _decimal(value: object, reason: str) -> Decimal:
    try:
        return parse_decimal(cast(str | int | Decimal, value))
    except (TypeError, ValueError, ArithmeticError) as error:
        raise TotalMarketContextError(reason) from error


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class ContextPolicyThreshold:
    component_type: ContextComponentType
    supportive: str
    cautionary: str
    stressed: str


POLICY_THRESHOLDS = (
    ContextPolicyThreshold(
        ContextComponentType.US_EQUITY_MARKET,
        "20-session return >= 0.03",
        "20-session return <= -0.03",
        "20-session return <= -0.10",
    ),
    ContextPolicyThreshold(
        ContextComponentType.TREASURY_RATE,
        "TLT 20-session price return >= 0.03",
        "TLT 20-session price return <= -0.03",
        "TLT 20-session price return <= -0.07",
    ),
    ContextPolicyThreshold(
        ContextComponentType.MARKET_VOLATILITY,
        "annualized 20-session realized volatility < 0.15",
        "annualized 20-session realized volatility >= 0.25",
        "annualized 20-session realized volatility >= 0.40",
    ),
    ContextPolicyThreshold(
        ContextComponentType.COMMODITY_OR_INFLATION,
        "not assigned by this policy",
        "absolute USO 20-session return >= 0.05",
        "absolute USO 20-session return >= 0.15",
    ),
)
TOTAL_MARKET_CONTEXT_POLICY_HASH = content_digest(
    (
        TOTAL_MARKET_CONTEXT_POLICY_V1,
        POLICY_THRESHOLDS,
        COMPONENT_FRESHNESS_LIMIT_SECONDS,
        MAX_COMPONENT_OBSERVATION_SKEW_SECONDS,
        MINIMUM_OVERALL_COMPONENTS,
        "ANY_STRESSED_IS_MARKET_STRESS",
        "CONTRIBUTION_SUM_GTE_1_SUPPORTIVE_LTE_NEGATIVE_2_CAUTIONARY_OTHERWISE_MIXED",
    )
)


def classify_component(
    component_type: ContextComponentType,
    *,
    normalized_change: Decimal,
    current_value: Decimal,
) -> tuple[ContextComponentState, int, str]:
    if component_type is ContextComponentType.US_EQUITY_MARKET:
        if normalized_change <= Decimal("-0.10"):
            return (
                ContextComponentState.STRESSED,
                -2,
                "EQUITY_RETURN_AT_OR_BELOW_NEGATIVE_10_PERCENT",
            )
        if normalized_change <= Decimal("-0.03"):
            return (
                ContextComponentState.CAUTIONARY,
                -1,
                "EQUITY_RETURN_AT_OR_BELOW_NEGATIVE_3_PERCENT",
            )
        if normalized_change >= Decimal("0.03"):
            return ContextComponentState.SUPPORTIVE, 1, "EQUITY_RETURN_AT_OR_ABOVE_3_PERCENT"
        return ContextComponentState.NEUTRAL, 0, "EQUITY_RETURN_WITHIN_NEUTRAL_BAND"
    if component_type is ContextComponentType.TREASURY_RATE:
        if normalized_change <= Decimal("-0.07"):
            return (
                ContextComponentState.STRESSED,
                -2,
                "TREASURY_PRICE_PROXY_AT_OR_BELOW_NEGATIVE_7_PERCENT",
            )
        if normalized_change <= Decimal("-0.03"):
            return (
                ContextComponentState.CAUTIONARY,
                -1,
                "TREASURY_PRICE_PROXY_AT_OR_BELOW_NEGATIVE_3_PERCENT",
            )
        if normalized_change >= Decimal("0.03"):
            return (
                ContextComponentState.SUPPORTIVE,
                1,
                "TREASURY_PRICE_PROXY_AT_OR_ABOVE_3_PERCENT",
            )
        return ContextComponentState.NEUTRAL, 0, "TREASURY_PRICE_PROXY_WITHIN_NEUTRAL_BAND"
    if component_type is ContextComponentType.MARKET_VOLATILITY:
        if current_value >= Decimal("0.40"):
            return ContextComponentState.STRESSED, -2, "REALIZED_VOLATILITY_AT_OR_ABOVE_40_PERCENT"
        if current_value >= Decimal("0.25"):
            return (
                ContextComponentState.CAUTIONARY,
                -1,
                "REALIZED_VOLATILITY_AT_OR_ABOVE_25_PERCENT",
            )
        if current_value < Decimal("0.15"):
            return ContextComponentState.SUPPORTIVE, 1, "REALIZED_VOLATILITY_BELOW_15_PERCENT"
        return ContextComponentState.NEUTRAL, 0, "REALIZED_VOLATILITY_WITHIN_NEUTRAL_BAND"
    if component_type is ContextComponentType.COMMODITY_OR_INFLATION:
        absolute = abs(normalized_change)
        if absolute >= Decimal("0.15"):
            return ContextComponentState.STRESSED, -2, "COMMODITY_MOVE_AT_OR_ABOVE_15_PERCENT"
        if absolute >= Decimal("0.05"):
            return ContextComponentState.CAUTIONARY, -1, "COMMODITY_MOVE_AT_OR_ABOVE_5_PERCENT"
        return ContextComponentState.NEUTRAL, 0, "COMMODITY_MOVE_WITHIN_NEUTRAL_BAND"
    raise TotalMarketContextError("TOTAL_MARKET_COMPONENT_TYPE_UNSUPPORTED")


@dataclass(frozen=True, slots=True)
class ContextComponent:
    component_id: str
    version: int
    component_type: ContextComponentType
    source_observation_id: str
    provider: str
    subject: str
    observation_at: datetime
    available_at: datetime
    ingested_at: datetime
    current_value: Decimal
    prior_comparable_value: Decimal
    normalized_change: Decimal
    freshness_seconds: int
    freshness_limit_seconds: int
    quality_status: str
    evidence_class: str
    limitations: tuple[str, ...]
    interpretation_category: str
    state: ContextComponentState
    contribution: int
    classification_reason: str
    supporting_evidence_ids: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    order_authority: str = "NONE"
    financial_mutation_capability: str = "NONE"

    @classmethod
    def create(
        cls,
        *,
        component_id: str,
        component_type: ContextComponentType,
        source_observation_id: str,
        provider: str,
        subject: str,
        observation_at: datetime,
        available_at: datetime,
        ingested_at: datetime,
        current_value: object,
        prior_comparable_value: object,
        freshness_seconds: int,
        freshness_limit_seconds: int,
        quality_status: str,
        evidence_class: str,
        limitations: tuple[str, ...],
        interpretation_category: str,
        supporting_evidence_ids: tuple[str, ...],
    ) -> ContextComponent:
        observation = _time(observation_at, "TOTAL_MARKET_OBSERVATION_TIME_INVALID")
        availability = _time(available_at, "TOTAL_MARKET_AVAILABILITY_TIME_INVALID")
        ingestion = _time(ingested_at, "TOTAL_MARKET_INGESTION_TIME_INVALID")
        if availability < observation or ingestion < availability:
            raise TotalMarketContextError("TOTAL_MARKET_SOURCE_TIME_ORDER_INVALID")
        current = _decimal(current_value, "TOTAL_MARKET_CURRENT_VALUE_INVALID")
        prior = _decimal(prior_comparable_value, "TOTAL_MARKET_PRIOR_VALUE_INVALID")
        if prior == 0:
            raise TotalMarketContextError("TOTAL_MARKET_PRIOR_VALUE_ZERO")
        if freshness_seconds < 0 or freshness_limit_seconds <= 0:
            raise TotalMarketContextError("TOTAL_MARKET_FRESHNESS_INVALID")
        normalized_change = _q(current / prior - 1)
        if quality_status != "VALID":
            state, contribution, reason = (
                ContextComponentState.QUALITY_BLOCKED,
                0,
                "SOURCE_QUALITY_NOT_VALID",
            )
        elif freshness_seconds > freshness_limit_seconds:
            state, contribution, reason = ContextComponentState.STALE, 0, "SOURCE_EVIDENCE_STALE"
        else:
            state, contribution, reason = classify_component(
                component_type,
                normalized_change=normalized_change,
                current_value=current,
            )
        values = (
            _identity(component_id, "TOTAL_MARKET_COMPONENT_ID_INVALID"),
            1,
            component_type,
            _identity(source_observation_id, "TOTAL_MARKET_SOURCE_OBSERVATION_INVALID"),
            _identity(provider, "TOTAL_MARKET_PROVIDER_INVALID"),
            _identity(subject, "TOTAL_MARKET_SUBJECT_INVALID"),
            observation,
            availability,
            ingestion,
            current,
            prior,
            normalized_change,
            freshness_seconds,
            freshness_limit_seconds,
            _identity(quality_status, "TOTAL_MARKET_QUALITY_INVALID"),
            _identity(evidence_class, "TOTAL_MARKET_EVIDENCE_CLASS_INVALID"),
            tuple(sorted(set(limitations))),
            _identity(interpretation_category, "TOTAL_MARKET_INTERPRETATION_INVALID"),
            state,
            contribution,
            reason,
            tuple(sorted(set(supporting_evidence_ids))),
            "NONE",
            False,
            False,
            "NONE",
            "NONE",
            "NONE",
        )
        return cls(
            *values[:-6], content_digest((TOTAL_MARKET_CONTEXT_COMPONENT_V1, *values)), *values[-6:]
        )


@dataclass(frozen=True, slots=True)
class UnavailableContextComponent:
    component_type: ContextComponentType
    reason: str


@dataclass(frozen=True, slots=True)
class ContextSourceBundle:
    components: tuple[ContextComponent, ...]
    unavailable_components: tuple[UnavailableContextComponent, ...]


@dataclass(frozen=True, slots=True)
class TotalMarketContextSnapshot:
    snapshot_id: str
    version: int
    created_at: datetime
    source_batch_id: str
    component_ids: tuple[str, ...]
    source_evidence_ids: tuple[str, ...]
    source_timestamps: tuple[datetime, ...]
    source_availability_timestamps: tuple[datetime, ...]
    source_ingestion_timestamps: tuple[datetime, ...]
    freshness_limit_seconds: int
    unavailable_components: tuple[UnavailableContextComponent, ...]
    component_states: tuple[tuple[str, str], ...]
    overall_state: OverallContextState
    contribution_total: int
    policy_id: str
    policy_hash: str
    warnings: tuple[str, ...]
    limitations: tuple[str, ...]
    manifest_hash: str
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    order_authority: str = "NONE"
    financial_mutation_capability: str = "NONE"


def build_context_snapshot(
    *,
    snapshot_id: str,
    created_at: datetime,
    source_batch_id: str,
    components: tuple[ContextComponent, ...],
    unavailable_components: tuple[UnavailableContextComponent, ...],
) -> TotalMarketContextSnapshot:
    created = _time(created_at, "TOTAL_MARKET_SNAPSHOT_TIME_INVALID")
    ordered = tuple(sorted(components, key=lambda item: item.component_type.value))
    if len({item.component_type for item in ordered}) != len(ordered):
        raise TotalMarketContextError("TOTAL_MARKET_COMPONENT_TYPE_DUPLICATE")
    unavailable = tuple(sorted(unavailable_components, key=lambda item: item.component_type.value))
    if set(item.component_type for item in ordered).intersection(
        item.component_type for item in unavailable
    ):
        raise TotalMarketContextError("TOTAL_MARKET_COMPONENT_AVAILABLE_AND_UNAVAILABLE")
    expected = set(ContextComponentType)
    represented = {item.component_type for item in ordered}.union(
        item.component_type for item in unavailable
    )
    if represented != expected:
        raise TotalMarketContextError("TOTAL_MARKET_COMPONENT_VOCABULARY_INCOMPLETE")
    if any(item.available_at > created or item.ingested_at > created for item in ordered):
        raise TotalMarketContextError("TOTAL_MARKET_SOURCE_AFTER_CONTEXT_CUTOFF")
    if any(
        int((created - item.available_at).total_seconds()) != item.freshness_seconds
        for item in ordered
    ):
        raise TotalMarketContextError("TOTAL_MARKET_COMPONENT_FRESHNESS_MISMATCH")
    observation_times = tuple(item.observation_at for item in ordered)
    if observation_times and int(
        (max(observation_times) - min(observation_times)).total_seconds()
    ) > (MAX_COMPONENT_OBSERVATION_SKEW_SECONDS):
        raise TotalMarketContextError("TOTAL_MARKET_COMPONENT_CAPTURE_PERIOD_INCOMPATIBLE")
    usable = tuple(
        item
        for item in ordered
        if item.state not in {ContextComponentState.STALE, ContextComponentState.QUALITY_BLOCKED}
    )
    contribution = sum(item.contribution for item in usable)
    if len(usable) < MINIMUM_OVERALL_COMPONENTS:
        overall = OverallContextState.INSUFFICIENT_CONTEXT
    elif any(item.state is ContextComponentState.STRESSED for item in usable):
        overall = OverallContextState.MARKET_STRESS
    elif contribution >= 1:
        overall = OverallContextState.RISK_SUPPORTIVE
    elif contribution <= -2:
        overall = OverallContextState.RISK_CAUTIONARY
    else:
        overall = OverallContextState.MIXED
    manifest = content_digest(tuple((item.component_id, item.canonical_hash) for item in ordered))
    limitations = (
        "CONTEXT_DOES_NOT_PREDICT_RETURNS",
        "CONTEXT_DOES_NOT_CHANGE_OPPORTUNITY_DECISIONS",
        "TREASURY_COMPONENT_IS_AN_EXPLICIT_TLT_PRICE_PROXY_NOT_A_YIELD",
        "COMMODITY_COMPONENT_IS_AN_EXPLICIT_USO_PRICE_PROXY_NOT_AN_INFLATION_MEASURE",
    )
    values = (
        _identity(snapshot_id, "TOTAL_MARKET_SNAPSHOT_ID_INVALID"),
        1,
        created,
        _identity(source_batch_id, "TOTAL_MARKET_SOURCE_BATCH_INVALID"),
        tuple(item.component_id for item in ordered),
        tuple(sorted({value for item in ordered for value in item.supporting_evidence_ids})),
        tuple(item.observation_at for item in ordered),
        tuple(item.available_at for item in ordered),
        tuple(item.ingested_at for item in ordered),
        COMPONENT_FRESHNESS_LIMIT_SECONDS,
        unavailable,
        tuple((item.component_type.value, item.state.value) for item in ordered),
        overall,
        contribution,
        TOTAL_MARKET_CONTEXT_POLICY_V1,
        TOTAL_MARKET_CONTEXT_POLICY_HASH,
        (TOTAL_MARKET_CONTEXT_WARNING,),
        limitations,
        manifest,
        "NONE",
        False,
        False,
        "NONE",
        "NONE",
        "NONE",
    )
    return TotalMarketContextSnapshot(
        *values[:-6],
        content_digest((TOTAL_MARKET_CONTEXT_SNAPSHOT_V1, *values)),
        *values[-6:],
    )


@dataclass(frozen=True, slots=True)
class ContextApplicability:
    component_type: ContextComponentType
    state: ApplicabilityState
    component_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class OpportunityContextAttachment:
    attachment_id: str
    version: int
    projection_id: str
    context_snapshot_id: str
    attached_at: datetime
    relevant_component_ids: tuple[str, ...]
    applicability: tuple[ContextApplicability, ...]
    contextual_observations: tuple[str, ...]
    limitations: tuple[str, ...]
    explanation: str
    policy_id: str
    canonical_hash: str
    authority: str = "NONE"
    opportunity_authority: str = "NONE"
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    order_authority: str = "NONE"
    financial_mutation_capability: str = "NONE"


_CRYPTO_RELEVANT = frozenset(
    {
        ContextComponentType.US_EQUITY_MARKET,
        ContextComponentType.TREASURY_RATE,
        ContextComponentType.MARKET_VOLATILITY,
        ContextComponentType.US_DOLLAR,
        ContextComponentType.MACRO_EVENT_PROXIMITY,
    }
)
_KALSHI_KEYWORDS = (
    (
        ("inflation", "cpi", "prices"),
        frozenset(
            {
                ContextComponentType.COMMODITY_OR_INFLATION,
                ContextComponentType.TREASURY_RATE,
                ContextComponentType.YIELD_CURVE,
                ContextComponentType.MACRO_EVENT_PROXIMITY,
            }
        ),
    ),
    (
        ("fed", "rate", "treasury", "gdp", "jobs", "unemployment", "economy"),
        frozenset(
            {
                ContextComponentType.TREASURY_RATE,
                ContextComponentType.YIELD_CURVE,
                ContextComponentType.MACRO_EVENT_PROXIMITY,
            }
        ),
    ),
    (
        ("stock", "s&p", "sp500", "nasdaq", "dow", "equity"),
        frozenset(
            {
                ContextComponentType.US_EQUITY_MARKET,
                ContextComponentType.MARKET_VOLATILITY,
                ContextComponentType.TREASURY_RATE,
            }
        ),
    ),
    (
        ("oil", "gold", "commodity"),
        frozenset(
            {
                ContextComponentType.COMMODITY_OR_INFLATION,
                ContextComponentType.US_DOLLAR,
            }
        ),
    ),
    (
        ("dollar", "currency", "foreign exchange", "fx"),
        frozenset({ContextComponentType.US_DOLLAR, ContextComponentType.TREASURY_RATE}),
    ),
)


def _relevant_types(projection: SharedOpportunityProjection) -> frozenset[ContextComponentType]:
    if projection.venue is SharedVenue.CRYPTO:
        return _CRYPTO_RELEVANT
    searchable = " ".join(
        (
            projection.instrument_id,
            projection.product_or_market_id,
            *(value for _, value in projection.source_details),
        )
    ).lower()
    relevant: set[ContextComponentType] = set()
    for keywords, component_types in _KALSHI_KEYWORDS:
        if any(keyword in searchable for keyword in keywords):
            relevant.update(component_types)
    return frozenset(relevant)


def attach_context(
    *,
    enriched_set_id: str,
    projection: SharedOpportunityProjection,
    snapshot: TotalMarketContextSnapshot,
    components: tuple[ContextComponent, ...],
    attached_at: datetime,
) -> OpportunityContextAttachment:
    attached = _time(attached_at, "TOTAL_MARKET_ATTACHMENT_TIME_INVALID")
    by_type = {item.component_type: item for item in components}
    unavailable = {item.component_type: item.reason for item in snapshot.unavailable_components}
    relevant_types = _relevant_types(projection)
    assessments: list[ContextApplicability] = []
    observations: list[str] = []
    for component_type in ContextComponentType:
        component = by_type.get(component_type)
        if component_type not in relevant_types:
            assessments.append(
                ContextApplicability(
                    component_type,
                    ApplicabilityState.IRRELEVANT,
                    None if component is None else component.component_id,
                    "NOT_RELEVANT_TO_RETAINED_OPPORTUNITY_SEMANTICS",
                )
            )
        elif component is None:
            assessments.append(
                ContextApplicability(
                    component_type,
                    ApplicabilityState.UNAVAILABLE,
                    None,
                    unavailable[component_type],
                )
            )
            observations.append(f"{component_type.value}=UNAVAILABLE")
        else:
            assessments.append(
                ContextApplicability(
                    component_type,
                    ApplicabilityState.RELEVANT,
                    component.component_id,
                    "RELEVANT_CONTEXT_ONLY_NO_DECISION_EFFECT",
                )
            )
            observations.append(f"{component_type.value}={component.state.value}")
    relevant_ids = tuple(
        item.component_id for item in components if item.component_type in relevant_types
    )
    relevant_summary = ", ".join(observations) if observations else "none for this contract"
    explanation = (
        f"Overall Total Market state {snapshot.overall_state.value}. "
        f"Relevant context: {relevant_summary}. This context does not cause, predict, "
        "reject, rank, or allocate the Opportunity."
    )
    limitations = (
        "SOURCE_OPPORTUNITY_EXPLANATION_UNCHANGED",
        "ELIGIBILITY_PRIORITY_AND_ALLOCATION_UNCHANGED",
        "CONTEXT_IS_NOT_CAUSAL_OR_PREDICTIVE",
    )
    attachment_id = deterministic_id(
        "opportunity_context_attachment",
        {
            "enriched_set_id": _identity(enriched_set_id, "TOTAL_MARKET_ENRICHED_SET_ID_INVALID"),
            "projection_id": projection.projection_id,
            "context_snapshot_id": snapshot.snapshot_id,
            "version": 1,
        },
    )
    values = (
        attachment_id,
        1,
        projection.projection_id,
        snapshot.snapshot_id,
        attached,
        relevant_ids,
        tuple(assessments),
        tuple(observations),
        limitations,
        explanation,
        TOTAL_MARKET_CONTEXT_POLICY_V1,
        "NONE",
        projection.authority,
        False,
        False,
        "NONE",
        "NONE",
        "NONE",
    )
    return OpportunityContextAttachment(
        *values[:-7],
        content_digest((TOTAL_MARKET_CONTEXT_ATTACHMENT_V1, *values)),
        *values[-7:],
    )


@dataclass(frozen=True, slots=True)
class ContextEnrichedOpportunitySet:
    enriched_set_id: str
    version: int
    original_opportunity_set_id: str
    original_opportunity_set_hash: str
    original_allocation_id: str | None
    original_allocation_hash: str | None
    context_snapshot_id: str
    attachment_ids: tuple[str, ...]
    attachment_manifest_hash: str
    eligibility_hash_before: str
    eligibility_hash_after: str
    priority_hash_before: str
    priority_hash_after: str
    allocation_hash_before: str | None
    allocation_hash_after: str | None
    eligibility_unchanged: bool
    priority_unchanged: bool
    allocation_unchanged: bool
    created_at: datetime
    limitations: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_eligible: bool = False
    capital_authority: str = "NONE"
    order_authority: str = "NONE"
    financial_mutation_capability: str = "NONE"


def build_context_enriched_set(
    *,
    enriched_set_id: str,
    original_opportunity_set_id: str,
    original_opportunity_set_hash: str,
    original_allocation_id: str | None,
    original_allocation_hash: str | None,
    context_snapshot_id: str,
    attachments: tuple[OpportunityContextAttachment, ...],
    eligibility_hash: str,
    priority_hash: str,
    created_at: datetime,
) -> ContextEnrichedOpportunitySet:
    created = _time(created_at, "TOTAL_MARKET_ENRICHED_SET_TIME_INVALID")
    ordered_attachments = tuple(sorted(attachments, key=lambda item: item.attachment_id))
    if len({item.attachment_id for item in ordered_attachments}) != len(ordered_attachments):
        raise TotalMarketContextError("TOTAL_MARKET_ATTACHMENT_ID_DUPLICATE")
    attachment_manifest_hash = content_digest(
        tuple((item.attachment_id, item.canonical_hash) for item in ordered_attachments)
    )
    values = (
        _identity(enriched_set_id, "TOTAL_MARKET_ENRICHED_SET_ID_INVALID"),
        1,
        original_opportunity_set_id,
        original_opportunity_set_hash,
        original_allocation_id,
        original_allocation_hash,
        context_snapshot_id,
        tuple(item.attachment_id for item in ordered_attachments),
        attachment_manifest_hash,
        eligibility_hash,
        eligibility_hash,
        priority_hash,
        priority_hash,
        original_allocation_hash,
        original_allocation_hash,
        True,
        True,
        True,
        created,
        (
            "ORIGINAL_SHARED_OPPORTUNITY_SET_IMMUTABLE",
            "ORIGINAL_ELIGIBILITY_PRIORITY_AND_ALLOCATION_UNCHANGED",
            "CONTEXT_IS_EXPLANATORY_ONLY",
        ),
        "NONE",
        False,
        False,
        "NONE",
        "NONE",
        "NONE",
    )
    return ContextEnrichedOpportunitySet(
        *values[:-6],
        content_digest((CONTEXT_ENRICHED_OPPORTUNITY_SET_V1, *values)),
        *values[-6:],
    )
