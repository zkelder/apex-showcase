"""Point-in-time market facts and contract rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256

from apex.domain.values import (
    ContentHash,
    InformationCutoff,
    Price,
    ProbabilityVector,
    Quantity,
    ValueValidationError,
    content_digest,
    deterministic_id,
    parse_decimal,
)


class SettlementOutcome(StrEnum):
    YES = "YES"
    NO = "NO"
    VOID = "VOID"


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source_id: str
    source_event_at: datetime
    available_at: datetime
    sequence: int | None
    schema_version: str
    content_hash: ContentHash

    def __post_init__(self) -> None:
        event_at = _require_aware(self.source_event_at, "source_event_at")
        available_at = _require_aware(self.available_at, "available_at")
        if event_at > available_at:
            raise ValueValidationError("source event cannot occur after it becomes available")
        if self.sequence is not None and (isinstance(self.sequence, bool) or self.sequence <= 0):
            raise ValueValidationError("source sequence must be a positive integer")
        object.__setattr__(self, "source_event_at", event_at)
        object.__setattr__(self, "available_at", available_at)


@dataclass(frozen=True, slots=True)
class ContractRuleVersion:
    rule_version_id: str
    exact_text: str
    resolution_source: str
    effective_at: datetime
    close_at: datetime
    determination_at: datetime
    expected_settlement_at: datetime
    content_hash: ContentHash

    def __post_init__(self) -> None:
        effective_at = _require_aware(self.effective_at, "effective_at")
        close_at = _require_aware(self.close_at, "close_at")
        determination_at = _require_aware(self.determination_at, "determination_at")
        expected_settlement_at = _require_aware(
            self.expected_settlement_at, "expected_settlement_at"
        )
        if not effective_at <= close_at <= determination_at <= expected_settlement_at:
            raise ValueValidationError("contract lifecycle timestamps are inconsistent")
        if not self.exact_text or not self.resolution_source:
            raise ValueValidationError("contract rule text and source must be preserved")
        object.__setattr__(self, "effective_at", effective_at)
        object.__setattr__(self, "close_at", close_at)
        object.__setattr__(self, "determination_at", determination_at)
        object.__setattr__(self, "expected_settlement_at", expected_settlement_at)


def void_settlement_term_content_hash(
    rule_content_hash: ContentHash,
    outcome_classification: SettlementOutcome,
    settlement_value_text: str,
) -> ContentHash:
    """Bind one explicit VOID term to the exact hashed rule text."""

    return ContentHash(
        content_digest(
            {
                "rule_content_hash": rule_content_hash,
                "outcome_classification": outcome_classification,
                "settlement_value": settlement_value_text,
            }
        )
    )


@dataclass(frozen=True, slots=True)
class VoidSettlementRuleTerm:
    outcome_classification: SettlementOutcome
    settlement_value: Decimal
    settlement_value_text: str
    content_hash: ContentHash

    def __post_init__(self) -> None:
        if self.outcome_classification is not SettlementOutcome.VOID:
            raise ValueValidationError("VOID rule term requires VOID classification")
        value = parse_decimal(self.settlement_value, allow_zero=False)
        if value > Decimal(1):
            raise ValueValidationError("VOID rule term value must be between zero and one")
        if parse_decimal(self.settlement_value_text, allow_zero=False) != value:
            raise ValueValidationError("VOID rule term text does not match its value")
        object.__setattr__(self, "settlement_value", value)


@dataclass(frozen=True, slots=True)
class VoidContractRuleVersion(ContractRuleVersion):
    void_settlement_term: VoidSettlementRuleTerm

    def __post_init__(self) -> None:
        ContractRuleVersion.__post_init__(self)
        if sha256(self.exact_text.encode("utf-8")).hexdigest() != self.content_hash.value:
            raise ValueValidationError("VOID rule content hash does not match its exact text")
        term = self.void_settlement_term
        expected_fragment = (
            f"{term.settlement_value_text} if it is {term.outcome_classification.value}"
        )
        if self.exact_text.count(expected_fragment) != 1:
            raise ValueValidationError("exact rule text does not authorize its VOID term")
        expected_term_hash = void_settlement_term_content_hash(
            self.content_hash,
            term.outcome_classification,
            term.settlement_value_text,
        )
        if term.content_hash != expected_term_hash:
            raise ValueValidationError("VOID rule term hash does not match the exact rule")


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    category: str
    payout_unit: Decimal
    price_tick: Decimal
    quantity_increment: Decimal
    yes_settlement_value: Decimal
    no_settlement_value: Decimal
    void_settlement_value: Decimal

    def __post_init__(self) -> None:
        payout = parse_decimal(self.payout_unit, allow_zero=False)
        tick = parse_decimal(self.price_tick, allow_zero=False)
        increment = parse_decimal(self.quantity_increment, allow_zero=False)
        yes_value = parse_decimal(self.yes_settlement_value)
        no_value = parse_decimal(self.no_settlement_value)
        void_value = parse_decimal(self.void_settlement_value)
        if self.category.upper() == "SPORTS":
            raise ValueValidationError("sports instruments are prohibited")
        if any(value > payout for value in (yes_value, no_value, void_value)):
            raise ValueValidationError("settlement value exceeds the bounded payout")
        object.__setattr__(self, "payout_unit", payout)
        object.__setattr__(self, "price_tick", tick)
        object.__setattr__(self, "quantity_increment", increment)
        object.__setattr__(self, "yes_settlement_value", yes_value)
        object.__setattr__(self, "no_settlement_value", no_value)
        object.__setattr__(self, "void_settlement_value", void_value)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    yes_ask: Price
    no_ask: Price
    visible_yes_quantity: Quantity

    @property
    def spread(self) -> Decimal:
        return self.yes_ask.value + self.no_ask.value - Decimal(1)


@dataclass(frozen=True, slots=True)
class InformationSetMember:
    source_id: str
    available_at: datetime
    sequence: int | None
    content_hash: ContentHash


@dataclass(frozen=True, slots=True)
class InformationSet:
    information_set_id: str
    instrument: Instrument
    cutoff: InformationCutoff
    ordered_members: tuple[InformationSetMember, ...]
    rule: ContractRuleVersion
    market: MarketSnapshot
    valid: bool
    content_hash: ContentHash


class InformationSetBuilder:
    @staticmethod
    def build(
        records: tuple[SourceRecord, ...],
        instrument: Instrument,
        cutoff: InformationCutoff,
        rule: ContractRuleVersion,
        market: MarketSnapshot,
    ) -> InformationSet:
        eligible = tuple(record for record in records if record.available_at <= cutoff.value)
        if not eligible:
            raise ValueValidationError("information set has no point-in-time eligible records")
        ordered = tuple(
            sorted(
                eligible,
                key=lambda record: (
                    record.sequence if record.sequence is not None else 2**63,
                    record.source_id,
                ),
            )
        )
        sequences = tuple(record.sequence for record in ordered if record.sequence is not None)
        if sequences:
            expected = tuple(range(sequences[0], sequences[0] + len(sequences)))
            if sequences != expected:
                raise ValueValidationError("eligible source records contain a sequence gap")
        if not rule.effective_at <= cutoff.value < rule.close_at:
            raise ValueValidationError("contract rule is not active at the information cutoff")
        members = tuple(
            InformationSetMember(
                source_id=record.source_id,
                available_at=record.available_at,
                sequence=record.sequence,
                content_hash=record.content_hash,
            )
            for record in ordered
        )
        payload = {
            "instrument_id": instrument.instrument_id,
            "cutoff": cutoff,
            "members": members,
            "rule_version_id": rule.rule_version_id,
            "market": market,
        }
        digest = content_digest(payload)
        return InformationSet(
            information_set_id=deterministic_id("information_set", payload),
            instrument=instrument,
            cutoff=cutoff,
            ordered_members=members,
            rule=rule,
            market=market,
            valid=True,
            content_hash=ContentHash(digest),
        )


@dataclass(frozen=True, slots=True)
class ReferenceEstimate:
    estimate_id: str
    source_id: str
    probability_vector: ProbabilityVector
    published_at: datetime
    expires_at: datetime
    method_version: str
    information_set_id: str

    def __post_init__(self) -> None:
        if not self.estimate_id or not self.source_id or not self.information_set_id:
            raise ValueValidationError("reference estimate identifiers must not be empty")
        if not self.method_version:
            raise ValueValidationError("reference estimate method version must not be empty")
        published = _require_aware(self.published_at, "published_at")
        expires = _require_aware(self.expires_at, "expires_at")
        if published >= expires:
            raise ValueValidationError("reference estimate must expire after publication")
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "expires_at", expires)
