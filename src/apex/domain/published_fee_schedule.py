"""Immutable, source-bound provider fee contracts for read-only research.

This module models only the trade-fee arithmetic published by a provider.  It
does not model account balance rounding, rebates, account tiering, fills, or
execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from types import MappingProxyType

from apex.domain.values import ContentHash, ValueValidationError, content_digest, parse_decimal

CENT = Decimal("0.01")
CENTICENT = Decimal("0.0001")
ONE = Decimal("1")

KALSHI_FEE_SCHEDULE_SOURCE_URL = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
KALSHI_FEE_SCHEDULE_SOURCE_SHA256 = (
    "815e2d5127d02d2fb90773d1a3844dc15a987696171eddc4e58de87b59c6124c"
)
KALSHI_FEE_SCHEDULE_IDENTITY = "kalshi-fee-schedule-2026-07-07"
KALSHI_PUBLISHED_FEE_CONTRACT_IDENTITY = "kalshi-published-fee-contract-2026-07-07-v1"
KALSHI_FEE_SCHEDULE_EFFECTIVE_DATE = "2026-07-07"
KALSHI_TAKER_FEE_FORMULA_IDENTITY = (
    "multiplier-times-0.07-times-contracts-times-p-times-one-minus-p"
)
KALSHI_MAKER_FEE_FORMULA_IDENTITY = (
    "multiplier-times-0.0175-times-contracts-times-p-times-one-minus-p"
)
KALSHI_TRADE_FEE_ROUNDING_IDENTITY = "fee-plus-position-cost-centicent-ceiling-v1"
KALSHI_ACCOUNT_ROUNDING_TREATMENT = "NOT_MODELED"
KALSHI_SERIES_FEE_TYPE = "quadratic_with_maker_fees"
KALSHI_PUBLISHED_FEE_APPLICABILITY_VERSION = "kalshi-published-fee-applicability-v1"


class FeeScheduleApplicabilityState(StrEnum):
    """Source-bound conclusions about one retained provider fee record."""

    EXACT_SCHEDULE_MATCH = "EXACT_SCHEDULE_MATCH"
    LEGACY_SCHEDULE_MATCH = "LEGACY_SCHEDULE_MATCH"
    API_LABEL_ALIAS_PROVEN = "API_LABEL_ALIAS_PROVEN"
    SCHEDULE_NOT_APPLICABLE = "SCHEDULE_NOT_APPLICABLE"
    MULTIPLIER_TIME_AMBIGUOUS = "MULTIPLIER_TIME_AMBIGUOUS"
    AUTHORITATIVE_MAPPING_MISSING = "AUTHORITATIVE_MAPPING_MISSING"
    CONFLICTING_FEE_EVIDENCE = "CONFLICTING_FEE_EVIDENCE"


class FeeMultiplierSource(StrEnum):
    """The exact July 2026 PDF term that supplies a series multiplier."""

    PDF_GENERAL_DEFAULT = "PDF_GENERAL_DEFAULT"
    PDF_NON_STANDARD_SERIES_TABLE = "PDF_NON_STANDARD_SERIES_TABLE"


# Transcribed from pages 6--11 of the source-bound July 7, 2026 PDF.  The
# document first states that its general fee terms apply to every event-contract
# market except the following exact series, then provides maker/taker overrides.
# The mapping is deliberately exact-ticker only: it never relies on a prefix,
# title, category, or an API fee-type label.
KALSHI_PUBLISHED_NON_STANDARD_SERIES_MULTIPLIERS = MappingProxyType(
    {
        "KXAAAGASM": (Decimal("1"), Decimal("1")),
        "KXATPMATCH": (Decimal("1"), Decimal("1")),
        "KXBALLONDOR": (Decimal("1"), Decimal("1")),
        "KXBTCMAX150": (Decimal("1"), Decimal("1")),
        "KXBTCY": (Decimal("0"), Decimal("0")),
        "KXCITRINI": (Decimal("0"), Decimal("0")),
        "KXCPI": (Decimal("1"), Decimal("1")),
        "KXCPIYOY": (Decimal("1"), Decimal("1")),
        "KXDOED": (Decimal("0"), Decimal("0")),
        "KXEGGS": (Decimal("1"), Decimal("1")),
        "KXELECTIRAN": (Decimal("0"), Decimal("0")),
        "KXEMMYCACTO": (Decimal("1"), Decimal("1")),
        "KXEMMYCACTR": (Decimal("1"), Decimal("1")),
        "KXEMMYCSERIES": (Decimal("1"), Decimal("1")),
        "KXEMMYDACTO": (Decimal("1"), Decimal("1")),
        "KXEMMYDACTR": (Decimal("1"), Decimal("1")),
        "KXEMMYDSERIES": (Decimal("1"), Decimal("1")),
        "KXETHY": (Decimal("0"), Decimal("0")),
        "KXFED": (Decimal("1"), Decimal("1")),
        "KXFEDDECISION": (Decimal("1"), Decimal("1")),
        "KXGAMBLINGREPEAL": (Decimal("0"), Decimal("0")),
        "KXGDP": (Decimal("1"), Decimal("1")),
        "KXGREENLAND": (Decimal("0"), Decimal("0")),
        "KXHEISMAN": (Decimal("1"), Decimal("1")),
        "KXINXY": (Decimal("1"), Decimal("1")),
        "KXIPO": (Decimal("1"), Decimal("1")),
        "KXIRANDEMOCRACY": (Decimal("0"), Decimal("0")),
        "KXLALIGA": (Decimal("1"), Decimal("1")),
        "KXLAYOFFSYINFO": (Decimal("0"), Decimal("0")),
        "KXLLM1": (Decimal("1"), Decimal("1")),
        "KXMARMAD": (Decimal("1"), Decimal("1")),
        "KXMENWORLDCUP": (Decimal("1"), Decimal("1")),
        "KXMLB": (Decimal("1"), Decimal("1")),
        "KXMLBAL": (Decimal("1"), Decimal("1")),
        "KXMLBASGAME": (Decimal("1"), Decimal("1")),
        "KXMLBGAME": (Decimal("1"), Decimal("1")),
        "KXMLBNL": (Decimal("1"), Decimal("1")),
        "KXNASDAQ100Y": (Decimal("1"), Decimal("1")),
        "KXNBA": (Decimal("1"), Decimal("1")),
        "KXNBAEAST": (Decimal("1"), Decimal("1")),
        "KXNBAMVP": (Decimal("1"), Decimal("1")),
        "KXNBAROY": (Decimal("1"), Decimal("1")),
        "KXNBAWEST": (Decimal("1"), Decimal("1")),
        "KXNCAAF": (Decimal("1"), Decimal("1")),
        "KXNCAAFACC": (Decimal("1"), Decimal("1")),
        "KXNCAAFB10": (Decimal("1"), Decimal("1")),
        "KXNCAAFB12": (Decimal("1"), Decimal("1")),
        "KXNCAAFGAME": (Decimal("1"), Decimal("1")),
        "KXNCAAFPLAYOFF": (Decimal("1"), Decimal("1")),
        "KXNCAAFSEC": (Decimal("1"), Decimal("1")),
        "KXNFLAFCCHAMP": (Decimal("1"), Decimal("1")),
        "KXNFLAFCEAST": (Decimal("1"), Decimal("1")),
        "KXNFLAFCNORTH": (Decimal("1"), Decimal("1")),
        "KXNFLAFCSOUTH": (Decimal("1"), Decimal("1")),
        "KXNFLAFCWEST": (Decimal("1"), Decimal("1")),
        "KXNFLCOTY": (Decimal("1"), Decimal("1")),
        "KXNFLCPOTY": (Decimal("1"), Decimal("1")),
        "KXNFLDPOTY": (Decimal("1"), Decimal("1")),
        "KXNFLDROTY": (Decimal("1"), Decimal("1")),
        "KXNFLGAME": (Decimal("1"), Decimal("1")),
        "KXNFLMVP": (Decimal("1"), Decimal("1")),
        "KXNFLNFCCHAMP": (Decimal("1"), Decimal("1")),
        "KXNFLNFCEAST": (Decimal("1"), Decimal("1")),
        "KXNFLNFCNORTH": (Decimal("1"), Decimal("1")),
        "KXNFLNFCSOUTH": (Decimal("1"), Decimal("1")),
        "KXNFLNFCWEST": (Decimal("1"), Decimal("1")),
        "KXNFLOPOTY": (Decimal("1"), Decimal("1")),
        "KXNFLOROTY": (Decimal("1"), Decimal("1")),
        "KXNHL": (Decimal("1"), Decimal("1")),
        "KXNHLEAST": (Decimal("1"), Decimal("1")),
        "KXNHLWEST": (Decimal("1"), Decimal("1")),
        "KXPAHLAVIHEAD": (Decimal("0"), Decimal("0")),
        "KXPAYROLLS": (Decimal("1"), Decimal("1")),
        "KXPGARYDER": (Decimal("1"), Decimal("1")),
        "KXPGASOLHEIM": (Decimal("1"), Decimal("1")),
        "KXPGATOUR": (Decimal("1"), Decimal("1")),
        "KXRATECUTCOUNT": (Decimal("1"), Decimal("1")),
        "KXSB": (Decimal("1"), Decimal("1")),
        "KXSUPERBOWLHEADLINE": (Decimal("1"), Decimal("1")),
        "KXU3": (Decimal("1"), Decimal("1")),
        "KXUCL": (Decimal("1"), Decimal("1")),
        "KXUCLGAME": (Decimal("1"), Decimal("1")),
        "KXWCGAME": (Decimal("1"), Decimal("1")),
        "KXWNBA": (Decimal("1"), Decimal("1")),
        "KXWNBAGAME": (Decimal("1"), Decimal("1")),
        "KXWTAMATCH": (Decimal("1"), Decimal("1")),
    }
)


class FeeLiquidityRole(StrEnum):
    """The published fee role.  Immediate-cross research always uses TAKER."""

    TAKER = "TAKER"
    MAKER = "MAKER"


def _ceil_to_centicent(value: Decimal) -> Decimal:
    return (value / CENTICENT).to_integral_value(rounding=ROUND_CEILING) * CENTICENT


@dataclass(frozen=True, slots=True)
class PublishedProviderFeeSchedule:
    """One immutable provider document contract with exact trade-fee arithmetic."""

    provider: str
    contract_identity: str
    schedule_identity: str
    document_title: str
    effective_date: str
    source_url: str
    source_sha256: str
    provider_fee_type: str
    taker_coefficient: Decimal
    maker_coefficient: Decimal
    taker_formula_identity: str
    maker_formula_identity: str
    trade_fee_rounding_identity: str
    account_rounding_treatment: str
    multiplier_semantics: str
    contract_count_semantics: str
    price_semantics: str

    def __post_init__(self) -> None:
        for label, value, maximum in (
            ("provider", self.provider, 30),
            ("contract identity", self.contract_identity, 160),
            ("schedule identity", self.schedule_identity, 160),
            ("document title", self.document_title, 300),
            ("effective date", self.effective_date, 20),
            ("source URL", self.source_url, 500),
            ("provider fee type", self.provider_fee_type, 120),
            ("taker formula identity", self.taker_formula_identity, 200),
            ("maker formula identity", self.maker_formula_identity, 200),
            ("trade fee rounding identity", self.trade_fee_rounding_identity, 200),
            ("account rounding treatment", self.account_rounding_treatment, 120),
            ("multiplier semantics", self.multiplier_semantics, 300),
            ("contract count semantics", self.contract_count_semantics, 300),
            ("price semantics", self.price_semantics, 300),
        ):
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise ValueValidationError(f"published fee {label} is invalid")
        if not self.source_url.startswith("https://kalshi.com/"):
            raise ValueValidationError("published fee source URL must be a Kalshi HTTPS URL")
        try:
            ContentHash(self.source_sha256)
        except ValueValidationError as error:
            raise ValueValidationError("published fee source hash is invalid") from error
        for name in ("taker_coefficient", "maker_coefficient"):
            coefficient = parse_decimal(getattr(self, name), allow_zero=False)
            object.__setattr__(self, name, coefficient)

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "contract_identity": self.contract_identity,
            "schedule_identity": self.schedule_identity,
            "document_title": self.document_title,
            "effective_date": self.effective_date,
            "source_url": self.source_url,
            "source_sha256": self.source_sha256,
            "provider_fee_type": self.provider_fee_type,
            "taker_coefficient": self.taker_coefficient,
            "maker_coefficient": self.maker_coefficient,
            "taker_formula_identity": self.taker_formula_identity,
            "maker_formula_identity": self.maker_formula_identity,
            "trade_fee_rounding_identity": self.trade_fee_rounding_identity,
            "account_rounding_treatment": self.account_rounding_treatment,
            "multiplier_semantics": self.multiplier_semantics,
            "contract_count_semantics": self.contract_count_semantics,
            "price_semantics": self.price_semantics,
        }

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self.canonical_payload))

    def trade_fee(
        self,
        *,
        role: FeeLiquidityRole,
        multiplier: Decimal,
        contract_count: Decimal,
        price: Decimal,
    ) -> Decimal:
        """Apply the published per-trade fee rounding rule exactly.

        The July 2026 document rounds the combined position cost and fee up to
        a centicent, so the fee is the resulting total less the exact position
        cost.  Account/balance rounding is intentionally outside this method.
        """

        if not isinstance(role, FeeLiquidityRole):
            raise ValueValidationError("published fee role is invalid")
        parsed_multiplier = parse_decimal(multiplier)
        parsed_count = parse_decimal(contract_count, allow_zero=False)
        parsed_price = parse_decimal(price)
        if parsed_count != parsed_count.to_integral_value():
            raise ValueValidationError("published fee contract count must be an integer")
        if not Decimal(0) <= parsed_price <= ONE or parsed_price % CENT != 0:
            raise ValueValidationError("published fee price must be a cent-aligned binary price")
        coefficient = (
            self.taker_coefficient if role is FeeLiquidityRole.TAKER else self.maker_coefficient
        )
        position_cost = parsed_count * parsed_price
        raw_fee = (
            parsed_multiplier * coefficient * parsed_count * parsed_price * (ONE - parsed_price)
        )
        return parse_decimal(_ceil_to_centicent(position_cost + raw_fee) - position_cost)


KALSHI_PUBLISHED_FEE_SCHEDULE = PublishedProviderFeeSchedule(
    provider="KALSHI",
    contract_identity=KALSHI_PUBLISHED_FEE_CONTRACT_IDENTITY,
    schedule_identity=KALSHI_FEE_SCHEDULE_IDENTITY,
    document_title="Fee Schedule for July 2026 - 7.7.26 Update",
    effective_date=KALSHI_FEE_SCHEDULE_EFFECTIVE_DATE,
    source_url=KALSHI_FEE_SCHEDULE_SOURCE_URL,
    source_sha256=KALSHI_FEE_SCHEDULE_SOURCE_SHA256,
    provider_fee_type=KALSHI_SERIES_FEE_TYPE,
    taker_coefficient=Decimal("0.07"),
    maker_coefficient=Decimal("0.0175"),
    taker_formula_identity=KALSHI_TAKER_FEE_FORMULA_IDENTITY,
    maker_formula_identity=KALSHI_MAKER_FEE_FORMULA_IDENTITY,
    trade_fee_rounding_identity=KALSHI_TRADE_FEE_ROUNDING_IDENTITY,
    account_rounding_treatment=KALSHI_ACCOUNT_ROUNDING_TREATMENT,
    multiplier_semantics=(
        "M is the series-specific maker or taker multiplier; default 1 taker and 0 maker "
        "unless otherwise indicated"
    ),
    contract_count_semantics="C is the number of contracts being traded",
    price_semantics="P is the contract price in dollars; 50 cents is 0.5",
)


@dataclass(frozen=True, slots=True)
class PublishedFeeScheduleApplicability:
    """One reproducible decision that a retained fee fact uses the July schedule.

    The provider label remains evidence, not the schedule authority.  This
    value binds the exact series, the source document, the applicable schedule
    term, its effective interval, and the immediate-cross taker role.
    """

    state: FeeScheduleApplicabilityState
    version: str
    provider_fee_type: str | None
    series_ticker: str
    published_schedule_identity: str
    published_schedule_source_sha256: str
    multiplier_source: FeeMultiplierSource | None
    taker_multiplier: Decimal | None
    maker_multiplier: Decimal | None
    fee_effective_at: datetime | None
    schedule_effective_date: str
    taker_formula_identity: str
    maker_formula_identity: str
    trade_fee_rounding_identity: str
    applied_liquidity_role: FeeLiquidityRole

    def __post_init__(self) -> None:
        if self.version != KALSHI_PUBLISHED_FEE_APPLICABILITY_VERSION:
            raise ValueValidationError("published fee applicability version is invalid")
        if not self.series_ticker or len(self.series_ticker) > 300:
            raise ValueValidationError("published fee applicability series ticker is invalid")
        if not self.published_schedule_identity or not self.schedule_effective_date:
            raise ValueValidationError("published fee applicability schedule identity is invalid")
        try:
            ContentHash(self.published_schedule_source_sha256)
        except ValueValidationError as error:
            raise ValueValidationError(
                "published fee applicability source hash is invalid"
            ) from error
        if self.provider_fee_type is not None and (
            not self.provider_fee_type or len(self.provider_fee_type) > 120
        ):
            raise ValueValidationError("published fee applicability provider type is invalid")
        if self.fee_effective_at is not None:
            if self.fee_effective_at.tzinfo is None or self.fee_effective_at.utcoffset() is None:
                raise ValueValidationError("published fee applicability effective time is invalid")
            object.__setattr__(self, "fee_effective_at", self.fee_effective_at.astimezone(UTC))
        for field in ("taker_multiplier", "maker_multiplier"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, parse_decimal(value))
        if self.is_exact_taker_compatible:
            if (
                self.multiplier_source is None
                or self.taker_multiplier is None
                or self.maker_multiplier is None
            ):
                raise ValueValidationError("exact fee applicability requires multiplier evidence")
        elif any(
            value is not None
            for value in (self.multiplier_source, self.taker_multiplier, self.maker_multiplier)
        ):
            raise ValueValidationError("rejected fee applicability cannot retain multiplier terms")

    @property
    def is_exact_taker_compatible(self) -> bool:
        return self.state in {
            FeeScheduleApplicabilityState.EXACT_SCHEDULE_MATCH,
            FeeScheduleApplicabilityState.API_LABEL_ALIAS_PROVEN,
        }

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "version": self.version,
            "provider_fee_type": self.provider_fee_type,
            "series_ticker": self.series_ticker,
            "published_schedule_identity": self.published_schedule_identity,
            "published_schedule_source_sha256": self.published_schedule_source_sha256,
            "multiplier_source": (
                self.multiplier_source.value if self.multiplier_source is not None else None
            ),
            "taker_multiplier": self.taker_multiplier,
            "maker_multiplier": self.maker_multiplier,
            "fee_effective_at": self.fee_effective_at,
            "schedule_effective_date": self.schedule_effective_date,
            "taker_formula_identity": self.taker_formula_identity,
            "maker_formula_identity": self.maker_formula_identity,
            "trade_fee_rounding_identity": self.trade_fee_rounding_identity,
            "applied_liquidity_role": self.applied_liquidity_role.value,
        }

    @property
    def canonical_hash(self) -> ContentHash:
        return ContentHash(content_digest(self.canonical_payload))


def reconcile_kalshi_published_fee_schedule(
    *,
    provider_fee_type: str | None,
    series_ticker: str,
    fee_multiplier: Decimal | None,
    fee_effective_at: datetime | None,
    decision_at: datetime,
    fee_model_identity: str,
    taker_coefficient: Decimal,
    maker_coefficient: Decimal,
    retained_rounding_identity: str,
    schedule_identity: str,
    schedule_effective_date: str,
    expected_fee_model_identity: str,
    expected_retained_rounding_identity: str,
    schedule: PublishedProviderFeeSchedule = KALSHI_PUBLISHED_FEE_SCHEDULE,
) -> PublishedFeeScheduleApplicability:
    """Resolve one current provider record against the exact published schedule.

    ``quadratic`` is accepted only after the exact series is proved to use the
    PDF's general/default term or its exact table override.  The literal label
    itself proves nothing and is preserved in the returned evidence.
    """

    if decision_at.tzinfo is None or decision_at.utcoffset() is None:
        raise ValueValidationError("fee applicability decision time must be timezone-aware")
    decision = decision_at.astimezone(UTC)
    if not series_ticker or len(series_ticker) > 300:
        raise ValueValidationError("fee applicability series ticker is invalid")

    def result(
        state: FeeScheduleApplicabilityState,
        *,
        source: FeeMultiplierSource | None = None,
        taker: Decimal | None = None,
        maker: Decimal | None = None,
    ) -> PublishedFeeScheduleApplicability:
        return PublishedFeeScheduleApplicability(
            state=state,
            version=KALSHI_PUBLISHED_FEE_APPLICABILITY_VERSION,
            provider_fee_type=provider_fee_type,
            series_ticker=series_ticker,
            published_schedule_identity=schedule.schedule_identity,
            published_schedule_source_sha256=schedule.source_sha256,
            multiplier_source=source,
            taker_multiplier=taker,
            maker_multiplier=maker,
            fee_effective_at=fee_effective_at,
            schedule_effective_date=schedule.effective_date,
            taker_formula_identity=schedule.taker_formula_identity,
            maker_formula_identity=schedule.maker_formula_identity,
            trade_fee_rounding_identity=schedule.trade_fee_rounding_identity,
            applied_liquidity_role=FeeLiquidityRole.TAKER,
        )

    if (
        schedule != KALSHI_PUBLISHED_FEE_SCHEDULE
        or schedule.source_sha256 != KALSHI_FEE_SCHEDULE_SOURCE_SHA256
        or schedule.schedule_identity != KALSHI_FEE_SCHEDULE_IDENTITY
    ):
        return result(FeeScheduleApplicabilityState.AUTHORITATIVE_MAPPING_MISSING)
    if (
        fee_effective_at is None
        or fee_effective_at.tzinfo is None
        or fee_effective_at.utcoffset() is None
    ):
        return result(FeeScheduleApplicabilityState.MULTIPLIER_TIME_AMBIGUOUS)
    effective_at = fee_effective_at.astimezone(UTC)
    if effective_at > decision or schedule.effective_date > decision.date().isoformat():
        return result(FeeScheduleApplicabilityState.MULTIPLIER_TIME_AMBIGUOUS)
    if (
        schedule_identity != schedule.schedule_identity
        or schedule_effective_date != schedule.effective_date
        or fee_model_identity != expected_fee_model_identity
        or parse_decimal(taker_coefficient) != schedule.taker_coefficient
        or parse_decimal(maker_coefficient) != schedule.maker_coefficient
        or retained_rounding_identity != expected_retained_rounding_identity
    ):
        return result(FeeScheduleApplicabilityState.CONFLICTING_FEE_EVIDENCE)
    if fee_multiplier is None:
        return result(FeeScheduleApplicabilityState.AUTHORITATIVE_MAPPING_MISSING)
    multiplier = parse_decimal(fee_multiplier)
    explicit = KALSHI_PUBLISHED_NON_STANDARD_SERIES_MULTIPLIERS.get(series_ticker)
    if explicit is None:
        source = FeeMultiplierSource.PDF_GENERAL_DEFAULT
        maker, taker = Decimal("0"), Decimal("1")
    else:
        source = FeeMultiplierSource.PDF_NON_STANDARD_SERIES_TABLE
        maker, taker = explicit
    if multiplier != taker:
        return result(FeeScheduleApplicabilityState.CONFLICTING_FEE_EVIDENCE)
    if provider_fee_type == KALSHI_SERIES_FEE_TYPE:
        state = FeeScheduleApplicabilityState.EXACT_SCHEDULE_MATCH
    elif provider_fee_type == "quadratic":
        state = FeeScheduleApplicabilityState.API_LABEL_ALIAS_PROVEN
    elif provider_fee_type is None:
        return result(FeeScheduleApplicabilityState.AUTHORITATIVE_MAPPING_MISSING)
    else:
        return result(FeeScheduleApplicabilityState.SCHEDULE_NOT_APPLICABLE)
    return result(state, source=source, taker=taker, maker=maker)
