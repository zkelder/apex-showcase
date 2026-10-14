"""Exact structured threshold-ladder evidence and conservative cross-market shadows.

This module has no provider, account, order, or execution authority.  It
accepts only already-retained structured market and event fields; titles,
subtitles, ticker syntax, and response ordering never establish a relation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from itertools import combinations

from apex.domain.public_complement import (
    FEE_MODEL_IDENTITY,
    ONE,
    ROUNDING_IDENTITY,
    OrderbookSide,
    PublicOrderbookLevel,
    PublicOrderbookSnapshot,
    SeriesFeeSnapshot,
    _conservative_per_contract_lower_bound,
    _fill_fee,
)
from apex.domain.public_market import PublicMarketProvider
from apex.domain.values import (
    ContentHash,
    ValueValidationError,
    content_digest,
    deterministic_id,
    parse_decimal,
)

THRESHOLD_EVALUATOR_VERSION = "public-threshold-ladder-v1"
THRESHOLD_RULE_FAMILY_IDENTITY = "kalshi-structured-numeric-threshold-v1"
KALSHI_RELATED_MARKET_CONSISTENCY_V1 = "KALSHI_RELATED_MARKET_CONSISTENCY_V1"


class ThresholdDirection(StrEnum):
    GREATER = "GREATER"
    LESS = "LESS"


class ThresholdRelationshipState(StrEnum):
    VERIFIED = "VERIFIED"
    UNSUPPORTED = "UNSUPPORTED"
    INVALID = "INVALID"


class ThresholdRelationshipReasonCode(StrEnum):
    EVENT_IDENTITY_MISMATCH = "EVENT_IDENTITY_MISMATCH"
    EVENT_MEMBERSHIP_MISSING = "EVENT_MEMBERSHIP_MISSING"
    SERIES_IDENTITY_MISMATCH = "SERIES_IDENTITY_MISMATCH"
    SETTLEMENT_SOURCE_MISSING = "SETTLEMENT_SOURCE_MISSING"
    SETTLEMENT_SOURCE_MISMATCH = "SETTLEMENT_SOURCE_MISMATCH"
    RULE_EVIDENCE_MISSING = "RULE_EVIDENCE_MISSING"
    RULE_FAMILY_MISMATCH = "RULE_FAMILY_MISMATCH"
    MARKET_TYPE_UNSUPPORTED = "MARKET_TYPE_UNSUPPORTED"
    STRIKE_TYPE_UNSUPPORTED = "STRIKE_TYPE_UNSUPPORTED"
    STRIKE_VALUE_MISSING = "STRIKE_VALUE_MISSING"
    STRIKE_METADATA_UNSUPPORTED = "STRIKE_METADATA_UNSUPPORTED"
    THRESHOLD_DIRECTION_MISMATCH = "THRESHOLD_DIRECTION_MISMATCH"
    THRESHOLD_ORDER_INVALID = "THRESHOLD_ORDER_INVALID"


class ThresholdEvaluationState(StrEnum):
    CANDIDATE = "CANDIDATE"
    NO_CANDIDATE = "NO_CANDIDATE"
    INELIGIBLE = "INELIGIBLE"
    INVALID = "INVALID"


class ThresholdEvaluationReasonCode(StrEnum):
    RELATIONSHIP_UNSUPPORTED = "RELATIONSHIP_UNSUPPORTED"
    RELATIONSHIP_INVALID = "RELATIONSHIP_INVALID"
    MARKET_NOT_ACTIVE = "MARKET_NOT_ACTIVE"
    ORDERBOOK_MISSING_SIDE = "ORDERBOOK_MISSING_SIDE"
    ORDERBOOK_LEVEL_INVALID = "ORDERBOOK_LEVEL_INVALID"
    OBSERVATION_SKEW_EXCEEDED = "OBSERVATION_SKEW_EXCEEDED"
    FEE_EVIDENCE_MISSING = "FEE_EVIDENCE_MISSING"
    FEE_MODEL_UNSUPPORTED = "FEE_MODEL_UNSUPPORTED"
    FEE_ROUNDING_UNSUPPORTED = "FEE_ROUNDING_UNSUPPORTED"
    INSUFFICIENT_MATCHED_SIZE = "INSUFFICIENT_MATCHED_SIZE"
    GROSS_LOWER_BOUND_NOT_POSITIVE = "GROSS_LOWER_BOUND_NOT_POSITIVE"
    FEES_ELIMINATE_LOWER_BOUND = "FEES_ELIMINATE_LOWER_BOUND"
    ROUNDING_ELIMINATES_LOWER_BOUND = "ROUNDING_ELIMINATES_LOWER_BOUND"
    POSITIVE_CONSERVATIVE_LOWER_BOUND = "POSITIVE_CONSERVATIVE_LOWER_BOUND"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_decimal(value: Decimal | int | str) -> Decimal:
    source: Decimal | int | str = (
        format(value.normalize(), "f") if isinstance(value, Decimal) else value
    )
    decimal = parse_decimal(source, allow_negative=True)
    return parse_decimal(format(decimal.normalize(), "f"), allow_negative=True)


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _decimal(value: object) -> Decimal | None:
    try:
        return parse_decimal(value)  # type: ignore[arg-type]
    except (TypeError, ValueValidationError):
        return None


def _relationship_reasons(
    values: tuple[ThresholdRelationshipReasonCode, ...],
) -> tuple[ThresholdRelationshipReasonCode, ...]:
    return tuple(sorted(set(values), key=lambda value: value.value))


def _evaluation_reasons(
    values: tuple[ThresholdEvaluationReasonCode, ...],
) -> tuple[ThresholdEvaluationReasonCode, ...]:
    return tuple(sorted(set(values), key=lambda value: value.value))


def _settlement_source_hash(event: Mapping[str, object]) -> ContentHash | None:
    raw_sources = event.get("settlement_sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        return None
    sources: list[tuple[str, str]] = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, Mapping):
            return None
        name = _string(raw_source.get("name"))
        url = _string(raw_source.get("url"))
        if name is None or url is None:
            return None
        sources.append((name, url))
    return ContentHash(content_digest(tuple(sorted(sources))))


@dataclass(frozen=True, slots=True)
class ThresholdMarketEvidence:
    """One exact refreshed market plus its exact event-membership evidence."""

    market_snapshot_id: str
    provider: PublicMarketProvider
    provider_market_ticker: str
    provider_event_ticker: str
    series_ticker: str
    market_is_active: bool
    direction: ThresholdDirection | None
    strike: Decimal | None
    structured_strike_identity: ContentHash | None
    rule_family_identity: ContentHash | None
    rule_content_hash: ContentHash | None
    settlement_source_hash: ContentHash | None
    reason_codes: tuple[ThresholdRelationshipReasonCode, ...]

    def __post_init__(self) -> None:
        if (
            not self.market_snapshot_id
            or self.provider is not PublicMarketProvider.KALSHI
            or not self.provider_market_ticker
            or not self.provider_event_ticker
            or not self.series_ticker
        ):
            raise ValueValidationError("threshold market evidence lacks exact identity")
        if self.strike is not None:
            object.__setattr__(self, "strike", _canonical_decimal(self.strike))
            if self.strike < 0:
                raise ValueValidationError("threshold strike must not be negative")
        object.__setattr__(self, "reason_codes", _relationship_reasons(self.reason_codes))

    @property
    def is_supported(self) -> bool:
        return (
            self.direction is not None
            and self.strike is not None
            and self.structured_strike_identity is not None
            and self.rule_family_identity is not None
            and self.rule_content_hash is not None
            and self.settlement_source_hash is not None
            and not self.reason_codes
        )

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class ThresholdMarketRelationship:
    relationship_id: str
    scan_id: str
    provider: PublicMarketProvider
    provider_event_ticker: str
    series_ticker: str
    broader_market_snapshot_id: str
    narrower_market_snapshot_id: str
    broader_market_ticker: str
    narrower_market_ticker: str
    direction: ThresholdDirection | None
    broader_strike: Decimal | None
    narrower_strike: Decimal | None
    structured_strike_identity: ContentHash | None
    rule_family_identity: ContentHash | None
    broader_rule_content_hash: ContentHash | None
    narrower_rule_content_hash: ContentHash | None
    settlement_source_hash: ContentHash | None
    state: ThresholdRelationshipState
    reason_codes: tuple[ThresholdRelationshipReasonCode, ...]
    canonical_input_hash: ContentHash

    def __post_init__(self) -> None:
        if (
            not self.relationship_id
            or not self.scan_id
            or self.provider is not PublicMarketProvider.KALSHI
            or not self.provider_event_ticker
            or not self.series_ticker
            or not self.broader_market_snapshot_id
            or not self.narrower_market_snapshot_id
            or not self.broader_market_ticker
            or not self.narrower_market_ticker
        ):
            raise ValueValidationError("threshold relationship lacks exact identity")
        for name in ("broader_strike", "narrower_strike"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _canonical_decimal(value))
        object.__setattr__(self, "reason_codes", _relationship_reasons(self.reason_codes))
        if self.state is ThresholdRelationshipState.VERIFIED:
            if self.reason_codes:
                raise ValueValidationError("verified threshold relationship has a reason")
            if (
                self.direction is None
                or self.broader_strike is None
                or self.narrower_strike is None
                or self.structured_strike_identity is None
                or self.rule_family_identity is None
                or self.broader_rule_content_hash is None
                or self.narrower_rule_content_hash is None
                or self.settlement_source_hash is None
            ):
                raise ValueValidationError(
                    "verified threshold relationship has incomplete evidence"
                )
            if self.direction is ThresholdDirection.GREATER and not (
                self.broader_strike < self.narrower_strike
            ):
                raise ValueValidationError("greater threshold relationship order is invalid")
            if self.direction is ThresholdDirection.LESS and not (
                self.broader_strike > self.narrower_strike
            ):
                raise ValueValidationError("less threshold relationship order is invalid")
        elif not self.reason_codes:
            raise ValueValidationError("non-verified threshold relationship needs a reason")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class ThresholdMatchedSlice:
    slice_id: str
    evaluation_id: str
    ordinal: int
    broader_yes_level_id: str
    narrower_no_level_id: str
    quantity: Decimal
    broader_yes_ask: Decimal
    narrower_no_ask: Decimal
    broader_cost: Decimal
    narrower_cost: Decimal
    broader_fee: Decimal
    narrower_fee: Decimal
    broader_rounding_reserve: Decimal
    narrower_rounding_reserve: Decimal
    settlement_floor_value: Decimal
    conservative_lower_bound: Decimal

    def __post_init__(self) -> None:
        if (
            not self.slice_id
            or not self.evaluation_id
            or not self.broader_yes_level_id
            or not self.narrower_no_level_id
            or self.ordinal < 0
        ):
            raise ValueValidationError("threshold slice identity is invalid")
        for name in (
            "quantity",
            "broader_yes_ask",
            "narrower_no_ask",
            "broader_cost",
            "narrower_cost",
            "broader_fee",
            "narrower_fee",
            "broader_rounding_reserve",
            "narrower_rounding_reserve",
            "settlement_floor_value",
            "conservative_lower_bound",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        if self.quantity <= 0 or not (
            Decimal(0) <= self.broader_yes_ask <= ONE and Decimal(0) <= self.narrower_no_ask <= ONE
        ):
            raise ValueValidationError("threshold slice price or quantity is invalid")
        if self.broader_cost != self.quantity * self.broader_yes_ask or self.narrower_cost != (
            self.quantity * self.narrower_no_ask
        ):
            raise ValueValidationError("threshold slice costs must be exact")
        if self.settlement_floor_value != self.quantity:
            raise ValueValidationError("threshold slice settlement floor must be one per pair")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class ThresholdBundleEvaluation:
    evaluation_id: str
    scan_id: str
    round_ordinal: int
    relationship_id: str
    broader_orderbook_snapshot_id: str | None
    narrower_orderbook_snapshot_id: str | None
    broader_fee_snapshot_id: str | None
    narrower_fee_snapshot_id: str | None
    broader_observed_at: datetime | None
    narrower_observed_at: datetime | None
    observation_skew: timedelta | None
    maximum_observation_skew: timedelta
    matched_quantity: Decimal
    broader_yes_weighted_ask: Decimal | None
    narrower_no_weighted_ask: Decimal | None
    broader_cost: Decimal
    narrower_cost: Decimal
    gross_acquisition_cost: Decimal
    settlement_floor_per_pair: Decimal
    gross_lower_bound_value: Decimal
    broader_modeled_fee: Decimal
    narrower_modeled_fee: Decimal
    total_modeled_fee: Decimal
    conservative_rounding_reserve: Decimal
    total_modeled_acquisition_cost: Decimal
    conservative_net_lower_bound: Decimal
    conservative_lower_bound_per_contract: Decimal | None
    state: ThresholdEvaluationState
    reason_codes: tuple[ThresholdEvaluationReasonCode, ...]
    canonical_configuration_hash: ContentHash
    canonical_input_hash: ContentHash

    def __post_init__(self) -> None:
        if (
            not self.evaluation_id
            or not self.scan_id
            or not self.relationship_id
            or self.round_ordinal < 0
        ):
            raise ValueValidationError("threshold evaluation identity is invalid")
        if self.maximum_observation_skew < timedelta(seconds=1):
            raise ValueValidationError("threshold observation skew must be positive")
        for name in ("broader_observed_at", "narrower_observed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        for name in (
            "matched_quantity",
            "broader_cost",
            "narrower_cost",
            "gross_acquisition_cost",
            "settlement_floor_per_pair",
            "gross_lower_bound_value",
            "broader_modeled_fee",
            "narrower_modeled_fee",
            "total_modeled_fee",
            "conservative_rounding_reserve",
            "total_modeled_acquisition_cost",
            "conservative_net_lower_bound",
        ):
            object.__setattr__(self, name, _canonical_decimal(getattr(self, name)))
        for name in (
            "broader_yes_weighted_ask",
            "narrower_no_weighted_ask",
            "conservative_lower_bound_per_contract",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _canonical_decimal(value))
        if self.matched_quantity < 0 or self.settlement_floor_per_pair != ONE:
            raise ValueValidationError("threshold evaluation quantity or floor is invalid")
        if self.gross_acquisition_cost != self.broader_cost + self.narrower_cost:
            raise ValueValidationError("threshold evaluation leg costs must sum")
        if self.total_modeled_fee != self.broader_modeled_fee + self.narrower_modeled_fee:
            raise ValueValidationError("threshold evaluation leg fees must sum")
        if self.total_modeled_acquisition_cost != (
            self.gross_acquisition_cost
            + self.total_modeled_fee
            + self.conservative_rounding_reserve
        ):
            raise ValueValidationError("threshold modeled acquisition cost is invalid")
        if self.gross_lower_bound_value != self.matched_quantity * ONE:
            raise ValueValidationError("threshold gross floor must be one per matched pair")
        if self.matched_quantity:
            expected = _conservative_per_contract_lower_bound(
                self.conservative_net_lower_bound, self.matched_quantity
            )
            if self.conservative_lower_bound_per_contract != expected:
                raise ValueValidationError("threshold lower bound per contract is not conservative")
        elif self.conservative_lower_bound_per_contract is not None:
            raise ValueValidationError(
                "zero threshold quantity cannot have a lower bound per contract"
            )
        object.__setattr__(self, "reason_codes", _evaluation_reasons(self.reason_codes))
        if self.state is ThresholdEvaluationState.CANDIDATE:
            if self.conservative_net_lower_bound <= 0 or self.reason_codes:
                raise ValueValidationError("threshold candidate needs a clean positive lower bound")
        elif not self.reason_codes:
            raise ValueValidationError("non-candidate threshold evaluation needs a reason")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(
            content_digest(
                {
                    "evaluation_id": self.evaluation_id,
                    "scan_id": self.scan_id,
                    "round_ordinal": self.round_ordinal,
                    "relationship_id": self.relationship_id,
                    "broader_orderbook_snapshot_id": self.broader_orderbook_snapshot_id,
                    "narrower_orderbook_snapshot_id": self.narrower_orderbook_snapshot_id,
                    "broader_fee_snapshot_id": self.broader_fee_snapshot_id,
                    "narrower_fee_snapshot_id": self.narrower_fee_snapshot_id,
                    "broader_observed_at": self.broader_observed_at,
                    "narrower_observed_at": self.narrower_observed_at,
                    "observation_skew_microseconds": (
                        int(self.observation_skew.total_seconds() * 1_000_000)
                        if self.observation_skew is not None
                        else None
                    ),
                    "maximum_observation_skew_microseconds": int(
                        self.maximum_observation_skew.total_seconds() * 1_000_000
                    ),
                    "matched_quantity": self.matched_quantity,
                    "broader_yes_weighted_ask": self.broader_yes_weighted_ask,
                    "narrower_no_weighted_ask": self.narrower_no_weighted_ask,
                    "broader_cost": self.broader_cost,
                    "narrower_cost": self.narrower_cost,
                    "gross_acquisition_cost": self.gross_acquisition_cost,
                    "settlement_floor_per_pair": self.settlement_floor_per_pair,
                    "gross_lower_bound_value": self.gross_lower_bound_value,
                    "broader_modeled_fee": self.broader_modeled_fee,
                    "narrower_modeled_fee": self.narrower_modeled_fee,
                    "total_modeled_fee": self.total_modeled_fee,
                    "conservative_rounding_reserve": self.conservative_rounding_reserve,
                    "total_modeled_acquisition_cost": self.total_modeled_acquisition_cost,
                    "conservative_net_lower_bound": self.conservative_net_lower_bound,
                    "conservative_lower_bound_per_contract": (
                        self.conservative_lower_bound_per_contract
                    ),
                    "state": self.state,
                    "reason_codes": self.reason_codes,
                    "canonical_configuration_hash": self.canonical_configuration_hash,
                    "canonical_input_hash": self.canonical_input_hash,
                }
            )
        )


def threshold_market_evidence(
    *,
    market_snapshot_id: str,
    provider_market_ticker: str,
    market_is_active: bool,
    rule_content_hash: ContentHash | None,
    market_document: Mapping[str, object],
    event_document: Mapping[str, object],
) -> ThresholdMarketEvidence:
    """Parse only official structured fields into fail-closed threshold evidence."""

    reasons: list[ThresholdRelationshipReasonCode] = []
    event_ticker = _string(event_document.get("event_ticker"))
    series_ticker = _string(event_document.get("series_ticker"))
    if event_ticker is None or market_document.get("event_ticker") != event_ticker:
        reasons.append(ThresholdRelationshipReasonCode.EVENT_IDENTITY_MISMATCH)
    # Kalshi's exact event is the documented authority for series lineage.  An
    # exact market response may omit that field; a present conflicting value is
    # still fail-closed evidence.
    if series_ticker is None or (
        market_document.get("series_ticker") is not None
        and market_document.get("series_ticker") != series_ticker
    ):
        reasons.append(ThresholdRelationshipReasonCode.SERIES_IDENTITY_MISMATCH)
    nested_markets = event_document.get("markets")
    nested = (
        next(
            (
                value
                for value in nested_markets
                if isinstance(value, Mapping) and value.get("ticker") == provider_market_ticker
            ),
            None,
        )
        if isinstance(nested_markets, list)
        else None
    )
    if (
        nested is None
        or nested.get("event_ticker") != event_ticker
        or (
            nested.get("series_ticker") is not None and nested.get("series_ticker") != series_ticker
        )
    ):
        reasons.append(ThresholdRelationshipReasonCode.EVENT_MEMBERSHIP_MISSING)
    nested_rule = nested.get("rules_primary") if isinstance(nested, Mapping) else None
    if (
        rule_content_hash is None
        or not isinstance(nested_rule, str)
        or ContentHash(sha256(nested_rule.encode("utf-8")).hexdigest()) != rule_content_hash
    ):
        reasons.append(ThresholdRelationshipReasonCode.RULE_EVIDENCE_MISSING)
    settlement_hash = _settlement_source_hash(event_document)
    if settlement_hash is None:
        reasons.append(ThresholdRelationshipReasonCode.SETTLEMENT_SOURCE_MISSING)

    market_type = _string(market_document.get("market_type"))
    if market_type != "binary":
        reasons.append(ThresholdRelationshipReasonCode.MARKET_TYPE_UNSUPPORTED)
    raw_strike_type = _string(market_document.get("strike_type"))
    direction = (
        ThresholdDirection.GREATER
        if raw_strike_type == "greater"
        else ThresholdDirection.LESS
        if raw_strike_type == "less"
        else None
    )
    if direction is None:
        reasons.append(ThresholdRelationshipReasonCode.STRIKE_TYPE_UNSUPPORTED)
    raw_strike = (
        market_document.get("floor_strike")
        if direction is ThresholdDirection.GREATER
        else market_document.get("cap_strike")
        if direction is ThresholdDirection.LESS
        else None
    )
    strike = _decimal(raw_strike)
    if strike is None:
        reasons.append(ThresholdRelationshipReasonCode.STRIKE_VALUE_MISSING)
    custom_strike = market_document.get("custom_strike")
    mve_collection = market_document.get("mve_collection_ticker")
    mve_legs = market_document.get("mve_selected_legs")
    if (
        (isinstance(custom_strike, Mapping) and custom_strike)
        or (custom_strike not in (None, {}))
        or mve_collection is not None
        or (isinstance(mve_legs, list) and mve_legs)
    ):
        reasons.append(ThresholdRelationshipReasonCode.STRIKE_METADATA_UNSUPPORTED)

    structured_identity = (
        ContentHash(
            content_digest(
                {
                    "market_type": market_type,
                    "strike_type": raw_strike_type,
                    "functional_strike": market_document.get("functional_strike"),
                    "custom_strike": custom_strike,
                    "mve_collection_ticker": mve_collection,
                    "mve_selected_legs": mve_legs,
                }
            )
        )
        if direction is not None and strike is not None and not reasons
        else None
    )
    rule_family = (
        ContentHash(
            content_digest(
                {
                    "identity": THRESHOLD_RULE_FAMILY_IDENTITY,
                    "market_type": market_type,
                    "strike_type": raw_strike_type,
                    "functional_strike": market_document.get("functional_strike"),
                    "response_price_units": market_document.get("response_price_units"),
                    "price_level_structure": market_document.get("price_level_structure"),
                    "settlement_source_hash": settlement_hash,
                }
            )
        )
        if direction is not None and settlement_hash is not None and rule_content_hash is not None
        else None
    )
    return ThresholdMarketEvidence(
        market_snapshot_id=market_snapshot_id,
        provider=PublicMarketProvider.KALSHI,
        provider_market_ticker=provider_market_ticker,
        provider_event_ticker=event_ticker or "MISSING",
        series_ticker=series_ticker or "MISSING",
        market_is_active=market_is_active,
        direction=direction,
        strike=strike,
        structured_strike_identity=structured_identity,
        rule_family_identity=rule_family,
        rule_content_hash=rule_content_hash,
        settlement_source_hash=settlement_hash,
        reason_codes=tuple(reasons),
    )


def _relationship(
    *, scan_id: str, left: ThresholdMarketEvidence, right: ThresholdMarketEvidence
) -> ThresholdMarketRelationship:
    reasons: list[ThresholdRelationshipReasonCode] = []
    if left.provider_event_ticker != right.provider_event_ticker:
        reasons.append(ThresholdRelationshipReasonCode.EVENT_IDENTITY_MISMATCH)
    if left.series_ticker != right.series_ticker:
        reasons.append(ThresholdRelationshipReasonCode.SERIES_IDENTITY_MISMATCH)
    if left.settlement_source_hash != right.settlement_source_hash:
        reasons.append(ThresholdRelationshipReasonCode.SETTLEMENT_SOURCE_MISMATCH)
    if left.rule_family_identity != right.rule_family_identity:
        reasons.append(ThresholdRelationshipReasonCode.RULE_FAMILY_MISMATCH)
    reasons.extend(left.reason_codes)
    reasons.extend(right.reason_codes)
    if left.direction is None or right.direction is None:
        reasons.append(ThresholdRelationshipReasonCode.STRIKE_TYPE_UNSUPPORTED)
    elif left.direction is not right.direction:
        reasons.append(ThresholdRelationshipReasonCode.THRESHOLD_DIRECTION_MISMATCH)
    if left.strike is None or right.strike is None or left.strike == right.strike:
        reasons.append(ThresholdRelationshipReasonCode.THRESHOLD_ORDER_INVALID)

    direction = left.direction if left.direction is right.direction else None
    if (
        direction is ThresholdDirection.GREATER
        and left.strike is not None
        and right.strike is not None
    ):
        broader, narrower = (left, right) if left.strike < right.strike else (right, left)
    elif (
        direction is ThresholdDirection.LESS
        and left.strike is not None
        and right.strike is not None
    ):
        broader, narrower = (left, right) if left.strike > right.strike else (right, left)
    else:
        broader, narrower = tuple(
            sorted((left, right), key=lambda value: value.provider_market_ticker)
        )

    clean_reasons = _relationship_reasons(tuple(reasons))
    state = (
        ThresholdRelationshipState.VERIFIED
        if not clean_reasons
        else ThresholdRelationshipState.UNSUPPORTED
        if any(
            reason
            in {
                ThresholdRelationshipReasonCode.MARKET_TYPE_UNSUPPORTED,
                ThresholdRelationshipReasonCode.STRIKE_TYPE_UNSUPPORTED,
                ThresholdRelationshipReasonCode.STRIKE_VALUE_MISSING,
                ThresholdRelationshipReasonCode.STRIKE_METADATA_UNSUPPORTED,
                ThresholdRelationshipReasonCode.THRESHOLD_DIRECTION_MISMATCH,
            }
            for reason in clean_reasons
        )
        else ThresholdRelationshipState.INVALID
    )
    payload = {
        "scan_id": scan_id,
        "left": left.canonical_hash,
        "right": right.canonical_hash,
        "state": state,
        "reasons": clean_reasons,
    }
    return ThresholdMarketRelationship(
        relationship_id=deterministic_id("threshold_market_relationship", payload),
        scan_id=scan_id,
        provider=PublicMarketProvider.KALSHI,
        provider_event_ticker=broader.provider_event_ticker,
        series_ticker=broader.series_ticker,
        broader_market_snapshot_id=broader.market_snapshot_id,
        narrower_market_snapshot_id=narrower.market_snapshot_id,
        broader_market_ticker=broader.provider_market_ticker,
        narrower_market_ticker=narrower.provider_market_ticker,
        direction=direction,
        broader_strike=broader.strike,
        narrower_strike=narrower.strike,
        structured_strike_identity=(
            broader.structured_strike_identity
            if broader.structured_strike_identity == narrower.structured_strike_identity
            else None
        ),
        rule_family_identity=(
            broader.rule_family_identity
            if broader.rule_family_identity == narrower.rule_family_identity
            else None
        ),
        broader_rule_content_hash=broader.rule_content_hash,
        narrower_rule_content_hash=narrower.rule_content_hash,
        settlement_source_hash=(
            broader.settlement_source_hash
            if broader.settlement_source_hash == narrower.settlement_source_hash
            else None
        ),
        state=state,
        reason_codes=clean_reasons,
        canonical_input_hash=ContentHash(content_digest(payload)),
    )


def build_threshold_relationships(
    *, scan_id: str, markets: tuple[ThresholdMarketEvidence, ...]
) -> tuple[ThresholdMarketRelationship, ...]:
    """Build every exact pair; unsupported evidence remains explicit and immutable."""

    values = tuple(sorted(markets, key=lambda value: value.provider_market_ticker))
    return tuple(
        _relationship(scan_id=scan_id, left=left, right=right)
        for left, right in combinations(values, 2)
    )


def _ask_levels(
    levels: tuple[PublicOrderbookLevel, ...], *, bid_side: OrderbookSide
) -> tuple[PublicOrderbookLevel, ...]:
    values = tuple(value for value in levels if value.side is bid_side)
    if any(
        values[index].bid_price >= values[index + 1].bid_price for index in range(len(values) - 1)
    ):
        raise ValueValidationError("orderbook bid levels must be strictly ascending")
    return tuple(reversed(values))


def _non_positive_reason(
    *, gross_lower_bound: Decimal, modeled_fees: Decimal, rounding_reserve: Decimal
) -> ThresholdEvaluationReasonCode:
    if gross_lower_bound <= 0:
        return ThresholdEvaluationReasonCode.GROSS_LOWER_BOUND_NOT_POSITIVE
    if gross_lower_bound - modeled_fees <= 0:
        return ThresholdEvaluationReasonCode.FEES_ELIMINATE_LOWER_BOUND
    return ThresholdEvaluationReasonCode.ROUNDING_ELIMINATES_LOWER_BOUND


def evaluate_threshold_bundle(
    *,
    scan_id: str,
    round_ordinal: int,
    relationship: ThresholdMarketRelationship,
    broader_orderbook: PublicOrderbookSnapshot | None,
    narrower_orderbook: PublicOrderbookSnapshot | None,
    broader_levels: tuple[PublicOrderbookLevel, ...],
    narrower_levels: tuple[PublicOrderbookLevel, ...],
    broader_fee: SeriesFeeSnapshot | None,
    narrower_fee: SeriesFeeSnapshot | None,
    maximum_bundle_quantity: Decimal,
    maximum_observation_skew: timedelta,
    canonical_configuration_hash: ContentHash,
    evaluated_at: datetime,
) -> tuple[ThresholdBundleEvaluation, tuple[ThresholdMatchedSlice, ...]]:
    """Match broader YES with narrower NO at a one-dollar settlement floor only."""

    if round_ordinal < 0:
        raise ValueValidationError("threshold round ordinal must not be negative")
    maximum = parse_decimal(maximum_bundle_quantity, allow_zero=False)
    if maximum_observation_skew < timedelta(seconds=1):
        raise ValueValidationError("threshold maximum observation skew is invalid")
    reasons: list[ThresholdEvaluationReasonCode] = []
    if relationship.state is ThresholdRelationshipState.UNSUPPORTED:
        reasons.append(ThresholdEvaluationReasonCode.RELATIONSHIP_UNSUPPORTED)
    elif relationship.state is not ThresholdRelationshipState.VERIFIED:
        reasons.append(ThresholdEvaluationReasonCode.RELATIONSHIP_INVALID)
    if broader_orderbook is None or narrower_orderbook is None:
        reasons.append(ThresholdEvaluationReasonCode.ORDERBOOK_MISSING_SIDE)
    if broader_fee is None or narrower_fee is None:
        reasons.append(ThresholdEvaluationReasonCode.FEE_EVIDENCE_MISSING)
    for fee in (broader_fee, narrower_fee):
        if fee is not None and (
            not fee.is_supported or fee.fee_model_identity != FEE_MODEL_IDENTITY
        ):
            reasons.append(ThresholdEvaluationReasonCode.FEE_MODEL_UNSUPPORTED)
        if fee is not None and fee.rounding_identity != ROUNDING_IDENTITY:
            reasons.append(ThresholdEvaluationReasonCode.FEE_ROUNDING_UNSUPPORTED)
    if broader_orderbook is not None and broader_orderbook.data_quality_state != "VALID":
        reasons.append(ThresholdEvaluationReasonCode.ORDERBOOK_LEVEL_INVALID)
    if narrower_orderbook is not None and narrower_orderbook.data_quality_state != "VALID":
        reasons.append(ThresholdEvaluationReasonCode.ORDERBOOK_LEVEL_INVALID)
    broader_observed_at = broader_orderbook.observed_at if broader_orderbook else None
    narrower_observed_at = narrower_orderbook.observed_at if narrower_orderbook else None
    skew = (
        abs(broader_observed_at - narrower_observed_at)
        if broader_observed_at is not None and narrower_observed_at is not None
        else None
    )
    if skew is not None and skew > maximum_observation_skew:
        reasons.append(ThresholdEvaluationReasonCode.OBSERVATION_SKEW_EXCEEDED)
    try:
        broader_yes_asks = _ask_levels(broader_levels, bid_side=OrderbookSide.NO_BID)
        narrower_no_asks = _ask_levels(narrower_levels, bid_side=OrderbookSide.YES_BID)
    except ValueValidationError:
        broader_yes_asks = ()
        narrower_no_asks = ()
        reasons.append(ThresholdEvaluationReasonCode.ORDERBOOK_LEVEL_INVALID)
    if not broader_yes_asks or not narrower_no_asks:
        reasons.append(ThresholdEvaluationReasonCode.ORDERBOOK_MISSING_SIDE)

    _aware(evaluated_at, "evaluated_at")
    input_payload = {
        "scan_id": scan_id,
        "round_ordinal": round_ordinal,
        "relationship_hash": relationship.canonical_hash,
        "broader_orderbook_hash": broader_orderbook.canonical_hash if broader_orderbook else None,
        "narrower_orderbook_hash": narrower_orderbook.canonical_hash
        if narrower_orderbook
        else None,
        "broader_fee_hash": broader_fee.canonical_hash if broader_fee else None,
        "narrower_fee_hash": narrower_fee.canonical_hash if narrower_fee else None,
        "maximum_bundle_quantity": maximum,
        "maximum_observation_skew_microseconds": int(
            maximum_observation_skew.total_seconds() * 1_000_000
        ),
        "configuration_hash": canonical_configuration_hash,
    }
    evaluation_id = deterministic_id("threshold_bundle_evaluation", input_payload)
    slices: list[ThresholdMatchedSlice] = []
    remaining = maximum
    broader_index = narrower_index = 0
    broader_used = narrower_used = Decimal(0)
    while (
        not reasons
        and remaining > 0
        and broader_index < len(broader_yes_asks)
        and narrower_index < len(narrower_no_asks)
    ):
        broader_level = broader_yes_asks[broader_index]
        narrower_level = narrower_no_asks[narrower_index]
        quantity = min(
            remaining,
            broader_level.quantity - broader_used,
            narrower_level.quantity - narrower_used,
        )
        if quantity <= 0:
            raise ValueValidationError("threshold matching produced a non-positive slice")
        assert broader_fee is not None and broader_fee.fee_multiplier is not None
        assert narrower_fee is not None and narrower_fee.fee_multiplier is not None
        broader_fee_value, broader_rounding = _fill_fee(
            quantity=quantity,
            ask_price=broader_level.derived_ask_price,
            multiplier=broader_fee.fee_multiplier,
        )
        narrower_fee_value, narrower_rounding = _fill_fee(
            quantity=quantity,
            ask_price=narrower_level.derived_ask_price,
            multiplier=narrower_fee.fee_multiplier,
        )
        broader_cost = quantity * broader_level.derived_ask_price
        narrower_cost = quantity * narrower_level.derived_ask_price
        conservative_cost = (
            broader_cost
            + narrower_cost
            + broader_fee_value
            + narrower_fee_value
            + broader_rounding
            + narrower_rounding
        )
        if quantity - conservative_cost <= 0:
            if not slices:
                reasons.append(
                    _non_positive_reason(
                        gross_lower_bound=quantity - broader_cost - narrower_cost,
                        modeled_fees=broader_fee_value + narrower_fee_value,
                        rounding_reserve=broader_rounding + narrower_rounding,
                    )
                )
            break
        slice_payload = {
            "evaluation_id": evaluation_id,
            "ordinal": len(slices),
            "broader_level_id": broader_level.level_id,
            "narrower_level_id": narrower_level.level_id,
            "quantity": quantity,
        }
        slices.append(
            ThresholdMatchedSlice(
                slice_id=deterministic_id("threshold_matched_slice", slice_payload),
                evaluation_id=evaluation_id,
                ordinal=len(slices),
                broader_yes_level_id=broader_level.level_id,
                narrower_no_level_id=narrower_level.level_id,
                quantity=quantity,
                broader_yes_ask=broader_level.derived_ask_price,
                narrower_no_ask=narrower_level.derived_ask_price,
                broader_cost=broader_cost,
                narrower_cost=narrower_cost,
                broader_fee=broader_fee_value,
                narrower_fee=narrower_fee_value,
                broader_rounding_reserve=broader_rounding,
                narrower_rounding_reserve=narrower_rounding,
                settlement_floor_value=quantity,
                conservative_lower_bound=quantity - conservative_cost,
            )
        )
        remaining -= quantity
        broader_used += quantity
        narrower_used += quantity
        if broader_used == broader_level.quantity:
            broader_index += 1
            broader_used = Decimal(0)
        if narrower_used == narrower_level.quantity:
            narrower_index += 1
            narrower_used = Decimal(0)

    matched = maximum - remaining
    if not reasons and matched == 0:
        reasons.append(ThresholdEvaluationReasonCode.INSUFFICIENT_MATCHED_SIZE)
    broader_cost = sum((value.broader_cost for value in slices), Decimal(0))
    narrower_cost = sum((value.narrower_cost for value in slices), Decimal(0))
    broader_modeled_fee = sum((value.broader_fee for value in slices), Decimal(0))
    narrower_modeled_fee = sum((value.narrower_fee for value in slices), Decimal(0))
    rounding_reserve = sum(
        (value.broader_rounding_reserve + value.narrower_rounding_reserve for value in slices),
        Decimal(0),
    )
    gross_cost = broader_cost + narrower_cost
    gross_value = matched * ONE
    total_fee = broader_modeled_fee + narrower_modeled_fee
    total_cost = gross_cost + total_fee + rounding_reserve
    lower_bound = gross_value - total_cost
    if not reasons and lower_bound <= 0:
        reasons.append(
            _non_positive_reason(
                gross_lower_bound=gross_value - gross_cost,
                modeled_fees=total_fee,
                rounding_reserve=rounding_reserve,
            )
        )
    invalid = ThresholdEvaluationReasonCode.ORDERBOOK_LEVEL_INVALID in reasons
    ineligible_reasons = {
        ThresholdEvaluationReasonCode.RELATIONSHIP_UNSUPPORTED,
        ThresholdEvaluationReasonCode.RELATIONSHIP_INVALID,
        ThresholdEvaluationReasonCode.MARKET_NOT_ACTIVE,
        ThresholdEvaluationReasonCode.ORDERBOOK_MISSING_SIDE,
        ThresholdEvaluationReasonCode.OBSERVATION_SKEW_EXCEEDED,
        ThresholdEvaluationReasonCode.FEE_EVIDENCE_MISSING,
        ThresholdEvaluationReasonCode.FEE_MODEL_UNSUPPORTED,
        ThresholdEvaluationReasonCode.FEE_ROUNDING_UNSUPPORTED,
    }
    state = (
        ThresholdEvaluationState.INVALID
        if invalid
        else ThresholdEvaluationState.INELIGIBLE
        if set(reasons).intersection(ineligible_reasons)
        else ThresholdEvaluationState.NO_CANDIDATE
        if reasons
        else ThresholdEvaluationState.CANDIDATE
    )
    return (
        ThresholdBundleEvaluation(
            evaluation_id=evaluation_id,
            scan_id=scan_id,
            round_ordinal=round_ordinal,
            relationship_id=relationship.relationship_id,
            broader_orderbook_snapshot_id=(
                broader_orderbook.orderbook_snapshot_id if broader_orderbook else None
            ),
            narrower_orderbook_snapshot_id=(
                narrower_orderbook.orderbook_snapshot_id if narrower_orderbook else None
            ),
            broader_fee_snapshot_id=broader_fee.fee_snapshot_id if broader_fee else None,
            narrower_fee_snapshot_id=narrower_fee.fee_snapshot_id if narrower_fee else None,
            broader_observed_at=broader_observed_at,
            narrower_observed_at=narrower_observed_at,
            observation_skew=skew,
            maximum_observation_skew=maximum_observation_skew,
            matched_quantity=matched,
            broader_yes_weighted_ask=(broader_cost / matched if matched else None),
            narrower_no_weighted_ask=(narrower_cost / matched if matched else None),
            broader_cost=broader_cost,
            narrower_cost=narrower_cost,
            gross_acquisition_cost=gross_cost,
            settlement_floor_per_pair=ONE,
            gross_lower_bound_value=gross_value,
            broader_modeled_fee=broader_modeled_fee,
            narrower_modeled_fee=narrower_modeled_fee,
            total_modeled_fee=total_fee,
            conservative_rounding_reserve=rounding_reserve,
            total_modeled_acquisition_cost=total_cost,
            conservative_net_lower_bound=lower_bound,
            conservative_lower_bound_per_contract=(
                _conservative_per_contract_lower_bound(lower_bound, matched) if matched else None
            ),
            state=state,
            reason_codes=tuple(reasons),
            canonical_configuration_hash=canonical_configuration_hash,
            canonical_input_hash=ContentHash(content_digest(input_payload)),
        ),
        tuple(slices),
    )
