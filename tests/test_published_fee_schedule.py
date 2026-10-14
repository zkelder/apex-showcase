from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apex.domain.public_complement import FEE_MODEL_IDENTITY, ROUNDING_IDENTITY, SCHEDULE_IDENTITY
from apex.domain.published_fee_schedule import (
    KALSHI_FEE_SCHEDULE_SOURCE_SHA256,
    KALSHI_FEE_SCHEDULE_SOURCE_URL,
    KALSHI_PUBLISHED_FEE_SCHEDULE,
    FeeLiquidityRole,
    FeeMultiplierSource,
    FeeScheduleApplicabilityState,
    reconcile_kalshi_published_fee_schedule,
)
from apex.domain.values import ValueValidationError

NOW = datetime(2026, 7, 26, 20, tzinfo=UTC)


def _reconcile(**overrides: object):
    values: dict[str, object] = {
        "provider_fee_type": "quadratic",
        "series_ticker": "KXMLBF3",
        "fee_multiplier": Decimal("1"),
        "fee_effective_at": datetime(2026, 6, 30, 21, 40, tzinfo=UTC),
        "decision_at": NOW,
        "fee_model_identity": FEE_MODEL_IDENTITY,
        "taker_coefficient": Decimal("0.07"),
        "maker_coefficient": Decimal("0.0175"),
        "retained_rounding_identity": ROUNDING_IDENTITY,
        "schedule_identity": SCHEDULE_IDENTITY,
        "schedule_effective_date": "2026-07-07",
        "expected_fee_model_identity": FEE_MODEL_IDENTITY,
        "expected_retained_rounding_identity": ROUNDING_IDENTITY,
    }
    values.update(overrides)
    return reconcile_kalshi_published_fee_schedule(**values)


def test_verified_pdf_contract_binds_exact_coefficients_source_and_terms() -> None:
    schedule = KALSHI_PUBLISHED_FEE_SCHEDULE

    assert schedule.source_url == KALSHI_FEE_SCHEDULE_SOURCE_URL
    assert schedule.source_sha256 == KALSHI_FEE_SCHEDULE_SOURCE_SHA256
    assert schedule.effective_date == "2026-07-07"
    assert schedule.taker_coefficient == Decimal("0.07")
    assert schedule.maker_coefficient == Decimal("0.0175")
    assert (
        schedule.canonical_hash.value
        == "6450fd1c13526965a8c553736bdb6b724081b40dc36aa98268979d45fa1007e9"
    )


def test_published_trade_fee_formula_multiplier_and_centicent_rounding_are_exact() -> None:
    schedule = KALSHI_PUBLISHED_FEE_SCHEDULE

    assert schedule.trade_fee(
        role=FeeLiquidityRole.TAKER,
        multiplier=Decimal("1"),
        contract_count=Decimal("100"),
        price=Decimal("0.50"),
    ) == Decimal("1.75")
    assert schedule.trade_fee(
        role=FeeLiquidityRole.MAKER,
        multiplier=Decimal("1"),
        contract_count=Decimal("100"),
        price=Decimal("0.50"),
    ) == Decimal("0.4375")
    assert schedule.trade_fee(
        role=FeeLiquidityRole.TAKER,
        multiplier=Decimal("2"),
        contract_count=Decimal("1"),
        price=Decimal("0.01"),
    ) == Decimal("0.0014")
    assert schedule.trade_fee(
        role=FeeLiquidityRole.MAKER,
        multiplier=Decimal("0"),
        contract_count=Decimal("1"),
        price=Decimal("0.50"),
    ) == Decimal("0")


def test_published_fee_rejects_fractional_contract_count_and_non_cent_price() -> None:
    schedule = KALSHI_PUBLISHED_FEE_SCHEDULE

    with pytest.raises(ValueValidationError, match="contract count"):
        schedule.trade_fee(
            role=FeeLiquidityRole.TAKER,
            multiplier=Decimal("1"),
            contract_count=Decimal("1.5"),
            price=Decimal("0.50"),
        )
    with pytest.raises(ValueValidationError, match="cent-aligned"):
        schedule.trade_fee(
            role=FeeLiquidityRole.TAKER,
            multiplier=Decimal("1"),
            contract_count=Decimal("1"),
            price=Decimal("0.505"),
        )


def test_provider_label_and_published_schedule_identity_are_separate() -> None:
    applicability = _reconcile()

    assert applicability.provider_fee_type == "quadratic"
    assert applicability.published_schedule_identity == "kalshi-fee-schedule-2026-07-07"
    assert applicability.published_schedule_source_sha256 == KALSHI_FEE_SCHEDULE_SOURCE_SHA256
    assert applicability.state is FeeScheduleApplicabilityState.API_LABEL_ALIAS_PROVEN
    assert applicability.is_exact_taker_compatible
    assert applicability.multiplier_source is FeeMultiplierSource.PDF_GENERAL_DEFAULT
    assert applicability.taker_multiplier == Decimal("1")
    assert applicability.maker_multiplier == Decimal("0")


def test_exact_non_standard_multiplier_and_effective_time_are_required() -> None:
    applicability = _reconcile(series_ticker="KXBTCY", fee_multiplier=Decimal("0"))

    assert applicability.is_exact_taker_compatible
    assert applicability.multiplier_source is FeeMultiplierSource.PDF_NON_STANDARD_SERIES_TABLE
    assert applicability.taker_multiplier == Decimal("0")
    assert applicability.maker_multiplier == Decimal("0")

    ambiguous = _reconcile(fee_effective_at=None)
    assert ambiguous.state is FeeScheduleApplicabilityState.MULTIPLIER_TIME_AMBIGUOUS
    assert not ambiguous.is_exact_taker_compatible


def test_unproven_or_conflicting_label_evidence_remains_rejected() -> None:
    unproven = _reconcile(provider_fee_type=None)
    assert unproven.state is FeeScheduleApplicabilityState.AUTHORITATIVE_MAPPING_MISSING
    assert not unproven.is_exact_taker_compatible

    conflict = _reconcile(fee_multiplier=Decimal("0"))
    assert conflict.state is FeeScheduleApplicabilityState.CONFLICTING_FEE_EVIDENCE
    assert not conflict.is_exact_taker_compatible

    incompatible = _reconcile(provider_fee_type="flat")
    assert incompatible.state is FeeScheduleApplicabilityState.SCHEDULE_NOT_APPLICABLE
    assert not incompatible.is_exact_taker_compatible
