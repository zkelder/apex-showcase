"""Provider-neutral, read-only public prediction-market observations.

This module deliberately models only captured market evidence and deterministic
data-quality checks.  It has no provider, HTTP, campaign, financial, or
execution dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from apex.domain.values import ContentHash, ValueValidationError, content_digest


class PublicMarketProvider(StrEnum):
    """The provider provenance recorded for this bounded capability."""

    KALSHI = "KALSHI"


class PublicMarketDataQualityState(StrEnum):
    """A structural research-readiness result, never a trading decision."""

    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    INVALID = "INVALID"


class PublicMarketReasonCode(StrEnum):
    """Stable machine-readable reasons for a shadow market evaluation."""

    MARKET_NOT_OPEN = "MARKET_NOT_OPEN"
    RULE_TEXT_MISSING = "RULE_TEXT_MISSING"
    CLOSE_TIME_NOT_AFTER_OBSERVATION = "CLOSE_TIME_NOT_AFTER_OBSERVATION"
    YES_PRICE_INVALID = "YES_PRICE_INVALID"
    NO_PRICE_INVALID = "NO_PRICE_INVALID"
    QUOTE_INCOMPLETE = "QUOTE_INCOMPLETE"
    BID_EXCEEDS_ASK = "BID_EXCEEDS_ASK"
    BINARY_BID_CONSISTENCY_INVALID = "BINARY_BID_CONSISTENCY_INVALID"
    BINARY_ASK_CONSISTENCY_INVALID = "BINARY_ASK_CONSISTENCY_INVALID"
    NEGATIVE_SIZE = "NEGATIVE_SIZE"
    UNSUPPORTED_MARKET_STRUCTURE = "UNSUPPORTED_MARKET_STRUCTURE"
    PROVIDER_DATA_INVALID = "PROVIDER_DATA_INVALID"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def parse_public_market_timestamp(value: str) -> datetime:
    """Parse a generic RFC 3339 UTC provider timestamp without losing precision."""

    if not isinstance(value, str) or not value.endswith("Z") or value != value.strip():
        raise ValueValidationError("public market timestamp must be an RFC 3339 UTC string")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueValidationError("public market timestamp is malformed") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueValidationError("public market timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _canonical_reasons(
    reasons: tuple[PublicMarketReasonCode, ...],
) -> tuple[PublicMarketReasonCode, ...]:
    return tuple(sorted(set(reasons), key=lambda value: value.value))


@dataclass(frozen=True, slots=True)
class NormalizedPublicMarketSnapshot:
    """One provider-neutral market occurrence with immutable raw-page lineage."""

    snapshot_id: str
    raw_response_id: str
    provider: PublicMarketProvider
    provider_payload_ordinal: int
    observed_at: datetime
    provider_market_ticker: str | None
    event_ticker: str | None
    title: str | None
    subtitle: str | None
    status: str | None
    open_time: datetime | None
    close_time: datetime | None
    latest_expiration_time: datetime | None
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    yes_bid_size: Decimal | None
    yes_ask_size: Decimal | None
    volume: Decimal | None
    volume_24h: Decimal | None
    open_interest: Decimal | None
    liquidity: Decimal | None
    rules_primary: str | None
    rules_secondary: str | None
    rule_content_hash: ContentHash | None
    normalization_reason_codes: tuple[PublicMarketReasonCode, ...] = ()

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.raw_response_id:
            raise ValueValidationError("public market snapshot identities must not be empty")
        if self.provider_payload_ordinal < 0:
            raise ValueValidationError("public market payload ordinal must not be negative")
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        for name in ("open_time", "close_time", "latest_expiration_time"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        object.__setattr__(
            self,
            "normalization_reason_codes",
            _canonical_reasons(self.normalization_reason_codes),
        )

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


@dataclass(frozen=True, slots=True)
class ShadowMarketEvaluation:
    """Pure structural evaluation; it intentionally has no pricing or signal output."""

    evaluation_id: str
    snapshot_id: str
    evaluator_version: str
    evaluated_at: datetime
    state: PublicMarketDataQualityState
    reason_codes: tuple[PublicMarketReasonCode, ...]
    yes_spread: Decimal | None
    no_spread: Decimal | None
    yes_midpoint: Decimal | None
    no_midpoint: Decimal | None

    def __post_init__(self) -> None:
        if not self.evaluation_id or not self.snapshot_id or not self.evaluator_version:
            raise ValueValidationError("shadow evaluation identities must not be empty")
        object.__setattr__(self, "evaluated_at", _aware(self.evaluated_at, "evaluated_at"))
        object.__setattr__(self, "reason_codes", _canonical_reasons(self.reason_codes))
        if self.state is PublicMarketDataQualityState.ELIGIBLE and self.reason_codes:
            raise ValueValidationError("eligible shadow evaluation cannot carry a reason code")
        if self.state is not PublicMarketDataQualityState.ELIGIBLE and not self.reason_codes:
            raise ValueValidationError("non-eligible shadow evaluation requires a reason code")

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self))


def _quote_pair(
    bid: Decimal | None,
    ask: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    if bid is None or ask is None:
        return None, None
    return ask - bid, (ask + bid) / Decimal(2)


def evaluate_public_market_snapshot(
    snapshot: NormalizedPublicMarketSnapshot,
    *,
    evaluated_at: datetime,
    evaluator_version: str,
) -> ShadowMarketEvaluation:
    """Evaluate quote/rule structure deterministically without generating a trade."""

    observed_at = _aware(evaluated_at, "evaluated_at")
    reasons = list(snapshot.normalization_reason_codes)
    if snapshot.status != "open":
        reasons.append(PublicMarketReasonCode.MARKET_NOT_OPEN)
    if not snapshot.rules_primary:
        reasons.append(PublicMarketReasonCode.RULE_TEXT_MISSING)
    if snapshot.close_time is None or snapshot.close_time <= snapshot.observed_at:
        reasons.append(PublicMarketReasonCode.CLOSE_TIME_NOT_AFTER_OBSERVATION)

    yes_quotes = (snapshot.yes_bid, snapshot.yes_ask)
    no_quotes = (snapshot.no_bid, snapshot.no_ask)

    for value in yes_quotes:
        if value is not None and not Decimal(0) <= value <= Decimal(1):
            reasons.append(PublicMarketReasonCode.YES_PRICE_INVALID)
    for value in no_quotes:
        if value is not None and not Decimal(0) <= value <= Decimal(1):
            reasons.append(PublicMarketReasonCode.NO_PRICE_INVALID)
    for value in (snapshot.yes_bid_size, snapshot.yes_ask_size):
        if value is not None and value < 0:
            reasons.append(PublicMarketReasonCode.NEGATIVE_SIZE)

    if (
        snapshot.yes_bid is not None
        and snapshot.yes_ask is not None
        and snapshot.yes_bid > snapshot.yes_ask
    ):
        reasons.append(PublicMarketReasonCode.BID_EXCEEDS_ASK)
    if (
        snapshot.no_bid is not None
        and snapshot.no_ask is not None
        and snapshot.no_bid > snapshot.no_ask
    ):
        reasons.append(PublicMarketReasonCode.BID_EXCEEDS_ASK)
    if (
        snapshot.yes_bid is not None
        and snapshot.no_ask is not None
        and snapshot.yes_bid + snapshot.no_ask != Decimal(1)
    ):
        reasons.append(PublicMarketReasonCode.BINARY_BID_CONSISTENCY_INVALID)
    if (
        snapshot.yes_ask is not None
        and snapshot.no_bid is not None
        and snapshot.yes_ask + snapshot.no_bid != Decimal(1)
    ):
        reasons.append(PublicMarketReasonCode.BINARY_ASK_CONSISTENCY_INVALID)

    canonical_reasons = _canonical_reasons(tuple(reasons))
    invalid_codes = {
        PublicMarketReasonCode.YES_PRICE_INVALID,
        PublicMarketReasonCode.NO_PRICE_INVALID,
        PublicMarketReasonCode.BID_EXCEEDS_ASK,
        PublicMarketReasonCode.BINARY_BID_CONSISTENCY_INVALID,
        PublicMarketReasonCode.BINARY_ASK_CONSISTENCY_INVALID,
        PublicMarketReasonCode.NEGATIVE_SIZE,
        PublicMarketReasonCode.UNSUPPORTED_MARKET_STRUCTURE,
        PublicMarketReasonCode.PROVIDER_DATA_INVALID,
    }
    state = (
        PublicMarketDataQualityState.INVALID
        if invalid_codes.intersection(canonical_reasons)
        else (
            PublicMarketDataQualityState.INELIGIBLE
            if canonical_reasons
            else PublicMarketDataQualityState.ELIGIBLE
        )
    )
    yes_spread, yes_midpoint = _quote_pair(snapshot.yes_bid, snapshot.yes_ask)
    no_spread, no_midpoint = _quote_pair(snapshot.no_bid, snapshot.no_ask)
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "evaluator_version": evaluator_version,
        "evaluated_at": observed_at,
        "state": state,
        "reason_codes": canonical_reasons,
        "yes_spread": yes_spread,
        "no_spread": no_spread,
        "yes_midpoint": yes_midpoint,
        "no_midpoint": no_midpoint,
    }
    return ShadowMarketEvaluation(
        evaluation_id=f"shadow_market_evaluation_{content_digest(payload)[:24]}",
        snapshot_id=snapshot.snapshot_id,
        evaluator_version=evaluator_version,
        evaluated_at=observed_at,
        state=state,
        reason_codes=canonical_reasons,
        yes_spread=yes_spread,
        no_spread=no_spread,
        yes_midpoint=yes_midpoint,
        no_midpoint=no_midpoint,
    )
