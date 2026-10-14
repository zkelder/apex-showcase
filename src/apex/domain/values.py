"""Authoritative immutable values and deterministic canonicalization."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256

MAX_PRECISION = 18
MAX_SCALE = 8

type DecimalInput = str | int | Decimal
_DECIMAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ValueValidationError(ValueError):
    """An authoritative value violated the declared representation policy."""


def parse_decimal(
    value: DecimalInput,
    *,
    allow_negative: bool = False,
    allow_zero: bool = True,
) -> Decimal:
    """Parse an authoritative fixed-point decimal without binary floating point."""

    if isinstance(value, (bool, float)):
        raise ValueValidationError("bool and float values are not authoritative decimals")
    if isinstance(value, str):
        if _DECIMAL_PATTERN.fullmatch(value) is None:
            raise ValueValidationError("decimal strings must use canonical fixed-point notation")
        source = value
    elif isinstance(value, int):
        source = str(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueValidationError("authoritative decimals must be finite")
        source = format(value, "f")
    else:
        raise ValueValidationError(
            f"unsupported authoritative decimal type: {type(value).__name__}"
        )

    try:
        parsed = Decimal(source)
    except InvalidOperation as error:
        raise ValueValidationError("malformed decimal") from error
    if not parsed.is_finite():
        raise ValueValidationError("authoritative decimals must be finite")
    if parsed < 0 and not allow_negative:
        raise ValueValidationError("negative values are not permitted")
    if parsed == 0 and not allow_zero:
        raise ValueValidationError("zero is not permitted")

    plain = format(abs(parsed), "f")
    whole, dot, fractional = plain.partition(".")
    significant = (whole.lstrip("0") + fractional).lstrip("0") or "0"
    scale = len(fractional) if dot else 0
    if len(significant) > MAX_PRECISION:
        raise ValueValidationError(f"decimal precision exceeds {MAX_PRECISION} digits")
    if scale > MAX_SCALE:
        raise ValueValidationError(f"decimal scale exceeds {MAX_SCALE} digits")
    return Decimal(0) if parsed == 0 else parsed


def canonical_decimal(value: Decimal) -> str:
    """Serialize a finite Decimal in minimal plain notation."""

    parsed = parse_decimal(value, allow_negative=True)
    if parsed == 0:
        return "0"
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def parse_utc_timestamp(value: str) -> datetime:
    """Parse the fixture's whole-second RFC 3339 UTC representation."""

    if not isinstance(value, str):
        raise ValueValidationError("timestamp must be a string")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise ValueValidationError("timestamp must be whole-second UTC ending in Z") from error
    return parsed


def canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueValidationError("timestamp must be timezone-aware")
    utc_value = value.astimezone(UTC)
    timespec = "microseconds" if utc_value.microsecond else "seconds"
    return utc_value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _canonicalize(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise ValueValidationError("canonical data cannot contain binary floats")
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        return canonical_timestamp(value)
    if isinstance(value, Enum):
        return _canonicalize(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonicalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueValidationError("canonical object keys must be strings")
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    raise ValueValidationError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_digest(value: object) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def deterministic_id(namespace: str, value: object) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_]*", namespace) is None:
        raise ValueValidationError("identifier namespace is invalid")
    return f"{namespace}_{content_digest(value)[:24]}"


def floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueValidationError("quantity candidate must be a finite non-negative Decimal")
    parsed_increment = parse_decimal(increment, allow_zero=False)
    steps = (value / parsed_increment).to_integral_value(rounding=ROUND_FLOOR)
    return parse_decimal(steps * parsed_increment)


@dataclass(frozen=True, slots=True, init=False)
class Money:
    amount: Decimal
    currency: str

    def __init__(self, amount: DecimalInput, currency: str = "USD") -> None:
        if currency != "USD":
            raise ValueValidationError("the golden slice supports USD only")
        object.__setattr__(self, "amount", parse_decimal(amount))
        object.__setattr__(self, "currency", currency)


@dataclass(frozen=True, slots=True, init=False)
class Price:
    value: Decimal
    tick: Decimal

    def __init__(self, value: DecimalInput, tick: DecimalInput) -> None:
        parsed_value = parse_decimal(value)
        parsed_tick = parse_decimal(tick, allow_zero=False)
        if parsed_value > 1:
            raise ValueValidationError("prediction-market prices cannot exceed one")
        if parsed_value % parsed_tick != 0:
            raise ValueValidationError("price is not aligned to its tick")
        object.__setattr__(self, "value", parsed_value)
        object.__setattr__(self, "tick", parsed_tick)


@dataclass(frozen=True, slots=True, init=False)
class Quantity:
    value: Decimal
    increment: Decimal

    def __init__(self, value: DecimalInput, increment: DecimalInput) -> None:
        parsed_value = parse_decimal(value)
        parsed_increment = parse_decimal(increment, allow_zero=False)
        if parsed_value % parsed_increment != 0:
            raise ValueValidationError("quantity is not aligned to its increment")
        object.__setattr__(self, "value", parsed_value)
        object.__setattr__(self, "increment", parsed_increment)


@dataclass(frozen=True, slots=True, init=False)
class Probability:
    value: Decimal

    def __init__(self, value: DecimalInput) -> None:
        parsed = parse_decimal(value)
        if parsed > 1:
            raise ValueValidationError("probability must be between zero and one")
        object.__setattr__(self, "value", parsed)


@dataclass(frozen=True, slots=True, init=False)
class ProbabilityVector:
    yes: Probability
    no: Probability
    void: Probability

    def __init__(
        self,
        yes: Probability | DecimalInput,
        no: Probability | DecimalInput,
        void: Probability | DecimalInput,
    ) -> None:
        parsed_yes = yes if isinstance(yes, Probability) else Probability(yes)
        parsed_no = no if isinstance(no, Probability) else Probability(no)
        parsed_void = void if isinstance(void, Probability) else Probability(void)
        if parsed_yes.value + parsed_no.value + parsed_void.value != Decimal("1"):
            raise ValueValidationError("probability vector must sum exactly to one")
        object.__setattr__(self, "yes", parsed_yes)
        object.__setattr__(self, "no", parsed_no)
        object.__setattr__(self, "void", parsed_void)


@dataclass(frozen=True, slots=True, init=False)
class RiskFraction:
    value: Decimal

    def __init__(self, value: DecimalInput) -> None:
        parsed = parse_decimal(value)
        if parsed > 1:
            raise ValueValidationError("risk fraction must be between zero and one")
        object.__setattr__(self, "value", parsed)


@dataclass(frozen=True, slots=True)
class InformationCutoff:
    value: datetime

    def __post_init__(self) -> None:
        if self.value.tzinfo is None or self.value.utcoffset() is None:
            raise ValueValidationError("information cutoff must be timezone-aware")
        object.__setattr__(self, "value", self.value.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class ContentHash:
    value: str

    def __post_init__(self) -> None:
        if _HASH_PATTERN.fullmatch(self.value) is None:
            raise ValueValidationError("content hash must be a lowercase SHA-256 digest")
