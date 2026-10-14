from __future__ import annotations

from decimal import Decimal

import pytest

from apex.domain.values import (
    Money,
    Price,
    Probability,
    ProbabilityVector,
    Quantity,
    ValueValidationError,
    canonical_decimal,
    canonical_json_bytes,
    parse_decimal,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("0", Decimal("0")),
        ("1000.00", Decimal("1000.00")),
        (7, Decimal("7")),
        (Decimal("0.125"), Decimal("0.125")),
    ),
)
def test_authoritative_decimal_inputs(source: str | int | Decimal, expected: Decimal) -> None:
    assert parse_decimal(source) == expected


@pytest.mark.parametrize(
    "source",
    (
        True,
        False,
        0.1,
        float("nan"),
        float("inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        "NaN",
        "Infinity",
        "1e-2",
        "1E2",
        "+1",
        "01",
        " 1",
        "1 ",
        "1.",
        ".1",
        "1.000000000",
        "1234567890123456789",
    ),
)
def test_invalid_authoritative_decimals_are_rejected(source: object) -> None:
    with pytest.raises(ValueValidationError):
        parse_decimal(source)  # type: ignore[arg-type]


def test_negative_money_and_binary_float_are_rejected() -> None:
    with pytest.raises(ValueValidationError):
        Money("-0.01")
    with pytest.raises(ValueValidationError):
        Money(0.1)


@pytest.mark.parametrize("value", ("-0.01", "1.01"))
def test_probability_bounds(value: str) -> None:
    with pytest.raises(ValueValidationError):
        Probability(value)


def test_probability_vector_requires_exact_sum() -> None:
    assert ProbabilityVector("0.70", "0.25", "0.05").yes.value == Decimal("0.70")
    with pytest.raises(ValueValidationError):
        ProbabilityVector("0.70", "0.25", "0.04")


def test_price_and_quantity_must_align_to_boundaries() -> None:
    assert Price("0.40", "0.01").value == Decimal("0.40")
    assert Quantity("12", "1").value == Decimal("12")
    with pytest.raises(ValueValidationError):
        Price("0.405", "0.01")
    with pytest.raises(ValueValidationError):
        Quantity("1.5", "1")


@pytest.mark.parametrize(
    ("value", "rendered"),
    (
        (Decimal("0.000"), "0"),
        (Decimal("-0.00"), "0"),
        (Decimal("1000.00"), "1000"),
        (Decimal("0.125000"), "0.125"),
    ),
)
def test_decimal_serialization_is_plain_and_stable(value: Decimal, rendered: str) -> None:
    assert canonical_decimal(value) == rendered


def test_canonical_json_emits_authoritative_decimals_as_strings() -> None:
    assert canonical_json_bytes({"amount": Decimal("1.2300")}) == b'{"amount":"1.23"}'


# --------------------------------------------------------------------------
# Operating Daily Checkpoint Inspect Canonicalization Fix v1: a real
# production defect had a CLI read-model composer return a bare Python
# ``float`` (from ``round(100 * observed / considered, 2)`` -- ``int / int``
# is true, binary-float division in Python 3) nested inside an otherwise
# fully canonical dict. ``_print_autopilot`` renders every CLI command's
# output through this exact ``canonical_json_bytes`` path, so any bare float
# anywhere in the tree -- not just at the top level -- must still fail
# closed here. These tests pin that contract directly, independent of any
# one call site.
# --------------------------------------------------------------------------


def test_canonical_json_bytes_rejects_bare_binary_float_at_any_nesting_depth() -> None:
    with pytest.raises(ValueValidationError, match="canonical data cannot contain binary floats"):
        canonical_json_bytes({"outer": {"inner": 0.66}})


def test_canonical_json_bytes_still_rejects_nan_and_infinity() -> None:
    """The fix for the binary-float defect above must not weaken this
    orthogonal guard: neither a bare float NaN/Infinity nor a non-finite
    Decimal may ever reach rendered/hashed canonical JSON."""

    for non_finite_float in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(
            ValueValidationError, match="canonical data cannot contain binary floats"
        ):
            canonical_json_bytes({"x": non_finite_float})
    for non_finite_decimal in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueValidationError, match="must be finite"):
            canonical_json_bytes({"x": non_finite_decimal})


def test_canonical_json_bytes_accepts_the_real_retained_coverage_percentage_value() -> None:
    """The exact value Operating Daily Checkpoint Inspect Canonicalization
    Fix v1 now produces for the retained production cycle that originally
    crashed (11 observed / 1666 considered pairs) -- proves the corrected
    ``Decimal`` representation is both accepted and rendered with the same
    human-readable precision the broken ``float`` would have shown."""

    assert canonical_json_bytes({"coverage_percentage": Decimal("0.66")}) == (
        b'{"coverage_percentage":"0.66"}'
    )
