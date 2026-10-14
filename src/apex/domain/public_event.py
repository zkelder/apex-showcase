"""Provider-neutral public event evidence and shadow consistency checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.public_market import NormalizedPublicMarketSnapshot, PublicMarketProvider
from apex.domain.values import ContentHash, ValueValidationError, content_digest, parse_decimal

MAX_EVENT_MARKET_OBSERVATION_SKEW_SECONDS = 10


class EventEvidenceState(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    UNKNOWN = "UNKNOWN"


class EventRelationshipState(StrEnum):
    EXACT = "EXACT"
    RULE_MISMATCH = "RULE_MISMATCH"
    SETTLEMENT_SOURCE_MISMATCH = "SETTLEMENT_SOURCE_MISMATCH"
    CLOSE_TIME_MISMATCH = "CLOSE_TIME_MISMATCH"
    MULTIPLE_MISMATCHES = "MULTIPLE_MISMATCHES"


class PublicEventDataQualityState(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INVALID = "INVALID"


class ConsistencyEvaluationState(StrEnum):
    CONSISTENT = "CONSISTENT"
    CANDIDATE = "CANDIDATE"
    INELIGIBLE = "INELIGIBLE"
    INVALID = "INVALID"


class EventConsistencyReasonCode(StrEnum):
    EVENT_NOT_MUTUALLY_EXCLUSIVE = "EVENT_NOT_MUTUALLY_EXCLUSIVE"
    EXHAUSTIVENESS_UNKNOWN = "EXHAUSTIVENESS_UNKNOWN"
    EXHAUSTIVENESS_NOT_VERIFIED = "EXHAUSTIVENESS_NOT_VERIFIED"
    QUOTE_INCOMPLETE = "QUOTE_INCOMPLETE"
    RULE_MISMATCH = "RULE_MISMATCH"
    SETTLEMENT_SOURCE_MISMATCH = "SETTLEMENT_SOURCE_MISMATCH"
    SETTLEMENT_SOURCE_UNKNOWN = "SETTLEMENT_SOURCE_UNKNOWN"
    CLOSE_TIME_MISMATCH = "CLOSE_TIME_MISMATCH"
    OBSERVATION_TIME_MISMATCH = "OBSERVATION_TIME_MISMATCH"
    INVALID_PRICE = "INVALID_PRICE"
    NEGATIVE_SIZE = "NEGATIVE_SIZE"
    INSUFFICIENT_VISIBLE_LIQUIDITY = "INSUFFICIENT_VISIBLE_LIQUIDITY"
    EXECUTABLE_YES_ASK_SUM_BELOW_BOUND = "EXECUTABLE_YES_ASK_SUM_BELOW_BOUND"
    EXECUTABLE_YES_BID_SUM_ABOVE_BOUND = "EXECUTABLE_YES_BID_SUM_ABOVE_BOUND"
    CONSISTENCY_CANDIDATE = "CONSISTENCY_CANDIDATE"
    STRUCTURALLY_CONSISTENT = "STRUCTURALLY_CONSISTENT"
    PROVIDER_DATA_INVALID = "PROVIDER_DATA_INVALID"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _reason_codes(
    values: tuple[EventConsistencyReasonCode, ...],
) -> tuple[EventConsistencyReasonCode, ...]:
    return tuple(sorted(set(values), key=lambda value: value.value))


def _canonical_decimal(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    # PostgreSQL NUMERIC values commonly retain their declared scale.  Trim
    # insignificant zeros before using them in immutable canonical evidence.
    return parse_decimal(format(value.normalize(), "f"), allow_negative=True)


@dataclass(frozen=True, slots=True)
class EventSettlementSource:
    """Canonical provider text; URLs are retained as data, never fetched or executed."""

    name: str
    url: str

    def __post_init__(self) -> None:
        if not self.name or not self.url:
            raise ValueValidationError("event settlement source fields must not be empty")


@dataclass(frozen=True, slots=True)
class EventNestedMarketObservation:
    """Nested evidence used for exact ticker linking, never title matching."""

    provider_market_ticker: str | None
    provider_event_ticker: str | None
    ordinal: int
    rule_content_hash: ContentHash | None
    close_time: datetime | None
    settlement_source_hash: ContentHash | None
    reason_codes: tuple[EventConsistencyReasonCode, ...] = ()

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise ValueValidationError("nested event market ordinal must not be negative")
        if self.close_time is not None:
            object.__setattr__(self, "close_time", _aware(self.close_time, "close_time"))
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))


@dataclass(frozen=True, slots=True)
class NormalizedPublicEventSnapshot:
    """An immutable event occurrence and the provider's nested-market evidence."""

    snapshot_id: str
    raw_response_id: str
    provider: PublicMarketProvider
    provider_payload_ordinal: int
    observed_at: datetime
    provider_event_ticker: str | None
    series_ticker: str | None
    title: str | None
    subtitle: str | None
    category: str | None
    mutually_exclusive: bool | None
    settlement_sources: tuple[EventSettlementSource, ...]
    strike_date: datetime | None
    strike_period: str | None
    exhaustive_evidence: EventEvidenceState
    nested_markets: tuple[EventNestedMarketObservation, ...]
    data_quality_state: PublicEventDataQualityState
    normalization_reason_codes: tuple[EventConsistencyReasonCode, ...] = ()

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.raw_response_id:
            raise ValueValidationError("public event snapshot identities must not be empty")
        if self.provider_payload_ordinal < 0:
            raise ValueValidationError("public event payload ordinal must not be negative")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        if self.strike_date is not None:
            object.__setattr__(self, "strike_date", _aware(self.strike_date, "strike_date"))
        if tuple(sorted(self.nested_markets, key=lambda item: item.ordinal)) != self.nested_markets:
            raise ValueValidationError("nested event markets must preserve provider response order")
        object.__setattr__(
            self,
            "normalization_reason_codes",
            _reason_codes(self.normalization_reason_codes),
        )
        if (
            self.data_quality_state is PublicEventDataQualityState.ELIGIBLE
            and self.normalization_reason_codes
        ):
            raise ValueValidationError("eligible public event snapshot cannot carry reason codes")
        if (
            self.data_quality_state is PublicEventDataQualityState.INVALID
            and not self.normalization_reason_codes
        ):
            raise ValueValidationError("invalid public event snapshot requires a reason code")

    @property
    def settlement_source_hash(self) -> ContentHash:
        return ContentHash(content_digest(self.settlement_sources))

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class EventMarketRelationship:
    """One exact event snapshot to market snapshot relationship."""

    relationship_id: str
    event_snapshot_id: str
    market_snapshot_id: str
    provider: PublicMarketProvider
    provider_event_ticker: str
    provider_market_ticker: str
    relationship_ordinal: int
    market_rule_content_hash: ContentHash | None
    event_nested_rule_content_hash: ContentHash | None
    event_settlement_source_hash: ContentHash
    nested_settlement_source_hash: ContentHash | None
    event_nested_close_time: datetime | None
    relationship_state: EventRelationshipState

    def __post_init__(self) -> None:
        if not all(
            (
                self.relationship_id,
                self.event_snapshot_id,
                self.market_snapshot_id,
                self.provider_event_ticker,
                self.provider_market_ticker,
            )
        ):
            raise ValueValidationError("event-market relationship identities must not be empty")
        if self.relationship_ordinal < 0:
            raise ValueValidationError("event-market relationship ordinal must not be negative")
        if self.event_nested_close_time is not None:
            object.__setattr__(
                self,
                "event_nested_close_time",
                _aware(self.event_nested_close_time, "event_nested_close_time"),
            )

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class ConsistencyEvaluation:
    """A reproducible structural result, not a recommendation or trade signal."""

    evaluation_id: str
    event_snapshot_id: str
    evaluator_version: str
    evaluated_at: datetime
    mutually_exclusive_evidence: EventEvidenceState
    exhaustiveness_evidence: EventEvidenceState
    quote_coverage_count: int
    missing_quote_count: int
    yes_ask_sum: Decimal | None
    yes_bid_sum: Decimal | None
    no_ask_sum: Decimal | None
    no_bid_sum: Decimal | None
    minimum_visible_executable_size: Decimal | None
    settlement_source_compatibility: EventEvidenceState
    rule_compatibility: EventEvidenceState
    close_time_compatibility: EventEvidenceState
    state: ConsistencyEvaluationState
    reason_codes: tuple[EventConsistencyReasonCode, ...]
    canonical_input_hash: ContentHash

    def __post_init__(self) -> None:
        if not self.evaluation_id or not self.event_snapshot_id or not self.evaluator_version:
            raise ValueValidationError("consistency evaluation identities must not be empty")
        if self.quote_coverage_count < 0 or self.missing_quote_count < 0:
            raise ValueValidationError("consistency quote counts must not be negative")
        object.__setattr__(self, "evaluated_at", _aware(self.evaluated_at, "evaluated_at"))
        for name in (
            "yes_ask_sum",
            "yes_bid_sum",
            "no_ask_sum",
            "no_bid_sum",
            "minimum_visible_executable_size",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


def _relationship_state(
    event: NormalizedPublicEventSnapshot,
    nested: EventNestedMarketObservation,
    market: NormalizedPublicMarketSnapshot,
) -> EventRelationshipState:
    rule_mismatch = (
        nested.rule_content_hash is not None
        and market.rule_content_hash is not None
        and nested.rule_content_hash != market.rule_content_hash
    )
    source_mismatch = (
        nested.settlement_source_hash is not None
        and nested.settlement_source_hash != event.settlement_source_hash
    )
    close_mismatch = (
        nested.close_time is not None
        and market.close_time is not None
        and nested.close_time != market.close_time
    )
    if sum((rule_mismatch, source_mismatch, close_mismatch)) > 1:
        return EventRelationshipState.MULTIPLE_MISMATCHES
    if rule_mismatch:
        return EventRelationshipState.RULE_MISMATCH
    if source_mismatch:
        return EventRelationshipState.SETTLEMENT_SOURCE_MISMATCH
    if close_mismatch:
        return EventRelationshipState.CLOSE_TIME_MISMATCH
    return EventRelationshipState.EXACT


def link_event_markets(
    event: NormalizedPublicEventSnapshot,
    markets: tuple[NormalizedPublicMarketSnapshot, ...],
) -> tuple[EventMarketRelationship, ...]:
    """Link only exact matching provider/event/market tickers."""

    if event.provider_event_ticker is None:
        return ()

    market_by_ticker = {
        market.provider_market_ticker: market
        for market in markets
        if market.provider is event.provider
        and market.provider_market_ticker is not None
        and market.event_ticker == event.provider_event_ticker
    }
    values: list[EventMarketRelationship] = []
    for nested in event.nested_markets:
        if (
            nested.provider_market_ticker is None
            or nested.provider_event_ticker != event.provider_event_ticker
        ):
            continue
        market = market_by_ticker.get(nested.provider_market_ticker)
        if market is None:
            continue
        if market.provider_market_ticker is None:
            continue
        state = _relationship_state(event, nested, market)
        payload = {
            "event_snapshot_id": event.snapshot_id,
            "market_snapshot_id": market.snapshot_id,
            "provider_event_ticker": event.provider_event_ticker,
            "provider_market_ticker": market.provider_market_ticker,
            "relationship_ordinal": nested.ordinal,
            "state": state,
        }
        values.append(
            EventMarketRelationship(
                relationship_id=f"event_market_relationship_{content_digest(payload)[:24]}",
                event_snapshot_id=event.snapshot_id,
                market_snapshot_id=market.snapshot_id,
                provider=event.provider,
                provider_event_ticker=event.provider_event_ticker,
                provider_market_ticker=market.provider_market_ticker,
                relationship_ordinal=nested.ordinal,
                market_rule_content_hash=market.rule_content_hash,
                event_nested_rule_content_hash=nested.rule_content_hash,
                event_settlement_source_hash=event.settlement_source_hash,
                nested_settlement_source_hash=nested.settlement_source_hash,
                event_nested_close_time=nested.close_time,
                relationship_state=state,
            )
        )
    return tuple(values)


def _compatibility(
    relationships: tuple[EventMarketRelationship, ...],
    states: set[EventRelationshipState],
) -> EventEvidenceState:
    if not relationships:
        return EventEvidenceState.UNKNOWN
    if any(value.relationship_state in states for value in relationships):
        return EventEvidenceState.NOT_VERIFIED
    return EventEvidenceState.VERIFIED


def evaluate_event_consistency(
    event: NormalizedPublicEventSnapshot,
    *,
    relationships: tuple[EventMarketRelationship, ...],
    markets: tuple[NormalizedPublicMarketSnapshot, ...],
    evaluated_at: datetime,
    evaluator_version: str,
) -> ConsistencyEvaluation:
    """Evaluate exact quote sums only when outcome coverage is explicitly verified."""

    evaluated = _aware(evaluated_at, "evaluated_at")
    market_by_id = {market.snapshot_id: market for market in markets}
    reasons = list(event.normalization_reason_codes)
    mutual = (
        EventEvidenceState.VERIFIED
        if event.mutually_exclusive is True
        else EventEvidenceState.NOT_VERIFIED
    )
    if mutual is EventEvidenceState.NOT_VERIFIED:
        reasons.append(EventConsistencyReasonCode.EVENT_NOT_MUTUALLY_EXCLUSIVE)
    if event.exhaustive_evidence is EventEvidenceState.UNKNOWN:
        reasons.append(EventConsistencyReasonCode.EXHAUSTIVENESS_UNKNOWN)
    if event.exhaustive_evidence is EventEvidenceState.NOT_VERIFIED:
        reasons.append(EventConsistencyReasonCode.EXHAUSTIVENESS_NOT_VERIFIED)

    expected_count = len(event.nested_markets)
    complete: list[NormalizedPublicMarketSnapshot] = []
    missing_quote_count = (
        max(1, expected_count - len(relationships))
        if expected_count == 0
        else max(0, expected_count - len(relationships))
    )
    for relationship in relationships:
        market = market_by_id.get(relationship.market_snapshot_id)
        if market is None:
            missing_quote_count += 1
            continue
        quotes = (market.yes_bid, market.yes_ask, market.no_bid, market.no_ask)
        if any(value is None for value in quotes):
            missing_quote_count += 1
            continue
        if any(not Decimal(0) <= value <= Decimal(1) for value in quotes if value is not None):
            reasons.append(EventConsistencyReasonCode.INVALID_PRICE)
            continue
        if market.yes_bid_size is not None and market.yes_bid_size < 0:
            reasons.append(EventConsistencyReasonCode.NEGATIVE_SIZE)
            continue
        if market.yes_ask_size is None or market.yes_ask_size <= 0:
            reasons.append(EventConsistencyReasonCode.INSUFFICIENT_VISIBLE_LIQUIDITY)
            missing_quote_count += 1
            continue
        complete.append(market)

    if missing_quote_count > 0 or len(complete) != expected_count:
        reasons.append(EventConsistencyReasonCode.QUOTE_INCOMPLETE)
    if any(
        abs((market.observed_at - event.observed_at).total_seconds())
        > MAX_EVENT_MARKET_OBSERVATION_SKEW_SECONDS
        for market in complete
    ):
        reasons.append(EventConsistencyReasonCode.OBSERVATION_TIME_MISMATCH)

    source_compatibility = _compatibility(
        relationships,
        {
            EventRelationshipState.SETTLEMENT_SOURCE_MISMATCH,
            EventRelationshipState.MULTIPLE_MISMATCHES,
        },
    )
    rule_compatibility = _compatibility(
        relationships,
        {EventRelationshipState.RULE_MISMATCH, EventRelationshipState.MULTIPLE_MISMATCHES},
    )
    close_compatibility = _compatibility(
        relationships,
        {EventRelationshipState.CLOSE_TIME_MISMATCH, EventRelationshipState.MULTIPLE_MISMATCHES},
    )
    if source_compatibility is EventEvidenceState.NOT_VERIFIED:
        reasons.append(EventConsistencyReasonCode.SETTLEMENT_SOURCE_MISMATCH)
    elif any(value.nested_settlement_source_hash is None for value in relationships):
        source_compatibility = EventEvidenceState.UNKNOWN
        reasons.append(EventConsistencyReasonCode.SETTLEMENT_SOURCE_UNKNOWN)
    if rule_compatibility is EventEvidenceState.NOT_VERIFIED:
        reasons.append(EventConsistencyReasonCode.RULE_MISMATCH)
    elif any(
        value.market_rule_content_hash is None or value.event_nested_rule_content_hash is None
        for value in relationships
    ):
        rule_compatibility = EventEvidenceState.UNKNOWN
    if close_compatibility is EventEvidenceState.NOT_VERIFIED:
        reasons.append(EventConsistencyReasonCode.CLOSE_TIME_MISMATCH)
    elif any(value.event_nested_close_time is None for value in relationships):
        close_compatibility = EventEvidenceState.UNKNOWN

    complete_quote_set = expected_count > 0 and len(complete) == expected_count
    yes_ask_sum = (
        sum((market.yes_ask or Decimal(0) for market in complete), Decimal(0))
        if complete_quote_set
        else None
    )
    yes_bid_sum = (
        sum((market.yes_bid or Decimal(0) for market in complete), Decimal(0))
        if complete_quote_set
        else None
    )
    no_ask_sum = (
        sum((market.no_ask or Decimal(0) for market in complete), Decimal(0))
        if complete_quote_set
        else None
    )
    no_bid_sum = (
        sum((market.no_bid or Decimal(0) for market in complete), Decimal(0))
        if complete_quote_set
        else None
    )
    minimum_size = (
        min(value.yes_ask_size for value in complete if value.yes_ask_size is not None)
        if complete_quote_set
        else None
    )
    if yes_ask_sum is not None and yes_ask_sum < Decimal(1):
        reasons.append(EventConsistencyReasonCode.EXECUTABLE_YES_ASK_SUM_BELOW_BOUND)
    if yes_bid_sum is not None and yes_bid_sum > Decimal(1):
        reasons.append(EventConsistencyReasonCode.EXECUTABLE_YES_BID_SUM_ABOVE_BOUND)

    canonical_reasons = _reason_codes(tuple(reasons))
    invalid_reasons = {
        EventConsistencyReasonCode.RULE_MISMATCH,
        EventConsistencyReasonCode.SETTLEMENT_SOURCE_MISMATCH,
        EventConsistencyReasonCode.CLOSE_TIME_MISMATCH,
        EventConsistencyReasonCode.INVALID_PRICE,
        EventConsistencyReasonCode.NEGATIVE_SIZE,
        EventConsistencyReasonCode.PROVIDER_DATA_INVALID,
    }
    ineligible_reasons = {
        EventConsistencyReasonCode.EVENT_NOT_MUTUALLY_EXCLUSIVE,
        EventConsistencyReasonCode.EXHAUSTIVENESS_UNKNOWN,
        EventConsistencyReasonCode.EXHAUSTIVENESS_NOT_VERIFIED,
        EventConsistencyReasonCode.QUOTE_INCOMPLETE,
        EventConsistencyReasonCode.SETTLEMENT_SOURCE_UNKNOWN,
        EventConsistencyReasonCode.OBSERVATION_TIME_MISMATCH,
        EventConsistencyReasonCode.INSUFFICIENT_VISIBLE_LIQUIDITY,
    }
    if invalid_reasons.intersection(canonical_reasons):
        state = ConsistencyEvaluationState.INVALID
    elif ineligible_reasons.intersection(canonical_reasons):
        state = ConsistencyEvaluationState.INELIGIBLE
    elif yes_ask_sum is not None and yes_ask_sum < Decimal(1):
        state = ConsistencyEvaluationState.CANDIDATE
        canonical_reasons = _reason_codes(
            (*canonical_reasons, EventConsistencyReasonCode.CONSISTENCY_CANDIDATE)
        )
    else:
        state = ConsistencyEvaluationState.CONSISTENT
        canonical_reasons = _reason_codes(
            (*canonical_reasons, EventConsistencyReasonCode.STRUCTURALLY_CONSISTENT)
        )
    payload = {
        "event_snapshot_id": event.snapshot_id,
        "relationship_hashes": tuple(value.canonical_hash for value in relationships),
        # Snapshot identities are deterministic over immutable raw evidence and
        # normalized content.  Reusing them here also avoids PostgreSQL's
        # fixed numeric display scale changing a logically identical hash.
        "market_snapshot_ids": tuple(value.snapshot_id for value in markets),
        "evaluator_version": evaluator_version,
        "evaluated_at": evaluated,
    }
    input_hash = ContentHash(content_digest(payload))
    return ConsistencyEvaluation(
        evaluation_id=f"consistency_evaluation_{content_digest({**payload, 'state': state})[:24]}",
        event_snapshot_id=event.snapshot_id,
        evaluator_version=evaluator_version,
        evaluated_at=evaluated,
        mutually_exclusive_evidence=mutual,
        exhaustiveness_evidence=event.exhaustive_evidence,
        quote_coverage_count=len(complete),
        missing_quote_count=missing_quote_count,
        yes_ask_sum=yes_ask_sum,
        yes_bid_sum=yes_bid_sum,
        no_ask_sum=no_ask_sum,
        no_bid_sum=no_bid_sum,
        minimum_visible_executable_size=minimum_size,
        settlement_source_compatibility=source_compatibility,
        rule_compatibility=rule_compatibility,
        close_time_compatibility=close_compatibility,
        state=state,
        reason_codes=canonical_reasons,
        canonical_input_hash=input_hash,
    )


def rank_consistency_candidates(
    values: tuple[tuple[NormalizedPublicEventSnapshot, ConsistencyEvaluation], ...],
) -> tuple[tuple[NormalizedPublicEventSnapshot, ConsistencyEvaluation], ...]:
    """Rank shadow candidates transparently without calling them opportunities."""

    candidates = tuple(
        value for value in values if value[1].state is ConsistencyEvaluationState.CANDIDATE
    )
    return tuple(
        sorted(
            candidates,
            key=lambda value: (
                -value[1].quote_coverage_count,
                -(Decimal(1) - (value[1].yes_ask_sum or Decimal(1))),
                -(value[1].minimum_visible_executable_size or Decimal(0)),
                value[0].provider_event_ticker or "",
            ),
        )
    )
