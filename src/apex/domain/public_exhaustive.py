"""Model-free complete-event-set consistency over retained public Kalshi evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from apex.domain.public_complement import SeriesFeeSnapshot, model_public_taker_fee
from apex.domain.public_event import EventEvidenceState, EventRelationshipState
from apex.domain.values import ValueValidationError, content_digest, parse_decimal

KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1 = "KALSHI_EXHAUSTIVE_EVENT_SET_CONSISTENCY_V1"
EXHAUSTIVE_EVENT_SET_EVALUATOR_V1 = "kalshi-exhaustive-event-set-consistency-v1"
MAXIMUM_EXHAUSTIVE_EVENT_SETS = 100
MAXIMUM_EXHAUSTIVE_EVENT_LEGS = 50
MAXIMUM_EXHAUSTIVE_PACKAGE_QUANTITY = Decimal("10")
EXHAUSTIVE_EVIDENCE_FRESHNESS_SECONDS = 300


def _aware(value: datetime, reason: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(reason)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ExhaustiveEventSetLegInput:
    market_snapshot_id: str
    market_ticker: str
    event_ticker: str
    relationship_id: str
    relationship_state: EventRelationshipState
    rule_content_hash: str | None
    rules_primary: str | None
    rules_secondary: str | None
    market_status: str | None
    close_at: datetime | None
    settlement_at: datetime | None
    orderbook_snapshot_id: str | None
    orderbook_hash: str | None
    orderbook_observed_at: datetime | None
    orderbook_quality: str | None
    yes_ask: Decimal | None
    visible_yes_quantity: Decimal
    fee: SeriesFeeSnapshot | None


@dataclass(frozen=True, slots=True)
class ExhaustiveEventSetInput:
    scan_id: str
    event_ticker: str
    event_snapshot_ids: tuple[str, ...]
    event_snapshot_hashes: tuple[str, ...]
    mutually_exclusive_values: tuple[bool | None, ...]
    exhaustiveness_values: tuple[EventEvidenceState, ...]
    nested_market_counts: tuple[int, ...]
    settlement_source_hashes: tuple[str, ...]
    legs: tuple[ExhaustiveEventSetLegInput, ...]


@dataclass(frozen=True, slots=True)
class ExhaustiveEventSetLeg:
    market_snapshot_id: str
    market_ticker: str
    relationship_id: str
    orderbook_snapshot_id: str | None
    fee_snapshot_id: str | None
    rule_content_hash: str | None
    yes_ask: Decimal | None
    visible_quantity: Decimal
    matched_quantity: Decimal
    acquisition_cost: Decimal
    modeled_fee: Decimal
    rounding_reserve: Decimal
    close_at: datetime | None
    settlement_at: datetime | None
    evidence_ids: tuple[str, ...]
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class ExhaustiveEventSetEvaluation:
    evaluation_id: str
    scan_id: str
    event_ticker: str
    evaluator_version: str
    evaluated_at: datetime
    event_snapshot_ids: tuple[str, ...]
    legs: tuple[ExhaustiveEventSetLeg, ...]
    mutually_exclusive_verified: bool
    exhaustiveness_verified: bool
    rules_verified: bool
    timing_verified: bool
    matched_quantity: Decimal
    gross_package_cost: Decimal
    modeled_fees: Decimal
    rounding_reserve: Decimal
    total_modeled_cost: Decimal
    guaranteed_settlement_payout: Decimal
    conservative_net_gap: Decimal
    required_capital: Decimal
    maximum_loss: Decimal
    maximum_payoff: Decimal
    close_at: datetime | None
    settlement_at: datetime | None
    expires_at: datetime | None
    source_eligible: bool
    rejection_reasons: tuple[str, ...]
    evidence_lineage: tuple[str, ...]
    canonical_hash: str
    authority: str = "NONE"
    paper_eligible: bool = False
    live_authority: str = "NONE"


def _ambiguous_rule(leg: ExhaustiveEventSetLegInput) -> bool:
    text = " ".join(value or "" for value in (leg.rules_primary, leg.rules_secondary)).casefold()
    return any(
        term in text
        for term in (
            "none of the above",
            "all other",
            "other outcome",
            "conditional on",
            "market will be void",
            "market is void",
            "market may be void",
            "market will be canceled",
            "market may be canceled",
        )
    )


def evaluate_exhaustive_event_set(
    value: ExhaustiveEventSetInput,
    *,
    evaluated_at: datetime,
    maximum_quantity: Decimal = MAXIMUM_EXHAUSTIVE_PACKAGE_QUANTITY,
) -> ExhaustiveEventSetEvaluation:
    """Evaluate one complete YES set without probabilities or execution authority."""

    evaluated = _aware(evaluated_at, "exhaustive event-set evaluation time must be aware")
    maximum = parse_decimal(maximum_quantity, allow_zero=False)
    if not value.scan_id or not value.event_ticker or not value.event_snapshot_ids:
        raise ValueValidationError("exhaustive event-set identity is incomplete")
    if not 1 <= len(value.legs) <= MAXIMUM_EXHAUSTIVE_EVENT_LEGS:
        raise ValueValidationError("exhaustive event-set leg count is outside its bound")
    if len({leg.market_ticker for leg in value.legs}) != len(value.legs):
        raise ValueValidationError("exhaustive event-set market identity is duplicated")

    reasons: list[str] = []
    mutual = bool(value.mutually_exclusive_values) and all(
        item is True for item in value.mutually_exclusive_values
    )
    exhaustive = bool(value.exhaustiveness_values) and all(
        item is EventEvidenceState.VERIFIED for item in value.exhaustiveness_values
    )
    if not mutual:
        reasons.append("MUTUAL_EXCLUSIVITY_NOT_VERIFIED")
    if not exhaustive:
        reasons.append("COLLECTIVE_EXHAUSTIVENESS_NOT_VERIFIED")
    if len(set(value.nested_market_counts)) != 1 or not value.nested_market_counts:
        reasons.append("EVENT_NESTED_MARKET_COUNT_CONFLICT")
    elif value.nested_market_counts[0] != len(value.legs):
        reasons.append("EVENT_SET_COVERAGE_INCOMPLETE")
    if len(set(value.settlement_source_hashes)) != 1 or not all(value.settlement_source_hashes):
        reasons.append("SETTLEMENT_SOURCE_INCOMPATIBLE_OR_MISSING")

    rules_verified = True
    for leg in value.legs:
        if leg.event_ticker != value.event_ticker:
            reasons.append("EVENT_IDENTITY_MISMATCH")
            rules_verified = False
        if leg.relationship_state is not EventRelationshipState.EXACT:
            reasons.append(f"RELATIONSHIP_{leg.relationship_state.value}")
            rules_verified = False
        if leg.rule_content_hash is None:
            reasons.append("RULE_EVIDENCE_MISSING")
            rules_verified = False
        if _ambiguous_rule(leg):
            reasons.append("AMBIGUOUS_VOID_CANCELLATION_CONDITIONAL_OR_OTHER_RULE")
            rules_verified = False
        if leg.market_status != "open":
            reasons.append("MARKET_NOT_OPEN")

    close_values = tuple(leg.close_at for leg in value.legs if leg.close_at is not None)
    settlement_values = tuple(
        leg.settlement_at for leg in value.legs if leg.settlement_at is not None
    )
    timing_verified = (
        len(close_values) == len(value.legs)
        and len(set(close_values)) == 1
        and len(settlement_values) == len(value.legs)
        and len(set(settlement_values)) == 1
        and settlement_values[0] >= close_values[0]
    )
    if not timing_verified:
        reasons.append("CLOSE_OR_SETTLEMENT_TIMING_INCOMPATIBLE")
    close_at = close_values[0] if timing_verified else None
    settlement_at = settlement_values[0] if timing_verified else None
    if close_at is not None and evaluated >= close_at:
        reasons.append("EVENT_SET_EXPIRED")

    quantities: list[Decimal] = []
    for leg in value.legs:
        if (
            leg.orderbook_snapshot_id is None
            or leg.orderbook_hash is None
            or leg.orderbook_quality != "VALID"
            or leg.yes_ask is None
            or leg.visible_yes_quantity <= 0
        ):
            reasons.append("ORDERBOOK_YES_SIDE_MISSING_OR_INVALID")
            continue
        if not Decimal(0) < leg.yes_ask < Decimal(1):
            reasons.append("YES_ASK_OUTSIDE_BINARY_RANGE")
            continue
        if leg.orderbook_observed_at is None or abs(
            evaluated - _aware(leg.orderbook_observed_at, "orderbook time must be aware")
        ) > timedelta(seconds=EXHAUSTIVE_EVIDENCE_FRESHNESS_SECONDS):
            reasons.append("ORDERBOOK_EVIDENCE_STALE")
            continue
        if leg.fee is None:
            reasons.append("FEE_EVIDENCE_MISSING")
            continue
        if not leg.fee.is_supported:
            reasons.append("FEE_MODEL_UNSUPPORTED")
            continue
        if abs(evaluated - leg.fee.observed_at) > timedelta(minutes=10):
            reasons.append("FEE_EVIDENCE_STALE")
            continue
        quantities.append(parse_decimal(leg.visible_yes_quantity, allow_zero=False))
    matched = min((maximum, *quantities)) if len(quantities) == len(value.legs) else Decimal(0)
    if matched <= 0:
        reasons.append("NO_MATCHED_VISIBLE_PACKAGE_QUANTITY")

    output_legs: list[ExhaustiveEventSetLeg] = []
    for leg in sorted(value.legs, key=lambda item: item.market_ticker):
        ask = None if leg.yes_ask is None else parse_decimal(leg.yes_ask)
        acquisition = Decimal(0) if ask is None else matched * ask
        modeled_fee = Decimal(0)
        rounding = Decimal(0)
        if matched > 0 and ask is not None and leg.fee is not None and leg.fee.is_supported:
            assert leg.fee.fee_multiplier is not None
            modeled_fee, rounding = model_public_taker_fee(
                quantity=matched,
                ask_price=ask,
                multiplier=leg.fee.fee_multiplier,
            )
        evidence_ids = tuple(
            item
            for item in (
                leg.market_snapshot_id,
                leg.relationship_id,
                leg.orderbook_snapshot_id,
                None if leg.fee is None else leg.fee.fee_snapshot_id,
                leg.rule_content_hash,
                leg.orderbook_hash,
            )
            if item
        )
        leg_payload = (
            leg.market_snapshot_id,
            leg.market_ticker,
            leg.relationship_id,
            leg.orderbook_snapshot_id,
            None if leg.fee is None else leg.fee.fee_snapshot_id,
            leg.rule_content_hash,
            ask,
            leg.visible_yes_quantity,
            matched,
            acquisition,
            modeled_fee,
            rounding,
            leg.close_at,
            leg.settlement_at,
            evidence_ids,
        )
        output_legs.append(
            ExhaustiveEventSetLeg(
                market_snapshot_id=leg.market_snapshot_id,
                market_ticker=leg.market_ticker,
                relationship_id=leg.relationship_id,
                orderbook_snapshot_id=leg.orderbook_snapshot_id,
                fee_snapshot_id=None if leg.fee is None else leg.fee.fee_snapshot_id,
                rule_content_hash=leg.rule_content_hash,
                yes_ask=ask,
                visible_quantity=leg.visible_yes_quantity,
                matched_quantity=matched,
                acquisition_cost=acquisition,
                modeled_fee=modeled_fee,
                rounding_reserve=rounding,
                close_at=leg.close_at,
                settlement_at=leg.settlement_at,
                evidence_ids=evidence_ids,
                canonical_hash=content_digest(leg_payload),
            )
        )
    gross_cost = sum((leg.acquisition_cost for leg in output_legs), Decimal(0))
    modeled_fees = sum((leg.modeled_fee for leg in output_legs), Decimal(0))
    rounding_reserve = sum((leg.rounding_reserve for leg in output_legs), Decimal(0))
    total_cost = gross_cost + modeled_fees + rounding_reserve
    payout = matched
    net_gap = payout - total_cost
    if matched > 0 and net_gap <= 0:
        reasons.append("CONSERVATIVE_NET_GAP_NOT_POSITIVE")
    canonical_reasons = tuple(sorted(set(reasons)))
    eligible = not canonical_reasons
    lineage = tuple(
        dict.fromkeys(
            (
                value.scan_id,
                *value.event_snapshot_ids,
                *value.event_snapshot_hashes,
                *(item for leg in output_legs for item in leg.evidence_ids),
            )
        )
    )
    identity_payload = (
        value.scan_id,
        value.event_ticker,
        value.event_snapshot_ids,
        tuple(leg.canonical_hash for leg in output_legs),
        evaluated,
        EXHAUSTIVE_EVENT_SET_EVALUATOR_V1,
    )
    evaluation_id = f"exhaustive_event_set_{content_digest(identity_payload)[:24]}"
    payload = (
        evaluation_id,
        identity_payload,
        mutual,
        exhaustive,
        rules_verified,
        timing_verified,
        matched,
        gross_cost,
        modeled_fees,
        rounding_reserve,
        total_cost,
        payout,
        net_gap,
        close_at,
        settlement_at,
        eligible,
        canonical_reasons,
        lineage,
        "NONE",
        False,
        "NONE",
    )
    return ExhaustiveEventSetEvaluation(
        evaluation_id=evaluation_id,
        scan_id=value.scan_id,
        event_ticker=value.event_ticker,
        evaluator_version=EXHAUSTIVE_EVENT_SET_EVALUATOR_V1,
        evaluated_at=evaluated,
        event_snapshot_ids=value.event_snapshot_ids,
        legs=tuple(output_legs),
        mutually_exclusive_verified=mutual,
        exhaustiveness_verified=exhaustive,
        rules_verified=rules_verified,
        timing_verified=timing_verified,
        matched_quantity=matched,
        gross_package_cost=gross_cost,
        modeled_fees=modeled_fees,
        rounding_reserve=rounding_reserve,
        total_modeled_cost=total_cost,
        guaranteed_settlement_payout=payout,
        conservative_net_gap=net_gap,
        required_capital=total_cost,
        maximum_loss=total_cost,
        maximum_payoff=payout,
        close_at=close_at,
        settlement_at=settlement_at,
        expires_at=close_at,
        source_eligible=eligible,
        rejection_reasons=canonical_reasons,
        evidence_lineage=lineage,
        canonical_hash=content_digest(payload),
    )
